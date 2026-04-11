"""
db_setup.py — shared Neo4j index creation for all MiroFish standalone scripts.

Call setup() as the first thing in every script's main():
    from db_setup import setup
    driver = setup()
"""
import sys
import os

# Allow running from any working directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from neo4j import GraphDatabase
from app.config import Config


def ensure_indexes(driver) -> None:
    """Create the three MemoryEvent/Entity indexes idempotently."""
    with driver.session() as session:
        session.run(
            "CREATE INDEX IF NOT EXISTS FOR (m:MemoryEvent) ON (m.agent_uuid)"
        )
        session.run(
            "CREATE INDEX IF NOT EXISTS FOR (m:MemoryEvent) ON (m.timestamp)"
        )
        session.run(
            "CREATE INDEX IF NOT EXISTS FOR (n:Entity) ON (n.is_synthetic)"
        )


def setup(uri: str = None, user: str = None, password: str = None):
    """
    Verify Neo4j connectivity and create required indexes.
    Returns the connected driver — callers are responsible for driver.close().
    Raises immediately if Neo4j is unreachable.
    """
    uri = uri or Config.NEO4J_URI
    user = user or Config.NEO4J_USER
    password = password or Config.NEO4J_PASSWORD

    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()  # Raises if unreachable
    ensure_indexes(driver)
    return driver


if __name__ == "__main__":
    print("Running db_setup...")
    d = setup()
    print("Neo4j connected and indexes ensured.")
    d.close()
