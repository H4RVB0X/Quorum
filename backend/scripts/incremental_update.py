"""
incremental_update.py — Process a briefing .txt file into the Neo4j knowledge graph.

Extracts entities via spaCy NER (en_core_web_sm) and MERGEs them into Neo4j using
batched UNWIND queries (batch size 500). This replaces the previous LLM-based NER
pipeline, which made one Ollama call per chunk and took 6–8 hours for a 2160-chunk
briefing. spaCy brings that to under 2 minutes.

Never overwrites properties on synthetic agent nodes (is_synthetic=True).

Usage:
  python incremental_update.py --briefing /path/to/briefing.txt [--graph-id <uuid>]
"""
import sys
import os
import json
import uuid
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Tuple

import spacy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from db_setup import setup as db_setup
from app.config import Config
from app.services.text_processor import TextProcessor
from app.utils.logger import get_logger

logger = get_logger('mirofish.incremental_update')

# ---------------------------------------------------------------------------
# spaCy model — loaded once at module level to avoid cold-start per call
# ---------------------------------------------------------------------------
nlp = spacy.load("en_core_web_sm")

# spaCy label → application entity type
SPACY_LABEL_MAP: Dict[str, str] = {
    "ORG":     "Company",
    "PERSON":  "Person",
    "GPE":     "Location",
    "FAC":     "Location",
    "MONEY":   "FinancialFigure",
    "PERCENT": "FinancialFigure",
    "PRODUCT": "Product",
    "EVENT":   "Event",
    "NORP":    "Group",
}

# Neo4j UNWIND batch size — keeps payload under Neo4j's recommended limits
BATCH_SIZE = 500

# ---------------------------------------------------------------------------
# Cypher queries
# ---------------------------------------------------------------------------

# Batched entity upsert. Guards is_synthetic so agent nodes are never overwritten.
# central_bank_source is set true if ANY chunk contributing this entity was from
# a central bank feed; once true it is never cleared (OR logic in ON MATCH).
BATCH_MERGE_ENTITY_QUERY = """
UNWIND $entities AS e
MERGE (n:Entity {graph_id: $gid, name_lower: e.name_lower})
ON CREATE SET
    n.uuid                = e.uuid,
    n.name                = e.name,
    n.type                = e.type,
    n.summary             = e.summary,
    n.created_at          = $now,
    n.last_seen           = datetime(),
    n.is_synthetic        = false,
    n.central_bank_source = e.central_bank_source
ON MATCH SET
    n.last_seen           = CASE WHEN n.is_synthetic THEN n.last_seen ELSE datetime() END,
    n.type                = CASE WHEN n.is_synthetic THEN n.type   ELSE e.type        END,
    n.central_bank_source = CASE
        WHEN n.is_synthetic        THEN n.central_bank_source
        WHEN e.central_bank_source THEN true
        ELSE n.central_bank_source
    END
"""

# Exposed so tests can inspect
MERGE_ENTITY_QUERY = BATCH_MERGE_ENTITY_QUERY


# ---------------------------------------------------------------------------
# Graph ID resolution
# ---------------------------------------------------------------------------

