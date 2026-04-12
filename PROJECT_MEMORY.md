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

---

## Session: 2026-04-11 — Signal Backtesting, SentimentSnapshot, Leverage Weighting, Dashboard Overhaul

### What was built

**`backend/scripts/backtester.py`** (new file)
Standalone backtesting module. One bulk Neo4j query fetches all MemoryEvents from the last N days; events are grouped by date in Python. For each day with price data, computes: per-asset signal accuracy, per-archetype accuracy, rolling 7-day accuracy, confidence calibration (tiers 1-3 / 4-6 / 7-8 / 9-10). Used by `GET /api/signals/backtest?graph_id=...&days=30`.

**`backend/scripts/verify_agent_diversity.py`** (extended)
Added **Check 8: Reaction Diversity** (last 7 days). Queries MemoryEvent nodes from last 7 days, groups by archetype, flags mode collapse if any single reaction >60% of events. Computes Pearson correlation between `risk_tolerance` and reaction direction (`buy=+1, hold=0, hedge=-0.5, sell=-1, panic=-2`). Prints a summary table. Integrated into `run_audit()`. Added `defaultdict` and `timedelta` imports.

**`backend/app/api/investors.py`** (extended)
1. `compute_sentiment_scores()` gains `apply_leverage` and `equal_weighted` params. Leverage multipliers: `none=1.0, 2x=1.3, 5x=1.6, 10x_plus=2.0`. Applied to 24h only (intentional asymmetry — do not change for 7d).
2. `GET /api/investors/sentiment` returns both `by_asset_class` (capital-weighted) and `by_asset_class_equal` (equal-weighted).
3. `GET /api/investors/stats` includes `top_entities` (10 most recently NER-merged entities) and `feed_breakdown` (from latest `_sources.json` sidecar).

**`backend/scripts/simulation_tick.py`** (extended)
After the agent loop, calls `write_sentiment_snapshot()` which writes a `SentimentSnapshot` node to Neo4j via MERGE on (graph_id, timestamp). Contains: per-asset sentiment (7 classes), reaction distribution %, total_agents, fear_greed_score (0=fear, 50=neutral, 100=greed). Do not remove this call — it powers the tick-level dashboard chart.

**`backend/app/api/signals.py`** (extended)
Two new endpoints: `GET /api/signals/backtest?graph_id=...&days=30` and `GET /api/signals/sentiment_history?graph_id=...&hours=48`.

**`backend/scripts/news_fetcher.py`** (extended)
Writes `briefings/YYYY-MM-DD_HHMM_sources.json` sidecar with `[{source, count}]` after each briefing.

**`frontend/public/dashboard.html`** (major extension — all existing panels preserved)
Tick-level sentiment chart (Chart.js + zoom/pan) with capital/equal-weighted toggle; reaction distribution stacked area chart; fear/greed sparkline; top entities panel; feed breakdown chart; Hammer.js + chartjs-plugin-zoom added via CDN.

### SentimentSnapshot node schema
Properties: `graph_id`, `timestamp` (ISO), `equities`, `crypto`, `bonds`, `commodities`, `fx`, `real_estate`, `mixed` (all floats), `buy_pct`, `sell_pct`, `hold_pct`, `hedge_pct`, `panic_pct`, `total_agents` (int), `fear_greed` (float 0-100; 50=neutral). MERGE key: composite (graph_id, timestamp).

