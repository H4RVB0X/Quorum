"""
price_fetcher.py — Daily closing price fetcher for MiroFish signal system.

Fetches the latest available closing prices for five asset-class proxy tickers
via yfinance and writes them to backend/prices/YYYY-MM-DD.json.

Ticker map:
  equities    → SPY
  crypto      → BTC-USD
  bonds       → TLT
  commodities → GLD
  fx          → DX-Y.NYB

Usage (standalone):
  python price_fetcher.py

Called by scheduler.py hourly_job().
"""

import json
import sys
import os
from datetime import date, datetime, timezone
from pathlib import Path

# Resolve backend/prices/ relative to this script regardless of cwd
_PRICES_DIR = Path(__file__).parent.parent / "prices"

ASSET_TICKERS = {
    "equities":    "SPY",
    "crypto":      "BTC-USD",
    "bonds":       "TLT",
    "commodities": "GLD",
    "fx":          "DX-Y.NYB",
    "mixed":       "VT",
    "real_estate": "VNQ",
}


def fetch_prices(prices_dir: Path = _PRICES_DIR) -> Path:
    """
    Fetch latest daily closing prices for all asset-class proxies and write
    them to prices_dir/YYYY-MM-DD.json.

    Uses period='2d' so we always get at least one complete trading day even
    when today's market hasn't closed yet.

    Returns the path of the written file.
    Raises ImportError if yfinance is not installed.
    """
    try:
        import yfinance as yf
    except ImportError as exc:
        raise ImportError(
            "yfinance is required for price_fetcher. "
            "Install with: pip install yfinance"
        ) from exc

    prices_dir.mkdir(parents=True, exist_ok=True)

    prices: dict = {}
    for asset, ticker in ASSET_TICKERS.items():
        try:
            data = yf.download(ticker, period="2d", progress=False, auto_adjust=True)
            if data.empty:
                prices[asset] = None
                continue
            close = data["Close"]
            # Handle multi-level columns produced by some yfinance versions
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
            last_close = float(close.dropna().iloc[-1])
            prices[asset] = round(last_close, 6)
        except Exception:
            prices[asset] = None

    today_str = date.today().isoformat()
    out_path = prices_dir / f"{today_str}.json"
    payload = {
        "date": today_str,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "prices": prices,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger = logging.getLogger(__name__)
    try:
        path = fetch_prices()
        logger.info("Prices written to %s", path)
        with open(path, encoding="utf-8") as f:
            print(f.read())
    except Exception as e:
        logger.error("price_fetcher failed: %s", e)
        sys.exit(1)