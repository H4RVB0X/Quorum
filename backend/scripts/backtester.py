"""
backtester.py — Signal accuracy backtesting.

For each day in the last N days:
  - Load MemoryEvent nodes from Neo4j (one bulk query, grouped by date in Python)
  - Load price snapshots from backend/prices/
  - For each asset class, compare signal direction (bullish = net positive
    capital-weighted sentiment, bearish = net negative) against actual
    next-day price move
  - Compute per-asset, per-archetype, rolling-7d accuracy
  - Compute confidence calibration (accuracy by confidence tier 1-3, 4-6, 7-8, 9-10)

Exposed as a function so signals.py can call it from the Flask route:
  run_backtest(driver, graph_id, days=30) → dict

Requires at least 2 days of price snapshots in backend/prices/.
Currently have: Apr 7, 8, 9, 10, 11.
"""

import json
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from collections import defaultdict
from typing import Optional

_PRICES_DIR = Path(__file__).parent.parent / "prices"

_REACTION_SCORES = {
    'buy': 1.0,
    'hedge': 0.5,
    'hold': 0.0,
    'sell': -1.0,
    'panic': -1.0,
}

# Confidence tiers: (label, lower_bound_exclusive, upper_bound_inclusive)
# Confidence is 0–10; tier boundaries match the spec labels.
CONFIDENCE_TIERS = [
    ('1-3',  0.0,  3.0),
    ('4-6',  3.0,  6.0),
    ('7-8',  6.0,  8.0),
    ('9-10', 8.0, 10.0),
]

