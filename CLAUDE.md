# Quorum — Claude Code Instructions

## Read First
Before doing anything, read PROJECT_MEMORY.md in full. It contains the complete history of what has been built, all architecture decisions, gotchas, and current system state.

## At the End of Every Session
Append a new session block to PROJECT_MEMORY.md following the exact same format as existing entries:

Be specific. Future Claude sessions rely entirely on this document for context. Do not skip this step.

## Key Facts
- Graph ID: d3a38be8-37d9-4818-be28-5d2d0efa82c0
- Root .env uses localhost hostnames (for host Python scripts)
- backend/.env uses neo4j/ollama hostnames (for Docker containers)
- Dashboard served at localhost:5001/dashboard
- Project is called Quorum in docs, mirofish in internal code
- After any backend change: docker cp the file + docker restart mirofish-offline

## News Feed Gotchas
- **Central bank feeds are never relevance-filtered** — this is intentional. `CENTRAL_BANK_FEEDS` (Fed, ECB, BoE) bypass `filter_articles()` entirely. Every item from a central bank is financially relevant by definition. Do not add them to `RSS_FEEDS` or `NITTER_FEEDS` or they will be filtered.
- **`NITTER_ENABLED` flag** — currently `False` in `news_fetcher.py`. All public nitter.net instances are timing out on every request and producing zero articles. Re-enable only when self-hosted Nitter is running in Docker. The `NITTER_FEEDS` list is preserved in the file — only the flag needs to change.
- **Bloomberg Markets removed from `RSS_FEEDS`** — was returning 403 on all server-side requests. A comment marks the removal location. Replace with an authenticated source (Bloomberg API, RapidAPI proxy) if needed.
- **Unusual Whales removed from `RSS_FEEDS`** — no public RSS feed exists; requires a paid API. A comment marks the removal location.
- **Benzinga URL corrected** — was `benzinga.com/feeds/news` (404); now `benzinga.com/latest?page=1&feed=rss`.
- **Investopedia removed** — returns 402 Payment Required (paywalled). A comment marks the removal location.
- **TheStreet removed** — both `thestreet.com/feeds/rss/index.xml` and `thestreet.com/rss/index.xml` return 403. A comment marks the removal location.
- **AP Business added** — `apnews.com/hub/business/feed`. No paywall, reliable uptime, no timeout needed. Uses feedparser's default fetch (not in `_TIMEOUT_FEEDS`).
- **Benzinga and Seeking Alpha have a 5-second feed timeout** — `parse_feed()` accepts a `timeout` parameter. These two feeds pass `timeout=5` via the `_TIMEOUT_FEEDS` set check in `fetch()`. On timeout, a WARNING is logged (`"{source_name} timed out"`) and the feed returns `[]`. All other feeds (Reuters, Yahoo, CNBC, MarketWatch, FT, AP Business, central banks) have no timeout.
- **Relevance filter is a whitelist, not a blacklist** — if briefings are too short, expand `FINANCIAL_TERMS` in `news_relevance_filter.py`. Common missing terms: company names (Nvidia, Apple, Tesla), commodity-specific terms, emerging-market indices.

