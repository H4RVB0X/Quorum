"""Tests for incremental_update.py."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

from unittest.mock import MagicMock, patch


def test_resolve_graph_id_from_projects_dir(tmp_path):
    """resolve_active_graph_id should return graph_id from most recent GRAPH_COMPLETED project."""
    import json
    projects_dir = tmp_path / "projects"
    proj = projects_dir / "proj_001"
    proj.mkdir(parents=True)
    (proj / "project.json").write_text(json.dumps({
        "status": "GRAPH_COMPLETED",
        "graph_id": "test-graph-uuid-123"
    }))

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))
    from incremental_update import resolve_active_graph_id
    result = resolve_active_graph_id(projects_dir=str(projects_dir))
    assert result == "test-graph-uuid-123"


def test_merge_skips_synthetic_nodes():
    """
    The Cypher MERGE must use CASE WHEN is_synthetic to avoid overwriting synthetic agent nodes.
    Check that the query sent contains the is_synthetic guard.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))
    from incremental_update import MERGE_ENTITY_QUERY
    assert 'is_synthetic' in MERGE_ENTITY_QUERY
