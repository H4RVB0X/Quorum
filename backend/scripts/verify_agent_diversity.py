"""
verify_agent_diversity.py — Full diversity audit on the agent pool in Neo4j.

Checks:
  1. Uniqueness       — cosine similarity matrix; flag pairs > 0.85
  2. Distribution     — per-trait distribution; flag any value > 25% of pool
  3. Fat tails        — confirm extremes: risk>=9, risk<=1, capital>=10M, horizon<=3d
  4. Strategy coverage — all 10 strategies >= 1% of pool
  5. Archetype coverage — all 7 archetypes present
  6. Trait correlation — Pearson > 0.95 between any two numeric traits
  7. News reaction variance — 3 headlines × 100 agents via LLM; flag dominance > 60%

Report saved to: ./reports/diversity_audit_YYYYMMDD_HHMM.txt
Terminal summary: one-line PASS/FAIL for CI use.

Usage:
  python verify_agent_diversity.py [--graph-id <uuid>]
"""
import sys
import os
import argparse
import random
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np

from db_setup import setup as db_setup
from app.storage.embedding_service import EmbeddingService
from app.utils.llm_client import LLMClient
from app.utils.logger import get_logger

logger = get_logger('mirofish.verify_agent_diversity')

REPORTS_DIR = Path(__file__).parent / "reports"

# Thresholds
DUPLICATE_SIM_THRESHOLD = 0.85
DISTRIBUTION_DOMINANCE_THRESHOLD = 0.25
FAT_TAIL_MIN_PCT = 0.05
STRATEGY_MIN_PCT = 0.01
CORRELATION_THRESHOLD = 0.95
REACTION_DOMINANCE_THRESHOLD = 0.60

NEWS_SAMPLE_SIZE = 100

ALL_STRATEGIES = [
    'day_trading', 'swing', 'value', 'growth', 'momentum',
    'index', 'income', 'macro', 'contrarian', 'quant',
]
ALL_ARCHETYPES = [
    'retail_amateur', 'retail_experienced', 'prop_trader',
    'fund_manager', 'family_office', 'hedge_fund', 'pension_fund',
]
VALID_REACTIONS = {'buy', 'hold', 'sell', 'panic', 'hedge'}

HEADLINES = [
    "Fed raises rates by 50bps unexpectedly",
    "Iran ceasefire collapses, Strait of Hormuz closes again",
    "Nvidia beats earnings by 40%, raises guidance",
]

# Numeric traits used for correlation check
NUMERIC_TRAITS = [
    'risk_tolerance', 'capital_usd', 'time_horizon_days',
    'herd_behaviour', 'news_sensitivity', 'geopolitical_sensitivity',
    'overconfidence_bias', 'loss_aversion_multiplier', 'reaction_speed_minutes',
]

# Categorical traits for distribution dominance check
CATEGORICAL_TRAITS = [
    'fear_greed_dominant', 'primary_strategy', 'investor_archetype',
    'leverage_typical', 'formative_crash',
]

# Numeric traits reported as bucketed distributions
NUMERIC_DISTRIBUTION_TRAITS = [
    'risk_tolerance', 'capital_usd', 'time_horizon_days', 'herd_behaviour',
]


# ---------------------------------------------------------------------------
# Neo4j
# ---------------------------------------------------------------------------

def load_all_agents(driver, graph_id: str) -> list:
    """Load every agent with investor traits from Neo4j."""
    with driver.session() as session:
        result = session.run(
            """
            MATCH (n:Entity {graph_id: $gid})
            WHERE n.risk_tolerance IS NOT NULL
            RETURN n.uuid AS uuid,
                   n.name AS name,
                   n.risk_tolerance AS risk_tolerance,
                   n.capital_usd AS capital_usd,
                   n.time_horizon_days AS time_horizon_days,
                   n.fear_greed_dominant AS fear_greed_dominant,
                   n.loss_aversion_multiplier AS loss_aversion_multiplier,
                   n.herd_behaviour AS herd_behaviour,
                   n.reaction_speed_minutes AS reaction_speed_minutes,
                   n.primary_strategy AS primary_strategy,
                   n.asset_class_bias AS asset_class_bias,
                   n.news_sensitivity AS news_sensitivity,
                   n.geopolitical_sensitivity AS geopolitical_sensitivity,
                   n.investor_archetype AS investor_archetype,
                   n.formative_crash AS formative_crash,
                   n.overconfidence_bias AS overconfidence_bias,
                   n.leverage_typical AS leverage_typical
            """,
            gid=graph_id,
        )
        return [dict(r) for r in result]


