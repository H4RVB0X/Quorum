# Quorum — Claude Code Instructions

## Read First
Before doing anything, read PROJECT_MEMORY.md in full.

## At the End of Every Session
Append a new session block to PROJECT_MEMORY.md following the exact same format as existing entries. Be specific — future Claude sessions rely entirely on this document. Do not skip this step.

## Key Facts
- Graph ID: d3a38be8-37d9-4818-be28-5d2d0efa82c0
- Root `.env`: localhost hostnames (host Python scripts); `backend/.env`: neo4j/ollama hostnames (Docker containers)
- Dashboard: `localhost:5001/dashboard`; project called Quorum in docs, mirofish in internal code
- After any backend change: `docker cp <file> mirofish-offline:/app/backend/...` + `docker restart mirofish-offline`
- **NEVER `docker-compose up --force-recreate`** — wipes all `docker cp`'d files. Only recreate when changing `docker-compose.yml`, then immediately recopy every modified file.

## Deploy: Two Required Processes
```
python backend/scripts/scheduler.py --graph-id d3a38be8-37d9-4818-be28-5d2d0efa82c0
python backend/scripts/dashboard_refresh.py --graph-id d3a38be8-37d9-4818-be28-5d2d0efa82c0
```
These are completely separate processes. Do NOT add `dashboard_refresh.py` as a job inside `scheduler.py`.

## Shared State: `backend/live/`
`docker-compose.yml` bind-mounts `./backend/live:/app/backend/live` — the ONLY way host-written files reach Flask. Files: `live_state.json`, `price_sentiment_history.json`, `regime.json`, `economic_calendar.json`, `earnings_calendar.json`. All written atomically (tmp + rename). Old path `backend/live_state.json` is outside the mount — invisible to Flask.

`dashboard_refresh.py` loads ROOT `.env` (two levels up from `backend/scripts/`), NOT `backend/.env`. Loading `backend/.env` on the host causes DNS failures (`neo4j:7687` unresolvable). Default fallback in `_get_neo4j_driver()` is `bolt://localhost:7687`.

## News Feed Gotchas
- **Central bank feeds bypass `filter_articles()`** — `CENTRAL_BANK_FEEDS` (Fed, ECB, BoE) are always financially relevant. Do NOT add them to `RSS_FEEDS` or `NITTER_FEEDS`.
- **`NITTER_ENABLED = False`** — all public nitter.net instances timeout. Re-enable only with self-hosted Nitter in Docker.
- **Removed feeds** (comments in source mark each): Bloomberg Markets (403), Unusual Whales (no public RSS), Investopedia (402), TheStreet (403).
- **Benzinga and Seeking Alpha: 5s timeout** — `_TIMEOUT_FEEDS` set in `parse_feed()`. On timeout logs `"{source_name} timed out"` and returns `[]`. All other feeds have no timeout.
- **Relevance filter is a whitelist** — expand `FINANCIAL_TERMS` in `news_relevance_filter.py` if briefings are too short. Common false negatives: Nvidia, Apple, Tesla, emerging-market indices.
- **`TOKEN_BUDGET` removed** — briefings are now unlimited. If Ollama context overflows, re-add a char limit in `write_briefing()`. `verify_all.py` check for `TOKEN_BUDGET = 4096` will fail — skip it.
- **Reddit uses JSON API not RSS** — `https://www.reddit.com/r/{sub}/new.json?limit=25`. User-Agent "Quorum/1.0 financial-simulation" required. `REDDIT_ENABLED = True`. Per-subreddit 429 does not abort fetch. Cap: 15 articles sorted by score. Min scores: SecurityAnalysis=10, investing/economics/finance=50, stocks=100, wallstreetbets=200 (WSB also requires approved flair). Dedup key: `"reddit_{post_id}"`.
- **Stocktwits bypasses `filter_articles()`** — ticker-specific by construction. `STOCKTWITS_ENABLED = True`. Rate limit: 200 req/hour unauthenticated; 7 symbols = 14/hour. Do NOT expand beyond ~20 without auth. One combined article per symbol (top-5 messages). Dedup key: `"stocktwits_{message_id}"`.
- **Sidecar is a dict**: `{"sources": [{source, count}...], "reddit": {subreddit: count}, "stocktwits": {symbol: count}}`. Old list format handled. Counts are pre-filter. Do NOT change to post-filter — relevance filter is aggressive, most feeds would appear empty.

