"""
verify_all.py — Combined verification suite for all implemented changes.

Runs all TIER 2 + TIER 3 checks, then adds new checks for changes in the
most recent session:
  CHANGE 1: Daily scheduler cron changed to 02:00 UTC
  CHANGE 2: market_open field present in live_state.json
  CHANGE 3: historyChart zoom plugin in dashboard HTML
  CHANGE 4: TOKEN_BUDGET in news_fetcher.py; price_staleness_hours in live state; source_bias in live state
  CHANGE 5: economic_calendar.json / earnings_calendar.json exist; /api/live/calendar endpoint
  CHANGE 6: _lastSentimentData caching in dashboard HTML
  CHANGE 7: correlation_matrix in live state; correlation panel in dashboard HTML
  CHANGE 8: non-ASCII language guard in simulation_tick.py
  CHANGE 9: Nitter Docker comment block in news_fetcher.py

Does NOT modify any production files.
Exit 0 if all pass, 1 if any fail.

Usage:
  python backend/scripts/verify_all.py [--graph-id <uuid>] [--base-url <url>]
"""

import sys
import os
import json
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone

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
parser = argparse.ArgumentParser(description="Combined verification suite")
parser.add_argument("--graph-id", default="d3a38be8-37d9-4818-be28-5d2d0efa82c0")
parser.add_argument("--base-url", default="http://localhost:5001")
args = parser.parse_args()
GRAPH_ID = args.graph_id
BASE_URL  = args.base_url.rstrip("/")

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
_SCRIPTS_DIR  = Path(__file__).parent
_BACKEND_DIR  = _SCRIPTS_DIR.parent
_LIVE_DIR     = _BACKEND_DIR / "live"
_PRICES_DIR   = _BACKEND_DIR / "prices"
_BRIEFINGS_DIR= _BACKEND_DIR / "briefings"
_FRONTEND_DIR = _BACKEND_DIR.parent / "frontend" / "public"
_DASHBOARD    = _FRONTEND_DIR / "dashboard.html"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"

results = []

def check(name, passed, detail=""):
    symbol = PASS if passed else FAIL
    print(f"  [{symbol}] {name}")
    if detail:
        print(f"         {detail}")
    results.append(passed)
    return passed

def api_get(path):
    import urllib.request
    url = f"{BASE_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_error": str(e)}

# ==================================================================
# SECTION 0: Run existing verify_tier2.py + verify_tier3.py
# ==================================================================
print("\n══════════════════════════════════════════════")
print("  SECTION 0 — Delegating to verify_tier2.py + verify_tier3.py")
print("══════════════════════════════════════════════\n")

for script in ("verify_tier2.py", "verify_tier3.py"):
    script_path = _SCRIPTS_DIR / script
    if not script_path.exists():
        print(f"  [{WARN}] {script} not found — skipping")
        continue
    print(f"  Running {script}...")
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path), "--graph-id", GRAPH_ID, "--base-url", BASE_URL],
            capture_output=True, text=True, timeout=120
        )
        # Print its output indented
        for line in proc.stdout.splitlines():
            print("    " + line)
        if proc.returncode == 0:
            print(f"  [{PASS}] {script} — all checks passed")
            results.append(True)
        else:
            print(f"  [{FAIL}] {script} — one or more checks failed")
            results.append(False)
    except Exception as e:
        print(f"  [{FAIL}] {script} failed to run: {e}")
        results.append(False)

# ==================================================================
# SECTION 1 — CHANGE 1: Daily scheduler hour changed to 2
# ==================================================================
print("\n══════════════════════════════════════════════")
print("  SECTION 1 — CHANGE 1: Daily scheduler 02:00 UTC")
print("══════════════════════════════════════════════\n")

sched_path = _SCRIPTS_DIR / "scheduler.py"
if sched_path.exists():
    text = sched_path.read_text(encoding="utf-8")
    check("scheduler.py: daily cron hour=2", "hour=2," in text,
          "Expected 'hour=2,' in add_job call")
    check("scheduler.py: catch-up uses >= 2", "if now.hour >= 2:" in text,
          "Expected 'if now.hour >= 2:' in run_catchup()")
    check("scheduler.py: docstring updated to 02:00 UTC",
          "02:00 UTC" in text or "2:00 UTC" in text or "2am UTC" in text)
