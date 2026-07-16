"""Startup status file for monitoring/watchdog.py to read.

Written once at orchestrator startup (orchestration/main_loop.py). Lets a
separate watchdog process detect two things nothing inside the bot's own
process could ever check about itself: whether its PID is still alive, and
whether it's running stale code (see CLAUDE-REFERENCE.md#history incidents
#18/#22 -- a running process silently older than the latest fix, twice,
caught only because a human happened to check).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_REPO_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_STATUS_FILE = _REPO_DIR / "bot_status.json"


def get_git_commit(repo_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return None


def write_status_file(path: Path = _DEFAULT_STATUS_FILE, repo_dir: Path = _REPO_DIR) -> None:
    status = {
        "pid": os.getpid(),
        "commit": get_git_commit(repo_dir),
        "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        path.write_text(json.dumps(status))
    except Exception as exc:
        log.warning("Could not write status file %s: %s", path, exc)


def read_status_file(path: Path = _DEFAULT_STATUS_FILE) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None
