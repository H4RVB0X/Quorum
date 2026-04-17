# MiroFish-Offline — Project Memory

---

## Early Sessions Summary (2026-04-11 through 2026-04-13)

### Agent pool
`generate_agents.py` creates 8,192 agents with archetype-conditional trait distributions. Pool composition: retail_amateur 50%, retail_experienced 20%, prop_trader 8%, fund_manager 8%, hedge_fund 6%, family_office 5%, pension_fund 3%. Capital ranges from real AUM data. MERGE key is `uuid` (not name_lower — prevents collision overwriting). Fixed RNG seed 42. `--clear` deletes all agents and their MemoryEvents. Old `agent_evolver.py` is superseded but still in repo.

### Scheduler
- Daily job has priority: `hourly_job()` bails immediately at top if `_daily_running` is set. Secondary re-check before tick as race-condition safety net.
- Daily pre-pass: if NewsChunk graph links exist for the current briefing, skip `process_briefing` (prevents `MENTIONED_WITH.weight` inflation from duplicate runs). If missing, run once before `run_tick`.

### NER pipeline
spaCy `en_core_web_sm` replaced the LLM-per-chunk pipeline (was 6–8h; now <2 min). All pipeline details and filters documented in CLAUDE.md NER section.

### Graph relationships
`incremental_update.py` writes `:NewsChunk` nodes (keyed by graph_id + chunk_id with sha256 prefix), `:MENTIONED_IN` entity→chunk links, and weighted `:MENTIONED_WITH` co-occurrence edges (all batched UNWIND). `simulation_tick.py` calls `load_briefing_link_context()` once per tick to inject top co-mention pairs into agent system prompts. `write_memory_event()` creates `(:MemoryEvent)-[:CONCERNS]->(:Entity)` links from `assets_mentioned`. Historical briefings not backfilled. `MENTIONED_WITH.weight` is cumulative — only run incremental_update once per briefing.

### Sentiment API
`GET /api/investors/sentiment` — capital-weighted 24h and 7d, plus equal-weighted. `_reaction_score()` and `compute_sentiment_scores()` are pure functions importable without Flask. Original reaction mapping (pre-TIER-2B): `buy→+1, hedge→+0.5, hold→0, sell→-1, panic→-1`. Conviction model replaced this in TIER-2B; mapping only relevant for Check 8 diversity correlation (`hedge=-0.5` in that context).

### Dashboard
Layout: sidebar (260px sticky) + main flex split. Signal strip above split. Header with 7 `.mkt-dot` market state dots. Single self-contained HTML file. `fetchAll()` every 30s + `fetchLiveState()` every 15 min — additive. `detectAlerts()` compares latest vs 4 ticks back, delta ≥ ±0.25 → `.alert-chip`.

### Volume mount and shared state
`backend/live/` bind-mounted to container. `backend/logs/` created at runtime (not in git). Two .env files: root for host scripts (localhost), backend for container (neo4j/ollama). Full details in CLAUDE.md Key Facts and Deploy sections.

### Tooling
- `force_retick.py` — full reset: backdates scheduler log, clears seen_urls window, deletes checkpoint + contagion flag, wipes all graph-scoped MemoryEvents (10k-node batches). Flags: `--graph-id`, `--keep-memory-events`, `--keep-checkpoint`, `--keep-contagion-flag`, `--dry-run`.
- `verify_agent_diversity.py` — 8-check diversity audit including Check 8 (reaction diversity / mode collapse / Pearson risk-reaction correlation). Reaction direction for correlation: `buy=+1.0, hold=0.0, hedge=-0.5, sell=-1.0, panic=-2.0`.
- In-memory NumPy cosine similarity for chunk retrieval (not FAISS) — `(n × d) @ (n × d).T` after L2-normalisation. Appropriate for current scale; FAISS needed at 10k+ agents.

---

## Session: 2026-04-14 — TIER 1 + Pipeline Hardening

