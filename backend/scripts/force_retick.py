"""
force_retick.py — Reset scheduler state so the next scheduler.py startup
immediately re-processes fresh state instead of resuming stale in-progress work.

What this does:
  1. Backdates the last halfhourly entry in scheduler_runs.json by 2 hours
     so the catch-up logic treats it as overdue.
  2. Removes seen_urls.json entries that were added in the last HOURS hours
     (default: 1) so news_fetcher will re-fetch and re-score those articles.
    3. Removes local resume artifacts (tick_checkpoint.json and contagion_flag.txt)
         so simulation_tick starts from scratch instead of resuming processed_agent_ids.
    4. Deletes MemoryEvent nodes for the target graph_id from Neo4j so per-agent
         memory context is fully cleared.

What this does NOT do:
  - Touch daily/weekly log entries.
  - Delete the whole seen_urls.json (that would re-process ALL history).
    - Delete Entity nodes or price/briefing files.

Important scheduler behavior:
    - If scheduler starts after the configured daily time and daily is overdue,
        catch-up runs daily first. This is expected. This script clears resume and
        memory state so that daily/halfhourly both run cleanly.

Usage:
    python backend/scripts/force_retick.py [--hours 1] [--graph-id <uuid>]

Then immediately start (or restart) the scheduler:
  python backend/scripts/scheduler.py --graph-id d3a38be8-37d9-4818-be28-5d2d0efa82c0
"""

import sys
import json
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

from db_setup import setup as db_setup

parser = argparse.ArgumentParser(description="Reset scheduler state for immediate retick")
parser.add_argument("--hours", type=float, default=1.0,
                    help="Remove seen_urls newer than this many hours ago (default: 1)")
parser.add_argument("--graph-id", default=None,
                    help="Neo4j graph_id for MemoryEvent wipe (auto-detected if omitted)")
parser.add_argument("--keep-memory-events", action="store_true",
                    help="Do not delete MemoryEvent nodes in Neo4j")
parser.add_argument("--keep-checkpoint", action="store_true",
                    help="Do not delete tick_checkpoint.json")
parser.add_argument("--keep-contagion-flag", action="store_true",
                    help="Do not delete backend/briefings/contagion_flag.txt")
parser.add_argument("--dry-run", action="store_true",
                    help="Print what would change without writing anything")
args = parser.parse_args()

DRY_RUN   = args.dry_run
CUTOFF    = datetime.now(timezone.utc) - timedelta(hours=args.hours)

_SCRIPTS_DIR  = Path(__file__).parent
_LOGS_PATH    = _SCRIPTS_DIR.parent / "logs" / "scheduler_runs.json"
_SEEN_PATH    = _SCRIPTS_DIR / "seen_urls.json"
_CHECKPOINT_PATH = _SCRIPTS_DIR / "tick_checkpoint.json"
_CONTAGION_FLAG_PATH = _SCRIPTS_DIR.parent / "briefings" / "contagion_flag.txt"
_PROJECTS_DIR = _SCRIPTS_DIR.parent / "uploads" / "projects"


