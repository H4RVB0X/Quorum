# MiroFish-Offline — Project Memory

---

## Session: 2026-04-11 — Agent Diversity Audit Script + Project Knowledge Export

### What was built
Created `backend/scripts/verify_agent_diversity.py` — a standalone 7-check diversity audit that runs against the live Neo4j agent pool and produces a report file plus a one-line CI-friendly terminal summary.

Also produced a full project knowledge document (for pasting into a Claude project) covering the entire codebase: architecture, all 3 features, every script's role, code patterns, schema, and non-obvious design decisions.

### Architecture decisions
- **Full cosine similarity matrix via numpy** — `(n × d) @ (n × d).T` after L2-normalisation. For 4096 agents × 768 dims this is ~64 MB in float32, manageable locally. Alternative (FAISS range search) was considered but rejected for simplicity since this is a periodic audit, not a hot path.
- **10-bucket histogram for numeric traits** — covers the full value range and catches hard clustering (e.g. everyone at risk=5.0) without being confused by naturally-skewed fat-tail distributions. Categorical traits use exact Counter values.
- **Binary trait (`fear_greed_dominant`) will always flag Distribution check** — both values (fear ~45%, greed ~55%) exceed the 25% threshold. This is expected and documented. Not a bug.
- **News reaction check uses 100 random agents × 3 headlines = 300 LLM calls** — deliberate: enough statistical signal without running the whole pool. Temperature 0.7, max_tokens 64 for speed.
- **`serialise_traits()` duplicated from agent_evolver.py** — kept local rather than shared import to avoid cross-script coupling. Both must stay in sync if trait fields change.
- **Reports directory** — `backend/scripts/reports/` created at runtime by the script via `mkdir(parents=True, exist_ok=True)`. Not committed to git.
- **Exit code 0/1** — passes through to CI cleanly. Any flagged issue (any check fails) = exit 1.

### Blockers / incomplete items
- No unit tests written for `verify_agent_diversity.py`. The script is audit tooling rather than pipeline code, so this was left for later.
- Check 7 (news reaction) is slow (~5–12 min total) and depends on Ollama being up. If Ollama is down, all LLM calls fail and that check auto-fails with a "all calls failed" error. No fallback.
- The uniqueness check embeds all agents in a single `embed_batch()` call. If the pool ever exceeds ~10k agents, the in-memory similarity matrix becomes large — would need batched FAISS range search at that scale.

### Gotchas for next session
- **`fear_greed_dominant` Distribution flag is always expected** — binary trait; both values will exceed 25%. Not an actionable warning.
- **Check 1 (uniqueness) is the slow step** — embedding 4096 agents via Ollama takes 2–4 min. If iterating on report formatting, comment out `check1 = check_uniqueness(...)` and replace with a stub to speed up the loop.
- **Run from `backend/scripts/`** — the `sys.path.insert` makes imports work relative to `backend/`. Running from the repo root will fail imports.
- **`serialise_traits()` must match `agent_evolver.py`** — if investor trait fields are added or renamed, update the function in both files.
- **`fear_greed_dominant` is a string `'fear'`/`'greed'`, not a bool** — any code reading this field must treat it as a string category.

### New files / folders
| Path | Purpose |
|------|---------|
| `backend/scripts/verify_agent_diversity.py` | Standalone 7-check diversity audit script |
| `backend/scripts/reports/` | Runtime output directory for audit report `.txt` files (created on first run, not committed) |

### Setup needed
Run from `backend/scripts/`:
```bash
python verify_agent_diversity.py [--graph-id <uuid>]
```
Neo4j and Ollama must both be running. `--graph-id` is optional if projects exist in `uploads/projects/`. Report written to `backend/scripts/reports/diversity_audit_YYYYMMDD_HHMM.txt`. Exit code 0 = pass, 1 = fail.

---

## Session: 2026-04-11 — Agent Memory Context in Simulation Tick

### What was built
Modified `backend/scripts/simulation_tick.py` to give each agent awareness of its own past reactions. Before each LLM call, the agent's last 5 `MemoryEvent` nodes are fetched from Neo4j and injected into the **system prompt** alongside a structured persona block. Added a `--no-memory` CLI flag to skip the memory fetch on first runs.

### Architecture decisions
- **Memory in system prompt, not user message** — persona + memory block are part of the system turn so the LLM treats them as fixed identity context rather than conversational input. Briefing/news stays in the user turn.
- **Last-5 by timestamp DESC** (not a rolling 7-day window) — gives the most recent reactions regardless of inactivity gaps. The old `load_agent_memory()` (7-day window) is retained untouched; new function `load_agent_recent_memory()` handles the new behaviour.
- **`no_memory=False` default on `run_tick()`** — scheduler.py required zero changes; both hourly and daily calls inherit the default.
- **Graceful skip when no history** — `format_memory_block()` returns `""` for empty lists; the system prompt simply omits the memory section rather than printing "No recent memory."
- **Persona block format** — structured as `You are {name}, a {archetype} investor. Capital/risk/strategy/fear-greed/herd/sensitivity` — matches the user spec exactly, pulled from the agent dict already loaded by `load_agents()`.

### Blockers / incomplete items
- Neo4j `MemoryEvent` nodes must already exist for memory injection to have any effect. The very first tick will always produce empty memory blocks unless `--no-memory` is used.
- The `briefing_source` field is fetched in `load_agent_recent_memory()` but currently not rendered in the memory block display (only reaction/confidence/reasoning/timestamp are shown). Could be added later if source attribution is useful.
- No unit tests cover `format_memory_block()` or `build_prompt()` yet.

### Gotchas for next session
- **Deploy step is manual** — changes are local only until you run:
  ```
  docker cp backend/scripts/simulation_tick.py mirofish-offline:/app/backend/scripts/simulation_tick.py
  docker restart mirofish-offline
  ```
- **First run needs `--no-memory`** — if no `MemoryEvent` nodes exist yet, the Neo4j query is fast but pointless. Use `--no-memory` for the bootstrap run, then drop it for all subsequent ticks.
- **`args.no_memory`** — argparse converts `--no-memory` to `args.no_memory` (underscore), not `args.no-memory`. This is already correct in the code but worth noting if adding more hyphenated flags.
- The old `load_agent_memory()` function is still in the file and no longer called by `run_tick()`. It can be removed in a cleanup pass if nothing else references it.
- `scheduler.py` calls `run_tick()` by keyword — safe, no changes needed there.

### New files / folders
None. Only `backend/scripts/simulation_tick.py` was modified.

### Setup needed
1. Copy updated file into container and restart (see deploy step above).
2. First bootstrap tick: `python simulation_tick.py --briefing <path> --graph-id d3a38be8-37d9-4818-be28-5d2d0efa82c0 --no-memory`
3. All subsequent ticks (via scheduler or manual): omit `--no-memory`.

---

## Session: 2026-04-11 — Sentiment Aggregator API + Dashboard Chart

