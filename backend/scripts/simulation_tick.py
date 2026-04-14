"""
simulation_tick.py — Run a per-agent LLM news reaction pass.

For each sampled agent:
  1. Fetch last 7 days of MemoryEvent nodes from Neo4j
  2. Select top-5 briefing chunks by cosine similarity to agent's trait query
  3. Call LLM → {reaction, confidence, reasoning, assets_mentioned}
  4. Write result as new :MemoryEvent node linked to agent

Checkpoint recovery: every 100 agents writes tick_checkpoint.json.
On restart with same briefing, already-processed agents are skipped.

Usage:
  python simulation_tick.py --briefing /path/to/briefing.txt --graph-id <uuid> [--full]

Changelog:
  2026-04-14  Stratified sampling — guarantee min 5 agents per archetype per tick.
              Time-horizon gating — agents with slow reaction_speed_minutes have
              proportionally reduced participation probability in 30-min ticks.
              Trait mappings updated to exact spec text with revised breakpoints
              for loss_aversion, time_horizon, and formative_crash ("none" omitted).
              leverage_typical and asset_class_bias added to persona block.
  2026-04-13  TIER 1 — All 16 agent traits now injected into build_prompt().
              Previously only 8 traits were passed to the LLM. Six additional traits
              (formative_crash, loss_aversion_multiplier, time_horizon_days,
              geopolitical_sensitivity, reaction_speed_minutes, overconfidence_bias)
              are now mapped to natural-language descriptions and included in the
              agent persona block. No schema changes. No new dependencies.
"""
import sys
import os
import json
import uuid
import random
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np

from db_setup import setup as db_setup
from app.config import Config
from app.storage.embedding_service import EmbeddingService
from app.services.text_processor import TextProcessor
from app.utils.llm_client import LLMClient
from app.utils.logger import get_logger

logger = get_logger('mirofish.simulation_tick')

CHECKPOINT_PATH    = Path(__file__).parent / "tick_checkpoint.json"
_PRICES_DIR        = Path(__file__).parent.parent / "prices"
CONTAGION_FLAG_PATH = Path(__file__).parent.parent / "briefings" / "contagion_flag.txt"
CHECKPOINT_INTERVAL = 100
SAMPLE_SIZE = 500
TOP_K_CHUNKS = 5
MEMORY_WINDOW_DAYS = 7

VALID_REACTIONS = {'buy', 'hold', 'sell', 'panic', 'hedge'}

ARCHETYPE_BEHAVIORS = {
    'retail_amateur': (
        "You are emotional and reactive. You follow the crowd and are easily spooked by negative headlines. "
        "You have limited analytical tools and make gut-feel decisions.\n"
        "Reaction guidance: lean toward panic or sell on bad news; buy on FOMO momentum. "
        "Hold means you are frozen with uncertainty — if you are unsure, you hold, not hedge. "
        "You almost never hedge — you do not trade derivatives."
    ),
    'retail_experienced': (
        "You have survived multiple market cycles and learned to control your emotions, though you still feel them. "
        "You tend to buy dips when you have conviction and hold through volatility more than most.\n"
        "Reaction guidance: default is hold or buy. Sell if fundamentals are clearly broken. "
        "Panic is rare but possible in extreme scenarios. "
        "If uncertain, hold — you almost never hedge with derivatives, and only when you can name the exact instrument and risk."
    ),
    'prop_trader': (
        "You are aggressive, decisive, and live for volatility. You either take the trade or you don't — "
        "sitting on the fence is not your style. You look for momentum opportunities in every market move.\n"
        "Reaction guidance: your default is buy or sell — you act on your edge. "
        "Hold means no trade setup, passing on this one. "
        "Panic is never appropriate — prop traders cut losses fast with a sell, not an emotional spiral. "
        "Hedge means you are running a deliberate derivatives overlay as a sized position with a specific thesis — not a safety blanket. "
        "If you cannot state the exact instrument and the specific exposure you are offsetting, the answer is hold."
    ),
    'fund_manager': (
        "You manage a mandate with benchmark constraints. Your decisions are systematic and defensible to a compliance committee. "
        "When news is ambiguous or only partially relevant, you hold — you do not act without a clear mandate-compliant thesis.\n"
        "Reaction guidance: default is hold or modest buy within mandate limits. "
        "Sell means reducing a position within portfolio guidelines. "
        "Panic is never appropriate — you have a process. "
        "If uncertain, hold — hedge requires an explicit risk management mandate decision and a named instrument, not a reaction to ambiguous news."
    ),
    'family_office': (
        "You think in decades, not days. Your primary objective is growing and preserving generational wealth through compounding. "
        "You move deliberately, have no benchmark to track, and uncertainty is never a reason to act — when in doubt, you hold.\n"
        "Reaction guidance: default is hold. Buy when valuation is compelling and you have a clear thesis. "
        "Hedge means deliberately buying protective puts or making a real-asset allocation shift — a specific thesis-driven move, not a defensive reflex. "
        "If you cannot name the instrument and the risk being offset, choose hold. "
        "Panic is never appropriate — you have no redemption pressure. Sell is rare and considered."
    ),
    'hedge_fund': (
        "You run a high-conviction, high-leverage book and you are paid to have a view. "
        "Uncertainty is not an excuse for inaction — you form a thesis and trade it. You are unemotional and analytical.\n"
        "Reaction guidance: buy or sell with conviction based on your thesis. "
        "Hold means you have no edge here — flat. "
        "Hedge is a legitimate tool — a deliberate derivatives or short position with a specific thesis and a named instrument, not a vague uncertainty response. "
        "Panic is never appropriate."
    ),
    'pension_fund': (
        "You manage capital on behalf of beneficiaries with a 20–30 year time horizon. "
        "Decisions go through an investment committee. You move very slowly and deliberately.\n"
        "Reaction guidance: you react very slowly to news — only major systemic events (rate policy shifts, sovereign defaults) justify action. "
        "Default is hold. Buy means a strategic rebalancing decision within your IPS. "
        "Sell is a formal divestment process. "
        "Panic is never appropriate. "
        "Hedge is an explicit liability-driven risk management action with a named instrument — not a reaction to headlines. If uncertain, hold."
    ),
}


# ---------------------------------------------------------------------------
# Chunk cache and similarity
# ---------------------------------------------------------------------------

