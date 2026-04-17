"""
scheduler.py — APScheduler orchestrator for MiroFish live news pipeline.

Jobs:
  - Half-hourly (every 30 min):  news_fetcher → incremental_update → simulation_tick (500 agents)
  - Daily  (02:00 UTC):          simulation_tick --full (all agents, no sampling)
                                  Starts 2am UTC, finishes ~12pm UTC — well before US market open at 13:30 UTC.
  - Weekly (Sunday 04:00 UTC):   MemoryEvent TTL pruning — DETACH DELETE events older than 90 days.

Mutex: _daily_running Event blocks the half-hourly tick while daily job is running.
Status: writes /backend/scripts/status.json after every job cycle.
Log: appends to /backend/logs/scheduler_runs.json after every job completes.
Catch-up: on startup and after every job, re-checks for overdue jobs:
  - daily: if past 02:00 UTC and not yet run today (UTC date)
  - half-hourly: if last completed run > 30 min ago (or no record exists)
  Daily always runs before half-hourly if both are overdue.

Ollama health check: before each tick (both 30-min and daily), verifies that
  Ollama is reachable and qwen2.5:14b is loaded. If unavailable or loading,
  waits up to 60 seconds (10-second polls). Skips the tick if still unavailable.

Usage:
  python scheduler.py [--graph-id <uuid>]
"""
import sys
import os
import json
import time
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
from price_fetcher import fetch_prices
from app.utils.logger import get_logger

logger = get_logger('mirofish.scheduler')

STATUS_PATH = Path(__file__).parent / "status.json"
LOGS_DIR    = Path(__file__).parent.parent / "logs"
LOGS_PATH   = LOGS_DIR / "scheduler_runs.json"
MAX_LOG_ENTRIES = 10_000

# Mutex: daily job sets this; half-hourly tick checks it
_daily_running = threading.Event()
# Re-entrance guard: prevents run_catchup from recursively invoking itself
_catchup_running = threading.Event()


# ---------------------------------------------------------------------------
# Run log helpers
# ---------------------------------------------------------------------------

def _ensure_logs_dir() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def append_run_log(
    job_type: str,
    started_at: str,
    finished_at: str,
    status: str,
    error: Optional[str],
) -> None:
    """Append a job-completion entry to scheduler_runs.json.
    Capped at MAX_LOG_ENTRIES (oldest trimmed). Never overwrites — permanent audit trail.
    """
    _ensure_logs_dir()
    entry = {
        "job_type": job_type,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "error": error,
    }
    try:
        if LOGS_PATH.exists():
            entries = json.loads(LOGS_PATH.read_text(encoding='utf-8'))
            if not isinstance(entries, list):
                entries = []
        else:
            entries = []
        entries.append(entry)
        if len(entries) > MAX_LOG_ENTRIES:
            entries = entries[-MAX_LOG_ENTRIES:]
        LOGS_PATH.write_text(json.dumps(entries, indent=2), encoding='utf-8')
    except Exception as e:
        logger.warning(f"Failed to write run log: {e}")


def _last_run_of_type(job_type: str) -> Optional[dict]:
    """Return the most recent scheduler_runs.json entry for job_type, or None."""
    try:
        if not LOGS_PATH.exists():
            return None
        entries = json.loads(LOGS_PATH.read_text(encoding='utf-8'))
        if not isinstance(entries, list):
            return None
        for entry in reversed(entries):
            if isinstance(entry, dict) and entry.get('job_type') == job_type:
                return entry
    except Exception:
        pass
    return None


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


