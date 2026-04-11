"""Tests for news_fetcher.py."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))

from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta


def test_url_hash_is_deterministic():
    from news_fetcher import url_hash
    h1 = url_hash("https://example.com/article")
    h2 = url_hash("https://example.com/article")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_prune_seen_urls_removes_old_entries():
    from news_fetcher import prune_seen_urls
    old_ts = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    new_ts = datetime.now(timezone.utc).isoformat()
    seen = {"abc": old_ts, "def": new_ts}
    pruned = prune_seen_urls(seen)
    assert "abc" not in pruned
    assert "def" in pruned


def test_fetch_body_falls_back_on_short_response():
    """If body is < 200 chars, fallback to rss_summary."""
    with patch('news_fetcher.requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body><p>Short.</p></body></html>"
        mock_get.return_value = mock_resp

        from news_fetcher import fetch_body
        result = fetch_body("https://example.com", rss_summary="RSS fallback text here")
    assert result == "RSS fallback text here"


def test_fetch_body_falls_back_on_request_exception():
    """If requests.get raises, return rss_summary."""
    with patch('news_fetcher.requests.get', side_effect=Exception("timeout")):
        from news_fetcher import fetch_body
        result = fetch_body("https://example.com", rss_summary="fallback")
    assert result == "fallback"


def test_fetch_body_falls_back_on_non_200():
    """If status != 200, return rss_summary."""
    with patch('news_fetcher.requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_get.return_value = mock_resp

        from news_fetcher import fetch_body
        result = fetch_body("https://example.com", rss_summary="fallback")
    assert result == "fallback"
