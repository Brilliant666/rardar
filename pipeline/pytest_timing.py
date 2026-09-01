"""Write bounded, machine-readable pytest timing evidence for Verify."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_started_at: datetime | None = None
_started_clock: float | None = None
_durations: dict[str, dict[str, float]] = defaultdict(dict)
_outcomes: dict[str, str] = {}


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def pytest_sessionstart(session: Any) -> None:
    del session
    global _started_at, _started_clock
    _started_at = datetime.now(timezone.utc)
    _started_clock = time.perf_counter()
    _durations.clear()
    _outcomes.clear()


def pytest_runtest_logreport(report: Any) -> None:
    phases = _durations[report.nodeid]
    phases[report.when] = phases.get(report.when, 0.0) + float(report.duration)
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    elif report.skipped and _outcomes.get(report.nodeid) != "failed":
        _outcomes[report.nodeid] = "skipped"
    elif report.when == "call" and report.passed and report.nodeid not in _outcomes:
        _outcomes[report.nodeid] = "passed"


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    output = os.environ.get("RARDAR_PYTEST_TIMING_PATH")
    if not output:
        return
    completed_at = datetime.now(timezone.utc)
    started_at = _started_at or completed_at
    elapsed = max(0.0, time.perf_counter() - (_started_clock or time.perf_counter()))
    collected = int(getattr(session, "testscollected", 0))
    failed = sum(outcome == "failed" for outcome in _outcomes.values())
    skipped = sum(outcome == "skipped" for outcome in _outcomes.values())
    passed = sum(outcome == "passed" for outcome in _outcomes.values())
    not_run = max(0, collected - passed - failed - skipped)
    top_slow = []
    for node_id, phases in _durations.items():
        duration = sum(phases.values())
        top_slow.append(
            {
                "nodeId": node_id,
                "outcome": _outcomes.get(node_id, "not_run"),
                "durationSeconds": round(duration, 6),
                "setupSeconds": round(phases.get("setup", 0.0), 6),
                "callSeconds": round(phases.get("call", 0.0), 6),
                "teardownSeconds": round(phases.get("teardown", 0.0), 6),
            }
        )
    top_slow.sort(key=lambda item: (-item["durationSeconds"], item["nodeId"]))
    payload = {
        "schemaVersion": 1,
        "startedAt": _timestamp(started_at),
        "completedAt": _timestamp(completed_at),
        "totalDurationSeconds": round(elapsed, 3),
        "exitStatus": int(exitstatus),
        "collected": collected,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "notRun": not_run,
        "topSlowTests": top_slow[:50],
    }
    target = Path(output).expanduser().absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
