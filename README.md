# Quorum

**A local-first financial market simulation engine powered by a live knowledge graph and 8,192 synthetic investor agents.**

Quorum continuously ingests real financial news, builds a live knowledge graph from it, and simulates how a diverse pool of synthetic investors would respond — generating weighted sentiment signals and trading indicators across seven asset classes. Everything runs on local hardware with no cloud dependencies beyond RSS feeds and market price data.

---

## Background

This project is built on top of [MiroFish-Offline](https://github.com/nikmcfly/MiroFish-Offline), a local infrastructure fork of [MiroFish](https://github.com/666ghj/MiroFish) by 666ghj. MiroFish-Offline replaced the original's cloud dependencies (Zep Cloud, DashScope) with local Neo4j and Ollama. Quorum extends that foundation into a full financial market simulation with a custom agent system, autonomous pipeline, sentiment API, and live trading dashboard.

---

## What It Does

Every 30 minutes, an autonomous pipeline:

1. Fetches headlines from Reuters, Yahoo Finance, CNBC, and MarketWatch
2. Extracts named entities via spaCy (`en_core_web_sm`) and merges them into a Neo4j knowledge graph
3. Samples 500 agents, retrieves the most relevant news chunks for each via cosine similarity, and runs a per-agent LLM call to generate a `buy / sell / hold / panic / hedge` reaction in character
4. Writes each reaction as a `MemoryEvent` node in Neo4j, linked to the agent
5. Fetches daily closing prices for 7 asset-class proxies

A daily job at 20:00 (Europe/London scheduler time) runs the full tick across all 8,192 agents.

On startup and after every job completes, the scheduler runs a catch-up check: if the daily job hasn't run today (UTC) and it is past 20:00 UTC, it fires immediately; if the last half-hourly tick finished more than 30 minutes ago (or no record exists), it fires immediately. Daily always takes priority. Every job completion — success, error, or `model_unavailable` — is appended to `backend/logs/scheduler_runs.json` as a permanent audit trail (capped at 10,000 entries).

The resulting sentiment scores — weighted by agent capital and conviction — feed a live dashboard showing trading signals, sentiment charts, agent distributions, and a real-time memory event feed.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        scheduler.py                              │
│  APScheduler — 30-min news+tick job, 20:00 daily full run       │
│  Catch-up on startup + after each job · run log in backend/logs │
└──────┬─────────────────────┬───────────────────────┬────────────┘
       │                     │                       │
  news_fetcher.py    incremental_update.py    simulation_tick.py
  (RSS → briefing)   (spaCy NER → Neo4j)      (LLM per-agent)
       │                     │                       │
       └─────────────────────┴───────────────────────┘
                             │
                     ┌───────▼────────┐         ┌──────────────┐
                     │   Neo4j CE     │         │ price_fetcher │
                     │  Entity nodes  │         │  (yfinance)  │
                     │  MemoryEvents  │         └──────────────┘
                     └───────┬────────┘
                             │
  ┌──────────────────────────┴───────────────────────────────────┐
  │                                                              │
  ▼                                                              ▼
┌─────────────────────────────────┐   ┌──────────────────────────────────┐
│         Flask API               │   │      dashboard_refresh.py        │
│  /api/investors/sentiment       │   │  Separate process — 15-min write │
│  /api/signals/current           │   │  Queries Neo4j + yfinance        │
│  /api/signals/history           │   │  Writes backend/live/            │
│  /api/signals/backtest          │   │    live_state.json               │
│  /api/signals/sentiment_history │   │    price_sentiment_history.json  │
│  /api/signals/archetype_split   │   └──────────────┬───────────────────┘
│  /api/investors/stats           │                  │ bind mount
│  /api/investors/agents          │   ┌──────────────▼───────────────────┐
│  /api/live/state  (file read)   │   │  backend/live/  ← volume mount   │
│  /api/live/history (file read)  │   │  ./backend/live:/app/backend/live│
│  /api/live/regime (file read)   │   └──────────────────────────────────┘
│  /dashboard                     │
└─────────────────────────────────┘
```

---

## Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Web framework | Flask + Flask-CORS |
| Graph database | Neo4j Community Edition 5.18 |
| LLM inference | Ollama (`qwen2.5:14b` default) |
| Embeddings | Ollama (`nomic-embed-text`, 768-dimensional) |
| Vector similarity | NumPy cosine similarity |
| Scheduler | APScheduler (BlockingScheduler) |
| NER | spaCy `en_core_web_sm` |
| News ingestion | feedparser + requests + BeautifulSoup (requests used as a pre-fetch wrapper for feeds that require timeout enforcement, since feedparser has no native timeout API) |
| Market prices | yfinance |
| Frontend | Custom HTML dashboard served by Flask |
| Containerisation | Docker Compose |
| Package manager | uv |

---

## The 8,192 Agent System

Agents are generated by `backend/scripts/generate_agents.py` using archetype-conditional trait sampling. The LLM generates names and backstories — all 16 numeric traits are sampled from statistical distributions calibrated against real market research (Preqin, Broadridge, SIFMA AUM data).

### Pool Composition

| Archetype | Share | Count | Real-world basis |
|---|---|---|---|
| `retail_amateur` | 50% | ~4,096 | Retail investors, ISA/brokerage accounts |
| `retail_experienced` | 20% | ~1,638 | Self-directed investors, multiple cycles |
| `prop_trader` | 8% | ~655 | Proprietary trading desks, ex-bank traders |
| `fund_manager` | 8% | ~655 | Long-only equity funds, CFA charterholders |
| `hedge_fund` | 6% | ~491 | Global macro, quant, event-driven |
| `family_office` | 5% | ~410 | UHNW single/multi-family offices |
| `pension_fund` | 3% | ~246 | National, corporate, university endowments |

### The 16 Per-Agent Traits

| Trait | Type | Description |
|---|---|---|
| `risk_tolerance` | float 0–10 | Beta-sampled; pension funds skew 1–2, prop traders 7–9 |
| `herd_behaviour` | float 0–10 | Tendency to follow consensus; hedge funds near-zero |
| `news_sensitivity` | float 0–10 | Reactivity to news events |
| `geopolitical_sensitivity` | float 0–10 | Weighting of geopolitical risk |
| `overconfidence_bias` | float 0–10 | Confidence above rational baseline |
| `capital_usd` | float | Log-uniform; retail $200–$50k, pension $1B–$100B |
| `time_horizon_days` | int | Pareto-sampled; prop traders 1–90, pensions 1825–3650 |
| `loss_aversion_multiplier` | float | Lognormal; pension funds 1.2–8.0, hedge funds 0.2–2.5 |
| `reaction_speed_minutes` | float | Lognormal; prop traders 1–120min, pensions days–weeks |
| `investor_archetype` | string | One of the 7 archetypes above |
| `primary_strategy` | string | index / growth / value / momentum / quant / macro / etc. |
| `leverage_typical` | string | none / 2x / 5x / 10x_plus |
| `formative_crash` | string | none / gfc_2008 / dotcom / covid_2020 |
| `fear_greed_dominant` | string | fear / greed |
| `asset_class_bias` | string | equities / bonds / crypto / real_estate / commodities / fx / mixed |
| `is_synthetic` | bool | Always `true`; guards against news NER overwriting agent nodes |

Agents are bulk-inserted in batches of 500 using Cypher `UNWIND`. A fixed RNG seed makes numeric distributions reproducible; names and backstories vary each run.

---

## The Autonomous Pipeline

### News Fetching

Polls three tiers of feeds every 30 minutes:

- **General RSS** (relevance-filtered): Reuters, Yahoo Finance, CNBC Markets, MarketWatch, FT Markets, Benzinga, Seeking Alpha, AP Business
- **Nitter RSS** (relevance-filtered; toggled by `NITTER_ENABLED`): 10 curated financial Twitter accounts via nitter.net — `@unusual_whales`, `@DeItaone`, `@financialjuice`, `@WallStreetSilv`, `@zerohedge`, `@markets`, `@bespokeinvest`, `@elerianm`, `@NorthmanTrader`, `@ReformedBroker`. **Currently disabled** (`NITTER_ENABLED = False`) — public nitter.net instances are unreliable. Re-enable when self-hosted Nitter is running in Docker.
- **Central bank** (never filtered; tagged `[CENTRAL BANK]` in briefing): Federal Reserve, ECB, Bank of England press releases

Benzinga and Seeking Alpha are fetched with a **5-second timeout** enforced via `requests.get()` — feedparser has no native timeout API, so a `requests` pre-fetch is used as a workaround. On timeout, a WARNING is logged and that feed is skipped without blocking the rest of the run. All other feeds use feedparser's default fetch.

Each article URL is SHA-256 hashed and checked against a deduplication store (30-day TTL). New articles are fetched and scraped via BeautifulSoup. General and Nitter articles are passed through a keyword-whitelist relevance filter (`news_relevance_filter.py`) before being written to the briefing; central bank articles bypass the filter entirely. The result is written as a timestamped briefing file.

### Knowledge Graph Update

Chunks each briefing and runs spaCy (`en_core_web_sm`) NER to extract named entities — organisations, people, locations, financial figures, and more — then batch-MERGEs them into Neo4j via `UNWIND` in batches of 500. No LLM calls are made during this step; the full pipeline completes in under 2 minutes regardless of briefing length. A `CASE WHEN is_synthetic` guard in the Cypher prevents news-extracted entities from ever overwriting synthetic agent node properties — so a news article mentioning "Tesla" can't corrupt an agent node that happens to share a lowercased name.

Before writing to Neo4j, three noise filters are applied:
- **`ENTITY_REMOVE_LIST`** — financial metrics (EBITDA, GDP, CPI…), role acronyms (CEO, CFO…), ticker symbols (SPY, QQQ…), and FX/crypto codes (USD, EUR, BTC…) are dropped entirely
- **Minimum name length** — names shorter than 3 characters are discarded
- **Single-token Person names** — Person entities with no space in the name (e.g. "Matt" without a surname) are dropped as unreliable partial matches

### Sentiment Snapshots

After all agents in a tick are processed, `simulation_tick.py` writes a `SentimentSnapshot` node to Neo4j containing:
- Exact tick timestamp
- Per-asset capital-weighted sentiment scores
- Reaction distribution (% buy/sell/hold/hedge/panic)
- Total agents processed
- Fear/greed score (0–100, where 50 = neutral)

These snapshots power the tick-level chart and reaction distribution chart on the dashboard. At 30-min cadence, 48 hours = ~96 data points.

### Simulation Tick

For each sampled agent:

1. Briefing chunks are embedded once with `nomic-embed-text` and cached in memory for the run
2. **Stratified sampling** guarantees min 5 agents per archetype before random fill to `SAMPLE_SIZE=500`
3. **Time-horizon gating** (30-min tick only): `participation_prob = min(1.0, 30 / reaction_speed_minutes)`. Prop traders (5 min) always participate; pension funds (14,400 min) ~0.2% probability.
4. The agent's last 5 `MemoryEvent` nodes are fetched and injected into the system prompt
5. **All 16 traits** are injected into the persona block with natural-language descriptions — formative crash experience, loss aversion tier, time horizon label, reaction speed, overconfidence tier, geopolitical sensitivity (only if ≥7), leverage pressure, and portfolio concentration
6. A relevance query is built from `asset_class_bias` + `primary_strategy`; cosine similarity selects the 5 most relevant chunks
7. The LLM produces a JSON reaction: `{reaction, confidence, reasoning, assets_mentioned}`
8. A `MemoryEvent` node is written to Neo4j and linked to the agent

If the tick crashes mid-run, a checkpoint file records processed UUIDs so a restart resumes where it left off.

The scheduler pre-checks Ollama availability (GET `/api/tags`, 1-token probe, 60s wait) before starting any tick. If Ollama is unavailable, the tick is skipped and `model_unavailable` is logged.

**MemoryEvent TTL pruning:** Weekly Sunday 04:00 UTC job deletes MemoryEvents older than 90 days (batched at 10,000 nodes). At 500 agents × 48 ticks/day, ~24,000 nodes are written daily — pruning is critical for Neo4j CE longevity.

### Price Fetching

Daily closing prices for 7 asset-class proxies via yfinance. Reliability improvements:
- Network pre-check (HEAD `https://finance.yahoo.com`) before any yfinance calls
- Per-ticker retry: up to 3 attempts with 5-second delays; empty data triggers retry
- Per-ticker WARNING logging with ticker, asset name, error type, and attempt number

| Asset class | Proxy |
|---|---|
| Equities | SPY |
| Crypto | BTC-USD |
| Bonds | TLT |
| Commodities | GLD |
| FX | DX-Y.NYB |
| Mixed | VT |
| Real estate | VNQ |

---

## Sentiment and Signal System

### Sentiment Scoring

All `MemoryEvent` nodes from the last 24h and 7 days are queried. The 24h path is conviction-aware and time-decayed; the 7d path is intentionally simpler.

- **24h path (primary):** if event fields include `direction`, `conviction`, and `position_size`, score = `direction`, and weight = `capital_usd × conviction × position_size × leverage × decay`.
- **Legacy fallback:** score uses reaction mapping and weight = `capital_usd × (confidence/10) × 0.3 × leverage × decay`.
- **7d path:** no leverage multiplier and no decay (intentional asymmetry).
- **Equal-weighted path:** confidence/conviction-weighted scores with capital ignored.

Legacy reaction mapping (fallback path):

| Reaction | Score |
|---|---|
| buy | +1.0 |
| hedge | +0.5 |
| hold | 0.0 |
| sell | −1.0 |
| panic | −1.0 |

The capital-weighted average per asset class is clipped to `[−1, +1]`.

### Trading Signals

24h sentiment scores are combined with the latest price snapshot. Thresholds are volatility-aware per asset:

- `threshold = clamp(0.5 × std × sqrt(48), 0.25, 0.55)` from recent `SentimentSnapshot` history
- fallback threshold `0.4` when there are fewer than 10 snapshots
- if `event_count < 200`, signal is forced to `neutral` (`low_participation=true`)

| Sentiment score | Signal |
|---|---|
| > +threshold | bullish |
| < −threshold | bearish |
| between | neutral |

Also returns `price_change_24h` (% vs previous day's close), `dynamic_threshold`, and `data_quality` per asset class.

---

## The Dashboard

Served at `/dashboard` by Flask. Single self-contained HTML file — all CSS and JS inline, CDN imports only (Google Fonts, Chart.js, chartjs-plugin-zoom). Terminal-style dark aesthetic (Inter + IBM Plex Mono, deep navy with cyan/green/purple accents).

### Layout

**Header** — logo, 7 market-state dots (green/red/amber per asset), status, last tick age, live timestamp, refresh button.

**Signal strip** — horizontal row of 7 asset tiles, each showing: signal badge, live price, 24h change, sentiment score, 2px coloured indicator bar. Horizontal-scrolls on mobile.

**Sidebar + Main split** (desktop flex, column on mobile):
- Sidebar (260px sticky): pool stats (agents, events, avg risk/capital/sensitivity/loss-aversion), fear/greed gauge + 48-tick sparkline, top entities list, article sources bar chart
- Main: tick sentiment chart, reaction distribution chart, sentiment vs price history, 2×2 distribution charts, memory event feed

**Agent table** — full-width below the split; archetype filter tabs, 200 agents.

### Features

- **Alert strip** — auto-detects sentiment shift ≥±0.25 in a 2h window; shows dismissable chips. Session-deduped.
- **Tick-level sentiment chart** — per-asset sentiment from `SentimentSnapshot` nodes; capital-weighted / equal-weighted toggle; 48h / 30d window; scroll/pinch zoom + pan
- **Reaction distribution** — stacked area chart of buy/sell/hold/hedge/panic % over time; zoom/pan
- **Sentiment vs Price History** — dual-axis chart (sentiment + price Δ% from window start) at 15-min resolution, powered by `price_sentiment_history.json`. Fills in as `dashboard_refresh.py` accumulates data. Refreshes on every 15-min live state update.
- **Distributions** — archetype, risk histogram, strategy, asset bias bar charts
- **Fear/greed gauge** — pool trait distribution dial
- **Memory event feed** — 20 most recent agent reactions with archetype, confidence, reasoning
- **Top entities** — top 10 non-synthetic entities from NER with mention count
- **Feed breakdown** — article counts per news source (from `_sources.json` sidecar)
- **Prices stale badge** — shown when yfinance fails; auto-hides when live prices return
- **Header health indicators** — tick age ("14m ago") and live state timestamp for remote monitoring

Auto-refreshes every 30 seconds (full API). 15-minute live state overlay from `dashboard_refresh.py`.

---

## Project Structure

```
Quorum/
├── docker-compose.yml
├── Dockerfile
├── backend/
│   ├── run.py                        # Flask entry point
│   ├── app/
│   │   ├── api/
│   │   │   ├── investors.py          # /api/investors/* — stats, agents, sentiment
│   │   │   ├── signals.py            # /api/signals/* — current signals, history
│   │   │   ├── live.py               # /api/live/* — serves file-backed live state/history/regime (no Neo4j)
│   │   │   ├── graph.py              # Knowledge graph API routes
│   │   │   ├── simulation.py         # OASIS simulation routes
│   │   │   └── control.py            # Pipeline control endpoints
│   │   ├── storage/
│   │   │   ├── neo4j_storage.py      # Neo4j implementation (hybrid search: 0.7 vector + 0.3 BM25)
│   │   │   ├── embedding_service.py  # nomic-embed-text via Ollama
│   │   │   └── ner_extractor.py      # LLM-based NER (superseded by spaCy in incremental_update.py)
│   │   └── services/
│   │       ├── simulation_runner.py
│   │       ├── graph_builder.py
│   │       └── report_agent.py
│   ├── scripts/
│   │   ├── generate_agents.py        # Generate 8,192 synthetic investor agents
│   │   ├── news_fetcher.py           # RSS polling + briefing writer + sources sidecar
│   │   ├── incremental_update.py     # NER extraction + Neo4j MERGE
│   │   ├── simulation_tick.py        # Per-agent LLM reaction pass + SentimentSnapshot writer
│   │   ├── price_fetcher.py          # yfinance daily price snapshot
│   │   ├── backtester.py             # Signal accuracy backtest (per-asset, per-archetype, calibration)
│   │   ├── verify_agent_diversity.py # 8-check diversity audit (incl. reaction diversity Check 8)
│   │   ├── scheduler.py              # APScheduler orchestrator (simulation pipeline)
│   │   └── dashboard_refresh.py      # Separate process — writes live_state + history every 15 min
│   ├── briefings/                    # Timestamped news briefing files
│   ├── logs/                         # scheduler_runs.json — append-only job audit log
│   ├── prices/                       # Daily price snapshots (JSON)
│   ├── live/                         # File-backed live dashboard data
│   │   ├── live_state.json           # Current live dashboard state
│   │   ├── price_sentiment_history.json
│   │   └── regime.json               # Current market regime snapshot
│   └── tests/
│       ├── test_sentiment.py
│       └── test_traits.py
└── frontend/
    └── public/
        └── dashboard.html            # Live dashboard
```

---

## Credits

Built on [MiroFish-Offline](https://github.com/nikmcfly/MiroFish-Offline), which is itself a local-infrastructure fork of [MiroFish](https://github.com/666ghj/MiroFish) by [666ghj](https://github.com/666ghj), originally supported by Shanda Group. The simulation engine foundation is [OASIS](https://github.com/camel-ai/oasis) from the CAMEL-AI team.

## License

AGPL-3.0 — inherited from the original MiroFish project. See [LICENSE](./LICENSE).