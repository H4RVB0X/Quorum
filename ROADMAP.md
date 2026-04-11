# MiroFish-Offline Roadmap

## Current State

The autonomous pipeline is running. Every 30 minutes the system fetches financial news, merges entities into the Neo4j knowledge graph, runs a 500-agent LLM reaction tick, and fetches closing prices. At 03:00 daily it runs a full tick over all 8,192 agents. The live dashboard shows capital-weighted sentiment scores and trading signals per asset class.

Core infrastructure complete:
- 8,192 synthetic investor agents with 16 archetype-conditional traits
- SHA-256 URL deduplication with 30-day TTL
- Per-agent FAISS-free cosine similarity chunk retrieval
- Agent memory continuity (last 5 MemoryEvents injected per tick)
- Checkpoint recovery on crash
- Scheduler mutex preventing daily/hourly tick overlap
- Flask API: `/api/investors/sentiment`, `/api/signals/current`, `/api/signals/history`, `/api/investors/stats`, `/api/investors/agents`
- Live dashboard at `/dashboard`

---

## Near Term

### Data Sources

- [ ] **Twitter/Nitter RSS** — Add social media feeds as additional news sources for real-time retail sentiment signals
- [ ] **FT / Bloomberg RSS** — Broaden institutional news coverage (FT free RSS, Bloomberg markets RSS)
- [ ] **Central bank feeds** — ECB, Federal Reserve, Bank of England press release RSS feeds
- [ ] **News relevance filter** — Pre-filter briefing articles before graph ingestion; drop non-financial content (sports, entertainment) to reduce noise in the knowledge graph and tick context

### Signal Quality

- [ ] **Signal backtesting** — Compare each day's bullish/bearish signals against the actual next-day price move for each asset class. Track rolling accuracy, precision/recall per archetype and per signal direction
- [ ] **Confidence calibration** — Analyse whether agent `confidence` scores correlate with signal accuracy. High-confidence wrong signals may indicate specific archetype or prompt issues
- [ ] **Reaction diversity audit** — Check for LLM mode collapse: do all hedge fund agents tend to say `buy` on the same news regardless of trait differences? If diversity is low, trait injection into the prompt is not working as intended

### Simulation Quality

- [ ] **Archetype-conditional tick prompts** — Currently all agents receive the same prompt structure with traits injected as parameters. Pension fund and prop trader agents should receive structurally different prompts that model their genuinely different reasoning processes (liability-driven vs. momentum-driven)
- [ ] **Leverage-aware reaction weighting** — Agents using 10x leverage should have amplified signal weight on short-horizon reactions; currently leverage is a trait but doesn't affect the sentiment score calculation

---

## Mid Term

### Infrastructure

- [ ] **Docker volume mount for prices** — Mount `backend/prices/` as a named volume in `docker-compose.yml` so price files are shared between host and container without manual `docker cp` on first run
- [ ] **Scheduler inside Docker** — Option to run `scheduler.py` as a separate container service rather than as a host process, with proper volume mounts for `briefings/` and `prices/`
- [ ] **`/api/status` endpoint** — Expose scheduler state (last run times, agent pool size, last error) from `status.json` as a JSON API endpoint for the dashboard to consume
- [ ] **WebSocket updates** — Push new MemoryEvents and signal updates to the dashboard in real-time rather than polling every 30 seconds

### Agent System

- [ ] **Agent trait evolution** — Allow agents to update their own traits over time based on accumulated MemoryEvents (e.g. repeated panic reactions increase `loss_aversion_multiplier`). The experimental `agent_evolver.py` scaffolding exists; complete the update logic
- [ ] **Agent interaction graph** — Model information propagation between agents: high-herd agents should be more likely to adopt the reaction of capital-weighted neighbours
- [ ] **Cross-archetype contagion** — Simulate institutional panic spreading to retail (pension fund `panic` reaction raises `herd_behaviour` modifier for `retail_amateur` agents in the same tick)

### Knowledge Graph

- [ ] **Entity relationship decay** — Older relationships in the graph should carry lower weight. Implement time-weighted relevance so recent entity connections are prioritised in NER similarity searches
- [ ] **Graph-aware chunk retrieval** — When selecting top-k briefing chunks for an agent, boost chunks that mention entities already in the agent's recent MemoryEvents. Currently chunk selection is purely embedding-based
- [ ] **Named entity deduplication** — The NER pipeline can create near-duplicate entity nodes (e.g. "Federal Reserve" and "Fed"). Add a post-merge deduplication pass

---

## Long Term

### Backtesting and Analytics

- [ ] **Full backtesting harness** — Replay historical briefings against the agent pool with known subsequent prices. Report per-archetype signal accuracy, optimal sentiment threshold calibration, and Sharpe ratio of hypothetical signal-following strategies
- [ ] **Archetype alpha analysis** — Do hedge fund agents produce more accurate signals than retail amateurs? Quantify per-archetype predictive value
- [ ] **Market event annotation** — Tag briefing files with known market events (earnings, central bank decisions, geopolitical events) and analyse how different archetypes respond by event type

### Production Hardening

- [ ] **Authentication** — Add basic auth or API key protection to the Flask API before any external exposure
- [ ] **Structured logging** — Replace the current log files with structured JSON output for log aggregation (Loki, Datadog, etc.)
- [ ] **Neo4j memory tuning guide** — Document Neo4j heap and page cache sizing for different agent pool sizes and MemoryEvent accumulation rates
- [ ] **Comprehensive test suite** — Unit tests for sentiment weighting, signal threshold logic, and the `is_synthetic` guard; integration tests that hit a real Neo4j instance

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

- News relevance filtering (reduces wasted LLM calls on non-financial content)
- Signal backtesting (the most valuable missing feature)
- Additional RSS sources with good financial coverage
- Archetype-conditional prompt templates
- Agent diversity audit tooling

See [GitHub Issues](https://github.com/nikmcfly/MiroFish-Offline/issues) for current tasks.
