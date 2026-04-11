# MiroFish-Offline: Investor Traits, Agent Evolution & Live News — Design Spec

**Date:** 2026-04-09
**Status:** Approved

---

## Overview

Three independent feature sets added to MiroFish-Offline. All new scripts are standalone (Option C architecture), live in `/backend/scripts/`, import config and services the same way existing simulation runners do, and require no new Flask endpoints. The existing UI, API routes, and graph build pipeline are untouched.

---

## Feature 1 — Investor Agent Trait System

### Scope

Modify `oasis_profile_generator.py` so that every agent generated during simulation preparation receives a full 16-field investor trait profile. Traits are stored as flat properties on the agent's existing `:Entity` node in Neo4j.

### Trait Fields

| Field | Type | Range / Values |
|---|---|---|
| `risk_tolerance` | float | 0–10 |
| `capital_usd` | float | 500–100,000,000 (log scale) |
| `time_horizon_days` | int | 1–3650 |
| `fear_greed_dominant` | str | `fear` / `greed` |
| `loss_aversion_multiplier` | float | 0.5–10.0 |
| `herd_behaviour` | float | 0–10 |
| `reaction_speed_minutes` | float | 1–10080 |
| `primary_strategy` | str | `day_trading` / `swing` / `value` / `growth` / `momentum` / `index` / `income` / `macro` / `contrarian` / `quant` |
| `asset_class_bias` | str | `equities` / `bonds` / `crypto` / `commodities` / `fx` / `real_estate` / `mixed` |
| `news_sensitivity` | float | 0–10 |
| `geopolitical_sensitivity` | float | 0–10 |
| `investor_archetype` | str | `retail_amateur` / `retail_experienced` / `prop_trader` / `fund_manager` / `family_office` / `hedge_fund` / `pension_fund` |
| `formative_crash` | str | `none` / `dotcom` / `gfc_2008` / `covid_2020` / `iran_war_2026` |
| `overconfidence_bias` | float | 0–10 |
| `leverage_typical` | str | `none` / `2x` / `5x` / `10x_plus` |
| `is_synthetic` | bool | False for document-extracted agents, True for evolver-generated |

### Distribution Strategy (Non-Normal / Fat Tails)

- `risk_tolerance`, `herd_behaviour`, `news_sensitivity`, `geopolitical_sensitivity`, `overconfidence_bias` — `Beta(0.5, 0.5)` × 10 (U-shaped, mass at extremes)
- `capital_usd` — `10 ** Uniform(log10(500), log10(100_000_000))` (log-uniform, equal probability across orders of magnitude)
- `time_horizon_days` — Pareto-distributed (many short-term, power-law tail of long-term)
- `loss_aversion_multiplier` — Log-normal (μ=0.7, σ=0.5), clamped to [0.5, 10.0]
- `reaction_speed_minutes` — Log-normal (μ=3.0, σ=1.5), clamped to [1, 10080]
- `investor_archetype` — Weighted categorical: `retail_amateur` 35%, `retail_experienced` 25%, `prop_trader` 15%, `fund_manager` 10%, `family_office` 7%, `hedge_fund` 6%, `pension_fund` 2%
- `primary_strategy` — Weighted: `day_trading` 15%, `swing` 15%, `value` 12%, `growth` 12%, `momentum` 12%, `index` 10%, `income` 8%, `macro` 7%, `contrarian` 5%, `quant` 4%
- `leverage_typical` — Weighted: `none` 55%, `2x` 25%, `5x` 12%, `10x_plus` 8%
- `formative_crash` — Weighted: `none` 25%, `dotcom` 15%, `gfc_2008` 35%, `covid_2020` 20%, `iran_war_2026` 5%
- `fear_greed_dominant` — Bernoulli(0.45 fear, 0.55 greed)
- `asset_class_bias` — Weighted: `equities` 35%, `mixed` 20%, `crypto` 15%, `bonds` 10%, `commodities` 8%, `fx` 7%, `real_estate` 5%

### Implementation

- New private method `_sample_investor_traits() -> dict` in `OasisProfileGenerator`
- Called inside `generate_profiles_from_entities()` for every agent, before LLM enhancement
- Traits written as `SET n += $traits` on the agent's `:Entity` node after profile creation
- `is_synthetic` set to `False` for all document-extracted agents here

