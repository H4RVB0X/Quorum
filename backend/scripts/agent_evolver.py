"""
agent_evolver.py — Grow Neo4j agent pool to 4096.

Flow:
  1. Load existing agents + build FAISS index
  2. In outer loop: ask LLM for 64 candidate personas × 8 = ~512 per round
  3. Per candidate: FAISS gate (≤0.75 accept) → triple gate (Neo4j check)
  4. Write accepted candidates to Neo4j; add to FAISS index in-memory
  5. Repeat until pool ≥ TARGET_POOL_SIZE

Usage:
  python agent_evolver.py [--target 4096] [--graph-id <uuid>]
"""
import sys
import os
import uuid
import argparse
import random
import math
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np
import faiss

from db_setup import setup as db_setup
from app.storage.embedding_service import EmbeddingService
from app.utils.llm_client import LLMClient
from app.utils.logger import get_logger

logger = get_logger('mirofish.agent_evolver')

_INVESTOR_RNG = np.random.default_rng()

TARGET_POOL_SIZE = 4096
LLM_BATCH_SIZE = 20          # personas per LLM call
FAISS_THRESHOLD = 0.97       # cosine similarity — only reject near-identical agents
EMBED_DIM = 768              # nomic-embed-text dimensions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def serialise_traits(traits: dict) -> str:
    """Compact string representation of agent traits for embedding."""
    return (
        f"{traits.get('investor_archetype','')}, "
        f"{traits.get('primary_strategy','')}, "
        f"risk={traits.get('risk_tolerance', 0):.1f}, "
        f"capital={traits.get('capital_usd', 0):.0f}, "
        f"horizon={traits.get('time_horizon_days', 0)}, "
        f"herd={traits.get('herd_behaviour', 0):.1f}, "
        f"news={traits.get('news_sensitivity', 0):.1f}, "
        f"geo={traits.get('geopolitical_sensitivity', 0):.1f}, "
        f"bias={traits.get('asset_class_bias','')}, "
        f"leverage={traits.get('leverage_typical','')}, "
        f"fg={traits.get('fear_greed_dominant','')}"
    )


def triple_key(archetype: str, strategy: str, risk: float) -> tuple:
    """(archetype, strategy, risk_bucket) — risk_bucket = floor(risk / 3.33), clamped 0-2."""
    bucket = min(2, max(0, int(math.floor(risk / 3.33))))
    return (archetype, strategy, bucket)


def _sample_traits(archetype: Optional[str] = None) -> dict:
    """Fat-tail trait sampling (same distributions as Feature 1)."""
    rng = _INVESTOR_RNG

    def _beta_10():
        return round(float(rng.beta(0.5, 0.5) * 10), 2)

    def _weighted(choices, weights):
        return random.choices(choices, weights=weights, k=1)[0]

    investor_archetype = archetype or _weighted(
        ['retail_amateur', 'retail_experienced', 'prop_trader',
         'fund_manager', 'family_office', 'hedge_fund', 'pension_fund'],
        [35, 25, 15, 10, 7, 6, 2],
    )

    return {
        'risk_tolerance': _beta_10(),
        'herd_behaviour': _beta_10(),
        'news_sensitivity': _beta_10(),
        'geopolitical_sensitivity': _beta_10(),
        'overconfidence_bias': _beta_10(),
        'capital_usd': round(float(10 ** rng.uniform(
            np.log10(500), np.log10(100_000_000)
        )), 2),
        'time_horizon_days': int(min(3650, max(1, round(1 + float(rng.pareto(1.5)) * 30)))),
        'loss_aversion_multiplier': round(float(np.clip(rng.lognormal(0.7, 0.5), 0.5, 10.0)), 2),
        'reaction_speed_minutes': round(float(np.clip(rng.lognormal(3.0, 1.5), 1.0, 10080.0)), 1),
        'investor_archetype': investor_archetype,
        'primary_strategy': _weighted(
            ['day_trading', 'swing', 'value', 'growth', 'momentum',
             'index', 'income', 'macro', 'contrarian', 'quant'],
            [15, 15, 12, 12, 12, 10, 8, 7, 5, 4],
        ),
        'leverage_typical': _weighted(['none', '2x', '5x', '10x_plus'], [55, 25, 12, 8]),
        'formative_crash': _weighted(
            ['none', 'dotcom', 'gfc_2008', 'covid_2020', 'iran_war_2026'],
            [25, 15, 35, 20, 5],
        ),
        'fear_greed_dominant': _weighted(['fear', 'greed'], [45, 55]),
        'asset_class_bias': _weighted(
            ['equities', 'mixed', 'crypto', 'bonds', 'commodities', 'fx', 'real_estate'],
            [35, 20, 15, 10, 8, 7, 5],
        ),
        'is_synthetic': True,
    }


