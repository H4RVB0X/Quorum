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
"""
import sys
import os
import json
import uuid
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

CHECKPOINT_PATH = Path(__file__).parent / "tick_checkpoint.json"
CHECKPOINT_INTERVAL = 100
SAMPLE_SIZE = 500
TOP_K_CHUNKS = 5
MEMORY_WINDOW_DAYS = 7

VALID_REACTIONS = {'buy', 'hold', 'sell', 'panic', 'hedge'}


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

def load_agents(driver, graph_id: str, sample_size: Optional[int]) -> list:
    """Load agents with investor trait profiles. If sample_size is None, load all."""
    limit_clause = f"LIMIT {sample_size}" if sample_size else ""
    with driver.session() as session:
        result = session.run(
            f"""
            MATCH (n:Entity {{graph_id: $gid}})
            WHERE n.risk_tolerance IS NOT NULL
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
            ORDER BY rand()
            {limit_clause}
            """,
            gid=graph_id,
        )
        return [dict(r) for r in result]


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


def write_memory_event(driver, agent_uuid: str, briefing_source: str, reaction_data: dict) -> None:
    """Create a :MemoryEvent node and link it to the agent."""
    event_uuid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

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
                    assets_mentioned: $assets
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


def build_prompt(agent: dict, memory_events: list, context_chunks: list) -> list:
    """Build messages list for LLMClient.chat_json()."""
    name = agent.get('name', 'Unknown')
    archetype = agent.get('investor_archetype', 'general')
    capital = agent.get('capital_usd', 'unknown')
    risk = agent.get('risk_tolerance', 'unknown')
    strategy = agent.get('primary_strategy', 'unknown')
    fear_greed = agent.get('fear_greed_dominant', 'unknown')
    herd = agent.get('herd_behaviour', 'unknown')
    sensitivity = agent.get('news_sensitivity', 'unknown')

    persona_block = (
        f"You are {name}, a {archetype} investor.\n"
        f"Capital: {capital}. Risk tolerance: {risk}/10.\n"
        f"Strategy: {strategy}. Fear/greed: {fear_greed}.\n"
        f"Herd behaviour: {herd}/10. News sensitivity: {sensitivity}/10."
    )

    memory_block = format_memory_block(memory_events)

    system_parts = [
        "You are modelling an investor's reaction to financial news. "
        "Return only valid JSON with keys: reaction, confidence, reasoning, assets_mentioned.",
        persona_block,
    ]
    if memory_block:
        system_parts.append(memory_block)

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
                "Based on your profile and the news above, return JSON:\n"
                '{"reaction": "buy|hold|sell|panic|hedge", '
                '"confidence": <0-10 float>, '
                '"reasoning": "<1-2 sentences>", '
                '"assets_mentioned": ["<ticker or asset name>", ...]}'
            ),
        },
    ]


def call_llm_for_agent(llm: LLMClient, agent: dict, memory: list, chunks: list) -> Optional[dict]:
    """Returns validated reaction dict, or None on failure."""
    messages = build_prompt(agent, memory, chunks)
    try:
        result = llm.chat_json(messages, temperature=0.7, max_tokens=512)
        # Validate reaction field
        if result.get('reaction') not in VALID_REACTIONS:
            result['reaction'] = 'hold'
        result['confidence'] = max(0.0, min(10.0, float(result.get('confidence', 5.0))))
        if not isinstance(result.get('assets_mentioned'), list):
            result['assets_mentioned'] = []
        return result
    except Exception as e:
        logger.warning(f"LLM call failed for agent {agent.get('uuid')}: {e}")
        return None


# ---------------------------------------------------------------------------
# Main tick
# ---------------------------------------------------------------------------

def run_tick(driver, graph_id: str, briefing_path: str, full: bool = False, checkpoint_path: Path = CHECKPOINT_PATH, no_memory: bool = False) -> None:
    llm = LLMClient()
    embedding_svc = EmbeddingService()

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
    sample_size = None if full else SAMPLE_SIZE
    agents = load_agents(driver, graph_id, sample_size)
    print(f"Loaded {len(agents)} agents for tick")

    # --- Checkpoint recovery ---
    briefing_source = str(Path(briefing_path).name)
    already_processed = load_checkpoint(checkpoint_path, briefing_source)
    agents = [a for a in agents if a['uuid'] not in already_processed]
    print(f"Resuming: {len(already_processed)} already processed, {len(agents)} remaining")

    processed_ids = list(already_processed)
    errors = 0

    for i, agent in enumerate(agents):
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

        # LLM reaction
        reaction = call_llm_for_agent(llm, agent, memory, relevant_chunks)
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
            processed_ids.append(agent['uuid'])

        # Checkpoint every CHECKPOINT_INTERVAL agents
        if (i + 1) % CHECKPOINT_INTERVAL == 0:
            write_checkpoint(checkpoint_path, briefing_source, processed_ids)
            print(f"  Checkpoint: {i+1}/{len(agents)} agents processed ({errors} errors)")

    # Clean up checkpoint on success
    if checkpoint_path.exists():
        checkpoint_path.unlink()

    print(f"Tick complete: {len(agents)} agents processed, {errors} errors")


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