def top_k_chunks(query_embedding: list, chunk_cache: list, k: int = TOP_K_CHUNKS) -> list:
    """
    Return the k chunk texts most similar to query_embedding.
    chunk_cache: list of (text, embedding) tuples.
    """
    if not chunk_cache:
        return []

    q = np.array(query_embedding, dtype='float64')
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return [c[0] for c in chunk_cache[:k]]

    scored = []
    for text, emb in chunk_cache:
        e = np.array(emb, dtype='float64')
        e_norm = np.linalg.norm(e)
        if e_norm == 0:
            sim = 0.0
        else:
            sim = float(np.dot(q, e) / (q_norm * e_norm))
        scored.append((sim, text))

    scored.sort(reverse=True)
    return [text for _, text in scored[:k]]


def build_agent_query(traits: dict) -> str:
    """
    Build a short relevance query string from agent traits for chunk selection.
    High geopolitical_sensitivity (>=7) appends geopolitical keywords.
    """
    parts = [
        traits.get('asset_class_bias', ''),
        traits.get('primary_strategy', ''),
    ]
    geo = traits.get('geopolitical_sensitivity')
    if geo is not None and geo >= 7:
        parts.append("geopolitical risk sanctions war conflict")
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def write_checkpoint(path: Path, briefing_source: str, processed_ids: list) -> None:
    data = {
        "briefing_source": briefing_source,
        "processed_agent_ids": list(processed_ids),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(data, indent=2), encoding='utf-8')


def load_checkpoint(path: Path, briefing_source: str) -> set:
    """Return set of already-processed agent UUIDs if checkpoint matches briefing_source."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get("briefing_source") == briefing_source:
            return set(data.get("processed_agent_ids", []))
    except Exception:
        pass
    return set()


# ---------------------------------------------------------------------------
# Neo4j helpers
# ---------------------------------------------------------------------------

_AGENT_RETURN_FIELDS = """
    RETURN n.uuid AS uuid, n.name AS name,
           n.risk_tolerance AS risk_tolerance,
           n.capital_usd AS capital_usd,
           n.time_horizon_days AS time_horizon_days,
           n.fear_greed_dominant AS fear_greed_dominant,
           n.loss_aversion_multiplier AS loss_aversion_multiplier,
           n.herd_behaviour AS herd_behaviour,
           n.reaction_speed_minutes AS reaction_speed_minutes,
           n.primary_strategy AS primary_strategy,
           n.asset_class_bias AS asset_class_bias,
           n.news_sensitivity AS news_sensitivity,
           n.geopolitical_sensitivity AS geopolitical_sensitivity,
           n.investor_archetype AS investor_archetype,
           n.formative_crash AS formative_crash,
           n.overconfidence_bias AS overconfidence_bias,
           n.leverage_typical AS leverage_typical
"""

_ARCHETYPES = [
    'retail_amateur', 'retail_experienced', 'prop_trader',
    'fund_manager', 'family_office', 'hedge_fund', 'pension_fund',
]
_STRATIFIED_GUARANTEE = 5  # minimum agents per archetype


def load_agents(driver, graph_id: str, sample_size: Optional[int]) -> list:
    """Load agents with investor trait profiles. If sample_size is None, load all."""
    limit_clause = f"LIMIT {sample_size}" if sample_size else ""
    with driver.session() as session:
        result = session.run(
            f"""
            MATCH (n:Entity {{graph_id: $gid}})
            WHERE n.risk_tolerance IS NOT NULL
            {_AGENT_RETURN_FIELDS}
            ORDER BY rand()
            {limit_clause}
            """,
            gid=graph_id,
        )
        return [dict(r) for r in result]


def load_agents_stratified(driver, graph_id: str, sample_size: int) -> list:
    """
    Stratified sample: guarantee min(_STRATIFIED_GUARANTEE, archetype_pool) agents per
    archetype, then fill remaining slots with random agents from the full pool.
    This prevents pension_fund and hedge_fund from being absent due to sampling variance.
    """
    guaranteed: list = []
    guaranteed_ids: set = set()

    # Step 1: guaranteed slots per archetype
    for archetype in _ARCHETYPES:
        with driver.session() as session:
            result = session.run(
                f"""
                MATCH (n:Entity {{graph_id: $gid, investor_archetype: $arch}})
                WHERE n.risk_tolerance IS NOT NULL
                {_AGENT_RETURN_FIELDS}
                ORDER BY rand()
                LIMIT {_STRATIFIED_GUARANTEE}
                """,
                gid=graph_id,
                arch=archetype,
            )
            for r in result:
                a = dict(r)
                if a['uuid'] not in guaranteed_ids:
                    guaranteed.append(a)
                    guaranteed_ids.add(a['uuid'])

    # Step 2: fill remaining slots randomly from the full pool (excluding already picked)
    remaining = sample_size - len(guaranteed)
    if remaining > 0:
        with driver.session() as session:
            result = session.run(
                f"""
                MATCH (n:Entity {{graph_id: $gid}})
                WHERE n.risk_tolerance IS NOT NULL AND NOT n.uuid IN $excluded
                {_AGENT_RETURN_FIELDS}
                ORDER BY rand()
                LIMIT {remaining}
                """,
                gid=graph_id,
                excluded=list(guaranteed_ids),
            )
            for r in result:
                guaranteed.append(dict(r))

    logger.info(
        f"Stratified sample: {len(guaranteed)} agents "
        f"({min(_STRATIFIED_GUARANTEE, len(guaranteed))} guaranteed per archetype + {max(0, len(guaranteed) - len(_ARCHETYPES) * _STRATIFIED_GUARANTEE)} random fill)"
    )
    return guaranteed


def load_agent_memory(driver, agent_uuid: str) -> list:
    """Return last MEMORY_WINDOW_DAYS days of MemoryEvent records for this agent."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MEMORY_WINDOW_DAYS)).isoformat()
    with driver.session() as session:
        result = session.run(
            """
            MATCH (n:Entity {uuid: $uuid})-[:HAS_MEMORY]->(m:MemoryEvent)
            WHERE m.timestamp > $cutoff
            RETURN m.reaction AS reaction, m.reasoning AS reasoning,
                   m.confidence AS confidence, m.timestamp AS ts
            ORDER BY m.timestamp
            """,
            uuid=agent_uuid,
            cutoff=cutoff,
        )
        return [dict(r) for r in result]