## NER / Knowledge Graph Gotchas
- **spaCy replaces LLM-based NER in `incremental_update.py`** — entity extraction uses `en_core_web_sm` loaded once at module level. The old `NERExtractor` (Ollama LLM per chunk) is no longer called. The container uses a `uv` venv at `/app/backend/.venv/` — if spaCy is missing, install via `docker exec mirofish-offline uv pip install spacy "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl" --python /app/backend/.venv/bin/python`. Do NOT use the system `pip` — it installs into the wrong Python.
- **Relation extraction is not performed** — spaCy NER does not extract entity-to-entity relations. The `RELATION` edges from the old LLM pipeline are no longer created by this script.
- **`central_bank_source` property is schema-ready but always `false`** — the briefing `.txt` format does not expose per-chunk source metadata, so no entity is currently tagged `central_bank_source=true` from this script. The property and UNWIND logic are in place for when chunk-level metadata is added.
- **Neo4j writes are batched (BATCH_SIZE=500)** — `UNWIND $entities AS e MERGE ...`. Do not replace with single-entity MERGEs; at 2160+ chunks the round-trip overhead is prohibitive.
- **`mention_count` tracks chunk-level appearances** — `extract_entities_spacy` deduplicates within each chunk, so `mention_count` counts how many distinct chunks an entity appeared in across the whole briefing run. Written to Neo4j via `BATCH_MERGE_ENTITY_QUERY`. ON MATCH has an `is_synthetic` guard — synthetic agent nodes will never have their `mention_count` overwritten.
- **`ENTITY_TYPE_CORRECTIONS` dict** — post-processing override applied in `extract_entities_spacy` after `SPACY_LABEL_MAP` lookup, before per-chunk deduplication. Corrects known misclassifications (Bloomberg, Reuters, CNBC, Nasdaq, Fed, FactSet, Refinitiv, Morningstar, S&P, Dow Jones, Wall Street, Treasury → Company; Washington, White House → Location). Extend this dict when the top_entities panel shows obviously wrong types.
- **`ENTITY_REMOVE_LIST` set** — case-sensitive set of entity names dropped entirely before Neo4j write. Covers financial metrics (ARPU, CapEx, EBITDA, EPS, GDP, CPI, PNT), role acronyms (ETF, IPO, CEO, CFO, COO, CTO), ticker symbols (SPY, QQQ, TLT, GLD), and FX/crypto codes (VIX, USD, EUR, GBP, JPY, BTC, ETH). Applied after ENTITY_TYPE_CORRECTIONS. To add new noise terms, extend this set.
- **Minimum entity name length is 3 characters** — raised from 2 in the same session to drop 2-letter noise tokens (e.g. "AI" tagged as ORG).
- **Single-token Person names are dropped** — if `etype == "Person"` and the name has no space, it is discarded. "Matt" alone is noise; "Matt Desch" passes. Check runs after type resolution, so ENTITY_TYPE_CORRECTIONS can reclassify away from Person before this gate.

## LLM / Prompt Gotchas
- **qwen2.5:14b code-switches to Chinese** on FOMO/emotional/stress language unless `"You must respond entirely in English. Do not use any other language."` is the first line of the system prompt in `build_prompt()`. Do not remove or relocate this line.
- **hedge defaults to catch-all without a naming gate** — qwen2.5:14b will use `hedge` as a generic cautious response unless the prompt explicitly requires the model to name the instrument and the exposure being hedged. The current definition in `build_prompt()` enforces this: "you must be able to state exactly what instrument you are using and what exposure you are hedging… If you cannot name the instrument and the risk, choose hold instead." Do not soften this language.
- **panic is scenario-conditional** — the current definition in `build_prompt()` lists explicit triggers (market crash, circuit breaker, trading halt, emergency rate hike, bank collapse, war escalation, systemic crisis). This is intentional: vague panic definitions cause the model to treat `hedge` as the default caution response instead. The "you do not think — you react" framing is load-bearing — do not replace with polite language.

## SentimentSnapshot Gotchas
- **SentimentSnapshot nodes are written every tick** — `write_sentiment_snapshot()` in `simulation_tick.py` is called at the end of every `run_tick()`. Do NOT remove this call or the tick-level sentiment history chart in `dashboard.html` will stop receiving data. The `SentimentSnapshot` nodes accumulate in Neo4j and power the `GET /api/signals/sentiment_history` endpoint.
- **SentimentSnapshot uses MERGE on (graph_id, timestamp)** — re-running the same tick will overwrite the existing snapshot for that timestamp, not create a duplicate. This is intentional.
- **fear_greed_score in SentimentSnapshot**: defined as `50 + mean_asset_sentiment * 50`, ranging 0–100. 50 = neutral, 0 = extreme fear, 100 = extreme greed. Not the same as the pool fear_greed_dominant distribution shown in the gauge (which is based on agent trait, not reaction).

## Sentiment Weighting Gotchas
- **Leverage weighting asymmetry: 24h uses multipliers, 7d does not** — this is intentional. `compute_sentiment_scores(rows, apply_leverage=True)` is only called for the 24h window in `get_sentiment()`. The 7d window always uses `apply_leverage=False`. Leverage is a short-horizon amplifier — applying it to a 7-day aggregate would compound fictitious signal strength. Do not "fix" this asymmetry.
- **Leverage multiplier values**: `none=1.0`, `2x=1.3`, `5x=1.6`, `10x_plus=2.0`. These are defined in `_LEVERAGE_MULTIPLIERS` in `investors.py`.
- **Equal-weighted sentiment path exists alongside capital-weighted** — `compute_sentiment_scores(rows, equal_weighted=True)` uses weight=confidence (capital ignored). Both paths are returned by `GET /api/investors/sentiment` as `by_asset_class` (capital-weighted) and `by_asset_class_equal`. Both must be maintained if `investors.py` is modified.

