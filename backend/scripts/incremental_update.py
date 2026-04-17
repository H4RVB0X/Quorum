"""
incremental_update.py — Process a briefing .txt file into the Neo4j knowledge graph.

Extracts entities via spaCy NER (en_core_web_sm), then writes:
    - Entity nodes (:Entity)
    - Chunk nodes (:NewsChunk)
    - Entity→chunk links (:MENTIONED_IN)
    - Entity↔entity co-occurrence links (:MENTIONED_WITH, weighted)

All writes use batched UNWIND queries (batch size 500). This keeps the speed
advantage over the previous LLM-per-chunk NER pipeline while restoring graph edges.

Never overwrites properties on synthetic agent nodes (is_synthetic=True).

Usage:
  python incremental_update.py --briefing /path/to/briefing.txt [--graph-id <uuid>]
"""
import sys
import os
import json
import uuid
import time
import hashlib
import argparse
from datetime import datetime, timezone
from pathlib import Path
from itertools import combinations
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

# Post-processing corrections for entities spaCy commonly misclassifies.
# Applied after label mapping, before deduplication.
ENTITY_TYPE_CORRECTIONS: Dict[str, str] = {
    "Bloomberg":       "Company",
    "Reuters":         "Company",
    "CNBC":            "Company",
    "MarketWatch":     "Company",
    "Qualcomm":        "Company",
    "Intel":           "Company",
    "AMD":             "Company",
    "Nasdaq":          "Company",
    "NYSE":            "Company",
    "Fed":             "Company",
    "Federal Reserve": "Company",
    # Media / financial data providers
    "FactSet":         "Company",
    "Refinitiv":       "Company",
    "Morningstar":     "Company",
    "S&P":             "Company",
    "S&P 500":         "Company",
    "Dow Jones":       "Company",
    "Wall Street":     "Company",
    # Commonly misclassified as PERSON or GPE
    "Washington":      "Location",
    "Treasury":        "Company",
    "White House":     "Location",
}

# Entities whose exact names are noise — financial metrics, role acronyms,
# common ticker symbols, and FX codes that spaCy surfaces as entities.
# Applied after ENTITY_TYPE_CORRECTIONS; any entity whose name appears in
# this set (case-sensitive) is dropped and never written to Neo4j.
ENTITY_REMOVE_LIST: set = {
    "ARPU", "CapEx", "PNT", "EBITDA", "EPS", "GDP", "CPI",
    "ETF", "IPO", "CEO", "CFO", "COO", "CTO",
    "SPY", "QQQ", "TLT", "GLD",
    "VIX", "USD", "EUR", "GBP", "JPY", "BTC", "ETH",
}

# ---------------------------------------------------------------------------
# Entity name normalisation (CHANGE 5)
# Maps common variant forms to canonical names before Neo4j writes.
# Applied in extract_entities_spacy() before any other processing.
# ---------------------------------------------------------------------------

ENTITY_ALIASES: Dict[str, str] = {
    # Federal Reserve variants
    "Fed":                       "Federal Reserve",
    "the Fed":                   "Federal Reserve",
    "US Fed":                    "Federal Reserve",
    "U.S. Fed":                  "Federal Reserve",
    "FED":                       "Federal Reserve",
    "Fed Reserve":               "Federal Reserve",
    # Other central banks
    "ECB":                       "European Central Bank",
    "BoE":                       "Bank of England",
    "BoJ":                       "Bank of Japan",
    # US investment banks
    "Goldman":                   "Goldman Sachs",
    "GS":                        "Goldman Sachs",
    "JPM":                       "JPMorgan Chase",
    "JP Morgan":                 "JPMorgan Chase",
    "BofA":                      "Bank of America",
    "MS":                        "Morgan Stanley",
    "Citi":                      "Citigroup",
    # Crypto
    "BTC":                       "Bitcoin",
    "ETH":                       "Ethereum",
    # Tech companies
    "Nvidia":                    "NVIDIA",
    "Apple Inc":                 "Apple",
    "Meta Platforms":            "Meta",
    "Alphabet Inc":              "Alphabet",
    "Amazon.com":                "Amazon",
    # Political figures
    "Trump":                     "Donald Trump",
    "Biden":                     "Joe Biden",
    "Powell":                    "Jerome Powell",
    "Yellen":                    "Janet Yellen",
    "Lagarde":                   "Christine Lagarde",
    # International organisations
    "IMF":                       "International Monetary Fund",
    "WTO":                       "World Trade Organization",
    "SEC":                       "Securities and Exchange Commission",
    "CFTC":                      "Commodity Futures Trading Commission",
}


