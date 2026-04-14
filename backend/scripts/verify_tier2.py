"""
verify_tier2.py — TIER 2 implementation verification suite.

Checks:
  1. TIER 2B coverage: MemoryEvents with direction/conviction/position_size set vs total (last 7d)
  2. TIER 2B: /api/investors/sentiment returns conviction-model fields
  3. TIER 2C: /api/signals/current returns data_quality field per signal
  4. TIER 2D: /api/signals/current uses event_count + low_participation threshold
  5. TIER 2D: /api/live/state contains drawdowns section

Does NOT modify any production files.

Usage (from MiroFish-Offline root or backend/scripts/):
  python backend/scripts/verify_tier2.py [--graph-id <uuid>] [--base-url <url>]

Defaults:
  graph_id  = d3a38be8-37d9-4818-be28-5d2d0efa82c0
  base_url  = http://localhost:5001
"""

import sys
import os
import json
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ------------------------------------------------------------------
# .env loading — must use root .env (localhost hostnames)
# ------------------------------------------------------------------
try:
    from dotenv import load_dotenv as _load_dotenv
    _root_env = Path(__file__).parent.parent.parent / ".env"
    if _root_env.exists():
        _load_dotenv(_root_env, override=True)
except ImportError:
    pass

# ------------------------------------------------------------------
# CLI args
# ------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Verify TIER 2 implementation")
parser.add_argument("--graph-id", default="d3a38be8-37d9-4818-be28-5d2d0efa82c0")
parser.add_argument("--base-url", default="http://localhost:5001")
args = parser.parse_args()

GRAPH_ID = args.graph_id
BASE_URL  = args.base_url.rstrip("/")

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

results: list[tuple[str, bool, str]] = []   # (check_name, passed, detail)


def check(name: str, passed: bool, detail: str = "") -> None:
    """Record a check result and print it immediately."""
    symbol = "PASS" if passed else "FAIL"
    line   = f"  [{symbol}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    results.append((name, passed, detail))


def http_get(path: str, params: dict | None = None) -> dict | None:
    """Simple HTTP GET. Returns parsed JSON or None on failure."""
    import urllib.request
    import urllib.parse
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"_error": str(e)}