def load_agent_recent_memory(driver, agent_uuid: str, limit: int = 5) -> list:
    """Return the last `limit` MemoryEvent records for this agent, newest first."""
    with driver.session() as session:
        result = session.run(
            """
            MATCH (a:Entity {uuid: $agent_uuid})-[:HAS_MEMORY]->(m:MemoryEvent)
            RETURN m.reaction AS reaction, m.confidence AS confidence,
                   m.reasoning AS reasoning, m.timestamp AS timestamp,
                   m.briefing_source AS briefing_source
            ORDER BY m.timestamp DESC LIMIT $limit
            """,
            agent_uuid=agent_uuid,
            limit=limit,
        )
        return [dict(r) for r in result]


# ---------------------------------------------------------------------------
# Portfolio position helpers (TIER 2A)
# ---------------------------------------------------------------------------

_POSITION_ASSETS = ['equities', 'crypto', 'bonds', 'commodities', 'fx', 'real_estate', 'mixed']
_ASSET_TICKERS   = {
    'equities':    'SPY',
    'crypto':      'BTC-USD',
    'bonds':       'TLT',
    'commodities': 'GLD',
    'fx':          'DX-Y.NYB',
    'real_estate': 'VNQ',
    'mixed':       'VT',
}


def _load_current_prices() -> dict:
    """
    Load per-asset-class prices from the most recent price file in backend/prices/.
    Returns {} if no price files exist — positions block will omit P&L lines.
    """
    if not _PRICES_DIR.is_dir():
        return {}
    files = sorted(_PRICES_DIR.glob("????-??-??.json"), reverse=True)
    if not files:
        return {}
    try:
        data = json.loads(files[0].read_text(encoding='utf-8'))
        return data.get('prices', {})
    except Exception:
        return {}