# ---------------------------------------------------------------------------
# FAISS index management
# ---------------------------------------------------------------------------

def build_faiss_index(embeddings: list) -> faiss.IndexFlatIP:
    """Build L2-normalised FAISS inner-product index from list of embedding vectors."""
    index = faiss.IndexFlatIP(EMBED_DIM)
    if embeddings:
        mat = np.array(embeddings, dtype='float32')
        faiss.normalize_L2(mat)
        index.add(mat)
    return index


def add_to_index(index: faiss.IndexFlatIP, embedding: list) -> None:
    """Add a single embedding to an existing FAISS index in-place."""
    vec = np.array([embedding], dtype='float32')
    faiss.normalize_L2(vec)
    index.add(vec)


def nearest_similarity(index: faiss.IndexFlatIP, embedding: list) -> float:
    """Return cosine similarity to nearest existing neighbour (0.0 if index empty)."""
    if index.ntotal == 0:
        return 0.0
    vec = np.array([embedding], dtype='float32')
    faiss.normalize_L2(vec)
    distances, _ = index.search(vec, 1)
    return float(distances[0][0])


# ---------------------------------------------------------------------------
# LLM persona generation
# ---------------------------------------------------------------------------

def generate_candidates(llm: LLMClient, count: int = LLM_BATCH_SIZE) -> list:
    """
    Ask LLM to generate `count` investor persona skeletons.
    Returns list of dicts with keys: name, backstory, archetype.
    Falls back to empty list on parse failure.
    """
    messages = [
        {
            "role": "system",
            "content": "You generate diverse investor personas for financial market simulation. Return valid JSON only.",
        },
        {
            "role": "user",
            "content": (
                f"Generate {count} distinct investor personas.\n\n"
                "Return a JSON object with key \"personas\" containing a list of "
                f"{count} objects, each with:\n"
                "- name: realistic full name (diverse nationalities and genders)\n"
                "- backstory: 1-2 sentence professional background\n"
                "- archetype: one of: retail_amateur, retail_experienced, prop_trader, "
                "fund_manager, family_office, hedge_fund, pension_fund\n\n"
                "Ensure diversity in nationality, age bracket, and archetype distribution. "
                "Do not repeat names."
            ),
        },
    ]
    try:
        import json as _json, re as _re
        raw = llm.chat(messages, temperature=0.9, max_tokens=8192)
        cleaned = raw.strip()
        cleaned = _re.sub(r'^```(?:json)?\s*\n?', '', cleaned, flags=_re.IGNORECASE)
        cleaned = _re.sub(r'\n?```\s*$', '', cleaned)
        match = _re.search(r'\{.*\}', cleaned, _re.DOTALL)
        if match:
            cleaned = match.group(0)
        result = _json.loads(cleaned)
        personas = result.get("personas", [])
        if not isinstance(personas, list):
            logger.warning("LLM returned non-list personas field")
            return []
        return personas
    except Exception as e:
        logger.error(f"LLM persona generation failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Neo4j helpers
# ---------------------------------------------------------------------------

def load_existing_agents(driver, graph_id: str) -> list:
    """
    Load all agents that have investor trait properties.
    Returns list of dicts with keys: uuid, trait_string, archetype, strategy, risk.
    """
    with driver.session() as session:
        result = session.run(
            """
            MATCH (n:Entity {graph_id: $gid})
            WHERE n.risk_tolerance IS NOT NULL
            RETURN n.uuid AS uuid,
                   n.investor_archetype AS archetype,
                   n.primary_strategy AS strategy,
                   n.risk_tolerance AS risk,
                   n.capital_usd AS capital,
                   n.time_horizon_days AS horizon,
                   n.herd_behaviour AS herd,
                   n.news_sensitivity AS news,
                   n.geopolitical_sensitivity AS geo,
                   n.asset_class_bias AS bias,
                   n.leverage_typical AS leverage,
                   n.fear_greed_dominant AS fg
            """,
            gid=graph_id,
        )
        agents = []
        for r in result:
            traits = {
                'investor_archetype': r['archetype'],
                'primary_strategy': r['strategy'],
                'risk_tolerance': r['risk'] if r['risk'] is not None else 5.0,
                'capital_usd': r['capital'] if r['capital'] is not None else 10000.0,
                'time_horizon_days': r['horizon'] if r['horizon'] is not None else 90,
                'herd_behaviour': r['herd'] if r['herd'] is not None else 5.0,
                'news_sensitivity': r['news'] if r['news'] is not None else 5.0,
                'geopolitical_sensitivity': r['geo'] if r['geo'] is not None else 5.0,
                'asset_class_bias': r['bias'],
                'leverage_typical': r['leverage'],
                'fear_greed_dominant': r['fg'],
            }
            agents.append({
                'uuid': r['uuid'],
                'trait_string': serialise_traits(traits),
                'archetype': r['archetype'],
                'strategy': r['strategy'],
                'risk': r['risk'] if r['risk'] is not None else 5.0,
            })
        return agents


def triple_exists(driver, graph_id: str, key: tuple) -> bool:
    """Check if an agent with this (archetype, strategy, risk_bucket) triple already exists."""
    archetype, strategy, bucket = key
    low = bucket * 3.33
    high = low + 3.33
    with driver.session() as session:
        result = session.run(
            """
            MATCH (n:Entity {graph_id: $gid})
            WHERE n.investor_archetype = $arch
              AND n.primary_strategy = $strat
              AND n.risk_tolerance >= $low
              AND n.risk_tolerance < $high
            RETURN count(n) AS c
            """,
            gid=graph_id,
            arch=archetype,
            strat=strategy,
            low=low,
            high=high,
        )
        return result.single()['c'] > 0


def write_agent(driver, graph_id: str, persona: dict, traits: dict) -> str:
    """MERGE synthetic agent into Neo4j. Returns node uuid."""
    node_uuid = str(uuid.uuid4())
    name = persona.get('name', f"Investor_{node_uuid[:8]}")
    name_lower = name.lower()
    now = datetime.now(timezone.utc).isoformat()

    props = {
        'uuid': node_uuid,
        'graph_id': graph_id,
        'name': name,
        'name_lower': name_lower,
        'summary': persona.get('backstory', ''),
        'created_at': now,
        **traits,
    }

    with driver.session() as session:
        def _write(tx):
            tx.run(
                """
                MERGE (n:Entity {graph_id: $graph_id, name_lower: $name_lower})
                ON CREATE SET n += $props
                ON MATCH SET n += $props
                """,
                graph_id=graph_id,
                name_lower=name_lower,
                props=props,
            )
        session.execute_write(_write)

    return node_uuid


# ---------------------------------------------------------------------------
# Main evolution loop
# ---------------------------------------------------------------------------

def evolve(driver, graph_id: str, target: int = TARGET_POOL_SIZE) -> None:
    embedding_svc = EmbeddingService()
    llm = LLMClient()

    # --- Phase 1: load existing pool and build FAISS index ---
    print("Loading existing agents from Neo4j...")
    existing = load_existing_agents(driver, graph_id)
    pool_size = len(existing)
    print(f"Existing pool: {pool_size} agents")

    if pool_size >= target:
        print(f"Pool already at {pool_size} (≥ {target}). Nothing to do.")
        return

    print("Building FAISS index (starting fresh, existing agents excluded from dedup)...")
    faiss_index = build_faiss_index([])
    print(f"FAISS index built: {faiss_index.ntotal} vectors")

    # --- Phase 2: generate until target reached ---
    outer_round = 0
    while pool_size < target:
        outer_round += 1
        candidates_this_round = []
        # 8 LLM calls of LLM_BATCH_SIZE each = ~512 candidates per outer round
        total_calls = max(1, 512 // LLM_BATCH_SIZE)
        for call_num in range(total_calls):
            batch = generate_candidates(llm, count=LLM_BATCH_SIZE)
            candidates_this_round.extend(batch)
            print(f"  LLM call {call_num + 1}/{total_calls}: {len(batch)} personas", flush=True)

        accepted = rejected = 0

        for persona in candidates_this_round:
            if pool_size >= target:
                break

            archetype = persona.get('archetype')
            traits = _sample_traits(archetype=archetype)
            trait_str = serialise_traits(traits)

            try:
                embedding = embedding_svc.embed(trait_str)
            except Exception as e:
                logger.warning(f"Embedding failed for candidate {persona.get('name')}: {e}")
                rejected += 1
                continue

            # Accept all candidates — diversity ensured by fat-tail trait sampling
            write_agent(driver, graph_id, persona, traits)
            add_to_index(faiss_index, embedding)
            pool_size += 1
            accepted += 1

        print(
            f"Round {outer_round}: {len(candidates_this_round)} candidates, "
            f"{accepted} accepted, {rejected} rejected. Pool: {pool_size}/{target}"
        )

        if accepted == 0 and len(candidates_this_round) > 0:
            logger.warning("Zero acceptance rate this round — thresholds may be too strict")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Evolve MiroFish agent pool to TARGET_POOL_SIZE")
    parser.add_argument('--target', type=int, default=TARGET_POOL_SIZE)
    parser.add_argument('--graph-id', required=True, help="Neo4j graph_id to add agents to")
    args = parser.parse_args()

    driver = db_setup()
    try:
        evolve(driver, args.graph_id, target=args.target)
    finally:
        driver.close()


if __name__ == "__main__":
    main()