"""
news_fetcher.py — Poll RSS feeds and write a Quorum briefing .txt file.

Feeds (in order of processing):
  General RSS (relevance-filtered):
    - Reuters Top News
    - Yahoo Finance
    - CNBC Markets
    - MarketWatch Top Stories
    - FT Markets
    - Benzinga
    - Seeking Alpha
    - AP Business
  Nitter (relevance-filtered; toggled by NITTER_ENABLED):
    - 10 curated financial Twitter accounts via nitter.net
    - Disabled by default (NITTER_ENABLED = False) — public instances unreliable.
      Re-enable only when self-hosted Nitter is running in Docker.
  Central bank (never filtered; tagged [CENTRAL BANK] in briefing):
    - Federal Reserve press releases
    - ECB press releases
    - Bank of England news

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
from news_relevance_filter import filter_articles

import feedparser
import requests
from bs4 import BeautifulSoup

logger = get_logger('mirofish.news_fetcher')

# ---------------------------------------------------------------------------
# Feed configuration
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    ("Reuters",        "https://feeds.reuters.com/reuters/topNews"),
    ("Yahoo Finance",  "https://finance.yahoo.com/rss/topstories"),
    ("CNBC Markets",   "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
    ("MarketWatch",    "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("FT Markets",     "https://www.ft.com/rss/home/uk"),
    # Bloomberg removed — 403 on server requests, replace with authenticated source if needed
    # Unusual Whales removed — no public RSS feed, requires paid API
    ("Benzinga",      "https://www.benzinga.com/latest?page=1&feed=rss"),
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
    # Investopedia removed — 402 Payment Required, paywalled
    # TheStreet removed — could not find working RSS URL
    ("AP Business",  "https://apnews.com/hub/business/feed"),
]

# Set False to disable all Nitter feeds without code changes (e.g. if instances go down).
# Currently disabled — public nitter.net instances are timing out on every request.
# Re-enable only when self-hosted Nitter is running in Docker.
NITTER_ENABLED = False

NITTER_FEEDS = [
    ("Nitter/@unusual_whales",  "https://nitter.net/unusual_whales/rss"),
    ("Nitter/@DeItaone",        "https://nitter.net/DeItaone/rss"),
    ("Nitter/@financialjuice",  "https://nitter.net/financialjuice/rss"),
    ("Nitter/@WallStreetSilv",  "https://nitter.net/WallStreetSilv/rss"),
    ("Nitter/@zerohedge",       "https://nitter.net/zerohedge/rss"),
    ("Nitter/@markets",         "https://nitter.net/markets/rss"),
    ("Nitter/@bespokeinvest",   "https://nitter.net/bespokeinvest/rss"),
    ("Nitter/@elerianm",        "https://nitter.net/elerianm/rss"),
    ("Nitter/@NorthmanTrader",  "https://nitter.net/NorthmanTrader/rss"),
    ("Nitter/@ReformedBroker",  "https://nitter.net/ReformedBroker/rss"),
]

# Low-volume, high-signal feeds — never relevance-filtered.
# Every item from a central bank is financially relevant by definition.
# Articles are tagged source_type: "central_bank" for downstream weighting.
CENTRAL_BANK_FEEDS = [
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("ECB",             "https://www.ecb.europa.eu/rss/press.html"),
    ("Bank of England", "https://www.bankofengland.co.uk/rss/news"),
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
    # Timestamps are always stored as UTC ISO strings so lexicographic comparison
    # is equivalent to chronological comparison.
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

def parse_feed(source_name: str, feed_url: str, seen: dict, source_type: str = "", timeout: Optional[int] = None) -> list:
    """
    Parse a single RSS feed. Returns list of new article dicts.
    Any exception is caught — returns empty list on failure (dead feeds are non-fatal).

    source_type: optional tag added to each article (e.g. "central_bank").
                 Downstream code uses this to weight or label articles.
    timeout: if set, the feed URL is pre-fetched via requests with this timeout
             (seconds). A Timeout exception logs a WARNING and returns [].
             If None, feedparser fetches the URL directly (no enforced timeout).
    """
    try:
        if timeout is not None:
            try:
                resp = requests.get(feed_url, timeout=timeout, headers={"User-Agent": "MiroFish/1.0"})
                resp.raise_for_status()
                parsed = feedparser.parse(resp.text)
            except requests.exceptions.Timeout:
                logger.warning(f"{source_name} timed out")
                return []
            except Exception as e:
                logger.warning(f"Feed '{source_name}' failed: {e} — skipping")
                return []
        else:
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
            article = {
                'title': title,
                'source': source_name,
                'url': link,
                'hash': h,
                'body': body,
            }
            if source_type:
                article['source_type'] = source_type
            articles.append(article)

        logger.info(f"{source_name}: {len(articles)} new articles")
        return articles

    except Exception as e:
        logger.warning(f"Feed '{source_name}' failed: {e} — skipping")
        return []


# ---------------------------------------------------------------------------
# Briefing writer
# ---------------------------------------------------------------------------

def _build_regime_header() -> str:
    """
    TIER 3: Build a regime header string from backend/live/regime.json.
    Returns the header string, or a COMPUTING placeholder if unavailable.
    """
    import json as _json
    live_dir = Path(__file__).parent.parent / "live"
    regime_path = live_dir / "regime.json"
    try:
        if not regime_path.exists():
            return "[MARKET REGIME: COMPUTING...]\n"
        regime = _json.loads(regime_path.read_text(encoding="utf-8"))
        vol  = regime.get("volatility",   "INSUFFICIENT_DATA")
        tnd  = regime.get("trend",        "INSUFFICIENT_DATA")
        yc   = regime.get("yield_curve",  "INSUFFICIENT_DATA")
        fear = regime.get("fear",         "INSUFFICIENT_DATA")
        v_pct = regime.get("annualised_vol_pct")
        vix   = regime.get("vix")
        spread = regime.get("yield_spread_pct")

        # If any dimension is INSUFFICIENT_DATA, use the placeholder
        if any(v == "INSUFFICIENT_DATA" for v in (vol, tnd, yc, fear)):
            return "[MARKET REGIME: COMPUTING...]\n"

        vol_str = f"{vol} ({v_pct}% annualised)" if v_pct is not None else vol
        header = f"[MARKET REGIME: {vol_str} | {tnd} | {yc} | {fear}]"
        extras = []
        if vix is not None:
            extras.append(f"VIX: {vix}")
        if spread is not None:
            extras.append(f"Yield spread (10Y-3M): {spread}%")
        if extras:
            header += "\n" + "  ".join(extras)
        return header + "\n"
    except Exception as e:
        logger.warning(f"regime header: failed to read regime.json ({e}) — using placeholder")
        return "[MARKET REGIME: COMPUTING...]\n"


def write_briefing(articles: list, output_path: Path) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    # TIER 3: prepend market regime header
    regime_header = _build_regime_header()
    lines = [regime_header, f"MIROFISH BRIEFING — {now_str}\n{'='*60}\n"]
    for art in articles:
        source_label = art['source']
        if art.get('source_type') == 'central_bank':
            source_label = f"[CENTRAL BANK] {source_label}"
        lines.append(f"HEADLINE: {art['title']}")
        lines.append(f"SOURCE: {source_label}")
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
    Poll all feeds, deduplicate, apply relevance filter to general feeds,
    write briefing. Returns Path to briefing file, or None if no new articles.

    Processing order:
      1. General RSS feeds → relevance filter
      2. Nitter feeds (if enabled) → relevance filter (same pass)
      3. Central bank feeds → bypass filter, tag as central_bank
      4. Write briefing with all surviving articles
      5. Mark ALL fetched articles (including filtered-out) as seen so they
         are not re-fetched on the next run.
    """
    seen = load_seen_urls()

    # --- General RSS feeds ---
    # Benzinga and Seeking Alpha use a 5-second timeout: third-party aggregator feeds
    # can hang silently without an explicit timeout guard.
    _TIMEOUT_FEEDS = {"Benzinga", "Seeking Alpha"}
    general_articles: list = []
    for source_name, feed_url in RSS_FEEDS:
        feed_timeout = 5 if source_name in _TIMEOUT_FEEDS else None
        general_articles.extend(parse_feed(source_name, feed_url, seen, timeout=feed_timeout))

    # --- Nitter feeds (toggled by flag) ---
    # 5-second per-feed timeout: public Nitter instances can hang indefinitely.
    if NITTER_ENABLED:
        for source_name, feed_url in NITTER_FEEDS:
            general_articles.extend(parse_feed(source_name, feed_url, seen, timeout=5))

    # --- Apply relevance filter to all general + social feeds ---
    filtered_articles = filter_articles(general_articles)

    # --- Central bank feeds — never filtered, always tagged ---
    cb_articles: list = []
    for source_name, feed_url in CENTRAL_BANK_FEEDS:
        cb_articles.extend(parse_feed(source_name, feed_url, seen, source_type="central_bank"))

    all_articles = filtered_articles + cb_articles

    if not all_articles:
        logger.info("No new articles found.")
        # Still mark general articles as seen to avoid re-fetching on next run
        if general_articles or cb_articles:
            now_iso = datetime.now(timezone.utc).isoformat()
            for art in general_articles + cb_articles:
                seen[art['hash']] = now_iso
            save_seen_urls(seen)
        return None

    # Write briefing
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    output_path = BRIEFINGS_DIR / f"{ts}.txt"

    if dry_run:
        for art in all_articles:
            tag = " [CENTRAL BANK]" if art.get('source_type') == 'central_bank' else ""
            print(f"\n--- {art['source']}{tag}: {art['title']} ---\n{art['body'][:200]}...")
        return None

    write_briefing(all_articles, output_path)

    # Write feed breakdown sidecar for the dashboard per-feed article volume chart.
    # Counts all fetched articles (general_articles + cb_articles, before relevance
    # filtering) so the chart reflects every feed that returned content, not just the
    # articles that survived the filter.
    # Path: briefings/YYYY-MM-DD_HHMM_sources.json alongside the .txt briefing.
    try:
        from collections import Counter as _Counter
        source_counts = _Counter(a['source'] for a in general_articles + cb_articles)
        sidecar = [{"source": s, "count": c}
                   for s, c in source_counts.most_common()]
        sidecar_path = output_path.with_name(output_path.stem + "_sources.json")
        sidecar_path.write_text(
            json.dumps(sidecar, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"Sidecar sources write failed: {e} — feed breakdown will be unavailable")

    logger.info(
        f"Briefing written: {output_path} "
        f"({len(all_articles)} articles — {len(filtered_articles)} general, {len(cb_articles)} central bank)"
    )

    # Update seen-URL store with ALL fetched articles (including filtered-out ones)
    # so non-financial articles are not re-fetched on the next run.
    now_iso = datetime.now(timezone.utc).isoformat()
    for art in general_articles + cb_articles:
        seen[art['hash']] = now_iso
    save_seen_urls(seen)

    return output_path


def main():
    parser = argparse.ArgumentParser(description="Fetch RSS news and write Quorum briefing")
    parser.add_argument('--dry-run', action='store_true', help="Print to stdout, don't write file")
    args = parser.parse_args()
    fetch(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