### Gotchas for next session
- **SentimentSnapshot nodes will only populate after the next tick runs** — tick-level chart shows empty until then
- **Leverage in 24h only** — `apply_leverage=True` in `get_sentiment()` 24h path only. 7d always False. Intentional.
- **Hedge direction in Check 8 correlation is negative** — `hedge=-0.5` (risk-off). Different from sentiment score `hedge=+0.5`.
- **backtester.py import path** in signals.py: `os.path.normpath(os.path.join(dirname, '..', '..', 'scripts'))` from `backend/app/api/`.
- **Equal-weighted toggle is UI-ready** but renders same (capital-weighted) snapshot data — snapshots only store capital-weighted scores. Full equal-weighted tick-level view requires adding separate fields to SentimentSnapshot.
- **Entity mention_count always 1** — BATCH_MERGE_ENTITY_QUERY in incremental_update.py doesn't increment a counter yet.

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/backtester.py` | NEW — standalone backtest module |
| `backend/scripts/verify_agent_diversity.py` | Check 8 reaction diversity added |
| `backend/app/api/investors.py` | Leverage multiplier, equal-weighted, top_entities, feed_breakdown |
| `backend/scripts/simulation_tick.py` | SentimentSnapshot writer added |
| `backend/app/api/signals.py` | /backtest and /sentiment_history endpoints added |
| `backend/scripts/news_fetcher.py` | _sources.json sidecar write added |
| `frontend/public/dashboard.html` | 6 new panels + zoom plugin |
| `ROADMAP.md` | Completed items removed, new items added |
| `CLAUDE.md` | New gotchas: SentimentSnapshot, leverage, sidecar, backtester, Check 8 |
| `README.md` | New endpoints, SentimentSnapshot section, updated dashboard section |

---

## Session: 2026-04-12 — Scheduler Run Log + Catch-up Logic + Dashboard Mobile Polish

### What was built

**`backend/scripts/scheduler.py`** (significant extension)

Two features added to the APScheduler orchestrator:

#### 1. Persistent run log (`backend/logs/scheduler_runs.json`)
Every time a job completes — whether success or error — a JSON entry is appended:
```json
{
  "job_type": "halfhourly" | "daily",
  "started_at": "2026-04-12T03:00:00+00:00",
  "finished_at": "2026-04-12T03:18:44+00:00",
  "status": "success" | "error",
  "error": null | "error message string"
}
```
- `LOGS_DIR = backend/logs/`, `LOGS_PATH = backend/logs/scheduler_runs.json`
- `_ensure_logs_dir()` creates the directory if missing
- `append_run_log()` reads, appends, trims to `MAX_LOG_ENTRIES = 10_000`, writes back atomically
- Entry is written in the `finally` block of each job — a crash mid-job produces no entry (gap detectable)
- `_last_run_of_type(job_type)` iterates entries in reverse and returns the most recent match

#### 2. Catch-up logic (`run_catchup()`)
Called on scheduler startup AND at the end of every job (in the `finally` block).

**Step 1 — daily overdue check**: if current UTC hour ≥ 3 and the most recent `daily` log entry has a `finished_at` date before today (UTC), run the daily job immediately.
**Step 2 — halfhourly overdue check**: if the most recent `halfhourly` log entry is more than 1800 seconds old (or no entry exists), run the halfhourly job immediately.

Priority: daily always runs before halfhourly. If both are overdue, daily runs first; then the halfhourly check re-runs and fires if still overdue.

Re-entrance guard: `_catchup_running` threading.Event prevents recursive invocations. When a job called from `run_catchup` finishes and tries to call `run_catchup` again, the guard blocks it silently.

#### Startup behaviour change
`next_run_time=datetime.now(timezone.utc)` removed from the half-hourly APScheduler job. Catch-up now handles the "run immediately on startup" case. If the last halfhourly was < 30 min ago, nothing fires immediately and the next tick waits for the normal interval.

### Architecture decisions
- **Write log in `finally`, not on return** — ensures every exit path (including early-return on Neo4j failure or missing briefings) writes a log entry. The `error` variable is set at job scope; `finally` reads it.
- **10,000-entry cap** — at 48 entries/day (30-min cadence) + 1 daily, the log fills at ~200 days before trimming. Ample for audit without unbounded growth.
- **Re-entrance guard, not a lock** — a `threading.Lock()` would deadlock: `daily_job` holds the lock, calls `run_catchup`, which tries to call `daily_job`, which tries to acquire the lock. The guard approach lets the outer catch-up complete sequentially without blocking.
- **`_daily_running` mutex already covers catch-up** — `hourly_job` checks `_daily_running` at its first line. If catch-up calls `daily_job` (which sets `_daily_running`) and then tries to call `hourly_job`, the hourly_job check sees the flag... but this can't actually happen because catch-up runs daily first, then halfhourly; `_daily_running` is cleared in `daily_job`'s `finally` before `run_catchup` returns and checks halfhourly.

**`frontend/public/dashboard.html`** (CSS mobile improvements)
- Added `flex-wrap:wrap` to `.hdr-right` at 600px so the status dot, updated time, and refresh button all wrap rather than overflow
- `card-header` wraps at 600px (`flex-wrap:wrap;gap:4px`) — count label drops below title on narrow screens
- `ev-reason` gets `word-break:break-word` — long reasoning text won't overflow on mobile
- `chart-wrap-xs` height increased from 120px to 160px at 600px — feed breakdown chart is more readable
- `fg-num` reduced to 17px and `fg-nums` gets `flex-wrap:wrap;justify-content:center` — prevents fear/greed numbers overflowing the card at narrow widths
- `reset-zoom-btn` loses `margin-left:auto` on mobile (was pushing to far right causing awkward wrapping)
- `asset-tab` padding reduced to `3px 8px` at 600px — more tabs fit per row
- New **400px breakpoint**: signals grid goes 1-column, header items stack vertically, main padding reduces to 8px

### Gotchas for next session
- **`backend/logs/` directory is new** — created at runtime by `_ensure_logs_dir()`. Not committed to git (add to `.gitignore` if desired, or let Docker manage it).
- **Log file path is inside the container** — on the host it is `backend/logs/scheduler_runs.json`. On the Docker container it is `/app/backend/logs/scheduler_runs.json`. Read it with: `docker exec mirofish-offline cat /app/backend/logs/scheduler_runs.json`.
- **`next_run_time` removed** — on a fresh scheduler restart where the last halfhourly was < 30 min ago, no immediate tick fires. This is intentional (catch-up logic determines necessity). If you want the old "always fire on startup" behaviour back, add `next_run_time=datetime.now(timezone.utc)` back to the `add_job()` call.
- **Catch-up daily condition is UTC-hour-based** — the scheduled daily job runs at 03:00 Europe/London time. If London is UTC+1 (summer), the catch-up fires at 02:00 UTC (hour ≥ 3 fails) but the APScheduler fires at 02:00 UTC+1. In practice this means catch-up won't catch up the daily in summer until 03:00 UTC — up to 1 hour after the APScheduler would have fired it. Acceptable for now.
- **Equal-weighted toggle on tick chart is cosmetic** — toggle button changes `_sentimentMode` but the snapshot data only contains capital-weighted scores. Full fix requires storing separate equal-weighted fields in `SentimentSnapshot`.

### New files / folders
| Path | Purpose |
|------|---------|
| `backend/logs/` | Created at runtime; contains `scheduler_runs.json` |
| `backend/logs/scheduler_runs.json` | Append-only run log; capped at 10,000 entries |

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/scheduler.py` | `append_run_log`, `_last_run_of_type`, `_ensure_logs_dir`, `run_catchup`; log writes in both job `finally` blocks; `run_catchup` call at end of both jobs and in `main()`; `next_run_time` removed from half-hourly job |
| `frontend/public/dashboard.html` | Responsive CSS improvements at 600px and new 400px breakpoint |