def normalise_entity_name(name: str) -> str:
    stripped   = name.strip()
    canonical  = ENTITY_ALIASES.get(stripped, stripped)
    if canonical != stripped:
        logger.debug(f"Entity normalised: {stripped} -> {canonical}")
    return canonical


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
    n.central_bank_source = e.central_bank_source,
    n.mention_count       = e.mention_count
ON MATCH SET
    n.last_seen           = CASE WHEN n.is_synthetic THEN n.last_seen ELSE datetime() END,
    n.type                = CASE WHEN n.is_synthetic THEN n.type   ELSE e.type        END,
    n.central_bank_source = CASE
        WHEN n.is_synthetic        THEN n.central_bank_source
        WHEN e.central_bank_source THEN true
        ELSE n.central_bank_source
    END,
    n.mention_count       = CASE WHEN n.is_synthetic THEN n.mention_count ELSE e.mention_count END
"""

# Batched chunk node upsert. One NewsChunk per (graph_id, chunk_id).
BATCH_MERGE_CHUNK_QUERY = """
UNWIND $chunks AS ch
MERGE (c:NewsChunk {graph_id: $gid, chunk_id: ch.chunk_id})
ON CREATE SET
    c.uuid            = ch.uuid,
    c.briefing_source = ch.briefing_source,
    c.chunk_index     = ch.chunk_index,
    c.chunk_hash      = ch.chunk_hash,
    c.preview         = ch.preview,
    c.created_at      = $now,
    c.last_seen       = datetime()
ON MATCH SET
    c.briefing_source = ch.briefing_source,
    c.chunk_index     = ch.chunk_index,
    c.preview         = ch.preview,
    c.last_seen       = datetime()
"""

# Link entities to source chunks (graph navigation path: Entity -> NewsChunk).
BATCH_MENTIONED_IN_QUERY = """
UNWIND $rows AS row
MATCH (c:NewsChunk {graph_id: $gid, chunk_id: row.chunk_id})
UNWIND row.entity_name_lowers AS name_lower
MATCH (e:Entity {graph_id: $gid, name_lower: name_lower})
MERGE (e)-[r:MENTIONED_IN]->(c)
ON CREATE SET
    r.first_seen = datetime(),
    r.last_seen  = datetime()
ON MATCH SET
    r.last_seen  = datetime()
"""

# Weighted co-occurrence link between entities that appeared in the same chunk.
BATCH_MENTIONED_WITH_QUERY = """
UNWIND $pairs AS pair
MATCH (a:Entity {graph_id: $gid, name_lower: pair.a})
MATCH (b:Entity {graph_id: $gid, name_lower: pair.b})
WHERE a.name_lower < b.name_lower
MERGE (a)-[r:MENTIONED_WITH]-(b)
ON CREATE SET
    r.weight     = pair.weight,
    r.first_seen = datetime(),
    r.last_seen  = datetime()
ON MATCH SET
    r.weight     = coalesce(r.weight, 0) + pair.weight,
    r.last_seen  = datetime()
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
    - Name must be at least 3 characters
    - Name must not be purely numeric (e.g. bare years like "2024" are dropped)
    - Name must not appear in ENTITY_REMOVE_LIST (financial metrics, role acronyms,
      common tickers, FX codes)
    - Person entities must contain a space (single-token first names are dropped)

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
        # CHANGE 5: normalise to canonical form before any other checks
        name = normalise_entity_name(name)

        if len(name) < 3:
            continue
        if name.isnumeric():
            continue

        # Drop noise acronyms, metrics, tickers, and FX codes (after normalisation)
        if name in ENTITY_REMOVE_LIST:
            continue

        etype = ENTITY_TYPE_CORRECTIONS.get(name, SPACY_LABEL_MAP[label])

        # Drop single-token Person names (no surname = unreliable partial match)
        if etype == "Person" and " " not in name:
            continue

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
            "mention_count":       int(e.get("mention_count", 1)),
        }
        for e in entities
    ]

    total = 0
    for i in range(0, len(payload), BATCH_SIZE):
        batch = payload[i : i + BATCH_SIZE]
        session.run(BATCH_MERGE_ENTITY_QUERY, entities=batch, gid=graph_id, now=now)
        total += len(batch)

    return total


