# MiroFish-Offline Roadmap

## Current State

The autonomous pipeline is running. Every 30 minutes the system fetches financial news, merges entities into the Neo4j knowledge graph, runs a 500-agent LLM reaction tick, writes a SentimentSnapshot to Neo4j, and fetches closing prices. At 03:00 daily it runs a full tick over all 8,192 agents. The live dashboard shows tick-level sentiment, trading signals, reaction distribution over time, fear/greed sparkline, top entities, and per-feed article volume.

Core infrastructure complete:
- 8,192 synthetic investor agents with 16 archetype-conditional traits
- SHA-256 URL deduplication with 30-day TTL
- Per-agent FAISS-free cosine similarity chunk retrieval
- Agent memory continuity (last 5 MemoryEvents injected per tick)
- Checkpoint recovery on crash
- Scheduler mutex preventing daily/hourly tick overlap + catch-up on startup
- News relevance filter (whitelist-based, FINANCIAL_TERMS in news_relevance_filter.py)
- Active sources: Reuters, Yahoo Finance, CNBC, MarketWatch, FT Markets, Benzinga, Seeking Alpha, AP Business, ECB, BoE, Fed
- spaCy NER pipeline (en_core_web_sm) with entity type corrections, noise filters, mention counting
- Archetype-conditional tick prompts (hedge/panic definitions, English enforcement, reaction skew fixed)
- Agent reaction diversity audit (verify_agent_diversity.py, 8 checks including Check 8)
- Signal accuracy backtest (backtester.py, per-asset/per-archetype/calibration, powers `/api/signals/backtest`)
- Flask API: `/api/investors/sentiment`, `/api/signals/current`, `/api/signals/history`,
  `/api/signals/backtest`, `/api/signals/sentiment_history`, `/api/investors/stats`, `/api/investors/agents`,
  `/api/live/state`, `/api/live/history`
- `dashboard_refresh.py` — separate 15-min process writing `live_state.json` + `price_sentiment_history.json`
- Live dashboard at `/dashboard` — sidebar+main layout, signal strip, tick charts, reaction distribution, fear/greed gauge, entities, feed breakdown, 15-min sentiment+price history chart

---

## Near Term

### Data Sources

- [ ] **Self-hosted Nitter in Docker** — Twitter/social sentiment feeds. `NITTER_FEEDS` list is preserved in `news_fetcher.py`; set `NITTER_ENABLED = True` when a self-hosted Nitter container is running.

### Signal Quality

- [ ] **Confidence bands on sentiment chart** — Add ±1σ shading around tick-level sentiment lines to show signal confidence range across agents.
- [ ] **Archetype alpha analysis** — Do hedge fund agents produce more accurate signals than retail amateurs? Quantify per-archetype predictive value once backtest has 30+ trading days of history.

### Simulation Quality

- [ ] **Agent archetype drill-down in memory events feed** — Click an archetype badge in the event feed to filter the full feed to that archetype only.
- [ ] **Entity-level memory in tick prompts** — Currently last 5 reactions are injected. Consider injecting entity-level memory: "last time SPY was mentioned, you bought."

---

## Mid Term

### Infrastructure

- [ ] **Docker volume mount for prices** — Mount `backend/prices/` as a named volume in `docker-compose.yml` so price files are shared between host and container without manual `docker cp`.
- [ ] **Scheduler inside Docker** — Option to run `scheduler.py` as a separate container service rather than as a host process.
- [ ] **`/api/status` endpoint** — Expose scheduler state (last run times, agent pool size, last error) from `status.json` as a JSON API endpoint.
- [ ] **WebSocket updates** — Push new MemoryEvents and signal updates to the dashboard in real-time.

### Agent System

- [ ] **Agent trait evolution** — Allow agents to update their own traits over time based on accumulated MemoryEvents. The experimental `agent_evolver.py` scaffolding exists.
- [ ] **Agent interaction graph** — Model information propagation between agents: high-herd agents adopt the reaction of capital-weighted neighbours.
- [ ] **Cross-archetype contagion** — Simulate institutional panic spreading to retail.

### Knowledge Graph

- [ ] **Entity relationship decay** — Time-weighted relevance for entity connections.
- [ ] **Graph-aware chunk retrieval** — Boost chunks that mention entities in agent's recent MemoryEvents.
- [ ] **Named entity deduplication** — Post-merge pass to deduplicate "Federal Reserve" / "Fed".

---

## Long Term

### Backtesting and Analytics

- [ ] **Full backtesting harness** — Replay historical briefings against the agent pool with known subsequent prices.
- [ ] **Market event annotation** — Tag briefing files with known market events (earnings, central bank decisions, geopolitical events).

### Production Hardening

- [ ] **Authentication** — Add basic auth or API key protection to the Flask API before any external exposure.
- [ ] **Structured logging** — Replace the current log files with structured JSON output.
- [ ] **Neo4j memory tuning guide** — Document Neo4j heap and page cache sizing for SentimentSnapshot accumulation at 30-min cadence.
- [ ] **Comprehensive test suite** — Unit tests for sentiment weighting, signal threshold logic, backtester logic, leverage multiplier asymmetry.

---

## Hardware Tiers

| Tier | RAM | GPU VRAM | Recommended Model | 500-agent tick time |
|---|---|---|---|---|
| Minimum | 16 GB | — (CPU) | `qwen2.5:7b` | Several hours |
| Standard | 32 GB | 12–16 GB | `qwen2.5:14b-instruct-q8_0` | 45–90 minutes |
| Power | 64 GB | 24+ GB | `qwen2.5:32b` | 20–40 minutes |

Full 8,192-agent daily run scales linearly from the 500-agent sample time.

---

## Contributing

AGPL-3.0 licensed. Contributions welcome, especially:

- Self-hosted Nitter in Docker (Twitter social sentiment)
- Confidence bands on sentiment chart (±1σ shading on tick-level chart)
- Archetype alpha analysis (per-archetype signal accuracy once 30+ days of backtest history accumulates)
- Agent trait evolution via `agent_evolver.py` scaffolding

See [GitHub Issues](https://github.com/nikmcfly/MiroFish-Offline/issues) for current tasks.