## News Feed Sidecar
- **Feed breakdown sidecar** — `news_fetcher.py` writes `briefings/YYYY-MM-DD_HHMM_sources.json` alongside each briefing. This is read by `investors.py/_latest_sources_sidecar()` for the `feed_breakdown` field in `/api/investors/stats`. If the sidecar is missing (first run before a new briefing), `feed_breakdown` returns `[]` — this is correct, not a bug.
- **Sidecar counts pre-filter articles** — the Counter uses `general_articles + cb_articles` (all fetched articles, before relevance filtering). This shows every feed that returned content, including feeds whose articles were all filtered out. Do NOT change it to `all_articles` (post-filter) — that would hide most feeds since the relevance filter is aggressive.

## Simulation Tick Gotchas (2026-04-14)
- **`formative_crash` key is `dotcom_2000`, not `dotcom`** — if agents in Neo4j have `formative_crash: 'dotcom'` (old format from generate_agents.py), they silently get no formative crash prose (treated as 'none'). Check: `MATCH (n:Entity {formative_crash: 'dotcom'}) RETURN count(n)`. Fix with a Cypher SET or `generate_agents.py --clear`.
- **Time-horizon gating only applies to 30-min ticks** — `full=True` bypasses gating entirely. Do not add gating to the daily tick — the point is to simulate agents catching up on a full day of news.
- **Stratified sampling uses 8 Neo4j queries per tick** (7 archetype + 1 fill) vs 1 for the old random sample. Fast on indexed fields but worth noting if tick startup time increases.
- **`compute_price_staleness_hours()` is in price_fetcher.py** — imported lazily in signals.py via sys.path insertion. If the container's scripts path changes, update `signals.py` `_price_staleness_hours()`.

## Scheduler Gotchas (2026-04-14)
- **Ollama health check adds ~2s to tick startup** — `_check_ollama()` sends a 1-token generate probe to confirm the model is loaded, not just running. If Ollama is partially loading, it polls every 10s for up to 60s before aborting. Both `hourly_job` and `daily_job` call it.
- **Ollama check tries container hostname first** (`http://ollama:11434`), then localhost. If scheduler runs on the host (not in Docker), the ollama hostname will fail but localhost will succeed.
- **Weekly TTL pruning uses LIMIT 10000 per run** — at 24,000 nodes/day, a 90-day backlog is ~2.16M nodes. The first run on a backlogged database will only delete 10,000 nodes; subsequent weekly runs chip away at the rest. This is intentional to avoid long-running transactions.
- **`model_unavailable` is a valid `status` value in scheduler_runs.json** — in addition to `success` and `error`. Added in 2026-04-14 session. Any monitoring that checks for non-success status should handle this value.

## Signal API Gotchas (2026-04-14)
- **`/api/signals/history` now returns null price (not skip) for missing price files** — every date in the requested range is included, with `price: null` when no file exists. Chart.js renders a gap via `spanGaps: false`. Previously, dates with no price file were silently omitted from the response.
- **History endpoint uses UTC-aware `day_start`** — `tzinfo=timezone.utc` explicitly set. SentimentSnapshot timestamps are stored as UTC ISO strings; timezone-naive `>=` comparison was causing misalignment especially around midnight.
- **`data_quality: "degraded"` in signal entries** — added to every signal when `price_staleness_hours > 36`. Field absent when prices are fresh. The dashboard does not yet render this visually (planned under TIER 3B).
- **`price_staleness_hours` in `/api/signals/current` response** — new top-level field in `data` object. Rounds to 2 decimal places. Absent if no price files exist.

