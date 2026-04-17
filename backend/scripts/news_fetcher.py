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

# ---------------------------------------------------------------------------
# Nitter self-hosting guide
#
# Public nitter.net instances are unreliable (timeouts, bans, rate limits).
# To reliably feed Twitter/X content into Quorum, run your own Nitter in Docker.
#
# Docker image:
#   ghcr.io/zedeus/nitter:latest  (actively maintained community fork)
#
# docker-compose.yml service block to add:
# ─────────────────────────────────────────────────────────────────────────────
#   nitter:
#     image: ghcr.io/zedeus/nitter:latest
#     container_name: nitter
#     ports:
#       - "8080:8080"
#     environment:
#       - NITTER_HOST=0.0.0.0
#       - NITTER_PORT=8080
#       # Twitter bearer token (required for API v2 access):
#       - NITTER_BEARER_TOKEN=${NITTER_BEARER_TOKEN}
#     restart: unless-stopped
# ─────────────────────────────────────────────────────────────────────────────
#
# Required environment variables (add to .env):
#   NITTER_BEARER_TOKEN=<your Twitter API v2 Bearer Token>
#
# Once nitter is healthy:
#   1. Update NITTER_FEEDS below to use http://localhost:8080 instead of https://nitter.net
#   2. Set NITTER_ENABLED = True
#   3. docker cp this file into the container and restart mirofish-offline
# ---------------------------------------------------------------------------

# Set False to disable all Nitter feeds without code changes (e.g. if instances go down).
# Currently disabled — public nitter.net instances are timing out on every request.
# Re-enable only when self-hosted Nitter is running in Docker (see guide above).
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

# ---------------------------------------------------------------------------
# Reddit JSON API feeds (CHANGE 1)
# Use JSON API (not feedparser) — unauthenticated, no OAuth required.
# User-Agent required to avoid 429; 5s timeout per subreddit.
# ---------------------------------------------------------------------------
REDDIT_ENABLED = True

REDDIT_SOURCES = {
    "SecurityAnalysis": {"min_score": 10,  "min_body_chars": 100},
    "investing":        {"min_score": 50,  "min_body_chars": 150},
    "economics":        {"min_score": 50,  "min_body_chars": 100},
    "finance":          {"min_score": 50,  "min_body_chars": 100},
    "stocks":           {"min_score": 100, "min_body_chars": 150},
    "wallstreetbets":   {
        "min_score":      200,
        "min_body_chars": 300,
        "allowed_flairs": {
            "DD", "News", "Discussion", "Earnings Thread",
            "Fundamentals", "Technical Analysis",
        },
    },
}
_REDDIT_CAP = 15  # max total Reddit articles per cycle across all subreddits

# ---------------------------------------------------------------------------
# Stocktwits unauthenticated public API (CHANGE 2)
# 200 req/hour rate limit — one request per symbol per cycle is safe.
# No auth headers needed. Filter: sentiment not null + followers >= 50.
# ---------------------------------------------------------------------------
STOCKTWITS_ENABLED = True
STOCKTWITS_SYMBOLS = ["SPY", "BTC", "GLD", "TLT", "DX-Y.NYB", "VNQ", "VT"]

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
# Reddit + Stocktwits fetchers
# ---------------------------------------------------------------------------

