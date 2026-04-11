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
from price_fetcher import fetch_prices
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

    # Step 0: price fetch (non-blocking — failure doesn't abort the job)
    try:
        fetch_prices()
        logger.info("Price fetch complete")
    except Exception as e:
        logger.warning(f"price_fetcher: {e}")

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
            write_status(
                status_path,
                last_news_fetch=status_state.get('last_news_fetch'),
                last_tick=status_state.get('last_tick'),
                last_full_simulation=status_state.get('last_full_simulation'),
                agent_pool_size=get_agent_pool_size(driver, graph_id),
                last_error="daily_simulation_tick: no briefing files found",
            )
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

    graph_id = args.graph_id if args.graph_id is not None else resolve_active_graph_id()
    print(f"Scheduler starting for graph_id: {graph_id}")

    status_state: dict = {
        'last_news_fetch': None,
        'last_tick': None,
        'last_full_simulation': None,
    }

    scheduler = BlockingScheduler(timezone="Europe/London")

    # Runs every 30 minutes, firing immediately on startup.
    # next_run_time=datetime.now(...) triggers the first run instantly rather
    # than waiting a full interval.
    # misfire_grace_time=None: missed triggers never expire, so if the job
    # overruns the 30-min window the missed trigger fires immediately on finish.
    # coalesce=True + max_instances=1: collapses any backlog to at most one
    # queued run — no pile-up regardless of how long a run takes.
    scheduler.add_job(
        hourly_job,
        trigger='interval',
        minutes=30,
        next_run_time=datetime.now(timezone.utc),
        kwargs={'graph_id': graph_id, 'status_path': STATUS_PATH, 'status_state': status_state},
        id='hourly',
        name='30-min news + tick',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,
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