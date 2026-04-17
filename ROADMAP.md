# Quorum Roadmap — Revised (Code Audit + Realism Pass)

## Current State

The autonomous pipeline is fully functional. Every 30 minutes: fetch news → NER extract entities → sample 500 agents (stratified, min 5 per archetype), run per-agent LLM reaction tick with time-horizon gating → write SentimentSnapshot to Neo4j. Daily at 02:00 UTC: full 8,192-agent tick (~10 hours). Live dashboard shows trading signals, sentiment per asset class, agent reaction distribution, fear/greed, recent events, agent pool stats, cross-asset correlation matrix, source bias, market open indicator.

Infrastructure status:
- ✅ 8,192 agents with 16-trait taxonomy, archetype-conditional
- ✅ 30-min autonomous news fetch + NER + tick pipeline
- ✅ SentimentSnapshot per tick (tick-level history)
- ✅ Checkpoint recovery on crash
- ✅ Archetype-conditional prompt skeletons (ARCHETYPE_BEHAVIORS dict)
- ✅ Agent memory continuity (last 5 MemoryEvents injected)
- ✅ Backtester (per-asset, per-archetype, confidence calibration)
- ✅ Live dashboard with signal charts, entity feed, pool stats
- ✅ **All 16 traits injected into prompts** (TIER 1 complete — 2026-04-14)
- ✅ **Time-horizon gating** on 30-min tick (2026-04-14)
- ✅ **Stratified agent sampling** min 5 per archetype (2026-04-14)
- ✅ **MemoryEvent TTL pruning** weekly Sunday 04:00 UTC, 90-day window (2026-04-14)
- ✅ **Ollama health check** before each tick (2026-04-14)
- ✅ **Portfolio state tracking per agent** (TIER 2A — 2026-04-14)
- ✅ **Continuous conviction scoring** (TIER 2B — 2026-04-14)
- ✅ **Signal confidence decay** (TIER 2C — 2026-04-14, λ=0.05 ~14h half-life)
- ✅ **Conviction threshold for signal emission** (TIER 2D — 2026-04-14, 200-event minimum)
- ✅ **Drawdown tracking per asset class** (TIER 2D — 2026-04-14, live_state.json)
- ✅ **Volatility-aware signal thresholds** (TIER 2C-ii — 2026-04-14, dynamic per-asset)
- ✅ **Archetype signal decomposition** (TIER 2C-iii — 2026-04-14, /api/signals/archetype_split)
- ✅ **Panic contagion injection** (TIER 2D-i — 2026-04-14, contagion_flag.txt + herd gate)
- ✅ **Macro regime detection** (TIER 3 — 2026-04-14, price_fetcher + live/regime.json)
- ✅ **Daily scheduler moved to 02:00 UTC** (2026-04-16)
- ✅ **Market open/closed indicator** (2026-04-16, live_state.json + dashboard badges)
- ✅ **historyChart zoom/pan** (2026-04-16, Chart.js zoom plugin)
- ✅ **Token budget enforcement** (2026-04-16, TOKEN_BUDGET=4096 in news_fetcher.py)
- ✅ **price_staleness_hours in live_state** (2026-04-16, fast path in signals.py)
- ✅ **Source bias panel** (2026-04-16, 7d MemoryEvent source distribution in sidebar)
- ✅ **OpenBB VIX term structure** (2026-04-16, contango/backwardation/flat in regime.json)
- ✅ **OpenBB institutional flow** (2026-04-16, IEF/HYG → risk_on/risk_off/neutral in regime.json)
- ✅ **Economic + earnings calendar** (2026-04-16, backend/live/*.json + /api/live/calendar)
- ✅ **Capital/equal-weighted toggle fixed** (2026-04-16, _lastSentimentData cache)
- ✅ **Cross-asset correlation matrix** (2026-04-16, Pearson from 48 snapshots, heatmap panel)
- ✅ **Non-ASCII language guard** (2026-04-16, retry with English re-prompt in simulation_tick.py)
- ✅ **Nitter self-hosting documentation** (2026-04-16, Docker/compose guide in news_fetcher.py)
- ✅ **Reddit JSON API integration** (2026-04-16, 6 subreddits, min_score/flair gates, cap 15 articles)
- ✅ **Stocktwits integration** (2026-04-16, 7 symbols, follower gate, 5 msgs/symbol cap)
- ✅ **Embedding cache persistence** (2026-04-16, sha256 key, atomic pickle, 10-file prune)
- ✅ **Per-asset agent drilldown endpoint + modal** (2026-04-16, /api/signals/drilldown, dashboard modal)
- ✅ **Entity alias deduplication** (2026-04-16, 32-entry ENTITY_ALIASES, normalise_entity_name())
- ✅ **Token budget removed** (2026-04-16, unlimited briefing length)
- ❌ **Herding/contagion modelling** (deep cascade — TIER 4A, requires 30 days data)
- ❌ **Signal lag analysis** (leading vs coincident)

---

## Revised Priority Tier System

### TIER 1: ✅ DONE (2026-04-14)

**Inject all 16 traits into agent prompts** — complete.

`build_prompt()` now injects all 16 traits with exact spec mappings:
- `formative_crash` → gfc_2008 / dotcom_2000 / covid_2020 prose; "none" omitted entirely
- `loss_aversion_multiplier` → 4-tier labels (<0.5 / 0.5–1.5 / 1.5–3.5 / >3.5)
- `time_horizon_days` → 4-tier labels (<30d / 30–365d / 365–1095d / >1095d)
- `geopolitical_sensitivity` → only injected if ≥7
- `reaction_speed_minutes` → "within X min" / "over days to weeks, not hours"
- `overconfidence_bias` → 3-tier: <4 / 4–6 / ≥7 with descriptive text
- `leverage_typical` → injected if not "none" with margin pressure description
- `asset_class_bias` → "Your portfolio is concentrated in {asset}"

**Time-horizon gating** (TIER 1 extension) — complete.
30-min tick uses `participation_probability = min(1.0, 30.0 / reaction_speed_minutes)`.
Prop traders (5 min) always participate; pension funds (14400 min) participate ~0.2% of ticks.
Daily full tick bypasses gating — all sampled agents always run.

**Stratified agent sampling** (TIER 1 extension) — complete.
Each tick guarantees min(5, archetype_pool_size) agents per archetype before random fill.
Prevents pension_fund and hedge_fund from disappearing due to random sampling variance.

---

### TIER 2: ✅ DONE (2026-04-14)

#### 2A: Portfolio State Tracking — ✅ DONE

Agent nodes now have per-asset position state written lazily after each reaction:
- `position_{asset}` — "long" | "flat" (written on buy/sell/panic)
- `entry_price_{asset}` — float set on new long; null when flat
- `last_reaction_{asset}` — most recent reaction for that asset
- `last_reaction_timestamp` — ISO UTC timestamp of most recent reaction

In `simulation_tick.py`, before the LLM call:
- `load_agent_positions()` fetches all 7 assets in one query
- `format_positions_block()` builds P&L string injected into the system prompt
- If all flat: "no open positions — you are deciding whether to enter"

After writing MemoryEvent:
- `update_agent_positions()` writes all updates in a single batched SET
- buy → long + entry_price (only if currently flat); sell → flat; panic → ALL flat
- Uses MATCH not MERGE — never creates agent nodes

**Realism gain:** Disposition effect (hold losers too long) and anchoring (compare current to entry) are now grounded in real state.

#### 2B: Continuous Conviction Scoring — ✅ DONE

JSON output now includes `direction` (−1/0/+1), `conviction` (0–1), `position_size` (0–1).
`confidence` kept for backwards compatibility.

Sentiment scoring formula (24h, apply_leverage=True):
- New: `capital × conviction × position_size × leverage × decay`
  (direction carries the sign, weight is always positive)
- Legacy fallback for pre-TIER-2B events: `capital × (confidence/10) × 0.3 × leverage × decay`

**Realism gain:** A pension fund at conviction=0.9, position_size=0.02 moves less than a retail amateur at conviction=1.0, position_size=0.8 — even if the hedge fund has more capital. This matches real order flow.

---

### TIER 2C: SIGNAL QUALITY

- ✅ **Signal confidence decay** — exp(−0.05 × age_hours), λ=0.05 gives ~14h half-life within 24h window. Applied to both capital-weighted and equal-weighted 24h paths. 7d window unaffected.
- ✅ **Volatility-aware signal thresholds** — `max(0.25, min(0.6, 0.5 × std × √48))` per asset from last 48 SentimentSnapshots. Falls back to 0.4 if <10 snapshots. `dynamic_threshold` field added to each signal entry in `/api/signals/current`. (2026-04-14)
- [ ] **Cross-asset correlation warnings** — Pearson correlation matrix between asset sentiment series. Dashboard panel showing correlated asset pairs. When equities and crypto are >0.8 correlated, diversification signals are misleading.
- ✅ **Archetype signal decomposition endpoint** — `/api/signals/archetype_split?graph_id=...` returning smart money (hedge_fund + prop_trader) vs dumb money (retail_amateur) signal split per asset. Divergence > 0.3 = smart_leads_*; ≤0.15 = converging. (2026-04-14)

### TIER 2D: RISK

- ✅ **Panic contagion via MemoryEvent context injection** — when >50% of an archetype panicked in the last tick, write `backend/briefings/contagion_flag.txt`. Consumed at the start of the next tick and injected into agents with `herd_behaviour >= 7` only. (2026-04-14)
- ✅ **Drawdown tracking per asset class in live_state.json** — rolling 48-snapshot window; peak/current/drawdown_pct per asset. Written by dashboard_refresh.py. Requires ≥5 snapshots per asset.
- ✅ **Conviction threshold for signal emission** — minimum 200 agent events required before emitting bullish/bearish in `/api/signals/current`. Below threshold: signal="neutral", low_participation=true added to response.

### TIER 3: WEEK 3 (2 hours, market regimes)

**Add macro regime detection to `price_fetcher.py`**

Compute every price update:
- **Volatility:** 20-day realised volatility annualised. >30% = HIGH, 15–30% = ELEVATED, <15% = LOW
- **Trend:** MA-20 vs MA-50 vs current price. Current > MA-20 > MA-50 = BULL_TRENDING, etc.
- **Yield curve:** TLT vs SPY 5-day correlation. TLT up >1.5% and SPY down >1% = FLIGHT_TO_SAFETY. TLT down >1.5% = RISING_RATES.
- **Fear level:** Express as annualised volatility %

Example output:
```
[MARKET REGIME: HIGH_VOLATILITY (47% annualised) | BEAR_TRENDING | FLIGHT_TO_SAFETY]
```

Prepend this to every briefing file header in `news_fetcher.py`.

**Realism gain:** Agents immediately calibrate reactions to regime context. A GFC-scarred agent sees HIGH_VOLATILITY + BEAR_TRENDING + FLIGHT_TO_SAFETY and reacts defensively **even if the specific news is neutral**. A COVID-era agent in the same regime buys dips aggressively. This is empirically how investor behaviour changes across regimes.

---

### TIER 3B: DATA INTEGRITY

- [ ] **News source sentiment bias tracking** — each RSS feed has a measurable lean (e.g. Zerohedge is structurally bearish; AP Business is neutral). Track per-source reaction distributions over time and apply a small bias correction in `compute_sentiment_scores()`.
- ~~**Briefing length normalisation with token budget cap**~~ — superseded; TOKEN_BUDGET removed (2026-04-16). qwen2.5:14b 32k context + chunking handles all briefing sizes naturally.
- [ ] **`price_staleness_hours` propagation into signal API** — currently computed in `signals.py` at request time. Move into `live_state.json` via `dashboard_refresh.py` for consistency across endpoints and reduced per-request computation.

### TIER 4: AFTER 30 DAYS DATA (Backtesting maturity)

Once you have 30 days of price history (roughly mid-May 2026):

#### 4A: Contagion Pass

Add to end of `simulation_tick.py` after all MemoryEvents written:

```python
def run_contagion_pass(driver, tick_id, graph_id):
    """
    Find archetypes where >50% of agents panicked this tick.
    High-herd agents (herd_behaviour ≥7) in panic archetypes override
    their reaction to panic, simulating cascade propagation.
    """
    # Query: which archetypes had >50% panic rate?
    # Update: all high-herd agents in those archetypes → panic reaction
    # Set contagion_override=true so dashboard can distinguish real from cascade panic
```

**Realism gain:** Herding crashes don't happen because every agent panics simultaneously. They happen because high-herd agents copy the modal reaction of their archetype neighbourhood after a threshold is crossed. This is how 2008/2020 actually worked.

#### 4B: Complete Backtest Metrics

Add to `backtester.py`:

1. **Sharpe ratio** of mechanically following signals
2. **Max drawdown** of equity curve
3. **Signal lag accuracy** at 1h, 3h, 6h, 12h, 24h horizons
   - Reveals whether your signal is **leading** (predicts future) or **coincident** (noise tracking current price)

Most amateur sentiment models are coincident. If Quorum shows 55%+ accuracy at 6h lag, that's genuine signal.

---

### TIER 5: LONG TERM (Production hardening)

**Completed in this tier:**
- ✅ MemoryEvent TTL pruning — weekly Sunday 04:00 UTC, 90-day window, batched DETACH DELETE (2026-04-14)
- ✅ Ollama health check before each tick — verifies qwen2.5:14b loaded, 60s wait with 10s polls (2026-04-14)

**Remaining:**
- [ ] **OpenBB Platform** (github.com/OpenBB-finance/OpenBB) — evaluate as replacement/supplement for yfinance. Better data coverage, built-in fallback handling, institutional data sources, and no rate-limiting issues. Priority given recurring yfinance reliability problems.
- [ ] Self-hosted Nitter in Docker (Twitter/social sentiment feeds via RSS)
- [ ] WebSocket real-time updates to dashboard
- [ ] Agent trait evolution via `agent_evolver.py` scaffolding (allow agents to update traits based on MemoryEvent performance)
- ✅ **Named entity deduplication post-NER** — DONE (2026-04-16, ENTITY_ALIASES dict + normalise_entity_name() in incremental_update.py)
- [ ] Entity relationship decay (time-weight relevance for entity connections)
- [ ] Authentication + structured JSON logging
- [ ] Neo4j memory tuning guide for SentimentSnapshot accumulation at 30-min cadence
- ✅ **Embedding cache persistence to disk** — DONE (2026-04-16, briefing_cache/ dir, sha256 key, atomic pickle, 10-file prune)
- [ ] **Reaction diversity canary in scheduler_runs.json** — after the daily tick completes, run a condensed version of `verify_agent_diversity.py` check 8 and append `diversity_ok: bool` to the log entry. Visible signal if the pool collapses to unanimous reactions.

**UX extensions:**
- [ ] **Signal history CSV export endpoint** — `GET /api/signals/export?graph_id=...&days=30` returning a CSV for spreadsheet analysis.
- ✅ **Per-asset agent drill-down on dashboard** — DONE (2026-04-16, /api/signals/drilldown + modal with conviction bars, archetype badges, reasoning)
- [ ] **Entity-level alert triggers** — mention spike alert when an entity's mention count in the current tick exceeds 2× its rolling 7-day average. More useful than the current absolute-threshold alert chip.

---

## What to Remove / Mark Done

**Redundant or incorrectly named roadmap items:**

- ~~"Portfolio state tracking per agent"~~ → ✅ DONE (TIER 2A — 2026-04-14)
- ~~"Continuous conviction scoring"~~ → ✅ DONE (TIER 2B — 2026-04-14)
- ~~"Signal confidence decay"~~ → ✅ DONE (TIER 2C — 2026-04-14)
- ~~"Conviction threshold for signal emission"~~ → ✅ DONE (TIER 2D — 2026-04-14)
- ~~"Drawdown tracking per asset class"~~ → ✅ DONE (TIER 2D — 2026-04-14)
- ~~"Agent memory context injected into simulation tick prompts"~~ → ✅ DONE (TIER 1 complete)
- ~~"Archetype-conditional simulation tick prompts"~~ → ✅ DONE (`ARCHETYPE_BEHAVIORS` dict in simulation_tick.py)
- ~~"Reaction diversity audit tooling"~~ → ✅ DONE (`verify_agent_diversity.py` with 8 checks)
- ~~"Signal accuracy backtesting framework"~~ → ✅ DONE (`backtester.py`). **Note:** results are noise with <30 days data.
- ~~"Inject all 16 unused traits into agent prompts"~~ → ✅ DONE (2026-04-14, TIER 1)
- ~~"Time-horizon gating on tick participation"~~ → ✅ DONE (2026-04-14)
- ~~"Stratified agent sampling"~~ → ✅ DONE (2026-04-14)
- ~~"MemoryEvent TTL enforcement"~~ → ✅ DONE (2026-04-14, weekly pruning job)
- ~~"Ollama health check before tick"~~ → ✅ DONE (2026-04-14)

---

## Critical Realism Gaps This Roadmap Addresses

| Gap | Root Cause | How Tiers 1–4 Fix It |
|---|---|---|
| **Recency bias** | Agents don't know their formative crash | TIER 1: inject formative_crash with descriptions |
| **Disposition effect** | No position tracking; can't compute P&L | TIER 2A: add position state + entry price to Neo4j |
| **Anchoring** | No way to compare current to entry | TIER 2A: inject unrealised P&L into prompts |
| **Over/underconfidence** | Fixed confidence mapping; no calibration feedback | TIER 4B: confidence tiers reveal miscalibration in backtest |
| **Wrong order flow weights** | Fixed reaction scores (buy=+1) don't match real flow | TIER 2B: position_size amplifies retail vs institutions naturally |
| **Regime-blind reactions** | Agents don't know if volatility is high or low | TIER 3: prepend market regime to every briefing |
| **Herding crashes absent** | No cascade propagation modelling | TIER 4A: contagion pass when >50% of archetype panics |
| **Signal is coincident, not leading** | No lag analysis; don't know if signal leads price | TIER 4B: signal lag accuracy reveals leading vs noise |

---

## Implementation Timeline

| Week | Focus | Effort | Files modified |
|---|---|---|---|
| **This week** | TIER 1: Trait injection | 30 min | `simulation_tick.py` |
| **Next week** | TIER 2A: Position tracking | 2 hrs | `simulation_tick.py`, Neo4j schema |
| **Next week** | TIER 2B: Conviction scoring | 1 hr | `simulation_tick.py`, `signals.py` |
| **Week 3** | TIER 3: Regime detection | 2 hrs | `price_fetcher.py`, `news_fetcher.py` |
| **Week 4–5** | Data accumulation (30 days price history) | Passive | — |
| **Week 5** | TIER 4A: Contagion pass | 1.5 hrs | `simulation_tick.py` |
| **Week 5** | TIER 4B: Backtest metrics | 2 hrs | `backtester.py` |
| **Ongoing** | TIER 5: Production hardening | Variable | Various |

---

## Scheduler Change: Move Daily Tick to 8pm

**Current:** Daily full 8,192-agent tick runs at 03:00 UTC, finishes ~14:00 UTC (2pm), data stale during trading hours.

**Target:** Run at 20:00 UTC (8pm), finishes ~06:00 UTC next day (6am), data fresh before US market open (13:30 UTC / 8:30am EST).

**Implementation in `backend/scripts/scheduler.py`:**

Find the line defining the daily full tick job (search for `add_job` with `scheduler.every().day.at("03:00")`):

```python
# OLD:
scheduler.add_job(run_full_tick, 'cron', hour=3, minute=0, 
                  id='daily_full_tick', replace_existing=True)

# NEW:
scheduler.add_job(run_full_tick, 'cron', hour=20, minute=0, 
                  id='daily_full_tick', replace_existing=True)
```

Or if using APScheduler's simpler API:
```python
# OLD:
scheduler.add_job(run_full_tick, 'interval', days=1, start_date=...)

# NEW (with specific time):
from apscheduler.triggers.cron import CronTrigger
trigger = CronTrigger(hour=20, minute=0)
scheduler.add_job(run_full_tick, trigger, id='daily_full_tick', replace_existing=True)
```

**After TIER 1 implementation and initial testing, make this change and restart the scheduler.**

---

## Why This Order Matters

1. **TIER 1 is free.** Zero schema changes, no risk of breaking anything, produces immediate visible improvement in agent diversity. Do this today.

2. **TIER 2 requires TIER 1.** Position state is useless if the agent doesn't know their formative crash or loss aversion — they'll make identical decisions regardless. TIER 1 gives the context; TIER 2 gives the memory.

3. **TIER 3 amplifies TIER 1.** Regime detection makes the formative crash injection actually matter. A GFC agent sees HIGH_VOLATILITY and reacts defensively; a COVID agent buys dips. Without regime context, both types see the same briefing and lose differentiation.

4. **TIER 4 requires 30 days data.** You can't measure archetype alpha or signal lag with 5 days of history. But you can build the code now, so it runs correctly when data matures.

5. **TIER 5 is polish.** Only matters when TIER 1–4 are working and you're ready for production.

---

## Hardware Notes

Standard setup (32 GB RAM, 12–16 GB GPU VRAM, `qwen2.5:14b`):
- 500-agent tick: ~45–90 minutes
- 8,192-agent full tick: ~12–16 hours

TIER 1 + 2 changes will add ~10–15% overhead (more LLM tokens in prompts). Still well within daily window if you move the 03:00 job to 23:00 (as noted in earlier memory).

---

## Contributing

Contributions welcome for:
- TIER 1 trait injection (most impactful, lowest risk entry point)
- TIER 2A/B position tracking and conviction scoring
- TIER 3 regime detection enhancements (additional regimes beyond volatility/trend/yield curve)
- TIER 4 contagion and backtest metrics
- Self-hosted Nitter integration (TIER 5)

---

**Next step:** Implement TIER 1 this week. Harvey will see noticeably more diverse agent reactions immediately. Then reassess based on results.