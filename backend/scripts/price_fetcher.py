"""
price_fetcher.py — Daily closing price fetcher for MiroFish signal system.

Fetches the latest available closing prices for five asset-class proxy tickers
via yfinance (with OpenBB as optional primary source) and writes them to
backend/prices/YYYY-MM-DD.json.

Ticker map:
  equities    → SPY
  crypto      → BTC-USD
  bonds       → TLT
  commodities → GLD
  fx          → DX-Y.NYB
  mixed       → VT
  real_estate → VNQ

Reliability improvements (2026-04-14):
  - Network pre-check: HEAD https://finance.yahoo.com before any yfinance calls.
    If unreachable, skip the entire fetch cycle rather than writing partial data.
  - Per-ticker retry: up to 3 attempts with 5-second delay for empty/failed data.
  - Per-ticker error logging at WARNING level with ticker name and error type.
  - compute_price_staleness_hours() helper for signals.py data_quality checks.

TIER 3 additions (2026-04-14):
  - compute_market_regime() computes volatility/trend/yield_curve/fear regime
    from price history + OpenBB (with yfinance fallback). Written to backend/live/regime.json.
  - Price snapshot now embeds regime dict under "regime" key.
  - OpenBB is an optional dependency — all OpenBB calls are wrapped in try/except
    with yfinance fallback. If OpenBB is unavailable, everything continues normally.

Usage (standalone):
  python price_fetcher.py

Called by scheduler.py hourly_job().
"""

import json
import math
import sys
import os
import time
import logging
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# Resolve backend/prices/ and backend/live/ relative to this script regardless of cwd
_PRICES_DIR = Path(__file__).parent.parent / "prices"
_LIVE_DIR   = Path(__file__).parent.parent / "live"

ASSET_TICKERS = {
    "equities":    "SPY",
    "crypto":      "BTC-USD",
    "bonds":       "TLT",
    "commodities": "GLD",
    "fx":          "DX-Y.NYB",
    "mixed":       "VT",
    "real_estate": "VNQ",
}

_CONNECTIVITY_URL = "https://finance.yahoo.com"
_CONNECTIVITY_TIMEOUT = 5  # seconds
_MAX_RETRIES = 3
_RETRY_DELAY = 5  # seconds between retries

logger = logging.getLogger(__name__)


def _check_network() -> bool:
    """
    Quick connectivity check against finance.yahoo.com.
    Returns True if reachable, False otherwise.
    Logs a WARNING if the check fails.
    """
    try:
        import requests
        resp = requests.get(_CONNECTIVITY_URL, timeout=_CONNECTIVITY_TIMEOUT)
        return resp.status_code < 500
    except Exception as e:
        logger.warning(f"Network pre-check failed ({type(e).__name__}: {e}) — skipping price fetch cycle")
        return False


def _fetch_ticker_with_retry(yf, ticker: str, asset: str) -> Optional[float]:
    """
    Fetch closing price for a single ticker with up to _MAX_RETRIES attempts.
    Returns the last close as float, or None on persistent failure.
    Logs WARNING for every failed attempt with ticker and error type.
    """
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            data = yf.download(ticker, period="2d", progress=False, auto_adjust=True)
            if data.empty:
                logger.warning(
                    f"price_fetcher: {ticker} ({asset}) returned empty data "
                    f"(attempt {attempt}/{_MAX_RETRIES})"
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY)
                continue
            close = data["Close"]
            # Handle multi-level columns produced by some yfinance versions
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
            last_close = float(close.dropna().iloc[-1])
            if attempt > 1:
                logger.info(f"price_fetcher: {ticker} ({asset}) succeeded on attempt {attempt}")
            return round(last_close, 6)
        except Exception as e:
            last_exc = e
            logger.warning(
                f"price_fetcher: {ticker} ({asset}) failed — "
                f"{type(e).__name__}: {e} (attempt {attempt}/{_MAX_RETRIES})"
            )
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY)

    logger.warning(
        f"price_fetcher: {ticker} ({asset}) — all {_MAX_RETRIES} attempts failed; "
        f"last error: {type(last_exc).__name__}: {last_exc}"
    )
    return None