### What was built
- **`GET /api/investors/sentiment`** — new Flask endpoint in `backend/app/api/investors.py`. Queries Neo4j for `MemoryEvent` nodes over the last 24 h and last 7 days, weights each event by `agent.capital_usd × event.confidence`, groups by `agent.asset_class_bias`, and returns a sentiment score in `[-1, +1]` per asset class for both windows.
- **Two pure helper functions** added to `investors.py`: `_reaction_score()` (reaction string → float) and `compute_sentiment_scores()` (list of rows → scored dict). Both are independently testable with no DB dependency.
- **18 pytest tests** in `backend/tests/test_sentiment.py` — covers all reaction mappings, edge cases (zero-weight rows, score clamping, unknown reactions), multi-asset grouping, capital-dominance weighting, and Flask endpoint behaviour (400 on missing graph_id, correct 24h/7d split, event counts, `generated_at` field).
- **Sentiment trend chart** added to `frontend/public/dashboard.html` — full-width grouped bar chart (Chart.js) above the existing Asset Bias / Fear-Greed row. Green = 24h, blue = 7d. Y-axis −1 to +1 with highlighted zero-line. Fetched in parallel with every `fetchAll()` refresh (every 30 s).

### Architecture decisions
- **Reaction score mapping**: `buy→+1`, `hedge→+0.5`, `hold→0`, `sell→-1`, `panic→-1`. Hedge is treated as defensive-positive (half weight) to distinguish from outright selling.
- **Weight = capital × confidence** — larger agents with higher conviction dominate. Agents with zero capital or zero confidence contribute nothing (prevents division-by-zero, rows effectively skipped).
- **Two separate Cypher queries** (one per time window) rather than a single query with grouping — simpler to reason about, avoids complex Cypher aggregation, and both queries run against the same session.
- **`since` parameter uses ISO-8601 string** comparison in Cypher (`m.timestamp >= $since`). Assumes `MemoryEvent.timestamp` is stored as an ISO string (which `simulation_tick.py` already does via `datetime.utcnow().isoformat()`).
- **Score rounded to 4 decimal places** in the response — enough precision for charting without floating-point noise.
- **Pure functions kept module-level** (not inside the route) so tests can import them directly without standing up a Flask app.
- **`fetchSentiment()` is fire-and-forget** — called inside `fetchAll()` but not awaited in the same chain. Failures silently swallowed to avoid blocking the main stats refresh.

### Blockers / incomplete items
- No historical trend data (e.g. hourly snapshots over 24 h) — the chart shows a single 24h and 7d aggregate, not a time-series line. Building a true trend line would require either storing periodic snapshots or adding a `bucket` param to the query.
- No `asset_class` filter param on the endpoint — currently returns all asset classes in one payload.

### Gotchas for next session
- **`MemoryEvent.timestamp` must be an ISO-8601 string** — the Cypher `>=` comparison relies on lexicographic ordering, which only works for ISO format (e.g. `2026-04-11T13:00:00`). If timestamps are stored as Neo4j `DateTime` objects instead of strings, the query will need `datetime($since)` casting.
- **`asset_class_bias` comes from the agent node, not the event** — if an agent has `null` for `asset_class_bias`, those events appear under the key `'unknown'` in the response (via `row.get('asset_class') or 'unknown'` in `compute_sentiment_scores`).
- **`_driver_cache` is module-level** — in tests, `_get_driver` must be patched at `app.api.investors._get_driver`, not at `neo4j.GraphDatabase.driver`, otherwise the cached driver from a previous call leaks in.
- **Chart Y-axis grid line highlighting** — uses `grid.color` as a callback (`ctx => ctx.tick.value === 0 ? ... : ...`). This is Chart.js 4.x API; it will break on Chart.js 3.x.

### New files / folders
| Path | Purpose |
|------|---------|
| `backend/tests/test_sentiment.py` | 18 TDD tests for sentiment scoring logic and Flask endpoint |

### Modified files
| Path | Change |
|------|--------|
| `backend/app/api/investors.py` | Added `_reaction_score()`, `compute_sentiment_scores()`, and `GET /api/investors/sentiment` route |
| `frontend/public/dashboard.html` | Added full-width sentiment grouped bar chart (Chart.js), `fetchSentiment()` JS function, legend, and CSS |

### Setup needed
No new dependencies. Endpoint is live as soon as the Flask server restarts. Neo4j must have `MemoryEvent` nodes with `timestamp`, `reaction`, and `confidence` fields, and the linked `Entity` nodes must have `capital_usd` and `asset_class_bias`.

---

## Session: 2026-04-11 — Trading Signal System (Steps 1–3)


### What was built
- **`backend/scripts/price_fetcher.py`** — standalone yfinance price fetcher. Fetches daily closing prices for 7 asset-class proxy tickers and writes `backend/prices/YYYY-MM-DD.json`.
- **`backend/scripts/scheduler.py`** — updated hourly job to call `fetch_prices()` as Step 0 (non-blocking, failures are warnings not errors).
- **`backend/app/api/signals.py`** — new Flask blueprint at `/api/signals` with two endpoints:
  - `GET /api/signals/current?graph_id=...` — combines 24 h sentiment scores with the latest price snapshot; returns signal + price + 24 h price change per asset class.
  - `GET /api/signals/history?graph_id=...&days=30&asset=...` — returns daily sentiment + price rows for charting.
- **`backend/app/__init__.py`** — `signals_bp` registered at `/api/signals`.
- **`frontend/public/dashboard.html`** — two new sections inserted above the Bias/Fear-Greed row:
  - **Trading Signals panel** — colour-coded cards (green=bullish, red=bearish, grey=neutral) showing score, price, 24 h Δ%.
  - **Sentiment vs Price Change chart** — dual-axis Chart.js line chart (sentiment left axis, price Δ% right axis), per-asset tabs, 30-day window.

### Architecture decisions
- **Signal thresholds**: score > +0.4 → bullish, score < −0.4 → bearish, else neutral. Chosen to avoid noise from low-conviction days; easily adjustable in `_signal()`.
- **Ticker map** (set by user after initial build): `equities→SPY`, `crypto→BTC-USD`, `bonds→TLT`, `commodities→GLD`, `fx→DX-Y.NYB`, `real_estate→VNQ`, `mixed→VT`.
- **Price files are date-stamped JSON** (`YYYY-MM-DD.json`) in `backend/prices/`. Latest file is found by sorting glob results descending — no DB, no state needed.
- **24 h price change** computed by reading yesterday's price file at request time, not stored anywhere. Returns `null` if yesterday's file doesn't exist yet.
- **History endpoint skips days with no price file** (except today) to avoid flooding Neo4j with empty-result queries.
- **`compute_sentiment_scores` and `_get_driver` are imported from `investors.py`** — no duplication of sentiment logic.
- **History chart uses relative price change** (day-over-day %) not absolute price, so the two y-axes are meaningfully comparable at similar scales.
- **`yfinance` uses `period='2d'`** — ensures at least one complete trading day is always returned even when today's market hasn't closed yet.

### Blockers / incomplete items
- `yfinance` not yet installed in the Docker container — must be done via `docker exec mirofish-offline pip install yfinance` before the price fetcher will work.
- History sentiment query hits Neo4j once per day per call — for 30 days that's 30 queries. Fine for now but could be batched or cached if latency becomes an issue.
- No price data exists yet in `backend/prices/` — the signal panel and history chart will show placeholder state until the first `fetch_prices()` run succeeds.
- `real_estate` and `mixed` tickers (VNQ, VT) were added to the ticker map post-build but are not covered by any price data yet.