# ---------------------------------------------------------------------------
# Trait string (mirrors agent_evolver.py serialise_traits)
# ---------------------------------------------------------------------------

def serialise_traits(traits: dict) -> str:
    return (
        f"{traits.get('investor_archetype', '')}, "
        f"{traits.get('primary_strategy', '')}, "
        f"risk={traits.get('risk_tolerance', 0) or 0:.1f}, "
        f"capital={traits.get('capital_usd', 0) or 0:.0f}, "
        f"horizon={traits.get('time_horizon_days', 0) or 0}, "
        f"herd={traits.get('herd_behaviour', 0) or 0:.1f}, "
        f"news={traits.get('news_sensitivity', 0) or 0:.1f}, "
        f"geo={traits.get('geopolitical_sensitivity', 0) or 0:.1f}, "
        f"bias={traits.get('asset_class_bias', '')}, "
        f"leverage={traits.get('leverage_typical', '')}, "
        f"fg={traits.get('fear_greed_dominant', '')}"
    )


# ---------------------------------------------------------------------------
# Check 1: Uniqueness
# ---------------------------------------------------------------------------

def check_uniqueness(agents: list, embedding_svc: EmbeddingService) -> dict:
    """
    Embed every agent's trait string, build cosine similarity matrix,
    flag all pairs with similarity > DUPLICATE_SIM_THRESHOLD.
    """
    logger.info(f"Check 1: embedding {len(agents)} trait strings...")
    trait_strings = [serialise_traits(a) for a in agents]

    try:
        embeddings = embedding_svc.embed_batch(trait_strings)
    except Exception as e:
        return {'error': str(e), 'passed': False, 'flags': [f"Embedding failed: {e}"]}

    mat = np.array(embeddings, dtype='float32')
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    mat /= norms  # L2-normalise → dot product = cosine similarity

    # Full similarity matrix (n × n)
    sim = mat @ mat.T

    # Upper-triangle pairs above threshold (exclude self-similarity on diagonal)
    rows, cols = np.where(sim > DUPLICATE_SIM_THRESHOLD)
    dup_pairs = [(int(r), int(c)) for r, c in zip(rows, cols) if r < c]
    dup_pairs.sort(key=lambda p: -sim[p[0], p[1]])

    worst = [
        {
            'agent_a': agents[r].get('name') or agents[r]['uuid'][:8],
            'agent_b': agents[c].get('name') or agents[c]['uuid'][:8],
            'similarity': float(sim[r, c]),
        }
        for r, c in dup_pairs[:10]
    ]

    flags = []
    if dup_pairs:
        flags.append(
            f"{len(dup_pairs)} duplicate pair(s) with cosine similarity > {DUPLICATE_SIM_THRESHOLD}"
        )

    return {
        'total_agents': len(agents),
        'duplicate_pairs': len(dup_pairs),
        'worst_offenders': worst,
        'flags': flags,
        'passed': len(dup_pairs) == 0,
    }


# ---------------------------------------------------------------------------
# Check 2: Distribution
# ---------------------------------------------------------------------------

def _bucket_numeric(values: list, n_buckets: int = 10) -> dict:
    """Return {label: count} for n_buckets equal-width bins."""
    arr = np.array(values, dtype='float64')
    lo, hi = arr.min(), arr.max()
    if lo == hi:
        return {f"{lo:.2g}": len(values)}
    edges = np.linspace(lo, hi, n_buckets + 1)
    dist = {}
    for i in range(n_buckets):
        edge_lo, edge_hi = edges[i], edges[i + 1]
        if i < n_buckets - 1:
            count = int(np.sum((arr >= edge_lo) & (arr < edge_hi)))
        else:
            count = int(np.sum((arr >= edge_lo) & (arr <= edge_hi)))
        dist[f"{edge_lo:.2g}–{edge_hi:.2g}"] = count
    return dist


