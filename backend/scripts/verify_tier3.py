"""
verify_tier3.py — TIER 3 + TIER 2 completion verification suite.

Checks:
  Section 1: OpenBB integration
    1.  OpenBB is importable inside the container (optional — warns if absent)
    2.  price_fetcher.py imports succeed without error
    3.  backend/prices/ has at least one .json snapshot with a "regime" key

  Section 2: Regime computation
    4.  compute_market_regime() returns all required keys
    5.  volatility is one of the 5 valid values
    6.  backend/live/regime.json exists and is valid JSON with required keys
    7.  GET /api/live/regime returns 200 and valid regime dict

  Section 3: Briefing regime header
    8.  Most recent briefing starts with "[MARKET REGIME:"
    9.  Briefing still has article content after the header (not truncated)

  Section 4: TIER 2 completion checks
    10. GET /api/signals/current returns dynamic_threshold on each signal entry
    11. GET /api/signals/archetype_split returns smart_money/dumb_money/divergence per asset

  Section 5: Panic contagion (TIER 2D-i)
    12. backend/briefings/contagion_flag.txt does NOT exist (only written on panic > 50%)

  Section 6: Dashboard
    13. GET /dashboard HTML contains: "regime-strip", "fetchRegime", "archetype_split",
        "Smart Money vs Dumb Money", "Sentiment Drawdowns"
    14. GET /api/live/regime returns all 4 dimension fields

Print PASS/FAIL per check. Final summary: X/14 checks passed.
On any FAIL, print what was expected vs what was found.
Does NOT modify any production files.
Exit 0 if all pass, 1 if any fail.

Usage:
  python backend/scripts/verify_tier3.py [--graph-id <uuid>] [--base-url <url>]
"""

import sys
import os
import json
import argparse
from pathlib import Path

# ------------------------------------------------------------------
# Root .env loading (host-side script)
# ------------------------------------------------------------------
try:
    from dotenv import load_dotenv as _ld
    _root_env = Path(__file__).parent.parent.parent / ".env"
    if _root_env.exists():
        _ld(_root_env, override=True)
except ImportError:
    pass

# ------------------------------------------------------------------
# CLI args
# ------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Verify TIER 3 implementation")
parser.add_argument("--graph-id", default="d3a38be8-37d9-4818-be28-5d2d0efa82c0")
parser.add_argument("--base-url", default="http://localhost:5001")
args = parser.parse_args()

GRAPH_ID = args.graph_id
BASE_URL  = args.base_url.rstrip("/")

_SCRIPTS_DIR = Path(__file__).parent
_PRICES_DIR  = _SCRIPTS_DIR.parent / "prices"
_LIVE_DIR    = _SCRIPTS_DIR.parent / "live"
_BRIEFINGS_DIR = _SCRIPTS_DIR.parent / "briefings"

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    symbol = "PASS" if passed else "FAIL"
    line   = f"  [{symbol}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    results.append((name, passed, detail))


def http_get(path: str, params: dict | None = None) -> dict | None:
    import urllib.request
    import urllib.parse
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"_error": str(e)}


def http_get_text(path: str) -> str | None:
    import urllib.request
    url = BASE_URL + path
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None


# ==================================================================
# Section 1: OpenBB integration
# ==================================================================
print("\n=== Section 1: OpenBB integration ===")

# Check 1 — OpenBB importable (optional; WARN not FAIL if absent)
print("\n--- Check 1: OpenBB import ---")
try:
    import importlib
    spec = importlib.util.find_spec("openbb")
    if spec is not None:
        check("OpenBB importable", True, "openbb package found")
    else:
        # Not installed → PASS with warning (optional dependency)
        check("OpenBB importable (optional)", True,
              "openbb not installed — price_fetcher falls back to yfinance")
except Exception as e:
    check("OpenBB importable (optional)", True,
          f"import check skipped ({e}) — fallback active")

# Check 2 — price_fetcher.py imports succeed
print("\n--- Check 2: price_fetcher.py imports ---")
try:
    sys.path.insert(0, str(_SCRIPTS_DIR))
    import price_fetcher as _pf  # type: ignore
    check("price_fetcher.py imports", True)
except Exception as e:
    check("price_fetcher.py imports", False, str(e))

# Check 3 — At least one price snapshot has a "regime" key
print("\n--- Check 3: price snapshot contains regime key ---")
try:
    files = sorted(_PRICES_DIR.glob("????-??-??.json"), reverse=True)
    if not files:
        check("price snapshot with regime key", False, "no price files found in backend/prices/")
    else:
        found = False
        for f in files[:5]:  # check most recent 5
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if "regime" in data:
                    found = True
                    check("price snapshot with regime key", True,
                          f"{f.name} has regime key")
                    break
            except Exception:
                continue
        if not found:
            check("price snapshot with regime key", False,
                  "most recent price snapshots have no regime key — run price_fetcher.py")