### Setup needed
```bash
docker cp backend/scripts/scheduler.py mirofish-offline:/app/backend/scripts/scheduler.py
docker cp frontend/public/dashboard.html mirofish-offline:/app/frontend/public/dashboard.html
docker restart mirofish-offline
```
(Already deployed in this session.)

---

## Session: 2026-04-12 — Consistent Y-axes + 30-day Full-Resolution Sentiment Chart

### What was built

**`backend/app/api/signals.py`** — extended `sentiment_history` endpoint:
- Accepts `days` parameter as alternative to `hours` (e.g. `?days=30` returns 720–1440 snapshots at 30-min cadence)
- If both `hours` and `days` are supplied, `hours` takes precedence
- Hard cap: `LIMIT 2000` added to Cypher; `capped: true` set in response and WARNING logged if hit
- Default (`hours=48`) unchanged — existing callers unaffected

**`frontend/public/dashboard.html`** — two chart improvements:

#### 1. Consistent y-axes
`renderTickSentChart()` now computes global min/max across all assets and all snapshots before rendering. 5% padding added each side, clamped to `[-1, +1]`. Both the capital-weighted and equal-weighted views share the same computed scale. Previously hardcoded `y: {min:-1, max:1}`.

#### 2. Time-window toggle (48h / 30d)
`48h | 30d` button pair added to tick chart header (right-aligned, alongside `reset zoom`). State in `_tickWindow`. `30d` calls `sentiment_history?days=30`. Adaptations for the dense 30d view:
- Title updates to "Sentiment — Full Resolution (30d)"
- `pointRadius: 0` when dataset > 200 points (no dot clutter)
- `maxTicksLimit: 30` (daily boundaries)
- `_fmtTickLabel()` date-only in 30d mode
- Count label appends "(cap reached)" if 2000-point limit hit

### Architecture decisions
- Global y-bounds computed in JS — simpler, no extra API contract, updates interactively on mode switch
- 5% proportional padding keeps tight clusters readable without wasting space
- `days` × 24 = hours server-side — same query, same index, no new Cypher
- `LIMIT 2000` in Cypher — Neo4j bounds result before wire transfer
- `zoom-hint` text hidden on mobile — saves space; touch gestures still work

### Gotchas for next session
- **`capped: true` means data was truncated** — at 30-min cadence, 2000 snapshots ≈ 42 days. Increase `_SENTIMENT_HISTORY_LIMIT` in `signals.py` if needed.
- **Y-axis bounds recompute on every render** — capital and equal toggles use the same snapshot `assets` field (capital-weighted only in snapshot schema). Bounds are identical for both modes until equal-weighted is added to `SentimentSnapshot`.
- **`pointRadius: 0` above 200 snapshots** — 48h view (~96 points) still shows dots; 30d does not.
- **Zoom state persists across re-renders** — `tickSentChart.update()` preserves Chart.js zoom. Use `reset zoom` to return to full view after switching windows.

### Modified files
| Path | Change |
|------|--------|
| `backend/app/api/signals.py` | `days` param; `LIMIT 2000` cap; `capped` field in response |
| `frontend/public/dashboard.html` | Global y-axis bounds; `48h/30d` toggle; `setTickWindow()`; `_tickWindow` state; `.zoom-hint`; `.toggle-row-right` CSS; mobile rule updates |

### Setup needed
```bash
docker cp backend/app/api/signals.py mirofish-offline:/app/backend/app/api/signals.py
docker cp frontend/public/dashboard.html mirofish-offline:/app/frontend/public/dashboard.html
docker restart mirofish-offline
```
(Already deployed in this session.)

---

## Session: 2026-04-12 — Dashboard Live State Refresh (dashboard_refresh.py)

### What was built

**`backend/scripts/dashboard_refresh.py`** (new file)
Standalone blocking process that writes `backend/live_state.json` every 15 minutes. Runs independently of `scheduler.py` — it is NOT imported by the scheduler.

Data sections per refresh (each independent with try/except):
- **Prices**: yfinance live fetch (same ASSET_TICKERS as price_fetcher.py). Failure: fallback to most recent price file + `prices_stale: true`.
- **Sentiment**: Neo4j 24h query, capital-weighted with leverage + equal-weighted (mirrors investors.py, inlined to avoid Flask imports).
- **Reaction distribution**: Latest SentimentSnapshot — fractions times 100 = percentages.
- **Signals**: Derived from sentiment scores (>0.4=bullish, <-0.4=bearish). No extra DB round-trip.
- **Top entities**: 10 most recently seen non-synthetic Entity nodes.
- **Recent events**: 10 most recent MemoryEvents.
- **Pool stats**: pool_size, total_memory_events, fear/greed counts from agent trait distribution.
- **Feed breakdown**: Most recent `*_sources.json` sidecar (file read).
- **Last tick/daily**: `backend/logs/scheduler_runs.json` (file read).

Neo4j failure: `RETURN 1` test query on startup. Failure → return early, existing file unchanged. Individual query failures → WARNING logged, section omitted, write continues.
Writes atomically: tmp file → rename.