## NER / Knowledge Graph Gotchas
- **spaCy replaces LLM NER** — `en_core_web_sm` loaded at module level. Container install (uv venv only): `docker exec mirofish-offline uv pip install spacy "<wheel>" --python /app/backend/.venv/bin/python`. Do NOT use system `pip`.
- **Entity pipeline order**: `normalise_entity_name()` → SPACY_LABEL_MAP → `ENTITY_TYPE_CORRECTIONS` → `ENTITY_REMOVE_LIST` → 3-char min → single-token Person drop → per-chunk dedup.
- **`normalise_entity_name()` runs FIRST** — "BTC"→"Bitcoin" before ENTITY_REMOVE_LIST, so Bitcoin passes. To remove an alias target, add it to ENTITY_REMOVE_LIST separately.
- **`ENTITY_ALIASES` has 32 entries** — `verify_new_features.py` check 9 requires ≥20. Aliases are case-sensitive exact matches.
- **`ENTITY_TYPE_CORRECTIONS`** — applied after normalisation. Bloomberg, Reuters, CNBC, Nasdaq, Fed et al. → Company; Washington, White House → Location. Extend when top_entities panel shows wrong types.
- **`ENTITY_REMOVE_LIST`** — financial metrics (ARPU, CapEx, EBITDA, EPS, GDP, CPI, PNT), acronyms (ETF, IPO, CEO/CFO/COO/CTO), tickers (SPY, QQQ, TLT, GLD), FX/crypto (VIX, USD, EUR, GBP, JPY, BTC, ETH). Case-sensitive.
- **Single-token Person names dropped** — "Matt" is noise; "Matt Desch" passes. Runs after type resolution.
- **Neo4j writes batched (BATCH_SIZE=500)** — do not replace with single-entity MERGEs at 2160+ chunks.
- **`is_synthetic` guard on ON MATCH** — NER never overwrites agent nodes.
- **`MENTIONED_WITH.weight` is cumulative** — rerunning same briefing inflates weights. Run `incremental_update.py` once per briefing only.
- **`central_bank_source` always `false`** — schema-ready but flat `.txt` format lacks per-chunk source metadata.

## LLM / Prompt Gotchas
- **qwen2.5:14b code-switches to Chinese** on FOMO/emotional language. `"You must respond entirely in English. Do not use any other language."` must be the FIRST line of the system prompt in `build_prompt()`. Do not remove or relocate.
- **hedge requires naming gate** — current definition requires stating exact instrument + exposure: "If you cannot name the instrument and the risk, choose hold instead." Do not soften.
- **panic is scenario-conditional** — explicit triggers in `build_prompt()`: market crash, circuit breaker, trading halt, emergency rate hike, bank collapse, war escalation, systemic crisis. "you do not think — you react" framing is load-bearing.
- **Language guard in `call_llm_for_agent()`** — checks `result['reasoning']` for >5% non-ASCII (`sum(ord(c) > 127 for c in text) / len(text) > 0.05`). Retries with English injection. Double-fail: preserves reaction/direction/conviction, prefixes reasoning with `[LANGUAGE WARNING: partial non-English response]`.

## SentimentSnapshot & Sentiment Weighting
- **SentimentSnapshot written every tick** — `write_sentiment_snapshot()` at end of `run_tick()`. Do NOT remove — powers `GET /api/signals/sentiment_history`. MERGE on (graph_id, timestamp) — reruns overwrite, not duplicate.
- **`fear_greed_score`**: `50 + mean_asset_sentiment × 50` (0=extreme fear, 100=extreme greed). Not the same as pool trait distribution used in the gauge.
- **Leverage asymmetry: 24h uses multipliers, 7d does not** — intentional. `none=1.0, 2x=1.3, 5x=1.6, 10x_plus=2.0`. Applying to 7d would compound fictitious signal strength.
- **`_DECAY_LAMBDA = 0.05`** defined in both `signals.py` (module-level) and `investors.py` (inline). Half-life ≈ 13.9h. Must stay in sync. Applied to 24h only — do NOT apply to 7d (exp(-0.05×168) ≈ 0.0002 effectively zeros all events).
- **Legacy fallback for pre-TIER-2B events** (null direction/conviction/position_size): `weight = capital × (confidence/10) × 0.3 × leverage × decay`. Do not remove.
- **`direction` in `compute_sentiment_scores()` carries the sign** — weight is always positive. Do not negate weight or you'll double-count the sign.
- **`low_participation: true`** — signal forced to "neutral" when event_count < 200. Not an error.
- **Equal-weighted path** — `compute_sentiment_scores(rows, equal_weighted=True)`. Both paths returned by `GET /api/investors/sentiment`. Both must be maintained.
- **`_SENTIMENT_QUERY` must stay in sync** between `signals.py` and `investors.py` — if a field is added to one query, check the other.

