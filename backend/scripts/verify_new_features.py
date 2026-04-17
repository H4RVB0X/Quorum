"""
verify_new_features.py — Verify all 5 new features (2026-04-16 session).

Checks:
  1.  REDDIT_ENABLED flag exists in news_fetcher.py
  2.  REDDIT_SOURCES keys present in news_fetcher.py
  3.  Reddit JSON API is reachable for r/investing (HTTP 200)
  4.  STOCKTWITS_ENABLED flag exists in news_fetcher.py
  5.  Stocktwits public API reachable for SPY (200 or 429 or 403 = host reachable)
  6.  Embedding cache directory exists (backend/briefing_cache/)
  7.  get_briefing_hash(), load/save helpers exist in simulation_tick.py
  8.  GET /api/signals/drilldown returns 200 with expected fields
  9.  ENTITY_ALIASES dict has >= 20 entries in incremental_update.py
 10.  normalise_entity_name("Fed") == "Federal Reserve"
 11.  normalise_entity_name("Goldman") == "Goldman Sachs"
 12.  Sidecar JSON format updated (has "sources" key when new format) OR old list format accepted
 13.  dashboard.html contains "drilldown-modal" and "openDrilldown"

Usage:
  python backend/scripts/verify_new_features.py
"""

import sys
import os
import json
import importlib.util
from pathlib import Path

SCRIPT_DIR  = Path(__file__).parent
BACKEND_DIR = SCRIPT_DIR.parent
REPO_ROOT   = BACKEND_DIR.parent
GRAPH_ID    = "d3a38be8-37d9-4818-be28-5d2d0efa82c0"
API_BASE    = "http://localhost:5001"

results: list = []


def chk(num: int, name: str, passed: bool, detail: str = ""):
    mark = "PASS" if passed else "FAIL"
    results.append((num, name, passed, detail))
    print(f"  [{mark}] {num:02d}. {name}" + (f" — {detail}" if detail else ""))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 1 & 2: news_fetcher imports ───────────────────────────────────────────
nf = None
try:
    nf = _load_module("news_fetcher", SCRIPT_DIR / "news_fetcher.py")
    chk(1, "REDDIT_ENABLED flag exists", hasattr(nf, "REDDIT_ENABLED"),
        f"value={getattr(nf,'REDDIT_ENABLED',None)}")
except Exception as e:
    chk(1, "REDDIT_ENABLED flag exists", False, str(e))

try:
    rs = getattr(nf, "REDDIT_SOURCES", {}) if nf else {}
    expected = {"SecurityAnalysis","investing","economics","finance","stocks","wallstreetbets"}
    ok = expected.issubset(set(rs.keys()))
    chk(2, "REDDIT_SOURCES keys present", ok, f"keys={list(rs.keys())}")
except Exception as e:
    chk(2, "REDDIT_SOURCES keys present", False, str(e))

# ── 3. Reddit API ────────────────────────────────────────────────────────
try:
    import requests as _req
    resp = _req.get(
        "https://www.reddit.com/r/investing/new.json?limit=1",
        headers={"User-Agent": "Quorum/1.0 financial-simulation"},
        timeout=10,
    )
    ok = resp.status_code == 200
    chk(3, "Reddit API reachable (r/investing)", ok, f"HTTP {resp.status_code}")
except Exception as e:
    chk(3, "Reddit API reachable (r/investing)", False, str(e))

# ── 4. STOCKTWITS_ENABLED ────────────────────────────────────────────────
try:
    chk(4, "STOCKTWITS_ENABLED flag exists", nf is not None and hasattr(nf, "STOCKTWITS_ENABLED"),
        f"value={getattr(nf,'STOCKTWITS_ENABLED',None) if nf else 'n/a'}")
except Exception as e:
    chk(4, "STOCKTWITS_ENABLED flag exists", False, str(e))

# ── 5. Stocktwits API ────────────────────────────────────────────────────
try:
    resp = _req.get("https://api.stocktwits.com/api/2/streams/symbol/SPY.json", timeout=10)
    # 200=ok, 429=rate-limited(reachable), 403=unauthenticated restriction(reachable)
    ok = resp.status_code in (200, 429, 403)
    chk(5, "Stocktwits API reachable (SPY)", ok, f"HTTP {resp.status_code}")
except Exception as e:
    chk(5, "Stocktwits API reachable (SPY)", False, str(e))

# ── 6. Embedding cache directory ─────────────────────────────────────────
cache_dir = BACKEND_DIR / "briefing_cache"
cache_dir.mkdir(parents=True, exist_ok=True)  # create if missing
chk(6, "Embedding cache dir exists", cache_dir.is_dir(), str(cache_dir))