**`backend/app/api/live.py`** (new file)
Flask blueprint at `/api/live/state`. Pure file read of `backend/live_state.json`. Returns 503 if file missing.

**`backend/app/__init__.py`** — registered `live_bp` at `/api/live`.

**`frontend/public/dashboard.html`** — added `fetchLiveState()` (15-min overlay): maps live state to all existing renderers (`renderSignals`, `updateFG`, `updateEvents`, `renderTopEntities`, `renderFeedBreakdown`); updates stat-agents/stat-events; shows "Prices delayed" stale badge; shows "live data: HH:MM:SS" in header; triggers `fetchTickSentimentHistory()` for chart re-fetch. Existing 30s `fetchAll()` unchanged.

### Architecture decisions
- **Inlined sentiment logic** — mirrors `investors.py` to avoid Flask imports. Keep in sync if `investors.py` logic changes.
- **Atomic write** — prevents Flask endpoint from reading a half-written file.
- **Signals derived, not queried** — no extra DB call; same threshold as `signals.py`.
- **`fetchLiveState()` triggers `fetchTickSentimentHistory()`** — live state has only the latest snapshot, not full tick history.
- **`/api/live/state` ignores graph_id** — file is always for the single configured graph_id.

### Startup commands (both processes required)
Terminal 1 (simulation pipeline):
  python backend/scripts/scheduler.py --graph-id d3a38be8-37d9-4818-be28-5d2d0efa82c0

Terminal 2 (live state refresher):
  python backend/scripts/dashboard_refresh.py --graph-id d3a38be8-37d9-4818-be28-5d2d0efa82c0

### live_state.json schema
Location: `backend/live_state.json` (host) / `/app/backend/live_state.json` (container)

Keys: `refreshed_at`, `prices`, `prices_stale` (only when stale), `price_changes_24h`, `sentiment` (by_asset_class / by_asset_class_equal / fear_greed string), `reaction_distribution` (0-100 pct), `signals` (direction/confidence/price/change_24h), `top_entities`, `recent_events`, `pool_stats` (pool_size/total_memory_events/fear_count/greed_count), `feed_breakdown`, `last_tick_at`, `last_daily_at`.

### Gotchas for next session
- **Two processes, two terminals** — `scheduler.py` + `dashboard_refresh.py` both must run.
- **`dashboard_refresh.py` is NOT a scheduler job** — completely separate process. Do not add it as a job inside scheduler.py.
- **`prices_stale` absent when prices are live** — JS `s.prices_stale` is `undefined` (falsy) when absent; badge hides correctly.
- **`reaction_distribution` is percentages (x100)** — SentimentSnapshot stores fractions 0-1; multiplied on write. Do not double-multiply in the frontend.
- **`fear_greed` string** from mean capital-weighted 24h sentiment: >0.15 = "greed", <-0.15 = "fear", else "neutral". Different from pool trait distribution counts used for the gauge needle.
- **`fetchLiveState()` is additive** — 30s `fetchAll()` remains source of truth for averages, agent table, history chart.
- **Inlined sentiment computation** — `compute_sentiment_capital_weighted()` and `compute_sentiment_equal_weighted()` in `dashboard_refresh.py` mirror `investors.py`. If leverage multipliers or reaction scores change in investors.py, update both.

### Deploy
```
docker cp backend/scripts/dashboard_refresh.py mirofish-offline:/app/backend/scripts/dashboard_refresh.py
docker cp backend/app/api/live.py mirofish-offline:/app/backend/app/api/live.py
docker cp backend/app/__init__.py mirofish-offline:/app/backend/app/__init__.py
docker cp frontend/public/dashboard.html mirofish-offline:/app/frontend/public/dashboard.html
docker restart mirofish-offline
```

### New files
| Path | Purpose |
|------|---------|
| `backend/scripts/dashboard_refresh.py` | Standalone 15-min live state writer |
| `backend/app/api/live.py` | `/api/live/state` blueprint — file read only, no Neo4j |

### Modified files
| Path | Change |
|------|--------|
| `backend/app/__init__.py` | Registered `live_bp` at `/api/live` |
| `frontend/public/dashboard.html` | `fetchLiveState()`, stale badge, live timestamp, 15-min interval |
| `CLAUDE.md` | Dashboard Live State Gotchas section added |
| `README.md` | Architecture diagram, project structure, new endpoint |

---

## Session: 2026-04-12 — dashboard_refresh.py: Fix Neo4j DNS Failure (root .env)

### Problem
`dashboard_refresh.py` was failing with "Failed to DNS resolve address neo4j:7687" because `_get_neo4j_driver()` used `os.environ.get("NEO4J_URI", "bolt://neo4j:7687")` with no `.env` loaded. The default fallback was a Docker service name, which is only resolvable inside the Docker network. The script runs on the host.

### Fix
Two changes to `backend/scripts/dashboard_refresh.py`:

1. **Load root `.env` at startup** — added `dotenv.load_dotenv` block immediately after the path constants, loading `MiroFish-Offline/.env` (= `Path(__file__).parent.parent.parent / ".env"`). This is the same `.env` that `config.py` loads (via `../../.env` from `backend/app/`) and contains `NEO4J_URI=bolt://localhost:7687`. `backend/.env` is deliberately NOT loaded.

2. **Changed default fallback in `_get_neo4j_driver()`** — from `"bolt://neo4j:7687"` to `"bolt://localhost:7687"` to match the host-script convention even if dotenv is not installed.