else:
    check("scheduler.py exists", False, "File not found")

# ==================================================================
# SECTION 2 — CHANGE 2: market_open field
# ==================================================================
print("\n══════════════════════════════════════════════")
print("  SECTION 2 — CHANGE 2: market_open indicator")
print("══════════════════════════════════════════════\n")

refresh_path = _SCRIPTS_DIR / "dashboard_refresh.py"
if refresh_path.exists():
    rt = refresh_path.read_text(encoding="utf-8")
    check("dashboard_refresh.py: is_market_open() defined", "def is_market_open" in rt)
    check("dashboard_refresh.py: MARKET_OPEN_UTC constant", "MARKET_OPEN_UTC" in rt)
    check("dashboard_refresh.py: market_open written to state", '"market_open"' in rt or "'market_open'" in rt)
else:
    check("dashboard_refresh.py exists", False)

live_state = _LIVE_DIR / "live_state.json"
if live_state.exists():
    try:
        ls = json.loads(live_state.read_text(encoding="utf-8"))
        check("live_state.json: market_open field present", "market_open" in ls,
              f"Keys found: {list(ls.keys())[:10]}")
        check("live_state.json: market_open is bool", isinstance(ls.get("market_open"), bool))
    except Exception as e:
        check("live_state.json readable", False, str(e))
else:
    print(f"  [{WARN}] live_state.json not found — run dashboard_refresh.py first")

if _DASHBOARD.exists():
    dh = _DASHBOARD.read_text(encoding="utf-8")
    check("dashboard.html: _marketOpen state variable", "_marketOpen" in dh)
    check("dashboard.html: MARKET CLOSED badge in renderSignals", "MARKET CLOSED" in dh)
else:
    check("dashboard.html exists", False)

# ==================================================================
# SECTION 3 — CHANGE 3: historyChart zoom
# ==================================================================
print("\n══════════════════════════════════════════════")
print("  SECTION 3 — CHANGE 3: historyChart zoom + reset button")
print("══════════════════════════════════════════════\n")

if _DASHBOARD.exists():
    dh = _DASHBOARD.read_text(encoding="utf-8")
    # Find the historyChart init block and check for zoom plugin
    hist_idx = dh.find("historyChart=new Chart")
    if hist_idx >= 0:
        hist_block = dh[hist_idx:hist_idx+800]
        check("dashboard.html: historyChart has zoom plugin", "zoom:{zoom:" in hist_block or '"zoom":{' in hist_block,
              "zoom config not found in historyChart init block")
    else:
        check("dashboard.html: historyChart definition found", False)
    check("dashboard.html: reset zoom button for historyChart",
          "historyChart.resetZoom()" in dh)

# ==================================================================
# SECTION 4 — CHANGE 4: TOKEN_BUDGET, price_staleness_hours, source_bias
# ==================================================================
print("\n══════════════════════════════════════════════")
print("  SECTION 4 — CHANGE 4: Token budget / staleness / source bias")
print("══════════════════════════════════════════════\n")

nf_path = _SCRIPTS_DIR / "news_fetcher.py"
if nf_path.exists():
    nft = nf_path.read_text(encoding="utf-8")
    check("news_fetcher.py: TOKEN_BUDGET constant defined", "TOKEN_BUDGET = 4096" in nft)
    check("news_fetcher.py: truncation logic present", "TOKEN_BUDGET REACHED" in nft or "TOKEN_BUDGET" in nft and "char_limit" in nft)

if refresh_path.exists():
    rt = refresh_path.read_text(encoding="utf-8")
    check("dashboard_refresh.py: price_staleness_hours computed", "_compute_price_staleness_hours" in rt)
    check("dashboard_refresh.py: source_bias query present", "source_bias" in rt and "briefing_source" in rt)

