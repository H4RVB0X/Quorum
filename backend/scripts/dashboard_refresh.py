"""
dashboard_refresh.py — Live state file refresher for the Quorum dashboard.

Runs as a SEPARATE PROCESS from scheduler.py — NOT imported by scheduler.py.
Writes backend/live_state.json every 15 minutes.
The dashboard reads /api/live/state which serves this file directly.

Data sources (all fetched independently with try/except):
  - Prices:           yfinance live fetch (falls back to latest price file if yfinance fails)
  - Sentiment:        Neo4j direct query (mirrors investors.py logic)
  - Reaction dist:    Latest SentimentSnapshot from Neo4j
  - Signals:          Derived from sentiment scores (mirrors signals.py thresholds)
  - Top entities:     Neo4j — 10 most recently seen non-synthetic entities
  - Recent events:    Neo4j — 10 most recent MemoryEvents across all agents
  - Pool stats:       Neo4j — pool_size, total_memory_events, fear/greed counts
  - Feed breakdown:   Most recent *_sources.json sidecar from backend/briefings/
  - Last tick/daily:  backend/logs/scheduler_runs.json

Error handling:
  - yfinance failure  → write stale prices from latest price file + prices_stale: true
  - Neo4j conn fail   → skip write entirely, keep existing live_state.json unchanged
  - Individual query  → log WARNING, omit that section, continue writing

Usage:
  python backend/scripts/dashboard_refresh.py --graph-id <graph_id>
"""

import sys
import os
import json
import argparse
import logging
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

# Allow imports relative to backend/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from apscheduler.schedulers.blocking import BlockingScheduler

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SCRIPT_DIR   = Path(__file__).parent
_BACKEND_DIR  = _SCRIPT_DIR.parent

# ---------------------------------------------------------------------------
# .env loading — must happen before any os.environ.get() calls.
# This script runs on the HOST (not inside Docker), so it must load the
# ROOT .env (MiroFish-Offline/.env) which uses localhost hostnames.
# backend/.env uses Docker service names (neo4j, ollama) and must NOT
# be loaded here — that file is only for the Docker container runtime.
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv as _load_dotenv
    _root_env = Path(__file__).parent.parent.parent / ".env"  # MiroFish-Offline/.env
    if _root_env.exists():
        _load_dotenv(_root_env, override=True)
except ImportError:
    pass  # python-dotenv not installed; rely on env vars already in the shell
_PRICES_DIR   = _BACKEND_DIR / "prices"
_BRIEFINGS_DIR = _BACKEND_DIR / "briefings"
_LOGS_PATH    = _BACKEND_DIR / "logs" / "scheduler_runs.json"
_LIVE_DIR           = _BACKEND_DIR / "live"          # bind-mounted into container
_LIVE_STATE_PATH    = _LIVE_DIR / "live_state.json"
_HISTORY_PATH       = _LIVE_DIR / "price_sentiment_history.json"
_HISTORY_MAX_POINTS = 2880   # 30 days × 96 points/day at 15-min cadence

# Ensure the live/ directory exists (first run before docker-compose creates the mount point)
_LIVE_DIR.mkdir(parents=True, exist_ok=True)

_ASSET_CLASSES = ["equities", "crypto", "bonds", "commodities", "fx", "real_estate", "mixed"]

ASSET_TICKERS = {
    "equities":    "SPY",
    "crypto":      "BTC-USD",
    "bonds":       "TLT",
    "commodities": "GLD",
    "fx":          "DX-Y.NYB",
    "mixed":       "VT",
    "real_estate": "VNQ",
}

_REACTION_SCORES = {
    "buy":   1.0,
    "hedge": 0.5,
    "hold":  0.0,
    "sell": -1.0,
    "panic": -1.0,
}