def _briefing_graph_links_ready(driver, graph_id: str, briefing_source: str) -> bool:
    """
    Return True when graph-link context required by simulation_tick is already present
    for this briefing (NewsChunk nodes + MENTIONED_IN edges).

    This avoids running incremental_update twice for the same briefing, which would
    inflate MENTIONED_WITH weights due to ON MATCH weight increments.
    """
    try:
        with driver.session() as session:
            labels_row = session.run(
                "CALL db.labels() YIELD label RETURN collect(label) AS labels"
            ).single()
            rels_row = session.run(
                "CALL db.relationshipTypes() YIELD relationshipType RETURN collect(relationshipType) AS rels"
            ).single()

            labels = labels_row["labels"] if labels_row and labels_row["labels"] is not None else []
            rels = rels_row["rels"] if rels_row and rels_row["rels"] is not None else []

            if "NewsChunk" not in labels or "MENTIONED_IN" not in rels:
                return False

            chunks_row = session.run(
                """
                MATCH (c:NewsChunk {graph_id: $gid, briefing_source: $briefing})
                RETURN count(c) AS c
                """,
                gid=graph_id,
                briefing=briefing_source,
            ).single()
            chunk_count = int(chunks_row["c"]) if chunks_row and chunks_row["c"] is not None else 0
            if chunk_count == 0:
                return False

            links_row = session.run(
                """
                MATCH (:Entity {graph_id: $gid})-[r:MENTIONED_IN]->(c:NewsChunk {graph_id: $gid, briefing_source: $briefing})
                RETURN count(r) AS c
                """,
                gid=graph_id,
                briefing=briefing_source,
            ).single()
            link_count = int(links_row["c"]) if links_row and links_row["c"] is not None else 0
            return link_count > 0

    except Exception as e:
        logger.warning(f"Graph-link readiness check failed for '{briefing_source}': {e}")
        return False


# ---------------------------------------------------------------------------
# Catch-up logic
# ---------------------------------------------------------------------------

def run_catchup(graph_id: str, status_path: Path, status_state: dict) -> None:
    """
    Check whether the daily and/or half-hourly jobs are overdue; run them if so.
    Daily always takes priority — if both are overdue, daily runs first, then
    half-hourly is re-checked after daily completes.

    Re-entrance guarded via _catchup_running: when a job called from here
    tries to trigger another catch-up on completion, it is silently skipped.
    This prevents unbounded recursion while still letting APScheduler-triggered
    jobs call catch-up normally.
    """
    if _catchup_running.is_set():
        return
    _catchup_running.set()
    try:
        now = datetime.now(timezone.utc)

        # Step 1 — daily overdue? (past 02:00 UTC and not yet run today)
        daily_overdue = False
        if now.hour >= 2:
            last_daily = _last_run_of_type('daily')
            if last_daily is None:
                daily_overdue = True
            else:
                try:
                    last_dt = datetime.fromisoformat(last_daily['finished_at'])
                    if last_dt.date() < now.date():
                        daily_overdue = True
                except Exception:
                    daily_overdue = True

        if daily_overdue:
            logger.info("Catch-up: daily job overdue — running now")
            daily_job(graph_id, status_path, status_state)

        # Step 2 — half-hourly overdue? (last completed run > 30 min ago, or no record)
        hh_overdue = False
        last_hh = _last_run_of_type('halfhourly')
        if last_hh is None:
            hh_overdue = True
        else:
            try:
                last_dt = datetime.fromisoformat(last_hh['finished_at'])
                if (now - last_dt).total_seconds() > 1800:
                    hh_overdue = True
            except Exception:
                hh_overdue = True

        if hh_overdue:
            logger.info("Catch-up: half-hourly job overdue — running now")
            hourly_job(graph_id, status_path, status_state)

    finally:
        _catchup_running.clear()


# ---------------------------------------------------------------------------
# Ollama health check
# ---------------------------------------------------------------------------

_OLLAMA_TARGET_MODEL = "qwen2.5:14b"
# Try container hostname first (running inside Docker), fall back to localhost (host runner)
_OLLAMA_URLS = ["http://ollama:11434", "http://localhost:11434"]
_OLLAMA_POLL_INTERVAL = 10   # seconds between polls when model is loading
_OLLAMA_MAX_WAIT = 60        # total seconds to wait for model to become ready


