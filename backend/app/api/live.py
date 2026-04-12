"""
Live State API — serves backend/live_state.json and price_sentiment_history.json directly.

Written by backend/scripts/dashboard_refresh.py every 15 minutes.
No Neo4j query — pure file reads so all endpoints are always fast.

Endpoints:
  GET /api/live/state?graph_id=...
    Returns the current live_state.json contents.
    Returns 503 if the file does not exist yet (dashboard_refresh not running).

  GET /api/live/history
    Returns the rolling price + sentiment time series (15-min cadence, 30-day window).
    Each entry: {"ts": "<ISO>", "p": {asset: price, ...}, "s": {asset: score, ...}}
    Returns 503 if no history exists yet.
"""

import json
from pathlib import Path
from flask import Blueprint, jsonify, request

live_bp = Blueprint("live", __name__)

_BACKEND_ROOT = Path(__file__).parent.parent.parent.parent / "backend"
_BACKEND_ALT  = Path(__file__).parent.parent.parent

# Two candidate paths: handles both container layout and alternate working dirs
_LIVE_STATE_PATH     = _BACKEND_ROOT / "live_state.json"
_LIVE_STATE_PATH_ALT = _BACKEND_ALT  / "live_state.json"
_HISTORY_PATH        = _BACKEND_ROOT / "price_sentiment_history.json"
_HISTORY_PATH_ALT    = _BACKEND_ALT  / "price_sentiment_history.json"


def _live_state_path() -> Path:
    if _LIVE_STATE_PATH.exists():
        return _LIVE_STATE_PATH
    return _LIVE_STATE_PATH_ALT


def _history_path() -> Path:
    if _HISTORY_PATH.exists():
        return _HISTORY_PATH
    return _HISTORY_PATH_ALT


@live_bp.route("/state", methods=["GET"])
def get_live_state():
    """
    Return the contents of live_state.json.
    503 if the file has not been written yet (dashboard_refresh not running).
    """
    path = _live_state_path()
    if not path.exists():
        return jsonify({
            "success": False,
            "error": "live_state.json not found — start dashboard_refresh.py to generate it",
        }), 503
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@live_bp.route("/history", methods=["GET"])
def get_live_history():
    """
    Return the rolling price + sentiment history from price_sentiment_history.json.
    Written by dashboard_refresh.py every 15 minutes.
    503 if the file has not been written yet.
    """
    path = _history_path()
    if not path.exists():
        return jsonify({
            "success": False,
            "error": "No history yet — start dashboard_refresh.py to generate it",
        }), 503
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return jsonify({"success": True, "data": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