### Gotchas for next session
- **`_PRICES_DIR` path resolution in `signals.py`** — uses two candidate paths (`_PRICES_DIR` and `_PRICES_DIR_ALT`) because the app package can be run from different working directories inside the container. If prices are not found, check which path resolves correctly with a print statement.
- **`yfinance` multi-level column handling** — some versions of yfinance return a `DataFrame` with multi-level columns for `Close`. `signals.py` and `price_fetcher.py` both handle this with `if hasattr(close, 'columns'): close = close.iloc[:, 0]`.
- **DX-Y.NYB (USD Index)** — sometimes returns stale/missing data from yfinance if Yahoo Finance's feed lags. The fetcher stores `null` for any failed ticker rather than failing the whole run.
- **History chart asset tabs** — the selected asset is stored in `_historyAsset` (JS module-level). On page refresh it resets to `'equities'`. Tab state is not persisted.
- **Signal panel shows all 7 asset classes** even those with no price data (e.g. `real_estate`, `mixed` before first fetch). Those cards will show `price: —` and `24h: —` which is correct.
- **Docker deploy is manual** — nothing auto-deploys. Run the cp+restart block below after every backend change.

### New files / folders
| Path | Purpose |
|------|---------|
| `backend/scripts/price_fetcher.py` | Fetches daily closing prices via yfinance; writes to `backend/prices/` |
| `backend/prices/` | Directory for daily price JSON files (`YYYY-MM-DD.json`) |
| `backend/app/api/signals.py` | Flask blueprint — `/api/signals/current` and `/api/signals/history` endpoints |

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/scheduler.py` | Added `fetch_prices()` import + Step 0 call in `hourly_job()` |
| `backend/app/__init__.py` | Registered `signals_bp` at `/api/signals` |
| `frontend/public/dashboard.html` | Added signal panel CSS, two new HTML sections, `historyChart` init, `fetchSignals()`, `renderSignalPanel()`, `updateHistoryChart()`, asset tabs |

### Setup needed
```bash
# Install yfinance in container
docker exec mirofish-offline pip install yfinance

# Copy all changed files
docker cp backend/scripts/price_fetcher.py mirofish-offline:/app/backend/scripts/
docker cp backend/scripts/scheduler.py mirofish-offline:/app/backend/scripts/
docker cp backend/app/api/signals.py mirofish-offline:/app/backend/app/api/
docker cp backend/app/__init__.py mirofish-offline:/app/backend/app/
docker cp frontend/public/dashboard.html mirofish-offline:/app/frontend/public/

# Restart
docker restart mirofish-offline

# Seed first price file (run inside container or locally with yfinance installed)
python backend/scripts/price_fetcher.py
```

---

## Session: 2026-04-11 — README/ROADMAP Rewrite + Project Rename to Quorum

### What was built
- **`README.md`** — complete rewrite from scratch. The old README described the original MiroFish social opinion simulator and the Zep→Neo4j migration; the new one documents the actual current system: the 8,192 agent financial simulation, the autonomous 30-minute pipeline, the sentiment/signal API, and the live dashboard. Covers architecture diagram, full stack table, agent pool composition, all 16 traits, each pipeline step with implementation details, all API endpoints, dashboard section breakdown, full project structure tree, step-by-step setup from scratch, configuration reference (both `.env` files), hardware tiers, and a dedicated implementation notes section for non-obvious gotchas.
- **`ROADMAP.md`** — complete rewrite. Old roadmap tracked Zep migration tasks and generic OASIS improvements. New roadmap tracks actionable near/mid/long-term items specific to the financial simulation: signal backtesting, news relevance filtering, Twitter/Nitter sources, archetype-conditional prompts, reaction diversity audit, agent trait evolution, Docker volume improvements, and backtesting analytics.
- **`docs/progress.md`** — added a note at the top flagging the file as historical migration notes, redirecting to the README.

### Architecture decisions
- **Project has been renamed "Quorum"** — the user edited `README.md` after delivery to rename the project from "MiroFish-Offline" to "Quorum" and rewrote the opening section. This is now the canonical name. All internal code still uses `mirofish` in module names, logger names, and Docker container names — these are not yet updated.
- **Two-`.env`-file pattern explicitly documented** — this is the most common source of confusion for new contributors. Both files are now explained side-by-side in the README with a table showing which hostnames each file should contain and why.
- **`is_synthetic` guard documented in depth** — the Cypher `CASE WHEN` guard that prevents news NER from overwriting agent nodes was undocumented before. Now explained in both the pipeline step description and the dedicated implementation notes section.
- **FAISS absence explicitly called out** — chunk retrieval uses in-memory NumPy cosine similarity rebuilt from scratch each tick, not a persisted FAISS index. This was undocumented and surprised previous contributors. Now documented.

### Blockers / incomplete items
- The README still references `nikmcfly/MiroFish-Offline` GitHub URLs in badges and credits — these will need updating if the repo is renamed to match the "Quorum" rebrand.
- Internal code (logger names like `mirofish.scheduler`, Docker container name `mirofish-offline`, module paths) still uses `mirofish` everywhere. The rename is README-only so far.
- No CLAUDE.md was created for this session — project instructions live in PROJECT_MEMORY.md only.

### Gotchas for next session
- **The project is now called "Quorum"** — the user renamed it in the README. Use "Quorum" in all new user-facing documentation and comments. Internal code (`mirofish.*` loggers, `mirofish-offline` container name) has not been renamed — don't assume the rename is complete in the codebase.
- **README.md was edited by the user after delivery** — they rewrote the opening section under the Quorum name. The technical content (architecture, setup steps, agent system, API docs) was preserved intact. Always read the current file before editing again.
- **`docs/progress.md` is historical** — it documents the Zep→Neo4j migration phases. Don't treat it as current architectural documentation.
- **ROADMAP.md** is now written for the financial simulation. Highest-priority items: signal backtesting, news relevance filtering, archetype-conditional tick prompts.

### New files / folders
None. Documentation changes only.

### Modified files
| Path | Change |
|------|--------|
| `README.md` | Complete rewrite — documents Quorum's financial simulation, agent system, pipeline, API, and setup |
| `ROADMAP.md` | Complete rewrite — financial simulation roadmap replacing old Zep migration / OASIS items |
| `docs/progress.md` | Added "historical" note at top redirecting to README |

### Setup needed
None — documentation only. No code changes, no dependencies, no container restarts required.

---

## Session: 2026-04-11 — Scheduler Priority Fix (Daily Job Takes Priority)

### What was built
Single targeted fix to `backend/scripts/scheduler.py`. The `hourly_job()` function now bails out entirely at the top if `_daily_running` is set, before executing any work (price fetch, news fetch, incremental update, or tick).

### Architecture decisions
- **Early-exit guard, not mid-job guard** — the previous code only blocked step 3 (the simulation tick) while the daily job ran. The news fetch and `process_briefing` (incremental update) were still executing, mutating the Neo4j graph while the daily simulation was reading/writing it. The fix moves the check to the very first line of `hourly_job()` so the entire job is skipped.
- **Step-3 re-check retained** — a secondary `_daily_running.is_set()` check before the tick is kept as a safety net for the race condition where the daily job starts *between* the news fetch completing and the tick firing (i.e. within the same hourly run).
- **`_daily_running` is a `threading.Event`** — APScheduler runs jobs on threads from a thread pool, so the Event is the correct primitive here. No changes needed to the event setup itself.

### Blockers / incomplete items
- The hourly job's news fetch and briefing are skipped for the entire duration of the daily simulation. If the daily simulation takes >90 min, up to 3 hourly briefings will be missed. This is acceptable for now given 8,192 agents and a ~03:00 local run time with low news volume.

### Gotchas for next session
- **Restart required for this change to take effect** — `scheduler.py` is a long-running process; the file change is not picked up until it is restarted (Ctrl+C, re-run). No Docker changes needed — the scheduler runs as a plain Python process in a PowerShell tab.
- **The daily job does NOT have `misfire_grace_time=None`** — only the hourly job does. If the machine is asleep at 03:00, the daily job fires at next wakeup regardless (APScheduler default). This is fine.

### New files / folders
None.

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/scheduler.py` | Added early-exit `_daily_running` check at top of `hourly_job()`; updated docstring to reflect new behaviour |