def _check_ollama() -> bool:
    """
    Pre-flight check before any tick.

    Steps:
      1. GET /api/tags from Ollama (tries container URL then localhost).
      2. Verify _OLLAMA_TARGET_MODEL is in the returned model list.
      3. If model is present but not 'ready', poll every 10s for up to 60s.

    Returns True if model is available and ready.
    Returns False if Ollama is unreachable or the model is absent after the wait.
    Logs at WARNING level on every failure.
    """
    try:
        import requests as _requests
    except ImportError:
        logger.warning("Ollama health check: 'requests' not installed — skipping check (assuming available)")
        return True

    tags_url = None
    response_json = None

    for base_url in _OLLAMA_URLS:
        try:
            resp = _requests.get(f"{base_url}/api/tags", timeout=5)
            if resp.status_code == 200:
                tags_url = base_url
                response_json = resp.json()
                break
        except Exception:
            continue

    if response_json is None:
        logger.warning(
            f"Ollama unavailable — tried {_OLLAMA_URLS}. Skipping tick."
        )
        return False

    # Check whether the target model is listed
    models = response_json.get("models", [])
    model_names = [m.get("name", "") for m in models]
    model_present = any(_OLLAMA_TARGET_MODEL in name for name in model_names)

    if not model_present:
        logger.warning(
            f"Ollama reachable at {tags_url} but model '{_OLLAMA_TARGET_MODEL}' not found. "
            f"Models available: {model_names}. Skipping tick."
        )
        return False

    # Model found — check if it needs load time (poll up to _OLLAMA_MAX_WAIT seconds)
    waited = 0
    while waited < _OLLAMA_MAX_WAIT:
        # A simple generate probe to confirm the model is actually responding
        try:
            probe = _requests.post(
                f"{tags_url}/api/generate",
                json={"model": _OLLAMA_TARGET_MODEL, "prompt": "ping", "stream": False, "options": {"num_predict": 1}},
                timeout=15,
            )
            if probe.status_code == 200:
                logger.info(f"Ollama health check passed — {_OLLAMA_TARGET_MODEL} ready at {tags_url}")
                return True
        except Exception as e:
            logger.info(f"Ollama model probe not ready yet: {e} — retrying in {_OLLAMA_POLL_INTERVAL}s")

        time.sleep(_OLLAMA_POLL_INTERVAL)
        waited += _OLLAMA_POLL_INTERVAL

    logger.warning(
        f"Ollama model '{_OLLAMA_TARGET_MODEL}' did not respond after {_OLLAMA_MAX_WAIT}s. Skipping tick."
    )
    return False


# ---------------------------------------------------------------------------
# MemoryEvent TTL pruning
# ---------------------------------------------------------------------------

_MEMORY_TTL_DAYS = 90


def prune_memory_events(graph_id: str) -> None:
    """
    Delete MemoryEvent nodes older than _MEMORY_TTL_DAYS days.
    Run weekly to prevent Neo4j CE degrading under accumulation load.
    At 500 agents × 48 ticks/day, ~24,000 nodes are written daily; without
    pruning Neo4j CE will degrade significantly within months.
    """
    logger.info(f"MemoryEvent TTL pruning: deleting events older than {_MEMORY_TTL_DAYS} days")
    _driver = None
    try:
        _driver = db_setup()
        with _driver.session() as session:
            result = session.run(
                """
                MATCH (m:MemoryEvent {graph_id: $gid})
                WHERE m.timestamp < datetime() - duration({days: $days})
                WITH m LIMIT 10000
                DETACH DELETE m
                RETURN count(m) AS deleted
                """,
                gid=graph_id,
                days=_MEMORY_TTL_DAYS,
            )
            record = result.single()
            deleted = record["deleted"] if record else 0
            logger.info(f"MemoryEvent TTL pruning complete: {deleted} nodes deleted")
    except Exception as e:
        logger.error(f"MemoryEvent TTL pruning failed: {e}")
    finally:
        if _driver:
            _driver.close()


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

