"""Tests for /api/investors/sentiment endpoint and sentiment scoring logic."""
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Pure-logic tests — no Neo4j, no Flask
# ---------------------------------------------------------------------------

def test_reaction_score_buy_is_positive():
    from app.api.investors import _reaction_score
    assert _reaction_score('buy') == 1.0


def test_reaction_score_sell_is_negative():
    from app.api.investors import _reaction_score
    assert _reaction_score('sell') == -1.0


def test_reaction_score_panic_is_negative():
    from app.api.investors import _reaction_score
    assert _reaction_score('panic') == -1.0


def test_reaction_score_hold_is_zero():
    from app.api.investors import _reaction_score
    assert _reaction_score('hold') == 0.0


def test_reaction_score_hedge_is_half():
    from app.api.investors import _reaction_score
    assert _reaction_score('hedge') == 0.5


def test_reaction_score_unknown_is_zero():
    from app.api.investors import _reaction_score
    assert _reaction_score('unknown_reaction') == 0.0


def test_compute_sentiment_single_asset_all_buyers():
    """All buyers → score should be +1."""
    from app.api.investors import compute_sentiment_scores
    rows = [
        {'asset_class': 'equities', 'reaction': 'buy', 'confidence': 8.0, 'capital': 100_000},
        {'asset_class': 'equities', 'reaction': 'buy', 'confidence': 6.0, 'capital': 200_000},
    ]
    result = compute_sentiment_scores(rows)
    assert 'equities' in result
    assert result['equities']['score'] == pytest.approx(1.0)
    assert result['equities']['event_count'] == 2


def test_compute_sentiment_single_asset_all_sellers():
    """All sellers → score should be -1."""
    from app.api.investors import compute_sentiment_scores
    rows = [
        {'asset_class': 'bonds', 'reaction': 'sell', 'confidence': 5.0, 'capital': 50_000},
        {'asset_class': 'bonds', 'reaction': 'panic', 'confidence': 9.0, 'capital': 300_000},
    ]
    result = compute_sentiment_scores(rows)
    assert result['bonds']['score'] == pytest.approx(-1.0)


def test_compute_sentiment_weights_by_capital_and_confidence():
    """Large-capital buyer should dominate small-capital seller."""
    from app.api.investors import compute_sentiment_scores
    rows = [
        {'asset_class': 'crypto', 'reaction': 'buy',  'confidence': 10.0, 'capital': 10_000_000},
        {'asset_class': 'crypto', 'reaction': 'sell', 'confidence': 10.0, 'capital': 1_000},
    ]
    result = compute_sentiment_scores(rows)
    # Weighted score ≈ (10M*10*1 + 1K*10*-1) / (10M*10 + 1K*10)
    # ≈ (100M - 10K) / (100M + 10K) ≈ +0.9998 → clearly positive
    assert result['crypto']['score'] > 0.99


def test_compute_sentiment_mixed_holds_are_neutral():
    """Pure holds → score should be 0."""
    from app.api.investors import compute_sentiment_scores
    rows = [
        {'asset_class': 'real_estate', 'reaction': 'hold', 'confidence': 7.0, 'capital': 500_000},
        {'asset_class': 'real_estate', 'reaction': 'hold', 'confidence': 3.0, 'capital': 100_000},
    ]
    result = compute_sentiment_scores(rows)
    assert result['real_estate']['score'] == pytest.approx(0.0)


def test_compute_sentiment_multiple_asset_classes():
    """Results split correctly across asset classes."""
    from app.api.investors import compute_sentiment_scores
    rows = [
        {'asset_class': 'equities', 'reaction': 'buy',  'confidence': 8.0, 'capital': 100_000},
        {'asset_class': 'crypto',   'reaction': 'sell', 'confidence': 8.0, 'capital': 100_000},
    ]
    result = compute_sentiment_scores(rows)
    assert result['equities']['score'] == pytest.approx(1.0)
    assert result['crypto']['score'] == pytest.approx(-1.0)


def test_compute_sentiment_score_clipped_to_minus1_plus1():
    """Score must never exceed [-1, +1]."""
    from app.api.investors import compute_sentiment_scores
    rows = [
        {'asset_class': 'commodities', 'reaction': 'buy', 'confidence': 10.0, 'capital': 1e12},
    ]
    result = compute_sentiment_scores(rows)
    score = result['commodities']['score']
    assert -1.0 <= score <= 1.0