### What was built
1. **Trait injection** (`simulation_tick.py`): `formative_crash` key `'dotcom'`→`'dotcom_2000'`; `'none'` omits the block. Key breakpoints finalised — see CLAUDE.md.
2. **Time-horizon gating** (30-min ticks only): `participation_probability = min(1.0, 30.0 / reaction_speed_minutes)`. Daily tick (`full=True`) bypasses entirely.
3. **Stratified sampling**: `load_agents_stratified()` — 7 archetype queries + 1 fill = 8 queries per tick vs 1 for random sampling.
4. **price_fetcher.py hardening**: network pre-check, per-ticker retry (3× with 5s delay), `compute_price_staleness_hours()` helper.
5. **signals.py history fix**: null prices instead of skipping missing dates, UTC-aware `day_start`, `data_quality: degraded` when staleness >36h, `price_staleness_hours` in current response.
6. **dashboard.html**: `spanGaps: false` on historyChart; price dataset hidden when all-null.
7. **scheduler.py**: `_check_ollama()` (1-token probe, polls 10s up to 60s); `prune_memory_events()` weekly job (Sunday 04:00 UTC, 90d TTL, LIMIT 10000 per run).

### Modified files
`simulation_tick.py`, `price_fetcher.py`, `scheduler.py`, `signals.py`, `dashboard.html`

---

## Session: 2026-04-14 (continuation) — TIER 2A/2B/2C/2D

### TIER 2A — Portfolio state tracking
New agent fields: `position_{asset}`, `entry_price_{asset}`, `last_reaction_{asset}` (7 assets × 3 fields). Written lazily — always use `coalesce(a.position_equities, 'flat')`. `update_agent_positions()` uses MATCH not MERGE (silent no-op on UUID mismatch). panic flattens ALL assets. Entry price set on flat→long only. `_POSITION_ASSETS = ['equities','crypto','bonds','commodities','fx','real_estate','mixed']` — canonical order.

### TIER 2B — Conviction scoring
New MemoryEvent fields: `direction` (-1/0/+1), `conviction` (0–1), `position_size` (0–1). Null on older events — legacy fallback in `compute_sentiment_scores()` uses `0.3` neutral position_size default. `compute_sentiment_scores()` rewritten with conviction model + leverage + decay.

### TIER 2C — Time-decay
λ=0.05, half-life ≈ 13.9h. Applied 24h only (`apply_decay=True`). Do NOT apply to 7d. `_SENTIMENT_QUERY` in `signals.py` returns timestamp/direction/conviction/position_size/leverage_typical.

### TIER 2D — Data quality gates
`low_participation: true` when event_count < 200 per asset → forced "neutral". Drawdown section in `dashboard_refresh.py`: queries last 48 SentimentSnapshots, computes peak/current/drawdown_pct per asset (≥5 snapshots required).

### Modified files
`simulation_tick.py`, `investors.py`, `signals.py`, `dashboard_refresh.py`

---

## Session: 2026-04-14 (continuation 2) — TIER 2C signals.py wiring + verify_tier2.py

`signals.py` `current_signals()` was not passing conviction/decay fields to `compute_sentiment_scores` — `_SENTIMENT_QUERY` was missing 5 fields. Fix: added `_DECAY_LAMBDA = 0.05` constant, updated query, 24h call now uses `apply_leverage=True, apply_decay=True`. `data_quality` always present (was only on degraded). Event freshness scans row timestamps; empty rows → `event_degraded=True` → degraded.

`verify_tier2.py`: 13 checks at `backend/scripts/verify_tier2.py`. 13/13 on first run.

### Modified files
`signals.py`, `verify_tier2.py` (new)

---

## Session: 2026-04-14 (continuation 3) — TIER 2C-ii/2C-iii/2D-i + TIER 3

### Dynamic thresholds (TIER 2C-ii)
`_compute_dynamic_thresholds()` queries last 48 snapshots; formula: `max(0.25, min(0.6, 0.5 × std × sqrt(48)))`. `_THRESHOLD_FALLBACK=0.4`. `_signal()` uses `None` sentinel (Python evaluates default args at definition time — `_THRESHOLD_FALLBACK` is defined after `_signal()`). `dynamic_threshold` always present in signal entries.

