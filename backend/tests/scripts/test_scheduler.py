"""Tests for scheduler.py — mutex flag and status file writing."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))

import json
from pathlib import Path
from unittest.mock import patch, MagicMock


def test_write_status_creates_valid_json(tmp_path):
    """write_status should write a valid JSON file with expected keys."""
    from scheduler import write_status
    status_path = tmp_path / "status.json"

    write_status(
        status_path=status_path,
        last_news_fetch="2026-04-09T14:00:00",
        last_tick="2026-04-09T14:03:22",
        last_full_simulation="2026-04-09T02:00:00",
        agent_pool_size=1247,
        last_error=None,
    )

    data = json.loads(status_path.read_text())
    assert data['last_news_fetch'] == "2026-04-09T14:00:00"
    assert data['current_agent_pool_size'] == 1247
    assert data['last_error'] is None


def test_hourly_job_skips_tick_when_daily_running():
    """If _daily_running is set, hourly job must not call run_tick."""
    from scheduler import _daily_running

    _daily_running.set()
    try:
        with patch('scheduler.db_setup') as mock_db_setup, \
             patch('scheduler.run_tick') as mock_tick, \
             patch('scheduler.fetch', return_value=Path("/tmp/fake_briefing.txt")), \
             patch('scheduler.process_briefing'), \
             patch('scheduler.get_agent_pool_size', return_value=100), \
             patch('scheduler.write_status'):
            mock_db_setup.return_value = MagicMock()
            from scheduler import hourly_job
            hourly_job(
                graph_id="test-graph",
                status_path=Path("/tmp/status.json"),
                status_state={},
            )
        mock_tick.assert_not_called()
    finally:
        _daily_running.clear()
