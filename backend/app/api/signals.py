"""
Trading Signal API Routes

Endpoints:
  GET /api/signals/current?graph_id=...
    Returns the latest signal per asset class, combining 24-hour sentiment
    scores with the most recent price data.

  GET /api/signals/history?graph_id=...&days=30
    Returns daily sentiment scores alongside price data for charting.

  GET /api/signals/backtest?graph_id=...&days=30
    Returns signal accuracy vs actual next-day price moves.

  GET /api/signals/sentiment_history?graph_id=...&hours=48
    Returns all SentimentSnapshot nodes from the last N hours (tick-level).

Signal thresholds:
  score > +0.4  → bullish
  score < -0.4  → bearish
  otherwise     → neutral
"""

import json
import math
import sys
import os
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from flask import Blueprint, request, jsonify

from .investors import _get_driver, compute_sentiment_scores
from ..utils.logger import get_logger

signals_bp = Blueprint("signals", __name__)
logger = get_logger("mirofish.api.signals")

_PRICES_DIR = Path(__file__).parent.parent.parent.parent / "backend" / "prices"
# Resolved at import time: <repo>/backend/prices
# Fallback: sibling to the app package root
_APP_ROOT = Path(__file__).parent.parent  # backend/app
_PRICES_DIR_ALT = _APP_ROOT.parent / "prices"  # backend/prices


def _prices_dir() -> Path:
    """Return the prices directory, trying both candidate paths."""
    if _PRICES_DIR.is_dir():
        return _PRICES_DIR
    return _PRICES_DIR_ALT


def _read_price_file(date_str: str) -> dict:
    """Read backend/prices/YYYY-MM-DD.json. Returns {} if not found."""
    p = _prices_dir() / f"{date_str}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _latest_prices() -> tuple[dict, str]:
    """
    Return (prices_dict, date_str) for the most recently written price file.
    prices_dict maps asset_class → float (or None).
    Returns ({}, "") if no files exist.
    """
    d = _prices_dir()
    if not d.is_dir():
        return {}, ""
    files = sorted(d.glob("????-??-??.json"), reverse=True)
    if not files:
        return {}, ""
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
        return data.get("prices", {}), data.get("date", files[0].stem)
    except Exception:
        return {}, ""


def _signal(score: float, threshold: float | None = None) -> str:
    t = threshold if threshold is not None else _THRESHOLD_FALLBACK
    if score > t:
        return "bullish"
    if score < -t:
        return "bearish"
    return "neutral"


def _price_staleness_hours() -> float | None:
    """
    Return age in hours of the most recent price file.
    Delegates to price_fetcher.compute_price_staleness_hours() — imported lazily
    to avoid circular dependency and handle the case where price_fetcher is not
    on sys.path when the Flask app starts.
    Returns None if price files don't exist or import fails.
    """
    try:
        scripts_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), '..', '..', 'scripts')
        )
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from price_fetcher import compute_price_staleness_hours
        return compute_price_staleness_hours()
    except Exception as e:
        logger.debug(f"price_staleness_hours unavailable: {e}")
        return None


# Exponential time-decay constant for 24h sentiment window.
# λ=0.05 gives half-life ≈ ln(2)/0.05 ≈ 13.9 hours, meaning an event
# from 14h ago contributes ~50% of the weight of a current event.
# Applied only to the 24h window via compute_sentiment_scores(apply_decay=True).
# The 7d window intentionally uses no decay — at 168h, exp(-0.05×168) ≈ 0.0002
# which would collapse all weight onto the last few hours.
_DECAY_LAMBDA = 0.05

# TIER 2C-ii: Volatility-aware signal thresholds.
# Threshold = max(_THRESHOLD_MIN, min(_THRESHOLD_MAX, 0.5 × std × sqrt(48)))
# based on the rolling stddev of the last 48 SentimentSnapshot scores per asset.
# If fewer than 10 snapshots exist for an asset, fall back to _THRESHOLD_FALLBACK.
_THRESHOLD_FALLBACK = 0.4
_THRESHOLD_MIN      = 0.25
_THRESHOLD_MAX      = 0.55

# Archetype groups for the smart/dumb money split (TIER 2C-iii).
_SMART_MONEY_ARCHETYPES = ['hedge_fund', 'prop_trader']
_DUMB_MONEY_ARCHETYPES  = ['retail_amateur']

