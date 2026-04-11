"""
news_relevance_filter.py — Keyword-based financial relevance filter for news articles.

Exposes a single function:
    filter_articles(articles: list[dict]) -> list[dict]

An article passes if its title or body/summary contains at least one term from
FINANCIAL_TERMS (case-insensitive) or matches a ticker-symbol regex ($AAPL, #SPX).

Central bank articles should bypass this filter entirely — they are always relevant
by definition. This module is for general RSS and social feeds.

If briefings become very short, expand FINANCIAL_TERMS. If non-financial articles
are slipping through, add more specific exclusions upstream rather than shrinking
the whitelist here.
"""
import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.utils.logger import get_logger

logger = get_logger('mirofish.news_relevance_filter')

# ~80 terms covering ticker patterns, market structure, macro, corporates, and sectors.
# All matched case-insensitively via substring search.
FINANCIAL_TERMS = {
    # Major indices
    "s&p", "dow", "nasdaq", "nyse", "ftse", "dax", "nikkei", "russell", "vix",
    # Central banks / macro policy
    "fed", "federal reserve", "ecb", "boe", "bank of england", "fomc",
    "rate hike", "rate cut", "interest rate",
    "rate", "inflation", "cpi", "ppi", "pce", "gdp",
    "unemployment", "payroll", "payrolls", "nonfarm",
    # Rates / fixed income
    "yield", "yield curve", "treasury", "bond", "bonds", "gilts", "bund",
    # Equities / general market
    "equity", "equities", "stock", "stocks", "shares", "market cap",
    "bull market", "bear market", "rally", "selloff", "sell-off",
    "correction", "crash", "volatility",
    # Instruments / structures
    "etf", "futures", "options", "derivatives", "swap", "cds",
    "leverage", "margin", "short", "short-selling", "long position",
    "liquidity", "hedge", "hedging",
    # Commodities / FX / Crypto
    "commodity", "commodities", "crude", "oil", "gold", "silver", "copper",
    "forex", "fx", "currency", "dollar", "euro", "yen", "sterling",
    "crypto", "bitcoin", "ethereum", "btc", "defi", "stablecoin",
    # Real assets
    "reit", "real estate",
    # Corporate events
    "earnings", "revenue", "profit", "loss", "eps", "ebitda",
    "guidance", "quarterly results", "annual results",
    "ipo", "spac", "m&a", "merger", "acquisition", "buyout", "takeover",
    "dividend", "buyback", "share repurchase",
    "bankruptcy", "default", "restructuring", "liquidation", "insolvency",
    # Market conditions / macro themes
    "recession", "contraction", "stagflation", "expansion",
    "sanctions", "tariff", "tariffs", "trade war",
    # Analyst / ratings
    "analyst", "upgrade", "downgrade", "price target", "outlook", "forecast",
    "rating", "overweight", "underweight", "neutral",
    # Sectors
    "bank", "banks", "financial", "fintech", "insurance",
    "energy", "healthcare", "pharma", "semiconductor",
    # High-signal generic terms
    "market", "markets", "trading", "invest", "investor", "portfolio",
    "fund", "funds", "asset management", "asset class",
}

# Compiled for fast lookup — all terms stored lowercase
_TERMS_LOWER = frozenset(FINANCIAL_TERMS)

# Ticker-symbol regex: $AAPL, $BTC, #SPX etc. — matches original (not lowercased) text
_TICKER_RE = re.compile(r'[\$#][A-Z]{1,6}\b')


def _passes_filter(article: dict) -> bool:
    """Return True if the article contains at least one financial signal."""
    title = str(article.get('title', ''))
    body = str(article.get('body', '') or article.get('summary', ''))
    raw_text = title + ' ' + body

    # Check ticker-symbol pattern on original text (symbols are uppercase)
    if _TICKER_RE.search(raw_text):
        return True

    # Check keyword whitelist on lowercased text
    text_lower = raw_text.lower()
    for term in _TERMS_LOWER:
        if term in text_lower:
            return True

    return False


def filter_articles(articles: list) -> list:
    """
    Return only articles that pass the financial relevance check.
    Logs pass/fail counts at INFO level every call.
    """
    if not articles:
        return []

    passed = [a for a in articles if _passes_filter(a)]
    removed = len(articles) - len(passed)

    if removed > 0:
        logger.info(
            f"Relevance filter: {len(passed)}/{len(articles)} articles passed "
            f"({removed} removed as non-financial)"
        )
    else:
        logger.info(f"Relevance filter: all {len(articles)} articles passed")

    return passed