### Setup needed
Restart the running scheduler process (Ctrl+C in the PowerShell tab, then re-run). No Docker changes, no dependency changes.

---
---

## Session: 2026-04-11 — Realistic Agent Regeneration

### What was built
Replaced the LLM-driven `agent_evolver.py` approach with a new `backend/scripts/generate_agents.py` — a pure Python generator that creates 8,192 realistic agents in seconds using archetype-conditional trait distributions calibrated against real market research (Preqin 2025, Broadridge US Investor Study 2024, SIFMA Equity Market Structure Compendium, MEMX Retail Trading Insights).

### Architecture decisions
- **Archetype-conditional sampling** — each of the 7 archetypes has its own sampling function (`_sample_retail_amateur()`, `_sample_hedge_fund()` etc.) drawing from distributions that reflect real market behaviour. No shared distribution across archetypes.
- **Capital ranges grounded in real AUM data**: retail_amateur $200–$50k, prop_trader $50k–$5M, fund_manager $10M–$5B, family_office $5M–$500M, hedge_fund $50M–$50B, pension_fund $1B–$100B.
- **Pool composition**: retail_amateur 50%, retail_experienced 20%, prop_trader 8%, fund_manager 8%, hedge_fund 6%, family_office 5%, pension_fund 3% — matches real market participant data.
- **MERGE key is `uuid`** not `name_lower` — prevents name collision overwriting. Earlier versions used name_lower and silently dropped ~2000 agents due to duplicate names.
- **Fixed RNG seed (42)** for reproducibility. Name pools expanded to ~240 first names × ~200 last names = ~48k combinations.
- **Bulk insert via UNWIND** — batches of 500 agents per Neo4j transaction. Full 8,192 inserts in under 2 minutes.
- **`--clear` flag** deletes all existing synthetic agents and their MemoryEvents before generating new ones.

### Gotchas for next session
- **`target` in `investors.py` is hardcoded to 4141** — update this if regenerating with a different target. Currently returns `"target": 4141` in the stats API even though the pool is 8,192.
- **Old `agent_evolver.py` is still in the repo** — it works but is now superseded. Use `generate_agents.py` for all future pool regeneration.
- **Memory events are deleted with `--clear`** — if regenerating agents, all historical reaction data is lost. Only use `--clear` intentionally.
- **Pension fund leverage distribution**: `['none', '2x', '5x', '10x_plus'], [95, 5, 0, 0]` — the 0-weight entries are fine (random.choices handles zero weights by never selecting them) but could be cleaned up.

### New files / folders
| Path | Purpose |
|------|---------|
| `backend/scripts/generate_agents.py` | Realistic archetype-conditional agent generator — replaces agent_evolver for bulk inserts |

---

## Session: 2026-04-11 — Dashboard Rebuild + Investors API Fixes

### What was built
- Full rebuild of `frontend/public/dashboard.html` — removed non-functional System Control panel, replaced pool progress bar with simple agent count stat, moved trading signals to top (most actionable first), added archetype filter tabs on agent table, agent table loads 200 agents with client-side filtering, sticky header, error state on pulse dot, mobile-responsive at 600px/900px breakpoints.
- `backend/app/api/investors.py` — fixed `_get_driver()` to use `os.environ.get()` directly with `bolt://neo4j:7687` default rather than `storage._driver` (which used the cached localhost URI from the old .env). Added `GET /api/investors/sentiment` endpoint. `target` field in stats response — currently hardcoded to 4141, should be updated to match pool size.
- `backend/app/api/control.py` — added control blueprint at `/api/control` with `/status`, `/fetch-news`, `/run-update`, `/run-tick` endpoints. Removed from dashboard as it was unreliable (host Python processes vs Flask container scope).
- `backend/app/__init__.py` — registered `investors_bp`, `control_bp`, `signals_bp`, added `/dashboard` route serving `frontend/public/dashboard.html`.

### Critical config fix documented
**Two .env files, two purposes:**
- `MiroFish-Offline/.env` (root) → used by host Python scripts → must use `localhost` hostnames
- `MiroFish-Offline/backend/.env` → baked into Docker image → must use `neo4j` and `ollama` service hostnames

This was the root cause of multiple "connection refused" errors throughout the build. `config.py` loads `backend/.env` with `override=True` which silently overrides environment variables set by `docker-compose.yml`.

### Gotchas for next session
- **`target: 4141` in stats API** — update `investors.py` line 188 if pool size changes.
- **Dashboard served from Flask at `/dashboard`** — the file lives at `frontend/public/dashboard.html` on the host but is served by Flask via `send_file()`. Changes require `docker cp` to the container.
- **`_driver_cache` in `investors.py`** — module-level cache. If the container restarts with the wrong .env, the old driver is cached. Force-fix with `sed` in container or rebuild image.
- **ngrok free tier = 1 tunnel only** — can tunnel either port 3000 (React) or 5001 (Flask+dashboard), not both simultaneously.

---

## Current System State (as of 2026-04-11 10:00)

### What is running
- Docker: `mirofish-neo4j`, `mirofish-ollama`, `mirofish-offline` (Flask + React frontend)
- PowerShell Tab 1: `python scripts/scheduler.py --graph-id d3a38be8-37d9-4818-be28-5d2d0efa82c0`
- PowerShell Tab 2: `python scripts/incremental_update.py` — processing latest briefing

### Pool state
- 8,192 agents in Neo4j (archetype-conditional, realistic distributions)
- 3,632 memory events from first tick
- Reaction distribution issue: 68% hedge, 25% buy, 7% hold, 0.2% sell, 0% panic — overly hedge-heavy, flagged for fix via archetype-conditional tick prompts

### Planned next additions (priority order)
1. **Archetype-conditional tick prompts** — make hedge funds react aggressively, retail amateurs panic, prop traders take decisive positions
2. **News relevance filtering** — drop non-financial articles before NER processing
3. **Signal accuracy backtesting** — compare historical signals vs real price moves
4. **Twitter/Nitter RSS sources** — self-hosted Nitter in Docker, curated financial accounts
5. **More global news sources** — FT, Bloomberg, ECB, BoE press releases
6. **Agent reaction diversity audit** — run verify_agent_diversity.py check 7 after 1 week