def _resolve_active_graph_id(projects_dir: Path) -> str:
    """Find graph_id from the most recently completed project.json."""
    if not projects_dir.exists():
        raise FileNotFoundError(f"Projects directory not found: {projects_dir}")

    candidates = []
    for proj_dir in projects_dir.iterdir():
        proj_json = proj_dir / "project.json"
        if proj_json.exists():
            with open(proj_json, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("status") == "GRAPH_COMPLETED" and data.get("graph_id"):
                candidates.append((proj_json.stat().st_mtime, data["graph_id"]))

    if not candidates:
        raise ValueError("No completed graph project found in uploads/projects")

    candidates.sort(reverse=True)
    return candidates[0][1]

now_str = datetime.now(timezone.utc).isoformat()
print(f"force_retick.py  {'(DRY RUN) ' if DRY_RUN else ''}— {now_str}")
print(f"Removing seen_urls newer than: {CUTOFF.isoformat()}\n")

# ---------------------------------------------------------------------------
# Step 1 — backdate the last halfhourly log entry
# ---------------------------------------------------------------------------

print("--- Step 1: scheduler_runs.json ---")

if not _LOGS_PATH.exists():
    print("  [SKIP] scheduler_runs.json not found — scheduler has never run, no action needed.")
else:
    try:
        entries = json.loads(_LOGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            entries = []

        # Find the last halfhourly entry
        last_idx = None
        for i in range(len(entries) - 1, -1, -1):
            if isinstance(entries[i], dict) and entries[i].get("job_type") == "halfhourly":
                last_idx = i
                break

        if last_idx is None:
            print("  [SKIP] No halfhourly entry found — catch-up will treat scheduler as never run.")
        else:
            old_finished = entries[last_idx].get("finished_at", "(unknown)")
            # Backdate by 2 hours — well past the 30-min overdue threshold
            backdated = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            print(f"  Found entry at index {last_idx}:")
            print(f"    finished_at was: {old_finished}")
            print(f"    finished_at now: {backdated}")

            if not DRY_RUN:
                entries[last_idx]["finished_at"] = backdated
                _LOGS_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")
                print("  [OK] Written.")
            else:
                print("  [DRY RUN] Would write.")

    except Exception as e:
        print(f"  [ERROR] {e}")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Step 2 — remove recent entries from seen_urls.json
# ---------------------------------------------------------------------------

print("\n--- Step 2: seen_urls.json ---")

if not _SEEN_PATH.exists():
    print("  [SKIP] seen_urls.json not found — nothing to clear.")
else:
    try:
        seen = json.loads(_SEEN_PATH.read_text(encoding="utf-8"))
        if not isinstance(seen, dict):
            print("  [SKIP] Unexpected format — expected a dict.")
        else:
            total_before = len(seen)
            to_remove = []
            parse_errors = 0

            for url_hash, ts_str in seen.items():
                try:
                    ts = datetime.fromisoformat(ts_str)
                    # Make timezone-aware if naive
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= CUTOFF:
                        to_remove.append(url_hash)
                except Exception:
                    parse_errors += 1

            print(f"  Total entries: {total_before}")
            print(f"  Entries newer than cutoff: {len(to_remove)}")
            if parse_errors:
                print(f"  Entries with unparseable timestamps (kept): {parse_errors}")

            if not to_remove:
                print("  [SKIP] No entries within the time window — nothing to remove.")
            else:
                if not DRY_RUN:
                    for h in to_remove:
                        del seen[h]
                    _SEEN_PATH.write_text(json.dumps(seen, indent=2), encoding="utf-8")
                    print(f"  [OK] Removed {len(to_remove)} entries. {len(seen)} remain.")
                else:
                    print(f"  [DRY RUN] Would remove {len(to_remove)} entries. {total_before - len(to_remove)} would remain.")

    except Exception as e:
        print(f"  [ERROR] {e}")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Step 3 — clear local resume artifacts
# ---------------------------------------------------------------------------

print("\n--- Step 3: local resume artifacts ---")

if args.keep_checkpoint:
    print("  [SKIP] --keep-checkpoint set.")
else:
    if not _CHECKPOINT_PATH.exists():
        print("  [SKIP] tick_checkpoint.json not found.")
    else:
        print(f"  Found checkpoint: {_CHECKPOINT_PATH}")
        if DRY_RUN:
            print("  [DRY RUN] Would delete checkpoint.")
        else:
            _CHECKPOINT_PATH.unlink()
            print("  [OK] Deleted checkpoint.")

if args.keep_contagion_flag:
    print("  [SKIP] --keep-contagion-flag set.")
else:
    if not _CONTAGION_FLAG_PATH.exists():
        print("  [SKIP] contagion_flag.txt not found.")
    else:
        print(f"  Found contagion flag: {_CONTAGION_FLAG_PATH}")
        if DRY_RUN:
            print("  [DRY RUN] Would delete contagion flag.")
        else:
            _CONTAGION_FLAG_PATH.unlink()
            print("  [OK] Deleted contagion flag.")


# ---------------------------------------------------------------------------
# Step 4 — clear MemoryEvent nodes from Neo4j
# ---------------------------------------------------------------------------

print("\n--- Step 4: Neo4j MemoryEvent wipe ---")

if args.keep_memory_events:
    print("  [SKIP] --keep-memory-events set.")
else:
    try:
        graph_id = args.graph_id or _resolve_active_graph_id(_PROJECTS_DIR)
        print(f"  graph_id: {graph_id}")
    except Exception as e:
        print(f"  [ERROR] Could not resolve graph_id: {e}")
        sys.exit(1)

    driver = None
    try:
        driver = db_setup()
        with driver.session() as session:
            count_record = session.run(
                "MATCH (m:MemoryEvent {graph_id: $gid}) RETURN count(m) AS c",
                gid=graph_id,
            ).single()
            total_before = int(count_record["c"]) if count_record and count_record["c"] is not None else 0
            print(f"  MemoryEvent nodes before: {total_before}")

            if total_before == 0:
                print("  [SKIP] No MemoryEvent nodes to delete.")
            elif DRY_RUN:
                print("  [DRY RUN] Would delete all MemoryEvent nodes for this graph_id.")
            else:
                deleted_total = 0
                while True:
                    rec = session.run(
                        """
                        MATCH (m:MemoryEvent {graph_id: $gid})
                        WITH m LIMIT 10000
                        DETACH DELETE m
                        RETURN count(m) AS deleted
                        """,
                        gid=graph_id,
                    ).single()
                    deleted = int(rec["deleted"]) if rec and rec["deleted"] is not None else 0
                    deleted_total += deleted
                    if deleted == 0:
                        break

                print(f"  [OK] Deleted MemoryEvent nodes: {deleted_total}")

                after_record = session.run(
                    "MATCH (m:MemoryEvent {graph_id: $gid}) RETURN count(m) AS c",
                    gid=graph_id,
                ).single()
                total_after = int(after_record["c"]) if after_record and after_record["c"] is not None else 0
                print(f"  MemoryEvent nodes after: {total_after}")

    except Exception as e:
        print(f"  [ERROR] Neo4j wipe failed: {e}")
        sys.exit(1)
    finally:
        if driver:
            driver.close()

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

print("\nDone.")
if not DRY_RUN:
    print("You can now start the scheduler with clean resume/memory state:")
    print("  python backend/scripts/scheduler.py --graph-id d3a38be8-37d9-4818-be28-5d2d0efa82c0")
else:
    print("Re-run without --dry-run to apply changes.")
