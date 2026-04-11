"""Tests for simulation_tick.py."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

from unittest.mock import MagicMock, patch
import numpy as np

def test_cosine_top5_returns_correct_chunks():
    """top_k_chunks should return the k chunks most similar to query embedding."""
    from simulation_tick import top_k_chunks

    # chunk 0 is identical to query → similarity = 1.0
    query_emb = [1.0, 0.0, 0.0]
    cache = [
        ("similar chunk", [1.0, 0.0, 0.0]),
        ("unrelated",     [0.0, 1.0, 0.0]),
        ("orthogonal",    [0.0, 0.0, 1.0]),
    ]
    result = top_k_chunks(query_emb, cache, k=1)
    assert result == ["similar chunk"]


def test_build_agent_query_string_includes_bias():
    """build_agent_query should encode asset_class_bias and strategy."""
    from simulation_tick import build_agent_query

    traits = {
        'asset_class_bias': 'crypto',
        'primary_strategy': 'day_trading',
        'geopolitical_sensitivity': 9.0,
    }
    q = build_agent_query(traits)
    assert 'crypto' in q
    assert 'day_trading' in q
    assert 'geopolitical' in q.lower()  # High geo_sensitivity adds geo keywords


def test_build_agent_query_no_geo_keywords_when_low():
    """build_agent_query should NOT add geo keywords when geopolitical_sensitivity < 7."""
    from simulation_tick import build_agent_query

    traits = {
        'asset_class_bias': 'equities',
        'primary_strategy': 'index',
        'geopolitical_sensitivity': 2.0,
    }
    q = build_agent_query(traits)
    assert 'geopolitical' not in q.lower()


def test_checkpoint_write_and_resume(tmp_path):
    """Checkpoint file should be written with correct fields."""
    from simulation_tick import write_checkpoint, load_checkpoint

    ckpt_path = tmp_path / "tick_checkpoint.json"
    briefing = "briefings/2026-04-09_1400.txt"
    processed = ["uuid-1", "uuid-2"]

    write_checkpoint(ckpt_path, briefing, processed)
    loaded = load_checkpoint(ckpt_path, briefing)

    assert loaded == {"uuid-1", "uuid-2"}


def test_checkpoint_returns_empty_for_different_briefing(tmp_path):
    """load_checkpoint returns empty set if briefing_source doesn't match."""
    from simulation_tick import write_checkpoint, load_checkpoint

    ckpt_path = tmp_path / "tick_checkpoint.json"
    write_checkpoint(ckpt_path, "briefings/old.txt", ["uuid-1"])
    loaded = load_checkpoint(ckpt_path, "briefings/new.txt")
    assert loaded == set()