def test_compute_sentiment_zero_weight_rows_skipped():
    """Rows with zero capital or zero confidence contribute nothing (no division by zero)."""
    from app.api.investors import compute_sentiment_scores
    rows = [
        {'asset_class': 'fx', 'reaction': 'buy', 'confidence': 0.0, 'capital': 100_000},
        {'asset_class': 'fx', 'reaction': 'sell', 'confidence': 5.0, 'capital': 0},
    ]
    # All rows have zero weight → score defaults to 0
    result = compute_sentiment_scores(rows)
    assert result['fx']['score'] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Flask endpoint tests — Neo4j mocked
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    """Flask test client with Neo4j driver mocked."""
    import app.api.investors as inv_module
    mock_driver = MagicMock()
    with patch.object(inv_module, '_get_driver', return_value=mock_driver):
        from app import create_app
        flask_app = create_app()
        flask_app.config['TESTING'] = True
        with flask_app.test_client() as c:
            yield c, mock_driver


def _make_session_mock(rows_24h, rows_7d):
    """Build a mock Neo4j session that returns two query results."""
    mock_session = MagicMock()

    def run_side_effect(query, **kwargs):
        mock_result = MagicMock()
        # First call → 24h, second call → 7d
        if not hasattr(run_side_effect, '_call_count'):
            run_side_effect._call_count = 0
        run_side_effect._call_count += 1
        if run_side_effect._call_count == 1:
            mock_result.data.return_value = rows_24h
        else:
            mock_result.data.return_value = rows_7d
        return mock_result

    mock_session.run.side_effect = run_side_effect
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    return mock_session


def test_sentiment_endpoint_requires_graph_id(client):
    c, _ = client
    res = c.get('/api/investors/sentiment')
    assert res.status_code == 400
    data = res.get_json()
    assert data['success'] is False
    assert 'graph_id' in data['error']


def test_sentiment_endpoint_returns_by_asset_class(client):
    c, mock_driver = client

    rows = [
        {'asset_class': 'equities', 'reaction': 'buy',  'confidence': 8.0, 'capital': 100_000},
        {'asset_class': 'crypto',   'reaction': 'sell', 'confidence': 6.0, 'capital': 50_000},
    ]
    mock_session = _make_session_mock(rows_24h=rows, rows_7d=rows)
    mock_driver.session.return_value = mock_session

    res = c.get('/api/investors/sentiment?graph_id=test-graph-id')
    assert res.status_code == 200
    data = res.get_json()
    assert data['success'] is True
    by_class = data['data']['by_asset_class']
    assert 'equities' in by_class
    assert 'crypto' in by_class
    assert by_class['equities']['24h']['score'] == pytest.approx(1.0)
    assert by_class['crypto']['24h']['score'] == pytest.approx(-1.0)


def test_sentiment_endpoint_has_generated_at(client):
    c, mock_driver = client

    mock_session = _make_session_mock(rows_24h=[], rows_7d=[])
    mock_driver.session.return_value = mock_session

    res = c.get('/api/investors/sentiment?graph_id=any')
    assert res.status_code == 200
    data = res.get_json()
    assert 'generated_at' in data['data']


def test_sentiment_endpoint_7d_present(client):
    c, mock_driver = client

    rows_24h = [{'asset_class': 'bonds', 'reaction': 'buy', 'confidence': 7.0, 'capital': 200_000}]
    rows_7d  = [{'asset_class': 'bonds', 'reaction': 'sell','confidence': 7.0, 'capital': 200_000}]
    mock_session = _make_session_mock(rows_24h=rows_24h, rows_7d=rows_7d)
    mock_driver.session.return_value = mock_session

    res = c.get('/api/investors/sentiment?graph_id=any')
    data = res.get_json()
    bonds = data['data']['by_asset_class']['bonds']
    assert bonds['24h']['score'] == pytest.approx(1.0)
    assert bonds['7d']['score'] == pytest.approx(-1.0)


def test_sentiment_endpoint_event_count_correct(client):
    c, mock_driver = client

    rows = [
        {'asset_class': 'equities', 'reaction': 'buy',  'confidence': 5.0, 'capital': 50_000},
        {'asset_class': 'equities', 'reaction': 'hold', 'confidence': 5.0, 'capital': 50_000},
        {'asset_class': 'equities', 'reaction': 'sell', 'confidence': 5.0, 'capital': 50_000},
    ]
    mock_session = _make_session_mock(rows_24h=rows, rows_7d=rows)
    mock_driver.session.return_value = mock_session

    res = c.get('/api/investors/sentiment?graph_id=any')
    data = res.get_json()
    assert data['data']['by_asset_class']['equities']['24h']['event_count'] == 3
