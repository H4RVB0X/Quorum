# Quorum Roadmap — Revised (Code Audit + Realism Pass)

## Current State

The autonomous pipeline is fully functional. Every 30 minutes: fetch news → NER extract entities → sample 500 agents, run per-agent LLM reaction tick → write SentimentSnapshot to Neo4j. Daily at 03:00: full 8,192-agent tick (11 hours, ~0.12 agents/sec). Live dashboard shows trading signals, sentiment per asset class, agent reaction distribution, fear/greed, recent events, agent pool stats.

**Critical finding from code audit:** The system stores 16 traits per agent but only injects 6 into prompts. Traits like `formative_crash`, `loss_aversion_multiplier`, `time_horizon_days`, `geopolitical_sensitivity`, `reaction_speed_minutes` are completely wasted. Agents are **stateless** (no position tracking, no memory of P&L). Backtest infrastructure exists but has only 5 days of price history (minimum 30 days needed for meaningful archetype alpha, 90+ for regime detection).

Infrastructure status:
- ✅ 8,192 agents with 16-trait taxonomy, archetype-conditional
- ✅ 30-min autonomous news fetch + NER + tick pipeline
- ✅ SentimentSnapshot per tick (tick-level history)
- ✅ Checkpoint recovery on crash
- ✅ Archetype-conditional prompt skeletons (ARCHETYPE_BEHAVIORS dict)
- ✅ Agent memory continuity (last 5 MemoryEvents injected)
- ✅ Backtester (per-asset, per-archetype, confidence calibration)
- ✅ Live dashboard with signal charts, entity feed, pool stats
- ❌ **Portfolio state tracking per agent**
- ❌ **All 16 traits injected into prompts**
- ❌ **Macro regime detection**
- ❌ **Continuous conviction scoring** (currently fixed reaction categories)
- ❌ **Herding/contagion modelling**
- ❌ **Signal lag analysis** (leading vs coincident)

---

## Revised Priority Tier System

### TIER 1: THIS WEEK (30 mins, zero risk, immediate ROI)

**Inject all 16 unused traits into agent prompts**

Currently `build_prompt()` passes only: name, archetype, capital, risk_tolerance, strategy, fear_greed_dominant, herd_behaviour, news_sensitivity.

**Add these immediately:**
- `formative_crash` → mapped to English (GFC: "acutely sensitive to credit risk", COVID: "learned that aggressive buys into panics work", dotcom: "distrust narrative valuations", none: "no formative crash")
- `loss_aversion_multiplier` → tier labels ("unusually low (0.2x)", "moderate (1.5x)", "high (3.5x)", "extreme (7x)")
- `time_horizon_days` → English label ("intraday-to-weeks", "months", "years-to-decades")
- `geopolitical_sensitivity` → if ≥7, add "acute geopolitical sensitivity — rate hike news means less to you than sanctions, regional conflict, or trade war escalation"
- `reaction_speed_minutes` → "reacts within X minutes of news"
- `overconfidence_bias` → "confidence bias {value}/10"

**Implementation:** ~50 lines in `simulation_tick.py` `build_prompt()` function. No schema changes. This alone produces noticeably more agent diversity because the LLM now has the full behavioural fingerprint.

**Realism gain:** GFC-scarred pension fund with extreme loss aversion and 10-year horizon reacts to "Fed signals rate hike" completely differently from COVID-era prop trader with low loss aversion and 30-day horizon. As it should.

---

### TIER 2: WEEK 2 (4–6 hours, core realism)

#### 2A: Portfolio State Tracking (2 hours)

Add to Neo4j schema (agent nodes):
```cypher
position_{asset_class}     // "long" | "short" | "flat"
entry_price_{asset_class}  // float, null if flat
last_reaction_{asset_class} // "buy" | "sell" | "hold" | "panic" | "hedge"
last_reaction_timestamp    // ISO timestamp
```

In `simulation_tick.py`, before LLM call:
- Fetch agent's current positions (SPY, BTC, TLT, etc.)
- Compute unrealised P&L per asset
- Inject into prompt: "Current positions: SPY LONG (entered $380, now $385, +1.3% P&L), BTC SHORT (entered $42k, now $43.5k, -3.6% P&L), TLT FLAT"

After writing MemoryEvent:
- Update `position_{asset}` based on reaction (buy→long, sell→flat, hold→no change)
- Store entry_price for long/short

**Realism gain:** Enables **disposition effect** (hold losers too long) and **anchoring** (compare current to entry, not to fair value). Empirically the two most documented sources of real investor behaviour divergence.

#### 2B: Continuous Conviction Scoring (1 hour)

Replace fixed reaction categories with continuous scores.

**Current prompt:**
```
Return JSON: {"reaction": "buy|hold|sell|panic|hedge", "confidence": <0-10 float>, ...}
```

**New prompt:**
```
Return JSON with exact fields:
{
  "reaction": "buy|hold|sell|panic|hedge",
  "direction": 1 or 0 or -1,        # 1=bullish, 0=neutral, -1=bearish
  "conviction": <float 0.0 to 1.0>, # your certainty
  "position_size": <0.0 to 1.0>,    # fraction of capital you'd allocate
  "reasoning": "<1-2 sentences>",
  "assets_mentioned": [...]
}
```

**Aggregation change** in sentiment scoring:
- Old: `capital_usd × fixed_reaction_score` (buy=+1, sell=-1, hold=0)
- New: `capital_usd × direction × conviction × position_size`

**Realism gain:** Position_size acts as natural amplifier. A hedge fund at direction=1, conviction=0.9, position_size=0.05 (small bet) moves the needle less than a retail amateur at direction=1, conviction=1.0, position_size=0.8 (betting big), even though the hedge fund has more capital. This matches real order flow.

---

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

- [ ] Self-hosted Nitter in Docker (Twitter/social sentiment feeds via RSS)
- [ ] WebSocket real-time updates to dashboard
- [ ] Agent trait evolution via `agent_evolver.py` scaffolding (allow agents to update traits based on MemoryEvent performance)
- [ ] Named entity deduplication post-NER ("Federal Reserve" / "Fed" → single entity)
- [ ] Entity relationship decay (time-weight relevance for entity connections)
- [ ] Authentication + structured JSON logging
- [ ] Neo4j memory tuning guide for SentimentSnapshot accumulation at 30-min cadence

---

## What to Remove / Mark Done

**Redundant or incorrectly named roadmap items:**

- ~~"Agent memory context injected into simulation tick prompts"~~ → This is **TIER 1** under a clearer name (inject ALL traits, not just past reactions)
- ~~"Archetype-conditional simulation tick prompts"~~ → Already implemented as `ARCHETYPE_BEHAVIORS` dict in simulation_tick.py. Mark ✅ DONE.
- ~~"Reaction diversity audit tooling"~~ → `verify_agent_diversity.py` exists with 8 checks. Mark ✅ DONE.
- ~~"Signal accuracy backtesting framework"~~ → `backtester.py` exists (per-asset, per-archetype, confidence calibration). Mark ✅ DONE. **Note:** results are noise with 5 days data; reliable at 30+ days.

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