### Archetype split (TIER 2C-iii)
`GET /api/signals/archetype_split`. smart_money=hedge_fund+prop_trader, dumb_money=retail_amateur. `insufficient_data` if either group <20 events. Divergence = smart_money_score − dumb_money_score; >0.3 → smart_leads; ≤0.15 → converging.

### Panic contagion (TIER 2D-i)
`CONTAGION_FLAG_PATH = backend/briefings/contagion_flag.txt`. Written after tick if any archetype >50% panic rate. Read + deleted at START of next `run_tick()`. Injected only for agents with `herd_behaviour >= 7`. Flag deleted on clean tick too (prevents stale flags). `tick_reactions` entries include `archetype` field.

### Market Regime (TIER 3)
`compute_market_regime()` in `price_fetcher.py` — 4 dimensions: volatility, trend (MA-20/50), yield curve, fear. Written to `backend/live/regime.json`. `trend=INSUFFICIENT_DATA` until 20+ price days — briefings show `[MARKET REGIME: COMPUTING...]`. OpenBB optional (try/except); host uses yfinance fallbacks. `flow_signal` from IEF/HYG 5-day momentum. Price snapshot embeds `regime` key (backwards-compatible). `GET /api/live/regime` in `live.py`.

`verify_tier3.py`: 22 checks at `backend/scripts/verify_tier3.py`. `verify_all.py` combines tier2 + tier3 + 9 session checks.

### Modified files
`signals.py`, `simulation_tick.py`, `price_fetcher.py`, `news_fetcher.py`, `live.py`, `dashboard.html`, `verify_tier3.py` (new), `verify_all.py` (new)

---

## Session: 2026-04-14 — force_retick.py Full Reset Fix

Updated `force_retick.py` to perform full reset: existing behaviour (backdate log, clear seen_urls) plus: delete `tick_checkpoint.json`, delete `contagion_flag.txt`, graph-scoped MemoryEvent wipe (10k-node DETACH DELETE batches). New flags: `--graph-id`, `--keep-memory-events`, `--keep-checkpoint`, `--keep-contagion-flag`. If Neo4j unavailable, exits with error.

---

## Session: 2026-04-14 — Graph Relationship Restoration

`incremental_update.py` now writes `:NewsChunk` nodes, `:MENTIONED_IN` entity→chunk links, and weighted `:MENTIONED_WITH` co-occurrence pairs — all batched UNWIND. Chunk IDs: briefing_stem + chunk_index + sha256 prefix (deterministic, collision-resistant). Logging reports entity merges, NewsChunk merges, MENTIONED_IN links, and MENTIONED_WITH pairs.

`simulation_tick.py` adds `load_briefing_link_context()` — queries top co-mention pairs from NewsChunk+MENTIONED_IN for current briefing, injects graph-link summary into system prompt. `write_memory_event()` creates `CONCERNS` edges to asset class entities; creates entity node as `type='AssetClass'` if missing.

Historical briefings not backfilled. `MENTIONED_WITH.weight` cumulative across runs — only run incremental_update once per briefing.

### Modified files
`incremental_update.py`, `simulation_tick.py`

---

## Session: 2026-04-16 — 9 Changes

