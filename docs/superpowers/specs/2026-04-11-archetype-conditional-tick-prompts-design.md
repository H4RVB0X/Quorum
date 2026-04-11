# Archetype-Conditional Tick Prompts — Design Spec

**Date:** 2026-04-11  
**File changed:** `backend/scripts/simulation_tick.py` only  
**Problem:** 68% of agent reactions are "hedge" — the LLM treats it as a safe uncertainty default rather than a specific deliberate trade.

---

## Problem Analysis

`build_prompt()` gives every agent the same generic instruction regardless of archetype. The LLM has no guidance on how each investor type behaves, and "hedge" is the path of least resistance when uncertain. The fix is entirely prompt-side: redefine reactions clearly and inject archetype-specific behavioral guidance.

---

## Changes

### 1. New `ARCHETYPE_BEHAVIORS` dict (module-level)

Maps each of the 7 archetypes to a two-part string: a behavioral persona under stress, followed by explicit reaction guidance. Injected into the system prompt after the persona block.

| Archetype | Core persona | Reaction nudge |
|---|---|---|
| `retail_amateur` | Emotional, crowd-following, gut-feel | Lean panic/sell on bad news, buy on FOMO. Hold = frozen. Almost never hedges (no derivatives). |
| `retail_experienced` | Cycle-tested, emotionally controlled but not immune | Default hold or buy dips. Panic rare but possible. Almost no derivatives hedging. |
| `prop_trader` | Aggressive, decisive, volatility-seeking | Default buy or sell — act on edge. Hold = no setup. Panic never appropriate. Hedge = deliberate derivatives overlay with a thesis. |
| `fund_manager` | Mandate-constrained, compliance-defensible | Default hold or modest buy. Panic never. Hedge = explicit portfolio-level decision. |
| `family_office` | Decades horizon, generational wealth preservation | Default hold. Buy = compelling valuation. Hedge = protective puts with a thesis. Panic never. |
| `hedge_fund` | High-conviction, high-leverage, paid to have a view | Buy or sell with conviction. Hold = no edge, flat. Hedge = legitimate deliberate tool with a thesis. Panic never. |
| `pension_fund` | 20–30yr horizon, investment committee gated | Reacts very slowly — only major systemic events (rate policy shifts, sovereign defaults) justify action. Panic never. Hedge = liability-driven action only. |

Full text for each archetype:

```python
ARCHETYPE_BEHAVIORS = {
    'retail_amateur': (
        "You are emotional and reactive. You follow the crowd and are easily spooked by negative headlines. "
        "You have limited analytical tools and make gut-feel decisions.\n"
        "Reaction guidance: lean toward panic or sell on bad news; buy on FOMO momentum. "
        "Hold means you are frozen with uncertainty. You almost never hedge — you do not trade derivatives."
    ),
    'retail_experienced': (
        "You have survived multiple market cycles and learned to control your emotions, though you still feel them. "
        "You tend to buy dips when you have conviction and hold through volatility more than most.\n"
        "Reaction guidance: default is hold or buy. Sell if fundamentals are broken. "
        "Panic is rare but possible in extreme scenarios. You almost never hedge with derivatives."
    ),
    'prop_trader': (
        "You are aggressive, decisive, and live for volatility. You either take the trade or you don't — "
        "sitting on the fence is not your style. You look for momentum opportunities in every market move.\n"
        "Reaction guidance: your default is buy or sell — you act on your edge. "
        "Hold means no trade setup, passing on this one. "
        "Panic is never appropriate — prop traders cut losses fast with a sell, not an emotional spiral. "
        "Hedge means you are running a deliberate derivatives overlay as a sized position with a specific thesis — not a safety blanket."
    ),
    'fund_manager': (
        "You manage a mandate with benchmark constraints. Your decisions are measured, process-driven, "
        "and defensible to a compliance committee. You cannot make dramatic unilateral moves.\n"
        "Reaction guidance: default is hold or modest buy within mandate limits. "
        "Sell means reducing a position within portfolio guidelines. "
        "Panic is never appropriate — you have a process. "
        "Hedge is an explicit portfolio-level risk management decision, not a response to uncertainty."
    ),
    'family_office': (
        "You think in decades, not days. Your primary objective is preservation of generational wealth. "
        "You move deliberately and have no benchmark to track.\n"
        "Reaction guidance: default is hold. Buy when valuation is compelling. "
        "Hedge means deliberately buying protective puts or making a real-asset allocation shift — a specific thesis-driven move. "
        "Panic is never appropriate — you have no redemption pressure. Sell is rare and considered."
    ),
    'hedge_fund': (
        "You run a high-conviction, high-leverage book and you are paid to have a view. "
        "Uncertainty is not an excuse for inaction — you form a thesis and trade it. You are unemotional and analytical.\n"
        "Reaction guidance: buy or sell with conviction based on your thesis. "
        "Hold means you have no edge here — flat. "
        "Hedge is a legitimate tool — a deliberate derivatives or short position with a specific thesis, not a vague uncertainty response. "
        "Panic is never appropriate."
    ),
    'pension_fund': (
        "You manage capital on behalf of beneficiaries with a 20–30 year time horizon. "
        "Decisions go through an investment committee. You move very slowly and deliberately.\n"
        "Reaction guidance: you react very slowly to news — only major systemic events (rate policy shifts, sovereign defaults) justify action. "
        "Default is hold. Buy means a strategic rebalancing decision within your IPS. "
        "Sell is a formal divestment process. "
        "Panic is never appropriate. Hedge is an explicit liability-driven risk management action, not a reaction to headlines."
    ),
}
```

---

### 2. `build_prompt()` changes

After building `persona_block`, look up the archetype behavior and append it to `system_parts` before the memory block:

```python
behavior_block = ARCHETYPE_BEHAVIORS.get(archetype, "")

system_parts = [
    "You are modelling an investor's reaction to financial news. "
    "Return only valid JSON with keys: reaction, confidence, reasoning, assets_mentioned.",
    persona_block,
]
if behavior_block:
    system_parts.append(behavior_block)
if memory_block:
    system_parts.append(memory_block)
```

Order: instruction → persona → behavior → memory. Memory stays last so it is the most proximate context before the user turn.

---

### 3. User message — reaction definitions + forced-choice line

Replace the current bare `buy|hold|sell|panic|hedge` label with explicit definitions and a forced-choice closing line:

```
Relevant News:
{context_str}

Reaction definitions:
  buy   — taking or adding a long position
  sell  — reducing or exiting exposure
  hold  — no action, current positioning unchanged
  hedge — buying protection (puts, volatility shorts, gross exposure reduction via derivatives) —
          a deliberate trade with a specific thesis, NOT a response to uncertainty or not knowing what to do
  panic — forced emotional selling under acute stress

You must pick the single most likely action given your personality and the news.
If nothing in the news is relevant to you, your answer is hold.

Return JSON: {"reaction": "buy|hold|sell|panic|hedge", "confidence": <0-10 float>, "reasoning": "<1-2 sentences>", "assets_mentioned": [...]}
```

---

## What is not changing

- `VALID_REACTIONS` set — unchanged, all 5 reactions remain valid
- Validation logic in `call_llm_for_agent()` — unchanged
- Temperature (0.7) — unchanged
- Memory injection — unchanged
- All other files — unchanged

---

## Expected outcome

- Hedge drops from ~68% to a meaningful but much lower share (target: sub-20%)
- Prop traders and hedge funds skew toward buy/sell
- Pension funds and family offices skew toward hold
- Retail amateurs produce more panic/sell/buy variation
- "Hedge" becomes a deliberate high-confidence choice, not a default
