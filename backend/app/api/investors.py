"""
Investor Agent API Routes
Provides endpoints for querying synthetic investor agents and their memory events.
"""

import os
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from neo4j import GraphDatabase
from ..utils.logger import get_logger

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


def _reaction_score(reaction: str) -> float:
    return _REACTION_SCORES.get((reaction or '').lower(), 0.0)


def compute_sentiment_scores(rows: list) -> dict:
    """
    Compute weighted sentiment score per asset class.

    Each row must have: asset_class, reaction, confidence (0-10), capital (USD).
    Returns dict keyed by asset_class with {score: float, event_count: int}.
    Score is in [-1, +1], weighted by capital * confidence.
    """
    buckets: dict = {}
    for row in rows:
        asset = row.get('asset_class') or 'unknown'
        weight = (row.get('capital') or 0) * (row.get('confidence') or 0)
        score = _reaction_score(row.get('reaction', ''))
        if asset not in buckets:
            buckets[asset] = {'weighted_sum': 0.0, 'total_weight': 0.0, 'event_count': 0}
        buckets[asset]['weighted_sum'] += score * weight
        buckets[asset]['total_weight'] += weight
        buckets[asset]['event_count'] += 1

    result = {}
    for asset, b in buckets.items():
        if b['total_weight'] == 0:
            final_score = 0.0
        else:
            raw = b['weighted_sum'] / b['total_weight']
            final_score = max(-1.0, min(1.0, raw))
        result[asset] = {
            'score': round(final_score, 4),
            'event_count': b['event_count'],
        }
    return result

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
    Weights each MemoryEvent by agent capital_usd * confidence.
    Reactions map: buy→+1, hedge→+0.5, hold→0, sell→-1, panic→-1.
    Query params: graph_id (required)
    """
    graph_id = request.args.get('graph_id')
    if not graph_id:
        return jsonify({"success": False, "error": "graph_id required"}), 400

    _SENTIMENT_QUERY = """
        MATCH (a:Entity {graph_id: $gid, is_synthetic: true})-[:HAS_MEMORY]->(m:MemoryEvent)
        WHERE m.timestamp >= $since
        RETURN
            a.asset_class_bias  AS asset_class,
            m.reaction          AS reaction,
            m.confidence        AS confidence,
            a.capital_usd       AS capital
    """

    try:
        driver = _get_driver()
        now = datetime.now(timezone.utc)
        since_24h = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                        .isoformat().replace('+00:00', 'Z'))
        # Simpler: just subtract seconds
        from datetime import timedelta
        ts_24h = (now - timedelta(hours=24)).isoformat()
        ts_7d  = (now - timedelta(days=7)).isoformat()

        with driver.session() as session:
            rows_24h = session.run(_SENTIMENT_QUERY, gid=graph_id, since=ts_24h).data()
            rows_7d  = session.run(_SENTIMENT_QUERY, gid=graph_id, since=ts_7d).data()

        scores_24h = compute_sentiment_scores(rows_24h)
        scores_7d  = compute_sentiment_scores(rows_7d)

        all_assets = set(scores_24h) | set(scores_7d)
        by_asset_class = {}
        for asset in sorted(all_assets):
            by_asset_class[asset] = {
                '24h': scores_24h.get(asset, {'score': 0.0, 'event_count': 0}),
                '7d':  scores_7d.get(asset,  {'score': 0.0, 'event_count': 0}),
            }

        return jsonify({
            "success": True,
            "data": {
                "by_asset_class": by_asset_class,
                "generated_at": now.isoformat(),
            }
        })

    except Exception as e:
        logger.error(f"Sentiment error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500