### Files Modified

- `backend/app/services/oasis_profile_generator.py` — add `_sample_investor_traits()`, call it per agent

---

## Feature 2 — Iterative Agent Pool Evolution (`agent_evolver.py`)

### Scope

Standalone script that grows the agent pool in Neo4j to 4096 agents. New agents are hybrid: LLM invents name + backstory, trait values are fat-tail sampled (same distributions as Feature 1). Similarity filtering uses a FAISS index for O(log n) performance.

### Location

`/backend/scripts/agent_evolver.py`

### Flow

1. Call `db_setup.py` to ensure indexes exist
2. Load all existing agents from Neo4j (fetch trait fields + UUIDs)
3. Serialise each agent's traits as a short descriptive string: `"retail_amateur, day_trading, risk=8.2, capital=12000, horizon=30, herd=7.1, ..."`
4. Embed all existing trait strings via `EmbeddingService.embed_batch()` (nomic-embed-text, 768d)
5. Build a FAISS `IndexFlatIP` (inner product, vectors L2-normalised → cosine similarity) from existing embeddings
6. Loop until pool size ≥ 4096:
   a. LLM generates 512 candidate personas via single `chat_json` call — returns list of `{name, backstory, archetype}` (LLM chooses archetype; trait values are NOT LLM-generated)
   b. For each candidate:
      - Sample full trait profile using fat-tail distributions (archetype from LLM output, other traits sampled)
      - Serialise trait string and embed
      - **Gate 1 (FAISS):** query FAISS for nearest neighbour. If similarity ≤ 0.75 → accept immediately
      - **Gate 2 (Neo4j exact-triple):** if similarity > 0.75, query Neo4j for any agent with same `(investor_archetype, primary_strategy, risk_bucket)` where `risk_bucket = floor(risk_tolerance / 3.33)`. If match found → reject. Else → accept.
      - Accepted candidates: write to Neo4j via `MERGE (n:Entity {graph_id: $gid, name_lower: $name_lower})`, set all trait properties + `is_synthetic=True`
      - Add accepted embedding to FAISS index in-memory (no FAISS rebuild)
   c. Log batch stats: candidates generated, accepted, rejected, current pool size

### Deduplication Logic

- FAISS gate threshold: 0.75 (below this → accept without Neo4j check)
- Neo4j exact-triple gate threshold: blocks acceptance if archetype + strategy + risk_bucket all match
- Rejection does not retry the LLM — discarded silently, next batch compensates

### Dependencies

- `faiss-cpu` (pip install)
- All existing services: `Neo4jStorage`, `EmbeddingService`, `LLMClient`, `Config`

### Files Created

- `backend/scripts/agent_evolver.py`
- `backend/scripts/db_setup.py`

---

## Feature 3 — Live News Feed + Incremental Memory

### 3a — `news_fetcher.py`

**Location:** `backend/scripts/news_fetcher.py`

**RSS Sources:**
- Reuters Top News: `https://feeds.reuters.com/reuters/topNews`
- Yahoo Finance: `https://finance.yahoo.com/rss/topstories`
- CNBC Markets: `https://www.cnbc.com/id/15839069/device/rss/rss.html`
- MarketWatch Top Stories: `https://feeds.content.dowjones.io/public/rss/mw_topstories`

**Flow:**
1. Parse all four feeds via `feedparser`
2. Deduplicate entries by URL hash (across feeds and across runs — persist seen-URL hashes to `/backend/scripts/seen_urls.json` as `Dict[hash, iso_timestamp]`). On each run, prune entries older than 30 days before writing back.
3. For each new entry, attempt to fetch article body:
   - `requests.get(url, timeout=10)`
   - Parse with `BeautifulSoup`, extract `<p>` tags, join
   - If request fails (exception, non-200, or body < 200 chars) → fall back to RSS `summary` field silently, log to stdout
4. Write briefing to `/backend/briefings/YYYY-MM-DD_HHMM.txt`:
   - Header: `MIROFISH BRIEFING — {datetime}`
   - Each article: `HEADLINE: {title}\nSOURCE: {source}\n\n{body}\n\n---\n`
5. Return briefing file path

**Error handling:** Any single feed failure is caught and skipped; other feeds continue. Briefing is always written even if some feeds fail (may be empty if all fail — log warning).