def check_distributions(agents: list) -> dict:
    """
    For numeric traits: 10-bucket distribution; flag if any bucket > 25%.
    For categorical traits: value counts; flag if any value > 25%.
    """
    n = len(agents)
    results = {}
    flags = []

    for trait in NUMERIC_DISTRIBUTION_TRAITS:
        values = [a[trait] for a in agents if a.get(trait) is not None]
        if not values:
            flags.append(f"{trait}: no data")
            results[trait] = {}
            continue
        dist = _bucket_numeric(values)
        results[trait] = dist
        max_cnt = max(dist.values())
        pct = max_cnt / len(values)
        if pct > DISTRIBUTION_DOMINANCE_THRESHOLD:
            max_bucket = max(dist, key=dist.get)
            flags.append(
                f"{trait}: bucket '{max_bucket}' = {pct:.1%} of agents ({max_cnt}/{len(values)})"
            )

    for trait in CATEGORICAL_TRAITS:
        values = [str(a[trait]) for a in agents if a.get(trait) is not None]
        if not values:
            flags.append(f"{trait}: no data")
            results[trait] = {}
            continue
        counter = Counter(values)
        results[trait] = dict(counter.most_common())
        for val, cnt in counter.items():
            pct = cnt / len(values)
            if pct > DISTRIBUTION_DOMINANCE_THRESHOLD:
                flags.append(
                    f"{trait}='{val}': {pct:.1%} of agents ({cnt}/{len(values)})"
                )

    return {
        'distributions': results,
        'flags': flags,
        'passed': len(flags) == 0,
    }


# ---------------------------------------------------------------------------
# Check 3: Fat tails
# ---------------------------------------------------------------------------

def check_fat_tails(agents: list) -> dict:
    """Confirm that extreme-value cohorts each represent >= 5% of the pool."""
    n = len(agents)
    specs = [
        ('risk_tolerance >= 9',    lambda a: a.get('risk_tolerance') is not None and a['risk_tolerance'] >= 9),
        ('risk_tolerance <= 1',    lambda a: a.get('risk_tolerance') is not None and a['risk_tolerance'] <= 1),
        ('capital_usd >= 10M',     lambda a: a.get('capital_usd') is not None and a['capital_usd'] >= 10_000_000),
        ('time_horizon_days <= 3', lambda a: a.get('time_horizon_days') is not None and a['time_horizon_days'] <= 3),
    ]

    checks = []
    flags = []
    for label, fn in specs:
        count = sum(1 for a in agents if fn(a))
        pct = count / n if n else 0.0
        passed = pct >= FAT_TAIL_MIN_PCT
        checks.append({'check': label, 'count': count, 'pct': pct, 'passed': passed})
        if not passed:
            flags.append(
                f"{label}: {pct:.1%} ({count}/{n}) — need >= {FAT_TAIL_MIN_PCT:.0%}"
            )

    return {
        'checks': checks,
        'flags': flags,
        'passed': len(flags) == 0,
    }


# ---------------------------------------------------------------------------
# Check 4: Strategy coverage
# ---------------------------------------------------------------------------

def check_strategy_coverage(agents: list) -> dict:
    """All 10 primary strategies must each represent >= 1% of the pool."""
    n = len(agents)
    counter = Counter(a.get('primary_strategy') for a in agents if a.get('primary_strategy'))
    flags = []
    counts = {}

    for strategy in ALL_STRATEGIES:
        cnt = counter.get(strategy, 0)
        pct = cnt / n if n else 0.0
        counts[strategy] = {'count': cnt, 'pct': pct}
        if pct < STRATEGY_MIN_PCT:
            flags.append(
                f"strategy '{strategy}': {pct:.2%} ({cnt}/{n}) — need >= 1%"
            )

    return {
        'strategy_counts': counts,
        'flags': flags,
        'passed': len(flags) == 0,
    }


# ---------------------------------------------------------------------------
# Check 5: Archetype coverage
# ---------------------------------------------------------------------------

