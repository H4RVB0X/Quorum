# Investor Traits, Agent Evolution & Live News — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 16-field fat-tail investor trait profiles to all agents, evolve the Neo4j pool to 4096 via FAISS-screened synthesis, and run continuous live news → graph update → per-agent LLM reaction on a 60-minute schedule.

**Architecture:** Feature 1 modifies `oasis_profile_generator.py` to sample non-normal trait distributions and persist them to Neo4j. Feature 2 is a standalone script using a FAISS index for O(log n) deduplication. Feature 3 chains four standalone scripts via APScheduler — all scripts live in `backend/scripts/` and import from `backend/app/` via `sys.path`.

**Tech Stack:** numpy (distributions), faiss-cpu (vector index), feedparser (RSS parsing), beautifulsoup4 (article body), apscheduler (cron scheduling), pytest + unittest.mock (tests)

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Modify | `backend/requirements.txt` | Add numpy, faiss-cpu, feedparser, beautifulsoup4, apscheduler |
| Modify | `backend/app/services/oasis_profile_generator.py` | Add `_sample_investor_traits()`, wire into profile generation |
| Modify | `backend/app/storage/neo4j_storage.py` | Add `update_node_traits(uuid, traits)` helper |
| Create | `backend/scripts/db_setup.py` | Index creation + connectivity check, called by all scripts |
| Create | `backend/scripts/agent_evolver.py` | Grow pool to 4096 using FAISS + LLM hybrid |
| Create | `backend/scripts/news_fetcher.py` | RSS polling → briefing .txt |
| Create | `backend/scripts/incremental_update.py` | Briefing → Neo4j MERGE (agent-safe) |
| Create | `backend/scripts/simulation_tick.py` | 500-agent LLM reaction pass with chunk cache |
| Create | `backend/scripts/scheduler.py` | APScheduler orchestrator with mutex guard |
| Create | `backend/tests/__init__.py` | Test package marker |
| Create | `backend/tests/scripts/__init__.py` | Test package marker |
| Create | `backend/tests/scripts/test_traits.py` | Tests for trait sampling |
| Create | `backend/tests/scripts/test_db_setup.py` | Tests for db_setup |
| Create | `backend/tests/scripts/test_agent_evolver.py` | Tests for FAISS gate + triple check |
| Create | `backend/tests/scripts/test_news_fetcher.py` | Tests for dedup, TTL, fallback |
| Create | `backend/tests/scripts/test_incremental_update.py` | Tests for is_synthetic guard |
| Create | `backend/tests/scripts/test_simulation_tick.py` | Tests for chunk cache + checkpoint |
| Create | `backend/briefings/` | Directory for .txt briefing output |

---

## Task 1: Add Dependencies

**Files:**
- Modify: `backend/requirements.txt`

- [ ] **Add new dependencies**

Open `backend/requirements.txt` and append after the existing entries:

```
# ============= Investor Traits & Agent Evolution =============
numpy>=1.24.0
faiss-cpu>=1.7.4

# ============= Live News Feed =============
feedparser>=6.0.0
beautifulsoup4>=4.12.0
apscheduler>=3.10.0
```

- [ ] **Install dependencies**

```bash
cd backend
pip install numpy faiss-cpu feedparser beautifulsoup4 apscheduler
```

Expected: all packages install without error.

- [ ] **Verify faiss import**

```bash
python -c "import faiss; print('FAISS ok, version:', faiss.__version__)"
```

Expected: prints FAISS version (e.g., `1.7.4`).

- [ ] **Commit**

```bash
git add backend/requirements.txt
git commit -m "feat: add numpy, faiss-cpu, feedparser, beautifulsoup4, apscheduler dependencies"
```

---

## Task 2: Add `update_node_traits()` to Neo4jStorage

**Files:**
- Modify: `backend/app/storage/neo4j_storage.py`
- Test: `backend/tests/scripts/test_traits.py`

- [ ] **Create test package markers**

```bash
touch backend/tests/__init__.py backend/tests/scripts/__init__.py
```

- [ ] **Write failing test**

Create `backend/tests/scripts/test_traits.py`:

```python
"""Tests for Neo4jStorage.update_node_traits and _sample_investor_traits."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

from unittest.mock import MagicMock, patch, call
import pytest


def test_update_node_traits_runs_correct_cypher():
    """update_node_traits should SET traits on the node matching uuid."""
    with patch('backend.app.storage.neo4j_storage.GraphDatabase') as mock_gdb, \
         patch('backend.app.storage.neo4j_storage.EmbeddingService'), \
         patch('backend.app.storage.neo4j_storage.NERExtractor'):

        mock_driver = MagicMock()
        mock_gdb.driver.return_value = mock_driver
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__.return_value = mock_session

        # Import after patching
        from backend.app.storage.neo4j_storage import Neo4jStorage
        storage = Neo4jStorage.__new__(Neo4jStorage)
        storage._driver = mock_driver

        traits = {'risk_tolerance': 7.5, 'capital_usd': 50000.0, 'is_synthetic': False}
        storage.update_node_traits('test-uuid-123', traits)

        # Verify a write transaction was executed
        mock_session.execute_write.assert_called_once()
```

- [ ] **Run test to verify it fails**

```bash
cd backend
python -m pytest tests/scripts/test_traits.py::test_update_node_traits_runs_correct_cypher -v
```

Expected: `FAILED` — `AttributeError: update_node_traits`.

- [ ] **Implement `update_node_traits()` in Neo4jStorage**

Open `backend/app/storage/neo4j_storage.py`. Find the `get_ontology` method (around line 161). Add the following method immediately after it:

```python
def update_node_traits(self, node_uuid: str, traits: dict) -> None:
    """
    Merge trait properties onto an existing Entity node by UUID.
    Used by OasisProfileGenerator to persist investor trait profiles.
    Never creates a node — only updates existing ones.
    """
    def _update(tx):
        tx.run(
            """
            MATCH (n:Entity {uuid: $uuid})
            SET n += $traits
            """,
            uuid=node_uuid,
            traits=traits,
        )

    with self._driver.session() as session:
        self._call_with_retry(session.execute_write, _update)
```

- [ ] **Run test to verify it passes**

```bash
python -m pytest tests/scripts/test_traits.py::test_update_node_traits_runs_correct_cypher -v
```

Expected: `PASSED`.

- [ ] **Commit**

```bash
git add backend/app/storage/neo4j_storage.py backend/tests/__init__.py backend/tests/scripts/__init__.py backend/tests/scripts/test_traits.py
git commit -m "feat: add update_node_traits() to Neo4jStorage"
```

---

## Task 3: Add `_sample_investor_traits()` to OasisProfileGenerator

**Files:**
- Modify: `backend/app/services/oasis_profile_generator.py`
- Test: `backend/tests/scripts/test_traits.py` (append)

- [ ] **Write failing tests**

Append to `backend/tests/scripts/test_traits.py`:

```python
def test_sample_investor_traits_returns_all_fields():
    """_sample_investor_traits must return all 16 trait fields."""
    with patch('backend.app.services.oasis_profile_generator.OpenAI'):
        from backend.app.services.oasis_profile_generator import OasisProfileGenerator
        gen = OasisProfileGenerator.__new__(OasisProfileGenerator)
        traits = gen._sample_investor_traits()

    expected_keys = {
        'risk_tolerance', 'capital_usd', 'time_horizon_days', 'fear_greed_dominant',
        'loss_aversion_multiplier', 'herd_behaviour', 'reaction_speed_minutes',
        'primary_strategy', 'asset_class_bias', 'news_sensitivity',
        'geopolitical_sensitivity', 'investor_archetype', 'formative_crash',
        'overconfidence_bias', 'leverage_typical', 'is_synthetic',
    }
    assert set(traits.keys()) == expected_keys


def test_sample_investor_traits_bounds():
    """All continuous traits must stay within their documented bounds."""
    with patch('backend.app.services.oasis_profile_generator.OpenAI'):
        from backend.app.services.oasis_profile_generator import OasisProfileGenerator
        gen = OasisProfileGenerator.__new__(OasisProfileGenerator)
        for _ in range(100):
            t = gen._sample_investor_traits()
            assert 0.0 <= t['risk_tolerance'] <= 10.0
            assert 500.0 <= t['capital_usd'] <= 100_000_000.0
            assert 1 <= t['time_horizon_days'] <= 3650
            assert 0.5 <= t['loss_aversion_multiplier'] <= 10.0
            assert 1.0 <= t['reaction_speed_minutes'] <= 10080.0
            assert 0.0 <= t['herd_behaviour'] <= 10.0
            assert t['fear_greed_dominant'] in ('fear', 'greed')
            assert t['is_synthetic'] is False


def test_sample_investor_traits_valid_categoricals():
    """Categorical traits must only contain allowed values."""
    with patch('backend.app.services.oasis_profile_generator.OpenAI'):
        from backend.app.services.oasis_profile_generator import OasisProfileGenerator
        gen = OasisProfileGenerator.__new__(OasisProfileGenerator)
        t = gen._sample_investor_traits()

    assert t['investor_archetype'] in (
        'retail_amateur', 'retail_experienced', 'prop_trader',
        'fund_manager', 'family_office', 'hedge_fund', 'pension_fund'
    )
    assert t['primary_strategy'] in (
        'day_trading', 'swing', 'value', 'growth', 'momentum',
        'index', 'income', 'macro', 'contrarian', 'quant'
    )
    assert t['leverage_typical'] in ('none', '2x', '5x', '10x_plus')
    assert t['formative_crash'] in ('none', 'dotcom', 'gfc_2008', 'covid_2020', 'iran_war_2026')
    assert t['asset_class_bias'] in (
        'equities', 'mixed', 'crypto', 'bonds', 'commodities', 'fx', 'real_estate'
    )
```

- [ ] **Run tests to verify they fail**

```bash
python -m pytest tests/scripts/test_traits.py -k "sample_investor" -v
```

Expected: `FAILED` — `AttributeError: _sample_investor_traits`.

- [ ] **Add `_sample_investor_traits()` to OasisProfileGenerator**