## Signal Backtesting Gotchas
- **Backtester requires at least 2 days of price snapshots** — `backend/prices/` currently has Apr 7, 8, 9, 10, 11. Signal accuracy becomes statistically meaningful at 30+ trading days. Let the pipeline accumulate.
- **Backtest confidence calibration counts will be low early** — only directional events (buy/hedge/sell/panic) are counted, not hold. With ~500 agents per tick across ~5 days, counts per tier will be small initially.
- **`backtester.py` is imported at request time in signals.py** — the import adds `backend/scripts/` to `sys.path` dynamically. Do not rename or move `backtester.py` without updating the path resolution in `signals.py`.

## Reaction Diversity Check Gotchas
- **Check 8 (reaction_diversity) requires Neo4j MemoryEvents from the last 7 days** — if no events exist, check 8 returns `passed=True` (no data = no collapse). Not a bug.
- **Reaction direction for Pearson correlation**: `buy=+1.0`, `hold=0.0`, `hedge=-0.5`, `sell=-1.0`, `panic=-2.0`. Hedge is treated as defensive/risk-off (negative direction) because it implies the agent is offsetting rather than adding exposure.

## Scheduler Run Log Gotchas
- **Log file location**: `backend/logs/scheduler_runs.json` (host) / `/app/backend/logs/scheduler_runs.json` (container). Read with `docker exec mirofish-offline cat /app/backend/logs/scheduler_runs.json`.
- **Log schema**: each entry is `{"job_type": "halfhourly"|"daily", "started_at": "<ISO>", "finished_at": "<ISO>", "status": "success"|"error", "error": null|"string"}`. Written in the `finally` block — a crash mid-job leaves no entry (gap is the signal).
- **Capped at 10,000 entries** — oldest entries trimmed automatically. At 30-min cadence + 1 daily, this is ~200 days of history.
- **`backend/logs/` is created at runtime** — `_ensure_logs_dir()` creates it. Not in git.

## Sentiment Chart Gotchas
- **Tick chart y-axis is dynamically bounded** — `renderTickSentChart()` computes global min/max across all assets and all fetched snapshots, with 5% padding, clamped to `[-1, +1]`. Do not restore the hardcoded `min:-1, max:1` — the dynamic bounds are intentional for visual comparison.
- **`sentiment_history` accepts `hours` OR `days`** — `?hours=48` (default, unchanged) or `?days=30` (full half-hourly 30-day history). If both are supplied, `hours` takes precedence. Hard cap of 2000 results; `capped: true` in response and WARNING logged if hit. Constant: `_SENTIMENT_HISTORY_LIMIT = 2000` in `signals.py`.
- **`capped: true` means data was truncated** — at 30-min cadence, 2000 snapshots ≈ 42 days. Increase `_SENTIMENT_HISTORY_LIMIT` if the pipeline has run longer and you need the full 30-day view.
- **30d view hides point dots** — `pointRadius: 0` when snapshot count > 200. Intentional: at 1440+ points, dots create clutter. 48h view (≤200 points) keeps dots.
- **`_tickWindow` state** — JS module-level. `'48h'` (default) or `'30d'`. Controls both the fetch param and label formatting. Persists across `fetchAll()` refresh cycles.

## Dashboard Live State Gotchas
- **Two processes must run for full functionality**: `scheduler.py` (simulation pipeline) and `dashboard_refresh.py` (live data freshness). Each runs as an independent blocking process in its own terminal/shell.
  ```
  # Process 1 — simulation pipeline (30-min tick, daily full run)
  python backend/scripts/scheduler.py --graph-id d3a38be8-37d9-4818-be28-5d2d0efa82c0

  # Process 2 — live state refresher (15-min file write)
  python backend/scripts/dashboard_refresh.py --graph-id d3a38be8-37d9-4818-be28-5d2d0efa82c0
  ```
