"""
Control API — trigger MiroFish jobs remotely and check status.
"""
import os
import json
import subprocess
import threading
from pathlib import Path
from flask import Blueprint, jsonify, current_app
from ..utils.logger import get_logger

control_bp = Blueprint('control', __name__)
logger = get_logger('mirofish.api.control')

BACKEND_DIR = Path(__file__).parent.parent.parent  # /app/backend
STATUS_PATH = BACKEND_DIR / 'scripts' / 'status.json'
BRIEFINGS_DIR = BACKEND_DIR / 'briefings'
GRAPH_ID = 'd3a38be8-37d9-4818-be28-5d2d0efa82c0'

_running_jobs = {}
_job_lock = threading.Lock()


def _latest_briefing():
    files = sorted(BRIEFINGS_DIR.glob('*.txt'), reverse=True)
    return str(files[0]) if files else None


def _run_job(job_id, cmd):
    with _job_lock:
        if _running_jobs.get(job_id):
            return False
        _running_jobs[job_id] = True
    try:
        subprocess.run(cmd, cwd=str(BACKEND_DIR / 'scripts'))
    finally:
        with _job_lock:
            _running_jobs[job_id] = False
    return True


@control_bp.route('/status', methods=['GET'])
def get_status():
    status = {}
    if STATUS_PATH.exists():
        try:
            status = json.loads(STATUS_PATH.read_text())
        except Exception:
            pass
    with _job_lock:
        running = {k: v for k, v in _running_jobs.items() if v}
    briefing = _latest_briefing()
    return jsonify({
        "success": True,
        "data": {
            "status": status,
            "running_jobs": list(running.keys()),
            "latest_briefing": Path(briefing).name if briefing else None,
            "briefing_count": len(list(BRIEFINGS_DIR.glob('*.txt'))) if BRIEFINGS_DIR.exists() else 0,
        }
    })


@control_bp.route('/fetch-news', methods=['POST'])
def fetch_news():
    with _job_lock:
        if _running_jobs.get('news'):
            return jsonify({"success": False, "error": "News fetch already running"})

    def run():
        _running_jobs['news'] = True
        try:
            subprocess.run(
                ['python', 'news_fetcher.py'],
                cwd=str(BACKEND_DIR / 'scripts')
            )
        finally:
            _running_jobs['news'] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True, "message": "News fetch started"})


@control_bp.route('/run-tick', methods=['POST'])
def run_tick():
    briefing = _latest_briefing()
    if not briefing:
        return jsonify({"success": False, "error": "No briefing files found"})

    with _job_lock:
        if _running_jobs.get('tick'):
            return jsonify({"success": False, "error": "Tick already running"})

    def run():
        _running_jobs['tick'] = True
        try:
            subprocess.run(
                ['python', 'simulation_tick.py',
                 '--briefing', briefing,
                 '--graph-id', GRAPH_ID],
                cwd=str(BACKEND_DIR / 'scripts')
            )
        finally:
            _running_jobs['tick'] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True, "message": f"Tick started using {Path(briefing).name}"})


@control_bp.route('/run-update', methods=['POST'])
def run_update():
    briefing = _latest_briefing()
    if not briefing:
        return jsonify({"success": False, "error": "No briefing files found"})

    with _job_lock:
        if _running_jobs.get('update'):
            return jsonify({"success": False, "error": "Update already running"})

    def run():
        _running_jobs['update'] = True
        try:
            subprocess.run(
                ['python', 'incremental_update.py',
                 '--briefing', briefing,
                 '--graph-id', GRAPH_ID],
                cwd=str(BACKEND_DIR / 'scripts')
            )
        finally:
            _running_jobs['update'] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True, "message": f"Update started using {Path(briefing).name}"})