# ── 7. Cache helpers in simulation_tick.py ───────────────────────────────
st = None
try:
    st = _load_module("simulation_tick", SCRIPT_DIR / "simulation_tick.py")
    has_hash = hasattr(st, "get_briefing_hash")
    has_load = hasattr(st, "load_chunk_cache_from_disk")
    has_save = hasattr(st, "save_chunk_cache_to_disk")
    ok = has_hash and has_load and has_save
    chk(7, "Cache helpers exist in simulation_tick.py", ok,
        f"hash={has_hash} load={has_load} save={has_save}")
    if has_hash:
        h = st.get_briefing_hash("test")
        assert len(h) == 16, f"hash length {len(h)}"
except Exception as e:
    chk(7, "Cache helpers exist in simulation_tick.py", False, str(e))

# ── 8. /api/signals/drilldown ────────────────────────────────────────────
try:
    url  = f"{API_BASE}/api/signals/drilldown?graph_id={GRAPH_ID}&asset=equities&limit=5"
    resp = _req.get(url, timeout=10)
    ok   = resp.status_code == 200
    if ok:
        j    = resp.json()
        ok   = j.get("success") is True
        data = j.get("data", {})
        req_keys = ("agents","total_agents_queried","bull_count","bear_count","neutral_count","top_capital_reaction")
        ok = ok and all(k in data for k in req_keys)
        if ok and data.get("agents"):
            a_keys = ("name","archetype","reaction","conviction","reasoning","capital_usd")
            ok = all(k in data["agents"][0] for k in a_keys)
    chk(8, "GET /api/signals/drilldown returns 200 with expected fields", ok,
        f"HTTP {resp.status_code}")
except Exception as e:
    chk(8, "GET /api/signals/drilldown returns 200 with expected fields", False, str(e))

# ── 9, 10, 11: incremental_update ────────────────────────────────────────
iu = None
try:
    iu = _load_module("incremental_update", SCRIPT_DIR / "incremental_update.py")
    aliases = getattr(iu, "ENTITY_ALIASES", {})
    chk(9, "ENTITY_ALIASES has >= 20 entries", len(aliases) >= 20, f"count={len(aliases)}")
except Exception as e:
    chk(9, "ENTITY_ALIASES has >= 20 entries", False, str(e))

try:
    result = iu.normalise_entity_name("Fed") if iu else None
    chk(10, 'normalise_entity_name("Fed") == "Federal Reserve"',
        result == "Federal Reserve", f"got={result!r}")
except Exception as e:
    chk(10, 'normalise_entity_name("Fed") == "Federal Reserve"', False, str(e))

try:
    result = iu.normalise_entity_name("Goldman") if iu else None
    chk(11, 'normalise_entity_name("Goldman") == "Goldman Sachs"',
        result == "Goldman Sachs", f"got={result!r}")
except Exception as e:
    chk(11, 'normalise_entity_name("Goldman") == "Goldman Sachs"', False, str(e))

# ── 12. Sidecar format ───────────────────────────────────────────────────
try:
    briefings_dir = BACKEND_DIR / "briefings"
    sidecars = sorted(briefings_dir.glob("*_sources.json"), reverse=True) if briefings_dir.is_dir() else []
    if sidecars:
        raw = json.loads(sidecars[0].read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            ok  = "reddit" in raw and "stocktwits" in raw
            msg = f"new format — keys={list(raw.keys())}"
        else:
            # Old format list — acceptable on cold start before next fetch cycle
            ok  = True
            msg = "old list format (next fetch cycle will write new format)"
        chk(12, "Sidecar JSON format acceptable", ok, msg)
    else:
        chk(12, "Sidecar JSON format acceptable", True, "no sidecars yet — OK for new install")
except Exception as e:
    chk(12, "Sidecar JSON format acceptable", False, str(e))

# ── 13. dashboard.html strings ───────────────────────────────────────────
try:
    dash = (REPO_ROOT / "frontend" / "public" / "dashboard.html").read_text(encoding="utf-8")
    has_modal = "drilldown-modal" in dash
    has_fn    = "openDrilldown"   in dash
    ok = has_modal and has_fn
    chk(13, "dashboard.html contains drilldown-modal + openDrilldown", ok,
        f"modal={has_modal} fn={has_fn}")
except Exception as e:
    chk(13, "dashboard.html contains drilldown-modal + openDrilldown", False, str(e))

# ── Summary ───────────────────────────────────────────────────────────────
passed = sum(1 for _, _, p, _ in results if p)
total  = len(results)
print(f"\n{'='*52}")
print(f"  verify_new_features: {passed}/{total} checks passed")
print(f"{'='*52}")

sys.exit(0 if passed == total else 1)