- **`dashboard_refresh.py` is NOT imported by `scheduler.py`** — they are completely separate processes. Do not add it as a job inside the scheduler.
- **`backend/live/` is the shared state directory** — `docker-compose.yml` bind-mounts `./backend/live:/app/backend/live`. This is the ONLY way host-written files reach the container. `dashboard_refresh.py` writes here; `live.py` reads here. The full `backend/` cannot be mounted because it would shadow the container's uv venv at `/app/backend/.venv/`.
- **`live_state.json` location**: `backend/live/live_state.json` (host) / `/app/backend/live/live_state.json` (container). Written atomically (tmp file + rename). **Not** `backend/live_state.json` — that old path is outside the volume mount and invisible to Flask.
- **`price_sentiment_history.json` location**: `backend/live/price_sentiment_history.json` (host) / `/app/backend/live/price_sentiment_history.json` (container). Rolling 2880-entry array of `{ts, p, s}` snapshots written every 15 min by `dashboard_refresh.py`. Served by `GET /api/live/history`.
- **`GET /api/live/state`** — reads `live_state.json` directly, no Neo4j query, always fast. Returns 503 if the file doesn't exist (dashboard_refresh not running). No `graph_id` check — it trusts the file.
- **`GET /api/live/history`** — reads `price_sentiment_history.json` directly. Returns 503 if no history yet. Each entry: `{"ts": "<ISO>", "p": {asset: price}, "s": {asset: sentiment_score}}`. Powers the Sentiment vs Price chart at 15-min resolution.
- **Neo4j failure handling**: if the connection test fails at refresh time, the existing `live_state.json` is left unchanged and a WARNING is logged. The file is never written with partial/empty Neo4j data.
- **yfinance failure handling**: if yfinance fails, prices are read from the most recent price file and `prices_stale: true` is set in the JSON. The dashboard shows a yellow "Prices delayed" badge in the signals section.
- **`prices_stale` badge**: shown next to the "Trading Signals" section label. Driven by `prices_stale: true` in live state. Hidden automatically once live prices are available.
- **`live-state-ts` element**: shows "live HH:MM:SS" in the dashboard header using `refreshed_at` from the response. This is the time the file was last written, not the time the dashboard fetched it.
- **`last-tick-ts` element**: shows "tick Xm ago" in the dashboard header using `last_tick_at` from the live state. Populated by `fmtTimeAgo()` in JS. Hidden if `last_tick_at` is absent. Critical remote monitoring signal — shows immediately if the simulation is stuck.
- **Dashboard layout (current)**: sidebar+main flex split — sidebar (260px sticky) holds pool stats, fear/greed gauge, entity list, feed chart; main holds tick sentiment chart, reaction distribution, history chart, distributions, event feed. Signal strip (`#sig-strip`) is a horizontal row of 7 `.stile` tiles above the split — each tile shows asset, signal badge, price, 24h change, score, 2px indicator bar. Horizontal-scrolls on mobile.
- **Header market dots (`#mkt-dots`)**: 7 `.mkt-dot` coloured circles updated by `updateMktDots(signals)` after every `renderSignals()` call. `bull` class = green, `bear` = red, `neut` = amber. 8px circles with box-shadow glow.
- **Alert strip (`#alert-zone`)**: `detectAlerts(snapshots)` fires after every `fetchTickSentimentHistory()` call. Compares latest snapshot against 4 ticks back (~2h). Any asset delta ≥ ±0.25 → dismissable `.alert-chip`. Session-deduped via `_alertedKeys` Set. `checkAlertStrip()` hides the strip row when all chips dismissed.
- **`e.count` not `e.mention_count` in `renderTopEntities`** — the `/api/signals/current` endpoint returns `count` (from `coalesce(e.mention_count, 1) AS count`). The dashboard uses `e.count`. Do NOT change to `e.mention_count` — that field is never returned by the API.
- **Dashboard is self-contained single HTML file** — all CSS and JS inline. CDN imports only: Google Fonts, Chart.js 4.4.1, Hammer.js 2.0.8, chartjs-plugin-zoom 2.0.1. No external files.
- **Dashboard has two refresh layers**: (1) `fetchAll()` every 30s hits the full Flask API (sentiment, signals, stats); (2) `fetchLiveState()` every 15 min reads from the file. They are additive — the 15-min layer provides fresh data even between scheduler ticks.
- **Sentiment scores in live state** use capital-weighted + leverage (24h window), identical to `/api/investors/sentiment`. Equal-weighted is also stored under `sentiment.by_asset_class_equal`.
- **`reaction_distribution` in live state** is a single snapshot (percentages × 100 from the latest SentimentSnapshot). The tick-level reaction distribution chart fetches full history via `fetchTickSentimentHistory()` — `fetchLiveState()` triggers that re-fetch.
- **docker cp for dashboard_refresh**: since it runs on the host (not inside the container), no docker cp needed for the script itself. However if you're running it inside the container, cp as usual: `docker cp backend/scripts/dashboard_refresh.py mirofish-offline:/app/backend/scripts/`.
- **NEVER use `docker-compose up --force-recreate`** — this rebuilds the container from the original image, wiping every file ever added via `docker cp`. All modified scripts (`simulation_tick.py`, `incremental_update.py`, `news_fetcher.py`, `signals.py`, `investors.py`, `live.py`, `__init__.py`, `dashboard.html`, etc.) must be recopied immediately after any recreate. Always use `docker restart mirofish-offline` for normal restarts. Only recreate when changing `docker-compose.yml` (e.g. adding a volume mount), and always follow with a full `docker cp` pass.
- **`dashboard_refresh.py` loads the ROOT `.env`, not `backend/.env`** — it runs on the host so it must use `localhost` hostnames. The script loads `MiroFish-Offline/.env` (two directories above `backend/scripts/`) via `dotenv.load_dotenv` at import time. `backend/.env` uses Docker service names (`neo4j`, `ollama`) and is only for the container runtime — loading it on the host causes DNS failures like "Failed to resolve neo4j:7687". The default fallback in `_get_neo4j_driver()` is also `bolt://localhost:7687`, not `bolt://neo4j:7687`.