def load_agent_positions(driver, agent_uuid: str, graph_id: str) -> dict:
    """
    Fetch current portfolio position state for an agent from Neo4j in a single query.
    Returns {asset: {position, entry_price}} for all 7 asset classes.
    Returns {} on any failure — callers must handle gracefully.
    """
    try:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (a:Entity {uuid: $uuid, graph_id: $gid})
                RETURN
                    a.position_equities       AS position_equities,
                    a.entry_price_equities    AS entry_price_equities,
                    a.position_crypto         AS position_crypto,
                    a.entry_price_crypto      AS entry_price_crypto,
                    a.position_bonds          AS position_bonds,
                    a.entry_price_bonds       AS entry_price_bonds,
                    a.position_commodities    AS position_commodities,
                    a.entry_price_commodities AS entry_price_commodities,
                    a.position_fx             AS position_fx,
                    a.entry_price_fx          AS entry_price_fx,
                    a.position_real_estate    AS position_real_estate,
                    a.entry_price_real_estate AS entry_price_real_estate,
                    a.position_mixed          AS position_mixed,
                    a.entry_price_mixed       AS entry_price_mixed
                """,
                uuid=agent_uuid,
                gid=graph_id,
            ).single()
        if result is None:
            return {}
        positions = {}
        for asset in _POSITION_ASSETS:
            pos   = result.get(f'position_{asset}') or 'flat'
            entry = result.get(f'entry_price_{asset}')
            positions[asset] = {'position': pos, 'entry_price': entry}
        return positions
    except Exception as e:
        logger.warning(f"Position fetch failed for {agent_uuid}: {e}")
        return {}


def format_positions_block(positions: dict, current_prices: dict) -> str:
    """
    Build a portfolio positions string for injection into the agent system prompt.
    Shows direction, entry price, and unrealised P&L for held positions.
    """
    if not positions:
        return "Current portfolio: no open positions — you are deciding whether to enter."

    all_flat = all(
        (p.get('position') or 'flat') == 'flat'
        for p in positions.values()
    )
    if all_flat:
        return "Current portfolio: no open positions — you are deciding whether to enter."

    lines = ["Current portfolio positions:"]
    for asset in _POSITION_ASSETS:
        info    = positions.get(asset, {'position': 'flat', 'entry_price': None})
        pos     = (info.get('position') or 'flat').lower()
        entry   = info.get('entry_price')
        ticker  = _ASSET_TICKERS.get(asset, asset.upper())
        current = current_prices.get(asset)

        if pos == 'flat':
            lines.append(f"  {ticker}: FLAT")
        elif entry is not None and current is not None:
            try:
                entry_f   = float(entry)
                current_f = float(current)
                if pos == 'long':
                    pnl_pct = (current_f - entry_f) / entry_f * 100
                elif pos == 'short':
                    pnl_pct = (entry_f - current_f) / entry_f * 100
                else:
                    pnl_pct = 0.0
                sign = '+' if pnl_pct >= 0 else ''
                lines.append(
                    f"  {ticker}: {pos.upper()} "
                    f"(entered ${entry_f:,.2f}, now ${current_f:,.2f}, "
                    f"{sign}{pnl_pct:.2f}% unrealised P&L)"
                )
            except (TypeError, ValueError):
                lines.append(f"  {ticker}: {pos.upper()} (entry ${entry})")
        else:
            lines.append(f"  {ticker}: {pos.upper()}")

    return "\n".join(lines)


def update_agent_positions(driver, agent_uuid: str, graph_id: str,
                           reaction: str, assets_mentioned: list,
                           current_prices: dict, existing_positions: dict,
                           asset_class_bias: str) -> None:
    """
    Update agent position state in Neo4j after an LLM reaction.
    Uses MATCH (not MERGE) — never creates agent nodes.
    All updates written in a single batched SET statement.

    Position update rules:
      buy   → position=long; entry_price set only if currently flat (new position)
      sell  → position=flat, entry_price=null
      panic → ALL positions → flat, ALL entry_prices → null (full liquidation)
      hold/hedge → no position change
    """
    reaction = (reaction or 'hold').lower()

    if reaction in ('hold', 'hedge'):
        return  # no position change

    # Resolve target assets
    target_assets = [a for a in (assets_mentioned or []) if a in _POSITION_ASSETS]
    if not target_assets:
        # Infer from agent's declared bias
        if asset_class_bias and asset_class_bias in _POSITION_ASSETS:
            target_assets = [asset_class_bias]
        else:
            return  # Cannot determine target asset — skip update

    set_parts: list = []
    params: dict    = {'uuid': agent_uuid, 'gid': graph_id}

    if reaction == 'panic':
        # Full liquidation — flatten every position regardless of assets_mentioned
        for asset in _POSITION_ASSETS:
            set_parts.append(f"a.position_{asset} = 'flat'")
            set_parts.append(f"a.entry_price_{asset} = null")
            set_parts.append(f"a.last_reaction_{asset} = 'panic'")

    elif reaction == 'buy':
        for asset in target_assets:
            set_parts.append(f"a.position_{asset} = 'long'")
            set_parts.append(f"a.last_reaction_{asset} = 'buy'")
            # Only record entry price when opening a new position (not adding to existing long)
            existing_pos = (existing_positions.get(asset, {}).get('position') or 'flat')
            if existing_pos == 'flat':
                current = current_prices.get(asset)
                if current is not None:
                    key = f'entry_{asset}'
                    params[key] = float(current)
                    set_parts.append(f"a.entry_price_{asset} = ${key}")

    elif reaction == 'sell':
        for asset in target_assets:
            set_parts.append(f"a.position_{asset} = 'flat'")
            set_parts.append(f"a.entry_price_{asset} = null")
            set_parts.append(f"a.last_reaction_{asset} = 'sell'")

    if not set_parts:
        return

    set_parts.append("a.last_reaction_timestamp = datetime()")

    cypher = (
        f"MATCH (a:Entity {{uuid: $uuid, graph_id: $gid}}) "
        f"SET {', '.join(set_parts)}"
    )
    try:
        with driver.session() as session:
            session.run(cypher, **params)
    except Exception as e:
        logger.warning(f"Position update ({reaction}) failed for {agent_uuid}: {e}")


def write_sentiment_snapshot(driver, graph_id: str, tick_reactions: list) -> None:
    """
    Write a SentimentSnapshot node to Neo4j capturing the aggregate sentiment
    for this tick. Uses MERGE on (graph_id, timestamp) to prevent duplicates
    if the tick is re-run.

    tick_reactions: list of dicts, each with keys:
        asset_class, reaction, confidence, capital, leverage_typical
    """
    if not tick_reactions:
        return

    from collections import defaultdict as _dd

    # Per-asset capital-weighted sentiment
    asset_buckets: dict = _dd(lambda: {'wsum': 0.0, 'wtot': 0.0})
    _SCORES = {'buy': 1.0, 'hedge': 0.5, 'hold': 0.0, 'sell': -1.0, 'panic': -1.0}

    # Reaction distribution for this tick
    reaction_counts: dict = _dd(int)
    total = len(tick_reactions)

    for r in tick_reactions:
        asset = r.get('asset_class') or 'unknown'
        reaction = (r.get('reaction') or 'hold').lower()
        confidence = float(r.get('confidence') or 0)
        capital = float(r.get('capital') or 0)
        score = _SCORES.get(reaction, 0.0)
        weight = capital * confidence
        asset_buckets[asset]['wsum'] += score * weight
        asset_buckets[asset]['wtot'] += weight
        reaction_counts[reaction] += 1

    # Compute per-asset scores
    asset_scores = {}
    for asset, b in asset_buckets.items():
        if b['wtot'] == 0:
            asset_scores[asset] = 0.0
        else:
            asset_scores[asset] = round(max(-1.0, min(1.0, b['wsum'] / b['wtot'])), 4)

    # Reaction distribution as percentages
    reaction_dist = {r: round(c / total, 4) for r, c in reaction_counts.items()} if total else {}

    # Fear/greed score: 50 = neutral, 0 = extreme fear, 100 = extreme greed
    # Computed as overall mean sentiment across all assets mapped to [0, 100]
    all_scores = list(asset_scores.values())
    mean_sent = sum(all_scores) / len(all_scores) if all_scores else 0.0
    fear_greed_score = round(50 + mean_sent * 50, 2)

    now = datetime.now(timezone.utc).isoformat()

    with driver.session() as session:
        def _write(tx):
            tx.run(
                """
                MERGE (s:SentimentSnapshot {graph_id: $gid, timestamp: $ts})
                SET s.equities      = $equities,
                    s.crypto        = $crypto,
                    s.bonds         = $bonds,
                    s.commodities   = $commodities,
                    s.fx            = $fx,
                    s.real_estate   = $real_estate,
                    s.mixed         = $mixed,
                    s.buy_pct       = $buy_pct,
                    s.sell_pct      = $sell_pct,
                    s.hold_pct      = $hold_pct,
                    s.hedge_pct     = $hedge_pct,
                    s.panic_pct     = $panic_pct,
                    s.total_agents  = $total_agents,
                    s.fear_greed    = $fear_greed
                """,
                gid=graph_id,
                ts=now,
                equities=asset_scores.get('equities', 0.0),
                crypto=asset_scores.get('crypto', 0.0),
                bonds=asset_scores.get('bonds', 0.0),
                commodities=asset_scores.get('commodities', 0.0),
                fx=asset_scores.get('fx', 0.0),
                real_estate=asset_scores.get('real_estate', 0.0),
                mixed=asset_scores.get('mixed', 0.0),
                buy_pct=reaction_dist.get('buy', 0.0),
                sell_pct=reaction_dist.get('sell', 0.0),
                hold_pct=reaction_dist.get('hold', 0.0),
                hedge_pct=reaction_dist.get('hedge', 0.0),
                panic_pct=reaction_dist.get('panic', 0.0),
                total_agents=total,
                fear_greed=fear_greed_score,
            )
        session.execute_write(_write)

    logger.info(
        f"SentimentSnapshot written: {total} agents, "
        f"fear_greed={fear_greed_score}, assets={list(asset_scores.keys())}"
    )


def write_memory_event(driver, agent_uuid: str, briefing_source: str, reaction_data: dict) -> None:
    """Create a :MemoryEvent node and link it to the agent.

    TIER 2B fields stored on every event:
      direction     — int (-1/0/+1); null on older events (use coalesce in Cypher)
      conviction    — float 0-1; null on older events
      position_size — float 0-1; null on older events
    """
    event_uuid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # TIER 2B: parse conviction model fields with safe defaults
    direction = reaction_data.get('direction', 0)
    try:
        direction = int(direction)
        if direction not in (-1, 0, 1):
            direction = 0
    except (TypeError, ValueError):
        direction = 0

    conviction = reaction_data.get('conviction', 0.5)
    try:
        conviction = max(0.0, min(1.0, float(conviction)))
    except (TypeError, ValueError):
        conviction = 0.5

    position_size = reaction_data.get('position_size', 0.3)
    try:
        position_size = max(0.0, min(1.0, float(position_size)))
    except (TypeError, ValueError):
        position_size = 0.3

    with driver.session() as session:
        def _write(tx):
            tx.run(
                """
                MATCH (n:Entity {uuid: $agent_uuid})
                CREATE (m:MemoryEvent {
                    uuid: $uuid,
                    agent_uuid: $agent_uuid,
                    timestamp: $ts,
                    briefing_source: $src,
                    reaction: $reaction,
                    confidence: $confidence,
                    reasoning: $reasoning,
                    assets_mentioned: $assets,
                    direction: $direction,
                    conviction: $conviction,
                    position_size: $position_size
                })
                CREATE (n)-[:HAS_MEMORY]->(m)
                """,
                agent_uuid=agent_uuid,
                uuid=event_uuid,
                ts=now,
                src=briefing_source,
                reaction=reaction_data.get('reaction', 'hold'),
                confidence=float(reaction_data.get('confidence', 5.0)),
                reasoning=str(reaction_data.get('reasoning', '')),
                assets=json.dumps(reaction_data.get('assets_mentioned', [])),
                direction=direction,
                conviction=conviction,
                position_size=position_size,
            )
        session.execute_write(_write)


# ---------------------------------------------------------------------------
# LLM reaction call
# ---------------------------------------------------------------------------

def format_memory_block(memory_events: list) -> str:
    """
    Format recent MemoryEvent records into a readable block for the system prompt.
    Returns an empty string if there are no events (caller skips the block).
    """
    if not memory_events:
        return ""
    lines = []
    for e in memory_events:
        ts_raw = e.get('timestamp', '')
        try:
            dt = datetime.fromisoformat(ts_raw.replace('Z', '+00:00'))
            ts_str = dt.strftime('%d %b %H:%M')
        except Exception:
            ts_str = ts_raw[:16] if ts_raw else '??'
        reaction = str(e.get('reaction', 'hold')).upper()
        try:
            conf_str = f"{float(e.get('confidence', 5)):.0f}"
        except (TypeError, ValueError):
            conf_str = '5'
        reasoning = e.get('reasoning', '')
        lines.append(f'- [{ts_str}] {reaction} (confidence {conf_str}/10): "{reasoning}"')
    return "Your recent reaction history:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Tier 1: trait-to-English mapping helpers
# ---------------------------------------------------------------------------

_FORMATIVE_CRASH_LABELS = {
    'gfc_2008': (
        "You lived through the 2008 Global Financial Crisis. "
        "You are acutely sensitive to credit risk, counterparty exposure, and liquidity crises. "
        "Bank stress news activates deep defensive instincts."
    ),
    'dotcom_2000': (
        "You lived through the 2000 dotcom crash. "
        "You distrust narrative-driven valuations and are sceptical of "
        "'this time is different' arguments for tech and growth stocks."
    ),
    'covid_2020': (
        "You lived through the COVID-19 crash and recovery. "
        "You learned that aggressive buying into panic sells is often rewarded. "
        "You are less fearful of sharp drawdowns than most."
    ),
    # 'none' → omit entirely
}


def _loss_aversion_label(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return f"moderate loss aversion"
    if v < 0.5:
        return "unusually low loss aversion (you treat gains and losses almost symmetrically)"
    if v <= 1.5:
        return "moderate loss aversion"
    if v <= 3.5:
        return "high loss aversion (losses feel roughly 2–3x worse than equivalent gains)"
    return "extreme loss aversion (losses feel catastrophic — you prioritise capital preservation heavily over upside capture)"


def _time_horizon_label(value) -> str:
    try:
        d = int(value)
    except (TypeError, ValueError):
        return "months-scale time horizon"
    if d < 30:
        return "intraday-to-weeks time horizon — long-term macro trends are irrelevant to you"
    if d < 365:
        return "months-scale time horizon"
    if d <= 1095:
        return "1–3 year time horizon"
    return "multi-year to decade-scale time horizon — daily price moves are almost entirely irrelevant to your decision-making"


def _reaction_speed_label(value) -> str:
    try:
        mins = float(value)
    except (TypeError, ValueError):
        return "within 60 minutes of news breaking"
    if mins > 1440:
        return "over days to weeks, not hours"
    return f"within {mins:.0f} minutes of news breaking"


def _leverage_prose(value) -> Optional[str]:
    """Return English description for leverage_typical, or None if 'none'."""
    lev = (value or 'none').lower()
    if lev == '2x':
        return "You typically use 2x leverage — margin calls are a real constraint on your positioning."
    if lev == '5x':
        return "You use 5x leverage — volatility directly threatens your positions via margin pressure."
    if lev == '10x_plus':
        return (
            "You use 10x+ leverage — you are forced to react to adverse moves regardless of your opinion; "
            "margin calls create mechanical selling pressure."
        )
    return None  # 'none' → omit


def build_prompt(agent: dict, memory_events: list, context_chunks: list,
                 positions_block: str = '', contagion_context: str = '') -> list:
    """
    Build messages list for LLMClient.chat_json().

    TIER 1 (2026-04-13/14): All 16 agent traits injected into the system prompt.
    Trait mappings follow exact spec text:
      - formative_crash: 'none' omitted entirely; gfc/dotcom_2000/covid mapped to prose
      - loss_aversion_multiplier: <0.5 / 0.5-1.5 / 1.5-3.5 / >3.5 tiers
      - time_horizon_days: <30 / 30-365 / 365-1095 / >1095 tiers
      - geopolitical_sensitivity: only injected if >=7
      - reaction_speed_minutes: plain English, ">1440" → "days to weeks"
      - overconfidence_bias: three tiers with descriptive text
      - leverage_typical: injected if not 'none'
      - asset_class_bias: injected as portfolio concentration statement
    """
    # ── Core traits ───────────────────────────────────────────────────────────
    name        = agent.get('name', 'Unknown')
    archetype   = agent.get('investor_archetype', 'general')
    capital     = agent.get('capital_usd', 'unknown')
    risk        = agent.get('risk_tolerance', 'unknown')
    strategy    = agent.get('primary_strategy', 'unknown')
    fear_greed  = agent.get('fear_greed_dominant', 'unknown')
    herd        = agent.get('herd_behaviour', 'unknown')
    sensitivity = agent.get('news_sensitivity', 'unknown')

    # ── Extended trait mapping ────────────────────────────────────────────────
    formative_crash_raw = (agent.get('formative_crash') or 'none').lower()
    formative_crash_prose = _FORMATIVE_CRASH_LABELS.get(formative_crash_raw)
    # None means 'none' — omit entirely from prompt

    loss_aversion_prose   = _loss_aversion_label(agent.get('loss_aversion_multiplier', 2.0))
    time_horizon_prose    = _time_horizon_label(agent.get('time_horizon_days', 365))
    reaction_speed_prose  = _reaction_speed_label(agent.get('reaction_speed_minutes', 60))

    try:
        overconfidence = float(agent.get('overconfidence_bias', 5.0))
    except (TypeError, ValueError):
        overconfidence = 5.0

    if overconfidence >= 7.0:
        overconfidence_detail = "you systematically underestimate downside risk and rate your own judgement highly"
    elif overconfidence >= 4.0:
        overconfidence_detail = "moderate — you are reasonably calibrated"
    else:
        overconfidence_detail = "you tend to second-guess yourself and express lower confidence than your information warrants"

    try:
        geo = float(agent.get('geopolitical_sensitivity', 5.0))
    except (TypeError, ValueError):
        geo = 5.0

    # Only inject if >=7 (per spec)
    geopolitical_prose = None
    if geo >= 7.0:
        geopolitical_prose = (
            "acute geopolitical sensitivity — sanctions, regional conflict, trade war escalation, "
            "and election outcomes matter more to you than rate decisions or earnings beats"
        )

    leverage_prose   = _leverage_prose(agent.get('leverage_typical', 'none'))
    asset_class_bias = agent.get('asset_class_bias') or 'mixed'

    # ── Persona block ─────────────────────────────────────────────────────────
    fingerprint_lines = [
        f"- Loss aversion: {loss_aversion_prose}",
        f"- Time horizon: {time_horizon_prose}",
        f"- You react {reaction_speed_prose}",
        f"- Overconfidence bias: {overconfidence:.1f}/10 — {overconfidence_detail}",
        f"- Your portfolio is concentrated in {asset_class_bias} — frame your reaction in terms of your exposure to this asset class",
    ]
    if formative_crash_prose:
        fingerprint_lines.insert(0, f"- Formative experience: {formative_crash_prose}")
    if geopolitical_prose:
        fingerprint_lines.append(f"- {geopolitical_prose}")
    if leverage_prose:
        fingerprint_lines.append(f"- {leverage_prose}")

    persona_block = (
        f"You are {name}, a {archetype} investor.\n"
        f"Capital: {capital}. Risk tolerance: {risk}/10.\n"
        f"Strategy: {strategy}. Fear/greed: {fear_greed}.\n"
        f"Herd behaviour: {herd}/10. News sensitivity: {sensitivity}/10.\n"
        f"\n"
        f"Behavioural fingerprint:\n"
        + "\n".join(fingerprint_lines)
        + "\n\n"
        "Your reasoning must reflect these traits — especially your formative experience, "
        "loss aversion, and time horizon. Do not give generic market commentary."
    )

    memory_block  = format_memory_block(memory_events)
    behavior_block = ARCHETYPE_BEHAVIORS.get(archetype, "")

    system_parts = [
        "You must respond entirely in English. Do not use any other language.",
        "You are modelling an investor's reaction to financial news. "
        "Return only valid JSON with keys: reaction, direction, conviction, position_size, confidence, reasoning, assets_mentioned.",
        persona_block,
    ]
    if behavior_block:
        system_parts.append(behavior_block)
    if memory_block:
        system_parts.append(memory_block)
    if positions_block:
        system_parts.append(positions_block)
    if contagion_context:
        system_parts.append(contagion_context)

    context_str = "\n\n".join(context_chunks) or "No briefing context available."

    return [
        {
            "role": "system",
            "content": "\n\n".join(system_parts),
        },
        {
            "role": "user",
            "content": (
                f"Relevant News:\n{context_str}\n\n"
                "Reaction definitions:\n"
                "  buy   — taking or adding a long position\n"
                "  sell  — reducing or exiting exposure\n"
                "  hold  — no action, current positioning unchanged\n"
                "  hedge — you are making a deliberate, specific trade to offset a named risk — you must be able to state exactly what instrument you are using and what exposure you are hedging. This is NOT a response to uncertainty or partial relevance. If you cannot name the instrument and the risk, choose hold instead.\n"
                "  panic — you are selling everything immediately due to extreme fear — valid ONLY for retail_amateur and retail_experienced when the news contains any of: market crash, circuit breaker, trading halt, emergency rate hike, bank collapse, war escalation, systemic crisis. You do not think — you react. If you are retail and the news contains any of these triggers, panic is your most likely response.\n\n"
                "You must pick the single most likely action given your personality and the news. "
                "If nothing in the news is relevant to you, your answer is hold.\n\n"
                "You must respond with ONLY valid JSON in exactly this format — no other text, no markdown, no explanation:\n"
                '{"reaction": "buy" or "hold" or "sell" or "panic" or "hedge", '
                '"direction": 1 or 0 or -1, '
                '"conviction": <float 0.0 to 1.0>, '
                '"position_size": <float 0.0 to 1.0>, '
                '"confidence": <float 0.0 to 10.0>, '
                '"reasoning": "<1-2 sentences max>", '
                '"assets_mentioned": ["equities"|"crypto"|"bonds"|"commodities"|"fx"|"mixed"|"real_estate"]}\n\n'
                "direction: 1=bullish, 0=neutral, -1=bearish\n"
                "conviction: your certainty in this reaction (0=uncertain, 1=certain)\n"
                "position_size: fraction of your available capital you would deploy "
                "(0=nothing, 1=full allocation). Be realistic for your archetype — "
                "a pension fund rarely deploys more than 0.05 on a single signal; "
                "a retail amateur may deploy 0.8.\n"
                "confidence: your overall confidence 0-10 (keep for compatibility)"
            ),
        },
    ]


def call_llm_for_agent(llm: LLMClient, agent: dict, memory: list, chunks: list,
                       positions_block: str = '', contagion_context: str = '') -> Optional[dict]:
    """Returns validated reaction dict, or None on failure.

    TIER 2B: parses direction, conviction, position_size from LLM response.
    Missing or invalid fields fall back to safe defaults (logged at DEBUG).
    """
    messages = build_prompt(agent, memory, chunks, positions_block=positions_block,
                            contagion_context=contagion_context)
    try:
        result = llm.chat_json(messages, temperature=0.7, max_tokens=512)

        # Validate reaction
        if result.get('reaction') not in VALID_REACTIONS:
            result['reaction'] = 'hold'
        result['confidence'] = max(0.0, min(10.0, float(result.get('confidence', 5.0))))
        if not isinstance(result.get('assets_mentioned'), list):
            result['assets_mentioned'] = []

        # TIER 2B: direction (-1 / 0 / +1)
        direction = result.get('direction', 0)
        try:
            direction = int(direction)
            if direction not in (-1, 0, 1):
                raise ValueError(f"out of range: {direction}")
        except (TypeError, ValueError):
            logger.debug(f"direction missing/invalid for {agent.get('uuid')}, defaulting to 0")
            direction = 0
        result['direction'] = direction

        # TIER 2B: conviction (0.0–1.0)
        conviction = result.get('conviction', 0.5)
        try:
            conviction = max(0.0, min(1.0, float(conviction)))
        except (TypeError, ValueError):
            logger.debug(f"conviction missing/invalid for {agent.get('uuid')}, defaulting to 0.5")
            conviction = 0.5
        result['conviction'] = conviction

        # TIER 2B: position_size (0.0–1.0)
        position_size = result.get('position_size', 0.3)
        try:
            position_size = max(0.0, min(1.0, float(position_size)))
        except (TypeError, ValueError):
            logger.debug(f"position_size missing/invalid for {agent.get('uuid')}, defaulting to 0.3")
            position_size = 0.3
        result['position_size'] = position_size

        return result
    except Exception as e:
        logger.warning(f"LLM call failed for agent {agent.get('uuid')}: {e}")
        return None


# ---------------------------------------------------------------------------
# Panic contagion helpers (TIER 2D-i)
# ---------------------------------------------------------------------------

def _write_contagion_flag(tick_reactions: list) -> None:
    """
    Check per-archetype panic rate from this tick's reactions.
    If any archetype had > 50% of agents panic, write CONTAGION_FLAG_PATH so
    the NEXT tick can inject the alert into high-herd agents (herd_behaviour >= 7).
    If no archetype exceeded the threshold, delete the flag (if it exists).

    This simulates cascade propagation: a retail panic wave visible to highly
    herd-susceptible agents before they form their own reaction.
    """
    from collections import defaultdict

    arch_totals: dict = defaultdict(int)
    arch_panics: dict = defaultdict(int)

    for r in tick_reactions:
        arch = r.get('archetype', 'unknown')
        arch_totals[arch] += 1
        if r.get('reaction') == 'panic':
            arch_panics[arch] += 1

    # Find any archetype with panic_rate > 0.5
    contagion_lines = []
    for arch, total in arch_totals.items():
        if total == 0:
            continue
        panic_rate = arch_panics[arch] / total
        if panic_rate > 0.5:
            pct = round(panic_rate * 100, 1)
            contagion_lines.append(
                f"CONTAGION ALERT: {pct}% of {arch.replace('_', ' ')} investors "
                f"panicked in the last tick."
            )

    if contagion_lines:
        flag_text = (
            "CONTAGION WARNING — Panic has spread through part of the market:\n"
            + "\n".join(contagion_lines)
            + "\nIf you have high herd behaviour, you may feel the urge to follow."
        )
        try:
            CONTAGION_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONTAGION_FLAG_PATH.write_text(flag_text, encoding='utf-8')
            logger.info(f"Contagion flag written: {'; '.join(contagion_lines)}")
        except Exception as e:
            logger.warning(f"Contagion flag write failed: {e}")
    else:
        # No contagion this tick — delete stale flag if present
        if CONTAGION_FLAG_PATH.exists():
            try:
                CONTAGION_FLAG_PATH.unlink()
                logger.debug("Contagion flag cleared (no panic threshold exceeded)")
            except Exception as e:
                logger.warning(f"Contagion flag delete failed: {e}")


# ---------------------------------------------------------------------------
# Main tick
# ---------------------------------------------------------------------------

def run_tick(driver, graph_id: str, briefing_path: str, full: bool = False,
            checkpoint_path: Path = CHECKPOINT_PATH, no_memory: bool = False) -> None:
    llm = LLMClient()
    embedding_svc = EmbeddingService()

    # --- Panic contagion injection (TIER 2D-i) ---
    # If a contagion flag was written by the previous tick, consume it now.
    # Only injected into high-herd agents (herd_behaviour >= 7) in the agent loop below.
    contagion_context = ''
    if CONTAGION_FLAG_PATH.exists():
        try:
            contagion_context = CONTAGION_FLAG_PATH.read_text(encoding='utf-8').strip()
            CONTAGION_FLAG_PATH.unlink()
            logger.info(f"Contagion flag consumed: {contagion_context[:80]}")
        except Exception as e:
            logger.warning(f"Contagion flag read/delete failed: {e}")
            contagion_context = ''

    # --- Build chunk cache BEFORE agent loop ---
    briefing_text = Path(briefing_path).read_text(encoding='utf-8')
    briefing_text = TextProcessor.preprocess_text(briefing_text)
    chunks = TextProcessor.split_text(briefing_text,
                                      chunk_size=Config.DEFAULT_CHUNK_SIZE,
                                      overlap=Config.DEFAULT_CHUNK_OVERLAP)
    chunk_texts = chunks

    print(f"Embedding {len(chunk_texts)} briefing chunks...")
    try:
        chunk_embeddings = embedding_svc.embed_batch(chunk_texts)
    except Exception as e:
        logger.warning(f"Chunk embedding failed: {e} — proceeding with empty cache")
        chunk_embeddings = [[] for _ in chunk_texts]

    chunk_cache = list(zip(chunk_texts, chunk_embeddings))
    print(f"Briefing indexed: {len(chunk_cache)} chunks cached")

    # --- Load agents ---
    if full:
        agents = load_agents(driver, graph_id, sample_size=None)
        print(f"Loaded {len(agents)} agents for full tick (all agents)")
    else:
        agents = load_agents_stratified(driver, graph_id, SAMPLE_SIZE)
        print(f"Loaded {len(agents)} agents for tick (stratified sample)")

    # --- Checkpoint recovery ---
    briefing_source = str(Path(briefing_path).name)
    already_processed = load_checkpoint(checkpoint_path, briefing_source)
    agents = [a for a in agents if a['uuid'] not in already_processed]
    print(f"Resuming: {len(already_processed)} already processed, {len(agents)} remaining")

    processed_ids = list(already_processed)
    errors = 0
    # Accumulate reaction data for the SentimentSnapshot written after the loop
    tick_reactions: list = []

    # --- Load current prices once (reused for position P&L and position updates) ---
    current_prices = _load_current_prices()
    if current_prices:
        logger.info(f"Loaded current prices for {len(current_prices)} assets")
    else:
        logger.info("No price file found — positions will show without P&L")

    # --- Time-horizon gating (30-min tick only) ---
    # Participation probability = min(1.0, 30 / reaction_speed_minutes).
    # Agents who react over hours/days are unlikely to have acted within a 30-min window.
    # The daily full tick runs all agents regardless.
    active_count = 0
    sampled_count = len(agents)

    for i, agent in enumerate(agents):
        if not full:
            rsm = agent.get('reaction_speed_minutes')
            try:
                rsm_f = float(rsm) if rsm is not None else 30.0
            except (TypeError, ValueError):
                rsm_f = 30.0
            rsm_f = max(1.0, rsm_f)
            participation_prob = min(1.0, 30.0 / rsm_f)
            if random.random() >= participation_prob:
                continue  # gated out — counted in sample but no LLM call

        active_count += 1

        # Fetch current portfolio positions for this agent (TIER 2A)
        agent_positions: dict = {}
        try:
            agent_positions = load_agent_positions(driver, agent['uuid'], graph_id)
        except Exception as e:
            logger.warning(f"Position fetch failed for {agent['uuid']}: {e}")

        # Build positions string for prompt injection
        positions_block = format_positions_block(agent_positions, current_prices)

        # Fetch agent's recent reaction history (last 5 events)
        if no_memory:
            memory = []
        else:
            try:
                memory = load_agent_recent_memory(driver, agent['uuid'])
            except Exception as e:
                logger.warning(f"Memory load failed for {agent['uuid']}: {e}")
                memory = []

        # Select top-5 chunks relevant to this agent
        query_str = build_agent_query(agent)
        try:
            query_emb = embedding_svc.embed(query_str)
            relevant_chunks = top_k_chunks(query_emb, chunk_cache, k=TOP_K_CHUNKS)
        except Exception as e:
            logger.warning(f"Query embedding failed for agent {agent['uuid']}: {e}")
            relevant_chunks = [c[0] for c in chunk_cache[:TOP_K_CHUNKS]]

        # TIER 2D-i: only inject contagion context for high-herd agents
        try:
            herd_val = float(agent.get('herd_behaviour', 0) or 0)
        except (TypeError, ValueError):
            herd_val = 0.0
        agent_contagion = contagion_context if herd_val >= 7.0 else ''

        # LLM reaction (with position context injected)
        reaction = call_llm_for_agent(llm, agent, memory, relevant_chunks,
                                      positions_block=positions_block,
                                      contagion_context=agent_contagion)
        if reaction is None:
            errors += 1
            continue  # skip processed_ids append

        write_success = True
        try:
            write_memory_event(driver, agent['uuid'], briefing_source, reaction)
        except Exception as e:
            logger.warning(f"Memory write failed for {agent['uuid']}: {e} — agent will be retried on resume")
            errors += 1
            write_success = False

        if write_success:
            # Update agent's position state in Neo4j (TIER 2A)
            try:
                update_agent_positions(
                    driver,
                    agent_uuid=agent['uuid'],
                    graph_id=graph_id,
                    reaction=reaction.get('reaction', 'hold'),
                    assets_mentioned=reaction.get('assets_mentioned', []),
                    current_prices=current_prices,
                    existing_positions=agent_positions,
                    asset_class_bias=agent.get('asset_class_bias', ''),
                )
            except Exception as e:
                logger.warning(f"Position update failed for {agent['uuid']}: {e}")

            processed_ids.append(agent['uuid'])
            # Accumulate for SentimentSnapshot and contagion analysis
            tick_reactions.append({
                'asset_class':      agent.get('asset_class_bias'),
                'reaction':         reaction.get('reaction', 'hold'),
                'confidence':       reaction.get('confidence', 5.0),
                'capital':          agent.get('capital_usd', 0),
                'leverage_typical': agent.get('leverage_typical', 'none'),
                'archetype':        agent.get('investor_archetype', 'unknown'),
            })

        # Checkpoint every CHECKPOINT_INTERVAL agents
        if (i + 1) % CHECKPOINT_INTERVAL == 0:
            write_checkpoint(checkpoint_path, briefing_source, processed_ids)
            print(f"  Checkpoint: {i+1}/{len(agents)} agents processed ({errors} errors)")

    # Log time-horizon gating result (30-min tick only)
    if not full:
        logger.info(
            f"Tick participation: {active_count}/{sampled_count} agents active after time-horizon gating"
        )

    # Clean up checkpoint on success
    if checkpoint_path.exists():
        checkpoint_path.unlink()

    # Write SentimentSnapshot for this tick to Neo4j
    # This powers the tick-level sentiment history chart on the dashboard.
    # Do not remove — removing this breaks the tick-level chart in dashboard.html.
    try:
        write_sentiment_snapshot(driver, graph_id, tick_reactions)
    except Exception as e:
        logger.warning(f"SentimentSnapshot write failed: {e} — tick data will not appear in history chart")

    # TIER 2D-i: Panic contagion — compute per-archetype panic rate.
    # If any archetype had >50% panic, write a contagion flag for the NEXT tick.
    # Only agents with herd_behaviour >= 7 will receive this context (consumed in run_tick start above).
    _write_contagion_flag(tick_reactions)

    print(f"Tick complete: {active_count}/{sampled_count} agents active, {errors} errors")


def main():
    parser = argparse.ArgumentParser(description="Run MiroFish simulation tick")
    parser.add_argument('--briefing', required=True, help="Path to briefing .txt file")
    parser.add_argument('--graph-id', help="Neo4j graph_id (auto-detected if omitted)")
    parser.add_argument('--full', action='store_true', help="Process all agents (no sampling)")
    parser.add_argument('--no-memory', action='store_true', help="Skip memory fetch (useful for first runs with no history)")
    args = parser.parse_args()

    driver = db_setup()
    try:
        graph_id = args.graph_id
        if not graph_id:
            from incremental_update import resolve_active_graph_id
            graph_id = resolve_active_graph_id()
        print(f"Using graph_id: {graph_id}")
        run_tick(driver, graph_id, args.briefing, full=args.full, no_memory=args.no_memory)
    finally:
        driver.close()


if __name__ == "__main__":
    main()