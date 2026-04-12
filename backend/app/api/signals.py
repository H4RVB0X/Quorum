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


def _signal(score: float) -> str:
    if score > 0.4:
        return "bullish"
    if score < -0.4:
        return "bearish"
    return "neutral"


_SENTIMENT_QUERY = """
    MATCH (a:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
    WHERE m.timestamp >= $since AND m.timestamp < $until
    RETURN
        a.asset_class_bias  AS asset_class,
        m.reaction          AS reaction,
        m.confidence        AS confidence,
        a.capital_usd       AS capital
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

        scores = compute_sentiment_scores(rows)

        prices_today, today_str = _latest_prices()

        # Try to find yesterday's prices for 24h price change
        if today_str:
            yesterday_str = (date.fromisoformat(today_str) - timedelta(days=1)).isoformat()
            prices_yesterday = _read_price_file(yesterday_str).get("prices", {})
        else:
            prices_yesterday = {}

        signals = []
        all_assets = set(_ASSET_CLASSES) | set(scores.keys())
        for asset in sorted(all_assets):
            sentiment_data = scores.get(asset, {"score": 0.0, "event_count": 0})
            score = sentiment_data["score"]
            price = prices_today.get(asset)
            prev_price = prices_yesterday.get(asset)

            price_change_24h = None
            if price is not None and prev_price:
                price_change_24h = round((price - prev_price) / prev_price * 100, 4)

            signals.append({
                "asset": asset,
                "signal": _signal(score),
                "sentiment_score": score,
                "event_count": sentiment_data["event_count"],
                "price": price,
                "price_change_24h": price_change_24h,
                "price_date": today_str or None,
            })

        return jsonify({
            "success": True,
            "data": {
                "signals": signals,
                "generated_at": now.isoformat(),
            }
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
            day_start = (now - timedelta(days=day_offset + 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            day_end = day_start + timedelta(days=1)
            date_str = day_start.date().isoformat()

            price_data = _read_price_file(date_str).get("prices", {})

            # Only query Neo4j if there's price data for this day (avoids many empty queries)
            if not price_data and day_offset > 0:
                continue

            with driver.session() as session:
                rows = session.run(
                    _SENTIMENT_QUERY,
                    gid=graph_id,
                    since=day_start.isoformat(),
                    until=day_end.isoformat(),
                ).data()

            scores = compute_sentiment_scores(rows)

            for asset in assets_to_query:
                price = price_data.get(asset)
                sentiment_data = scores.get(asset, {"score": 0.0, "event_count": 0})
                score = sentiment_data["score"]

                history.append({
                    "date": date_str,
                    "asset": asset,
                    "sentiment_score": score,
                    "signal": _signal(score),
                    "event_count": sentiment_data["event_count"],
                    "price": price,
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