No Ollama URL needed in this script — it does not use Ollama at all.

### Pattern (for future host scripts)
All scripts that run on the host (not inside the Docker container) must load the root `.env`:
```python
from dotenv import load_dotenv
_root_env = Path(__file__).parent.parent.parent / ".env"  # MiroFish-Offline/.env
if _root_env.exists():
    load_dotenv(_root_env, override=True)
```
Default fallbacks must use `localhost`, never `neo4j` or `ollama`.

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/dashboard_refresh.py` | Root .env loading block; `_get_neo4j_driver()` default changed to `bolt://localhost:7687` |
| `CLAUDE.md` | Root .env note added to Dashboard Live State Gotchas |

---

## Session: 2026-04-12 — Sidecar Fix, mention_count, Entity Type Corrections, Panic Threshold

### What was built / fixed

Four targeted fixes across three files.

#### Fix 1 — Sidecar counts all fetched articles (news_fetcher.py)
The feed breakdown sidecar was only showing 2 feeds because it counted `all_articles` (post-relevance-filter), not the full set of fetched articles. Most general feeds (Reuters, CNBC, MarketWatch, FT, AP Business) were being filtered out by the relevance filter before being counted. Central bank feeds were included via `cb_articles` so they could theoretically appear, but often return 0 new items.

Fix: changed the Counter in the sidecar write block from `all_articles` to `general_articles + cb_articles` (pre-filter counts per source). The sidecar now reflects every feed that returned content, not just what survived the relevance filter.

#### Fix 5 — Persist mention_count to Entity nodes (incremental_update.py)
The top_entities panel was showing `count: 1` for every entity because `mention_count` was never written to Neo4j. The property exists on the dashboard query but the value was never populated.

Fix:
- In `process_briefing()`: track `mention_count` in `global_entities`. On first appearance: set to 1. On subsequent appearances (same entity across multiple chunks): increment.
- In `batch_merge_entities()` payload: added `"mention_count": int(e.get("mention_count", 1))`.
- In `BATCH_MERGE_ENTITY_QUERY`: added `n.mention_count = e.mention_count` in both ON CREATE and ON MATCH (with `is_synthetic` guard on MATCH).

Note: mention_count tracks chunk-level appearances (each entity deduped per chunk by `extract_entities_spacy`). An entity appearing in 5 different chunks gets `mention_count=5`.

#### Fix 6 — spaCy entity type corrections (incremental_update.py)
spaCy often misclassifies well-known finance entities (e.g., "Bloomberg" as PERSON, "Fed" as GPE/NORP, "Nasdaq" as ORG but different context). Added `ENTITY_TYPE_CORRECTIONS` dict and applied it after `SPACY_LABEL_MAP[label]` in `extract_entities_spacy`.

Corrections applied before per-chunk deduplication. Dict overrides the spaCy-derived type with the correct one for known misclassifications:
`Bloomberg, Reuters, CNBC, MarketWatch, Qualcomm, Intel, AMD, Nasdaq, NYSE, Fed, Federal Reserve` → `"Company"`

