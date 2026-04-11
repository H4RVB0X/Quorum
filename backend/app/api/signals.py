"""
Trading Signal API Routes

Endpoints:
  GET /api/signals/current?graph_id=...
    Returns the latest signal per asset class, combining 24-hour sentiment
    scores with the most recent price data.

  GET /api/signals/history?graph_id=...&days=30
    Returns daily sentiment scores alongside price data for charting.

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