## Scheduler Catch-up Gotchas
- **Catch-up runs on startup AND after every job completes** — `run_catchup()` is called from `main()` before `scheduler.start()`, and from within the `finally` block of both `hourly_job` and `daily_job`.
- **Daily catch-up condition**: current UTC hour ≥ 3 AND no `daily` log entry with today's UTC date. If the machine was offline at 03:00 UTC, catch-up fires the daily job immediately on next startup.
- **Half-hourly catch-up threshold**: 30 minutes (1800 seconds) since last `halfhourly` log entry's `finished_at`. No entry = treat as overdue.
- **Priority: daily before halfhourly** — `run_catchup()` always checks and runs daily first (step 1), then checks halfhourly (step 2). If daily was overdue and ran, the halfhourly check still fires if it too is overdue.
- **Re-entrance guard** — `_catchup_running` threading.Event prevents recursive calls. When a job launched from catch-up finishes and tries to trigger another catch-up, the guard returns immediately. This is intentional: the outer catch-up already handles both jobs sequentially.
- **`next_run_time` removed from half-hourly APScheduler job** — catch-up handles the startup run. If last halfhourly was < 30 min ago at startup, nothing fires immediately; next tick waits for the normal 30-min interval.
- **Summer time mismatch**: daily catch-up uses UTC hour ≥ 3; the APScheduler daily job uses Europe/London time. In summer (UTC+1), the APScheduler fires at 02:00 UTC but catch-up won't trigger until 03:00 UTC — up to 1 hour gap. Acceptable.

## Portfolio State Gotchas (2026-04-14)
- **Agent position fields are written lazily** — most agents will have null position_{asset} fields until they react. Always use `coalesce(a.position_equities, 'flat')` in Cypher when reading. Never assume the field exists.
- **Position updates use MATCH not MERGE** — `update_agent_positions()` uses `MATCH (a:Entity {uuid, graph_id}) SET ...`. This never creates new agent nodes. If the agent UUID doesn't match (wrong graph_id, etc.), the SET is silently skipped.
- **panic reaction flattens ALL positions** — regardless of `assets_mentioned`, panic triggers a full liquidation SET across all 7 assets. This is intentional — panic = exit everything.
- **`assets_mentioned` normalised to asset class keys** — the LLM returns values from `["equities","crypto","bonds","commodities","fx","mixed","real_estate"]`. The position update function filters against `_POSITION_ASSETS` — any unrecognised ticker (e.g. "SPY") is silently ignored. Make sure the JSON schema in `build_prompt()` lists the exact asset class names, not tickers.
- **Entry price only set on new long** — `update_agent_positions()` only sets `entry_price_{asset}` when `existing_pos == 'flat'`. If an agent is already long and buys again ("adding to position"), the original entry price is preserved. This models averaging-up behaviour accurately — do not change.
- **Position fetch adds one Neo4j query per active agent per tick** — at 30-min tick with ~100 active agents (after gating), this is ~100 additional MATCH queries per tick. Fast on indexed uuid, but monitor if tick startup latency increases significantly.