def check_archetype_coverage(agents: list) -> dict:
    """All 7 investor archetypes must be present in the pool."""
    counter = Counter(a.get('investor_archetype') for a in agents if a.get('investor_archetype'))
    flags = []
    counts = {}

    for archetype in ALL_ARCHETYPES:
        cnt = counter.get(archetype, 0)
        counts[archetype] = cnt
        if cnt == 0:
            flags.append(f"archetype '{archetype}' absent from pool")

    return {
        'archetype_counts': counts,
        'flags': flags,
        'passed': len(flags) == 0,
    }


# ---------------------------------------------------------------------------
# Check 6: Trait correlation
# ---------------------------------------------------------------------------

def check_trait_correlation(agents: list) -> dict:
    """
    Compute pairwise Pearson correlation for all numeric traits.
    Flag any pair with |r| > CORRELATION_THRESHOLD (too predictable).
    """
    rows = []
    for a in agents:
        row = [a.get(t) for t in NUMERIC_TRAITS]
        if all(v is not None for v in row):
            rows.append([float(v) for v in row])

    if len(rows) < 10:
        return {
            'error': f"Only {len(rows)} agents with complete numeric traits — skipping correlation check",
            'flags': [],
            'passed': True,
        }

    mat = np.array(rows, dtype='float64')  # (n_agents × n_traits)
    corr = np.corrcoef(mat.T)              # (n_traits × n_traits)

    pairs = []
    flags = []
    for i in range(len(NUMERIC_TRAITS)):
        for j in range(i + 1, len(NUMERIC_TRAITS)):
            r = float(corr[i, j])
            pairs.append({
                'trait_a': NUMERIC_TRAITS[i],
                'trait_b': NUMERIC_TRAITS[j],
                'r': r,
            })
            if abs(r) > CORRELATION_THRESHOLD:
                flags.append(
                    f"{NUMERIC_TRAITS[i]} ↔ {NUMERIC_TRAITS[j]}: r={r:.3f}"
                )

    pairs.sort(key=lambda p: -abs(p['r']))

    return {
        'agents_used': len(rows),
        'top_correlations': pairs[:10],
        'flags': flags,
        'passed': len(flags) == 0,
    }


# ---------------------------------------------------------------------------
# Check 7: News reaction variance
# ---------------------------------------------------------------------------

def _reaction_prompt(agent: dict, headline: str) -> list:
    profile = (
        f"Archetype: {agent.get('investor_archetype', 'unknown')}, "
        f"Strategy: {agent.get('primary_strategy', 'unknown')}, "
        f"Risk tolerance: {agent.get('risk_tolerance', 5) or 5:.1f}/10, "
        f"Capital: ${agent.get('capital_usd', 10000) or 10000:,.0f}, "
        f"Asset bias: {agent.get('asset_class_bias', 'mixed')}, "
        f"Fear/Greed: {agent.get('fear_greed_dominant', 'neutral')}, "
        f"News sensitivity: {agent.get('news_sensitivity', 5) or 5:.1f}/10, "
        f"Geo sensitivity: {agent.get('geopolitical_sensitivity', 5) or 5:.1f}/10"
    )
    return [
        {
            "role": "system",
            "content": (
                "You are simulating an investor's immediate reaction to a single financial headline. "
                "Return valid JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Investor: {profile}\n\n"
                f"Headline: \"{headline}\"\n\n"
                'Return JSON: {"reaction": "buy" | "hold" | "sell" | "panic" | "hedge"}'
            ),
        },
    ]