### Graph ID
`d3a38be8-37d9-4818-be28-5d2d0efa82c0`

### Key file locations
| What | Where |
|------|-------|
| Root .env (localhost) | `MiroFish-Offline/.env` |
| Backend .env (Docker hostnames) | `MiroFish-Offline/backend/.env` |
| Dashboard HTML | `frontend/public/dashboard.html` (+ container `/app/frontend/public/`) |
| Price files | `backend/prices/YYYY-MM-DD.json` |
| Briefings | `backend/briefings/YYYY-MM-DD_HHMM.txt` |
| Agent generator | `backend/scripts/generate_agents.py` |
| Seen URLs cache | `backend/scripts/seen_urls.json` |

---

## Session: 2026-04-11 (11:11) — Archetype-Conditional Tick Prompts

### What was built
Modified `backend/scripts/simulation_tick.py` to fix the 68% hedge reaction skew.

Two changes:
1. **`ARCHETYPE_BEHAVIORS` dict** (module-level, after `VALID_REACTIONS`) — 7 entries mapping each archetype to a behavioral persona + reaction guidance string. Injected into the system prompt after the persona block, before memory.
2. **Reaction definitions in user message** — replaced the bare `buy|hold|sell|panic|hedge` label with explicit per-reaction definitions. Critically, `hedge` is redefined as "a deliberate trade with a specific thesis, NOT a response to uncertainty." Added forced-choice closing line: "You must pick the single most likely action given your personality and the news. If nothing in the news is relevant to you, your answer is hold."

### Architecture decisions
- **Prompt-side only** — no post-processing fallback, no reaction remapping, no temperature changes. The redefinition of hedge is the primary fix; archetype behaviors are reinforcement.
- **Order in system prompt**: instruction → persona → behavior → memory. Memory stays last (most proximate to the user turn).
- **`ARCHETYPE_BEHAVIORS.get(archetype, "")`** — agents with unknown/missing archetype get no behavior block. Silent fallback, not an error.
- **`pension_fund` nudge** — "you react very slowly to news — only major systemic events (rate policy shifts, sovereign defaults) justify action" rather than "you do not react to daily news", so extreme macro events still produce valid non-hold reactions.
- **`prop_trader` panic** — "Panic is never appropriate — prop traders cut losses fast with a sell, not an emotional spiral." Consistent phrasing with other professional archetypes.

### Blockers / incomplete items
- No A/B data yet — need to run a tick with the new prompts and compare reaction distribution against the 68% hedge baseline.
- The `verify_agent_diversity.py` check 7 (news reaction diversity) is the right tool to measure improvement after a full run.

### Gotchas for next session
- **Deploy is manual** — changes are local only until:
  ```
  docker cp backend/scripts/simulation_tick.py mirofish-offline:/app/backend/scripts/simulation_tick.py
  docker restart mirofish-offline
  ```
- **Baseline for comparison**: pre-change distribution was 68% hedge / 25% buy / 7% hold / 0.2% sell / 0% panic. Target post-change: hedge sub-20%, more buy/sell/panic variation especially from prop_trader, hedge_fund, retail_amateur.
- **`ARCHETYPE_BEHAVIORS` and `generate_agents.py` archetype keys must stay in sync** — if a new archetype is added to the generator, add a matching entry to `ARCHETYPE_BEHAVIORS` or that archetype gets no behavioral guidance.

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/simulation_tick.py` | Added `ARCHETYPE_BEHAVIORS` dict; modified `build_prompt()` to inject behavior block; replaced bare reaction label with full definitions + forced-choice line |

### Setup needed
```bash
docker cp backend/scripts/simulation_tick.py mirofish-offline:/app/backend/scripts/simulation_tick.py
docker restart mirofish-offline
```

---

## Session: 2026-04-11 — Prompt Hardening: Language Enforcement, Hedge/Panic Definitions, Archetype Audit

### What was built
Four targeted changes to `backend/scripts/simulation_tick.py` to fix a persistent hedge skew (51.2% hedge) and mojibake in reasoning text caused by qwen2.5:14b code-switching to Chinese.

### Changes made
1. **English enforcement in system prompt** — Added `"You must respond entirely in English. Do not use any other language."` as the first element of `system_parts` in `build_prompt()`. Fixes mojibake in `reasoning` fields caused by qwen2.5:14b switching to Chinese on emotionally-loaded or FOMO-adjacent prompts.

2. **Tightened hedge definition** — Replaced the previous hedge definition with: "you are making a deliberate, specific trade to offset a named risk — you must be able to state exactly what instrument you are using and what exposure you are hedging. This is NOT a response to uncertainty or partial relevance. If you cannot name the instrument and the risk, choose hold instead." Adds the instrument+risk naming requirement as a hard gate.

3. **Concrete panic triggers** — Replaced the previous panic definition with an explicit archetype restriction: only `retail_amateur` and `retail_experienced` can panic, and only during extreme stress events. Professional archetypes (`prop_trader`, `fund_manager`, `hedge_fund`, `family_office`, `pension_fund`) are explicitly told they never panic — they sell instead.

4. **ARCHETYPE_BEHAVIORS audit** — Reviewed all 7 entries and removed/rewrote any phrasing that could imply hedging as a default cautious response. Key changes per archetype:
   - `retail_amateur`: Added "if you are unsure, you hold, not hedge" to the Hold description.
   - `retail_experienced`: Added "If uncertain, hold —" prefix; added naming requirement for hedge.
   - `prop_trader`: Added "If you cannot state the exact instrument and the specific exposure you are offsetting, the answer is hold."
   - `fund_manager`: Removed "measured, process-driven" (implies hedge as caution); replaced with "systematic"; added "When news is ambiguous, you hold" and "hedge requires a named instrument, not a reaction to ambiguous news."
   - `family_office`: Removed "preservation" framing (implies defensive hedging); replaced with "growing and preserving... through compounding"; added "uncertainty is never a reason to act — when in doubt, you hold"; added "not a defensive reflex" to hedge description.
   - `hedge_fund`: Added "and a named instrument" to hedge requirement.
   - `pension_fund`: Added "If uncertain, hold." to hedge guidance.

### Pre-change reaction distribution (baseline)
- hedge 51.2% / buy 31.3% / hold 15.9% / sell 1.6% / panic 0%

### Known issue fixed
- Mojibake in `reasoning` field: qwen2.5:14b code-switches to Chinese on emotionally-loaded language (FOMO, fear, stress). Fixed by enforcing English at the top of the system prompt.

### Target distribution
- hedge < 20%, panic > 0% for retail archetypes under stress news, more variation across buy/sell/hold

### Gotchas for next session
- **Panic is now prompt-restricted to retail archetypes** — professional archetypes are told to sell instead. This means panic events in MemoryEvent nodes should only appear for `retail_amateur` and `retail_experienced` going forward.
- **Hedge now requires instrument + risk naming in reasoning** — if the model hedges without naming an instrument in its reasoning, that is a prompt failure worth monitoring.
- **qwen2.5:14b will code-switch without English enforcement** — the "respond entirely in English" line must remain at the very top of system_parts. Do not move it below the persona block.

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/simulation_tick.py` | English enforcement in system prompt; tightened hedge definition; concrete panic archetype restrictions; ARCHETYPE_BEHAVIORS audit removing hedge-as-caution language |