def batch_merge_chunks(
    session,
    graph_id: str,
    chunk_rows: List[Dict[str, Any]],
    now: str,
) -> int:
    """UNWIND-merge NewsChunk nodes in batches of BATCH_SIZE."""
    if not chunk_rows:
        return 0

    payload = [
        {
            "uuid": str(uuid.uuid4()),
            "chunk_id": row["chunk_id"],
            "briefing_source": row["briefing_source"],
            "chunk_index": row["chunk_index"],
            "chunk_hash": row["chunk_hash"],
            "preview": row["preview"],
        }
        for row in chunk_rows
    ]

    total = 0
    for i in range(0, len(payload), BATCH_SIZE):
        batch = payload[i : i + BATCH_SIZE]
        session.run(BATCH_MERGE_CHUNK_QUERY, chunks=batch, gid=graph_id, now=now)
        total += len(batch)
    return total


def batch_link_entities_to_chunks(
    session,
    graph_id: str,
    chunk_rows: List[Dict[str, Any]],
) -> int:
    """
    Link entities to their source chunks with :MENTIONED_IN edges.

    Returns the number of attempted entity->chunk links.
    """
    if not chunk_rows:
        return 0

    attempted_links = 0
    for i in range(0, len(chunk_rows), BATCH_SIZE):
        batch = chunk_rows[i : i + BATCH_SIZE]
        attempted_links += sum(len(r.get("entity_name_lowers", [])) for r in batch)
        session.run(BATCH_MENTIONED_IN_QUERY, rows=batch, gid=graph_id)

    return attempted_links


def batch_merge_cooccurrence_pairs(
    session,
    graph_id: str,
    pair_weights: Dict[Tuple[str, str], int],
) -> int:
    """
    Merge weighted :MENTIONED_WITH links for co-occurring entity pairs.

    pair_weights maps (name_lower_a, name_lower_b) -> chunk co-occurrence count.
    Returns the number of distinct pairs merged.
    """
    if not pair_weights:
        return 0

    payload = [
        {"a": a, "b": b, "weight": int(w)}
        for (a, b), w in pair_weights.items()
    ]

    total = 0
    for i in range(0, len(payload), BATCH_SIZE):
        batch = payload[i : i + BATCH_SIZE]
        session.run(BATCH_MENTIONED_WITH_QUERY, pairs=batch, gid=graph_id)
        total += len(batch)

    return total


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_briefing(driver, graph_id: str, briefing_path: str) -> None:
    t_start = time.time()
    briefing_source = Path(briefing_path).name

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
    chunk_rows: List[Dict[str, Any]] = []
    pair_weights: Dict[Tuple[str, str], int] = {}

    for chunk_idx, chunk in enumerate(chunks):
        chunk_entity_lowers: set = set()

        for ent in extract_entities_spacy(chunk):
            key = (ent["name"].lower(), ent["type"])
            if key not in global_entities:
                global_entities[key] = ent
                global_entities[key]["mention_count"] = 1
            else:
                global_entities[key]["mention_count"] = global_entities[key].get("mention_count", 1) + 1
                if ent["central_bank_source"]:
                    global_entities[key]["central_bank_source"] = True

            chunk_entity_lowers.add(ent["name"].lower())

        if chunk_entity_lowers:
            chunk_hash = hashlib.sha256(chunk.encode('utf-8')).hexdigest()
            chunk_id = f"{Path(briefing_path).stem}:{chunk_idx:05d}:{chunk_hash[:16]}"
            preview = " ".join(chunk.split())[:220]

            sorted_entity_lowers = sorted(chunk_entity_lowers)
            chunk_rows.append({
                "chunk_id": chunk_id,
                "briefing_source": briefing_source,
                "chunk_index": int(chunk_idx),
                "chunk_hash": chunk_hash,
                "preview": preview,
                "entity_name_lowers": sorted_entity_lowers,
            })

            if len(sorted_entity_lowers) >= 2:
                for a, b in combinations(sorted_entity_lowers, 2):
                    pair_weights[(a, b)] = pair_weights.get((a, b), 0) + 1

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
        chunk_nodes = batch_merge_chunks(session, graph_id, chunk_rows, now)
        mention_links = batch_link_entities_to_chunks(session, graph_id, chunk_rows)
        pair_links = batch_merge_cooccurrence_pairs(session, graph_id, pair_weights)

    elapsed = time.time() - t_start
    logger.info(f"Merged {merged} unique entities into Neo4j")
    logger.info(f"Merged {chunk_nodes} NewsChunk nodes and {mention_links} MENTIONED_IN links")
    logger.info(f"Merged {pair_links} weighted MENTIONED_WITH links")
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
