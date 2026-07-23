from __future__ import annotations

import threading
import time
from pathlib import Path

from monitoring import heartbeat


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _dump_has_content(dump_file: Path) -> bool:
    return dump_file.exists() and dump_file.stat().st_size > 0


def test_start_heartbeat_writes_thread_dump_file(tmp_path: Path):
    dump_file = tmp_path / "threaddump.log"

    stop_event = heartbeat.start_heartbeat(interval_sec=0.05, dump_file=dump_file)
    try:
        assert _wait_for(lambda: _dump_has_content(dump_file)), \
            "heartbeat thread never wrote a non-empty dump file"
    finally:
        stop_event.set()

    assert "Thread" in dump_file.read_text()


def test_start_heartbeat_logs_alive_message(tmp_path: Path, caplog):
    dump_file = tmp_path / "threaddump.log"

    with caplog.at_level("INFO", logger="monitoring.heartbeat"):
        stop_event = heartbeat.start_heartbeat(interval_sec=0.05, dump_file=dump_file)
        try:
            assert _wait_for(
                lambda: any("heartbeat" in rec.message for rec in caplog.records)
            ), "heartbeat thread never logged"
        finally:
            stop_event.set()


def test_stop_event_stops_the_thread(tmp_path: Path):
    dump_file = tmp_path / "threaddump.log"

    stop_event = heartbeat.start_heartbeat(interval_sec=0.05, dump_file=dump_file)
    assert _wait_for(lambda: _dump_has_content(dump_file))
    thread = next(t for t in threading.enumerate() if t.name == "heartbeat")

    stop_event.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
