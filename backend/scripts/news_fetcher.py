"""
news_fetcher.py — Poll RSS feeds and write a MiroFish briefing .txt file.

Feeds:
  - Reuters Top News
  - Yahoo Finance
  - CNBC Markets
  - MarketWatch Top Stories

Usage:
  python news_fetcher.py          # writes to ../briefings/YYYY-MM-DD_HHMM.txt
  python news_fetcher.py --dry-run  # print briefing to stdout, don't write file
"""
import sys
import os
import json
import hashlib
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.utils.logger import get_logger

import feedparser
import requests
from bs4 import BeautifulSoup

logger = get_logger('mirofish.news_fetcher')

RSS_FEEDS = [
    ("Reuters",     "https://feeds.reuters.com/reuters/topNews"),
    ("Yahoo Finance", "https://finance.yahoo.com/rss/topstories"),
    ("CNBC Markets",  "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
    ("MarketWatch",   "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
]

BRIEFINGS_DIR = Path(__file__).parent.parent / "briefings"
SEEN_URLS_PATH = Path(__file__).parent / "seen_urls.json"
MIN_BODY_CHARS = 200
URL_TTL_DAYS = 30


# ---------------------------------------------------------------------------
# Seen-URL deduplication with 30-day TTL
# ---------------------------------------------------------------------------

def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def load_seen_urls() -> dict:
    if SEEN_URLS_PATH.exists():
        with open(SEEN_URLS_PATH) as f:
            return json.load(f)
    return {}


def prune_seen_urls(seen: dict) -> dict:
    # Timestamps are always stored as UTC ISO strings (e.g. "2026-04-09T12:00:00+00:00")
    # so lexicographic comparison is equivalent to chronological comparison.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=URL_TTL_DAYS)).isoformat()
    return {h: ts for h, ts in seen.items() if ts > cutoff}


def save_seen_urls(seen: dict) -> None:
    pruned = prune_seen_urls(seen)
    with open(SEEN_URLS_PATH, 'w') as f:
        json.dump(pruned, f, indent=2)


# ---------------------------------------------------------------------------
# Article body fetching with silent fallback
# ---------------------------------------------------------------------------

def fetch_body(url: str, rss_summary: str = "") -> str:
    """
    Attempt to fetch and parse article body. Falls back to rss_summary if:
      - requests.get raises any exception
      - HTTP status is not 200
      - Parsed body text is < MIN_BODY_CHARS characters
    Logs fallback to stdout but never raises.
    """
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "MiroFish/1.0"})
        if resp.status_code != 200:
            logger.info(f"Non-200 ({resp.status_code}) for {url} — using RSS summary")
            return rss_summary

        soup = BeautifulSoup(resp.text, "html.parser")
        paragraphs = soup.find_all("p")
        body = " ".join(p.get_text(strip=True) for p in paragraphs)

        if len(body) < MIN_BODY_CHARS:
            logger.info(f"Body too short ({len(body)} chars) for {url} — using RSS summary")
            return rss_summary

        return body

    except Exception as e:
        logger.warning(f"Body fetch failed for {url}: {e} — using RSS summary")
        return rss_summary


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------

def parse_feed(source_name: str, feed_url: str, seen: dict) -> list:
    """
    Parse a single RSS feed. Returns list of new article dicts.
    Any exception is caught — returns empty list on failure.
    """
    try:
        parsed = feedparser.parse(feed_url)
        articles = []
        for entry in parsed.entries:
            link = getattr(entry, 'link', '')
            if not link:
                continue
            h = url_hash(link)
            if h in seen:
                continue

            summary = getattr(entry, 'summary', '') or getattr(entry, 'description', '') or ''
            title = getattr(entry, 'title', 'Untitled')

            body = fetch_body(link, rss_summary=summary)
            articles.append({
                'title': title,
                'source': source_name,
                'url': link,
                'hash': h,
                'body': body,
            })

        logger.info(f"{source_name}: {len(articles)} new articles")
        return articles

    except Exception as e:
        logger.warning(f"Feed '{source_name}' failed: {e} — skipping")
        return []


# ---------------------------------------------------------------------------
# Briefing writer
# ---------------------------------------------------------------------------

def write_briefing(articles: list, output_path: Path) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"MIROFISH BRIEFING — {now_str}\n{'='*60}\n"]
    for art in articles:
        lines.append(f"HEADLINE: {art['title']}")
        lines.append(f"SOURCE: {art['source']}")
        lines.append(f"URL: {art['url']}")
        lines.append("")
        lines.append(art['body'])
        lines.append("\n---\n")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def fetch(dry_run: bool = False) -> Optional[Path]:
    """
    Poll all feeds, deduplicate, write briefing.
    Returns Path to briefing file, or None if no new articles.
    """
    seen = load_seen_urls()
    all_articles = []

    for source_name, feed_url in RSS_FEEDS:
        articles = parse_feed(source_name, feed_url, seen)
        all_articles.extend(articles)

    if not all_articles:
        logger.info("No new articles found.")
        return None

    # Write briefing
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    output_path = BRIEFINGS_DIR / f"{ts}.txt"

    if dry_run:
        for art in all_articles:
            print(f"\n--- {art['source']}: {art['title']} ---\n{art['body'][:200]}...")
        return None

    write_briefing(all_articles, output_path)
    logger.info(f"Briefing written: {output_path} ({len(all_articles)} articles)")

    # Update seen-URL store (after briefing is safely on disk)
    now_iso = datetime.now(timezone.utc).isoformat()
    for art in all_articles:
        seen[art['hash']] = now_iso
    save_seen_urls(seen)

    return output_path


def main():
    parser = argparse.ArgumentParser(description="Fetch RSS news and write MiroFish briefing")
    parser.add_argument('--dry-run', action='store_true', help="Print to stdout, don't write file")
    args = parser.parse_args()
    fetch(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