## Signals API Gotchas
- **`_signal()` uses `None` sentinel default, NOT `_THRESHOLD_FALLBACK`** — Python evaluates default args at definition time; `_THRESHOLD_FALLBACK` is defined after `_signal()`.
- **Dynamic thresholds**: `_compute_dynamic_thresholds()` queries last 48 snapshots; fallback `_THRESHOLD_FALLBACK=0.4` if <10 snapshots. Crypto threshold of 0.6 is normal. `dynamic_threshold` always present in signal entries.
- **`data_quality` always present** — `"fresh"` or `"degraded"`. Degraded when: price staleness >36h, newest 24h event >36h old, or no events. Cold start → degraded.
- **History endpoint uses UTC-aware `day_start`** (`tzinfo=timezone.utc`) — timezone-naive comparison causes midnight misalignment.
- **History endpoint returns `null` price for missing files** — `spanGaps: false` in Chart.js renders a gap.
- **`backtester.py` imported at request time** — `signals.py` adds `backend/scripts/` to `sys.path`. Do not rename/move without updating path resolution.
- **`/api/signals/archetype_split`** — smart_money = hedge_fund + prop_trader; dumb_money = retail_amateur. `insufficient_data` when either group <20 events. Uses same conviction model + decay as `current_signals()`.
- **`/api/signals/drilldown`** — `asset` must match one of the 7 asset_class_bias values. `reasoning` truncated 250 chars. Orders by `capital × coalesce(conviction,0.5) × coalesce(position_size,0.3) DESC`.
- **`sentiment_history`** — `hours` or `days`; `hours` takes precedence. Hard cap: `_SENTIMENT_HISTORY_LIMIT = 2000`. `capped: true` if truncated. At 30-min cadence, 2000 ≈ 42 days.

## Dashboard Live State Gotchas
- **Neo4j failure at refresh time** — `live_state.json` left unchanged, WARNING logged. Never written with partial data.
- **yfinance failure** — falls back to most recent price file, `prices_stale: true` in JSON.
- **`e.count` not `e.mention_count` in `renderTopEntities`** — API returns `count` (from `coalesce(e.mention_count, 1) AS count`). Do NOT use `e.mention_count`.
- **`reaction_distribution` in live state** — percentages × 100. Do NOT double-multiply in frontend.
- **Drawdowns are sentiment drawdowns, not price** — tracks drop from rolling 48-snapshot peak. Requires ≥5 snapshots per asset.
- **Correlation matrix requires ≥10 snapshots per asset** — absent from `live_state.json` with fewer.
- **`_marketOpen` default is `true`** — prevents flash-of-grey on load. Crypto tiles never grey out (`isCrypto` check in `renderSignals()`). `is_market_open()` in `dashboard_refresh.py`: Mon–Fri 13:30–20:00 UTC.
- **Dashboard is self-contained single HTML file** — CDN only: Chart.js 4.4.1, Hammer.js 2.0.8, chartjs-plugin-zoom 2.0.1.
- **Equal-weighted toggle** shows a single-point chart from `_lastSentimentData` cache — no historical equal-weighted series. `_lastSentimentData` is null on cold load.
- **Tick chart y-axis dynamically bounded** — do not restore hardcoded `min:-1, max:1`.
- **30d view hides point dots** (`pointRadius: 0` when >200 snapshots). `_tickWindow` is JS module-level state.
- **Alert strip**: delta ≥ ±0.25 vs 4 ticks back → `.alert-chip`. Session-deduped via `_alertedKeys`. `checkAlertStrip()` hides row when all chips dismissed.
- **ARCH_COLORS in dashboard.html** — add entries for new archetypes or they get grey badges.

