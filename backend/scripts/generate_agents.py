"""
generate_agents.py — Generate a realistic pool of synthetic investor agents
and bulk-insert them into Neo4j.

Archetype-conditional trait sampling based on real market research:
- Retail amateur:     50% of pool — index/growth biased, low capital, high herd
- Retail experienced: 20% — balanced, moderate capital, value/index focused
- Prop trader:         8% — short horizon, high leverage, news-reactive
- Fund manager:        8% — benchmark-hugging, large AUM, equity heavy
- Family office:       5% — wealth preservation, long horizon, diversified
- Hedge fund:          6% — alpha-seeking, high leverage, macro/quant
- Pension fund:        3% — ultra-conservative, massive AUM, bonds heavy

Usage:
    python generate_agents.py --graph-id <uuid> [--target 8192] [--clear]
"""

import sys
import os
import uuid
import argparse
import random
import math
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np
from db_setup import setup as db_setup
from app.utils.logger import get_logger

logger = get_logger('mirofish.generate_agents')

RNG = np.random.default_rng(42)  # Fixed seed for reproducibility

# ---------------------------------------------------------------------------
# Archetype pool composition (must sum to 1.0)
# ---------------------------------------------------------------------------
ARCHETYPE_WEIGHTS = {
    'retail_amateur':     0.50,
    'retail_experienced': 0.20,
    'prop_trader':        0.08,
    'fund_manager':       0.08,
    'family_office':      0.05,
    'hedge_fund':         0.06,
    'pension_fund':       0.03,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _weighted_choice(choices, weights):
    return random.choices(choices, weights=weights, k=1)[0]

def _beta(a, b, scale=10.0):
    return round(float(np.clip(RNG.beta(a, b) * scale, 0.0, scale)), 2)

def _lognormal(mu, sigma, lo, hi):
    return round(float(np.clip(RNG.lognormal(mu, sigma), lo, hi)), 2)

def _log_uniform_capital(lo_usd, hi_usd):
    """Log-uniform capital between lo and hi USD."""
    return round(float(10 ** RNG.uniform(math.log10(lo_usd), math.log10(hi_usd))), 2)

def _pareto_horizon(scale_days, lo, hi):
    return int(min(hi, max(lo, round(1 + float(RNG.pareto(1.5)) * scale_days))))

# ---------------------------------------------------------------------------
# Archetype-conditional trait samplers
# ---------------------------------------------------------------------------

def _sample_retail_amateur() -> dict:
    return {
        'risk_tolerance':           _beta(2.0, 3.5),          # lean conservative, 2–6 typical
        'herd_behaviour':           _beta(3.5, 1.5),          # high herd
        'news_sensitivity':         _beta(3.0, 2.0),          # high news sensitivity
        'geopolitical_sensitivity': _beta(1.5, 3.0),          # low-moderate geo
        'overconfidence_bias':      _beta(2.0, 3.0),          # moderate overconfidence
        'capital_usd':              _log_uniform_capital(200, 50_000),
        'time_horizon_days':        _pareto_horizon(60, 1, 730),
        'loss_aversion_multiplier': _lognormal(1.1, 0.4, 1.0, 8.0),
        'reaction_speed_minutes':   _lognormal(5.0, 1.2, 60, 10080),  # slow — hours to days
        'investor_archetype': 'retail_amateur',
        'primary_strategy': _weighted_choice(
            ['index', 'growth', 'income', 'value', 'day_trading', 'swing', 'momentum'],
            [40,      20,       15,       10,       6,            5,       4]),
        'leverage_typical': _weighted_choice(
            ['none', '2x', '5x', '10x_plus'], [82, 13, 4, 1]),
        'formative_crash': _weighted_choice(
            ['none', 'covid_2020', 'gfc_2008', 'dotcom', 'iran_war_2026'],
            [30, 30, 20, 10, 10]),
        'fear_greed_dominant': _weighted_choice(['fear', 'greed'], [55, 45]),
        'asset_class_bias': _weighted_choice(
            ['equities', 'mixed', 'bonds', 'crypto', 'real_estate', 'commodities', 'fx'],
            [50,         25,      10,      8,        5,              1,             1]),
        'is_synthetic': True,
    }


def _sample_retail_experienced() -> dict:
    return {
        'risk_tolerance':           _beta(2.5, 2.5),
        'herd_behaviour':           _beta(2.0, 3.0),
        'news_sensitivity':         _beta(2.5, 2.5),
        'geopolitical_sensitivity': _beta(2.0, 2.5),
        'overconfidence_bias':      _beta(2.5, 2.5),
        'capital_usd':              _log_uniform_capital(5_000, 500_000),
        'time_horizon_days':        _pareto_horizon(120, 30, 1825),
        'loss_aversion_multiplier': _lognormal(0.8, 0.4, 0.7, 6.0),
        'reaction_speed_minutes':   _lognormal(4.5, 1.0, 30, 4320),
        'investor_archetype': 'retail_experienced',
        'primary_strategy': _weighted_choice(
            ['value', 'index', 'growth', 'swing', 'income', 'momentum', 'contrarian'],
            [25,      25,      20,       15,      10,       4,          1]),
        'leverage_typical': _weighted_choice(
            ['none', '2x', '5x', '10x_plus'], [65, 27, 7, 1]),
        'formative_crash': _weighted_choice(
            ['none', 'gfc_2008', 'covid_2020', 'dotcom', 'iran_war_2026'],
            [25, 30, 25, 15, 5]),
        'fear_greed_dominant': _weighted_choice(['fear', 'greed'], [48, 52]),
        'asset_class_bias': _weighted_choice(
            ['equities', 'mixed', 'bonds', 'crypto', 'real_estate', 'commodities', 'fx'],
            [45,         25,      12,      8,        5,              3,             2]),
        'is_synthetic': True,
    }


def _sample_prop_trader() -> dict:
    return {
        'risk_tolerance':           _beta(4.0, 1.5),          # high risk
        'herd_behaviour':           _beta(1.0, 4.0),          # anti-herd / contrarian
        'news_sensitivity':         _beta(4.5, 1.5),          # extremely news reactive
        'geopolitical_sensitivity': _beta(3.5, 2.0),          # high geo sensitivity
        'overconfidence_bias':      _beta(4.0, 2.0),          # high overconfidence
        'capital_usd':              _log_uniform_capital(50_000, 5_000_000),
        'time_horizon_days':        _pareto_horizon(10, 1, 90),  # very short term
        'loss_aversion_multiplier': _lognormal(0.4, 0.3, 0.2, 3.0),  # low loss aversion
        'reaction_speed_minutes':   _lognormal(1.5, 0.8, 1, 120),    # very fast
        'investor_archetype': 'prop_trader',
        'primary_strategy': _weighted_choice(
            ['day_trading', 'swing', 'momentum', 'quant', 'contrarian', 'macro'],
            [40,            25,      20,          8,       5,            2]),
        'leverage_typical': _weighted_choice(
            ['none', '2x', '5x', '10x_plus'], [15, 35, 35, 15]),
        'formative_crash': _weighted_choice(
            ['none', 'gfc_2008', 'covid_2020', 'dotcom', 'iran_war_2026'],
            [20, 25, 30, 15, 10]),
        'fear_greed_dominant': _weighted_choice(['fear', 'greed'], [30, 70]),
        'asset_class_bias': _weighted_choice(
            ['equities', 'fx', 'commodities', 'crypto', 'bonds', 'mixed', 'real_estate'],
            [35,         20,   15,            15,       8,       5,       2]),
        'is_synthetic': True,
    }


def _sample_fund_manager() -> dict:
    return {
        'risk_tolerance':           _beta(2.5, 2.5),          # moderate — benchmark constrained
        'herd_behaviour':           _beta(3.0, 2.0),          # moderate herd (benchmark hugging)
        'news_sensitivity':         _beta(2.5, 2.5),
        'geopolitical_sensitivity': _beta(2.5, 2.5),
        'overconfidence_bias':      _beta(2.5, 2.5),
        'capital_usd':              _log_uniform_capital(10_000_000, 5_000_000_000),
        'time_horizon_days':        _pareto_horizon(365, 90, 3650),
        'loss_aversion_multiplier': _lognormal(0.7, 0.3, 0.5, 4.0),
        'reaction_speed_minutes':   _lognormal(3.5, 0.8, 60, 4320),
        'investor_archetype': 'fund_manager',
        'primary_strategy': _weighted_choice(
            ['index', 'growth', 'value', 'momentum', 'income', 'quant', 'macro'],
            [30,      25,       20,      12,          8,        3,       2]),
        'leverage_typical': _weighted_choice(
            ['none', '2x', '5x', '10x_plus'], [72, 24, 4, 0]),
        'formative_crash': _weighted_choice(
            ['none', 'gfc_2008', 'dotcom', 'covid_2020', 'iran_war_2026'],
            [20, 35, 20, 20, 5]),
        'fear_greed_dominant': _weighted_choice(['fear', 'greed'], [50, 50]),
        'asset_class_bias': _weighted_choice(
            ['equities', 'bonds', 'mixed', 'real_estate', 'commodities', 'fx', 'crypto'],
            [60,         20,      12,      4,             2,             1,    1]),
        'is_synthetic': True,
    }


def _sample_family_office() -> dict:
    return {
        'risk_tolerance':           _beta(2.0, 3.0),          # conservative — wealth preservation
        'herd_behaviour':           _beta(1.5, 3.5),          # low herd — independent
        'news_sensitivity':         _beta(2.0, 2.5),
        'geopolitical_sensitivity': _beta(3.0, 2.0),          # high geo (international assets)
        'overconfidence_bias':      _beta(2.0, 2.5),
        'capital_usd':              _log_uniform_capital(5_000_000, 500_000_000),
        'time_horizon_days':        _pareto_horizon(730, 365, 3650),  # long term
        'loss_aversion_multiplier': _lognormal(0.9, 0.3, 0.7, 5.0),
        'reaction_speed_minutes':   _lognormal(4.0, 1.0, 120, 10080),  # deliberate
        'investor_archetype': 'family_office',
        'primary_strategy': _weighted_choice(
            ['value', 'income', 'macro', 'growth', 'contrarian', 'index', 'swing'],
            [28,      22,       18,      15,       10,           5,       2]),
        'leverage_typical': _weighted_choice(
            ['none', '2x', '5x', '10x_plus'], [78, 18, 4, 0]),
        'formative_crash': _weighted_choice(
            ['none', 'gfc_2008', 'dotcom', 'covid_2020', 'iran_war_2026'],
            [20, 40, 20, 15, 5]),
        'fear_greed_dominant': _weighted_choice(['fear', 'greed'], [60, 40]),
        'asset_class_bias': _weighted_choice(
            ['mixed', 'equities', 'real_estate', 'bonds', 'commodities', 'fx', 'crypto'],
            [35,      25,         20,            12,      5,             2,    1]),
        'is_synthetic': True,
    }


def _sample_hedge_fund() -> dict:
    return {
        'risk_tolerance':           _beta(4.0, 1.5),          # high risk appetite
        'herd_behaviour':           _beta(0.8, 4.0),          # very low herd — alpha seeking
        'news_sensitivity':         _beta(4.0, 1.5),          # highly reactive
        'geopolitical_sensitivity': _beta(4.0, 1.5),          # very geo sensitive
        'overconfidence_bias':      _beta(4.5, 1.5),          # high overconfidence
        'capital_usd':              _log_uniform_capital(50_000_000, 50_000_000_000),
        'time_horizon_days':        _pareto_horizon(90, 1, 730),
        'loss_aversion_multiplier': _lognormal(0.3, 0.3, 0.2, 2.5),  # low loss aversion
        'reaction_speed_minutes':   _lognormal(1.5, 0.7, 1, 240),    # fast
        'investor_archetype': 'hedge_fund',
        'primary_strategy': _weighted_choice(
            ['macro', 'quant', 'momentum', 'contrarian', 'day_trading', 'swing', 'value'],
            [25,      25,      20,          15,           8,             5,       2]),
        'leverage_typical': _weighted_choice(
            ['none', '2x', '5x', '10x_plus'], [8, 28, 38, 26]),
        'formative_crash': _weighted_choice(
            ['none', 'gfc_2008', 'dotcom', 'covid_2020', 'iran_war_2026'],
            [15, 35, 25, 20, 5]),
        'fear_greed_dominant': _weighted_choice(['fear', 'greed'], [28, 72]),
        'asset_class_bias': _weighted_choice(
            ['equities', 'fx', 'commodities', 'bonds', 'mixed', 'crypto', 'real_estate'],
            [35,         20,   15,            14,      8,       6,        2]),
        'is_synthetic': True,
    }


def _sample_pension_fund() -> dict:
    return {
        'risk_tolerance':           _beta(1.0, 5.0),          # very conservative
        'herd_behaviour':           _beta(4.0, 1.5),          # high herd — liability driven
        'news_sensitivity':         _beta(1.5, 3.5),          # low news sensitivity
        'geopolitical_sensitivity': _beta(2.0, 2.5),
        'overconfidence_bias':      _beta(1.0, 4.0),          # very low overconfidence
        'capital_usd':              _log_uniform_capital(1_000_000_000, 100_000_000_000),
        'time_horizon_days':        _pareto_horizon(3650, 1825, 3650),  # very long
        'loss_aversion_multiplier': _lognormal(1.5, 0.3, 1.2, 8.0),  # very high loss aversion
        'reaction_speed_minutes':   _lognormal(5.5, 0.8, 1440, 10080),  # days to weeks
        'investor_archetype': 'pension_fund',
        'primary_strategy': _weighted_choice(
            ['index', 'income', 'value', 'growth', 'macro', 'quant', 'momentum'],
            [45,      25,       15,      8,        4,       2,       1]),
        'leverage_typical': _weighted_choice(
            ['none', '2x', '5x', '10x_plus'], [95, 5, 0, 0]),
        'formative_crash': _weighted_choice(
            ['none', 'gfc_2008', 'dotcom', 'covid_2020', 'iran_war_2026'],
            [15, 45, 20, 18, 2]),
        'fear_greed_dominant': _weighted_choice(['fear', 'greed'], [75, 25]),
        'asset_class_bias': _weighted_choice(
            ['bonds', 'equities', 'real_estate', 'mixed', 'commodities', 'fx', 'crypto'],
            [42,      32,         14,            8,       3,             1,    0]),
        'is_synthetic': True,
    }


SAMPLERS = {
    'retail_amateur':     _sample_retail_amateur,
    'retail_experienced': _sample_retail_experienced,
    'prop_trader':        _sample_prop_trader,
    'fund_manager':       _sample_fund_manager,
    'family_office':      _sample_family_office,
    'hedge_fund':         _sample_hedge_fund,
    'pension_fund':       _sample_pension_fund,
}

# ---------------------------------------------------------------------------
# Name pools — realistic diverse names per archetype region bias
# ---------------------------------------------------------------------------

FIRST_NAMES = [
    'James','Oliver','Liam','Noah','William','Benjamin','Lucas','Henry','Alexander','Mason',
    'Emma','Charlotte','Amelia','Olivia','Sophia','Isabella','Ava','Mia','Harper','Evelyn',
    'Mohammed','Ahmed','Ali','Omar','Yusuf','Fatima','Aisha','Zainab','Nour','Layla',
    'Wei','Jing','Fang','Lei','Hui','Mei','Xiao','Yan','Jun','Ling',
    'Arjun','Priya','Rahul','Ananya','Vikram','Neha','Aditya','Pooja','Rohan','Kavya',
    'Yuki','Kenji','Sakura','Takashi','Akira','Hana','Ryo','Saki','Hiroshi','Yuna',
    'Carlos','Sofia','Miguel','Isabella','Diego','Valentina','Andres','Camila','Juan','Maria',
    'Pierre','Marie','Jean','Claire','Louis','Sophie','Nicolas','Lucie','Antoine','Mathilde',
    'Aleksandr','Elena','Dmitri','Natasha','Ivan','Olga','Sergei','Tatiana','Pavel','Anna',
    'Kwame','Amara','Kofi','Abena','Yaw','Akosua','Fiifi','Ama','Kwesi','Efua',
    'Tariq','Samira','Hassan','Leila','Riad','Nadia','Karim','Yasmine','Bilal','Dina',
    'Tom','Sarah','Jack','Emily','Harry','Jessica','George','Laura','Charlie','Amy',
    'Daniel','Grace','Ryan','Hannah','Ethan','Lily','Nathan','Zoe','Samuel','Chloe',
    'Michael','Rachel','David','Rebecca','Andrew','Victoria','Robert','Catherine','Joseph','Elizabeth',
    'Luca','Giulia','Marco','Francesca','Lorenzo','Chiara','Matteo','Elena','Andrea','Sara',
    'Felix','Anna','Paul','Julia','Stefan','Nina','Max','Lisa','Jan','Petra',
    'Hamid','Shirin','Reza','Maryam','Darius','Nasrin','Farhad','Parisa','Cyrus','Azadeh',
    'Kwabena','Abigail','Emmanuel','Grace','Daniel','Patience','Frederick','Mercy','George','Comfort',
    'Rodrigo','Ana','Felipe','Mariana','Pedro','Julia','Gabriel','Isabela','Thiago','Beatriz',
    'Mikael','Astrid','Erik','Sigrid','Lars','Ingrid','Thor','Freya','Bjorn','Helga',
    'Sanjay','Deepa','Ravi','Meena','Suresh','Anita','Raj','Sunita','Mohan','Lakshmi',
    'Hiroshi','Keiko','Taro','Hanako','Ichiro','Fumiko','Jiro','Noriko','Saburo','Michiko',
    'Patrick','Siobhan','Brendan','Aoife','Ciarán','Niamh','Seán','Roisin','Declan','Sinéad',
]

LAST_NAMES = [
    'Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis','Wilson','Taylor',
    'Anderson','Thomas','Jackson','White','Harris','Martin','Thompson','Moore','Young','Lee',
    'Chen','Wang','Zhang','Liu','Yang','Huang','Zhao','Wu','Zhou','Li',
    'Patel','Shah','Kumar','Singh','Sharma','Gupta','Mehta','Joshi','Rao','Nair',
    'Nakamura','Yamamoto','Suzuki','Tanaka','Watanabe','Ito','Kobayashi','Kato','Sato','Abe',
    'Rodriguez','Gonzalez','Hernandez','Lopez','Martinez','Perez','Sanchez','Torres','Flores','Rivera',
    'Dubois','Martin','Bernard','Thomas','Petit','Richard','Leroy','Moreau','Simon','Laurent',
    'Ivanov','Petrov','Sidorov','Fedorov','Volkov','Popov','Novikov','Morozov','Sokolov','Kozlov',
    'Mensah','Asante','Owusu','Boateng','Agyei','Osei','Darko','Appiah','Acheampong','Amankwah',
    'Al-Hassan','El-Amin','Ben-David','Al-Rashid','Ibrahim','Khalil','Mansour','Samir','Nasser','Haddad',
    'Murphy','O\'Brien','Walsh','Burke','Byrne','Kelly','Ryan','Sullivan','O\'Connor','McCarthy',
    'Larsson','Andersson','Johansson','Eriksson','Nilsson','Lindqvist','Bergström','Magnusson','Olsson','Persson',
    'Rossi','Ferrari','Russo','Bianchi','Romano','Colombo','Ricci','Marino','Greco','Bruno',
    'Müller','Schmidt','Schneider','Fischer','Weber','Meyer','Wagner','Becker','Schulz','Hoffmann',
    'Silva','Santos','Oliveira','Souza','Lima','Carvalho','Almeida','Ferreira','Rodrigues','Costa',
    'Kim','Park','Choi','Jung','Kang','Yoon','Lim','Han','Oh','Seo',
    'Reyes','Cruz','Ramos','Morales','Jimenez','Diaz','Herrera','Castro','Vargas','Romero',
    'Novak','Kovač','Horváth','Szabó','Tóth','Varga','Kis','Nagy','Fekete','Fehér',
    'Okafor','Adeyemi','Nwosu','Eze','Obi','Chukwu','Onyeka','Afolabi','Balogun','Adeleke',
    'Lindberg','Strand','Haugen','Dahl','Moen','Berg','Bakke','Solberg','Holm','Lie',
]


def _random_name():
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


# ---------------------------------------------------------------------------
# Backstory templates per archetype
# ---------------------------------------------------------------------------

BACKSTORY_TEMPLATES = {
    'retail_amateur': [
        "Started investing during the pandemic with a small ISA account. Follows financial news on social media.",
        "Opened a brokerage account after receiving an inheritance. Self-taught through YouTube and Reddit.",
        "Works in {profession} and invests spare income monthly. Uses a robo-advisor for most holdings.",
        "Recently graduated and started a stocks & shares ISA. Focused on building a long-term portfolio.",
        "Former saver who switched to investing after low interest rates. Predominantly holds index ETFs.",
        "Retail investor who got drawn in by meme stocks in 2021. Now more cautious but still speculative.",
        "Part-time investor managing a small portfolio alongside a full-time job in {profession}.",
        "Inherited some shares from a parent and has been learning to manage the portfolio independently.",
    ],
    'retail_experienced': [
        "Has been investing for over 15 years through multiple market cycles. Manages a diversified ISA portfolio.",
        "Former finance professional turned independent investor. Runs a value-focused personal portfolio.",
        "Experienced investor who survived the GFC and used it as a buying opportunity. Now systematically rebalances.",
        "Self-directed investor with a background in {profession}. Specialises in dividend income strategies.",
        "Active investor for two decades, primarily in UK and US equities. Uses fundamental analysis.",
        "Built a substantial portfolio through disciplined monthly investing since the early 2000s.",
        "Experienced trader who transitioned from active trading to a more passive approach after having children.",
    ],
    'prop_trader': [
        "Former market maker now trading proprietary capital. Specialises in short-term momentum plays.",
        "Algorithmic trader running a systematic strategy across equities and FX. Ex-investment bank.",
        "Day trader with a background in options market making. Focuses on high-frequency technical setups.",
        "Ex-hedge fund analyst trading own capital. Combines macro themes with short-term technical execution.",
        "Quantitative trader who left a tier-1 bank to trade independently. Runs stat-arb and momentum strategies.",
        "Former futures pit trader now operating electronically. Specialises in commodity and FX momentum.",
    ],
    'fund_manager': [
        "CFA charterholder managing a mid-cap equity fund at an asset management firm. 20 years experience.",
        "Portfolio manager at a large institutional asset manager. Responsible for a global equity mandate.",
        "Senior fund manager overseeing a multi-asset portfolio. Former equity analyst at a bulge-bracket bank.",
        "Active fund manager specialising in European growth equities. Runs a concentrated high-conviction portfolio.",
        "Head of equities at a regional asset manager. Focuses on quality companies with strong cash generation.",
        "Long-only equity fund manager with a value tilt. Manages separate accounts for pension fund clients.",
    ],
    'family_office': [
        "CIO of a single-family office managing generational wealth. Oversees a diversified multi-asset portfolio.",
        "Investment director at a multi-family office. Focuses on capital preservation and real asset allocation.",
        "Managing director of a private family investment vehicle. Blends public markets with private equity.",
        "Head of investments for a UHNW family office. Manages $200M+ across equities, bonds and real assets.",
        "Senior investment professional at a European family office. Specialises in alternative asset allocation.",
        "Portfolio strategist at a family office founded by a successful entrepreneur. Long-term value orientation.",
    ],
    'hedge_fund': [
        "Portfolio manager at a global macro hedge fund. Trades sovereign bonds, FX and commodity futures.",
        "Partner at a multi-strategy hedge fund. Runs a quantitative equity long-short book.",
        "Senior PM at a systematic hedge fund. Develops and manages algorithmic trading strategies.",
        "Co-founder of a mid-size hedge fund specialising in event-driven and special situations investing.",
        "Head of macro trading at a credit-focused hedge fund. Former central bank economist.",
        "Principal at a quant fund deploying machine learning across global equity markets.",
    ],
    'pension_fund': [
        "Head of public equities at a major national pension fund. Manages a $50B+ equity allocation.",
        "CIO of a large corporate defined benefit pension scheme. Focuses on liability-driven investing.",
        "Senior investment officer at a public sector pension fund. Oversees passive equity and bond mandates.",
        "Portfolio strategist at a sovereign pension fund. Responsible for strategic asset allocation.",
        "Director of investments at a large university endowment. Manages a diversified long-horizon portfolio.",
        "Deputy CIO at a national pension authority. Focuses on risk management and regulatory compliance.",
    ],
}

PROFESSIONS = [
    'healthcare', 'engineering', 'education', 'retail', 'construction',
    'hospitality', 'technology', 'logistics', 'manufacturing', 'public sector'
]


def _random_backstory(archetype: str) -> str:
    template = random.choice(BACKSTORY_TEMPLATES[archetype])
    return template.replace('{profession}', random.choice(PROFESSIONS))


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def generate_agents(target: int) -> list:
    """Generate `target` agents with archetype-conditional traits."""
    agents = []
    
    # Calculate per-archetype counts
    counts = {}
    total_assigned = 0
    archetypes = list(ARCHETYPE_WEIGHTS.keys())
    for i, arch in enumerate(archetypes):
        if i == len(archetypes) - 1:
            counts[arch] = target - total_assigned
        else:
            counts[arch] = round(ARCHETYPE_WEIGHTS[arch] * target)
            total_assigned += counts[arch]
    
    print(f"Generating {target} agents:")
    for arch, count in counts.items():
        print(f"  {arch}: {count}")
    
    now = datetime.now(timezone.utc).isoformat()
    
    for archetype, count in counts.items():
        sampler = SAMPLERS[archetype]
        for _ in range(count):
            traits = sampler()
            name = _random_name()
            node_uuid = str(uuid.uuid4())
            agents.append({
                'uuid': node_uuid,
                'name': name,
                'name_lower': name.lower(),
                'summary': _random_backstory(archetype),
                'created_at': now,
                **traits,
            })
    
    random.shuffle(agents)  # Mix archetypes
    print(f"Generated {len(agents)} agents in memory.")
    return agents


# ---------------------------------------------------------------------------
# Neo4j bulk insert
# ---------------------------------------------------------------------------

BATCH_SIZE = 500

def clear_existing_agents(driver, graph_id: str):
    """Delete all synthetic agents and their memory events."""
    with driver.session() as session:
        result = session.run(
            "MATCH (n:Entity {graph_id: $gid, is_synthetic: true}) RETURN count(n) AS c",
            gid=graph_id
        ).single()
        count = result['c']
        print(f"Deleting {count} existing synthetic agents...")
        session.run(
            """
            MATCH (n:Entity {graph_id: $gid, is_synthetic: true})
            OPTIONAL MATCH (n)-[:HAS_MEMORY]->(m:MemoryEvent)
            DETACH DELETE n, m
            """,
            gid=graph_id
        )
        print("Existing agents deleted.")


def bulk_insert(driver, graph_id: str, agents: list):
    """Bulk insert agents using UNWIND for speed."""
    total = len(agents)
    inserted = 0
    failed = 0
    
    for i in range(0, total, BATCH_SIZE):
        batch = agents[i:i + BATCH_SIZE]
        # Add graph_id to each agent
        for a in batch:
            a['graph_id'] = graph_id
        
        try:
            with driver.session() as session:
                session.run(
                    """
                    UNWIND $agents AS a
                    MERGE (n:Entity {graph_id: a.graph_id, uuid: a.uuid})
                    ON CREATE SET n += a
                    ON MATCH SET n += a
                    """,
                    agents=batch
                )
            inserted += len(batch)
            print(f"  Inserted {inserted}/{total}...", end='\r')
        except Exception as e:
            failed += len(batch)
            logger.error(f"Batch insert failed: {e}")
    
    print(f"\nInserted: {inserted}, Failed: {failed}")
    return inserted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate realistic MiroFish investor agents")
    parser.add_argument('--graph-id', required=True)
    parser.add_argument('--target', type=int, default=8192)
    parser.add_argument('--clear', action='store_true', help="Clear existing synthetic agents first")
    args = parser.parse_args()
    
    driver = db_setup()
    try:
        if args.clear:
            clear_existing_agents(driver, args.graph_id)
        
        agents = generate_agents(args.target)
        inserted = bulk_insert(driver, args.graph_id, agents)
        print(f"\nDone. {inserted} agents in Neo4j for graph {args.graph_id}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()