def hourly_job(graph_id: str, status_path: Path, status_state: dict) -> None:
    """
    1. Fetch news → briefing file
    2. Merge briefing into Neo4j
    3. Run 500-agent tick (entire job skipped if daily job is running)

    Writes a log entry to scheduler_runs.json on completion (success or error).
    Runs catch-up check after completing to cover any job that became overdue
    while this run was in progress.
    """
    # Daily job takes priority — skip everything while it is running
    if _daily_running.is_set():
        logger.info("Hourly job skipped: daily full simulation in progress")
        return

    logger.info("Hourly job starting")
    started_at = datetime.now(timezone.utc).isoformat()
    error: Optional[str] = None
    now = lambda: datetime.now(timezone.utc).isoformat()
    _driver = None

    # Step 0: price fetch (non-blocking — failure doesn't abort the job)
    try:
        fetch_prices()
        logger.info("Price fetch complete")
    except Exception as e:
        logger.warning(f"price_fetcher: {e}")

    # Pre-flight: verify Ollama is up and the target model is loaded before any tick
    if not _check_ollama():
        logger.warning("Ollama unavailable — skipping hourly tick")
        finished_at = datetime.now(timezone.utc).isoformat()
        append_run_log(
            job_type='halfhourly',
            started_at=started_at,
            finished_at=finished_at,
            status='model_unavailable',
            error='Ollama health check failed',
        )
        return

    try:
        # Connect fresh driver for this job cycle
        try:
            _driver = db_setup()
        except Exception as e:
            logger.error(f"Hourly job: Neo4j connection failed: {e}")
            error = f"neo4j_connect: {e}"
            status_state['last_error'] = f"scheduler: {e}"
            write_status(status_path, **{k: status_state.get(k) for k in
                                          ('last_news_fetch', 'last_tick', 'last_full_simulation')},
                         agent_pool_size=-1, last_error=status_state.get('last_error'))
            return

        # Step 1: news fetch
        briefing_path = None
        try:
            briefing_path = fetch()
            status_state['last_news_fetch'] = now()
            logger.info(f"News fetched: {briefing_path}")
        except Exception as e:
            error = f"news_fetcher: {e}"
            logger.error(error)

        # Step 2: incremental update (only if briefing was written)
        if briefing_path:
            try:
                process_briefing(_driver, graph_id, str(briefing_path))
                logger.info("Incremental update complete")
            except Exception as e:
                error = f"incremental_update: {e}"
                logger.error(error)

        # Step 3: tick (re-check daily flag in case it started between steps 1-2 and now)
        if briefing_path:
            if _daily_running.is_set():
                logger.info("Skipping hourly tick: daily full simulation started during hourly run")
            else:
                try:
                    run_tick(_driver, graph_id, str(briefing_path), full=False)
                    status_state['last_tick'] = now()
                except Exception as e:
                    error = f"simulation_tick: {e}"
                    logger.error(error)

        pool_size = get_agent_pool_size(_driver, graph_id)
        write_status(
            status_path,
            last_news_fetch=status_state.get('last_news_fetch'),
            last_tick=status_state.get('last_tick'),
            last_full_simulation=status_state.get('last_full_simulation'),
            agent_pool_size=pool_size,
            last_error=error,
        )

    finally:
        if _driver:
            _driver.close()
        finished_at = datetime.now(timezone.utc).isoformat()
        append_run_log(
            job_type='halfhourly',
            started_at=started_at,
            finished_at=finished_at,
            status='error' if error else 'success',
            error=error,
        )
        logger.info("Hourly job complete")
        run_catchup(graph_id, status_path, status_state)