_LEVERAGE_MULTIPLIERS = {
    "none":     1.0,
    "2x":       1.3,
    "5x":       1.6,
    "10x_plus": 2.0,
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("mirofish.dashboard_refresh")


# ---------------------------------------------------------------------------
# Neo4j helper
# ---------------------------------------------------------------------------

def _get_neo4j_driver():
    from neo4j import GraphDatabase
    # Default uses localhost — this script runs on the host, not in Docker.
    # Root .env (loaded above) sets NEO4J_URI=bolt://localhost:7687.
    uri      = os.environ.get("NEO4J_URI",      "bolt://localhost:7687")
    user     = os.environ.get("NEO4J_USER",     "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "mirofish")
    return GraphDatabase.driver(uri, auth=(user, password))


# ---------------------------------------------------------------------------
# Sentiment computation (mirrors investors.py — inlined to avoid Flask imports)
# ---------------------------------------------------------------------------

def _reaction_score(reaction: str) -> float:
    return _REACTION_SCORES.get((reaction or "").lower(), 0.0)


def _leverage_mult(leverage: str) -> float:
    return _LEVERAGE_MULTIPLIERS.get((leverage or "none").lower(), 1.0)


def compute_sentiment_capital_weighted(rows: list) -> dict:
    """Capital × confidence × leverage (24h only). Returns {asset: score}."""
    buckets: dict = {}
    for row in rows:
        asset      = row.get("asset_class") or "unknown"
        capital    = float(row.get("capital")    or 0)
        confidence = float(row.get("confidence") or 0)
        lev        = _leverage_mult(row.get("leverage", "none"))
        score      = _reaction_score(row.get("reaction", ""))
        weight     = capital * confidence * lev
        if asset not in buckets:
            buckets[asset] = {"weighted_sum": 0.0, "total_weight": 0.0}
        buckets[asset]["weighted_sum"] += score * weight
        buckets[asset]["total_weight"] += weight
    result = {}
    for asset, b in buckets.items():
        if b["total_weight"] == 0:
            result[asset] = 0.0
        else:
            raw = b["weighted_sum"] / b["total_weight"]
            result[asset] = round(max(-1.0, min(1.0, raw)), 4)
    return result


def compute_sentiment_equal_weighted(rows: list) -> dict:
    """Confidence-only weight (capital has no influence). Returns {asset: score}."""
    buckets: dict = {}
    for row in rows:
        asset      = row.get("asset_class") or "unknown"
        confidence = float(row.get("confidence") or 0)
        score      = _reaction_score(row.get("reaction", ""))
        weight     = confidence
        if asset not in buckets:
            buckets[asset] = {"weighted_sum": 0.0, "total_weight": 0.0}
        buckets[asset]["weighted_sum"] += score * weight
        buckets[asset]["total_weight"] += weight
    result = {}
    for asset, b in buckets.items():
        if b["total_weight"] == 0:
            result[asset] = 0.0
        else:
            raw = b["weighted_sum"] / b["total_weight"]
            result[asset] = round(max(-1.0, min(1.0, raw)), 4)
    return result


# ---------------------------------------------------------------------------
# Signal threshold (mirrors signals.py)
# ---------------------------------------------------------------------------

def _signal(score: float) -> str:
    if score > 0.4:
        return "bullish"
    if score < -0.4:
        return "bearish"
    return "neutral"


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def _latest_price_file() -> tuple:
    """Return (prices_dict, date_str) from the most recently written price file."""
    if not _PRICES_DIR.is_dir():
        return {}, ""
    files = sorted(_PRICES_DIR.glob("????-??-??.json"), reverse=True)
    if not files:
        return {}, ""
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
        return data.get("prices", {}), data.get("date", files[0].stem)
    except Exception:
        return {}, ""


def _read_price_file_for_date(date_str: str) -> dict:
    p = _PRICES_DIR / f"{date_str}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("prices", {})
    except Exception:
        return {}


def _fetch_live_prices() -> tuple:
    """
    Fetch live prices from yfinance. Returns (prices_dict, stale: bool).
    On any failure, falls back to the most recent price file and sets stale=True.
    """
    try:
        import yfinance as yf
        prices = {}
        for asset, ticker in ASSET_TICKERS.items():
            try:
                data = yf.download(ticker, period="2d", progress=False, auto_adjust=True)
                if data.empty:
                    prices[asset] = None
                    continue
                close = data["Close"]
                # Handle multi-level columns from some yfinance versions
                if hasattr(close, "columns"):
                    close = close.iloc[:, 0]
                prices[asset] = round(float(close.dropna().iloc[-1]), 6)
            except Exception as e:
                logger.warning(f"yfinance: {ticker} failed: {e}")
                prices[asset] = None
        return prices, False
    except ImportError:
        logger.warning("yfinance not available — using stale prices from file")
    except Exception as e:
        logger.warning(f"yfinance fetch failed: {e} — using stale prices from file")
    fallback, _ = _latest_price_file()
    return fallback, True


# ---------------------------------------------------------------------------
# Scheduler run log
# ---------------------------------------------------------------------------

def _last_run_of_type(job_type: str) -> dict | None:
    try:
        if not _LOGS_PATH.exists():
            return None
        entries = json.loads(_LOGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            return None
        for entry in reversed(entries):
            if isinstance(entry, dict) and entry.get("job_type") == job_type:
                return entry
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Main refresh
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Price/sentiment history file — 15-min rolling snapshot
# ---------------------------------------------------------------------------

def _append_history_snapshot(timestamp: str, prices: dict, sentiment: dict) -> None:
    """
    Append one {ts, p, s} entry to price_sentiment_history.json and trim to
    _HISTORY_MAX_POINTS. Writes atomically (tmp → rename).
    Silently skips if both prices and sentiment are empty.
    """
    if not prices and not sentiment:
        return
    try:
        if _HISTORY_PATH.exists():
            history = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
            if not isinstance(history, list):
                history = []
        else:
            history = []

        history.append({"ts": timestamp, "p": prices, "s": sentiment})

        # Keep only the most recent _HISTORY_MAX_POINTS entries
        if len(history) > _HISTORY_MAX_POINTS:
            history = history[-_HISTORY_MAX_POINTS:]

        payload = json.dumps(history, separators=(",", ":"))  # compact — this file can be large
        tmp = _HISTORY_PATH.with_suffix(".hist.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(_HISTORY_PATH)
    except Exception as e:
        logger.warning(f"Failed to write price_sentiment_history.json: {e}")


_SENTIMENT_QUERY = """
    MATCH (a:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
    WHERE m.timestamp >= $since
    RETURN
        a.asset_class_bias  AS asset_class,
        m.reaction          AS reaction,
        m.confidence        AS confidence,
        a.capital_usd       AS capital,
        a.leverage_typical  AS leverage
"""


def refresh(graph_id: str) -> None:
    logger.info("Starting refresh")
    now   = datetime.now(timezone.utc)
    state: dict = {"refreshed_at": now.isoformat()}

    # ------------------------------------------------------------------
    # 1. Prices (independent of Neo4j — always attempted)
    # ------------------------------------------------------------------
    prices_stale = False
    prices: dict = {}
    price_changes_24h: dict = {a: None for a in _ASSET_CLASSES}

    try:
        prices, prices_stale = _fetch_live_prices()
        state["prices"] = {a: prices.get(a) for a in _ASSET_CLASSES}
        if prices_stale:
            state["prices_stale"] = True

        # 24h change: compare live prices against yesterday's price file
        _, today_str = _latest_price_file()
        if today_str:
            yesterday_str = (date.fromisoformat(today_str) - timedelta(days=1)).isoformat()
            prev_prices = _read_price_file_for_date(yesterday_str)
        else:
            prev_prices = {}

        for asset in _ASSET_CLASSES:
            p  = prices.get(asset)
            pp = prev_prices.get(asset)
            if p is not None and pp:
                price_changes_24h[asset] = round((p - pp) / pp * 100, 4)
        state["price_changes_24h"] = price_changes_24h

    except Exception as e:
        logger.warning(f"Price section error: {e}")
        fallback, _ = _latest_price_file()
        state["prices"]          = {a: fallback.get(a) for a in _ASSET_CLASSES}
        state["prices_stale"]    = True
        state["price_changes_24h"] = price_changes_24h

    # ------------------------------------------------------------------
    # 2. Neo4j sections
    # ------------------------------------------------------------------
    driver = None
    try:
        driver = _get_neo4j_driver()
        # Verify connectivity with a cheap query — if this fails we abort
        with driver.session() as s:
            s.run("RETURN 1").consume()
    except Exception as e:
        logger.warning(f"Neo4j connection failed: {e} — live_state.json not updated")
        if driver:
            try:
                driver.close()
            except Exception:
                pass
        return  # Keep existing file unchanged

    try:
        ts_24h = (now - timedelta(hours=24)).isoformat()

        # -- 2a. Sentiment (24h capital-weighted + equal-weighted) --
        sentiment_scores: dict = {}
        try:
            with driver.session() as session:
                rows = session.run(_SENTIMENT_QUERY, gid=graph_id, since=ts_24h).data()
            cap_scores = compute_sentiment_capital_weighted(rows)
            eq_scores  = compute_sentiment_equal_weighted(rows)
            by_asset     = {a: cap_scores.get(a, 0.0) for a in _ASSET_CLASSES}
            by_asset_eq  = {a: eq_scores.get(a,  0.0) for a in _ASSET_CLASSES}
            sentiment_scores = by_asset

            valid = [v for v in by_asset.values() if v is not None and v != 0.0]
            mean_sent = sum(valid) / len(valid) if valid else 0.0
            if mean_sent > 0.15:
                fg_label = "greed"
            elif mean_sent < -0.15:
                fg_label = "fear"
            else:
                fg_label = "neutral"

            state["sentiment"] = {
                "by_asset_class":       by_asset,
                "by_asset_class_equal": by_asset_eq,
                "fear_greed":           fg_label,
            }
        except Exception as e:
            logger.warning(f"Sentiment query failed: {e}")

        # -- 2b. Reaction distribution (latest SentimentSnapshot) --
        try:
            with driver.session() as session:
                snap = session.run(
                    """
                    MATCH (s:SentimentSnapshot {graph_id: $gid})
                    RETURN s.buy_pct AS buy, s.sell_pct AS sell,
                           s.hold_pct AS hold, s.hedge_pct AS hedge, s.panic_pct AS panic
                    ORDER BY s.timestamp DESC LIMIT 1
                    """,
                    gid=graph_id,
                ).single()
            if snap:
                state["reaction_distribution"] = {
                    "buy":   round((snap["buy"]   or 0) * 100, 2),
                    "sell":  round((snap["sell"]  or 0) * 100, 2),
                    "hold":  round((snap["hold"]  or 0) * 100, 2),
                    "hedge": round((snap["hedge"] or 0) * 100, 2),
                    "panic": round((snap["panic"] or 0) * 100, 2),
                }
        except Exception as e:
            logger.warning(f"Reaction distribution query failed: {e}")

        # -- 2c. Drawdown tracking per asset class (TIER 2D) --
        try:
            with driver.session() as session:
                snap_rows = session.run(
                    """
                    MATCH (s:SentimentSnapshot {graph_id: $gid})
                    RETURN s.timestamp   AS ts,
                           s.equities    AS equities,
                           s.crypto      AS crypto,
                           s.bonds       AS bonds,
                           s.commodities AS commodities,
                           s.fx          AS fx,
                           s.real_estate AS real_estate,
                           s.mixed       AS mixed
                    ORDER BY s.timestamp DESC LIMIT 48
                    """,
                    gid=graph_id,
                ).data()

            if len(snap_rows) >= 5:
                drawdowns = {}
                for asset in _ASSET_CLASSES:
                    values = [r.get(asset) for r in snap_rows if r.get(asset) is not None]
                    if len(values) < 5:
                        continue  # insufficient data for this asset
                    current_val = values[0]  # most recent (DESC order)
                    peak_val    = max(values)
                    if abs(peak_val) < 0.1:
                        continue  # near-zero peak causes meaningless / exploding drawdown
                    dd_pct = (current_val - peak_val) / abs(peak_val) * 100
                    dd_pct = max(-100.0, dd_pct)  # safety clamp — score is bounded [-1,+1]
                    drawdowns[asset] = {
                        "peak":         round(peak_val, 4),
                        "current":      round(current_val, 4),
                        "drawdown_pct": round(dd_pct, 2),
                    }
                if drawdowns:
                    state["drawdowns"] = drawdowns
        except Exception as e:
            logger.warning(f"Drawdown computation failed: {e}")

        # -- 2d. Signals (derived from sentiment + prices, no extra DB call) --
        try:
            signals = []
            for asset in _ASSET_CLASSES:
                score = sentiment_scores.get(asset, 0.0)
                signals.append({
                    "asset":     asset,
                    "direction": _signal(score),
                    "confidence": score,
                    "price":     state.get("prices", {}).get(asset),
                    "change_24h": price_changes_24h.get(asset),
                })
            state["signals"] = signals
        except Exception as e:
            logger.warning(f"Signals derivation failed: {e}")

        # -- 2e. Top entities --
        try:
            with driver.session() as session:
                rows = session.run(
                    """
                    MATCH (e:Entity {graph_id: $gid})
                    WHERE (e.is_synthetic IS NULL OR e.is_synthetic = false)
                      AND e.name IS NOT NULL
                      AND e.type IS NOT NULL
                    RETURN e.name AS name, e.type AS type,
                           coalesce(e.mention_count, 1) AS count
                    ORDER BY e.last_seen DESC, count DESC
                    LIMIT 10
                    """,
                    gid=graph_id,
                ).data()
            state["top_entities"] = [
                {"name": r["name"], "type": r["type"], "count": int(r["count"] or 1)}
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"Top entities query failed: {e}")

        # -- 2f. Recent events --
        try:
            with driver.session() as session:
                rows = session.run(
                    """
                    MATCH (a:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
                    RETURN
                        a.name              AS agent,
                        a.investor_archetype AS archetype,
                        m.reaction          AS reaction,
                        m.confidence        AS confidence,
                        m.reasoning         AS reasoning,
                        m.timestamp         AS timestamp
                    ORDER BY m.timestamp DESC LIMIT 10
                    """,
                    gid=graph_id,
                ).data()
            state["recent_events"] = [
                {
                    "agent":     r["agent"],
                    "archetype": r["archetype"],
                    "reaction":  r["reaction"],
                    "confidence": r["confidence"],
                    "reasoning": r["reasoning"],
                    "timestamp": r["timestamp"],
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"Recent events query failed: {e}")

        # -- 2g. Pool stats --
        try:
            with driver.session() as session:
                pool_size = session.run(
                    "MATCH (n:Entity {graph_id: $gid, is_synthetic: true}) RETURN count(n) AS c",
                    gid=graph_id,
                ).single()["c"]
                total_events = session.run(
                    """
                    MATCH (n:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
                    RETURN count(m) AS c
                    """,
                    gid=graph_id,
                ).single()["c"]
                fg_rows = session.run(
                    """
                    MATCH (n:Entity {graph_id: $gid, is_synthetic: true})
                    WHERE n.fear_greed_dominant IS NOT NULL
                    RETURN n.fear_greed_dominant AS fg, count(n) AS c
                    """,
                    gid=graph_id,
                ).data()
            fear_count  = next((r["c"] for r in fg_rows if r["fg"] == "fear"),  0)
            greed_count = next((r["c"] for r in fg_rows if r["fg"] == "greed"), 0)
            state["pool_stats"] = {
                "pool_size":           pool_size,
                "total_memory_events": total_events,
                "fear_count":          fear_count,
                "greed_count":         greed_count,
            }
        except Exception as e:
            logger.warning(f"Pool stats query failed: {e}")

    finally:
        try:
            driver.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 3. Feed breakdown (file read — independent of Neo4j)
    # ------------------------------------------------------------------
    try:
        if _BRIEFINGS_DIR.is_dir():
            sidecars = sorted(_BRIEFINGS_DIR.glob("*_sources.json"), reverse=True)
            if sidecars:
                state["feed_breakdown"] = json.loads(
                    sidecars[0].read_text(encoding="utf-8")
                )
    except Exception as e:
        logger.warning(f"Feed breakdown read failed: {e}")

    # ------------------------------------------------------------------
    # 4. Scheduler run log (file read — independent of Neo4j)
    # ------------------------------------------------------------------
    try:
        last_hh    = _last_run_of_type("halfhourly")
        last_daily = _last_run_of_type("daily")
        if last_hh:
            state["last_tick_at"]  = last_hh.get("finished_at")
        if last_daily:
            state["last_daily_at"] = last_daily.get("finished_at")
    except Exception as e:
        logger.warning(f"Scheduler log read failed: {e}")

    # ------------------------------------------------------------------
    # 5. Write live_state.json atomically (write to temp, then rename)
    # ------------------------------------------------------------------
    try:
        payload = json.dumps(state, indent=2)
        tmp_path = _LIVE_STATE_PATH.with_suffix(".tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(_LIVE_STATE_PATH)
        logger.info(f"live_state.json written ({len(payload)} bytes)")
    except Exception as e:
        logger.error(f"Failed to write live_state.json: {e}")

    # ------------------------------------------------------------------
    # 6. Append snapshot to price_sentiment_history.json
    # ------------------------------------------------------------------
    _append_history_snapshot(
        timestamp=now.isoformat(),
        prices={a: state.get("prices", {}).get(a) for a in _ASSET_CLASSES},
        sentiment=state.get("sentiment", {}).get("by_asset_class", {}),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dashboard live state refresher — writes live_state.json every 15 minutes"
    )
    parser.add_argument("--graph-id", required=True, help="Neo4j graph_id")
    args = parser.parse_args()
    graph_id = args.graph_id

    print(f"dashboard_refresh starting  graph_id={graph_id}")
    print(f"Output: {_LIVE_STATE_PATH}")

    # Immediate run on startup — dashboard has data before the first 15-min interval
    refresh(graph_id)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        refresh,
        trigger="interval",
        minutes=15,
        args=[graph_id],
        id="dashboard_refresh",
        name="Dashboard live state refresh",
        max_instances=1,
        coalesce=True,
    )

    print("Refreshing every 15 minutes. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        scheduler.shutdown()
        print("dashboard_refresh stopped.")


if __name__ == "__main__":
    main()