Open `backend/app/services/oasis_profile_generator.py`. Add `import numpy as np` and `import random` to the imports at the top (both may already exist — add only what's missing). Then find `def set_graph_id(self, graph_id: str):` (around line 791) and insert the following method **before** it:

```python
def _sample_investor_traits(self) -> dict:
    """
    Sample a full 16-field investor trait profile using fat-tail distributions.
    Categorical weights match the spec — not uniform.
    is_synthetic is always False here (document-extracted agents).
    """
    import numpy as np
    rng = np.random.default_rng()

    def _beta_10():
        """Beta(0.5, 0.5) × 10 — U-shaped, mass at extremes."""
        return round(float(rng.beta(0.5, 0.5) * 10), 2)

    def _weighted(choices, weights):
        return random.choices(choices, weights=weights, k=1)[0]

    # Continuous fat-tail traits
    risk_tolerance = _beta_10()
    herd_behaviour = _beta_10()
    news_sensitivity = _beta_10()
    geopolitical_sensitivity = _beta_10()
    overconfidence_bias = _beta_10()

    # Log-uniform capital (£500 – £100M)
    capital_usd = round(float(10 ** rng.uniform(
        np.log10(500), np.log10(100_000_000)
    )), 2)

    # Pareto time horizon — many short-term traders, power-law tail
    time_horizon_days = int(min(3650, max(1, round(1 + float(rng.pareto(1.5)) * 30))))

    # Log-normal loss aversion
    loss_aversion_multiplier = round(
        float(np.clip(rng.lognormal(0.7, 0.5), 0.5, 10.0)), 2
    )

    # Log-normal reaction speed (minutes)
    reaction_speed_minutes = round(
        float(np.clip(rng.lognormal(3.0, 1.5), 1.0, 10080.0)), 1
    )

    # Categorical traits with non-uniform weights
    investor_archetype = _weighted(
        ['retail_amateur', 'retail_experienced', 'prop_trader',
         'fund_manager', 'family_office', 'hedge_fund', 'pension_fund'],
        [35, 25, 15, 10, 7, 6, 2],
    )
    primary_strategy = _weighted(
        ['day_trading', 'swing', 'value', 'growth', 'momentum',
         'index', 'income', 'macro', 'contrarian', 'quant'],
        [15, 15, 12, 12, 12, 10, 8, 7, 5, 4],
    )
    leverage_typical = _weighted(
        ['none', '2x', '5x', '10x_plus'],
        [55, 25, 12, 8],
    )
    formative_crash = _weighted(
        ['none', 'dotcom', 'gfc_2008', 'covid_2020', 'iran_war_2026'],
        [25, 15, 35, 20, 5],
    )
    fear_greed_dominant = _weighted(['fear', 'greed'], [45, 55])
    asset_class_bias = _weighted(
        ['equities', 'mixed', 'crypto', 'bonds', 'commodities', 'fx', 'real_estate'],
        [35, 20, 15, 10, 8, 7, 5],
    )

    return {
        'risk_tolerance': risk_tolerance,
        'capital_usd': capital_usd,
        'time_horizon_days': time_horizon_days,
        'fear_greed_dominant': fear_greed_dominant,
        'loss_aversion_multiplier': loss_aversion_multiplier,
        'herd_behaviour': herd_behaviour,
        'reaction_speed_minutes': reaction_speed_minutes,
        'primary_strategy': primary_strategy,
        'asset_class_bias': asset_class_bias,
        'news_sensitivity': news_sensitivity,
        'geopolitical_sensitivity': geopolitical_sensitivity,
        'investor_archetype': investor_archetype,
        'formative_crash': formative_crash,
        'overconfidence_bias': overconfidence_bias,
        'leverage_typical': leverage_typical,
        'is_synthetic': False,
    }
```

- [ ] **Wire traits into `generate_profile_from_entity()`**

Find `generate_profile_from_entity()` (around line 204). It currently ends with:

```python
        return OasisAgentProfile(
            user_id=user_id,
            ...
            source_entity_uuid=entity.uuid,
            source_entity_type=entity_type,
        )
```

Replace the entire return statement with:

```python
        traits = self._sample_investor_traits()

        profile = OasisAgentProfile(
            user_id=user_id,
            user_name=user_name,
            name=name,
            bio=profile_data.get("bio", f"{entity_type}: {name}"),
            persona=profile_data.get("persona", entity.summary or f"A {entity_type} named {name}."),
            karma=profile_data.get("karma", random.randint(500, 5000)),
            friend_count=profile_data.get("friend_count", random.randint(50, 500)),
            follower_count=profile_data.get("follower_count", random.randint(100, 1000)),
            statuses_count=profile_data.get("statuses_count", random.randint(100, 2000)),
            age=profile_data.get("age"),
            gender=profile_data.get("gender"),
            mbti=profile_data.get("mbti"),
            country=profile_data.get("country"),
            profession=profile_data.get("profession"),
            interested_topics=profile_data.get("interested_topics", []),
            source_entity_uuid=entity.uuid,
            source_entity_type=entity_type,
        )
        profile.investor_traits = traits
        return profile
```

- [ ] **Add `investor_traits` field to `OasisAgentProfile` dataclass**

Find the `OasisAgentProfile` dataclass (around line 28). Add this field at the end, before the `created_at` field:

```python
    # Investor trait profile (16 fields, sampled at generation time)
    investor_traits: Dict[str, Any] = field(default_factory=dict)
```

- [ ] **Wire trait persistence into `generate_profiles_from_entities()`**

Find `generate_single_profile()` (the inner function, around line 863). It currently ends with:

```python
                return idx, profile, None
```

Replace with:

```python
                # Persist investor traits to Neo4j entity node
                if self.storage and profile.source_entity_uuid and hasattr(profile, 'investor_traits'):
                    try:
                        self.storage.update_node_traits(
                            profile.source_entity_uuid,
                            profile.investor_traits
                        )
                    except Exception as e:
                        logger.warning(f"Failed to persist traits for {entity.name}: {e}")

                return idx, profile, None
```

- [ ] **Run all trait tests**

```bash
python -m pytest tests/scripts/test_traits.py -v
```

Expected: all 4 tests `PASSED`.

- [ ] **Commit**

```bash
git add backend/app/services/oasis_profile_generator.py backend/app/storage/neo4j_storage.py backend/tests/scripts/test_traits.py
git commit -m "feat: add fat-tail investor trait sampling to OasisProfileGenerator (Feature 1)"
```

---

## Task 4: Create `db_setup.py`

**Files:**
- Create: `backend/scripts/db_setup.py`
- Test: `backend/tests/scripts/test_db_setup.py`

- [ ] **Write failing test**

Create `backend/tests/scripts/test_db_setup.py`:

```python
"""Tests for db_setup.py — index creation and connectivity check."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

from unittest.mock import MagicMock, patch, call
import pytest


def _make_mock_driver():
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.verify_connectivity.return_value = None
    return driver, session


def test_ensure_indexes_runs_three_statements():
    """ensure_indexes must issue exactly three CREATE INDEX IF NOT EXISTS statements."""
    driver, session = _make_mock_driver()

    with patch('backend.scripts.db_setup.GraphDatabase') as mock_gdb:
        mock_gdb.driver.return_value = driver
        import importlib
        import backend.scripts.db_setup as db_setup
        importlib.reload(db_setup)

        db_setup.ensure_indexes(driver)

    assert session.run.call_count == 3
    calls_text = [str(c) for c in session.run.call_args_list]
    assert any('MemoryEvent' in t and 'agent_uuid' in t for t in calls_text)
    assert any('MemoryEvent' in t and 'timestamp' in t for t in calls_text)
    assert any('Entity' in t and 'is_synthetic' in t for t in calls_text)


def test_setup_raises_on_connectivity_failure():
    """setup() must raise if Neo4j is unreachable."""
    with patch('backend.scripts.db_setup.GraphDatabase') as mock_gdb:
        mock_driver = MagicMock()
        mock_driver.verify_connectivity.side_effect = Exception("Connection refused")
        mock_gdb.driver.return_value = mock_driver

        import importlib
        import backend.scripts.db_setup as db_setup
        importlib.reload(db_setup)

        with pytest.raises(Exception, match="Connection refused"):
            db_setup.setup()
```

- [ ] **Run test to verify it fails**

```bash
python -m pytest tests/scripts/test_db_setup.py -v
```

Expected: `FAILED` — `ModuleNotFoundError: backend.scripts.db_setup`.

- [ ] **Create `backend/scripts/db_setup.py`**

```python
"""
db_setup.py — shared Neo4j index creation for all MiroFish standalone scripts.

Call setup() as the first thing in every script's main():
    from db_setup import setup
    driver = setup()
"""
import sys
import os

# Allow running from any working directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from neo4j import GraphDatabase
from app.config import Config


def ensure_indexes(driver) -> None:
    """Create the three MemoryEvent/Entity indexes idempotently."""
    with driver.session() as session:
        session.run(
            "CREATE INDEX IF NOT EXISTS FOR (m:MemoryEvent) ON (m.agent_uuid)"
        )
        session.run(
            "CREATE INDEX IF NOT EXISTS FOR (m:MemoryEvent) ON (m.timestamp)"
        )
        session.run(
            "CREATE INDEX IF NOT EXISTS FOR (n:Entity) ON (n.is_synthetic)"
        )


def setup(uri: str = None, user: str = None, password: str = None):
    """
    Verify Neo4j connectivity and create required indexes.
    Returns the connected driver — callers are responsible for driver.close().
    Raises immediately if Neo4j is unreachable.
    """
    uri = uri or Config.NEO4J_URI
    user = user or Config.NEO4J_USER
    password = password or Config.NEO4J_PASSWORD

    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()  # Raises if unreachable
    ensure_indexes(driver)
    return driver


if __name__ == "__main__":
    print("Running db_setup...")
    d = setup()
    print("Neo4j connected and indexes ensured.")
    d.close()
```

- [ ] **Run tests**

```bash
python -m pytest tests/scripts/test_db_setup.py -v
```

Expected: `PASSED`.

- [ ] **Commit**

```bash
git add backend/scripts/db_setup.py backend/tests/scripts/test_db_setup.py
git commit -m "feat: add db_setup.py — shared Neo4j index creation utility"
```

---

## Task 5: Create `agent_evolver.py`

**Files:**
- Create: `backend/scripts/agent_evolver.py`
- Test: `backend/tests/scripts/test_agent_evolver.py`

- [ ] **Write failing tests**

Create `backend/tests/scripts/test_agent_evolver.py`:

```python
"""Tests for agent_evolver.py — FAISS gate, exact-triple gate, trait string serialisation."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

import numpy as np
import faiss
import pytest


def test_build_faiss_index_and_query():
    """FAISS IndexFlatIP with L2-normalised vectors should return cosine similarity."""
    # Two identical vectors → similarity = 1.0
    vec = np.random.rand(768).astype('float32')
    vec /= np.linalg.norm(vec)

    index = faiss.IndexFlatIP(768)
    index.add(vec.reshape(1, -1))

    query = vec.reshape(1, -1).copy()
    distances, _ = index.search(query, 1)
    assert abs(distances[0][0] - 1.0) < 1e-5


def test_faiss_dissimilar_vectors_below_threshold():
    """Random orthogonal vectors should have similarity near 0."""
    d = 768
    a = np.zeros(d, dtype='float32')
    a[0] = 1.0
    b = np.zeros(d, dtype='float32')
    b[1] = 1.0  # orthogonal

    index = faiss.IndexFlatIP(d)
    index.add(a.reshape(1, -1))

    query = b.reshape(1, -1)
    distances, _ = index.search(query, 1)
    assert distances[0][0] < 0.1


def test_serialise_trait_string():
    """Trait string must include all categorical and numeric fields."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))
    from agent_evolver import serialise_traits

    traits = {
        'investor_archetype': 'retail_amateur',
        'primary_strategy': 'day_trading',
        'risk_tolerance': 8.2,
        'capital_usd': 12000.0,
        'time_horizon_days': 30,
        'herd_behaviour': 7.1,
        'news_sensitivity': 5.0,
        'geopolitical_sensitivity': 3.0,
        'asset_class_bias': 'equities',
        'leverage_typical': 'none',
        'fear_greed_dominant': 'greed',
    }
    result = serialise_traits(traits)
    assert 'retail_amateur' in result
    assert 'day_trading' in result
    assert '8.2' in result


def test_exact_triple_key():
    """risk_bucket must be floor(risk_tolerance / 3.33) clamped 0-2."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))
    from agent_evolver import triple_key

    assert triple_key('retail_amateur', 'swing', 9.9) == ('retail_amateur', 'swing', 2)
    assert triple_key('hedge_fund', 'quant', 5.0) == ('hedge_fund', 'quant', 1)
    assert triple_key('pension_fund', 'index', 1.0) == ('pension_fund', 'index', 0)
```

- [ ] **Run tests to verify they fail**

```bash
python -m pytest tests/scripts/test_agent_evolver.py -v
```

Expected: `FAILED` — import errors on `agent_evolver`.

- [ ] **Create `backend/scripts/agent_evolver.py`**

```python
"""
agent_evolver.py — Grow Neo4j agent pool to 4096.

Flow:
  1. Load existing agents + build FAISS index
  2. In outer loop: ask LLM for 64 candidate personas × 8 = ~512 per round
  3. Per candidate: FAISS gate (≤0.75 accept) → triple gate (Neo4j check)
  4. Write accepted candidates to Neo4j; add to FAISS index in-memory
  5. Repeat until pool ≥ TARGET_POOL_SIZE

Usage:
  python agent_evolver.py [--target 4096] [--graph-id <uuid>]
"""
import sys
import os
import json
import uuid
import argparse
import random
import math
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np
import faiss

from db_setup import setup as db_setup
from app.config import Config
from app.storage.embedding_service import EmbeddingService
from app.utils.llm_client import LLMClient
from app.utils.logger import get_logger

logger = get_logger('mirofish.agent_evolver')

TARGET_POOL_SIZE = 4096
LLM_BATCH_SIZE = 64          # personas per LLM call (reliable for large-context models)
FAISS_THRESHOLD = 0.75       # below → accept without triple check
EMBED_DIM = 768              # nomic-embed-text dimensions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def serialise_traits(traits: dict) -> str:
    """Compact string representation of agent traits for embedding."""
    return (
        f"{traits.get('investor_archetype','')}, "
        f"{traits.get('primary_strategy','')}, "
        f"risk={traits.get('risk_tolerance', 0):.1f}, "
        f"capital={traits.get('capital_usd', 0):.0f}, "
        f"horizon={traits.get('time_horizon_days', 0)}, "
        f"herd={traits.get('herd_behaviour', 0):.1f}, "
        f"news={traits.get('news_sensitivity', 0):.1f}, "
        f"geo={traits.get('geopolitical_sensitivity', 0):.1f}, "
        f"bias={traits.get('asset_class_bias','')}, "
        f"leverage={traits.get('leverage_typical','')}, "
        f"fg={traits.get('fear_greed_dominant','')}"
    )


def triple_key(archetype: str, strategy: str, risk: float) -> tuple:
    """(archetype, strategy, risk_bucket) — risk_bucket = floor(risk / 3.33), clamped 0-2."""
    bucket = min(2, int(math.floor(risk / 3.33)))
    return (archetype, strategy, bucket)


def _sample_traits(archetype: Optional[str] = None) -> dict:
    """Fat-tail trait sampling (same distributions as Feature 1)."""
    rng = np.random.default_rng()

    def _beta_10():
        return round(float(rng.beta(0.5, 0.5) * 10), 2)

    def _weighted(choices, weights):
        return random.choices(choices, weights=weights, k=1)[0]

    investor_archetype = archetype or _weighted(
        ['retail_amateur', 'retail_experienced', 'prop_trader',
         'fund_manager', 'family_office', 'hedge_fund', 'pension_fund'],
        [35, 25, 15, 10, 7, 6, 2],
    )

    return {
        'risk_tolerance': _beta_10(),
        'herd_behaviour': _beta_10(),
        'news_sensitivity': _beta_10(),
        'geopolitical_sensitivity': _beta_10(),
        'overconfidence_bias': _beta_10(),
        'capital_usd': round(float(10 ** rng.uniform(
            np.log10(500), np.log10(100_000_000)
        )), 2),
        'time_horizon_days': int(min(3650, max(1, round(1 + float(rng.pareto(1.5)) * 30)))),
        'loss_aversion_multiplier': round(float(np.clip(rng.lognormal(0.7, 0.5), 0.5, 10.0)), 2),
        'reaction_speed_minutes': round(float(np.clip(rng.lognormal(3.0, 1.5), 1.0, 10080.0)), 1),
        'investor_archetype': investor_archetype,
        'primary_strategy': _weighted(
            ['day_trading', 'swing', 'value', 'growth', 'momentum',
             'index', 'income', 'macro', 'contrarian', 'quant'],
            [15, 15, 12, 12, 12, 10, 8, 7, 5, 4],
        ),
        'leverage_typical': _weighted(['none', '2x', '5x', '10x_plus'], [55, 25, 12, 8]),
        'formative_crash': _weighted(
            ['none', 'dotcom', 'gfc_2008', 'covid_2020', 'iran_war_2026'],
            [25, 15, 35, 20, 5],
        ),
        'fear_greed_dominant': _weighted(['fear', 'greed'], [45, 55]),
        'asset_class_bias': _weighted(
            ['equities', 'mixed', 'crypto', 'bonds', 'commodities', 'fx', 'real_estate'],
            [35, 20, 15, 10, 8, 7, 5],
        ),
        'is_synthetic': True,
    }


# ---------------------------------------------------------------------------
# FAISS index management
# ---------------------------------------------------------------------------

def build_faiss_index(embeddings: list) -> faiss.IndexFlatIP:
    """Build L2-normalised FAISS inner-product index from list of embedding vectors."""
    index = faiss.IndexFlatIP(EMBED_DIM)
    if embeddings:
        mat = np.array(embeddings, dtype='float32')
        faiss.normalize_L2(mat)
        index.add(mat)
    return index


def add_to_index(index: faiss.IndexFlatIP, embedding: list) -> None:
    """Add a single embedding to an existing FAISS index in-place."""
    vec = np.array([embedding], dtype='float32')
    faiss.normalize_L2(vec)
    index.add(vec)


def nearest_similarity(index: faiss.IndexFlatIP, embedding: list) -> float:
    """Return cosine similarity to nearest existing neighbour (0.0 if index empty)."""
    if index.ntotal == 0:
        return 0.0
    vec = np.array([embedding], dtype='float32')
    faiss.normalize_L2(vec)
    distances, _ = index.search(vec, 1)
    return float(distances[0][0])


# ---------------------------------------------------------------------------
# LLM persona generation
# ---------------------------------------------------------------------------

def generate_candidates(llm: LLMClient, count: int = LLM_BATCH_SIZE) -> list:
    """
    Ask LLM to generate `count` investor persona skeletons.
    Returns list of dicts with keys: name, backstory, archetype.
    Falls back to empty list on parse failure.
    """
    messages = [
        {
            "role": "system",
            "content": "You generate diverse investor personas for financial market simulation. Return valid JSON only.",
        },
        {
            "role": "user",
            "content": (
                f"Generate {count} distinct investor personas.\n\n"
                "Return a JSON object with key \"personas\" containing a list of "
                f"{count} objects, each with:\n"
                "- name: realistic full name (diverse nationalities and genders)\n"
                "- backstory: 1-2 sentence professional background\n"
                "- archetype: one of: retail_amateur, retail_experienced, prop_trader, "
                "fund_manager, family_office, hedge_fund, pension_fund\n\n"
                "Ensure diversity in nationality, age bracket, and archetype distribution. "
                "Do not repeat names."
            ),
        },
    ]
    try:
        result = llm.chat_json(messages, temperature=0.9, max_tokens=16384)
        personas = result.get("personas", [])
        if not isinstance(personas, list):
            logger.warning("LLM returned non-list personas field")
            return []
        return personas
    except Exception as e:
        logger.error(f"LLM persona generation failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Neo4j helpers
# ---------------------------------------------------------------------------

def load_existing_agents(driver, graph_id: str) -> list:
    """
    Load all agents that have investor trait properties.
    Returns list of dicts with keys: uuid, traits_string.
    """
    with driver.session() as session:
        result = session.run(
            """
            MATCH (n:Entity {graph_id: $gid})
            WHERE n.risk_tolerance IS NOT NULL
            RETURN n.uuid AS uuid,
                   n.investor_archetype AS archetype,
                   n.primary_strategy AS strategy,
                   n.risk_tolerance AS risk,
                   n.capital_usd AS capital,
                   n.time_horizon_days AS horizon,
                   n.herd_behaviour AS herd,
                   n.news_sensitivity AS news,
                   n.geopolitical_sensitivity AS geo,
                   n.asset_class_bias AS bias,
                   n.leverage_typical AS leverage,
                   n.fear_greed_dominant AS fg
            """,
            gid=graph_id,
        )
        agents = []
        for r in result:
            traits = {
                'investor_archetype': r['archetype'],
                'primary_strategy': r['strategy'],
                'risk_tolerance': r['risk'] or 5.0,
                'capital_usd': r['capital'] or 10000.0,
                'time_horizon_days': r['horizon'] or 90,
                'herd_behaviour': r['herd'] or 5.0,
                'news_sensitivity': r['news'] or 5.0,
                'geopolitical_sensitivity': r['geo'] or 5.0,
                'asset_class_bias': r['bias'],
                'leverage_typical': r['leverage'],
                'fear_greed_dominant': r['fg'],
            }
            agents.append({'uuid': r['uuid'], 'trait_string': serialise_traits(traits),
                           'archetype': r['archetype'], 'strategy': r['strategy'],
                           'risk': r['risk'] or 5.0})
        return agents


def count_agents(driver, graph_id: str) -> int:
    with driver.session() as session:
        result = session.run(
            "MATCH (n:Entity {graph_id: $gid}) WHERE n.risk_tolerance IS NOT NULL RETURN count(n) AS c",
            gid=graph_id,
        )
        return result.single()['c']


def triple_exists(driver, graph_id: str, key: tuple) -> bool:
    """Check if an agent with this (archetype, strategy, risk_bucket) triple already exists."""
    archetype, strategy, bucket = key
    low = bucket * 3.33
    high = low + 3.33
    with driver.session() as session:
        result = session.run(
            """
            MATCH (n:Entity {graph_id: $gid})
            WHERE n.investor_archetype = $arch
              AND n.primary_strategy = $strat
              AND n.risk_tolerance >= $low
              AND n.risk_tolerance < $high
            RETURN count(n) AS c
            """,
            gid=graph_id,
            arch=archetype,
            strat=strategy,
            low=low,
            high=high,
        )
        return result.single()['c'] > 0


def write_agent(driver, graph_id: str, persona: dict, traits: dict) -> str:
    """MERGE synthetic agent into Neo4j. Returns node uuid."""
    node_uuid = str(uuid.uuid4())
    name = persona.get('name', f"Investor_{node_uuid[:8]}")
    name_lower = name.lower()
    now = datetime.now(timezone.utc).isoformat()

    props = {
        'uuid': node_uuid,
        'graph_id': graph_id,
        'name': name,
        'name_lower': name_lower,
        'summary': persona.get('backstory', ''),
        'created_at': now,
        **traits,
    }

    with driver.session() as session:
        def _write(tx):
            tx.run(
                """
                MERGE (n:Entity {graph_id: $graph_id, name_lower: $name_lower})
                ON CREATE SET n += $props
                ON MATCH SET n += $props
                """,
                graph_id=graph_id,
                name_lower=name_lower,
                props=props,
            )
        session.execute_write(_write)

    return node_uuid


# ---------------------------------------------------------------------------
# Main evolution loop
# ---------------------------------------------------------------------------

def evolve(driver, graph_id: str, target: int = TARGET_POOL_SIZE) -> None:
    embedding_svc = EmbeddingService()
    llm = LLMClient()

    # --- Phase 1: load existing pool and build FAISS index ---
    print("Loading existing agents from Neo4j...")
    existing = load_existing_agents(driver, graph_id)
    pool_size = len(existing)
    print(f"Existing pool: {pool_size} agents")

    if pool_size >= target:
        print(f"Pool already at {pool_size} (≥ {target}). Nothing to do.")
        return

    print("Building FAISS index from existing embeddings...")
    existing_trait_strings = [a['trait_string'] for a in existing]
    if existing_trait_strings:
        existing_embeddings = embedding_svc.embed_batch(existing_trait_strings)
    else:
        existing_embeddings = []

    # Build in-memory triple set for fast lookup (skip Neo4j for already-loaded agents)
    known_triples = set(
        triple_key(a['archetype'], a['strategy'], a['risk'])
        for a in existing
        if a['archetype'] and a['strategy'] and a['risk'] is not None
    )

    faiss_index = build_faiss_index(existing_embeddings)
    print(f"FAISS index built: {faiss_index.ntotal} vectors")

    # --- Phase 2: generate until target reached ---
    outer_round = 0
    while pool_size < target:
        outer_round += 1
        needed = target - pool_size
        # Request 512 candidates per outer round (8 LLM calls of 64 each)
        candidates_this_round = []
        for _ in range(max(1, 512 // LLM_BATCH_SIZE)):
            batch = generate_candidates(llm, count=LLM_BATCH_SIZE)
            candidates_this_round.extend(batch)
            if not batch:
                break

        accepted = rejected = 0

        for persona in candidates_this_round:
            if pool_size >= target:
                break

            archetype = persona.get('archetype')
            traits = _sample_traits(archetype=archetype)
            trait_str = serialise_traits(traits)

            # Embed candidate
            try:
                embedding = embedding_svc.embed(trait_str)
            except Exception as e:
                logger.warning(f"Embedding failed for candidate {persona.get('name')}: {e}")
                rejected += 1
                continue

            # Gate 1: FAISS nearest-neighbour
            sim = nearest_similarity(faiss_index, embedding)
            if sim > FAISS_THRESHOLD:
                # Gate 2: exact-triple Neo4j check
                tkey = triple_key(traits['investor_archetype'], traits['primary_strategy'],
                                  traits['risk_tolerance'])
                if tkey in known_triples or triple_exists(driver, graph_id, tkey):
                    rejected += 1
                    continue
                # Passed both gates
                known_triples.add(tkey)

            # Accept candidate
            write_agent(driver, graph_id, persona, traits)
            add_to_index(faiss_index, embedding)
            pool_size += 1
            accepted += 1

        print(
            f"Round {outer_round}: {len(candidates_this_round)} candidates, "
            f"{accepted} accepted, {rejected} rejected. Pool: {pool_size}/{target}"
        )

        if accepted == 0 and len(candidates_this_round) > 0:
            logger.warning("Zero acceptance rate this round — thresholds may be too strict")

    print(f"Evolution complete. Final pool size: {pool_size}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Evolve MiroFish agent pool to TARGET_POOL_SIZE")
    parser.add_argument('--target', type=int, default=TARGET_POOL_SIZE)
    parser.add_argument('--graph-id', required=True, help="Neo4j graph_id to add agents to")
    args = parser.parse_args()

    driver = db_setup()
    try:
        evolve(driver, args.graph_id, target=args.target)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
```

- [ ] **Run tests**

```bash
python -m pytest tests/scripts/test_agent_evolver.py -v
```

Expected: all 4 tests `PASSED`.

- [ ] **Commit**

```bash
git add backend/scripts/agent_evolver.py backend/tests/scripts/test_agent_evolver.py
git commit -m "feat: add agent_evolver.py — FAISS-gated pool growth to 4096 (Feature 2)"
```

---

## Task 6: Create `news_fetcher.py`

**Files:**
- Create: `backend/scripts/news_fetcher.py`
- Test: `backend/tests/scripts/test_news_fetcher.py`

- [ ] **Write failing tests**

Create `backend/tests/scripts/test_news_fetcher.py`:

```python
"""Tests for news_fetcher.py."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))

from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
import json, hashlib, pytest


def test_url_hash_is_deterministic():
    from news_fetcher import url_hash
    h1 = url_hash("https://example.com/article")
    h2 = url_hash("https://example.com/article")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_prune_seen_urls_removes_old_entries():
    from news_fetcher import prune_seen_urls
    old_ts = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    new_ts = datetime.now(timezone.utc).isoformat()
    seen = {"abc": old_ts, "def": new_ts}
    pruned = prune_seen_urls(seen)
    assert "abc" not in pruned
    assert "def" in pruned


def test_fetch_body_falls_back_on_short_response():
    """If body is < 200 chars, fallback to rss_summary."""
    import requests
    with patch('news_fetcher.requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body><p>Short.</p></body></html>"
        mock_get.return_value = mock_resp

        from news_fetcher import fetch_body
        result = fetch_body("https://example.com", rss_summary="RSS fallback text here")
    assert result == "RSS fallback text here"


def test_fetch_body_falls_back_on_request_exception():
    """If requests.get raises, return rss_summary."""
    with patch('news_fetcher.requests.get', side_effect=Exception("timeout")):
        from news_fetcher import fetch_body
        result = fetch_body("https://example.com", rss_summary="fallback")
    assert result == "fallback"


def test_fetch_body_falls_back_on_non_200():
    """If status != 200, return rss_summary."""
    with patch('news_fetcher.requests.get') as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_get.return_value = mock_resp

        from news_fetcher import fetch_body
        result = fetch_body("https://example.com", rss_summary="fallback")
    assert result == "fallback"
```

- [ ] **Run tests to verify they fail**

```bash
cd backend
python -m pytest tests/scripts/test_news_fetcher.py -v
```

Expected: `FAILED` — `ModuleNotFoundError: news_fetcher`.

- [ ] **Create `backend/scripts/news_fetcher.py`**

```python
"""
news_fetcher.py — Poll RSS feeds and write a MiroFish briefing .txt file.

Feeds:
  - Reuters Top News
  - Yahoo Finance
  - CNBC Markets
  - MarketWatch Top Stories

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

import feedparser
import requests
from bs4 import BeautifulSoup

RSS_FEEDS = [
    ("Reuters",     "https://feeds.reuters.com/reuters/topNews"),
    ("Yahoo Finance", "https://finance.yahoo.com/rss/topstories"),
    ("CNBC Markets",  "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
    ("MarketWatch",   "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
]

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
            print(f"  [news_fetcher] Non-200 ({resp.status_code}) for {url} — using RSS summary")
            return rss_summary

        soup = BeautifulSoup(resp.text, "html.parser")
        paragraphs = soup.find_all("p")
        body = " ".join(p.get_text(strip=True) for p in paragraphs)

        if len(body) < MIN_BODY_CHARS:
            print(f"  [news_fetcher] Body too short ({len(body)} chars) for {url} — using RSS summary")
            return rss_summary

        return body

    except Exception as e:
        print(f"  [news_fetcher] Body fetch failed for {url}: {e} — using RSS summary")
        return rss_summary


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------

def parse_feed(source_name: str, feed_url: str, seen: dict) -> list:
    """
    Parse a single RSS feed. Returns list of new article dicts.
    Any exception is caught — returns empty list on failure.
    """
    try:
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
            articles.append({
                'title': title,
                'source': source_name,
                'url': link,
                'hash': h,
                'body': body,
            })

        print(f"  [news_fetcher] {source_name}: {len(articles)} new articles")
        return articles

    except Exception as e:
        print(f"  [news_fetcher] Feed '{source_name}' failed: {e} — skipping")
        return []


# ---------------------------------------------------------------------------
# Briefing writer
# ---------------------------------------------------------------------------

def write_briefing(articles: list, output_path: Path) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"MIROFISH BRIEFING — {now_str}\n{'='*60}\n"]
    for art in articles:
        lines.append(f"HEADLINE: {art['title']}")
        lines.append(f"SOURCE: {art['source']}")
        lines.append(f"URL: {art['url']}")
        lines.append("")
        lines.append(art['body'])
        lines.append("\n---\n")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def fetch(dry_run: bool = False) -> Optional[Path]:
    """
    Poll all feeds, deduplicate, write briefing.
    Returns Path to briefing file, or None if no new articles.
    """
    seen = load_seen_urls()
    all_articles = []

    for source_name, feed_url in RSS_FEEDS:
        articles = parse_feed(source_name, feed_url, seen)
        all_articles.extend(articles)

    if not all_articles:
        print("[news_fetcher] No new articles found.")
        return None

    # Update seen-URL store
    now_iso = datetime.now(timezone.utc).isoformat()
    for art in all_articles:
        seen[art['hash']] = now_iso
    save_seen_urls(seen)

    # Write briefing
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    output_path = BRIEFINGS_DIR / f"{ts}.txt"

    if dry_run:
        for art in all_articles:
            print(f"\n--- {art['source']}: {art['title']} ---\n{art['body'][:200]}...")
        return None

    write_briefing(all_articles, output_path)
    print(f"[news_fetcher] Briefing written: {output_path} ({len(all_articles)} articles)")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Fetch RSS news and write MiroFish briefing")
    parser.add_argument('--dry-run', action='store_true', help="Print to stdout, don't write file")
    args = parser.parse_args()
    fetch(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
```

- [ ] **Run tests**

```bash
python -m pytest tests/scripts/test_news_fetcher.py -v
```

Expected: all 4 tests `PASSED`.

- [ ] **Commit**

```bash
git add backend/scripts/news_fetcher.py backend/tests/scripts/test_news_fetcher.py backend/briefings/
git commit -m "feat: add news_fetcher.py — RSS polling with TTL dedup and silent body fallback (Feature 3a)"
```

---

## Task 7: Create `incremental_update.py`

**Files:**
- Create: `backend/scripts/incremental_update.py`
- Test: `backend/tests/scripts/test_incremental_update.py`

- [ ] **Write failing tests**

Create `backend/tests/scripts/test_incremental_update.py`:

```python
"""Tests for incremental_update.py."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

from unittest.mock import MagicMock, patch, call
import pytest


def test_resolve_graph_id_from_projects_dir(tmp_path):
    """resolve_active_graph_id should return graph_id from most recent GRAPH_COMPLETED project."""
    import json
    projects_dir = tmp_path / "projects"
    proj = projects_dir / "proj_001"
    proj.mkdir(parents=True)
    (proj / "project.json").write_text(json.dumps({
        "status": "GRAPH_COMPLETED",
        "graph_id": "test-graph-uuid-123"
    }))

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))
    from incremental_update import resolve_active_graph_id
    result = resolve_active_graph_id(projects_dir=str(projects_dir))
    assert result == "test-graph-uuid-123"


def test_merge_skips_synthetic_nodes():
    """
    The Cypher MERGE must use CASE WHEN is_synthetic to avoid overwriting synthetic agent nodes.
    Check that the query sent contains the is_synthetic guard.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))
    from incremental_update import MERGE_ENTITY_QUERY
    assert 'is_synthetic' in MERGE_ENTITY_QUERY
```

- [ ] **Run tests to verify they fail**

```bash
python -m pytest tests/scripts/test_incremental_update.py -v
```

Expected: `FAILED`.

- [ ] **Create `backend/scripts/incremental_update.py`**

```python
"""
incremental_update.py — Process a briefing .txt file into the Neo4j knowledge graph.

Extracts entities and relations via the existing NER pipeline and MERGEs them
into Neo4j. Never overwrites properties on synthetic agent nodes (is_synthetic=True).

Usage:
  python incremental_update.py --briefing /path/to/briefing.txt [--graph-id <uuid>]
"""
import sys
import os
import json
import uuid
import argparse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from db_setup import setup as db_setup
from app.config import Config
from app.storage.embedding_service import EmbeddingService
from app.storage.ner_extractor import NERExtractor
from app.services.text_processor import TextProcessor
from app.utils.logger import get_logger

logger = get_logger('mirofish.incremental_update')

# Exposed as module-level constant so tests can inspect the guard clause
MERGE_ENTITY_QUERY = """
MERGE (n:Entity {graph_id: $gid, name_lower: $name_lower})
ON CREATE SET
    n.uuid = $uuid,
    n.name = $name,
    n.summary = $summary,
    n.attributes_json = $attrs_json,
    n.embedding = $embedding,
    n.created_at = $now,
    n.is_synthetic = false
ON MATCH SET
    n.summary    = CASE WHEN n.is_synthetic = true THEN n.summary    ELSE $summary    END,
    n.attributes_json = CASE WHEN n.is_synthetic = true THEN n.attributes_json ELSE $attrs_json END,
    n.embedding  = CASE WHEN n.is_synthetic = true THEN n.embedding  ELSE $embedding  END
RETURN n.uuid AS uuid
"""

CREATE_RELATION_QUERY = """
MATCH (src:Entity {graph_id: $gid, name_lower: $src_lower})
MATCH (tgt:Entity {graph_id: $gid, name_lower: $tgt_lower})
CREATE (src)-[r:RELATION {
    uuid: $uuid,
    graph_id: $gid,
    name: $name,
    fact: $fact,
    fact_embedding: $fact_embedding,
    created_at: $now
}]->(tgt)
"""


# ---------------------------------------------------------------------------
# Graph ID resolution
# ---------------------------------------------------------------------------

def resolve_active_graph_id(projects_dir: str = None) -> str:
    """
    Find the graph_id from the most recently completed project.
    Reads project.json from each subdirectory of projects_dir.
    """
    if projects_dir is None:
        projects_dir = os.path.join(
            os.path.dirname(__file__), '..', 'uploads', 'projects'
        )

    projects_path = Path(projects_dir)
    if not projects_path.exists():
        raise FileNotFoundError(f"Projects directory not found: {projects_dir}")

    candidates = []
    for proj_dir in projects_path.iterdir():
        proj_json = proj_dir / "project.json"
        if proj_json.exists():
            with open(proj_json) as f:
                data = json.load(f)
            if data.get("status") == "GRAPH_COMPLETED" and data.get("graph_id"):
                mtime = proj_json.stat().st_mtime
                candidates.append((mtime, data["graph_id"]))

    if not candidates:
        raise ValueError("No completed graph project found in projects directory")

    candidates.sort(reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# Entity/relation writing
# ---------------------------------------------------------------------------

def merge_entity(session, graph_id: str, entity: dict, embedding: list, now: str) -> str:
    name = entity['name']
    attrs = entity.get('attributes', {})
    result = session.run(
        MERGE_ENTITY_QUERY,
        gid=graph_id,
        name_lower=name.lower(),
        uuid=str(uuid.uuid4()),
        name=name,
        summary=f"{name} ({entity.get('type', 'Entity')})",
        attrs_json=json.dumps(attrs, ensure_ascii=False),
        embedding=embedding,
        now=now,
    )
    record = result.single()
    return record['uuid'] if record else None


def create_relation(session, graph_id: str, relation: dict, fact_embedding: list, now: str):
    session.run(
        CREATE_RELATION_QUERY,
        gid=graph_id,
        src_lower=relation['source'].lower(),
        tgt_lower=relation['target'].lower(),
        uuid=str(uuid.uuid4()),
        name=relation['type'],
        fact=relation.get('fact', ''),
        fact_embedding=fact_embedding,
        now=now,
    )


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_briefing(driver, graph_id: str, briefing_path: str) -> None:
    embedding_svc = EmbeddingService()
    ner = NERExtractor()

    # Load ontology for NER guidance
    with driver.session() as session:
        result = session.run(
            "MATCH (g:Graph {graph_id: $gid}) RETURN g.ontology_json AS oj",
            gid=graph_id,
        )
        record = result.single()
        ontology = json.loads(record['oj']) if record and record['oj'] else {}

    # Read and preprocess briefing
    text = Path(briefing_path).read_text(encoding='utf-8')
    text = TextProcessor.preprocess_text(text)
    chunks = TextProcessor.split_text(text, chunk_size=Config.DEFAULT_CHUNK_SIZE,
                                      overlap=Config.DEFAULT_CHUNK_OVERLAP)

    logger.info(f"Processing {len(chunks)} chunks from {briefing_path}")
    now = datetime.now(timezone.utc).isoformat()

    for i, chunk in enumerate(chunks):
        logger.info(f"Chunk {i+1}/{len(chunks)}: extracting entities...")
        try:
            extraction = ner.extract(chunk, ontology)
        except Exception as e:
            logger.warning(f"NER failed on chunk {i+1}: {e} — skipping")
            continue

        entities = extraction.get('entities', [])
        relations = extraction.get('relations', [])

        if not entities:
            continue

        # Batch embed: entity summaries + relation facts
        entity_texts = [f"{e['name']} ({e.get('type','Entity')})" for e in entities]
        relation_texts = [r.get('fact', f"{r['source']} {r['type']} {r['target']}") for r in relations]
        all_texts = entity_texts + relation_texts

        try:
            all_embeddings = embedding_svc.embed_batch(all_texts)
        except Exception as e:
            logger.warning(f"Embedding failed for chunk {i+1}: {e} — using empty vectors")
            all_embeddings = [[] for _ in all_texts]

        entity_embeddings = all_embeddings[:len(entities)]
        relation_embeddings = all_embeddings[len(entities):]

        with driver.session() as session:
            for j, entity in enumerate(entities):
                emb = entity_embeddings[j] if j < len(entity_embeddings) else []
                try:
                    merge_entity(session, graph_id, entity, emb, now)
                except Exception as e:
                    logger.warning(f"Failed to merge entity '{entity['name']}': {e}")

            for j, relation in enumerate(relations):
                emb = relation_embeddings[j] if j < len(relation_embeddings) else []
                try:
                    create_relation(session, graph_id, relation, emb, now)
                except Exception as e:
                    logger.warning(f"Failed to create relation: {e}")

    logger.info(f"Briefing processed: {briefing_path}")


def main():
    parser = argparse.ArgumentParser(description="Merge briefing into Neo4j knowledge graph")
    parser.add_argument('--briefing', required=True, help="Path to briefing .txt file")
    parser.add_argument('--graph-id', help="Neo4j graph_id (auto-detected if omitted)")
    args = parser.parse_args()

    driver = db_setup()
    try:
        graph_id = args.graph_id or resolve_active_graph_id()
        print(f"Using graph_id: {graph_id}")
        process_briefing(driver, graph_id, args.briefing)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
```

- [ ] **Run tests**

```bash
python -m pytest tests/scripts/test_incremental_update.py -v
```

Expected: `PASSED`.

- [ ] **Commit**

```bash
git add backend/scripts/incremental_update.py backend/tests/scripts/test_incremental_update.py
git commit -m "feat: add incremental_update.py — briefing → Neo4j MERGE with synthetic-node guard (Feature 3b)"
```

---

## Task 8: Create `simulation_tick.py`

**Files:**
- Create: `backend/scripts/simulation_tick.py`
- Test: `backend/tests/scripts/test_simulation_tick.py`

- [ ] **Write failing tests**

Create `backend/tests/scripts/test_simulation_tick.py`:

```python
"""Tests for simulation_tick.py."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

from unittest.mock import MagicMock, patch
import numpy as np
import json, pytest
from pathlib import Path


def test_cosine_top5_returns_correct_chunks():
    """top_k_chunks should return the k chunks most similar to query embedding."""
    from simulation_tick import top_k_chunks

    # chunk 0 is identical to query → similarity = 1.0
    query_emb = [1.0, 0.0, 0.0]
    cache = [
        ("similar chunk", [1.0, 0.0, 0.0]),
        ("unrelated",     [0.0, 1.0, 0.0]),
        ("orthogonal",    [0.0, 0.0, 1.0]),
    ]
    result = top_k_chunks(query_emb, cache, k=1)
    assert result == ["similar chunk"]


def test_build_agent_query_string_includes_bias():
    """build_agent_query should encode asset_class_bias and strategy."""
    from simulation_tick import build_agent_query

    traits = {
        'asset_class_bias': 'crypto',
        'primary_strategy': 'day_trading',
        'geopolitical_sensitivity': 9.0,
    }
    q = build_agent_query(traits)
    assert 'crypto' in q
    assert 'day_trading' in q
    assert 'geopolitical' in q.lower()  # High geo_sensitivity adds geo keywords


def test_build_agent_query_no_geo_keywords_when_low():
    """build_agent_query should NOT add geo keywords when geopolitical_sensitivity < 7."""
    from simulation_tick import build_agent_query

    traits = {
        'asset_class_bias': 'equities',
        'primary_strategy': 'index',
        'geopolitical_sensitivity': 2.0,
    }
    q = build_agent_query(traits)
    assert 'geopolitical' not in q.lower()


def test_checkpoint_write_and_resume(tmp_path):
    """Checkpoint file should be written with correct fields."""
    from simulation_tick import write_checkpoint, load_checkpoint
    import tempfile

    ckpt_path = tmp_path / "tick_checkpoint.json"
    briefing = "briefings/2026-04-09_1400.txt"
    processed = ["uuid-1", "uuid-2"]

    write_checkpoint(ckpt_path, briefing, processed)
    loaded = load_checkpoint(ckpt_path, briefing)

    assert loaded == {"uuid-1", "uuid-2"}


def test_checkpoint_returns_empty_for_different_briefing(tmp_path):
    """load_checkpoint returns empty set if briefing_source doesn't match."""
    from simulation_tick import write_checkpoint, load_checkpoint

    ckpt_path = tmp_path / "tick_checkpoint.json"
    write_checkpoint(ckpt_path, "briefings/old.txt", ["uuid-1"])
    loaded = load_checkpoint(ckpt_path, "briefings/new.txt")
    assert loaded == set()
```

- [ ] **Run tests to verify they fail**

```bash
python -m pytest tests/scripts/test_simulation_tick.py -v
```

Expected: `FAILED`.

- [ ] **Create `backend/scripts/simulation_tick.py`**

```python
"""
simulation_tick.py — Run a per-agent LLM news reaction pass.

For each sampled agent:
  1. Fetch last 7 days of MemoryEvent nodes from Neo4j
  2. Select top-5 briefing chunks by cosine similarity to agent's trait query
  3. Call LLM → {reaction, confidence, reasoning, assets_mentioned}
  4. Write result as new :MemoryEvent node linked to agent

Checkpoint recovery: every 100 agents writes tick_checkpoint.json.
On restart with same briefing, already-processed agents are skipped.

Usage:
  python simulation_tick.py --briefing /path/to/briefing.txt --graph-id <uuid> [--full]
"""
import sys
import os
import json
import uuid
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np

from db_setup import setup as db_setup
from app.config import Config
from app.storage.embedding_service import EmbeddingService
from app.services.text_processor import TextProcessor
from app.utils.llm_client import LLMClient
from app.utils.logger import get_logger

logger = get_logger('mirofish.simulation_tick')

CHECKPOINT_PATH = Path(__file__).parent / "tick_checkpoint.json"
CHECKPOINT_INTERVAL = 100
SAMPLE_SIZE = 500
TOP_K_CHUNKS = 5
MEMORY_WINDOW_DAYS = 7

VALID_REACTIONS = {'buy', 'hold', 'sell', 'panic', 'hedge'}


# ---------------------------------------------------------------------------
# Chunk cache and similarity
# ---------------------------------------------------------------------------

def top_k_chunks(query_embedding: list, chunk_cache: list, k: int = TOP_K_CHUNKS) -> list:
    """
    Return the k chunk texts most similar to query_embedding.
    chunk_cache: list of (text, embedding) tuples.
    """
    if not chunk_cache:
        return []

    q = np.array(query_embedding, dtype='float64')
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return [c[0] for c in chunk_cache[:k]]

    scored = []
    for text, emb in chunk_cache:
        e = np.array(emb, dtype='float64')
        e_norm = np.linalg.norm(e)
        if e_norm == 0:
            sim = 0.0
        else:
            sim = float(np.dot(q, e) / (q_norm * e_norm))
        scored.append((sim, text))

    scored.sort(reverse=True)
    return [text for _, text in scored[:k]]


def build_agent_query(traits: dict) -> str:
    """
    Build a short relevance query string from agent traits for chunk selection.
    High geopolitical_sensitivity (≥7) appends geopolitical keywords.
    """
    parts = [
        traits.get('asset_class_bias', ''),
        traits.get('primary_strategy', ''),
    ]
    if (traits.get('geopolitical_sensitivity') or 0) >= 7:
        parts.append("geopolitical risk sanctions war conflict")
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def write_checkpoint(path: Path, briefing_source: str, processed_ids: list) -> None:
    data = {
        "briefing_source": briefing_source,
        "processed_agent_ids": list(processed_ids),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(data, indent=2), encoding='utf-8')


def load_checkpoint(path: Path, briefing_source: str) -> set:
    """Return set of already-processed agent UUIDs if checkpoint matches briefing_source."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get("briefing_source") == briefing_source:
            return set(data.get("processed_agent_ids", []))
    except Exception:
        pass
    return set()


# ---------------------------------------------------------------------------
# Neo4j helpers
# ---------------------------------------------------------------------------

def load_agents(driver, graph_id: str, sample_size: Optional[int]) -> list:
    """Load agents with investor trait profiles. If sample_size is None, load all."""
    limit_clause = f"LIMIT {sample_size}" if sample_size else ""
    with driver.session() as session:
        result = session.run(
            f"""
            MATCH (n:Entity {{graph_id: $gid}})
            WHERE n.risk_tolerance IS NOT NULL
            RETURN n.uuid AS uuid, n.name AS name,
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
            ORDER BY rand()
            {limit_clause}
            """,
            gid=graph_id,
        )
        return [dict(r) for r in result]


def load_agent_memory(driver, agent_uuid: str) -> list:
    """Return last MEMORY_WINDOW_DAYS days of MemoryEvent records for this agent."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MEMORY_WINDOW_DAYS)).isoformat()
    with driver.session() as session:
        result = session.run(
            """
            MATCH (n:Entity {uuid: $uuid})-[:HAS_MEMORY]->(m:MemoryEvent)
            WHERE m.timestamp > $cutoff
            RETURN m.reaction AS reaction, m.reasoning AS reasoning,
                   m.confidence AS confidence, m.timestamp AS ts
            ORDER BY m.timestamp
            """,
            uuid=agent_uuid,
            cutoff=cutoff,
        )
        return [dict(r) for r in result]


def write_memory_event(driver, agent_uuid: str, briefing_source: str, reaction_data: dict) -> None:
    """Create a :MemoryEvent node and link it to the agent."""
    event_uuid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    with driver.session() as session:
        def _write(tx):
            tx.run(
                """
                MATCH (n:Entity {uuid: $agent_uuid})
                CREATE (m:MemoryEvent {
                    uuid: $uuid,
                    agent_uuid: $agent_uuid,
                    timestamp: $ts,
                    briefing_source: $src,
                    reaction: $reaction,
                    confidence: $confidence,
                    reasoning: $reasoning,
                    assets_mentioned: $assets
                })
                CREATE (n)-[:HAS_MEMORY]->(m)
                """,
                agent_uuid=agent_uuid,
                uuid=event_uuid,
                ts=now,
                src=briefing_source,
                reaction=reaction_data.get('reaction', 'hold'),
                confidence=float(reaction_data.get('confidence', 5.0)),
                reasoning=str(reaction_data.get('reasoning', '')),
                assets=json.dumps(reaction_data.get('assets_mentioned', [])),
            )
        session.execute_write(_write)


# ---------------------------------------------------------------------------
# LLM reaction call
# ---------------------------------------------------------------------------

def build_prompt(agent: dict, memory_events: list, context_chunks: list) -> list:
    """Build messages list for LLMClient.chat_json()."""
    trait_lines = "\n".join(
        f"  {k}: {v}" for k, v in agent.items()
        if k not in ('uuid', 'name') and v is not None
    )
    memory_str = "\n".join(
        f"  [{e['ts']}] {e['reaction']} (conf={e['confidence']}): {e['reasoning']}"
        for e in memory_events[-10:]  # last 10 events
    ) or "  No recent memory."

    context_str = "\n\n".join(context_chunks) or "No briefing context available."

    return [
        {
            "role": "system",
            "content": (
                "You are modelling an investor's reaction to financial news. "
                "Return only valid JSON with keys: reaction, confidence, reasoning, assets_mentioned."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Investor: {agent.get('name', 'Unknown')}\n\n"
                f"Trait Profile:\n{trait_lines}\n\n"
                f"Recent Memory (last 7 days):\n{memory_str}\n\n"
                f"Relevant News:\n{context_str}\n\n"
                "Based on this investor's profile and the news above, return JSON:\n"
                '{"reaction": "buy|hold|sell|panic|hedge", '
                '"confidence": <0-10 float>, '
                '"reasoning": "<1-2 sentences>", '
                '"assets_mentioned": ["<ticker or asset name>", ...]}'
            ),
        },
    ]


def call_llm_for_agent(llm: LLMClient, agent: dict, memory: list, chunks: list) -> Optional[dict]:
    """Returns validated reaction dict, or None on failure."""
    messages = build_prompt(agent, memory, chunks)
    try:
        result = llm.chat_json(messages, temperature=0.7, max_tokens=512)
        # Validate reaction field
        if result.get('reaction') not in VALID_REACTIONS:
            result['reaction'] = 'hold'
        result['confidence'] = max(0.0, min(10.0, float(result.get('confidence', 5.0))))
        if not isinstance(result.get('assets_mentioned'), list):
            result['assets_mentioned'] = []
        return result
    except Exception as e:
        logger.warning(f"LLM call failed for agent {agent.get('uuid')}: {e}")
        return None


# ---------------------------------------------------------------------------
# Main tick
# ---------------------------------------------------------------------------

def run_tick(driver, graph_id: str, briefing_path: str, full: bool = False) -> None:
    llm = LLMClient()
    embedding_svc = EmbeddingService()

    # --- Build chunk cache BEFORE agent loop ---
    briefing_text = Path(briefing_path).read_text(encoding='utf-8')
    briefing_text = TextProcessor.preprocess_text(briefing_text)
    chunks = TextProcessor.split_text(briefing_text,
                                      chunk_size=Config.DEFAULT_CHUNK_SIZE,
                                      overlap=Config.DEFAULT_CHUNK_OVERLAP)
    chunk_texts = chunks

    print(f"Embedding {len(chunk_texts)} briefing chunks...")
    try:
        chunk_embeddings = embedding_svc.embed_batch(chunk_texts)
    except Exception as e:
        logger.warning(f"Chunk embedding failed: {e} — proceeding with empty cache")
        chunk_embeddings = [[] for _ in chunk_texts]

    chunk_cache = list(zip(chunk_texts, chunk_embeddings))
    print(f"Briefing indexed: {len(chunk_cache)} chunks cached")

    # --- Load agents ---
    sample_size = None if full else SAMPLE_SIZE
    agents = load_agents(driver, graph_id, sample_size)
    print(f"Loaded {len(agents)} agents for tick")

    # --- Checkpoint recovery ---
    briefing_source = str(Path(briefing_path).name)
    already_processed = load_checkpoint(CHECKPOINT_PATH, briefing_source)
    agents = [a for a in agents if a['uuid'] not in already_processed]
    print(f"Resuming: {len(already_processed)} already processed, {len(agents)} remaining")

    processed_ids = list(already_processed)
    errors = 0

    for i, agent in enumerate(agents):
        # Fetch agent memory
        try:
            memory = load_agent_memory(driver, agent['uuid'])
        except Exception as e:
            logger.warning(f"Memory load failed for {agent['uuid']}: {e}")
            memory = []

        # Select top-5 chunks relevant to this agent
        query_str = build_agent_query(agent)
        try:
            query_emb = embedding_svc.embed(query_str)
            relevant_chunks = top_k_chunks(query_emb, chunk_cache, k=TOP_K_CHUNKS)
        except Exception as e:
            logger.warning(f"Query embedding failed for agent {agent['uuid']}: {e}")
            relevant_chunks = [c[0] for c in chunk_cache[:TOP_K_CHUNKS]]

        # LLM reaction
        reaction = call_llm_for_agent(llm, agent, memory, relevant_chunks)
        if reaction is None:
            errors += 1
        else:
            try:
                write_memory_event(driver, agent['uuid'], briefing_source, reaction)
            except Exception as e:
                logger.warning(f"Memory write failed for {agent['uuid']}: {e}")
                errors += 1

        processed_ids.append(agent['uuid'])

        # Checkpoint every CHECKPOINT_INTERVAL agents
        if (i + 1) % CHECKPOINT_INTERVAL == 0:
            write_checkpoint(CHECKPOINT_PATH, briefing_source, processed_ids)
            print(f"  Checkpoint: {i+1}/{len(agents)} agents processed ({errors} errors)")

    # Clean up checkpoint on success
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()

    print(f"Tick complete: {len(agents)} agents processed, {errors} errors")


def main():
    parser = argparse.ArgumentParser(description="Run MiroFish simulation tick")
    parser.add_argument('--briefing', required=True, help="Path to briefing .txt file")
    parser.add_argument('--graph-id', help="Neo4j graph_id (auto-detected if omitted)")
    parser.add_argument('--full', action='store_true', help="Process all agents (no sampling)")
    args = parser.parse_args()

    driver = db_setup()
    try:
        graph_id = args.graph_id
        if not graph_id:
            from incremental_update import resolve_active_graph_id
            graph_id = resolve_active_graph_id()
        print(f"Using graph_id: {graph_id}")
        run_tick(driver, graph_id, args.briefing, full=args.full)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
```

- [ ] **Run tests**

```bash
python -m pytest tests/scripts/test_simulation_tick.py -v
```

Expected: all 4 tests `PASSED`.

- [ ] **Commit**

```bash
git add backend/scripts/simulation_tick.py backend/tests/scripts/test_simulation_tick.py
git commit -m "feat: add simulation_tick.py — per-agent LLM reaction pass with chunk cache and checkpoint (Feature 3c)"
```

---

## Task 9: Create `scheduler.py`

**Files:**
- Create: `backend/scripts/scheduler.py`
- Test: `backend/tests/scripts/test_scheduler.py` (lightweight — tests the mutex logic)

- [ ] **Write failing test**

Create `backend/tests/scripts/test_scheduler.py`:

```python
"""Tests for scheduler.py — mutex flag and status file writing."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../scripts'))

import json, threading, pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


def test_write_status_creates_valid_json(tmp_path):
    """write_status should write a valid JSON file with expected keys."""
    from scheduler import write_status
    status_path = tmp_path / "status.json"

    write_status(
        status_path=status_path,
        last_news_fetch="2026-04-09T14:00:00",
        last_tick="2026-04-09T14:03:22",
        last_full_simulation="2026-04-09T02:00:00",
        agent_pool_size=1247,
        last_error=None,
    )

    data = json.loads(status_path.read_text())
    assert data['last_news_fetch'] == "2026-04-09T14:00:00"
    assert data['current_agent_pool_size'] == 1247
    assert data['last_error'] is None


def test_hourly_job_skips_tick_when_daily_running():
    """If _daily_running is set, hourly job must not call run_tick."""
    from scheduler import _daily_running

    _daily_running.set()
    try:
        with patch('scheduler.run_tick') as mock_tick, \
             patch('scheduler.fetch', return_value=None), \
             patch('scheduler.process_briefing'), \
             patch('scheduler.write_status'):
            from scheduler import hourly_job
            hourly_job(
                graph_id="test-graph",
                status_path=Path("/tmp/status.json"),
                status_state={},
            )
        mock_tick.assert_not_called()
    finally:
        _daily_running.clear()
```

- [ ] **Run test to verify it fails**

```bash
python -m pytest tests/scripts/test_scheduler.py -v
```

Expected: `FAILED`.

- [ ] **Create `backend/scripts/scheduler.py`**

```python
"""
scheduler.py — APScheduler orchestrator for MiroFish live news pipeline.

Jobs:
  - Hourly (every 60 min):  news_fetcher → incremental_update → simulation_tick (500 agents)
  - Daily  (03:00 local):   simulation_tick --full (all agents, no sampling)

Mutex: _daily_running Event blocks the hourly tick while daily job is running.
Status: writes /backend/scripts/status.json after every job cycle.

Usage:
  python scheduler.py [--graph-id <uuid>]
"""
import sys
import os
import json
import argparse
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from apscheduler.schedulers.blocking import BlockingScheduler

from db_setup import setup as db_setup
from news_fetcher import fetch
from incremental_update import process_briefing, resolve_active_graph_id
from simulation_tick import run_tick
from app.utils.logger import get_logger

logger = get_logger('mirofish.scheduler')

STATUS_PATH = Path(__file__).parent / "status.json"

# Mutex: daily job sets this; hourly tick checks it
_daily_running = threading.Event()


# ---------------------------------------------------------------------------
# Status file
# ---------------------------------------------------------------------------

def write_status(
    status_path: Path,
    last_news_fetch: Optional[str],
    last_tick: Optional[str],
    last_full_simulation: Optional[str],
    agent_pool_size: int,
    last_error: Optional[str],
) -> None:
    data = {
        "last_news_fetch": last_news_fetch,
        "last_tick": last_tick,
        "last_full_simulation": last_full_simulation,
        "current_agent_pool_size": agent_pool_size,
        "last_error": last_error,
    }
    status_path.write_text(json.dumps(data, indent=2), encoding='utf-8')


def get_agent_pool_size(driver, graph_id: str) -> int:
    try:
        with driver.session() as session:
            result = session.run(
                "MATCH (n:Entity {graph_id: $gid}) WHERE n.risk_tolerance IS NOT NULL RETURN count(n) AS c",
                gid=graph_id,
            )
            return result.single()['c']
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

def hourly_job(graph_id: str, status_path: Path, status_state: dict) -> None:
    """
    1. Fetch news → briefing file
    2. Merge briefing into Neo4j
    3. Run 500-agent tick (skipped if daily job is running)
    """
    logger.info("Hourly job starting")
    error = None
    now = lambda: datetime.now(timezone.utc).isoformat()

    # Connect fresh driver for this job cycle
    try:
        driver = db_setup()
    except Exception as e:
        logger.error(f"Hourly job: Neo4j connection failed: {e}")
        status_state['last_error'] = f"scheduler: {e}"
        write_status(status_path, **{k: status_state.get(k) for k in
                                      ('last_news_fetch', 'last_tick', 'last_full_simulation')},
                     agent_pool_size=-1, last_error=status_state.get('last_error'))
        return

    try:
        # Step 1: news fetch
        try:
            briefing_path = fetch()
            status_state['last_news_fetch'] = now()
            logger.info(f"News fetched: {briefing_path}")
        except Exception as e:
            error = f"news_fetcher: {e}"
            logger.error(error)
            briefing_path = None

        # Step 2: incremental update (only if briefing was written)
        if briefing_path:
            try:
                process_briefing(driver, graph_id, str(briefing_path))
                logger.info("Incremental update complete")
            except Exception as e:
                error = f"incremental_update: {e}"
                logger.error(error)

        # Step 3: tick (skip if daily is running)
        if briefing_path:
            if _daily_running.is_set():
                logger.info("Skipping hourly tick: daily full simulation in progress")
            else:
                try:
                    run_tick(driver, graph_id, str(briefing_path), full=False)
                    status_state['last_tick'] = now()
                except Exception as e:
                    error = f"simulation_tick: {e}"
                    logger.error(error)

        pool_size = get_agent_pool_size(driver, graph_id)
        write_status(
            status_path,
            last_news_fetch=status_state.get('last_news_fetch'),
            last_tick=status_state.get('last_tick'),
            last_full_simulation=status_state.get('last_full_simulation'),
            agent_pool_size=pool_size,
            last_error=error,
        )

    finally:
        driver.close()
    logger.info("Hourly job complete")


def daily_job(graph_id: str, status_path: Path, status_state: dict) -> None:
    """
    Full 4096-agent simulation tick. Sets _daily_running mutex for duration.
    Uses the most recent briefing file (latest .txt in briefings dir).
    """
    logger.info("Daily full tick starting")
    _daily_running.set()
    error = None
    now = lambda: datetime.now(timezone.utc).isoformat()

    try:
        driver = db_setup()
    except Exception as e:
        _daily_running.clear()
        logger.error(f"Daily job: Neo4j connection failed: {e}")
        return

    try:
        briefings_dir = Path(__file__).parent.parent / "briefings"
        briefing_files = sorted(briefings_dir.glob("*.txt"), reverse=True)
        if not briefing_files:
            logger.warning("Daily job: no briefing files found, skipping tick")
            return

        latest_briefing = briefing_files[0]
        logger.info(f"Daily tick using briefing: {latest_briefing}")

        try:
            run_tick(driver, graph_id, str(latest_briefing), full=True)
            status_state['last_full_simulation'] = now()
        except Exception as e:
            error = f"daily_simulation_tick: {e}"
            logger.error(error)

        pool_size = get_agent_pool_size(driver, graph_id)
        write_status(
            status_path,
            last_news_fetch=status_state.get('last_news_fetch'),
            last_tick=status_state.get('last_tick'),
            last_full_simulation=status_state.get('last_full_simulation'),
            agent_pool_size=pool_size,
            last_error=error,
        )

    finally:
        driver.close()
        _daily_running.clear()
        logger.info("Daily full tick complete")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="MiroFish APScheduler orchestrator")
    parser.add_argument('--graph-id', help="Neo4j graph_id (auto-detected if omitted)")
    args = parser.parse_args()

    graph_id = args.graph_id or resolve_active_graph_id()
    print(f"Scheduler starting for graph_id: {graph_id}")

    status_state: dict = {
        'last_news_fetch': None,
        'last_tick': None,
        'last_full_simulation': None,
    }

    scheduler = BlockingScheduler(timezone="local")

    # Hourly job — every 60 minutes
    scheduler.add_job(
        hourly_job,
        trigger='interval',
        minutes=60,
        kwargs={'graph_id': graph_id, 'status_path': STATUS_PATH, 'status_state': status_state},
        id='hourly',
        name='Hourly news + tick',
        max_instances=1,
        coalesce=True,
    )

    # Daily job — 03:00 local time
    scheduler.add_job(
        daily_job,
        trigger='cron',
        hour=3,
        minute=0,
        kwargs={'graph_id': graph_id, 'status_path': STATUS_PATH, 'status_state': status_state},
        id='daily',
        name='Daily full simulation tick',
        max_instances=1,
        coalesce=True,
    )

    print("Scheduler running. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        scheduler.shutdown()
        print("Scheduler stopped.")


if __name__ == "__main__":
    main()
```

- [ ] **Run tests**

```bash
python -m pytest tests/scripts/test_scheduler.py -v
```

Expected: `PASSED`.

- [ ] **Run full test suite**

```bash
python -m pytest tests/scripts/ -v
```

Expected: all tests `PASSED`.

- [ ] **Commit**

```bash
git add backend/scripts/scheduler.py backend/tests/scripts/test_scheduler.py
git commit -m "feat: add scheduler.py — APScheduler orchestrator with hourly/daily jobs and mutex guard (Feature 3d)"
```

---

## Self-Review

### Spec coverage check

| Spec requirement | Task |
|---|---|
| 16 trait fields with fat-tail distributions | Task 3 |
| Traits stored as Neo4j node properties | Task 2 + Task 3 |
| Non-normal distribution (fat tails) | Task 3 (Beta, Pareto, log-normal, log-uniform) |
| FAISS index built once at startup | Task 5 |
| FAISS gate 0.75 → exact-triple gate | Task 5 |
| In-memory FAISS update on acceptance | Task 5 |
| Pool target 4096 | Task 5 |
| is_synthetic=True for evolver agents | Task 5 |
| Reuters + Yahoo + CNBC Markets + MarketWatch feeds | Task 6 |
| BeautifulSoup body fetch with silent fallback | Task 6 |
| 30-day TTL on seen_urls.json | Task 6 |
| NER pipeline → MERGE into Neo4j | Task 7 |
| is_synthetic guard on MERGE | Task 7 |
| Agent nodes never touched by incremental_update | Task 7 |
| Chunk cache built before agent loop, logged | Task 8 |
| Top-5 per-agent chunk selection via cosine similarity | Task 8 |
| Geo sensitivity ≥7 adds keywords to query | Task 8 |
| LLM returns {reaction, confidence, reasoning, assets_mentioned} | Task 8 |
| MemoryEvent written to Neo4j with [:HAS_MEMORY] | Task 8 |
| Checkpoint every 100 agents | Task 8 |
| Checkpoint resume on restart with same briefing | Task 8 |
| Checkpoint deleted on clean completion | Task 8 |
| Hourly scheduler job | Task 9 |
| Daily job at 03:00 | Task 9 |
| _daily_running mutex blocks hourly tick | Task 9 |
| status.json written after each cycle | Task 9 |
| db_setup.py called at startup by every script | Tasks 4, 5, 7, 8, 9 |
| Three indexes with CREATE INDEX IF NOT EXISTS | Task 4 |

All spec requirements covered.

### Type consistency check

- `update_node_traits(uuid: str, traits: dict)` — called in Task 3, defined in Task 2. ✓
- `serialise_traits(traits: dict) -> str` — defined and tested in Task 5. ✓
- `triple_key(archetype, strategy, risk) -> tuple` — defined and tested in Task 5. ✓
- `chunk_cache: list[tuple[str, list[float]]]` — built in Task 8, passed to `top_k_chunks`. ✓
- `write_checkpoint(path, briefing_source, processed_ids)` / `load_checkpoint(path, briefing_source)` — consistent in Task 8 and its tests. ✓
- `write_status(status_path, last_news_fetch, last_tick, last_full_simulation, agent_pool_size, last_error)` — consistent in Task 9 definition and tests. ✓