def resolve_active_graph_id(projects_dir: str = None) -> str:
    """Find the graph_id from the most recently completed project."""
    if projects_dir is None:
        projects_dir = os.path.join(
            os.path.dirname(__file__), '..', 'uploads', 'projects'
        )

    projects_path = Path(projects_dir)
    if not projects_path.exists():
        raise FileNotFoundError(f"Projects directory not found: {projects_dir}")

    candidates = []
    for proj_dir in projects_path.iterdir():
        proj_json = proj_dir / "project.json"
        if proj_json.exists():
            with open(proj_json) as f:
                data = json.load(f)
            if data.get("status") == "GRAPH_COMPLETED" and data.get("graph_id"):
                mtime = proj_json.stat().st_mtime
                candidates.append((mtime, data["graph_id"]))

    if not candidates:
        raise ValueError("No completed graph project found in projects directory")

    candidates.sort(reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# spaCy entity extraction
# ---------------------------------------------------------------------------

def extract_entities_spacy(
    chunk_text: str,
    is_central_bank: bool = False,
) -> List[Dict[str, Any]]:
    """
    Extract named entities from one text chunk using spaCy.

    Filters:
    - Label must be in SPACY_LABEL_MAP (ORG, PERSON, GPE, …)
    - Name must be at least 2 characters
    - Name must not be purely numeric (e.g. bare years like "2024" are dropped)

    Deduplicates within the chunk by (name.lower(), type).

    Args:
        chunk_text:      Raw text of the chunk.
        is_central_bank: If True, marks every entity as central_bank_source=True.
                         NOTE: The current briefing format does not expose chunk-level
                         source metadata, so this always arrives as False from
                         process_briefing(). The parameter is retained for when
                         chunk-level source_type metadata is added.

    Returns:
        List of dicts: [{"name", "type", "central_bank_source"}]
    """
    doc = nlp(chunk_text)
    seen: set = set()
    entities: List[Dict[str, Any]] = []

    for ent in doc.ents:
        label = ent.label_
        if label not in SPACY_LABEL_MAP:
            continue

        name = ent.text.strip()
        if len(name) < 2:
            continue
        if name.isnumeric():
            continue

        etype = SPACY_LABEL_MAP[label]
        key: Tuple[str, str] = (name.lower(), etype)
        if key in seen:
            continue
        seen.add(key)

        entities.append({
            "name": name,
            "type": etype,
            "central_bank_source": is_central_bank,
        })

    return entities


# ---------------------------------------------------------------------------
# Batched Neo4j writes
# ---------------------------------------------------------------------------

def batch_merge_entities(
    session,
    graph_id: str,
    entities: List[Dict[str, Any]],
    now: str,
) -> int:
    """
    UNWIND-merge a list of entities into Neo4j in batches of BATCH_SIZE.

    Returns the total count of records sent (includes duplicates that Neo4j
    resolves via MERGE; the actual new-node count may be lower).
    """
    payload = [
        {
            "uuid":                str(uuid.uuid4()),
            "name":                e["name"],
            "name_lower":          e["name"].lower(),
            "type":                e["type"],
            "summary":             f"{e['name']} ({e['type']})",
            "central_bank_source": bool(e.get("central_bank_source", False)),
        }
        for e in entities
    ]

    total = 0
    for i in range(0, len(payload), BATCH_SIZE):
        batch = payload[i : i + BATCH_SIZE]
        session.run(BATCH_MERGE_ENTITY_QUERY, entities=batch, gid=graph_id, now=now)
        total += len(batch)

    return total


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_briefing(driver, graph_id: str, briefing_path: str) -> None:
    t_start = time.time()

    # Read and preprocess briefing
    text = Path(briefing_path).read_text(encoding='utf-8')
    text = TextProcessor.preprocess_text(text)
    chunks = TextProcessor.split_text(
        text,
        chunk_size=Config.DEFAULT_CHUNK_SIZE,
        overlap=Config.DEFAULT_CHUNK_OVERLAP,
    )
    total_chunks = len(chunks)

    # ------------------------------------------------------------------
    # Phase 1: extract entities from every chunk via spaCy (zero LLM calls)
    # Global deduplication: same (name.lower(), type) = one entity node.
    # central_bank_source is OR-ed: if any chunk marks it true, it stays true.
    # ------------------------------------------------------------------
    global_entities: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for chunk in chunks:
        for ent in extract_entities_spacy(chunk):
            key = (ent["name"].lower(), ent["type"])
            if key not in global_entities:
                global_entities[key] = ent
            elif ent["central_bank_source"]:
                global_entities[key]["central_bank_source"] = True

    unique_entities = list(global_entities.values())
    logger.info(
        f"Extracted {len(unique_entities)} entities from {total_chunks} chunks (spaCy)"
    )

    if not unique_entities:
        logger.info(f"No entities found in {briefing_path}")
        return

    # ------------------------------------------------------------------
    # Phase 2: batch-write entities to Neo4j via UNWIND
    # ------------------------------------------------------------------
    now = datetime.now(timezone.utc).isoformat()

    with driver.session() as session:
        merged = batch_merge_entities(session, graph_id, unique_entities, now)

    elapsed = time.time() - t_start
    logger.info(f"Merged {merged} unique entities into Neo4j")
    logger.info(f"Briefing processed in {elapsed:.1f}s: {briefing_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Merge briefing into Neo4j knowledge graph"
    )
    parser.add_argument('--briefing', required=True, help="Path to briefing .txt file")
    parser.add_argument(
        '--graph-id', help="Neo4j graph_id (auto-detected if omitted)"
    )
    args = parser.parse_args()

    driver = db_setup()
    try:
        graph_id = (
            args.graph_id if args.graph_id is not None else resolve_active_graph_id()
        )
        print(f"Using graph_id: {graph_id}")
        process_briefing(driver, graph_id, args.briefing)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