## Scheduler Gotchas
- **Daily cron is 02:00 UTC** (changed from 20:00). Catch-up condition: `now.hour >= 2`. Old code checking `>= 20` must be updated.
- **Ollama health check** (`_check_ollama()`): tries `http://ollama:11434` then `localhost:11434`. 1-token probe confirms model loaded (not just running). Polls every 10s up to 60s. Adds ~2s to startup. Both jobs log `model_unavailable` and return early if check fails.
- **`model_unavailable`** is a valid `status` in `scheduler_runs.json` (alongside `success`/`error`).
- **Catch-up** runs on startup AND in every job `finally`. Priority: daily first, then halfhourly. Daily: UTC hour ≥ 2 AND no daily entry for today. Halfhourly: last finished_at >1800s ago.
- **`_catchup_running`** threading.Event prevents recursive catch-up calls.
- **Weekly TTL pruning**: LIMIT 10,000 per run (90-day TTL). Large backlogs need multiple weeks to clear.
- **Log**: `backend/logs/scheduler_runs.json` (host) / `/app/backend/logs/scheduler_runs.json` (container). Schema: `{job_type, started_at, finished_at, status, error}`. Capped at 10,000. Created at runtime.
- **Summer time**: APScheduler fires at 02:00 UTC (Europe/London), catch-up condition `>= 2` UTC — up to 0 gap in summer. Acceptable.

## Simulation Tick Gotchas
- **`formative_crash` key is `dotcom_2000`** — agents with `formative_crash: 'dotcom'` (old format) silently get no prose. Check: `MATCH (n:Entity {formative_crash: 'dotcom'}) RETURN count(n)`.
- **Time-horizon gating: 30-min ticks only** — `full=True` bypasses entirely. Do NOT add gating to daily tick.
- **Stratified sampling**: 8 Neo4j queries per tick (7 archetype + 1 fill).
- **`compute_price_staleness_hours()` is in `price_fetcher.py`** — imported lazily in `signals.py`. Update sys.path if container scripts path changes.
- **Panic contagion**: `CONTAGION_FLAG_PATH = backend/briefings/contagion_flag.txt`. Written after tick if any archetype >50% panic. Consumed + deleted at START of next `run_tick()`. Only injected for agents with `herd_behaviour >= 7`. Deleted on clean tick too (prevents stale flags).
- **`tick_reactions` includes `archetype`** — required for panic rate computation; missing falls back to `'unknown'`.
- **Embedding cache**: `backend/briefing_cache/` (container only; not bind-mounted). Key: sha256[:16] of full briefing text. Atomic write via `.tmp` rename. 10-file prune. Cache failure never crashes tick.

## Portfolio State & Conviction Scoring
- **Position fields null until first reaction** — always use `coalesce(a.position_equities, 'flat')`.
- **Position updates use MATCH not MERGE** — silent no-op if UUID doesn't match.
- **panic flattens ALL positions** regardless of `assets_mentioned`.
- **`assets_mentioned` must be from `_POSITION_ASSETS`**: `["equities","crypto","bonds","commodities","fx","mixed","real_estate"]`. Unrecognised tickers silently ignored.
- **Entry price set on flat→long transition only** — preserves original entry on adding to position.
- **Pre-TIER-2B MemoryEvent fields may be null** — `compute_sentiment_scores()` legacy fallback handles this. Do not remove.
- **Time-decay λ=0.05**: 0h=1.0, 14h≈0.50, 24h≈0.30.

## Market Regime (TIER 3)
- **`compute_market_regime()` in `price_fetcher.py`** — called at end of `fetch_prices()`. Never raises; degrades gracefully. Written to `backend/live/regime.json`.
- **`trend=INSUFFICIENT_DATA` until 20+ price days** — briefings show `[MARKET REGIME: COMPUTING...]`. MA-20 needs 20, MA-50 needs 50.
- **OpenBB installed in container but NOT on host** — host scheduler always uses yfinance fallbacks. OpenBB calls wrapped in try/except.
- **`flow_signal`** from IEF/HYG 5-day momentum: `risk_on`=HYG up+IEF down, `risk_off`=IEF up+HYG down.
- **Calendars use FMP provider** (`provider="fmp"`). Falls back to empty list if FMP not configured.
- **Price snapshot embeds `regime` key** — backwards-compatible.

## Reaction Diversity (verify_agent_diversity.py Check 8)
- Requires MemoryEvents from last 7 days. No events → `passed=True`. Not a bug.
- Reaction direction for Pearson correlation: `buy=+1.0, hold=0.0, hedge=-0.5, sell=-1.0, panic=-2.0`.

## Verification Scripts
- `verify_tier2.py` — 13 checks (TIER 2B/2C/2D)
- `verify_tier3.py` — 22 checks (TIER 3)
- `verify_all.py` — combines both + 9 session changes. Check for `TOKEN_BUDGET = 4096` now fails — skip it.
- `verify_new_features.py` — 13 checks (Reddit/Stocktwits/cache/drilldown/aliases)