def check_news_reaction_variance(agents: list, llm: LLMClient) -> dict:
    """
    Feed each of 3 headlines to 100 random agents.
    Flag any headline where one reaction exceeds 60% of responses.
    """
    sample = random.sample(agents, min(NEWS_SAMPLE_SIZE, len(agents)))
    headline_results = {}
    flags = []

    for headline in HEADLINES:
        logger.info(f"Check 7: reactions for headline '{headline[:55]}'...")
        reactions = []
        errors = 0

        for agent in sample:
            messages = _reaction_prompt(agent, headline)
            try:
                data = llm.chat_json(messages, temperature=0.7, max_tokens=64)
                reaction = str(data.get('reaction', 'hold')).lower().strip()
                if reaction not in VALID_REACTIONS:
                    reaction = 'hold'
                reactions.append(reaction)
            except Exception as e:
                logger.warning(f"LLM failed for {agent.get('uuid', '?')[:8]}: {e}")
                errors += 1

        if not reactions:
            flags.append(f"'{headline[:50]}': all {len(sample)} LLM calls failed")
            headline_results[headline] = {'error': 'all LLM calls failed', 'errors': errors}
            continue

        counter = Counter(reactions)
        dominant_reaction, dominant_count = counter.most_common(1)[0]
        dominant_pct = dominant_count / len(reactions)

        headline_results[headline] = {
            'distribution': {k: {'count': v, 'pct': v / len(reactions)} for k, v in counter.most_common()},
            'dominant_reaction': dominant_reaction,
            'dominant_pct': dominant_pct,
            'total_responses': len(reactions),
            'errors': errors,
        }

        if dominant_pct > REACTION_DOMINANCE_THRESHOLD:
            flags.append(
                f"'{headline[:50]}': "
                f"'{dominant_reaction}' = {dominant_pct:.1%} of agents "
                f"(>{REACTION_DOMINANCE_THRESHOLD:.0%} threshold)"
            )

    return {
        'results': headline_results,
        'flags': flags,
        'passed': len(flags) == 0,
    }


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _pf(passed: Optional[bool]) -> str:
    return "PASS" if passed else "FAIL"


