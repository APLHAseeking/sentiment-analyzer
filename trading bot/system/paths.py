"""Single anchor for every relative state-file path in the bot.

All persisted state (SQLite DB, regime model, dashboard JSON, risk lock
file) defaults to a path relative to the repo root. Three different
modules (bot/db.py, bot/config.py, system/config.py) used to resolve these
relative paths independently against whatever the process cwd happened to
be at startup. PROJECT_ROOT pins them all to the same directory regardless
of cwd.
"""
from __future__ import annotations

import os

# "trading bot/" directory — parent of system/
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve(path: str) -> str:
    """Return `path` unchanged if absolute, else anchored to PROJECT_ROOT."""
    if os.path.isabs(path):
        return path
    return os.path.join(PROJECT_ROOT, path)
