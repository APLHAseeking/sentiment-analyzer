"""Dashboard state serialization.

The orchestration loop writes state here; the Streamlit app reads from here.
Using a JSON file on disk means the dashboard can run in a separate process
and does not need to import any trading modules directly.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, UTC
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT: dict[str, Any] = {
    "last_updated": None,
    "regime": {
        "label": "unknown",
        "confidence": 0.0,
        "is_stable": False,
        "n_regimes": 0,
        "posteriors": [],
    },
    "portfolio": {
        "equity": 0.0,
        "cash": 0.0,
        "positions": [],
    },
    "risk": {
        "state": "unknown",
        "peak_nav": 0.0,
        "day_start_nav": 0.0,
        "week_start_nav": 0.0,
        "lock_file_exists": False,
    },
}


class DashboardStore:
    def __init__(self, path: str) -> None:
        self._path = path

    def update(self, data: dict[str, Any]) -> None:
        payload = {**_DEFAULT, **data, "last_updated": datetime.now(UTC).isoformat()}
        try:
            with open(self._path, "w") as f:
                json.dump(payload, f, indent=2, default=str)
        except Exception as exc:
            log.warning("Dashboard store write failed: %s", exc)

    def read(self) -> dict[str, Any]:
        if not os.path.exists(self._path):
            return _DEFAULT.copy()
        try:
            with open(self._path) as f:
                return json.load(f)
        except Exception as exc:
            log.warning("Dashboard store read failed: %s", exc)
            return _DEFAULT.copy()
