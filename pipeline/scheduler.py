"""Local daily scheduler for the complete Rardar refresh cycle."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.refresh import refresh


def parse_clock(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError):
        raise ValueError("scheduled time must use HH:MM") from None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("scheduled time must be a valid 24-hour clock")
    return hour, minute


def next_run_at(now: datetime, hour: int, minute: int, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    local_now = now.astimezone(zone)
    target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local_now:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def _write_status(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_cycle(data_dir: Path, analyze_top: int) -> dict[str, object]:
    started = datetime.now(timezone.utc)
    try:
        catalog = refresh(data_dir, started, limit=30, analyze_top=analyze_top)
        return {
            "state": "healthy",
            "lastRunStartedAt": started.isoformat(),
            "lastRunCompletedAt": datetime.now(timezone.utc).isoformat(),
            "lastError": None,
            "candidateCount": catalog["sourceCount"],
            "projectCount": catalog["projectCount"],
            "signalCount": catalog.get("signalCount", 0),
        }
    except Exception as error:
        return {
            "state": "failed",
            "lastRunStartedAt": started.isoformat(),
            "lastRunCompletedAt": datetime.now(timezone.utc).isoformat(),
            "lastError": str(error),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Rardar refresh every day")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--at", default="08:00", help="local daily time in HH:MM")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--analyze-top", type=int, default=5)
    parser.add_argument("--status-path", type=Path, help="write scheduler heartbeat outside the data snapshot tree")
    parser.add_argument("--skip-initial", action="store_true", help="wait until the next scheduled time before the first refresh")
    parser.add_argument("--once", action="store_true", help="run one refresh and exit")
    arguments = parser.parse_args()

    hour, minute = parse_clock(arguments.at)
    analyze_top = max(0, min(arguments.analyze_top, 10))
    status_path = arguments.status_path or arguments.data_dir / "scheduler" / "status.json"

    if arguments.once:
        status = run_cycle(arguments.data_dir, analyze_top)
        status["schedule"] = {"time": arguments.at, "timezone": arguments.timezone}
        status["nextRunAt"] = None
        _write_status(status_path, status)
        print(json.dumps(status, ensure_ascii=False))
        return

    last_status: dict[str, object] = {
        "state": "scheduled",
        "lastRunStartedAt": None,
        "lastRunCompletedAt": None,
        "lastError": None,
    }
    if not arguments.skip_initial:
        last_status = run_cycle(arguments.data_dir, analyze_top)

    while True:
        target = next_run_at(datetime.now(timezone.utc), hour, minute, arguments.timezone)
        status = {
            **last_status,
            "processId": os.getpid(),
            "heartbeatAt": datetime.now(timezone.utc).isoformat(),
            "schedule": {"time": arguments.at, "timezone": arguments.timezone},
            "nextRunAt": target.isoformat(),
        }
        _write_status(status_path, status)
        print(f"next Rardar refresh: {target.isoformat()}", flush=True)

        while True:
            now = datetime.now(timezone.utc)
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                break
            status["heartbeatAt"] = now.isoformat()
            _write_status(status_path, status)
            time.sleep(min(60, remaining))

        last_status = run_cycle(arguments.data_dir, analyze_top)


if __name__ == "__main__":
    main()