## Conviction Scoring Gotchas (2026-04-14)
- **MemoryEvent `direction`/`conviction`/`position_size` may be null on older nodes** — pre-TIER-2B events never had these fields written. Use `coalesce(m.direction, null) IS NOT NULL` to detect new events in Cypher. `compute_sentiment_scores()` in `investors.py` handles this with a legacy fallback automatically.
- **Legacy fallback formula**: `score = reaction_score`, `weight = capital × (confidence/10) × 0.3 × leverage × decay`. The `0.3` is a neutral `position_size` default that keeps old events from dominating the 24h window relative to new conviction-weighted events.
- **Sentiment scoring asymmetry preserved**: 24h window uses conviction model + leverage + decay (`apply_leverage=True, apply_decay=True`). 7d window uses standard capital×confidence (`apply_leverage=False, apply_decay=False`). Do not apply decay to 7d — at 7d age, exp(-0.05×168) ≈ 0.0002 which would effectively zero out all but the last few hours.
- **`direction` in `compute_sentiment_scores()` carries the sign** — weight is always `|capital × conviction × position_size × leverage × decay|` (positive). The score passed to `weighted_sum += score * weight` is the direction float (-1, 0, +1). Do not negate weight or you'll double-count the sign.
- **`low_participation: true` in signals response** — added to any asset in `/api/signals/current` where event_count < 200. The signal is forced to "neutral". This is not an error — it's a data quality gate. Dashboard consumers should surface this visually (pending TIER 3B).
- **Time-decay λ=0.05 rationale**: half-life = ln(2)/0.05 ≈ 13.9 hours. Within a 24h window, events from 24h ago get weight ≈ 0.30 relative to current events. Events from 14h ago get weight ≈ 0.50. This gives recency bias without eliminating older events entirely.

## Drawdown Gotchas (2026-04-14)
- **Drawdown is a sentiment drawdown, not a price drawdown** — `drawdowns` in `live_state.json` tracks the drop from peak `SentimentSnapshot` score, not from peak price. A drawdown of −60% means sentiment is 60% below its 48-snapshot peak, not that price fell 60%.
- **Requires ≥5 snapshots per asset to appear** — assets with fewer than 5 non-null snapshots in the last 48 are omitted from the `drawdowns` dict. Not an error.
- **Peak is the maximum in the 48-snapshot window** — it's a rolling peak (48 × 30min = 24h), not a historical all-time high. The window resets every time the peak snapshot ages out.

## signals.py TIER 2C Gotchas (2026-04-14 session 3)
- **`_SENTIMENT_QUERY` in `signals.py` now returns TIER 2B fields** — `m.timestamp`, `m.direction`, `m.conviction`, `m.position_size`, `a.leverage_typical`. This is required so `compute_sentiment_scores(apply_leverage=True, apply_decay=True)` can use the conviction model and time-decay. If you add a new field to `investors.py`'s query, check whether `signals.py`'s `_SENTIMENT_QUERY` also needs it.
- **`_DECAY_LAMBDA = 0.05` constant in `signals.py`** — module-level, with half-life comment. This is the same λ used in `investors.py` (hardcoded inline). They must stay in sync if the decay rate changes.
- **`current_signals()` uses `compute_sentiment_scores(apply_leverage=True, apply_decay=True)`** — matches the 24h path in `investors.py`'s `get_sentiment()`. Intentional: same scoring formula, same asymmetry (7d does not use leverage or decay).
- **`data_quality` is always present in signal entries** — values are `"fresh"` or `"degraded"`. Degraded if: price staleness > 36h OR newest MemoryEvent in the 24h window is > 36h old OR no events at all. Previously `data_quality` was only added when degraded (price-staleness only).
- **Event-based staleness check** — `current_signals()` scans all row timestamps to find the newest event. If no events exist (empty pool, cold start), `event_degraded=True` → `data_quality="degraded"`. On parse failure, `event_degraded=False` (fail safe, assume fresh).
- **`verify_tier2.py`** — standalone verification script at `backend/scripts/verify_tier2.py`. Runs 13 checks covering TIER 2B/2C/2D. Does not modify production files. Run with: `python backend/scripts/verify_tier2.py`.
## TIER 2C-ii / 2C-iii / 2D-i / TIER 3 Gotchas (2026-04-14 session 4)