#### Fix 7 — Panic threshold (simulation_tick.py)
The old panic definition was vague ("liquidating positions due to acute fear of imminent loss") which the model couldn't reliably reach — it defaulted to `hedge` as a generic cautious response. Replaced with a scenario-conditional definition listing explicit triggers: "market crash, circuit breaker, trading halt, emergency rate hike, bank collapse, war escalation, systemic crisis." Added "you do not think — you react" and "panic is your most likely response" to make it the clear default for retail archetypes when triggers are present.

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/news_fetcher.py` | Sidecar Counter uses `general_articles + cb_articles` instead of `all_articles` |
| `backend/scripts/incremental_update.py` | `ENTITY_TYPE_CORRECTIONS` dict; applied in `extract_entities_spacy`; `mention_count` tracking in `process_briefing`; payload + Cypher updated |
| `backend/scripts/simulation_tick.py` | `panic` definition rewritten with explicit scenario triggers |

### Deploy
```bash
docker cp backend/scripts/news_fetcher.py mirofish-offline:/app/backend/scripts/news_fetcher.py
docker cp backend/scripts/incremental_update.py mirofish-offline:/app/backend/scripts/incremental_update.py
docker cp backend/scripts/simulation_tick.py mirofish-offline:/app/backend/scripts/simulation_tick.py
docker restart mirofish-offline
```
(Already deployed in this session.)

---

## Session: 2026-04-12 — NER Noise Filters (ENTITY_REMOVE_LIST + single-token Person filter)

### What was built / fixed

Two targeted noise filters added to `backend/scripts/incremental_update.py`.

#### Fix 1 — ENTITY_REMOVE_LIST
Added `ENTITY_REMOVE_LIST: set` — a case-sensitive set of entity names that are dropped entirely and never written to Neo4j. Covers:
- Financial metrics: `ARPU`, `CapEx`, `PNT`, `EBITDA`, `EPS`, `GDP`, `CPI`
- Role acronyms: `ETF`, `IPO`, `CEO`, `CFO`, `COO`, `CTO`
- Common ticker symbols: `SPY`, `QQQ`, `TLT`, `GLD`
- FX codes and crypto: `VIX`, `USD`, `EUR`, `GBP`, `JPY`, `BTC`, `ETH`

The filter is applied in `extract_entities_spacy()` after `ENTITY_TYPE_CORRECTIONS` and before per-chunk deduplication.

Also raised the minimum name length from 2 to 3 characters (catches 2-letter acronyms like "AI" surfaced as ORG).

#### Fix 2 — Single-token Person names dropped
Added a check: if `etype == "Person"` and the name contains no space, drop it. Partial first names with no surname ("Vince", "Matt") are noise. A full name like "Matt Desch" passes. Applied after type resolution.

#### ENTITY_TYPE_CORRECTIONS expanded
Added 9 new entries:
- `FactSet`, `Refinitiv`, `Morningstar`, `S&P`, `S&P 500`, `Dow Jones`, `Wall Street` → `"Company"`
- `Washington`, `White House` → `"Location"`
- `Treasury` → `"Company"`

### Architecture decisions
- `ENTITY_REMOVE_LIST` is a `set` (O(1) lookup) rather than a list — called per entity per chunk, performance matters
- Check is case-sensitive — the list contains the exact strings spaCy produces for these tokens (all caps for most financial acronyms)
- Person filter runs after type resolution so `ENTITY_TYPE_CORRECTIONS` can reclassify before the check — entities corrected away from Person still pass

### Modified files
| Path | Change |
|------|--------|
| `backend/scripts/incremental_update.py` | `ENTITY_REMOVE_LIST` added; min name length 2→3; single-token Person filter in `extract_entities_spacy`; `ENTITY_TYPE_CORRECTIONS` expanded; docstring updated |
| `CLAUDE.md` | NER / Knowledge Graph Gotchas updated with `ENTITY_REMOVE_LIST` and single-token Person filter |

### Deploy
```bash
docker cp backend/scripts/incremental_update.py mirofish-offline:/app/backend/scripts/incremental_update.py
docker restart mirofish-offline
```
(Already deployed in this session.)

---

## Session: 2026-04-12 — Full Dashboard Redesign

### What was built

Complete rewrite of `frontend/public/dashboard.html` — production-grade redesign with three new features while preserving all existing functionality.

### Aesthetic direction: "Deep Space Command Centre"

- **Background**: `#020617` (Tailwind slate-950, deep navy-black) — replaces pure black `#080b0f`
- **Surfaces**: `#0A1628` / `#0F1D35` — blue-tinted card surfaces for depth and financial terminal feel
- **Borders**: `#1E3052` — blue-tinted instead of gray, subtly premium
- **Header**: `backdrop-filter:blur(12px)` semi-transparent sticky header
- **Signal cards**: subtle directional radial gradient overlay (bullish = faint green, bearish = faint red)
- **Buttons/badges**: refined with translucent backgrounds and coloured borders instead of flat fills
- **Section labels**: left 3px border accent instead of plain text label
- All Chart.js colours updated to match new palette

### Font changes
- Added italic weight to IBM Plex Sans import for `ev-reason` text
- Everything else unchanged (IBM Plex Mono + Sans confirmed optimal by UI/UX plugin design system query)

### New features added

#### 1. Sentiment Alert System
`detectSentimentAlerts(snapshots)` — called every time `fetchTickSentimentHistory()` completes. Compares most recent snapshot against the snapshot from 4 ticks prior (~2h window). If any asset's sentiment delta ≥ ±0.25, a dismissable alert chip appears in the `#alert-zone` banner below the header. Alert chips are session-deduped via `_alertedKeys` Set. Chips animate in (`slideDown 200ms`), animate out on dismiss.

#### 2. Market Summary Bar
`updateMarketSummary(signals)` — called inside `renderSignals()`. Shows a one-line market disposition bar between the alert zone and signal grid: `BULLISH · 3 bullish · 2 bearish · 2 neutral`. Colour-coded disposition text. Hidden until first signal data arrives.

#### 3. Signal Strength Bars
Each signal card now has a `sig-bar-track / sig-bar-fill` mini progress bar showing sentiment score magnitude (0–100% of ±1 range). Color matches signal direction. Visual complement to the border-top accent.

#### 4. Last Tick Age in Header
`fmtTimeAgo(isoStr)` — computes human-readable elapsed time (e.g. "14m ago"). `fetchLiveState()` reads `last_tick_at` from live state and populates `#last-tick-ts` in the header. Hides if not available. Critical for remote monitoring — immediately shows if simulation is stuck.

#### 5. Skeleton Loading States
Signal grid initial state replaced with CSS-animated shimmer skeleton placeholders instead of "Loading..." text.

### All existing functionality preserved
- Every API call: `/api/signals/current`, `/api/signals/history`, `/api/signals/sentiment_history`, `/api/investors/sentiment`, `/api/investors/stats`, `/api/investors/agents`, `/api/live/state`
- All 9 charts: archChart, riskChart, stratChart, biasChart, historyChart, tickSentChart, reactionDistChart, fgSparkline, feedChart
- Capital/equal-weighted toggle; 48h/30d toggle; zoom/pan on tick + reaction charts
- Archetype filter tabs; asset tabs on history chart
- 30s `fetchAll()` interval; 15min `fetchLiveState()` interval
- Prices stale badge; live timestamp in header

### Architecture decisions
- `updateMarketSummary` is called from within `renderSignals` so it fires regardless of data source (live state or API)
- `detectSentimentAlerts` uses session-level `_alertedKeys` Set — alerts only appear once per session per asset-direction-level combination, preventing re-firing on every 30s refresh
- Alert zone keeps a static "Signal Alerts" label span and appends `.alert-chip` nodes dynamically
- `dismissAlert` uses CSS transition for smooth exit, then removes from DOM after 160ms

