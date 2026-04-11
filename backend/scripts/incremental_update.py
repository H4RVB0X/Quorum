"""
incremental_update.py — Process a briefing .txt file into the Neo4j knowledge graph.

Extracts entities and relations via the existing NER pipeline and MERGEs them
into Neo4j. Never overwrites properties on synthetic agent nodes (is_synthetic=True).

Usage:
  python incremental_update.py --briefing /path/to/briefing.txt [--graph-id <uuid>]
"""
import sys
import os
import json
import uuid
import argparse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from db_setup import setup as db_setup
from app.config import Config
from app.storage.embedding_service import EmbeddingService
from app.storage.ner_extractor import NERExtractor
from app.services.text_processor import TextProcessor
from app.utils.logger import get_logger

logger = get_logger('mirofish.incremental_update')

# Exposed as module-level constant so tests can inspect the guard clause
MERGE_ENTITY_QUERY = """
MERGE (n:Entity {graph_id: $gid, name_lower: $name_lower})
ON CREATE SET
    n.uuid = $uuid,
    n.name = $name,
    n.summary = $summary,
    n.attributes_json = $attrs_json,
    n.embedding = $embedding,
    n.created_at = $now,
    n.is_synthetic = false
ON MATCH SET
    n.summary    = CASE WHEN n.is_synthetic = true THEN n.summary    ELSE $summary    END,
    n.attributes_json = CASE WHEN n.is_synthetic = true THEN n.attributes_json ELSE $attrs_json END,
    n.embedding  = CASE WHEN n.is_synthetic = true THEN n.embedding  ELSE $embedding  END
RETURN n.uuid AS uuid
"""

CREATE_RELATION_QUERY = """
MATCH (src:Entity {graph_id: $gid, name_lower: $src_lower})
MATCH (tgt:Entity {graph_id: $gid, name_lower: $tgt_lower})
CREATE (src)-[r:RELATION {
    uuid: $uuid,
    graph_id: $gid,
    name: $name,
    fact: $fact,
    fact_embedding: $fact_embedding,
    created_at: $now
}]->(tgt)
"""


# ---------------------------------------------------------------------------
# Graph ID resolution
# ---------------------------------------------------------------------------

def resolve_active_graph_id(projects_dir: str = None) -> str:
    """
    Find the graph_id from the most recently completed project.
    Reads project.json from each subdirectory of projects_dir.
    """
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
# Entity/relation writing
# ---------------------------------------------------------------------------

def merge_entity(session, graph_id: str, entity: dict, embedding: list, now: str) -> str:
    name = entity['name']
    attrs = entity.get('attributes', {})
    result = session.run(
        MERGE_ENTITY_QUERY,
        gid=graph_id,
        name_lower=name.lower(),
        uuid=str(uuid.uuid4()),
        name=name,
        summary=f"{name} ({entity.get('type', 'Entity')})",
        attrs_json=json.dumps(attrs, ensure_ascii=False),
        embedding=embedding,
        now=now,
    )
    record = result.single()
    return record['uuid'] if record else None


def create_relation(session, graph_id: str, relation: dict, fact_embedding: list, now: str):
    session.run(
        CREATE_RELATION_QUERY,
        gid=graph_id,
        src_lower=relation['source'].lower(),
        tgt_lower=relation['target'].lower(),
        uuid=str(uuid.uuid4()),
        name=relation['type'],
        fact=relation.get('fact', ''),
        fact_embedding=fact_embedding,
        now=now,
    )


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_briefing(driver, graph_id: str, briefing_path: str) -> None:
    embedding_svc = EmbeddingService()
    ner = NERExtractor()

    # Load ontology for NER guidance
    with driver.session() as session:
        result = session.run(
            "MATCH (g:Graph {graph_id: $gid}) RETURN g.ontology_json AS oj",
            gid=graph_id,
        )
        record = result.single()
        ontology = json.loads(record['oj']) if record and record['oj'] else {}

    # Read and preprocess briefing
    text = Path(briefing_path).read_text(encoding='utf-8')
    text = TextProcessor.preprocess_text(text)
    chunks = TextProcessor.split_text(text, chunk_size=Config.DEFAULT_CHUNK_SIZE,
                                      overlap=Config.DEFAULT_CHUNK_OVERLAP)

    logger.info(f"Processing {len(chunks)} chunks from {briefing_path}")
    now = datetime.now(timezone.utc).isoformat()

    for i, chunk in enumerate(chunks):
        logger.info(f"Chunk {i+1}/{len(chunks)}: extracting entities...")
        try:
            extraction = ner.extract(chunk, ontology)
        except Exception as e:
            logger.warning(f"NER failed on chunk {i+1}: {e} — skipping")
            continue

        entities = extraction.get('entities', [])
        relations = extraction.get('relations', [])

        if not entities:
            continue

        # Batch embed: entity summaries + relation facts
        entity_texts = [f"{e['name']} ({e.get('type','Entity')})" for e in entities]
        relation_texts = [r.get('fact', f"{r['source']} {r['type']} {r['target']}") for r in relations]
        all_texts = entity_texts + relation_texts

        try:
            all_embeddings = embedding_svc.embed_batch(all_texts)
        except Exception as e:
            logger.warning(f"Embedding failed for chunk {i+1}: {e} — using empty vectors")
            all_embeddings = [[] for _ in all_texts]

        entity_embeddings = all_embeddings[:len(entities)]
        relation_embeddings = all_embeddings[len(entities):]

        with driver.session() as session:
            for j, entity in enumerate(entities):
                emb = entity_embeddings[j] if j < len(entity_embeddings) else []
                try:
                    merge_entity(session, graph_id, entity, emb, now)
                except Exception as e:
                    logger.warning(f"Failed to merge entity '{entity['name']}': {e}")

            for j, relation in enumerate(relations):
                emb = relation_embeddings[j] if j < len(relation_embeddings) else []
                try:
                    create_relation(session, graph_id, relation, emb, now)
                except Exception as e:
                    logger.warning(f"Failed to create relation: {e}")

    logger.info(f"Briefing processed: {briefing_path}")


def main():
    parser = argparse.ArgumentParser(description="Merge briefing into Neo4j knowledge graph")
    parser.add_argument('--briefing', required=True, help="Path to briefing .txt file")
    parser.add_argument('--graph-id', help="Neo4j graph_id (auto-detected if omitted)")
    args = parser.parse_args()

    driver = db_setup()
    try:
        graph_id = args.graph_id if args.graph_id is not None else resolve_active_graph_id()
        print(f"Using graph_id: {graph_id}")
        process_briefing(driver, graph_id, args.briefing)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