# Query to fetch last 48 SentimentSnapshot scores per asset (used for dynamic thresholds).
_SNAPSHOT_THRESHOLD_QUERY = """
    MATCH (s:SentimentSnapshot {graph_id: $gid})
    RETURN
        s.equities    AS equities,
        s.crypto      AS crypto,
        s.bonds       AS bonds,
        s.commodities AS commodities,
        s.fx          AS fx,
        s.real_estate AS real_estate,
        s.mixed       AS mixed
    ORDER BY s.timestamp DESC LIMIT 48
"""

# Query for archetype-split sentiment (TIER 2C-iii).
_ARCHETYPE_SENTIMENT_QUERY = """
    MATCH (a:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
    WHERE m.timestamp >= $since AND m.timestamp < $until
      AND a.investor_archetype IN $archetypes
    RETURN
        a.asset_class_bias  AS asset_class,
        m.reaction          AS reaction,
        m.confidence        AS confidence,
        a.capital_usd       AS capital,
        a.leverage_typical  AS leverage,
        m.timestamp         AS timestamp,
        m.direction         AS direction,
        m.conviction        AS conviction,
        m.position_size     AS position_size
"""


def _compute_dynamic_thresholds(driver, graph_id: str) -> dict:
    """
    Compute per-asset dynamic signal thresholds from the last 48 SentimentSnapshots.

    Formula: threshold = max(_THRESHOLD_MIN, min(_THRESHOLD_MAX, 0.5 × std × sqrt(48)))
    High-volatility assets (e.g. crypto) get wider thresholds automatically;
    low-volatility assets (e.g. bonds) get tighter thresholds.

    Falls back to _THRESHOLD_FALLBACK per asset when fewer than 10 snapshots exist.
    """
    thresholds = {a: _THRESHOLD_FALLBACK for a in _ASSET_CLASSES}
    try:
        with driver.session() as session:
            rows = session.run(
                _SNAPSHOT_THRESHOLD_QUERY, gid=graph_id
            ).data()

        if len(rows) < 10:
            return thresholds  # not enough data for any asset — all fall back

        sqrt48 = math.sqrt(48)
        for asset in _ASSET_CLASSES:
            values = [r.get(asset) for r in rows if r.get(asset) is not None]
            if len(values) < 10:
                continue  # keep fallback for this asset
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / len(values)
            std = math.sqrt(variance)
            threshold = max(_THRESHOLD_MIN, min(_THRESHOLD_MAX, 0.5 * std * sqrt48))
            thresholds[asset] = round(threshold, 4)
    except Exception as e:
        logger.warning(f"dynamic_thresholds: computation failed, using fallback {_THRESHOLD_FALLBACK} ({e})")
    return thresholds


_SENTIMENT_QUERY = """
    MATCH (a:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
    WHERE m.timestamp >= $since AND m.timestamp < $until
    RETURN
        a.asset_class_bias  AS asset_class,
        m.reaction          AS reaction,
        m.confidence        AS confidence,
        a.capital_usd       AS capital,
        a.leverage_typical  AS leverage,
        m.timestamp         AS timestamp,
        m.direction         AS direction,
        m.conviction        AS conviction,
        m.position_size     AS position_size
"""

_ASSET_CLASSES = ["equities", "crypto", "bonds", "commodities", "fx", "real_estate", "mixed"]