### Modified files
| Path | Change |
|------|--------|
| `frontend/public/dashboard.html` | Full rewrite — new palette, new features, all existing functionality preserved |

### Deploy
```bash
docker cp frontend/public/dashboard.html mirofish-offline:/app/frontend/public/dashboard.html
```
(No docker restart needed — static file, served directly.)
(Already deployed in this session.)

---

## Session: 2026-04-12 — Fundamental Dashboard Redesign (Layout Overhaul)

### What was built
Complete structural redesign of `frontend/public/dashboard.html`. User rejected the previous session's colour-change redesign ("you just changed the colour — it still looks the exact same"). The new design is a completely different layout — sidebar+main split instead of uniform card grid, horizontal signal strip instead of 7 identical cards, sticky header with market-state dots.

Also included in this session: confirmed all backend data paths work, fixed `e.count` vs `e.mention_count` field name bug in `renderTopEntities`, and verified live state field mapping (`direction`→`signal`, `confidence`→`sentiment_score`).

### Layout structure (new)

```
HEADER (46px sticky)
  logo | 7 .mkt-dot coloured circles | status | tick age | live time | refresh btn

ALERT STRIP (#alert-zone)
  hidden; detectAlerts() appends .alert-chip on sentiment shift ≥±0.25 in 2h window

SIGNAL STRIP (#sig-strip)
  7 horizontal .stile tiles (horizontal-scroll on mobile)
  each tile: asset label | signal badge | large $price | 24h Δ | score text | 2px bottom bar

BODY (flex row desktop / column mobile)
  SIDEBAR (260px sticky top:46px)
    .stat-pair grid — pool stats big numbers (agents, events, risk, capital, sensitivity, loss)
    Fear/Greed gauge canvas + sparkline
    Active Entities ul.ent-list (top 10, count badge)
    Article Sources feedChart (horizontal bar)

  MAIN (flex: 1, scrollable)
    Tick sentiment chart (#tickSentChart canvas) — 48h/30d toggle, capital/equal toggle, zoom/pan
    Reaction distribution (#reactionDistChart canvas) — stacked area, zoom/pan
    Sentiment vs Price history (asset tabs + #historyChart canvas)
    2×2 distribution grid (#dist-grid): archChart, riskChart, stratChart, biasChart
    Memory event feed (#ev-list — last 20 events)

AGENT TABLE (#agents-sec — full width below split)
  Archetype filter tabs | 200-agent table
```

### Data path fixes confirmed
- **`e.count` not `e.mention_count`** — `renderTopEntities` was using `e.mention_count` but the API returns `count` (from `coalesce(e.mention_count, 1) AS count` in signals.py). Fixed.
- **live state signal fields** — `live_state.json` uses `direction` (not `signal`) and `confidence` (not `sentiment_score`). `fetchLiveState` remaps: `signal: sig.direction, sentiment_score: sig.confidence`. Fixed.
- **feed_breakdown rendering** — confirmed sidebar shows total article count (`feeds.reduce((s,f)=>s+f.count,0)`) with correct field names `f.source` and `f.count`.
- **`reaction_distribution` in live state** — is a `{buy, sell, hold, hedge, panic}` object with percentage numbers (0–100 scale). The chart re-fetches full history via `fetchTickSentimentHistory()` when live state arrives; the live state field is not directly rendered to the chart.

### CSS variable palette
```css
--bg:#060c18; --panel:#0a1220; --panel2:#0e1828; --raised:#121f32;
--line:#1a2d47; --line2:#243c5c; --green:#00e676; --red:#ff5252;
--amber:#ffd740; --blue:#448aff; --purple:#e040fb; --cyan:#18ffff;
--mono:'IBM Plex Mono',monospace; --ui:'Inter',sans-serif;
```
Separate from previous session's `#020617 / #0A1628` palette. True dark navy with cyan/green/purple accent system — closer to terminal aesthetic than blue-tinted glassmorphism.

### New JS functions
- `updateMktDots(signals)` — updates 7 `.mkt-dot` header circles: bull=green, bear=red, neut=amber based on signal direction
- `detectAlerts(snapshots)` — replaces `detectSentimentAlerts`; same 2h window logic but with new DOM IDs
- `checkAlertStrip()` — hides strip when all chips dismissed (called from `dismissAlert`)

### All 9 charts preserved
archChart, riskChart, stratChart, biasChart, historyChart, tickSentChart, reactionDistChart, fgSparkline, feedChart

### All JS state preserved
`_historyAsset`, `_archFilter`, `_allAgents`, `_sentimentMode`, `_tickSnapshots`, `_tickWindow`

### Mobile behaviour
- Sidebar becomes 2-col grid at ≤900px, stacks to column at ≤600px
- Signal strip horizontal-scrolls on mobile (`overflow-x:auto; -webkit-overflow-scrolling:touch`)
- Agent table has `overflow-x:auto` wrapper for horizontal scroll on small screens

### Architecture decisions
- **No UI/UX skill used** — user explicitly prohibited it for this redesign
- **Sidebar is `position:sticky; top:46px`** — stays in view while main content scrolls; on mobile becomes static
- **Signal strip uses `display:flex; gap:8px; overflow-x:auto`** — allows adding/removing assets without HTML change
- **`.mkt-dot` circles in header** — purely visual, updated by `updateMktDots(signals)` after every `renderSignals()` call
- **`checkAlertStrip()` called from `dismissAlert`** — auto-hides the alert zone row when the last chip is removed