def compute_price_staleness_hours(prices_dir: Path = _PRICES_DIR) -> Optional[float]:
    """
    Return the age in hours of the most recent price file in prices_dir.
    Returns None if no price files exist.
    Used by signals.py to set data_quality: 'degraded' when >36 hours stale.
    """
    if not prices_dir.is_dir():
        return None
    files = sorted(prices_dir.glob("????-??-??.json"), reverse=True)
    if not files:
        return None
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
        fetched_at_str = data.get("fetched_at")
        if fetched_at_str:
            fetched_at = datetime.fromisoformat(fetched_at_str.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - fetched_at
            return delta.total_seconds() / 3600.0
    except Exception:
        pass
    # Fallback: use file mtime
    try:
        mtime = files[0].stat().st_mtime
        delta_s = datetime.now(timezone.utc).timestamp() - mtime
        return delta_s / 3600.0
    except Exception:
        return None


def compute_market_regime(prices_dir: Path = _PRICES_DIR) -> dict:
    """
    TIER 3: Compute a market regime dict from historical price files and live data.

    Dimensions:
      volatility  — 20-day annualised SPY vol (LOW/NORMAL/HIGH/EXTREME/INSUFFICIENT_DATA)
      trend       — MA-20 vs MA-50 vs current SPY price (BULL_TRENDING/BEAR_TRENDING/SIDEWAYS/MIXED/INSUFFICIENT_DATA)
      yield_curve — OpenBB federal_reserve yield spread or TLT/SPY fallback
      fear        — VIX via OpenBB or vol-proxy fallback

    Writes result to backend/live/regime.json.
    Returns the regime dict (also returned to caller — embedded in price snapshot).
    Never raises — failures are logged and degraded gracefully.
    """
    regime: dict = {
        "volatility":         "INSUFFICIENT_DATA",
        "trend":              "INSUFFICIENT_DATA",
        "yield_curve":        "INSUFFICIENT_DATA",
        "fear":               "INSUFFICIENT_DATA",
        "annualised_vol_pct": None,
        "vix":                None,
        "yield_spread_pct":   None,
        "computed_at":        datetime.now(timezone.utc).isoformat(),
        "data_sources":       [],
        "insufficient_data":  True,
    }

    # ── Step 1: Load SPY price history ───────────────────────────────────────
    spy_prices: list[float] = []
    try:
        if prices_dir.is_dir():
            files = sorted(prices_dir.glob("????-??-??.json"), reverse=False)
            for f in files:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    p = data.get("prices", {}).get("equities")
                    if p is not None:
                        spy_prices.append(float(p))
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"regime: SPY history load failed: {e}")

    tlt_prices: list[float] = []
    try:
        if prices_dir.is_dir():
            files = sorted(prices_dir.glob("????-??-??.json"), reverse=False)
            for f in files:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    p = data.get("prices", {}).get("bonds")
                    if p is not None:
                        tlt_prices.append(float(p))
                except Exception:
                    continue
    except Exception as e:
        logger.warning(f"regime: TLT history load failed: {e}")

    # ── Step 2: Volatility regime (20-day annualised) ────────────────────────
    annualised_vol = None
    if len(spy_prices) >= 5:
        try:
            recent = spy_prices[-21:]  # need 21 prices for 20 returns
            returns = [(recent[i] - recent[i-1]) / recent[i-1]
                       for i in range(1, len(recent))]
            if len(returns) >= 5:
                mean_r = sum(returns) / len(returns)
                variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
                std_daily = math.sqrt(variance)
                annualised_vol = round(std_daily * math.sqrt(252) * 100, 1)
                regime["annualised_vol_pct"] = annualised_vol
                regime["data_sources"].append("price_history")

                if annualised_vol < 15:
                    regime["volatility"] = "LOW_VOLATILITY"
                elif annualised_vol < 30:
                    regime["volatility"] = "NORMAL_VOLATILITY"
                elif annualised_vol < 50:
                    regime["volatility"] = "HIGH_VOLATILITY"
                else:
                    regime["volatility"] = "EXTREME_VOLATILITY"
        except Exception as e:
            logger.warning(f"regime: volatility computation failed: {e}")

    # ── Step 3: Trend regime (MA-20 vs MA-50) ────────────────────────────────
    if len(spy_prices) >= 20:
        try:
            current = spy_prices[-1]
            ma20 = sum(spy_prices[-20:]) / 20
            if len(spy_prices) >= 50:
                ma50 = sum(spy_prices[-50:]) / 50
                pct_diff = abs(ma20 - ma50) / ma50 if ma50 else 1.0
                if pct_diff <= 0.01:
                    regime["trend"] = "SIDEWAYS"
                elif current > ma20 > ma50:
                    regime["trend"] = "BULL_TRENDING"
                elif current < ma20 < ma50:
                    regime["trend"] = "BEAR_TRENDING"
                else:
                    regime["trend"] = "MIXED"
            else:
                # Fewer than 50 days — use MA20 vs current only
                if current > ma20:
                    regime["trend"] = "BULL_TRENDING"
                elif current < ma20:
                    regime["trend"] = "BEAR_TRENDING"
                else:
                    regime["trend"] = "SIDEWAYS"
        except Exception as e:
            logger.warning(f"regime: trend computation failed: {e}")

    # ── Step 4: Yield curve (OpenBB first, TLT fallback) ─────────────────────
    yield_spread = None
    openbb_yield_ok = False
    try:
        from openbb import obb  # type: ignore
        yc = obb.fixedincome.yield_curve(provider="federal_reserve")
        yc_df = yc.to_dataframe()
        # Columns vary — look for 3m and 10y rows
        # federal_reserve yield curve typically has 'maturity' column with values like '3m', '10y'
        row_3m  = yc_df[yc_df.index.astype(str).str.contains("0.25|3m|3mo", case=False, na=False)]
        row_10y = yc_df[yc_df.index.astype(str).str.contains("10|10y|10yr", case=False, na=False)]
        if row_3m.empty or row_10y.empty:
            # Try maturity column if available
            if "maturity" in yc_df.columns:
                row_3m  = yc_df[yc_df["maturity"].astype(str).str.contains("0.25|3m", case=False, na=False)]
                row_10y = yc_df[yc_df["maturity"].astype(str).str.contains("^10$|10y|10yr", case=False, na=False)]
        if not row_3m.empty and not row_10y.empty:
            # Rate column is usually 'rate' or the first numeric column
            rate_col = "rate" if "rate" in yc_df.columns else yc_df.select_dtypes("number").columns[0]
            y3m  = float(row_3m[rate_col].iloc[-1])
            y10y = float(row_10y[rate_col].iloc[-1])
            yield_spread = round(y10y - y3m, 3)
            regime["yield_spread_pct"] = yield_spread
            regime["data_sources"].append("openbb_federal_reserve")
            openbb_yield_ok = True
            if yield_spread > 1.0:
                regime["yield_curve"] = "NORMAL"
            elif yield_spread >= -0.5:
                regime["yield_curve"] = "FLAT"
            else:
                regime["yield_curve"] = "INVERTED"
            logger.info(f"regime: yield curve via OpenBB — spread {yield_spread}% → {regime['yield_curve']}")
    except Exception as e:
        logger.warning(f"regime: OpenBB yield curve unavailable ({type(e).__name__}: {e}) — using TLT/SPY fallback")

    if not openbb_yield_ok and len(tlt_prices) >= 5 and len(spy_prices) >= 5:
        try:
            tlt_5d = (tlt_prices[-1] - tlt_prices[-6]) / tlt_prices[-6] * 100 if tlt_prices[-6] else 0
            spy_5d = (spy_prices[-1] - spy_prices[-6]) / spy_prices[-6] * 100 if spy_prices[-6] else 0
            if tlt_5d > 1.5 and spy_5d < -1.0:
                regime["yield_curve"] = "FLIGHT_TO_SAFETY"
            elif tlt_5d < -1.5:
                regime["yield_curve"] = "RISING_RATES"
            else:
                regime["yield_curve"] = "STABLE"
            regime["data_sources"].append("fallback_tlt_spy")
            logger.info(f"regime: yield curve via TLT/SPY fallback — {regime['yield_curve']}")
        except Exception as e:
            logger.warning(f"regime: TLT/SPY fallback also failed: {e}")

    # ── Step 5: Fear level (OpenBB VIX first, vol fallback) ──────────────────
    vix_value = None
    openbb_vix_ok = False
    try:
        from openbb import obb  # type: ignore
        vix = obb.equity.price.historical("^VIX", provider="yfinance")
        vix_df = vix.to_dataframe()
        close_col = "close" if "close" in vix_df.columns else vix_df.select_dtypes("number").columns[0]
        vix_value = round(float(vix_df[close_col].dropna().iloc[-1]), 2)
        regime["vix"] = vix_value
        regime["data_sources"].append("openbb_vix")
        openbb_vix_ok = True
        if vix_value < 15:
            regime["fear"] = "COMPLACENT"
        elif vix_value < 25:
            regime["fear"] = "NEUTRAL"
        elif vix_value < 35:
            regime["fear"] = "ELEVATED_FEAR"
        else:
            regime["fear"] = "EXTREME_FEAR"
        logger.info(f"regime: VIX={vix_value} via OpenBB → {regime['fear']}")
    except Exception as e:
        logger.warning(f"regime: OpenBB VIX unavailable ({type(e).__name__}: {e}) — using vol fallback")

    if not openbb_vix_ok and annualised_vol is not None:
        if annualised_vol < 15:
            regime["fear"] = "COMPLACENT"
        elif annualised_vol < 30:
            regime["fear"] = "NEUTRAL"
        elif annualised_vol < 50:
            regime["fear"] = "ELEVATED_FEAR"
        else:
            regime["fear"] = "EXTREME_FEAR"
        if "price_history" not in regime["data_sources"]:
            regime["data_sources"].append("price_history")

    # ── Step 6: VIX term structure (VIX3M vs VIX — OpenBB only) ─────────────
    try:
        from openbb import obb  # type: ignore
        vix3m = obb.equity.price.historical("^VIX3M", provider="yfinance")
        vix3m_df = vix3m.to_dataframe()
        close_col = "close" if "close" in vix3m_df.columns else vix3m_df.select_dtypes("number").columns[0]
        vix3m_val = round(float(vix3m_df[close_col].dropna().iloc[-1]), 2)
        if vix_value is not None:
            spread_vix = round(vix3m_val - vix_value, 2)
            if spread_vix > 1.0:
                term_structure = "contango"      # VIX3M > VIX — market calm, normal
            elif spread_vix < -1.0:
                term_structure = "backwardation" # VIX3M < VIX — near-term fear spike
            else:
                term_structure = "flat"
            regime["vix_term_structure"] = term_structure
            regime["vix3m"] = vix3m_val
            regime["vix_spread"] = spread_vix
            regime["data_sources"].append("openbb_vix3m")
            logger.info(f"regime: VIX3M={vix3m_val}, spread={spread_vix} → {term_structure}")
    except Exception as e:
        logger.warning(f"regime: VIX term structure unavailable ({type(e).__name__}: {e})")

    # ── Step 7: Institutional flow (IEF/HYG — credit risk appetite) ──────────
    try:
        from openbb import obb  # type: ignore
        ief_data = obb.equity.price.historical("IEF", provider="yfinance")
        hyg_data = obb.equity.price.historical("HYG", provider="yfinance")
        ief_df = ief_data.to_dataframe()
        hyg_df = hyg_data.to_dataframe()
        cc = lambda df: "close" if "close" in df.columns else df.select_dtypes("number").columns[0]
        ief_prices = ief_df[cc(ief_df)].dropna().tolist()
        hyg_prices = hyg_df[cc(hyg_df)].dropna().tolist()
        if len(ief_prices) >= 5 and len(hyg_prices) >= 5:
            # IEF (investment-grade) rising + HYG (high-yield) rising = risk-on
            ief_5d = (ief_prices[-1] - ief_prices[-5]) / ief_prices[-5] * 100
            hyg_5d = (hyg_prices[-1] - hyg_prices[-5]) / hyg_prices[-5] * 100
            if hyg_5d > 0.5 and ief_5d < 0:
                flow_signal = "risk_on"    # HY credit rallying, rates rising
            elif ief_5d > 0.5 and hyg_5d < 0:
                flow_signal = "risk_off"   # Flight to quality bonds, HY selling off
            else:
                flow_signal = "neutral"
            regime["flow_signal"] = flow_signal
            regime["ief_5d_pct"] = round(ief_5d, 2)
            regime["hyg_5d_pct"] = round(hyg_5d, 2)
            regime["data_sources"].append("openbb_ief_hyg")
            logger.info(f"regime: IEF 5d={ief_5d:.2f}%, HYG 5d={hyg_5d:.2f}% → {flow_signal}")
    except Exception as e:
        logger.warning(f"regime: IEF/HYG flow signal unavailable ({type(e).__name__}: {e})")

    # ── Summarise insufficient_data flag ─────────────────────────────────────
    regime["insufficient_data"] = any(
        regime[k] == "INSUFFICIENT_DATA"
        for k in ("volatility", "trend", "yield_curve", "fear")
    )

    # ── Write to backend/live/regime.json ────────────────────────────────────
    try:
        _LIVE_DIR.mkdir(parents=True, exist_ok=True)
        regime_path = _LIVE_DIR / "regime.json"
        tmp_path = _LIVE_DIR / "regime.json.tmp"
        tmp_path.write_text(json.dumps(regime, indent=2), encoding="utf-8")
        tmp_path.replace(regime_path)
        logger.info(
            f"regime: wrote {regime_path} — "
            f"vol={regime['volatility']} trend={regime['trend']} "
            f"yc={regime['yield_curve']} fear={regime['fear']}"
        )
    except Exception as e:
        logger.warning(f"regime: failed to write regime.json: {e}")

    return regime