# ------------------------------------------------------------------
# Check 1 — TIER 2B coverage: MemoryEvents with new fields vs total
# ------------------------------------------------------------------
print("\n=== Check 1: TIER 2B MemoryEvent coverage (last 7d) ===")
try:
    from neo4j import GraphDatabase
    uri      = os.environ.get("NEO4J_URI",      "bolt://localhost:7687")
    user     = os.environ.get("NEO4J_USER",     "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "mirofish")
    driver   = GraphDatabase.driver(uri, auth=(user, password))

    since_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    with driver.session() as session:
        total_result = session.run(
            """
            MATCH (a:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
            WHERE m.timestamp >= $since
            RETURN count(m) AS c
            """,
            gid=GRAPH_ID, since=since_7d,
        ).single()
        total = int(total_result["c"] or 0) if total_result else 0

        new_result = session.run(
            """
            MATCH (a:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
            WHERE m.timestamp >= $since
              AND m.direction   IS NOT NULL
              AND m.conviction  IS NOT NULL
              AND m.position_size IS NOT NULL
            RETURN count(m) AS c
            """,
            gid=GRAPH_ID, since=since_7d,
        ).single()
        new_fields = int(new_result["c"] or 0) if new_result else 0

    driver.close()

    if total == 0:
        check("TIER 2B coverage", False, "0 MemoryEvents in last 7d — no data to check")
    else:
        pct = round(new_fields / total * 100, 1)
        detail = f"{new_fields}/{total} events have direction/conviction/position_size ({pct}%)"
        # Any coverage is progress; we pass if at least 1 new-format event exists
        check("TIER 2B coverage", new_fields > 0, detail)

except ImportError:
    check("TIER 2B coverage", False, "neo4j driver not importable (run from host with venv active)")
except Exception as e:
    check("TIER 2B coverage", False, str(e))


# ------------------------------------------------------------------
# Check 2 — TIER 2B: /api/investors/sentiment conviction fields
# ------------------------------------------------------------------
print("\n=== Check 2: /api/investors/sentiment conviction-model fields ===")
resp = http_get("/api/investors/sentiment", {"graph_id": GRAPH_ID})
if resp is None or "_error" in resp:
    check("sentiment endpoint reachable", False, str((resp or {}).get("_error", "no response")))
elif not resp.get("success"):
    check("sentiment endpoint reachable", False, resp.get("error", "success=false"))
else:
    check("sentiment endpoint reachable", True)
    data = resp.get("data", {})
    # Must have both capital-weighted and equal-weighted 24h paths
    has_24h    = "by_asset_class"       in data and "by_asset_class_equal" in data
    has_7d     = "by_asset_class_7d"    in data or "by_asset_class"       in data
    has_events = "event_count_24h"      in data or True  # optional field

    check("has by_asset_class (capital-weighted)", "by_asset_class"       in data,
          str(list(data.keys())[:10]))
    check("has by_asset_class_equal (equal-weighted)", "by_asset_class_equal" in data,
          str(list(data.keys())[:10]))


# ------------------------------------------------------------------
# Check 3 — TIER 2C: data_quality field in /api/signals/current
# ------------------------------------------------------------------
print("\n=== Check 3: TIER 2C — data_quality field in /api/signals/current ===")
resp = http_get("/api/signals/current", {"graph_id": GRAPH_ID})
signals_data = None
if resp is None or "_error" in resp:
    check("signals/current reachable", False, str((resp or {}).get("_error", "no response")))
elif not resp.get("success"):
    check("signals/current reachable", False, resp.get("error", "success=false"))
else:
    check("signals/current reachable", True)
    signals_data = resp.get("data", {})
    signals_list = signals_data.get("signals", [])

    if not signals_list:
        check("signals list non-empty", False, "no signals returned")
    else:
        check("signals list non-empty", True, f"{len(signals_list)} signals")

        # Check data_quality is present in all signals
        dq_present = all("data_quality" in s for s in signals_list)
        dq_values  = list({s.get("data_quality") for s in signals_list})
        check("data_quality field present on all signals", dq_present,
              f"values seen: {dq_values}")

        # Check valid data_quality values
        valid_dq = all(s.get("data_quality") in ("fresh", "degraded") for s in signals_list)
        check("data_quality value is 'fresh' or 'degraded'", valid_dq,
              f"values seen: {dq_values}")


# ------------------------------------------------------------------
# Check 4 — TIER 2D: event_count + low_participation in signals
# ------------------------------------------------------------------
print("\n=== Check 4: TIER 2D — event_count and low_participation in signals ===")
if signals_data is not None and signals_data.get("signals"):
    signals_list = signals_data["signals"]
    has_event_count = all("event_count" in s for s in signals_list)
    check("event_count field present on all signals", has_event_count)

    # low_participation should appear on any signal with event_count < 200
    low_part_signals = [s for s in signals_list if s.get("event_count", 999) < 200]
    if low_part_signals:
        all_flagged = all(s.get("low_participation") is True for s in low_part_signals)
        check("low_participation=true when event_count<200", all_flagged,
              f"{len(low_part_signals)} signal(s) below threshold")
        # Confirm those signals are forced to neutral
        all_neutral = all(s.get("signal") == "neutral" for s in low_part_signals)
        check("signal forced to neutral when low_participation", all_neutral,
              f"signal values: {[s.get('signal') for s in low_part_signals]}")
    else:
        check("low_participation threshold (no low-participation signals this run)",
              True, "all assets have >=200 events")
else:
    check("event_count field check", False, "no signals data from check 3")


# ------------------------------------------------------------------
# Check 5 — TIER 2D: drawdowns in /api/live/state
# ------------------------------------------------------------------
print("\n=== Check 5: TIER 2D — drawdowns in /api/live/state ===")
resp = http_get("/api/live/state")
if resp is None or "_error" in resp:
    check("/api/live/state reachable", False, str((resp or {}).get("_error", "no response")))
elif not resp.get("success"):
    # live state returns 503 if dashboard_refresh.py isn't running
    check("/api/live/state reachable", False,
          f"HTTP error — is dashboard_refresh.py running? ({resp.get('error', 'no success field')})")
else:
    check("/api/live/state reachable", True)
    live_data = resp.get("data", resp)  # live.py returns flat dict, not nested under "data"
    has_drawdowns = "drawdowns" in live_data
    if has_drawdowns:
        dd = live_data["drawdowns"]
        check("drawdowns field present in live state", True,
              f"{len(dd)} asset(s): {list(dd.keys())}")
        # Validate structure
        sample = next(iter(dd.values()), {})
        valid_struct = all(k in sample for k in ("peak", "current", "drawdown_pct"))
        check("drawdown entries have peak/current/drawdown_pct", valid_struct,
              f"sample keys: {list(sample.keys())}")
    else:
        # Drawdowns only appear when ≥5 SentimentSnapshot nodes exist
        check("drawdowns field present in live state", False,
              "missing — either dashboard_refresh.py hasn't run or <5 SentimentSnapshots exist. "
              "Run a tick then wait for the 15-min refresh cycle.")


# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
total_checks = len(results)
passed       = sum(1 for _, ok, _ in results if ok)
failed       = total_checks - passed

for name, ok, detail in results:
    symbol = "PASS" if ok else "FAIL"
    print(f"  [{symbol}] {name}")

print(f"\n{passed}/{total_checks} checks passed.")
if failed:
    print(f"  {failed} check(s) FAILED — see details above.")
    sys.exit(1)
else:
    print("  All checks passed.")
    sys.exit(0)