except Exception as e:
    check("price snapshot with regime key", False, str(e))


# ==================================================================
# Section 2: Regime computation
# ==================================================================
print("\n=== Section 2: Regime computation ===")

_VALID_VOLATILITY = {"LOW_VOLATILITY","NORMAL_VOLATILITY","HIGH_VOLATILITY",
                     "EXTREME_VOLATILITY","INSUFFICIENT_DATA"}
_VALID_TREND      = {"BULL_TRENDING","BEAR_TRENDING","SIDEWAYS","MIXED","INSUFFICIENT_DATA"}
_VALID_YC         = {"NORMAL","FLAT","INVERTED","FLIGHT_TO_SAFETY","RISING_RATES",
                     "STABLE","INSUFFICIENT_DATA"}
_VALID_FEAR       = {"COMPLACENT","NEUTRAL","ELEVATED_FEAR","EXTREME_FEAR","INSUFFICIENT_DATA"}
_REQUIRED_KEYS    = {"volatility","trend","yield_curve","fear","computed_at","insufficient_data"}

# Check 4 — compute_market_regime() returns required keys
print("\n--- Check 4: compute_market_regime() returns required keys ---")
_regime_result = None
try:
    _regime_result = _pf.compute_market_regime(_PRICES_DIR)
    missing = _REQUIRED_KEYS - set(_regime_result.keys())
    if missing:
        check("compute_market_regime() required keys", False,
              f"missing: {missing}")
    else:
        check("compute_market_regime() required keys", True,
              f"keys present: {list(_regime_result.keys())}")
except Exception as e:
    check("compute_market_regime() required keys", False, str(e))

# Check 5 — volatility is a valid value
print("\n--- Check 5: volatility value valid ---")
if _regime_result:
    vol = _regime_result.get("volatility")
    check("volatility value valid", vol in _VALID_VOLATILITY,
          f"got: {vol!r}, valid: {_VALID_VOLATILITY}")
else:
    check("volatility value valid", False, "regime not computed")

# Check 6 — backend/live/regime.json exists and valid
print("\n--- Check 6: backend/live/regime.json exists ---")
_regime_path = _LIVE_DIR / "regime.json"
if not _regime_path.exists():
    check("regime.json exists", False,
          f"not found at {_regime_path} — run price_fetcher.py")
else:
    try:
        regime_file = json.loads(_regime_path.read_text(encoding="utf-8"))
        missing = _REQUIRED_KEYS - set(regime_file.keys())
        if missing:
            check("regime.json valid JSON with required keys", False, f"missing: {missing}")
        else:
            check("regime.json valid JSON with required keys", True,
                  f"vol={regime_file.get('volatility')} trend={regime_file.get('trend')}")
    except Exception as e:
        check("regime.json valid JSON with required keys", False, str(e))

# Check 7 — GET /api/live/regime returns 200 and valid dict
print("\n--- Check 7: GET /api/live/regime ---")
resp = http_get("/api/live/regime", {"graph_id": GRAPH_ID})
if resp is None or "_error" in resp:
    check("/api/live/regime reachable", False, str((resp or {}).get("_error", "no response")))
elif not resp.get("success"):
    check("/api/live/regime reachable", False,
          f"success=false: {resp.get('error','—')}")
else:
    check("/api/live/regime reachable", True)
    regime_api = resp.get("data", {})
    missing = _REQUIRED_KEYS - set(regime_api.keys())
    check("/api/live/regime has required keys", not missing,
          f"missing: {missing}" if missing else f"all keys present")


# ==================================================================
# Section 3: Briefing regime header
# ==================================================================
print("\n=== Section 3: Briefing regime header ===")

# Check 8 — most recent briefing starts with "[MARKET REGIME:"
print("\n--- Check 8: most recent briefing has regime header ---")
try:
    briefings = sorted(_BRIEFINGS_DIR.glob("????-??-??_????.txt"), reverse=True)
    if not briefings:
        check("briefing regime header", False, "no briefing files found")
    else:
        latest = briefings[0]
        content = latest.read_text(encoding="utf-8", errors="replace")
        first_line = content.strip().split("\n")[0]
        has_header = first_line.startswith("[MARKET REGIME:")
        check("briefing starts with [MARKET REGIME:", has_header,
              f"file: {latest.name} | first line: {first_line[:80]!r}")
except Exception as e:
    check("briefing regime header", False, str(e))

# Check 9 — briefing still has article content after header
print("\n--- Check 9: briefing has article content after regime header ---")
try:
    if briefings:
        content = briefings[0].read_text(encoding="utf-8", errors="replace")
        # Should contain "HEADLINE:" or "MIROFISH BRIEFING"
        has_content = "HEADLINE:" in content or "MIROFISH BRIEFING" in content
        check("briefing has article content after header", has_content,
              f"file: {briefings[0].name}, len={len(content)} chars")
    else:
        check("briefing has article content after header", False, "no briefings to check")