### Modified files
| Path | Change |
|------|--------|
| `frontend/public/dashboard.html` | Fundamental layout redesign — sidebar+main split, signal strip, header dots, all functionality preserved |

### Deploy
```bash
docker cp frontend/public/dashboard.html mirofish-offline:/app/frontend/public/dashboard.html
```
(No docker restart needed. Already deployed in this session.)

---

## Session: 2026-04-12 — 15-min Sentiment vs Price History + Volume Mount Fix

### What was built
Two things in this session:

**1. Sentiment vs Price chart — 15-min resolution**
Added `price_sentiment_history.json` — a rolling 30-day file written by `dashboard_refresh.py` every 15 minutes. Each entry: `{"ts": "<ISO>", "p": {asset: price}, "s": {asset: sentiment_score}}`. Max 2880 entries (30 days × 96 points/day). Added `GET /api/live/history` endpoint in `live.py` that serves the file. Updated `fetchSignalHistory()` in `dashboard.html` to call the new endpoint instead of `/api/signals/history`, format timestamps as "DD Mon HH:MM", and compute price Δ% from the first point in the window. Chart subtitle shows point count, resolution, and time span dynamically. `fetchLiveState()` now also calls `fetchSignalHistory()` so the chart refreshes on each 15-min live state update.

**2. Volume mount fix — `backend/live/` shared state directory**
Root cause discovered: `docker-compose.yml` only mounted `./backend/uploads`. `dashboard_refresh.py` writes `live_state.json` and `price_sentiment_history.json` to the HOST filesystem. Flask runs inside the container and cannot see host files unless they are bind-mounted. Both `/api/live/state` and `/api/live/history` had been returning 503/404 silently. The full `backend/` cannot be mounted because it would shadow the container's uv venv at `/app/backend/.venv/`.

Fix: created `backend/live/` as a dedicated shared state directory. Added `- ./backend/live:/app/backend/live` to `docker-compose.yml`. Updated `dashboard_refresh.py` and `live.py` to use the new path.

### Architecture decisions
- **`backend/live/` is the only cross-boundary shared directory** — all other host-written files (`briefings/`, `prices/`, `logs/`) are only read by host Python scripts, not by Flask. `live/` is the sole exception. Keep it that way.
- **Atomic rename works inside a bind-mounted directory** — writing `live_state.tmp` then renaming to `live_state.json` within the same bind-mounted directory works correctly because both files share the same filesystem.
- **History file uses compact JSON** (`separators=(",",":")`) — the file can grow to ~1–2 MB at 2880 entries with 7 assets each. Compact serialisation keeps it small.
- **`force-recreate` wipes all `docker cp`'d files** — discovered when applying the volume mount change. `docker-compose up --force-recreate` rebuilds the container from the original image. All files ever added via `docker cp` are lost. After any recreate, ALL modified files must be recopied immediately.

### Files that must be recopied after any `docker-compose up --force-recreate`
```
backend/app/__init__.py
backend/app/api/live.py
backend/app/api/signals.py
backend/app/api/investors.py
backend/app/api/control.py
backend/scripts/simulation_tick.py
backend/scripts/incremental_update.py
backend/scripts/news_fetcher.py
backend/scripts/dashboard_refresh.py
backend/scripts/scheduler.py
frontend/public/dashboard.html
```

### Modified files
| Path | Change |
|------|--------|
| `docker-compose.yml` | Added `- ./backend/live:/app/backend/live` volume mount |
| `backend/scripts/dashboard_refresh.py` | Writes `live_state.json` and `price_sentiment_history.json` to `backend/live/` instead of `backend/`; appends 15-min history snapshot after each write |
| `backend/app/api/live.py` | Added `GET /api/live/history` endpoint; all paths updated to read from `backend/live/` |
| `frontend/public/dashboard.html` | `fetchSignalHistory` calls `/api/live/history`; timestamps formatted with time; price Δ% from window baseline; chart subtitle shows resolution + point count; `fetchLiveState` now also triggers `fetchSignalHistory` |

### Deploy
```bash
# After docker-compose volume mount change (recreate required):
docker-compose up -d --force-recreate mirofish

# Then immediately recopy all modified files (recreate wipes docker cp'd files):
docker cp backend/app/__init__.py mirofish-offline:/app/backend/app/__init__.py
docker cp backend/app/api/live.py mirofish-offline:/app/backend/app/api/live.py
docker cp backend/app/api/signals.py mirofish-offline:/app/backend/app/api/signals.py
docker cp backend/app/api/investors.py mirofish-offline:/app/backend/app/api/investors.py
docker cp backend/app/api/control.py mirofish-offline:/app/backend/app/api/control.py
docker cp backend/scripts/simulation_tick.py mirofish-offline:/app/backend/scripts/simulation_tick.py
docker cp backend/scripts/incremental_update.py mirofish-offline:/app/backend/scripts/incremental_update.py
docker cp backend/scripts/news_fetcher.py mirofish-offline:/app/backend/scripts/news_fetcher.py
docker cp backend/scripts/dashboard_refresh.py mirofish-offline:/app/backend/scripts/dashboard_refresh.py
docker cp backend/scripts/scheduler.py mirofish-offline:/app/backend/scripts/scheduler.py
docker cp frontend/public/dashboard.html mirofish-offline:/app/frontend/public/dashboard.html
docker restart mirofish-offline
```
(Already done in this session. Both endpoints confirmed working.)