def fetch_reddit(seen: dict) -> tuple:
    """
    Fetch financial posts from configured subreddits via the Reddit JSON API.
    Returns (capped_articles, all_qualifying_hashes, per_subreddit_counts).

    capped_articles: top-_REDDIT_CAP posts by score, pre-filtered, ready for
                     filter_articles(). Each article's hash is "reddit_{id}".
    all_qualifying_hashes: hashes for ALL posts that passed gates (to mark seen).
    per_subreddit_counts: {"investing": 3, ...} for sidecar.
    """
    if not REDDIT_ENABLED:
        return [], [], {}

    all_posts: list = []  # (score, article_dict)
    counts: dict = {}

    for subreddit, cfg in REDDIT_SOURCES.items():
        try:
            url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=25"
            resp = requests.get(
                url,
                headers={"User-Agent": "Quorum/1.0 financial-simulation"},
                timeout=5,
            )
            if resp.status_code != 200:
                logger.warning(f"Reddit r/{subreddit}: HTTP {resp.status_code} — skipping")
                counts[subreddit] = 0
                continue

            data = resp.json()
            posts = data.get("data", {}).get("children", [])

            min_score    = cfg["min_score"]
            min_body     = cfg["min_body_chars"]
            allowed_flairs = cfg.get("allowed_flairs")

            sub_count = 0
            for child in posts:
                post = child.get("data", {})
                post_id = post.get("id", "")
                if not post_id:
                    continue
                score = post.get("score", 0) or 0
                body  = post.get("selftext", "") or ""
                flair = post.get("link_flair_text")

                if score < min_score:
                    continue
                if len(body) < min_body:
                    continue
                # WSB flair filter — only applied when allowed_flairs is set
                if allowed_flairs is not None and flair not in allowed_flairs:
                    continue

                dedup_key = f"reddit_{post_id}"
                if dedup_key in seen:
                    continue

                article = {
                    "title": post.get("title", "Untitled"),
                    "source": f"[REDDIT/{subreddit}]",
                    "url":   post.get("url", f"https://reddit.com/r/{subreddit}/comments/{post_id}"),
                    "hash":  dedup_key,
                    "body":  body,
                }
                all_posts.append((score, article))
                sub_count += 1

            counts[subreddit] = sub_count
            logger.info(f"Reddit r/{subreddit}: {sub_count} qualifying posts")

        except Exception as e:
            logger.warning(f"Reddit r/{subreddit} failed: {e} — skipping")
            counts[subreddit] = 0

    # Cap at _REDDIT_CAP, highest-scoring first
    all_posts.sort(key=lambda x: x[0], reverse=True)
    capped      = [art for _, art in all_posts[:_REDDIT_CAP]]
    all_hashes  = [art["hash"] for _, art in all_posts]

    return capped, all_hashes, counts


def fetch_stocktwits(seen: dict) -> tuple:
    """
    Fetch pre-labelled messages from Stocktwits public API.
    Returns (articles, message_dedup_keys, per_symbol_counts).

    Messages are NOT passed through filter_articles() — already ticker-specific.
    Each symbol produces one combined article (summary + top-5 messages).
    """
    if not STOCKTWITS_ENABLED:
        return [], [], {}

    articles: list  = []
    all_keys: list  = []
    counts: dict    = {}

    for symbol in STOCKTWITS_SYMBOLS:
        try:
            url  = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                logger.warning(f"Stocktwits {symbol}: HTTP {resp.status_code} — skipping")
                counts[symbol] = 0
                continue

            data     = resp.json()
            messages = data.get("messages", [])

            qualified: list = []
            for msg in messages:
                entities       = msg.get("entities") or {}
                sentiment_obj  = entities.get("sentiment") or {}
                sentiment_basic = sentiment_obj.get("basic") if sentiment_obj else None
                if not sentiment_basic:
                    continue
                followers = (msg.get("user") or {}).get("followers", 0) or 0
                if followers < 50:
                    continue
                msg_id = msg.get("id")
                if not msg_id:
                    continue
                dedup_key = f"stocktwits_{msg_id}"
                if dedup_key in seen:
                    continue
                qualified.append({
                    "id":        msg_id,
                    "dedup_key": dedup_key,
                    "body":      msg.get("body", ""),
                    "sentiment": sentiment_basic,
                    "followers": followers,
                })

            # Top 5 by followers
            qualified.sort(key=lambda x: x["followers"], reverse=True)
            top_msgs = qualified[:5]

            if not top_msgs:
                counts[symbol] = 0
                continue

            bullish = sum(1 for m in top_msgs if m["sentiment"] == "Bullish")
            bearish = sum(1 for m in top_msgs if m["sentiment"] == "Bearish")

            summary   = (
                f"[STOCKTWITS SENTIMENT: {symbol} {bullish} bullish / "
                f"{bearish} bearish in last 30 messages]"
            )
            msg_block = "\n\n".join(
                f"[STOCKTWITS/{symbol} {m['sentiment']}] {m['body']}"
                for m in top_msgs
            )

            articles.append({
                "title":  f"Stocktwits {symbol}: {bullish} bullish / {bearish} bearish",
                "source": f"Stocktwits/{symbol}",
                "url":    f"https://stocktwits.com/symbol/{symbol}",
                "hash":   (
                    f"stocktwits_block_{symbol}_"
                    f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"
                ),
                "body":   f"{summary}\n\n{msg_block}",
            })
            all_keys.extend(m["dedup_key"] for m in qualified)  # mark all seen
            counts[symbol] = len(top_msgs)
            logger.info(f"Stocktwits {symbol}: {len(top_msgs)} messages ({bullish} bull / {bearish} bear)")

        except Exception as e:
            logger.warning(f"Stocktwits {symbol} failed: {e} — skipping")
            counts[symbol] = 0

    return articles, all_keys, counts


