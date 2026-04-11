import yfinance as yf
import json
from datetime import datetime, date, timedelta
import pytz

TICKERS = {
    "equities": "SPY",
    "crypto": "BTC-USD",
    "bonds": "TLT",
    "commodities": "GLD",
    "fx": "DX-Y.NYB",
    "mixed": "VT",
    "real_estate": "VNQ",
}

# Last 7 days excluding today and 2026-04-10 (already have it)
TARGET_DATES = [
    date(2026, 4, 3),
    date(2026, 4, 4),
    date(2026, 4, 7),
    date(2026, 4, 8),
    date(2026, 4, 9),
]

OUTPUT_DIR = r"C:\Users\Harvey\Documents\Mirofish\MiroFish-Offline\backend\prices"

for target_date in TARGET_DATES:
    prices = {}
    start = target_date.strftime("%Y-%m-%d")
    end = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
    
    for asset_class, ticker in TICKERS.items():
        try:
            data = yf.download(ticker, start=start, end=end, progress=False)
            if not data.empty:
                prices[asset_class] = round(float(data["Close"].iloc[-1]), 6)
            else:
                print(f"No data for {ticker} on {target_date} (market closed?)")
        except Exception as e:
            print(f"Failed {ticker}: {e}")

    if prices:
        payload = {
            "date": start,
            "fetched_at": datetime.combine(target_date, datetime.min.time())
                          .replace(tzinfo=pytz.utc).isoformat(),
            "prices": prices
        }
        path = f"{OUTPUT_DIR}\\{start}.json"
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"Written {path}")