1. **Daily cron 02:00 UTC** (was 20:00). Catch-up condition `>= 2`. Old code checking `>= 20` must be updated.
2. **Market open indicator** in `dashboard_refresh.py`: `is_market_open()` Mon–Fri 13:30–20:00 UTC, writes `market_open: bool`. Dashboard: non-crypto signal tiles grey out when closed (`_marketOpen` default `true` prevents flash-of-grey). Crypto never greys out.
3. **historyChart zoom/pan** — identical config to tickSentChart, reset zoom button added.
4. **Data integrity**: `TOKEN_BUDGET` removed from `news_fetcher.py` — briefings now unlimited. `price_staleness_hours` written to `live_state.json` by `dashboard_refresh.py`; `signals.py` reads it as fast path before computing from price files. **`verify_all.py` check for `TOKEN_BUDGET = 4096` now fails — skip it.**
5. **OpenBB expanded**: VIX term structure (VIX3M vs VIX → contango/backwardation/flat) and `flow_signal` (IEF/HYG 5-day momentum) added to `regime.json`. `fetch_economic_calendar()` and `fetch_earnings_calendar()` write to `backend/live/` via `fetch_prices()`. `GET /api/live/calendar` in `live.py`. Calendar fetches use `provider="fmp"` — fallback to empty list if FMP not configured.
6. **Equal-weighted toggle fix**: `_lastSentimentData` module-level cache in dashboard; `setSentimentMode('equal')` renders current snapshot as single-point chart (no historical equal-weighted series exists). Toggle does nothing until first `fetchSentiment()` completes.
7. **Correlation matrix**: computed in `dashboard_refresh.py` (same 48-snapshot window as drawdowns, pure Python `_pearson()`), written as `correlation_matrix` nested dict. Requires ≥10 snapshots per asset.
8. **Language guard** in `call_llm_for_agent()`: checks `result['reasoning']` for >5% non-ASCII. Retries with "IMPORTANT: Respond ONLY in English…" injected. Double-fail: preserves reaction/direction/conviction, prefixes reasoning with `[LANGUAGE WARNING: partial non-English response]`.
9. **Nitter self-hosting docs**: comment block in `news_fetcher.py` with Docker image (`ghcr.io/zedeus/nitter:latest`), docker-compose snippet, required env vars.

### Modified files
`scheduler.py`, `dashboard_refresh.py`, `news_fetcher.py`, `price_fetcher.py`, `simulation_tick.py`, `signals.py`, `live.py`, `dashboard.html`, `verify_all.py` (new)

---

## Session: 2026-04-16 — Reddit/Stocktwits/Embedding Cache/Drilldown/Aliases/Token Budget

### Changes

1. **Reddit** (`news_fetcher.py`): `REDDIT_ENABLED = True`, JSON API (`/r/{sub}/new.json?limit=25`), User-Agent required, 6 subreddits with min_score gates (SecurityAnalysis=10, investing/economics/finance=50, stocks=100, wallstreetbets=200 + flair filter), passes `filter_articles()`, cap 15 articles (highest-score first), dedup `"reddit_{post_id}"`.

2. **Stocktwits** (`news_fetcher.py`): `STOCKTWITS_ENABLED = True`, 7 symbols, bypasses `filter_articles()` (ticker-specific by construction), cap 5 msgs/symbol aggregated to 1 article, dedup `"stocktwits_{message_id}"`. Rate limit: 200 req/hour unauthenticated; 7 symbols = 14/hour safe.

3. **Sidecar format changed to dict**: `{"sources": [{source, count}...], "reddit": {subreddit: count}, "stocktwits": {symbol: count}}`. `investors.py` and `dashboard_refresh.py` both handle old list format and new dict format.

4. **Embedding cache** (`simulation_tick.py`): `BRIEFING_CACHE_DIR = backend/briefing_cache/` (container; not bind-mounted — wiped on force-recreate, no crash). Cache key: sha256[:16] of full briefing text. Atomic write via `.tmp` rename. 10-file prune on save. All in try/except — tick never crashes.

5. **Drilldown** (`signals.py` + `dashboard.html`): `GET /api/signals/drilldown?graph_id=...&asset=equities&limit=20`. Orders by `capital × coalesce(conviction,0.5) × coalesce(position_size,0.3) DESC`. `asset` must match one of 7 asset_class_bias values. `reasoning` truncated 250 chars. Signal tiles now `cursor:pointer` with `onclick="openDrilldown(asset)"`. Modal close: overlay click, × button, or Escape.

6. **Entity aliases** (`incremental_update.py`): `ENTITY_ALIASES` dict (32 entries), `normalise_entity_name()` applied first in pipeline (before ENTITY_REMOVE_LIST). `verify_new_features.py` check 9 requires ≥20 entries.

7. **Token budget removed** — briefings unlimited. `verify_all.py` check for `TOKEN_BUDGET = 4096` fails — skip it.

`verify_new_features.py`: 13 checks at `backend/scripts/verify_new_features.py`. All 13 passing.

### Modified files
`news_fetcher.py`, `simulation_tick.py`, `signals.py`, `incremental_update.py`, `dashboard.html`, `investors.py`, `dashboard_refresh.py`, `verify_new_features.py` (new)
