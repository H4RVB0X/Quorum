"""
Investor Agent API Routes
Provides endpoints for querying synthetic investor agents and their memory events.
"""

import os
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from flask import Blueprint, request, jsonify, current_app
from neo4j import GraphDatabase
from ..utils.logger import get_logger

# Sidecar source files written by news_fetcher.py alongside each briefing
_BRIEFINGS_DIR = Path(__file__).parent.parent.parent.parent / "backend" / "briefings"
_BRIEFINGS_DIR_ALT = Path(__file__).parent.parent.parent / "briefings"

investors_bp = Blueprint('investors', __name__)
logger = get_logger('mirofish.api.investors')

_driver_cache = None

_REACTION_SCORES = {
    'buy':   1.0,
    'hedge': 0.5,
    'hold':  0.0,
    'sell': -1.0,
    'panic': -1.0,
}

# Leverage multipliers applied ONLY to the 24h sentiment window.
# Leverage is a short-horizon amplifier: a 10x-leveraged retail trader reacts
# far more urgently to the same news than an unlevered pension fund.
# Not applied to the 7d window because sustained leverage over multiple days
# would compound fictitious signal strength beyond what the trait represents.
_LEVERAGE_MULTIPLIERS = {
    'none':     1.0,
    '2x':       1.3,
    '5x':       1.6,
    '10x_plus': 2.0,
}


def _leverage_mult(leverage: str) -> float:
    return _LEVERAGE_MULTIPLIERS.get((leverage or 'none').lower(), 1.0)


def _reaction_score(reaction: str) -> float:
    return _REACTION_SCORES.get((reaction or '').lower(), 0.0)


def compute_sentiment_scores(rows: list, apply_leverage: bool = False,
                             equal_weighted: bool = False,
                             apply_decay: bool = False) -> dict:
    """
    Compute weighted sentiment score per asset class.

    Each row must have: asset_class, reaction, confidence (0-10), capital (USD).
    Optional row fields: leverage (str), timestamp (ISO str),
                         direction (int), conviction (float), position_size (float).

    apply_leverage (bool): Multiply 24h weights by leverage multiplier.
        True for 24h window only — leverage is a short-horizon amplifier.
        False for 7d window (intentional asymmetry — do not change).
    equal_weighted (bool): If True, ignore capital; weight = confidence only.
        Shows what the unweighted agent majority thinks, regardless of AUM.
    apply_decay (bool): Apply exponential time-decay exp(-0.05 × age_hours).
        λ=0.05 gives ~14h half-life — semantically appropriate for the 24h
        sentiment window (recent events dominate over stale ones).
        Set True for 24h calculations, False for 7d (decay on 7d would collapse
        all weight onto the most recent few hours).

    TIER 2B — Conviction model (24h, apply_leverage=True, has new fields):
        score  = direction (-1/0/+1)
        weight = capital × conviction × position_size × leverage × decay
    Legacy fallback (pre-TIER-2B events missing direction/conviction/position_size):
        score  = reaction_score (buy=+1, sell=-1, hold=0, hedge=+0.5, panic=-1)
        weight = capital × (confidence/10) × 0.3 × leverage × decay
        (0.3 as a neutral position_size proxy for legacy events)

    Returns dict keyed by asset_class with {score: float, event_count: int}.
    Score is in [-1, +1].
    """
    now = datetime.now(timezone.utc)
    buckets: dict = {}

    for row in rows:
        asset      = row.get('asset_class') or 'unknown'
        capital    = float(row.get('capital')    or 0)
        confidence = float(row.get('confidence') or 0)
        reaction   = row.get('reaction', '')

        # Time-decay: λ=0.05 → half-life ≈ 14h (appropriate for 24h window)
        if apply_decay:
            ts_raw = row.get('timestamp')
            if ts_raw:
                try:
                    ts = datetime.fromisoformat(str(ts_raw).replace('Z', '+00:00'))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age_hours = max(0.0, (now - ts).total_seconds() / 3600)
                except Exception:
                    age_hours = 0.0
            else:
                age_hours = 0.0
            decay = math.exp(-0.05 * age_hours)
        else:
            decay = 1.0

        # Detect TIER 2B fields
        direction   = row.get('direction')
        conviction  = row.get('conviction')
        position_sz = row.get('position_size')
        has_new_fields = (
            direction   is not None and
            conviction  is not None and
            position_sz is not None
        )

        if equal_weighted:
            # Equal-weighted: capital ignored; conviction replaces confidence when available
            if has_new_fields:
                try:
                    score  = float(direction)
                    conv_f = max(0.0, min(1.0, float(conviction)))
                except (TypeError, ValueError):
                    score  = _reaction_score(reaction)
                    conv_f = confidence / 10.0
                weight = conv_f * decay
            else:
                score  = _reaction_score(reaction)
                weight = confidence * decay

        elif apply_leverage:
            lev = _leverage_mult(row.get('leverage', 'none'))
            if has_new_fields:
                # TIER 2B conviction model
                try:
                    score    = float(direction)
                    conv_f   = max(0.0, min(1.0, float(conviction)))
                    pos_sz_f = max(0.0, min(1.0, float(position_sz)))
                except (TypeError, ValueError):
                    # Field present but unparseable — fall back to legacy
                    score    = _reaction_score(reaction)
                    conv_f   = confidence / 10.0
                    pos_sz_f = 0.3
                weight = capital * conv_f * pos_sz_f * lev * decay
            else:
                # Legacy fallback: pre-TIER-2B MemoryEvents
                # Use 0.3 as default position_size to keep legacy events in range
                score  = _reaction_score(reaction)
                weight = capital * (confidence / 10.0) * 0.3 * lev * decay

        else:
            # Standard capital-weighted (7d window; no leverage, no conviction model)
            score  = _reaction_score(reaction)
            weight = capital * confidence * decay

        if asset not in buckets:
            buckets[asset] = {'weighted_sum': 0.0, 'total_weight': 0.0, 'event_count': 0}
        buckets[asset]['weighted_sum'] += score * weight
        buckets[asset]['total_weight'] += weight
        buckets[asset]['event_count']  += 1

    result = {}
    for asset, b in buckets.items():
        if b['total_weight'] == 0:
            final_score = 0.0
        else:
            raw = b['weighted_sum'] / b['total_weight']
            final_score = max(-1.0, min(1.0, raw))
        result[asset] = {
            'score':       round(final_score, 4),
            'event_count': b['event_count'],
        }
    return result