def daily_job(graph_id: str, status_path: Path, status_state: dict) -> None:
    """
    Full 8192-agent simulation tick. Sets _daily_running mutex for duration.
    Uses the most recent briefing file (latest .txt in briefings dir).

    Before tick execution, performs a readiness check for NewsChunk/MENTIONED_IN
    data used by simulation_tick graph-link prompt context. If missing, runs
    process_briefing() once for that briefing.

    Writes a log entry to scheduler_runs.json on completion (success or error).
    Runs catch-up check after completing — specifically to fire any half-hourly
    tick that became overdue during the (potentially long) daily run.
    """
    logger.info("Daily full tick starting")
    _daily_running.set()
    started_at = datetime.now(timezone.utc).isoformat()
    error: Optional[str] = None
    now = lambda: datetime.now(timezone.utc).isoformat()
    _driver = None

    try:
        try:
            _driver = db_setup()
        except Exception as e:
            logger.error(f"Daily job: Neo4j connection failed: {e}")
            error = f"neo4j_connect: {e}"
            return

        # Pre-flight: verify Ollama is up and the target model is loaded
        if not _check_ollama():
            logger.warning("Ollama unavailable — skipping daily tick")
            error = "model_unavailable"
            return

        briefings_dir = Path(__file__).parent.parent / "briefings"
        briefing_files = sorted(briefings_dir.glob("*.txt"), reverse=True)
        if not briefing_files:
            logger.warning("Daily job: no briefing files found, skipping tick")
            error = "no briefing files found"
            write_status(
                status_path,
                last_news_fetch=status_state.get('last_news_fetch'),
                last_tick=status_state.get('last_tick'),
                last_full_simulation=status_state.get('last_full_simulation'),
                agent_pool_size=get_agent_pool_size(_driver, graph_id),
                last_error="daily_simulation_tick: no briefing files found",
            )
            return

        latest_briefing = briefing_files[0]
        logger.info(f"Daily tick using briefing: {latest_briefing}")

        # Ensure graph-link context exists for the selected daily briefing.
        # Run incremental_update only when missing to avoid double-counting
        # MENTIONED_WITH weights for already-processed briefings.
        briefing_source = latest_briefing.name
        links_ready = _briefing_graph_links_ready(_driver, graph_id, briefing_source)
        if not links_ready:
            logger.info(
                "Daily pre-pass: graph link data missing for briefing — running incremental_update"
            )
            try:
                process_briefing(_driver, graph_id, str(latest_briefing))
                logger.info("Daily pre-pass incremental update complete")
            except Exception as e:
                logger.warning(f"Daily pre-pass incremental update failed: {e}")
        else:
            logger.info("Daily pre-pass: graph link data already present — skipping incremental_update")

        try:
            run_tick(_driver, graph_id, str(latest_briefing), full=True)
            status_state['last_full_simulation'] = now()
        except Exception as e:
            error = f"daily_simulation_tick: {e}"
            logger.error(error)

        pool_size = get_agent_pool_size(_driver, graph_id)
        write_status(
            status_path,
            last_news_fetch=status_state.get('last_news_fetch'),
            last_tick=status_state.get('last_tick'),
            last_full_simulation=status_state.get('last_full_simulation'),
            agent_pool_size=pool_size,
            last_error=error,
        )

    finally:
        if _driver:
            _driver.close()
        _daily_running.clear()
        finished_at = datetime.now(timezone.utc).isoformat()
        append_run_log(
            job_type='daily',
            started_at=started_at,
            finished_at=finished_at,
            status='error' if error else 'success',
            error=error,
        )
        logger.info("Daily full tick complete")
        run_catchup(graph_id, status_path, status_state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="MiroFish APScheduler orchestrator")
    parser.add_argument('--graph-id', help="Neo4j graph_id (auto-detected if omitted)")
    args = parser.parse_args()

    graph_id = args.graph_id if args.graph_id is not None else resolve_active_graph_id()
    print(f"Scheduler starting for graph_id: {graph_id}")

    status_state: dict = {
        'last_news_fetch': None,
        'last_tick': None,
        'last_full_simulation': None,
    }

    # Run catch-up before the normal schedule starts.
    # This ensures overdue jobs are executed immediately — no waiting for the
    # first scheduled interval tick.  Priority: daily before half-hourly.
    logger.info("Running startup catch-up check")
    run_catchup(graph_id, STATUS_PATH, status_state)

    scheduler = BlockingScheduler(timezone="Europe/London")

    # Half-hourly job.
    # next_run_time is NOT set here — catch-up already handles the initial run.
    # misfire_grace_time=None: missed triggers never expire, so if the job
    # overruns the 30-min window the missed trigger fires immediately on finish.
    # coalesce=True + max_instances=1: collapses any backlog to at most one
    # queued run — no pile-up regardless of how long a run takes.
    scheduler.add_job(
        hourly_job,
        trigger='interval',
        minutes=30,
        kwargs={'graph_id': graph_id, 'status_path': STATUS_PATH, 'status_state': status_state},
        id='hourly',
        name='30-min news + tick',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,
    )

    # Daily job — 02:00 UTC (starts 2am UTC, finishes ~12pm UTC, before US market open at 13:30 UTC)
    scheduler.add_job(
        daily_job,
        trigger='cron',
        hour=2,
        minute=0,
        kwargs={'graph_id': graph_id, 'status_path': STATUS_PATH, 'status_state': status_state},
        id='daily',
        name='Daily full simulation tick',
        max_instances=1,
        coalesce=True,
    )

    # Weekly MemoryEvent TTL pruning — Sunday 04:00 UTC
    # At 500 agents × 48 ticks/day = ~24,000 MemoryEvent nodes/day.
    # Without pruning, Neo4j CE degrades significantly within months.
    # Deletes events older than 90 days in batches of 10,000.
    scheduler.add_job(
        prune_memory_events,
        trigger='cron',
        day_of_week='sun',
        hour=4,
        minute=0,
        kwargs={'graph_id': graph_id},
        id='weekly_ttl_prune',
        name='Weekly MemoryEvent TTL pruning (90d)',
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