def fetch_economic_calendar() -> list:
    """
    CHANGE 5: Fetch economic calendar events for the next 48 hours via OpenBB.
    Writes to backend/live/economic_calendar.json.
    Returns the events list (may be empty on failure). Never raises.
    """
    events: list = []
    try:
        from openbb import obb  # type: ignore
        cal = obb.economy.calendar(provider="fmp")
        cal_df = cal.to_dataframe()
        if cal_df.empty:
            return events
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=48)
        # Normalise column names
        cal_df.columns = [c.lower() for c in cal_df.columns]
        date_col = next((c for c in ("date", "event_date", "datetime") if c in cal_df.columns), None)
        if date_col is None:
            logger.warning("economic_calendar: no date column found in OpenBB response")
            return events

        import pandas as pd
        cal_df[date_col] = pd.to_datetime(cal_df[date_col], utc=True, errors="coerce")
        upcoming = cal_df[
            (cal_df[date_col] >= pd.Timestamp(now)) &
            (cal_df[date_col] <= pd.Timestamp(cutoff))
        ]
        for _, row in upcoming.iterrows():
            event = {
                "event":    str(row.get("event", row.get("name", ""))),
                "date":     row[date_col].isoformat() if not pd.isnull(row[date_col]) else None,
                "country":  str(row.get("country", "")),
                "impact":   str(row.get("impact", row.get("importance", ""))),
                "forecast": str(row.get("consensus", row.get("forecast", ""))),
                "previous": str(row.get("previous", "")),
            }
            events.append(event)
        logger.info(f"economic_calendar: {len(events)} events in next 48h")
    except Exception as e:
        logger.warning(f"economic_calendar: OpenBB fetch failed ({type(e).__name__}: {e})")

    try:
        cal_path = _LIVE_DIR / "economic_calendar.json"
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": 48,
            "events": events,
        }
        tmp = _LIVE_DIR / "economic_calendar.json.tmp"
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(cal_path)
    except Exception as e:
        logger.warning(f"economic_calendar: write failed: {e}")

    return events


