"""Tests for db_setup.py — index creation and connectivity check."""
import sys, os
# Add backend/ to path (for app.* imports inside db_setup)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))
# Add scripts/ to path (for bare `import db_setup`)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))

from unittest.mock import MagicMock, patch
import pytest


def _make_mock_driver():
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.verify_connectivity.return_value = None
    return driver, session


def test_ensure_indexes_runs_three_statements():
    """ensure_indexes must issue exactly three CREATE INDEX IF NOT EXISTS statements."""
    import db_setup

    driver, session = _make_mock_driver()
    db_setup.ensure_indexes(driver)

    assert session.run.call_count == 3
    calls_text = [str(c) for c in session.run.call_args_list]
    assert any('MemoryEvent' in t and 'agent_uuid' in t for t in calls_text)
    assert any('MemoryEvent' in t and 'timestamp' in t for t in calls_text)
    assert any('Entity' in t and 'is_synthetic' in t for t in calls_text)


def test_setup_raises_on_connectivity_failure():
    """setup() must raise if Neo4j is unreachable."""
    import db_setup

    mock_driver = MagicMock()
    mock_driver.verify_connectivity.side_effect = Exception("Connection refused")

    with patch.object(db_setup, 'GraphDatabase') as mock_gdb:
        mock_gdb.driver.return_value = mock_driver
        with pytest.raises(Exception, match="Connection refused"):
            db_setup.setup()