### Setup needed
```bash
docker cp backend/scripts/simulation_tick.py mirofish-offline:/app/backend/scripts/simulation_tick.py
docker restart mirofish-offline
```
(Already deployed in this session.)

---

## Session: 2026-04-11 — News Relevance Filter + Feed Expansion (Nitter, FT, Bloomberg, Central Banks)

### What was built

**`backend/scripts/news_relevance_filter.py`** (new file)
Standalone keyword-whitelist filter. Exposes `filter_articles(articles: list[dict]) -> list[dict]`. An article passes if its title or body contains any term from `FINANCIAL_TERMS` (~80 terms covering indices, macro, rates, FX, crypto, corporate events, instruments, sectors) or matches a ticker-symbol regex (`$AAPL`, `#SPX`). Logs pass/fail counts at INFO per call.

**`backend/scripts/news_fetcher.py`** (significantly updated)
Four additions to the news pipeline:

1. **Relevance filter** — after all general RSS + Nitter articles are fetched, `filter_articles()` is applied before writing the briefing. Non-financial articles are dropped. `seen_urls` is updated for ALL fetched articles (including filtered-out ones) so non-financial articles are not re-fetched on the next run.

2. **Nitter RSS** — 10 curated financial Twitter accounts added via `NITTER_FEEDS`. `NITTER_ENABLED = True` flag at top of file toggles all Nitter feeds without code changes. Nitter articles go through the relevance filter (same pass as RSS). Each feed failure is caught silently per-feed.

3. **FT and Bloomberg** — added to `RSS_FEEDS`: FT Markets (`ft.com/rss/home/uk`) and Bloomberg Markets (`feeds.bloomberg.com/markets/news.rss`). These go through the relevance filter.

4. **Central bank feeds** — `CENTRAL_BANK_FEEDS` list: Federal Reserve (`federalreserve.gov/feeds/press_all.xml`), ECB (`ecb.europa.eu/rss/press.html`), BoE (`bankofengland.co.uk/rss/news`). These are fetched AFTER the filter step and bypass it entirely. Each article is tagged `source_type: "central_bank"` in the dict. The briefing labels them `[CENTRAL BANK] {source}` so downstream processing can weight them appropriately.

### Architecture decisions
- **Two-pass structure in `fetch()`**: general articles are collected first and filtered as a batch; central bank articles are collected after and concatenated without filtering. This keeps the logic simple and ensures CB articles can never be accidentally dropped.
- **`filter_articles()` is a pure module** — no Flask dependency, standalone. Can be imported from any script without bringing up the app context.
- **Substring matching, not word boundaries** — simple and fast. Accepted false positive: common substrings like "golden" matching "gold". For a financial news pipeline, false positives are preferable to false negatives.
- **All fetched articles marked seen** — filtered-out non-financial articles are added to `seen_urls` so they don't get re-fetched and re-rejected every 30 minutes.
- **`NITTER_ENABLED` flag** — nitter.net is a third-party public instance that can go down. Toggling `NITTER_ENABLED = False` disables all 10 Nitter feeds in one change without touching the list.
- **Central bank feeds are low-volume** — Fed/ECB/BoE typically publish 0-5 items per day. No rate limiting needed.