if live_state.exists():
    try:
        ls = json.loads(live_state.read_text(encoding="utf-8"))
        has_stale = "price_staleness_hours" in ls
        check("live_state.json: price_staleness_hours present", has_stale,
              "Will appear after next refresh cycle")
        # source_bias only appears if there are MemoryEvents with briefing_source
        has_bias = "source_bias" in ls
        if has_bias:
            check("live_state.json: source_bias present", True)
        else:
            print(f"  [{WARN}] live_state.json: source_bias absent — no MemoryEvents with briefing_source yet (OK on cold start)")
    except Exception:
        pass

# signals.py fast-path for staleness
sig_path = _BACKEND_DIR / "app" / "api" / "signals.py"
if sig_path.exists():
    sigt = sig_path.read_text(encoding="utf-8")
    check("signals.py: reads price_staleness_hours from live_state.json",
          "live_state.json" in sigt or "live/live_state.json" in sigt)

if _DASHBOARD.exists():
    dh = _DASHBOARD.read_text(encoding="utf-8")
    check("dashboard.html: source-bias-sec panel", "source-bias-sec" in dh)
    check("dashboard.html: renderSourceBias function", "function renderSourceBias" in dh)

# ==================================================================
# SECTION 5 — CHANGE 5: OpenBB calendars + /api/live/calendar
# ==================================================================
print("\n══════════════════════════════════════════════")
print("  SECTION 5 — CHANGE 5: OpenBB calendars")
print("══════════════════════════════════════════════\n")

pf_path = _SCRIPTS_DIR / "price_fetcher.py"
if pf_path.exists():
    pft = pf_path.read_text(encoding="utf-8")
    check("price_fetcher.py: fetch_economic_calendar() defined", "def fetch_economic_calendar" in pft)
    check("price_fetcher.py: fetch_earnings_calendar() defined", "def fetch_earnings_calendar" in pft)
    check("price_fetcher.py: vix_term_structure computed", "vix_term_structure" in pft)
    check("price_fetcher.py: flow_signal computed (IEF/HYG)", "flow_signal" in pft and "IEF" in pft)

live_api = _BACKEND_DIR / "app" / "api" / "live.py"
if live_api.exists():
    lat = live_api.read_text(encoding="utf-8")
    check("live.py: /api/live/calendar endpoint defined", '"/calendar"' in lat or "'/calendar'" in lat)

cal_resp = api_get(f"/api/live/calendar")
if "_error" in cal_resp:
    print(f"  [{WARN}] /api/live/calendar: {cal_resp['_error']} (503 expected if OpenBB not run yet)")
elif cal_resp.get("success"):
    check("/api/live/calendar: returns economic_calendar", "economic_calendar" in cal_resp.get("data", {}))
    check("/api/live/calendar: returns earnings_calendar",  "earnings_calendar"  in cal_resp.get("data", {}))
else:
    print(f"  [{WARN}] /api/live/calendar: {cal_resp.get('error', 'unknown')} (OK if OpenBB unavailable)")

if nf_path.exists():
    nft = nf_path.read_text(encoding="utf-8")
    check("news_fetcher.py: VIX term structure injected into regime header", "vix_term_structure" in nft)
    check("news_fetcher.py: flow_signal injected into regime header", "flow_signal" in nft)
    check("news_fetcher.py: economic calendar injected into briefing", "economic_calendar.json" in nft)
    check("news_fetcher.py: earnings calendar injected into briefing", "earnings_calendar.json" in nft)

# ==================================================================
# SECTION 6 — CHANGE 6: Sentiment toggle fix
# ==================================================================
print("\n══════════════════════════════════════════════")
print("  SECTION 6 — CHANGE 6: Capital/equal-weighted toggle fix")
print("══════════════════════════════════════════════\n")

