"""
force_retick.py — Reset scheduler state so the next scheduler.py startup
immediately fires a half-hourly tick on the most recent news, without waiting
30 minutes.

What this does:
  1. Backdates the last halfhourly entry in scheduler_runs.json by 2 hours
     so the catch-up logic treats it as overdue.
  2. Removes seen_urls.json entries that were added in the last HOURS hours
     (default: 1) so news_fetcher will re-fetch and re-score those articles.

What this does NOT do:
  - Touch daily/weekly log entries.
  - Delete the whole seen_urls.json (that would re-process ALL history).
  - Modify Neo4j, briefings, prices, or any other state.

Usage:
  python backend/scripts/force_retick.py [--hours 1]

Then immediately start (or restart) the scheduler:
  python backend/scripts/scheduler.py --graph-id d3a38be8-37d9-4818-be28-5d2d0efa82c0
"""

import sys
import json
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

parser = argparse.ArgumentParser(description="Reset scheduler state for immediate retick")
parser.add_argument("--hours", type=float, default=1.0,
                    help="Remove seen_urls newer than this many hours ago (default: 1)")
parser.add_argument("--dry-run", action="store_true",
                    help="Print what would change without writing anything")
args = parser.parse_args()

DRY_RUN   = args.dry_run
CUTOFF    = datetime.now(timezone.utc) - timedelta(hours=args.hours)

_SCRIPTS_DIR  = Path(__file__).parent
_LOGS_PATH    = _SCRIPTS_DIR.parent / "logs" / "scheduler_runs.json"
_SEEN_PATH    = _SCRIPTS_DIR / "seen_urls.json"

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
# Done
# ---------------------------------------------------------------------------

print("\nDone.")
if not DRY_RUN:
    print("You can now start the scheduler — it will fire a tick immediately on startup:")
    print("  python backend/scripts/scheduler.py --graph-id d3a38be8-37d9-4818-be28-5d2d0efa82c0")
else:
    print("Re-run without --dry-run to apply changes.")