except Exception as e:
    check("briefing has article content after header", False, str(e))


# ==================================================================
# Section 4: TIER 2 completion checks
# ==================================================================
print("\n=== Section 4: TIER 2 completion checks ===")

# Check 10 — /api/signals/current has dynamic_threshold
print("\n--- Check 10: /api/signals/current has dynamic_threshold ---")
resp = http_get("/api/signals/current", {"graph_id": GRAPH_ID})
if resp is None or "_error" in resp:
    check("signals/current reachable", False, str((resp or {}).get("_error", "no response")))
elif not resp.get("success"):
    check("signals/current reachable", False, resp.get("error", "success=false"))
else:
    check("signals/current reachable", True)
    sigs = resp.get("data", {}).get("signals", [])
    if not sigs:
        check("dynamic_threshold on signals", False, "no signals returned")
    else:
        all_have = all("dynamic_threshold" in s for s in sigs)
        sample = [s.get("dynamic_threshold") for s in sigs[:3]]
        check("dynamic_threshold present on all signals", all_have,
              f"sample values: {sample}")

# Check 11 — /api/signals/archetype_split works
print("\n--- Check 11: GET /api/signals/archetype_split ---")
resp = http_get("/api/signals/archetype_split", {"graph_id": GRAPH_ID})
if resp is None or "_error" in resp:
    check("archetype_split reachable", False, str((resp or {}).get("_error","no response")))
elif not resp.get("success"):
    check("archetype_split reachable", False, resp.get("error","success=false"))
else:
    check("archetype_split reachable", True)
    assets = resp.get("data", {}).get("assets", {})
    if not assets:
        check("archetype_split has asset entries", False, "empty assets dict")
    else:
        sample_asset = next(iter(assets.values()), {})
        required_fields = {"smart_money","dumb_money","divergence","signal"}
        missing = required_fields - set(sample_asset.keys())
        check("archetype_split has smart_money/dumb_money/divergence/signal",
              not missing,
              f"sample keys: {list(sample_asset.keys())[:6]}")


# ==================================================================
# Section 5: Panic contagion (TIER 2D-i)
# ==================================================================
print("\n=== Section 5: Panic contagion (TIER 2D-i) ===")

# Check 12 — contagion_flag.txt does NOT exist
print("\n--- Check 12: contagion_flag.txt not present at rest ---")
contagion_path = _BRIEFINGS_DIR / "contagion_flag.txt"
check("contagion_flag.txt absent (no recent panic wave)",
      not contagion_path.exists(),
      "file exists — a panic wave was detected in the last tick (this is informational, not a bug)"
      if contagion_path.exists() else "absent as expected")


# ==================================================================
# Section 6: Dashboard
# ==================================================================
print("\n=== Section 6: Dashboard ===")

# Check 13 — dashboard HTML contains required strings
print("\n--- Check 13: dashboard HTML contains new elements ---")
html = http_get_text("/dashboard")
if html is None:
    check("dashboard HTML reachable", False, "GET /dashboard failed")
else:
    check("dashboard HTML reachable", True, f"{len(html)} chars")
    required_strings = [
        "regime-strip",
        "fetchRegime",
        "archetype_split",
        "Smart Money vs Dumb Money",
        "Sentiment Drawdowns",
    ]
    for s in required_strings:
        check(f"dashboard contains '{s}'", s in html)

# Check 14 — /api/live/regime returns all 4 dimension fields
print("\n--- Check 14: /api/live/regime has all 4 dimensions ---")
resp = http_get("/api/live/regime", {"graph_id": GRAPH_ID})
if resp is None or "_error" in resp:
    check("/api/live/regime has 4 dimensions", False, str((resp or {}).get("_error","no response")))
elif not resp.get("success"):
    check("/api/live/regime has 4 dimensions", False, resp.get("error","success=false"))
else:
    data = resp.get("data", {})
    dims = ["volatility","trend","yield_curve","fear"]
    all_present = all(d in data for d in dims)
    check("/api/live/regime has volatility/trend/yield_curve/fear", all_present,
          str({d: data.get(d) for d in dims}))


# ==================================================================
# Summary
# ==================================================================
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
total_checks = len(results)
passed       = sum(1 for _, ok, _ in results if ok)
failed       = total_checks - passed

for name, ok, detail in results:
    symbol = "PASS" if ok else "FAIL"
    line   = f"  [{symbol}] {name}"
    if not ok and detail:
        line += f" — {detail}"
    print(line)

print(f"\n{passed}/{total_checks} checks passed.")
if failed:
    print(f"  {failed} check(s) FAILED — see details above.")
    sys.exit(1)
else:
    print("  All checks passed.")
    sys.exit(0)