def format_report(
    total_agents: int,
    graph_id: str,
    timestamp: str,
    check1: dict,
    check2: dict,
    check3: dict,
    check4: dict,
    check5: dict,
    check6: dict,
    check7: dict,
) -> str:
    W = 72
    lines = [
        "=" * W,
        "MIROFISH AGENT DIVERSITY AUDIT",
        f"Generated : {timestamp}",
        f"Graph ID  : {graph_id}",
        f"Agents    : {total_agents}",
        "=" * W,
        "",
    ]

    named_checks = [
        ("1. Uniqueness",            check1),
        ("2. Distribution",          check2),
        ("3. Fat Tails",             check3),
        ("4. Strategy Coverage",     check4),
        ("5. Archetype Coverage",    check5),
        ("6. Trait Correlation",     check6),
        ("7. News Reaction Variance",check7),
    ]

    all_flags = []

    for name, result in named_checks:
        passed = result.get('passed', False)
        lines.append(f"[{_pf(passed)}] {name}")

        if 'error' in result:
            lines.append(f"       NOTE: {result['error']}")

        for flag in result.get('flags', []):
            lines.append(f"       [WARNING] {flag}")
            all_flags.append(f"{name}: {flag}")

        # --- Per-check detail ---
        if name.startswith("1."):
            lines.append(
                f"       Pairs with sim > {DUPLICATE_SIM_THRESHOLD}: {result.get('duplicate_pairs', 0)}"
            )
            for w in result.get('worst_offenders', [])[:5]:
                lines.append(
                    f"         {w['agent_a']} ↔ {w['agent_b']}  sim={w['similarity']:.4f}"
                )

        elif name.startswith("2."):
            for trait, dist in result.get('distributions', {}).items():
                if not dist:
                    continue
                if isinstance(next(iter(dist.values())), int):
                    items = list(dist.items())
                else:
                    items = list(dist.items())
                top = items[:6]
                dist_str = "  ".join(f"{k}:{v}" for k, v in top)
                lines.append(f"         {trait}: {dist_str}")

        elif name.startswith("3."):
            for c in result.get('checks', []):
                icon = "✓" if c['passed'] else "✗"
                lines.append(
                    f"         {icon} {c['check']}: {c['pct']:.1%} ({c['count']} agents)"
                )

        elif name.startswith("4."):
            for strat, data in result.get('strategy_counts', {}).items():
                icon = "✓" if data['pct'] >= STRATEGY_MIN_PCT else "✗"
                lines.append(
                    f"         {icon} {strat}: {data['pct']:.1%} ({data['count']})"
                )

        elif name.startswith("5."):
            for arch, cnt in result.get('archetype_counts', {}).items():
                icon = "✓" if cnt > 0 else "✗"
                lines.append(f"         {icon} {arch}: {cnt}")

        elif name.startswith("6."):
            lines.append(f"       Agents used for correlation: {result.get('agents_used', 0)}")
            for pair in result.get('top_correlations', [])[:6]:
                marker = " ← FLAGGED" if abs(pair['r']) > CORRELATION_THRESHOLD else ""
                lines.append(
                    f"         {pair['trait_a']} ↔ {pair['trait_b']}: r={pair['r']:.3f}{marker}"
                )

        elif name.startswith("7."):
            for headline, data in result.get('results', {}).items():
                lines.append(f"       Headline: \"{headline}\"")
                if 'error' in data:
                    lines.append(f"         ERROR: {data['error']}")
                else:
                    for reaction, stats in data.get('distribution', {}).items():
                        marker = " ← FLAGGED" if stats['pct'] > REACTION_DOMINANCE_THRESHOLD else ""
                        lines.append(
                            f"         {reaction}: {stats['pct']:.1%} ({stats['count']}){marker}"
                        )
                    if data.get('errors', 0) > 0:
                        lines.append(f"         LLM errors: {data['errors']}")

        lines.append("")

    lines += [
        "=" * W,
        "FLAGGED ISSUES",
        "=" * W,
    ]
    if all_flags:
        for flag in all_flags:
            lines.append(f"  [WARNING] {flag}")
    else:
        lines.append("  None.")

    lines += [
        "",
        "=" * W,
        f"OVERALL VERDICT: {_pf(not all_flags)}",
        "=" * W,
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_audit(driver, graph_id: str) -> tuple:
    """
    Execute all 7 checks and write the report file.
    Returns (report_path, passed, total_agents, n_issues).
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info("Loading agents from Neo4j...")
    agents = load_all_agents(driver, graph_id)
    total = len(agents)
    logger.info(f"Loaded {total} agents")

    if total == 0:
        report = f"AUDIT FAILED — no agents found for graph_id={graph_id}\n"
        return report, False, 0, 1

    embedding_svc = EmbeddingService()
    llm = LLMClient()

    logger.info("Running Check 1: Uniqueness...")
    check1 = check_uniqueness(agents, embedding_svc)

    logger.info("Running Check 2: Distributions...")
    check2 = check_distributions(agents)

    logger.info("Running Check 3: Fat tails...")
    check3 = check_fat_tails(agents)

    logger.info("Running Check 4: Strategy coverage...")
    check4 = check_strategy_coverage(agents)

    logger.info("Running Check 5: Archetype coverage...")
    check5 = check_archetype_coverage(agents)

    logger.info("Running Check 6: Trait correlation...")
    check6 = check_trait_correlation(agents)

    logger.info("Running Check 7: News reaction variance (LLM)...")
    check7 = check_news_reaction_variance(agents, llm)

    checks = [check1, check2, check3, check4, check5, check6, check7]
    all_flags = sum(len(c.get('flags', [])) for c in checks)
    overall_passed = all(c.get('passed', False) for c in checks)

    report_text = format_report(
        total_agents=total,
        graph_id=graph_id,
        timestamp=timestamp,
        check1=check1,
        check2=check2,
        check3=check3,
        check4=check4,
        check5=check5,
        check6=check6,
        check7=check7,
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts_file = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    report_path = REPORTS_DIR / f"diversity_audit_{ts_file}.txt"
    report_path.write_text(report_text, encoding='utf-8')

    return report_path, overall_passed, total, all_flags


def main():
    parser = argparse.ArgumentParser(description="MiroFish agent pool diversity audit")
    parser.add_argument('--graph-id', help="Neo4j graph_id (auto-detected if omitted)")
    args = parser.parse_args()

    driver = db_setup()
    try:
        graph_id = args.graph_id
        if not graph_id:
            from incremental_update import resolve_active_graph_id
            graph_id = resolve_active_graph_id()
        if not graph_id:
            print("AUDIT FAILED — could not determine graph_id; pass --graph-id explicitly")
            sys.exit(1)

        report_path, passed, total_agents, n_issues = run_audit(driver, graph_id)

        if passed:
            print(f"AUDIT PASSED — {total_agents} agents, all checks green")
        else:
            print(f"AUDIT FAILED — {n_issues} issues found, see {report_path}")

        sys.exit(0 if passed else 1)

    finally:
        driver.close()


if __name__ == "__main__":
    main()