def _briefings_dir() -> Path:
    """Return the briefings directory, trying both candidate paths."""
    if _BRIEFINGS_DIR.is_dir():
        return _BRIEFINGS_DIR
    return _BRIEFINGS_DIR_ALT


def _latest_sources_sidecar() -> list:
    """
    Return feed breakdown from the most recent *_sources.json sidecar file.
    Written by news_fetcher.py alongside each briefing.
    Returns [] if no sidecar exists.
    """
    d = _briefings_dir()
    if not d.is_dir():
        return []
    sidecars = sorted(d.glob("*_sources.json"), reverse=True)
    if not sidecars:
        return []
    try:
        return json.loads(sidecars[0].read_text(encoding='utf-8'))
    except Exception:
        return []

def _get_driver():
    global _driver_cache
    if _driver_cache is None:
        uri = os.environ.get('NEO4J_URI', 'bolt://neo4j:7687')
        user = os.environ.get('NEO4J_USER', 'neo4j')
        password = os.environ.get('NEO4J_PASSWORD', 'mirofish')
        logger.info(f"Investors API connecting to Neo4j at {uri}")
        _driver_cache = GraphDatabase.driver(uri, auth=(user, password))
    return _driver_cache


@investors_bp.route('/stats', methods=['GET'])
def get_investor_stats():
    """
    Aggregate stats for the investor agent pool.
    Query params: graph_id (required)
    """
    graph_id = request.args.get('graph_id')
    if not graph_id:
        return jsonify({"success": False, "error": "graph_id required"}), 400

    try:
        driver = _get_driver()
        with driver.session() as session:

            # Pool size
            total = session.run(
                "MATCH (n:Entity {graph_id: $gid, is_synthetic: true}) RETURN count(n) AS c",
                gid=graph_id
            ).single()['c']

            # Archetype distribution
            arch_rows = session.run(
                """
                MATCH (n:Entity {graph_id: $gid, is_synthetic: true})
                WHERE n.investor_archetype IS NOT NULL
                RETURN n.investor_archetype AS archetype, count(n) AS c
                ORDER BY c DESC
                """,
                gid=graph_id
            ).data()

            # Strategy distribution
            strat_rows = session.run(
                """
                MATCH (n:Entity {graph_id: $gid, is_synthetic: true})
                WHERE n.primary_strategy IS NOT NULL
                RETURN n.primary_strategy AS strategy, count(n) AS c
                ORDER BY c DESC
                """,
                gid=graph_id
            ).data()

            # Fear/greed distribution
            fg_rows = session.run(
                """
                MATCH (n:Entity {graph_id: $gid, is_synthetic: true})
                WHERE n.fear_greed_dominant IS NOT NULL
                RETURN n.fear_greed_dominant AS fg, count(n) AS c
                """,
                gid=graph_id
            ).data()

            # Asset class bias
            bias_rows = session.run(
                """
                MATCH (n:Entity {graph_id: $gid, is_synthetic: true})
                WHERE n.asset_class_bias IS NOT NULL
                RETURN n.asset_class_bias AS bias, count(n) AS c
                ORDER BY c DESC
                """,
                gid=graph_id
            ).data()

            # Numeric trait averages
            avgs = session.run(
                """
                MATCH (n:Entity {graph_id: $gid, is_synthetic: true})
                WHERE n.risk_tolerance IS NOT NULL
                RETURN
                    avg(n.risk_tolerance) AS avg_risk,
                    avg(n.herd_behaviour) AS avg_herd,
                    avg(n.news_sensitivity) AS avg_news,
                    avg(n.geopolitical_sensitivity) AS avg_geo,
                    avg(n.overconfidence_bias) AS avg_overconf,
                    avg(n.capital_usd) AS avg_capital,
                    avg(n.loss_aversion_multiplier) AS avg_loss_aversion,
                    min(n.capital_usd) AS min_capital,
                    max(n.capital_usd) AS max_capital
                """,
                gid=graph_id
            ).single()

            # Risk tolerance histogram (10 buckets)
            risk_hist = session.run(
                """
                MATCH (n:Entity {graph_id: $gid, is_synthetic: true})
                WHERE n.risk_tolerance IS NOT NULL
                RETURN
                    floor(n.risk_tolerance) AS bucket,
                    count(n) AS c
                ORDER BY bucket
                """,
                gid=graph_id
            ).data()

            # Leverage distribution
            lev_rows = session.run(
                """
                MATCH (n:Entity {graph_id: $gid, is_synthetic: true})
                WHERE n.leverage_typical IS NOT NULL
                RETURN n.leverage_typical AS leverage, count(n) AS c
                ORDER BY c DESC
                """,
                gid=graph_id
            ).data()

            # Recent memory events
            events = session.run(
                """
                MATCH (a:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
                RETURN
                    a.name AS agent_name,
                    a.investor_archetype AS archetype,
                    m.reaction AS reaction,
                    m.confidence AS confidence,
                    m.reasoning AS reasoning,
                    m.timestamp AS ts,
                    m.briefing_source AS source
                ORDER BY m.timestamp DESC
                LIMIT 20
                """,
                gid=graph_id
            ).data()

            # Reaction distribution across all memory events
            reaction_dist = session.run(
                """
                MATCH (n:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
                RETURN m.reaction AS reaction, count(m) AS c
                ORDER BY c DESC
                """,
                gid=graph_id
            ).data()

            # Total memory events
            total_events = session.run(
                """
                MATCH (n:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
                RETURN count(m) AS c
                """,
                gid=graph_id
            ).single()['c']

            # Top 10 most recently seen non-synthetic entities (from latest briefing NER run)
            # last_seen is set on every MERGE in incremental_update.py — most recent = latest briefing
            top_entity_rows = session.run(
                """
                MATCH (e:Entity {graph_id: $gid})
                WHERE (e.is_synthetic IS NULL OR e.is_synthetic = false)
                  AND e.name IS NOT NULL
                  AND e.type IS NOT NULL
                RETURN e.name AS name, e.type AS type,
                       coalesce(e.mention_count, 1) AS count
                ORDER BY e.last_seen DESC, count DESC
                LIMIT 10
                """,
                gid=graph_id
            ).data()

        avg_data = {}
        if avgs:
            avg_data = {
                'avg_risk': round(avgs['avg_risk'] or 0, 2),
                'avg_herd': round(avgs['avg_herd'] or 0, 2),
                'avg_news': round(avgs['avg_news'] or 0, 2),
                'avg_geo': round(avgs['avg_geo'] or 0, 2),
                'avg_overconf': round(avgs['avg_overconf'] or 0, 2),
                'avg_capital': round(avgs['avg_capital'] or 0, 2),
                'avg_loss_aversion': round(avgs['avg_loss_aversion'] or 0, 2),
                'min_capital': round(avgs['min_capital'] or 0, 2),
                'max_capital': round(avgs['max_capital'] or 0, 2),
            }

        # Feed breakdown from latest news_fetcher sidecar file
        feed_breakdown = _latest_sources_sidecar()

        return jsonify({
            "success": True,
            "data": {
                "pool_size": total,
                "target": 4141,
                "total_memory_events": total_events,
                "archetypes": [{"name": r['archetype'], "count": r['c']} for r in arch_rows],
                "strategies": [{"name": r['strategy'], "count": r['c']} for r in strat_rows],
                "fear_greed": [{"name": r['fg'], "count": r['c']} for r in fg_rows],
                "asset_bias": [{"name": r['bias'], "count": r['c']} for r in bias_rows],
                "leverage": [{"name": r['leverage'], "count": r['c']} for r in lev_rows],
                "risk_histogram": [{"bucket": int(r['bucket'] or 0), "count": r['c']} for r in risk_hist],
                "averages": avg_data,
                "recent_events": [
                    {
                        "agent": r['agent_name'],
                        "archetype": r['archetype'],
                        "reaction": r['reaction'],
                        "confidence": r['confidence'],
                        "reasoning": r['reasoning'],
                        "timestamp": r['ts'],
                        "source": r['source'],
                    }
                    for r in events
                ],
                "reaction_distribution": [{"reaction": r['reaction'], "count": r['c']} for r in reaction_dist],
                "top_entities": [
                    {"name": r['name'], "type": r['type'], "count": int(r['count'] or 1)}
                    for r in top_entity_rows
                ],
                "feed_breakdown": feed_breakdown,
            }
        })

    except Exception as e:
        logger.error(f"Investor stats error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@investors_bp.route('/agents', methods=['GET'])
def list_agents():
    """
    List investor agents with traits.
    Query params: graph_id (required), limit (default 50), offset (default 0), archetype (optional filter)
    """
    graph_id = request.args.get('graph_id')
    if not graph_id:
        return jsonify({"success": False, "error": "graph_id required"}), 400

    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    archetype = request.args.get('archetype')

    try:
        driver = _get_driver()
        with driver.session() as session:
            where_clause = "WHERE n.is_synthetic = true"
            if archetype:
                where_clause += " AND n.investor_archetype = $arch"

            rows = session.run(
                f"""
                MATCH (n:Entity {{graph_id: $gid}})
                {where_clause}
                RETURN
                    n.uuid AS uuid,
                    n.name AS name,
                    n.investor_archetype AS archetype,
                    n.primary_strategy AS strategy,
                    n.risk_tolerance AS risk,
                    n.capital_usd AS capital,
                    n.time_horizon_days AS horizon,
                    n.fear_greed_dominant AS fear_greed,
                    n.asset_class_bias AS asset_bias,
                    n.leverage_typical AS leverage,
                    n.herd_behaviour AS herd,
                    n.news_sensitivity AS news,
                    n.summary AS backstory
                ORDER BY n.name
                SKIP $offset LIMIT $limit
                """,
                gid=graph_id,
                arch=archetype,
                offset=offset,
                limit=limit
            ).data()

        agents = [
            {
                "uuid": r['uuid'],
                "name": r['name'],
                "archetype": r['archetype'],
                "strategy": r['strategy'],
                "risk": round(r['risk'] or 0, 1) if r['risk'] is not None else None,
                "capital": round(r['capital'] or 0, 2) if r['capital'] is not None else None,
                "horizon": r['horizon'],
                "fear_greed": r['fear_greed'],
                "asset_bias": r['asset_bias'],
                "leverage": r['leverage'],
                "herd": round(r['herd'] or 0, 1) if r['herd'] is not None else None,
                "news": round(r['news'] or 0, 1) if r['news'] is not None else None,
                "backstory": r['backstory'],
            }
            for r in rows
        ]

        return jsonify({"success": True, "data": {"agents": agents, "count": len(agents), "offset": offset}})

    except Exception as e:
        logger.error(f"List agents error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@investors_bp.route('/sentiment', methods=['GET'])
def get_sentiment():
    """
    Weighted sentiment score per asset class for the last 24 h and 7 days.

    24h uses capital × confidence × leverage_multiplier (leverage amplifies
    short-horizon signal weight). 7d uses capital × confidence only
    (intentional asymmetry — leverage is a short-horizon amplifier only).

    Also returns equal-weighted scores (weight = confidence only, capital
    ignored) for both windows so the frontend can toggle the view.

    Query params: graph_id (required)
    """
    graph_id = request.args.get('graph_id')
    if not graph_id:
        return jsonify({"success": False, "error": "graph_id required"}), 400

    # Fetch leverage_typical + TIER 2B conviction fields for the 24h weighted path
    _SENTIMENT_QUERY = """
        MATCH (a:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
        WHERE m.timestamp >= $since
        RETURN
            a.asset_class_bias   AS asset_class,
            m.reaction           AS reaction,
            m.confidence         AS confidence,
            a.capital_usd        AS capital,
            a.leverage_typical   AS leverage,
            m.timestamp          AS timestamp,
            m.direction          AS direction,
            m.conviction         AS conviction,
            m.position_size      AS position_size
    """

    try:
        driver = _get_driver()
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        ts_24h = (now - timedelta(hours=24)).isoformat()
        ts_7d  = (now - timedelta(days=7)).isoformat()

        with driver.session() as session:
            rows_24h = session.run(_SENTIMENT_QUERY, gid=graph_id, since=ts_24h).data()
            rows_7d  = session.run(_SENTIMENT_QUERY, gid=graph_id, since=ts_7d).data()

        # Capital-weighted (24h: conviction model + leverage + decay; 7d: standard)
        scores_24h = compute_sentiment_scores(rows_24h, apply_leverage=True,  apply_decay=True)
        scores_7d  = compute_sentiment_scores(rows_7d,  apply_leverage=False, apply_decay=False)

        # Equal-weighted (confidence/conviction only; decay applied to 24h for consistency)
        scores_24h_eq = compute_sentiment_scores(rows_24h, equal_weighted=True, apply_decay=True)
        scores_7d_eq  = compute_sentiment_scores(rows_7d,  equal_weighted=True, apply_decay=False)

        all_assets = set(scores_24h) | set(scores_7d) | set(scores_24h_eq) | set(scores_7d_eq)
        by_asset_class = {}
        by_asset_class_equal = {}
        for asset in sorted(all_assets):
            by_asset_class[asset] = {
                '24h': scores_24h.get(asset,    {'score': 0.0, 'event_count': 0}),
                '7d':  scores_7d.get(asset,     {'score': 0.0, 'event_count': 0}),
            }
            by_asset_class_equal[asset] = {
                '24h': scores_24h_eq.get(asset, {'score': 0.0, 'event_count': 0}),
                '7d':  scores_7d_eq.get(asset,  {'score': 0.0, 'event_count': 0}),
            }

        return jsonify({
            "success": True,
            "data": {
                "by_asset_class":       by_asset_class,
                "by_asset_class_equal": by_asset_class_equal,
                "generated_at": now.isoformat(),
            }
        })

    except Exception as e:
        logger.error(f"Sentiment error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500