def fetch_earnings_calendar() -> list:
    """
    CHANGE 5: Fetch upcoming earnings calendar for the next 24 hours via OpenBB.
    Writes top 5 by market cap (or first 5) to backend/live/earnings_calendar.json.
    Returns the events list (may be empty on failure). Never raises.
    """
    events: list = []
    try:
        from openbb import obb  # type: ignore
        earnings = obb.equity.calendar.earnings(provider="fmp")
        earn_df = earnings.to_dataframe()
        if earn_df.empty:
            return events
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=24)
        earn_df.columns = [c.lower() for c in earn_df.columns]
        date_col = next((c for c in ("report_date", "date", "datetime") if c in earn_df.columns), None)
        if date_col is None:
            logger.warning("earnings_calendar: no date column in OpenBB response")
            return events

        import pandas as pd
        earn_df[date_col] = pd.to_datetime(earn_df[date_col], utc=True, errors="coerce")
        upcoming = earn_df[
            (earn_df[date_col] >= pd.Timestamp(now)) &
            (earn_df[date_col] <= pd.Timestamp(cutoff))
        ].head(5)
        for _, row in upcoming.iterrows():
            event = {
                "symbol":   str(row.get("symbol", row.get("ticker", ""))),
                "name":     str(row.get("name", row.get("company_name", ""))),
                "date":     row[date_col].isoformat() if not pd.isnull(row[date_col]) else None,
                "eps_est":  str(row.get("eps_estimated", row.get("eps_estimate", ""))),
                "rev_est":  str(row.get("revenue_estimated", "")),
                "timing":   str(row.get("timing", row.get("when", ""))),
            }
            events.append(event)
        logger.info(f"earnings_calendar: {len(events)} earnings in next 24h")
    except Exception as e:
        logger.warning(f"earnings_calendar: OpenBB fetch failed ({type(e).__name__}: {e})")

    try:
        earn_path = _LIVE_DIR / "earnings_calendar.json"
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": 24,
            "events": events,
        }
        tmp = _LIVE_DIR / "earnings_calendar.json.tmp"
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(earn_path)
    except Exception as e:
        logger.warning(f"earnings_calendar: write failed: {e}")

    return events