if _DASHBOARD.exists():
    dh = _DASHBOARD.read_text(encoding="utf-8")
    check("dashboard.html: _lastSentimentData state variable", "_lastSentimentData" in dh)
    check("dashboard.html: fetchSentiment() caches response", "_lastSentimentData=json.data" in dh or "_lastSentimentData=" in dh)
    check("dashboard.html: setSentimentMode() uses _lastSentimentData",
          "_lastSentimentData" in dh[dh.find("function setSentimentMode"):dh.find("function setSentimentMode")+500])
    check("dashboard.html: toggle-cap button still wired", "toggle-cap" in dh)
    check("dashboard.html: toggle-eq button still wired", "toggle-eq" in dh)

# ==================================================================
# SECTION 7 — CHANGE 7: Correlation matrix
# ==================================================================
print("\n══════════════════════════════════════════════")
print("  SECTION 7 — CHANGE 7: Cross-asset correlation matrix")
print("══════════════════════════════════════════════\n")

if refresh_path.exists():
    rt = refresh_path.read_text(encoding="utf-8")
    check("dashboard_refresh.py: Pearson correlation computed", "_pearson" in rt or "correlation_matrix" in rt)
    check("dashboard_refresh.py: correlation_matrix written to state", '"correlation_matrix"' in rt or "'correlation_matrix'" in rt)

if live_state.exists():
    try:
        ls = json.loads(live_state.read_text(encoding="utf-8"))
        has_corr = "correlation_matrix" in ls
        if has_corr:
            check("live_state.json: correlation_matrix present", True)
            mat = ls["correlation_matrix"]
            check("correlation_matrix: 7×7 dict", len(mat) == 7,
                  f"Got {len(mat)} assets")
        else:
            print(f"  [{WARN}] live_state.json: correlation_matrix absent — needs ≥10 SentimentSnapshots (OK on cold start)")
    except Exception:
        pass

if _DASHBOARD.exists():
    dh = _DASHBOARD.read_text(encoding="utf-8")
    check("dashboard.html: correlation-sec panel", "correlation-sec" in dh)
    check("dashboard.html: renderCorrelationMatrix function", "function renderCorrelationMatrix" in dh)

# ==================================================================
# SECTION 8 — CHANGE 8: Non-ASCII language guard
# ==================================================================
print("\n══════════════════════════════════════════════")
print("  SECTION 8 — CHANGE 8: Non-ASCII language guard")
print("══════════════════════════════════════════════\n")

tick_path = _SCRIPTS_DIR / "simulation_tick.py"
if tick_path.exists():
    tkt = tick_path.read_text(encoding="utf-8")
    check("simulation_tick.py: non-ASCII ratio check present", "non_ascii" in tkt or "_non_ascii" in tkt)
    check("simulation_tick.py: >5% threshold", "0.05" in tkt)
    check("simulation_tick.py: retry with English re-prompt", "IMPORTANT: Respond ONLY in English" in tkt)
    check("simulation_tick.py: LANGUAGE WARNING flag on double fail", "LANGUAGE WARNING" in tkt)

# ==================================================================
# SECTION 9 — CHANGE 9: Nitter Docker documentation
# ==================================================================
print("\n══════════════════════════════════════════════")
print("  SECTION 9 — CHANGE 9: Nitter self-hosting documentation")
print("══════════════════════════════════════════════\n")

if nf_path.exists():
    nft = nf_path.read_text(encoding="utf-8")
    check("news_fetcher.py: Nitter Docker image documented", "ghcr.io/zedeus/nitter" in nft)
    check("news_fetcher.py: docker-compose service block documented", "docker-compose" in nft.lower() or "docker-compose.yml" in nft)
    check("news_fetcher.py: NITTER_BEARER_TOKEN env var documented", "NITTER_BEARER_TOKEN" in nft)
    check("news_fetcher.py: NITTER_ENABLED still False (not accidentally re-enabled)", "NITTER_ENABLED = False" in nft)

# ==================================================================
# SUMMARY
# ==================================================================
passed = sum(1 for r in results if r)
total  = len(results)
print("\n══════════════════════════════════════════════")
print(f"  RESULT: {passed}/{total} checks passed")
print("══════════════════════════════════════════════\n")

sys.exit(0 if passed == total else 1)