# ---------------------------------------------------------------------------
# Briefing writer
# ---------------------------------------------------------------------------

def _build_regime_header() -> str:
    """
    TIER 3 / CHANGE 5: Build a regime + calendar header string.
    Sources: backend/live/regime.json, economic_calendar.json, earnings_calendar.json.
    Returns the header string, or a COMPUTING placeholder if unavailable.
    """
    import json as _json
    live_dir = Path(__file__).parent.parent / "live"
    regime_path = live_dir / "regime.json"
    lines = []
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
            lines.append("[MARKET REGIME: COMPUTING...]")
        else:
            vol_str = f"{vol} ({v_pct}% annualised)" if v_pct is not None else vol
            regime_line = f"[MARKET REGIME: {vol_str} | {tnd} | {yc} | {fear}]"
            extras = []
            if vix is not None:
                extras.append(f"VIX: {vix}")
            if spread is not None:
                extras.append(f"Yield spread (10Y-3M): {spread}%")
            # CHANGE 5: VIX term structure
            vts = regime.get("vix_term_structure")
            vix3m = regime.get("vix3m")
            if vts and vix3m is not None:
                extras.append(f"VIX3M: {vix3m} ({vts})")
            # CHANGE 5: institutional flow signal
            flow = regime.get("flow_signal")
            if flow:
                extras.append(f"Flow: {flow.upper()}")
            if extras:
                regime_line += "\n" + "  ".join(extras)
            lines.append(regime_line)
    except Exception as e:
        logger.warning(f"regime header: failed to read regime.json ({e}) — using placeholder")
        lines.append("[MARKET REGIME: COMPUTING...]")

    # CHANGE 5: Economic calendar — upcoming 48h events
    try:
        eco_path = live_dir / "economic_calendar.json"
        if eco_path.exists():
            eco = _json.loads(eco_path.read_text(encoding="utf-8"))
            events = eco.get("events", [])
            if events:
                lines.append("\n[UPCOMING ECONOMIC EVENTS — next 48h]")
                for ev in events[:5]:  # top 5 to keep briefing compact
                    impact = ev.get("impact", "")
                    impact_str = f" ({impact})" if impact and impact not in ("nan", "None") else ""
                    forecast = ev.get("forecast", "")
                    forecast_str = f" | est: {forecast}" if forecast and forecast not in ("nan", "None") else ""
                    lines.append(f"  • {ev.get('event','')} [{ev.get('country','')}]{impact_str}{forecast_str}")
    except Exception as e:
        logger.warning(f"regime header: economic calendar read failed ({e})")

    # CHANGE 5: Earnings calendar — upcoming 24h
    try:
        earn_path = live_dir / "earnings_calendar.json"
        if earn_path.exists():
            earn = _json.loads(earn_path.read_text(encoding="utf-8"))
            events = earn.get("events", [])
            if events:
                lines.append("\n[EARNINGS REPORTS — next 24h]")
                for ev in events[:5]:
                    eps = ev.get("eps_est", "")
                    eps_str = f" | EPS est: {eps}" if eps and eps not in ("nan", "None") else ""
                    timing = ev.get("timing", "")
                    timing_str = f" ({timing})" if timing and timing not in ("nan", "None") else ""
                    lines.append(f"  • {ev.get('symbol','')} — {ev.get('name','')}{timing_str}{eps_str}")
    except Exception as e:
        logger.warning(f"regime header: earnings calendar read failed ({e})")

    return "\n".join(lines) + "\n"


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

    full_text = "\n".join(lines)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(full_text, encoding="utf-8")


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

    # --- Reddit feeds (CHANGE 1) — after general RSS, before central bank ---
    reddit_raw: list     = []
    reddit_all_hashes: list = []
    reddit_counts: dict  = {}
    if REDDIT_ENABLED:
        reddit_raw, reddit_all_hashes, reddit_counts = fetch_reddit(seen)

    # --- Apply relevance filter to all general + Reddit articles ---
    filtered_articles = filter_articles(general_articles + reddit_raw)

    # --- Central bank feeds — never filtered, always tagged ---
    cb_articles: list = []
    for source_name, feed_url in CENTRAL_BANK_FEEDS:
        cb_articles.extend(parse_feed(source_name, feed_url, seen, source_type="central_bank"))

    # --- Stocktwits feeds (CHANGE 2) — ticker-specific, no relevance filter needed ---
    stocktwits_articles: list    = []
    stocktwits_msg_keys: list    = []
    stocktwits_counts: dict      = {}
    if STOCKTWITS_ENABLED:
        stocktwits_articles, stocktwits_msg_keys, stocktwits_counts = fetch_stocktwits(seen)

    all_articles = filtered_articles + cb_articles + stocktwits_articles

    if not all_articles:
        logger.info("No new articles found.")
        if general_articles or reddit_raw or cb_articles or stocktwits_articles:
            now_iso = datetime.now(timezone.utc).isoformat()
            for art in general_articles + reddit_raw + cb_articles:
                seen[art['hash']] = now_iso
            for h in reddit_all_hashes + stocktwits_msg_keys:
                seen[h] = now_iso
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

    # Write feed breakdown sidecar.
    # "sources" key: per-RSS/CB source counts (same as before).
    # "reddit" key: per-subreddit counts of qualifying posts (pre-filter).
    # "stocktwits" key: per-symbol counts of included messages.
    try:
        from collections import Counter as _Counter
        source_counts = _Counter(a['source'] for a in general_articles + cb_articles)
        sidecar = {
            "sources":    [{"source": s, "count": c} for s, c in source_counts.most_common()],
            "reddit":     reddit_counts,
            "stocktwits": stocktwits_counts,
        }
        sidecar_path = output_path.with_name(output_path.stem + "_sources.json")
        sidecar_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Sidecar sources write failed: {e} — feed breakdown will be unavailable")

    logger.info(
        f"Briefing written: {output_path} "
        f"({len(all_articles)} articles — {len(filtered_articles)} general+reddit, "
        f"{len(cb_articles)} central bank, {len(stocktwits_articles)} stocktwits)"
    )

    # Update seen-URL store: mark all fetched articles (including filtered-out)
    # so non-financial items are not re-fetched on the next run.
    now_iso = datetime.now(timezone.utc).isoformat()
    for art in general_articles + reddit_raw + cb_articles:
        seen[art['hash']] = now_iso
    for h in reddit_all_hashes + stocktwits_msg_keys:
        seen[h] = now_iso
    save_seen_urls(seen)

    return output_path


def main():
    parser = argparse.ArgumentParser(description="Fetch RSS news and write Quorum briefing")
    parser.add_argument('--dry-run', action='store_true', help="Print to stdout, don't write file")
    args = parser.parse_args()
    fetch(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