def fetch_prices(prices_dir: Path = _PRICES_DIR) -> Path:
    """
    Fetch latest daily closing prices for all asset-class proxies and write
    them to prices_dir/YYYY-MM-DD.json.

    Performs a network pre-check first — skips the cycle if finance.yahoo.com
    is unreachable to avoid writing partial/empty data.

    Each ticker is retried up to _MAX_RETRIES times with _RETRY_DELAY seconds
    between attempts. Per-ticker failures are logged at WARNING level.

    Returns the path of the written file.
    Raises ImportError if yfinance is not installed.
    Raises RuntimeError if network pre-check fails.
    """
    try:
        import yfinance as yf
    except ImportError as exc:
        raise ImportError(
            "yfinance is required for price_fetcher. "
            "Install with: pip install yfinance"
        ) from exc

    # Network pre-check — abort entire cycle if Yahoo Finance unreachable
    if not _check_network():
        raise RuntimeError(
            "Network pre-check failed: finance.yahoo.com unreachable. "
            "Skipping price fetch to avoid writing partial data."
        )

    prices_dir.mkdir(parents=True, exist_ok=True)

    prices: dict = {}
    for asset, ticker in ASSET_TICKERS.items():
        prices[asset] = _fetch_ticker_with_retry(yf, ticker, asset)

    today_str = date.today().isoformat()
    out_path = prices_dir / f"{today_str}.json"
    payload = {
        "date": today_str,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "prices": prices,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    success_count = sum(1 for v in prices.values() if v is not None)
    logger.info(
        f"price_fetcher: wrote {out_path.name} — "
        f"{success_count}/{len(ASSET_TICKERS)} tickers fetched successfully"
    )

    # TIER 3: compute market regime after price snapshot is written, then embed
    # the regime in the snapshot file so it's part of the historical record.
    try:
        regime = compute_market_regime(prices_dir)
        payload["regime"] = regime
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"price_fetcher: regime computation failed (snapshot written without regime): {e}")

    # CHANGE 5: fetch economic + earnings calendars (OpenBB optional — never blocks)
    try:
        fetch_economic_calendar()
    except Exception as e:
        logger.warning(f"price_fetcher: economic calendar fetch failed: {e}")
    try:
        fetch_earnings_calendar()
    except Exception as e:
        logger.warning(f"price_fetcher: earnings calendar fetch failed: {e}")

    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        path = fetch_prices()
        logger.info("Prices written to %s", path)
        with open(path, encoding="utf-8") as f:
            print(f.read())
    except Exception as e:
        logger.error("price_fetcher failed: %s", e)
        sys.exit(1)
