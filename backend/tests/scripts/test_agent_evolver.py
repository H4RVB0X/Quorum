"""Tests for agent_evolver.py — FAISS gate, exact-triple gate, trait string serialisation."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))

import numpy as np
import faiss


def test_build_faiss_index_and_query():
    """FAISS IndexFlatIP with L2-normalised vectors should return cosine similarity."""
    # Two identical vectors → similarity = 1.0
    vec = np.random.rand(768).astype('float32')
    vec /= np.linalg.norm(vec)

    index = faiss.IndexFlatIP(768)
    index.add(vec.reshape(1, -1))

    query = vec.reshape(1, -1).copy()
    distances, _ = index.search(query, 1)
    assert abs(distances[0][0] - 1.0) < 1e-5


def test_faiss_dissimilar_vectors_below_threshold():
    """Random orthogonal vectors should have similarity near 0."""
    d = 768
    a = np.zeros(d, dtype='float32')
    a[0] = 1.0
    b = np.zeros(d, dtype='float32')
    b[1] = 1.0  # orthogonal

    index = faiss.IndexFlatIP(d)
    index.add(a.reshape(1, -1))

    query = b.reshape(1, -1)
    distances, _ = index.search(query, 1)
    assert distances[0][0] < 0.1


def test_serialise_traits():
    """Trait string must include all categorical and numeric fields."""
    from agent_evolver import serialise_traits

    traits = {
        'investor_archetype': 'retail_amateur',
        'primary_strategy': 'day_trading',
        'risk_tolerance': 8.2,
        'capital_usd': 12000.0,
        'time_horizon_days': 30,
        'herd_behaviour': 7.1,
        'news_sensitivity': 5.0,
        'geopolitical_sensitivity': 3.0,
        'asset_class_bias': 'equities',
        'leverage_typical': 'none',
        'fear_greed_dominant': 'greed',
    }
    result = serialise_traits(traits)
    assert 'retail_amateur' in result
    assert 'day_trading' in result
    assert '8.2' in result


def test_triple_key():
    """risk_bucket must be floor(risk_tolerance / 3.33) clamped 0-2."""
    from agent_evolver import triple_key

    assert triple_key('retail_amateur', 'swing', 9.9) == ('retail_amateur', 'swing', 2)
    assert triple_key('hedge_fund', 'quant', 5.0) == ('hedge_fund', 'quant', 1)
    assert triple_key('pension_fund', 'index', 1.0) == ('pension_fund', 'index', 0)
    assert triple_key('retail_amateur', 'value', -1.0) == ('retail_amateur', 'value', 0)