### 3b — `incremental_update.py`

**Location:** `backend/scripts/incremental_update.py`

**CLI:** `python incremental_update.py --briefing /backend/briefings/2026-04-09_1400.txt --graph-id <uuid>`

**Flow:**
1. Call `db_setup.py`
2. Load graph ontology from Neo4j (`Graph` node's `ontology_json` property)
3. Read briefing file, preprocess via `TextProcessor.preprocess_text()`
4. Split into chunks via `TextProcessor.split_text()` (uses config chunk_size/overlap)
5. For each chunk:
   - `NERExtractor.extract(chunk, ontology)` → entities + relations
   - `EmbeddingService.embed_batch()` for entity + relation texts
   - Write to Neo4j: `MERGE (n:Entity {graph_id: $gid, name_lower: $name_lower}) WHERE (n.is_synthetic IS NULL OR n.is_synthetic = false)` — property updates (`SET n += $props`) are only applied when the matched node is not a synthetic agent
   - Synthetic agent nodes are structurally skipped even on a name collision
6. Agent nodes are fully protected — even if a news entity name collides with a synthetic agent name (e.g. "Goldman Sachs" in news vs a synthetic agent named "Goldman Sachs Investor"), the `is_synthetic` guard prevents any property overwrite on that node

**Active graph resolution:** Script accepts `--graph-id` explicitly. Scheduler passes the graph_id from the most recent completed project (read from project metadata files in `uploads/projects/`).

### 3c — `simulation_tick.py`

**Location:** `backend/scripts/simulation_tick.py`

**CLI:** `python simulation_tick.py --briefing /backend/briefings/2026-04-09_1400.txt --graph-id <uuid> [--full]`

**Flow:**
1. Call `db_setup.py`
2. Check for checkpoint file `/backend/scripts/tick_checkpoint.json` — if exists and `briefing_source` matches current briefing, load `processed_agent_ids` list and skip those agents
3. Query Neo4j for 500 random agents (or all agents if `--full` flag set): `MATCH (n:Entity) WHERE n.risk_tolerance IS NOT NULL RETURN n ORDER BY rand() LIMIT 500`
4. Filter out already-processed agents from checkpoint
5. For each agent (sequential, not parallel):
   a. Fetch agent's `:MemoryEvent` nodes from last 7 days: `MATCH (n)-[:HAS_MEMORY]->(m:MemoryEvent) WHERE m.timestamp > $seven_days_ago RETURN m ORDER BY m.timestamp`
   b. Select relevant briefing content per agent using pre-built chunk cache:
      - **Before the agent loop begins:** split briefing into chunks via `TextProcessor.split_text()` (chunk_size=500), embed all chunks via `EmbeddingService.embed_batch()`, store as `chunk_cache: list[tuple[str, list[float]]]` (chunk_text, embedding_vector). Log: `"Briefing indexed: {n} chunks cached"`. This runs exactly once per tick invocation.
      - **Inside the agent loop:** build an agent relevance query string from `asset_class_bias`, `geopolitical_sensitivity` (if ≥ 7, append "geopolitical risk sanctions war"), `primary_strategy`. Embed query, compute cosine similarity against each entry in `chunk_cache`, select top 5 by score, join into context string.
   c. Build LLM prompt:
      - System: "You are modelling an investor's reaction to financial news. Return only valid JSON."
      - User: trait profile (all 16 fields), last 7 days of memory events (reaction + reasoning), top-5 relevant briefing chunks
   d. Call `LLMClient.chat_json()`, expect: `{reaction, confidence, reasoning, assets_mentioned}`
      - Valid reactions: `buy / hold / sell / panic / hedge`
      - Confidence: float 0–10
      - Reasoning: string ≤ 2 sentences
      - assets_mentioned: list of strings
   e. Write new `:MemoryEvent` node, link via `[:HAS_MEMORY]`
   f. Every 100 agents: write checkpoint to `/backend/scripts/tick_checkpoint.json`
6. On clean completion: delete checkpoint file

**LLM failure handling:** If `chat_json` raises or returns invalid structure, log error and skip that agent (no partial write). Do not retry — scheduler will catch on next cycle.

### 3d — `scheduler.py`

**Location:** `backend/scripts/scheduler.py`

**Dependencies:** `apscheduler` (pip install)

**Jobs:**
- **Hourly job** (every 60 minutes): `news_fetcher` → `incremental_update` → `simulation_tick` (sequential, synchronous). On entry, checks `_daily_running` flag — if set, skips the tick step and logs `"Skipping hourly tick: daily full simulation in progress"`.
- **Daily job** (every 24 hours, at 03:00 local): sets `_daily_running = True`, runs `simulation_tick --full` (all agents, no sampling limit), clears `_daily_running = False` on completion or exception. The 03:00 start time ensures the ~6-hour full tick finishes by 09:00, outside peak hourly overlap windows.

**Race condition guard:** `_daily_running` is a `threading.Event` checked by the hourly job before running `simulation_tick`. The daily job sets and clears it atomically around the full tick. Hourly jobs that fire while daily is running skip their tick but still run `news_fetcher` and `incremental_update`.

**Status file** written after every job cycle to `/backend/scripts/status.json`:
```json
{
  "last_news_fetch": "2026-04-09T14:00:00",
  "last_tick": "2026-04-09T14:03:22",
  "last_full_simulation": "2026-04-09T02:00:00",
  "current_agent_pool_size": 1247,
  "last_error": null
}
```
On any exception during a cycle: `last_error` is set to `"{script_name}: {exception message}"`, cycle continues to next scheduled run.

---

## Shared Utility: `db_setup.py`

**Location:** `backend/scripts/db_setup.py`

Called at startup by every script. Creates the following indexes idempotently:

```cypher
CREATE INDEX IF NOT EXISTS FOR (m:MemoryEvent) ON (m.agent_uuid)
CREATE INDEX IF NOT EXISTS FOR (m:MemoryEvent) ON (m.timestamp)
CREATE INDEX IF NOT EXISTS FOR (n:Entity) ON (n.is_synthetic)
```

Also validates Neo4j connectivity and raises a clear error if unreachable.

---

## File Structure (new files only)

```
backend/
├── briefings/                          # NEW — briefing .txt files
│   └── YYYY-MM-DD_HHMM.txt
└── scripts/
    ├── db_setup.py                     # NEW — index creation + connectivity check
    ├── agent_evolver.py                # NEW — grow pool to 4096
    ├── news_fetcher.py                 # NEW — RSS polling → briefing .txt
    ├── incremental_update.py           # NEW — briefing → Neo4j MERGE
    ├── simulation_tick.py              # NEW — 500-agent LLM reaction pass
    ├── scheduler.py                    # NEW — APScheduler orchestrator
    ├── status.json                     # NEW (runtime) — scheduler status
    ├── tick_checkpoint.json            # NEW (runtime) — tick resume state
    └── seen_urls.json                  # NEW (runtime) — dedup URL hashes
```

**Files modified (existing):**
- `backend/app/services/oasis_profile_generator.py` — add `_sample_investor_traits()`

**Files not touched:**
- All API routes (`graph.py`, `simulation.py`, `report.py`)
- `Neo4jStorage`, `NERExtractor`, `EmbeddingService`, `LLMClient`
- Frontend
- `docker-compose.yml`

---

## Dependencies to Add

```
faiss-cpu
apscheduler
feedparser
beautifulsoup4
requests  # likely already present
```

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| RSS feeds change URL or go down | Each feed wrapped in try/except; briefing written even if feeds fail |
| Ollama saturation during tick (500 agents) | Sequential calls (not parallel); checkpoint recovery if interrupted |
| FAISS index diverges from Neo4j on crash | Evolver rebuilds FAISS from Neo4j on every startup (only the in-run update is in-memory) |
| Incremental update accidentally overwrites agent traits | `is_synthetic` guard on MERGE prevents property overwrite on synthetic agent nodes |
| Agent pool < 4096 when full tick runs | `simulation_tick.py` samples whatever agents exist — `--full` with LIMIT removed, not an error |
| Daily + hourly tick race condition | `threading.Event` flag blocks hourly tick while daily is running; daily job shifted to 03:00 |
| Agents only reacting to top-of-briefing news | Per-agent top-5 chunk selection via cosine similarity on `asset_class_bias` + `geopolitical_sensitivity` query |
| `seen_urls.json` growing indefinitely | 30-day TTL enforced on every fetch run; stored as `{hash: iso_timestamp}` |