_BULK_QUERY = """
MATCH (a:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
WHERE m.timestamp >= $since
RETURN
    a.asset_class_bias   AS asset_class,
    a.investor_archetype AS archetype,
    a.capital_usd        AS capital,
    m.reaction           AS reaction,
    m.confidence         AS confidence,
    m.timestamp          AS timestamp
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_price_file(date_str: str, prices_dir: Path = _PRICES_DIR) -> dict:
    """Return {asset_class: price} dict, or {} if file not found/invalid."""
    p = prices_dir / f"{date_str}.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        return data.get('prices', {})
    except Exception:
        return {}


def _next_trading_day_prices(d: date, prices_dir: Path, max_lookahead: int = 3) -> dict:
    """
    Return the price file for the nearest next trading day (up to max_lookahead
    days ahead to skip weekends/holidays).
    """
    for offset in range(1, max_lookahead + 1):
        next_str = (d + timedelta(days=offset)).isoformat()
        prices = _read_price_file(next_str, prices_dir)
        if prices:
            return prices
    return {}


def _reaction_score(reaction: str) -> float:
    return _REACTION_SCORES.get((reaction or '').lower(), 0.0)


def _compute_daily_sentiment(events: list) -> dict:
    """
    Compute capital-weighted sentiment per asset class from a list of events.
    Returns {asset_class: {'score': float, 'count': int}}.
    """
    buckets: dict = defaultdict(lambda: {'wsum': 0.0, 'wtot': 0.0, 'count': 0})
    for e in events:
        asset = e.get('asset_class') or 'unknown'
        weight = (e.get('capital') or 0.0) * (e.get('confidence') or 0.0)
        score = _reaction_score(e.get('reaction', ''))
        buckets[asset]['wsum'] += score * weight
        buckets[asset]['wtot'] += weight
        buckets[asset]['count'] += 1

    result = {}
    for asset, b in buckets.items():
        if b['wtot'] == 0:
            s = 0.0
        else:
            s = max(-1.0, min(1.0, b['wsum'] / b['wtot']))
        result[asset] = {'score': s, 'count': b['count']}
    return result


def _signal(score: float) -> str:
    """Convert a sentiment score to a directional signal string."""
    if score > 0.1:
        return 'bullish'
    if score < -0.1:
        return 'bearish'
    return 'neutral'


def _conf_tier(confidence: float) -> Optional[str]:
    """Map a 0–10 confidence float to a tier label."""
    for label, lo, hi in CONFIDENCE_TIERS:
        if lo < confidence <= hi:
            return label
    # confidence == 0 → lowest tier
    if confidence == 0:
        return '1-3'
    return None


# ---------------------------------------------------------------------------
# Main backtest
# ---------------------------------------------------------------------------

def run_backtest(driver, graph_id: str, days: int = 30,
                 prices_dir: Path = _PRICES_DIR) -> dict:
    """
    Run a full signal backtest over the last `days` days.

    Returns:
      {
        "per_asset":     {asset: {accuracy, total, correct}},
        "per_archetype": {arch:  {accuracy, total, correct}},
        "rolling_7d":    {asset: {dates: [...], accuracy: [...]}},
        "confidence_calibration": {tier: {accuracy, count}},
        "days_queried":  int,
        "trading_days_found": int,
      }
    """
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()

    with driver.session() as session:
        rows = session.run(_BULK_QUERY, gid=graph_id, since=since).data()

    # Group events by ISO date string
    by_date: dict = defaultdict(list)
    for row in rows:
        ts = row.get('timestamp', '')
        try:
            d = datetime.fromisoformat(ts.replace('Z', '+00:00')).date()
            by_date[d.isoformat()].append(row)
        except Exception:
            continue

    all_dates = sorted(by_date.keys())

    # Accumulators
    per_asset:      dict = defaultdict(lambda: {'correct': 0, 'total': 0})
    per_archetype:  dict = defaultdict(lambda: {'correct': 0, 'total': 0})
    daily_results:  dict = defaultdict(list)   # asset → [(date_str, correct_bool)]
    conf_cal:       dict = {t[0]: {'correct': 0, 'count': 0} for t in CONFIDENCE_TIERS}

    for date_str in all_dates:
        events = by_date[date_str]

        try:
            d = date.fromisoformat(date_str)
        except Exception:
            continue

        current_prices = _read_price_file(date_str, prices_dir)
        next_prices = _next_trading_day_prices(d, prices_dir)

        if not current_prices or not next_prices:
            continue

        # ── Per-asset accuracy ──
        daily_sent = _compute_daily_sentiment(events)
        for asset, sent in daily_sent.items():
            sig = _signal(sent['score'])
            if sig == 'neutral':
                continue
            curr = current_prices.get(asset)
            nxt = next_prices.get(asset)
            if curr is None or nxt is None or curr == 0:
                continue
            price_up = nxt > curr
            correct = (sig == 'bullish' and price_up) or (sig == 'bearish' and not price_up)
            per_asset[asset]['total'] += 1
            if correct:
                per_asset[asset]['correct'] += 1
            daily_results[asset].append((date_str, correct))

        # ── Per-archetype accuracy ──
        by_arch: dict = defaultdict(list)
        for e in events:
            arch = e.get('archetype') or 'unknown'
            by_arch[arch].append(e)

        for arch, arch_events in by_arch.items():
            arch_sent = _compute_daily_sentiment(arch_events)
            for asset, sent in arch_sent.items():
                sig = _signal(sent['score'])
                if sig == 'neutral':
                    continue
                curr = current_prices.get(asset)
                nxt = next_prices.get(asset)
                if curr is None or nxt is None or curr == 0:
                    continue
                price_up = nxt > curr
                correct = (sig == 'bullish' and price_up) or (sig == 'bearish' and not price_up)
                per_archetype[arch]['total'] += 1
                if correct:
                    per_archetype[arch]['correct'] += 1

        # ── Confidence calibration (per individual event) ──
        for e in events:
            reaction = (e.get('reaction') or '').lower()
            if reaction in ('hold', ''):
                continue  # neutral — excluded from directional accuracy
            if reaction not in ('buy', 'hedge', 'sell', 'panic'):
                continue

            asset = e.get('asset_class') or 'unknown'
            curr = current_prices.get(asset)
            nxt = next_prices.get(asset)
            if curr is None or nxt is None or curr == 0:
                continue

            direction_up = reaction in ('buy', 'hedge')
            price_up = nxt > curr
            correct = (direction_up and price_up) or (not direction_up and not price_up)

            try:
                conf = float(e.get('confidence') or 5.0)
            except (TypeError, ValueError):
                conf = 5.0

            tier = _conf_tier(conf)
            if tier:
                conf_cal[tier]['count'] += 1
                if correct:
                    conf_cal[tier]['correct'] += 1

    # ── Build output ──
    per_asset_out = {
        asset: {
            'accuracy': round(d['correct'] / d['total'], 4) if d['total'] else 0.0,
            'total':    d['total'],
            'correct':  d['correct'],
        }
        for asset, d in per_asset.items()
    }

    per_arch_out = {
        arch: {
            'accuracy': round(d['correct'] / d['total'], 4) if d['total'] else 0.0,
            'total':    d['total'],
            'correct':  d['correct'],
        }
        for arch, d in per_archetype.items()
    }

    rolling_7d_out = {}
    for asset, results in daily_results.items():
        sorted_r = sorted(results, key=lambda x: x[0])
        dates = [r[0] for r in sorted_r]
        values = [1 if r[1] else 0 for r in sorted_r]
        rolling = []
        for i in range(len(values)):
            window = values[max(0, i - 6): i + 1]
            acc = sum(window) / len(window) if window else 0.0
            rolling.append(round(acc, 4))
        rolling_7d_out[asset] = {'dates': dates, 'accuracy': rolling}

    conf_cal_out = {
        tier: {
            'accuracy': round(d['correct'] / d['count'], 4) if d['count'] else 0.0,
            'count':    d['count'],
        }
        for tier, d in conf_cal.items()
    }

    return {
        'per_asset':              per_asset_out,
        'per_archetype':          per_arch_out,
        'rolling_7d':             rolling_7d_out,
        'confidence_calibration': conf_cal_out,
        'days_queried':           days,
        'trading_days_found':     len(all_dates),
    }