@signals_bp.route("/current", methods=["GET"])
def current_signals():
    """
    Returns current trading signal per asset class.
    Combines 24-hour sentiment with the latest available price snapshot.
    """
    graph_id = request.args.get("graph_id")
    if not graph_id:
        return jsonify({"success": False, "error": "graph_id required"}), 400

    try:
        driver = _get_driver()
        now = datetime.now(timezone.utc)
        ts_24h = (now - timedelta(hours=24)).isoformat()
        ts_now = now.isoformat()

        with driver.session() as session:
            rows = session.run(
                _SENTIMENT_QUERY,
                gid=graph_id,
                since=ts_24h,
                until=ts_now,
            ).data()

        # TIER 2C: apply conviction model + exponential time-decay to 24h window.
        # Rows now include timestamp/direction/conviction/position_size/leverage so
        # compute_sentiment_scores can use the full conviction model + decay formula.
        scores = compute_sentiment_scores(rows, apply_leverage=True, apply_decay=True)

        # TIER 2C-ii: compute per-asset dynamic thresholds from last 48 snapshots
        dynamic_thresholds = _compute_dynamic_thresholds(driver, graph_id)

        # TIER 2C: event freshness — how old is the newest MemoryEvent in this window?
        # If agents haven't reacted in the last 36h, sentiment data is stale.
        event_degraded = True  # default: degraded if no events at all
        if rows:
            try:
                ts_values = []
                for r in rows:
                    raw = r.get("timestamp")
                    if raw:
                        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        ts_values.append(ts)
                if ts_values:
                    newest_event_ts = max(ts_values)
                    event_age_hours = (now - newest_event_ts).total_seconds() / 3600
                    event_degraded = event_age_hours > 36
            except Exception:
                event_degraded = False  # parse failure — assume fresh

        prices_today, today_str = _latest_prices()

        # Try to find yesterday's prices for 24h price change
        if today_str:
            yesterday_str = (date.fromisoformat(today_str) - timedelta(days=1)).isoformat()
            prices_yesterday = _read_price_file(yesterday_str).get("prices", {})
        else:
            prices_yesterday = {}

        # Data quality: degraded if price is stale OR events are stale
        staleness_hours = _price_staleness_hours()
        price_degraded  = staleness_hours is not None and staleness_hours > 36
        data_quality    = "degraded" if (price_degraded or event_degraded) else "fresh"

        signals = []
        all_assets = set(_ASSET_CLASSES) | set(scores.keys())
        for asset in sorted(all_assets):
            sentiment_data = scores.get(asset, {"score": 0.0, "event_count": 0})
            score       = sentiment_data["score"]
            event_count = sentiment_data["event_count"]
            price       = prices_today.get(asset)
            prev_price  = prices_yesterday.get(asset)

            price_change_24h = None
            if price is not None and prev_price:
                price_change_24h = round((price - prev_price) / prev_price * 100, 4)

            # TIER 2C-ii: dynamic threshold for this asset
            dyn_threshold = dynamic_thresholds.get(asset, _THRESHOLD_FALLBACK)

            # TIER 2D: require minimum 200 events before emitting non-neutral signal
            low_participation = event_count < 200
            effective_signal  = "neutral" if low_participation else _signal(score, dyn_threshold)

            signal_entry = {
                "asset": asset,
                "signal": effective_signal,
                "sentiment_score": score,
                "event_count": event_count,
                "price": price,
                "price_change_24h": price_change_24h,
                "price_date": today_str or None,
                "data_quality": data_quality,
                "dynamic_threshold": dyn_threshold,
            }
            if low_participation:
                signal_entry["low_participation"] = True

            signals.append(signal_entry)

        response_data: dict = {
            "signals": signals,
            "generated_at": now.isoformat(),
        }
        if staleness_hours is not None:
            response_data["price_staleness_hours"] = round(staleness_hours, 2)

        return jsonify({
            "success": True,
            "data": response_data,
        })

    except Exception as e:
        logger.error(f"Signals current error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@signals_bp.route("/history", methods=["GET"])
def signals_history():
    """
    Returns daily sentiment scores alongside price data for charting.
    Query params: graph_id (required), days (default 30), asset (optional filter)
    """
    graph_id = request.args.get("graph_id")
    if not graph_id:
        return jsonify({"success": False, "error": "graph_id required"}), 400

    days = request.args.get("days", 30, type=int)
    asset_filter = request.args.get("asset")  # optional single asset

    try:
        driver = _get_driver()
        now = datetime.now(timezone.utc)

        assets_to_query = [asset_filter] if asset_filter else _ASSET_CLASSES

        history = []

        for day_offset in range(days - 1, -1, -1):
            # Use UTC midnight boundaries to match SentimentSnapshot timestamps (stored in UTC)
            day_start = (now - timedelta(days=day_offset + 1)).replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
            )
            day_end = day_start + timedelta(days=1)
            date_str = day_start.date().isoformat()

            # Always try to read price data; missing file → null prices (not skip)
            price_file = _read_price_file(date_str)
            price_data = price_file.get("prices", {}) if price_file else {}
            if not price_file and day_offset > 0:
                # No price data and not today — log at debug level and use null prices
                logger.debug(f"history: no price file for {date_str} — using null prices")

            try:
                with driver.session() as session:
                    rows = session.run(
                        _SENTIMENT_QUERY,
                        gid=graph_id,
                        since=day_start.isoformat(),
                        until=day_end.isoformat(),
                    ).data()
            except Exception as e:
                logger.error(f"history: Neo4j query failed for {date_str}: {e}")
                rows = []

            scores = compute_sentiment_scores(rows)

            for asset in assets_to_query:
                # Return None for price if file missing — chart renders gap, not zero
                price = price_data.get(asset) if price_data else None
                sentiment_data = scores.get(asset, {"score": 0.0, "event_count": 0})
                score = sentiment_data["score"]

                history.append({
                    "date": date_str,
                    "asset": asset,
                    "sentiment_score": score,
                    "signal": _signal(score),
                    "event_count": sentiment_data["event_count"],
                    "price": price,  # null when price file missing
                })

        return jsonify({
            "success": True,
            "data": {
                "history": history,
                "days": days,
                "generated_at": now.isoformat(),
            }
        })

    except Exception as e:
        logger.error(f"Signals history error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@signals_bp.route("/backtest", methods=["GET"])
def signals_backtest():
    """
    Signal accuracy backtest — compares each day's directional signals
    against the actual next-day price move for each asset class.

    Query params: graph_id (required), days (default 30)

    Requires at least 2 days of price snapshots in backend/prices/.

    Returns:
      per_asset: {asset: {accuracy, total, correct}}
      per_archetype: {arch: {accuracy, total, correct}}
      rolling_7d: {asset: {dates, accuracy}}
      confidence_calibration: {tier: {accuracy, count}}
    """
    graph_id = request.args.get("graph_id")
    if not graph_id:
        return jsonify({"success": False, "error": "graph_id required"}), 400

    days = request.args.get("days", 30, type=int)

    try:
        # Import here to avoid circular dependency and keep backtester standalone.
        # Path from backend/app/api/ up two levels to backend/, then into scripts/.
        import sys, os
        scripts_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), '..', '..', 'scripts')
        )
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from backtester import run_backtest

        driver = _get_driver()
        result = run_backtest(driver, graph_id, days=days)
        return jsonify({"success": True, "data": result})

    except Exception as e:
        logger.error(f"Backtest error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


_SENTIMENT_HISTORY_LIMIT = 2000


@signals_bp.route("/sentiment_history", methods=["GET"])
def sentiment_history():
    """
    Returns all SentimentSnapshot nodes from the last N hours or N days.
    Snapshots are written every tick by simulation_tick.py.

    Query params:
      graph_id (required)
      hours    (default 48 → ~96 data points at 30-min cadence)
      days     (alternative to hours; e.g. days=30 → full half-hourly 30-day history)

    If both are supplied, hours takes precedence.
    Hard cap: at most 2000 snapshots returned; a warning is logged and
    ``capped: true`` is set in the response if the limit is hit.

    Each entry: {timestamp, assets: {equities, crypto, ...},
                 reactions: {buy, sell, hold, hedge, panic},
                 fear_greed, total_agents}
    """
    graph_id = request.args.get("graph_id")
    if not graph_id:
        return jsonify({"success": False, "error": "graph_id required"}), 400

    hours_param = request.args.get("hours", None, type=int)
    days_param  = request.args.get("days",  None, type=int)

    if hours_param is not None:
        hours = hours_param
    elif days_param is not None:
        hours = days_param * 24
    else:
        hours = 48  # default: last 48 hours (~96 snapshots at 30-min cadence)

    try:
        driver = _get_driver()
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=hours)).isoformat()

        with driver.session() as session:
            rows = session.run(
                """
                MATCH (s:SentimentSnapshot {graph_id: $gid})
                WHERE s.timestamp >= $since
                RETURN
                    s.timestamp   AS timestamp,
                    s.equities    AS equities,
                    s.crypto      AS crypto,
                    s.bonds       AS bonds,
                    s.commodities AS commodities,
                    s.fx          AS fx,
                    s.real_estate AS real_estate,
                    s.mixed       AS mixed,
                    s.buy_pct     AS buy_pct,
                    s.sell_pct    AS sell_pct,
                    s.hold_pct    AS hold_pct,
                    s.hedge_pct   AS hedge_pct,
                    s.panic_pct   AS panic_pct,
                    s.total_agents AS total_agents,
                    s.fear_greed  AS fear_greed
                ORDER BY s.timestamp ASC
                LIMIT $limit
                """,
                gid=graph_id,
                since=since,
                limit=_SENTIMENT_HISTORY_LIMIT,
            ).data()

        capped = len(rows) == _SENTIMENT_HISTORY_LIMIT
        if capped:
            logger.warning(
                f"sentiment_history: result capped at {_SENTIMENT_HISTORY_LIMIT} snapshots "
                f"(hours={hours}). Increase limit or reduce window if more data is needed."
            )

        snapshots = [
            {
                "timestamp": r["timestamp"],
                "assets": {
                    "equities":    r.get("equities",    0.0),
                    "crypto":      r.get("crypto",      0.0),
                    "bonds":       r.get("bonds",       0.0),
                    "commodities": r.get("commodities", 0.0),
                    "fx":          r.get("fx",          0.0),
                    "real_estate": r.get("real_estate", 0.0),
                    "mixed":       r.get("mixed",       0.0),
                },
                "reactions": {
                    "buy":   r.get("buy_pct",   0.0),
                    "sell":  r.get("sell_pct",  0.0),
                    "hold":  r.get("hold_pct",  0.0),
                    "hedge": r.get("hedge_pct", 0.0),
                    "panic": r.get("panic_pct", 0.0),
                },
                "fear_greed":    r.get("fear_greed", 50.0),
                "total_agents":  r.get("total_agents", 0),
            }
            for r in rows
        ]

        return jsonify({
            "success": True,
            "data": {
                "snapshots":    snapshots,
                "count":        len(snapshots),
                "hours":        hours,
                "capped":       capped,
                "generated_at": now.isoformat(),
            }
        })

    except Exception as e:
        logger.error(f"Sentiment history error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@signals_bp.route("/archetype_split", methods=["GET"])
def archetype_split():
    """
    TIER 2C-iii: Archetype signal decomposition — smart money vs dumb money.

    Returns per-asset capital-weighted sentiment (conviction model + decay, 24h window)
    split by archetype group:
      smart_money: hedge_fund + prop_trader
      dumb_money:  retail_amateur

    Divergence = smart_money_score - dumb_money_score per asset.
    Signal interpretation:
      |divergence| > 0.3  → smart_leads_bullish / smart_leads_bearish
      |divergence| <= 0.15 → converging
      event_count < 20 for either group → insufficient_data
    """
    graph_id = request.args.get("graph_id")
    if not graph_id:
        return jsonify({"success": False, "error": "graph_id required"}), 400

    try:
        driver = _get_driver()
        now = datetime.now(timezone.utc)
        ts_24h = (now - timedelta(hours=24)).isoformat()
        ts_now = now.isoformat()

        with driver.session() as session:
            smart_rows = session.run(
                _ARCHETYPE_SENTIMENT_QUERY,
                gid=graph_id,
                since=ts_24h,
                until=ts_now,
                archetypes=_SMART_MONEY_ARCHETYPES,
            ).data()
            dumb_rows = session.run(
                _ARCHETYPE_SENTIMENT_QUERY,
                gid=graph_id,
                since=ts_24h,
                until=ts_now,
                archetypes=_DUMB_MONEY_ARCHETYPES,
            ).data()

        smart_scores = compute_sentiment_scores(smart_rows, apply_leverage=True, apply_decay=True)
        dumb_scores  = compute_sentiment_scores(dumb_rows,  apply_leverage=True, apply_decay=True)

        assets_result = {}
        for asset in _ASSET_CLASSES:
            smart_data = smart_scores.get(asset, {"score": 0.0, "event_count": 0})
            dumb_data  = dumb_scores.get(asset,  {"score": 0.0, "event_count": 0})
            sm_score = smart_data["score"]
            dm_score = dumb_data["score"]
            divergence = round(sm_score - dm_score, 4)

            # Classify signal
            if smart_data["event_count"] < 20 or dumb_data["event_count"] < 20:
                split_signal = "insufficient_data"
            elif abs(divergence) > 0.3:
                split_signal = "smart_leads_bullish" if divergence > 0 else "smart_leads_bearish"
            else:
                split_signal = "converging"

            assets_result[asset] = {
                "smart_money":        sm_score,
                "dumb_money":         dm_score,
                "divergence":         divergence,
                "signal":             split_signal,
                "smart_event_count":  smart_data["event_count"],
                "dumb_event_count":   dumb_data["event_count"],
            }

        return jsonify({
            "success": True,
            "data": {
                "assets":                 assets_result,
                "smart_money_archetypes": _SMART_MONEY_ARCHETYPES,
                "dumb_money_archetypes":  _DUMB_MONEY_ARCHETYPES,
                "generated_at":           now.isoformat(),
            }
        })

    except Exception as e:
        logger.error(f"Archetype split error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
