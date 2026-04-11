"""Tests for Neo4jStorage.update_node_traits and _sample_investor_traits."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))

from unittest.mock import MagicMock, patch
import pytest


def test_update_node_traits_runs_correct_cypher():
    """update_node_traits should SET traits on the node matching uuid."""
    from app.storage.neo4j_storage import Neo4jStorage

    # Create a mock driver
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__.return_value = mock_session

    # Create a Neo4jStorage instance without going through __init__
    storage = Neo4jStorage.__new__(Neo4jStorage)
    storage._driver = mock_driver

    traits = {'risk_tolerance': 7.5, 'capital_usd': 50000.0, 'is_synthetic': False}
    storage.update_node_traits('test-uuid-123', traits)

    # Verify a write transaction was executed
    args, _ = mock_session.execute_write.call_args
    assert callable(args[0]), "execute_write must be called with the _update transaction function"

    # Verify the transaction function runs MATCH ... SET n += syntax
    tx_func = args[0]
    mock_tx = MagicMock()
    tx_func(mock_tx)  # execute the inner _update function
    cypher_call = mock_tx.run.call_args[0][0]
    assert 'MATCH' in cypher_call
    assert 'SET n +=' in cypher_call
    assert 'uuid' in cypher_call


def test_sample_investor_traits_returns_all_fields():
    """_sample_investor_traits must return all 16 trait fields."""
    with patch('app.services.oasis_profile_generator.OpenAI'):
        from app.services.oasis_profile_generator import OasisProfileGenerator
        gen = OasisProfileGenerator.__new__(OasisProfileGenerator)
        traits = gen._sample_investor_traits()

    expected_keys = {
        'risk_tolerance', 'capital_usd', 'time_horizon_days', 'fear_greed_dominant',
        'loss_aversion_multiplier', 'herd_behaviour', 'reaction_speed_minutes',
        'primary_strategy', 'asset_class_bias', 'news_sensitivity',
        'geopolitical_sensitivity', 'investor_archetype', 'formative_crash',
        'overconfidence_bias', 'leverage_typical', 'is_synthetic',
    }
    assert set(traits.keys()) == expected_keys


def test_sample_investor_traits_bounds():
    """All continuous traits must stay within their documented bounds."""
    with patch('app.services.oasis_profile_generator.OpenAI'):
        from app.services.oasis_profile_generator import OasisProfileGenerator
        gen = OasisProfileGenerator.__new__(OasisProfileGenerator)
        for _ in range(100):
            t = gen._sample_investor_traits()
            assert 0.0 <= t['risk_tolerance'] <= 10.0
            assert 500.0 <= t['capital_usd'] <= 100_000_000.0
            assert 1 <= t['time_horizon_days'] <= 3650
            assert 0.5 <= t['loss_aversion_multiplier'] <= 10.0
            assert 1.0 <= t['reaction_speed_minutes'] <= 10080.0
            assert 0.0 <= t['herd_behaviour'] <= 10.0
            assert t['fear_greed_dominant'] in ('fear', 'greed')
            assert t['is_synthetic'] is False


def test_sample_investor_traits_valid_categoricals():
    """Categorical traits must only contain allowed values."""
    with patch('app.services.oasis_profile_generator.OpenAI'):
        from app.services.oasis_profile_generator import OasisProfileGenerator
        gen = OasisProfileGenerator.__new__(OasisProfileGenerator)
        t = gen._sample_investor_traits()

        assert t['investor_archetype'] in (
            'retail_amateur', 'retail_experienced', 'prop_trader',
            'fund_manager', 'family_office', 'hedge_fund', 'pension_fund'
        )
        assert t['primary_strategy'] in (
            'day_trading', 'swing', 'value', 'growth', 'momentum',
            'index', 'income', 'macro', 'contrarian', 'quant'
        )
        assert t['leverage_typical'] in ('none', '2x', '5x', '10x_plus')
        assert t['formative_crash'] in ('none', 'dotcom', 'gfc_2008', 'covid_2020', 'iran_war_2026')
        assert t['asset_class_bias'] in (
            'equities', 'mixed', 'crypto', 'bonds', 'commodities', 'fx', 'real_estate'
        )