### Dynamic Signal Thresholds (TIER 2C-ii)
- **`_signal()` uses `None` sentinel default, NOT `_THRESHOLD_FALLBACK`** — the default `threshold` parameter is `None`, and the fallback is looked up inside the function body. Do NOT change to a module-constant default — Python evaluates default args at definition time, and `_THRESHOLD_FALLBACK` is defined after `_signal()`.
- **`_compute_dynamic_thresholds()` queries last 48 SentimentSnapshots** — falls back to `_THRESHOLD_FALLBACK=0.4` per asset if <10 snapshots exist. Returns `{asset: threshold}` dict. Called once per `/api/signals/current` request.
- **`dynamic_threshold` field always present in signal entries** — even when using the fallback. Dashboard consumers can use this to show how wide each asset's threshold band is.
- **Crypto will have a higher threshold than bonds** — correct behavior. When crypto std is high, the threshold widens to avoid false signals. A threshold of 0.6 for crypto is normal.

### Archetype Split (TIER 2C-iii)
- **`/api/signals/archetype_split` uses same conviction model as `/api/signals/current`** — `apply_leverage=True, apply_decay=True`. Do not use the 7d formula here.
- **`insufficient_data` when either group has <20 events** — not an error. Smart money archetypes (hedge_fund + prop_trader) typically have far fewer agents than retail. At pipeline start, expect most assets to show `insufficient_data`.
- **Divergence = smart_money_score − dumb_money_score** — positive means smart money more bullish than dumb money. `|divergence| > 0.3` → smart_leads. `|divergence| ≤ 0.15` → converging. Gap (0.15–0.3) defaults to converging.

### Panic Contagion (TIER 2D-i)
- **`CONTAGION_FLAG_PATH = backend/briefings/contagion_flag.txt`** — written by `_write_contagion_flag()` after tick; consumed and deleted at the START of the next `run_tick()` call. Transient — never accumulates.
- **Only injects into agents with `herd_behaviour >= 7`** — the flag text is stored in `contagion_context` inside `run_tick()`; per-agent check `herd_val >= 7.0` determines whether to pass it to `build_prompt()`.
- **`tick_reactions` now includes `archetype` field** — required for panic rate computation in `_write_contagion_flag()`. If archetype is missing, falls back to `'unknown'`.
- **Flag is deleted even if panic threshold NOT exceeded** — `_write_contagion_flag()` always removes a stale flag when no archetype crosses >50% panic. This prevents stale flags from persisting across restarts.

### Market Regime (TIER 3)
- **`compute_market_regime()` is in `price_fetcher.py`** — called at the end of `fetch_prices()`. Never raises — all failures logged at WARNING and degrade gracefully. Returns the regime dict.
- **`trend=INSUFFICIENT_DATA` until 20 days of price data** — MA-20 needs 20 prices, MA-50 needs 50. With only ~7 days of history, trend is always INSUFFICIENT_DATA. This is correct.
- **`regime.json` is written to `backend/live/`** — same bind-mounted directory as `live_state.json`. Flask reads it via `/api/live/regime`. Host scripts write it.
- **OpenBB is optional** — if `openbb` module is not importable, yield curve and VIX fall back to TLT/SPY correlation and SPY volatility proxy respectively. All OpenBB calls wrapped in try/except.
- **OpenBB installed in container but NOT on host** — `docker exec mirofish-offline uv pip install openbb --python /app/backend/.venv/bin/python`. On the host, price_fetcher.py uses yfinance fallback. This is correct: the host runs the scheduler/price_fetcher outside Docker; OpenBB VIX/yield can be added to host venv later.
- **Briefing regime header shows `COMPUTING...` when any dimension is `INSUFFICIENT_DATA`** — by design. Until 20+ days of price data, trend is INSUFFICIENT_DATA → all briefings show COMPUTING. This is the correct fallback per spec.
- **Price snapshot now embeds `regime` key** — `backend/prices/YYYY-MM-DD.json` now contains a `"regime"` sub-dict alongside `"prices"`. Backwards-compatible; old readers that only look at `"prices"` are unaffected.
- **`verify_tier3.py`** — 22-check verification at `backend/scripts/verify_tier3.py`. Run with: `python backend/scripts/verify_tier3.py`.