### Gotchas for next session
- **Nitter instances may go down** — if nitter.net is unavailable, all 10 Nitter feeds will silently return empty (logged as warnings). Set `NITTER_ENABLED = False` if this is causing log noise.
- **FT and Bloomberg may return 403** — FT especially restricts RSS access by IP. The feed failure will be caught and logged as a warning; the rest of the fetch continues unaffected.
- **Over-filtering warning** — if briefings become very short (< 10 articles on a busy news day), expand `FINANCIAL_TERMS` in `news_relevance_filter.py`. Common false negatives would be: geopolitical articles, central bank speeches (but those come from CB feeds which bypass the filter), and sector news using company names not in the whitelist (e.g. "Nvidia" doesn't appear in FINANCIAL_TERMS — add it).
- **Under-filtering warning** — substring matching means short terms like "gold" match "golden", "fund" matches "fundamental". This is acceptable in a financial news context. If clearly non-financial articles are appearing in briefings, review recent filtered-out article titles in the logs.
- **`source_type: "central_bank"` key** — only present on CB articles. Downstream code checking this field should use `.get('source_type') == 'central_bank'` not `['source_type']`.
- **Deploy is manual** — both files must be copied + container restarted.

### New files
| Path | Purpose |
|------|---------|
| `backend/scripts/news_relevance_filter.py` | Keyword-whitelist financial relevance filter |

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/news_fetcher.py` | Filter integration; Nitter feeds + NITTER_ENABLED flag; FT + Bloomberg in RSS_FEEDS; CENTRAL_BANK_FEEDS; source_type tagging; seen_urls covers filtered-out articles |

### Setup needed
```bash
docker cp backend/scripts/news_fetcher.py mirofish-offline:/app/backend/scripts/news_fetcher.py
docker cp backend/scripts/news_relevance_filter.py mirofish-offline:/app/backend/scripts/news_relevance_filter.py
docker restart mirofish-offline
```
(Already deployed in this session.)
---

## Session: 2026-04-11 — spaCy NER Replaces LLM-Based Entity Extraction

### What was built
Replaced the LLM-based NER pipeline in `backend/scripts/incremental_update.py` with spaCy `en_core_web_sm`. The previous pipeline called Ollama once per text chunk; at 2160 chunks this blocked the hourly pipeline for 6–8 hours. The new pipeline runs entirely in-process with no LLM calls and completes in under 2 minutes.

### Architecture decisions
- **spaCy model loaded at module level** — `nlp = spacy.load("en_core_web_sm")` runs once on import. Avoids cold-start overhead on every call.
- **Label mapping**: `ORG→Company`, `PERSON→Person`, `GPE→Location`, `FAC→Location`, `MONEY→FinancialFigure`, `PERCENT→FinancialFigure`, `PRODUCT→Product`, `EVENT→Event`, `NORP→Group`. Labels not in the map are silently ignored.
- **Entity filters**: name must be ≥2 characters and not purely numeric (`"2024"` is dropped; `"$82B"` is kept).
- **Two-phase deduplication**: within-chunk dedup by (name.lower(), type) in `extract_entities_spacy()`; then global dedup across all chunks via a dict keyed on (name.lower(), type) in `process_briefing()`.
- **Batched Neo4j writes via UNWIND** (`BATCH_SIZE=500`) — all entities collected first, then written in a single session with batched UNWIND queries. The old code opened a driver session per chunk and did one MERGE per entity.
- **`is_synthetic` guard preserved** — ON MATCH SET uses `CASE WHEN n.is_synthetic THEN ... ELSE ... END` so agent nodes are never overwritten by briefing NER.
- **`central_bank_source` property** — schema and UNWIND logic are in place; OR-logic means once set to `true` it is never cleared. However, the current briefing `.txt` format does not expose per-chunk source metadata, so all entities arrive with `central_bank_source=false`. Tag becomes live when chunk-level source_type metadata is added.
- **Relation extraction dropped** — spaCy NER does not extract entity-to-entity relations. The `RELATION` edges from the old LLM pipeline are no longer created by `incremental_update.py`. The `NERExtractor` class and `EmbeddingService` are no longer imported by this script.
- **Logging is per-batch** — three INFO lines total: entities extracted count, entities merged count, elapsed time. No per-chunk logs.

### Blockers / incomplete items
- `central_bank_source` tagging requires chunk-level source metadata that doesn't exist in the current briefing format. The briefing is a flat `.txt` concatenation; article SOURCE lines are in the text but not exposed to the splitter.
- Relation extraction is gone. If entity→entity relations are needed in the graph, a separate RE step (dependency parsing, coref, or a lightweight model) would need to be added.

### Gotchas for next session
- **spaCy must be installed in the container** before deploying — if missing, `incremental_update.py` fails on import:
  ```bash
  docker exec mirofish-offline pip install spacy
  docker exec mirofish-offline python -m spacy download en_core_web_sm
  ```
- **`requirements.txt` updated** — added `spacy>=3.7.0` and the `en_core_web_sm-3.7.1` wheel URL. Docker image rebuild will install these automatically.
- **`NERExtractor` is not imported** — `ner_extractor.py` still exists in `backend/app/storage/` but is no longer called from `incremental_update.py`. Do not re-add the import.
- **Ontology query removed** — the old code fetched the graph ontology from Neo4j and passed it to the LLM for guidance. spaCy doesn't use ontologies; that query is gone. The ontology node in Neo4j is unaffected.
- **No embeddings generated** — entity embeddings (`EmbeddingService.embed_batch`) are no longer created in this script. Entities land in Neo4j with no `embedding` property. If vector similarity search on entities is needed, a separate embedding pass would be required.

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/incremental_update.py` | Full rewrite — spaCy NER, batched UNWIND writes, dropped LLM/embedding calls |
| `backend/requirements.txt` | Added `spacy>=3.7.0` and `en_core_web_sm-3.7.1` wheel |
| `CLAUDE.md` | Added NER/Knowledge Graph Gotchas section |

---

## Session: 2026-04-11 — Nitter + Bloomberg Feed Timeout Guards

### What was built
Added per-feed timeouts to `backend/scripts/news_fetcher.py` to prevent Nitter and Bloomberg from hanging the news fetch indefinitely.

### Changes made
1. **`parse_feed()` gained a `timeout` parameter** — when set, the feed URL is pre-fetched via `requests.get(url, timeout=N)` before handing content to feedparser. A `requests.exceptions.Timeout` logs `WARNING: <source_name> timed out` and returns `[]`. Other request errors also return `[]` (logged as WARNING with the exception). When `timeout=None` (the default), feedparser fetches the URL directly, preserving existing behaviour for all other feeds.

2. **Nitter feeds: `timeout=5`** — all 10 feeds in the `NITTER_FEEDS` loop now pass `timeout=5`. Public Nitter instances can hang indefinitely; 5 seconds is enough to confirm a live instance.

3. **Bloomberg Markets: `timeout=5`** — Bloomberg was producing 0 articles consistently with no log output, consistent with a silent hang. The loop over `RSS_FEEDS` now checks `if source_name == "Bloomberg Markets"` and passes `timeout=5`.

### Architecture decisions
- **Pre-fetch via requests, then pass content to feedparser** — feedparser's internal URL fetch has no timeout API. The cleanest workaround is: fetch with requests (timeout enforced), pass `resp.text` to `feedparser.parse()`. feedparser accepts raw XML/HTML string input directly.
- **`timeout=None` default** — all other feeds (Reuters, Yahoo Finance, CNBC, MarketWatch, FT, central banks) keep their existing no-timeout behaviour. Only Bloomberg and Nitter are explicitly guarded.
- **WARNING log on timeout** — the log message is `"{source_name} timed out"` (no URL) to match the brevity of the existing `Feed '{source_name}' failed: {e}` pattern.

### Gotchas for next session
- **Bloomberg timeout may suppress all Bloomberg articles** — if Bloomberg's feed is consistently slow (>5s) rather than hanging, reducing to 5s will always produce 0 articles. If Bloomberg starts consistently timing out, bump its timeout to 10–15s or add it to a separate `SLOW_FEEDS` list.
- **feedparser receives `resp.text`, not the URL** — when timeout is set, feedparser never sees the original URL. This means feedparser's `bozo` detection for malformed feeds may trigger differently than when fetching directly. In practice this is not an issue, but worth noting.
- **Central bank feeds have no timeout guard** — they are low-volume, high-priority, and on reliable government infrastructure. No change made.

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/news_fetcher.py` | `parse_feed()` gained `timeout` param; Nitter loop passes `timeout=5`; Bloomberg passes `timeout=5` |

### Setup needed
```bash
docker cp backend/scripts/news_fetcher.py mirofish-offline:/app/backend/scripts/news_fetcher.py
docker restart mirofish-offline
```
(Already deployed in this session.)

---

### Setup needed
```bash
# Install spaCy in running container
docker exec mirofish-offline pip install spacy
docker exec mirofish-offline python -m spacy download en_core_web_sm

# Deploy updated script
docker cp backend/scripts/incremental_update.py mirofish-offline:/app/backend/scripts/incremental_update.py
docker restart mirofish-offline
```

---

## Session: 2026-04-11 — Nitter Disabled + Bloomberg Removed + Three Replacement RSS Feeds

### What was built
Three targeted changes to `backend/scripts/news_fetcher.py` to fix zero-article fetches from dead sources and replace them with working alternatives.

### Changes made
1. **`NITTER_ENABLED = False`** — all public nitter.net instances are timing out on every request and producing zero articles. Disabled at the flag level so no individual feed changes are needed.

2. **Bloomberg Markets removed from `RSS_FEEDS`** — was returning 403 on all server-side requests. Will never produce articles without browser-like headers or authentication. Left a comment: `# Bloomberg removed — 403 on server requests, replace with authenticated source if needed`.

3. **Three replacement feeds added to `RSS_FEEDS`** (all with 5-second timeout via `_TIMEOUT_FEEDS` set):
   - **Unusual Whales** (`unusualwhales.com/rss/news`) — options flow and retail sentiment
   - **Benzinga** (`benzinga.com/feeds/news`) — real-time financial news
   - **Seeking Alpha** (`seekingalpha.com/market_currents.xml`) — market commentary and analysis

4. **Timeout logic updated** — `Bloomberg Markets` condition replaced with `_TIMEOUT_FEEDS = {"Unusual Whales", "Benzinga", "Seeking Alpha"}`. All three new feeds use `timeout=5` to guard against slow responses.

### Architecture decisions
- **5-second timeout on all three new feeds** — third-party aggregator feeds are less reliable than primary wires (Reuters, AP). The `_TIMEOUT_FEEDS` set pattern is cleaner than individual name checks as the list grows.
- **Same fetch pattern as existing feeds** — parse_feed → WARNING log on timeout → continue. No special error handling.
- **NITTER_FEEDS list preserved** — the list stays in the file for when self-hosted Nitter in Docker is ready. Only the flag changes.

### Gotchas for next session
- **Nitter re-enable path**: set `NITTER_ENABLED = True` in `news_fetcher.py` once self-hosted Nitter is running in Docker. The feed list is already configured; no other changes needed.
- **Seeking Alpha may throttle** — `seekingalpha.com` is known to block automated fetches with 429s on some IPs. If it produces 0 articles consistently, it may need a User-Agent rotation or removal.
- **Unusual Whales RSS is unofficial** — the feed URL is community-documented. If it 404s in a future session, check whether the feed has moved or been removed.
- **Bloomberg replacement** — the comment in RSS_FEEDS notes to replace with an authenticated source. Options: Bloomberg API (paid), RapidAPI Bloomberg proxy, or a different business wire (AP Business, PR Newswire).

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/news_fetcher.py` | `NITTER_ENABLED = False`; Bloomberg removed; Unusual Whales + Benzinga + Seeking Alpha added; `_TIMEOUT_FEEDS` set replaces Bloomberg-specific timeout check; module docstring updated |

### Setup needed
```bash
docker cp backend/scripts/news_fetcher.py mirofish-offline:/app/backend/scripts/news_fetcher.py
docker restart mirofish-offline
```
(Already deployed in this session.)

---

## Session: 2026-04-11 — Feed Corrections: Benzinga URL Fix + Unusual Whales Removed + Investopedia/TheStreet Added

### What was built
Three targeted corrections to `backend/scripts/news_fetcher.py` to fix two 404-returning feeds.

### Changes made
1. **Benzinga URL corrected** — old URL `benzinga.com/feeds/news` was returning 404. Replaced with `benzinga.com/latest?page=1&feed=rss`. Label and 5-second timeout unchanged.

2. **Unusual Whales removed** — `unusualwhales.com/rss/news` returns 404 because Unusual Whales does not publish a public RSS feed; access requires their paid API. Removed the entry and left a comment: `# Unusual Whales removed — no public RSS feed, requires paid API`.

3. **Two free replacement feeds added** (both with 5-second timeout via `_TIMEOUT_FEEDS`):
   - **Investopedia** (`investopedia.com/feedbuilder/feed/getfeed/?feedName=rss_headline`) — financial news and market coverage
   - **TheStreet** (`thestreet.com/feeds/rss/index.xml`) — market commentary and stock analysis

4. **`_TIMEOUT_FEEDS` updated** — removed `"Unusual Whales"`, added `"Investopedia"` and `"TheStreet"`. Set is now `{"Benzinga", "Seeking Alpha", "Investopedia", "TheStreet"}`.

5. **Module docstring updated** — reflects current feed list.

### Gotchas for next session
- **Investopedia leans educational** — many articles are glossary/explainer style. The relevance filter should catch financially-relevant ones; pure definitions may pass through if they contain enough financial terms. Monitor briefing quality.
- **Seeking Alpha may throttle (429)** — flagged in previous session. Still in the feed list; remove if it consistently produces 0 articles.
- **Benzinga new URL is the paginated latest feed** — `?page=1` returns the most recent items. If Benzinga restructures their URL scheme this may break again.
- **Bloomberg replacement still open** — comment remains in RSS_FEEDS.

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/news_fetcher.py` | Benzinga URL corrected; Unusual Whales removed with comment; Investopedia + TheStreet added; `_TIMEOUT_FEEDS` updated; module docstring and fetch() comment updated |

### Setup needed
```bash
docker cp backend/scripts/news_fetcher.py mirofish-offline:/app/backend/scripts/news_fetcher.py
docker restart mirofish-offline
```
(Already deployed in this session.)

---

## Session: 2026-04-11 — Feed Removals: Investopedia (402) + TheStreet (403)

### What was built
Two feed removals from `backend/scripts/news_fetcher.py` to clear out sources that cannot serve content to server-side requests.

### Changes made
1. **Investopedia removed** — returns 402 Payment Required on all server-side requests. The RSS feed is paywalled. Comment added: `# Investopedia removed — 402 Payment Required, paywalled`.

2. **TheStreet removed** — `thestreet.com/feeds/rss/index.xml` (the alternative URL) was tested via `curl` and returns 403, same as the original `thestreet.com/rss/index.xml`. No working public RSS URL found. Comment added: `# TheStreet removed — could not find working RSS URL`.

3. **`_TIMEOUT_FEEDS` reduced** — now only `{"Benzinga", "Seeking Alpha"}`. Investopedia and TheStreet removed from the set.

4. **Module docstring updated** — reflects current feed list (Reuters, Yahoo Finance, CNBC Markets, MarketWatch, FT Markets, Benzinga, Seeking Alpha).

### Current active general RSS feeds
Reuters, Yahoo Finance, CNBC Markets, MarketWatch, FT Markets, Benzinga, Seeking Alpha.

### Gotchas for next session
- **Seeking Alpha may throttle (429)** — flagged in two prior sessions. Still in the feed list; remove if it consistently produces 0 articles.
- **Benzinga URL is paginated** — `benzinga.com/latest?page=1&feed=rss`. If it breaks again, check whether Benzinga has moved their RSS endpoint.
- **Feed expansion needed** — with Bloomberg, Unusual Whales, Investopedia, and TheStreet all dead, the general RSS tier is now down to 7 sources. Consider AP Business (`apnews.com/hub/business/feed`), PR Newswire, or Reuters Business as alternatives.

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/news_fetcher.py` | Investopedia removed with comment; TheStreet removed with comment; `_TIMEOUT_FEEDS` updated; module docstring updated; fetch() comment updated |

### Setup needed
```bash
docker cp backend/scripts/news_fetcher.py mirofish-offline:/app/backend/scripts/news_fetcher.py
docker restart mirofish-offline
```
(Already deployed in this session.)

---

## Session: 2026-04-11 — Feed Addition: AP Business

### What was built
Added AP Business RSS to `RSS_FEEDS` in `backend/scripts/news_fetcher.py`.

### Changes made
1. **AP Business added** — `apnews.com/hub/business/feed`. No paywall, reliable AP infrastructure, no timeout guard needed. Appended after the Investopedia/TheStreet removal comments.
2. **Module docstring updated** — AP Business added to the General RSS list.

### Architecture decisions
- **No timeout** — AP is primary wire infrastructure, not a third-party aggregator. Same treatment as Reuters, Yahoo Finance, CNBC, MarketWatch, FT.
- **Not added to `_TIMEOUT_FEEDS`** — only Benzinga and Seeking Alpha carry the 5-second guard.

### Current active general RSS feeds (8 total)
Reuters, Yahoo Finance, CNBC Markets, MarketWatch, FT Markets, Benzinga, Seeking Alpha, AP Business.

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/news_fetcher.py` | AP Business entry added to `RSS_FEEDS`; module docstring updated |

### Setup needed
```bash
docker cp backend/scripts/news_fetcher.py mirofish-offline:/app/backend/scripts/news_fetcher.py
docker restart mirofish-offline
```
(Already deployed in this session.)
