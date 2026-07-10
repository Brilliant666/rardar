"""Local daily scheduler for the complete Rardar refresh cycle."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.audit_data import audit_data
from pipeline.refresh import refresh


MAX_REFRESH_ATTEMPTS = 3
RETRY_DELAY_MINUTES = 5


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


def scheduled_run_for_local_day(now: datetime, hour: int, minute: int, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    local_now = now.astimezone(zone)
    return local_now.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def should_catch_up(
    now: datetime,
    last_run_completed_at: object,
    last_state: object,
    hour: int,
    minute: int,
    timezone_name: str,
    window_hours: int = 12,
    latest_snapshot_at: object = None,
) -> bool:
    """Return whether today's missed or failed scheduled run should resume."""
    now = now.astimezone(timezone.utc)
    target = scheduled_run_for_local_day(now, hour, minute, timezone_name)
    elapsed = now - target
    if elapsed.total_seconds() < 0 or elapsed > timedelta(hours=max(1, window_hours)):
        return False
    committed_snapshot = _parse_datetime(latest_snapshot_at)
    if committed_snapshot and target <= committed_snapshot <= now + timedelta(hours=2):
        return False
    completed = _parse_datetime(last_run_completed_at)
    return last_state == "failed" or completed is None or completed < target


def should_retry(last_state: object, attempts_in_cycle: int, max_attempts: int = MAX_REFRESH_ATTEMPTS) -> bool:
    return last_state == "failed" and 0 < attempts_in_cycle < max(1, max_attempts)


def _write_status(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_status(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def committed_refresh_at(data_dir: Path) -> str | None:
    """Return the capture time only when every scheduled artifact is coherent."""
    snapshot_at = _read_status(data_dir / "snapshots" / "latest.json").get("captured_at")
    catalog_at = _read_status(data_dir / "catalog" / "latest.json").get("capturedAt")
    signal_at = _read_status(data_dir / "signals" / "latest.json").get("capturedAt")
    queue_at = _read_status(data_dir / "queues" / "codex.json").get("generatedAt")
    instants = [_parse_datetime(value) for value in (snapshot_at, catalog_at, signal_at, queue_at)]
    if any(value is None for value in instants):
        return None
    snapshot_instant, catalog_instant, signal_instant, queue_instant = instants
    if snapshot_instant != catalog_instant or signal_instant < snapshot_instant:
        return None
    # Signals can be recollected and Codex processing can regenerate the queue
    # after the GitHub snapshot. Older derived artifacts are incomplete.
    if queue_instant < signal_instant:
        return None
    return str(snapshot_at)


def run_cycle(
    data_dir: Path,
    analyze_top: int,
    status_path: Path | None = None,
    schedule_time: str = "08:00",
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, object]:
    started = datetime.now(timezone.utc)
    heartbeat_stop = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    running_status: dict[str, object] = {
        "state": "running",
        "lastRunStartedAt": started.isoformat(),
        "lastRunCompletedAt": None,
        "lastError": None,
        "processId": os.getpid(),
        "heartbeatAt": started.isoformat(),
        "schedule": {"time": schedule_time, "timezone": timezone_name},
        "nextRunAt": None,
    }
    if status_path:
        _write_status(status_path, running_status)

        def keep_heartbeat_fresh() -> None:
            while not heartbeat_stop.wait(15):
                running_status["heartbeatAt"] = datetime.now(timezone.utc).isoformat()
                try:
                    _write_status(status_path, running_status)
                except OSError:
                    pass

        heartbeat_thread = threading.Thread(target=keep_heartbeat_fresh, name="rardar-refresh-heartbeat", daemon=True)
        heartbeat_thread.start()

    try:
        catalog = refresh(data_dir, started, limit=30, analyze_top=analyze_top)
        audit = audit_data(data_dir)
        if audit["status"] == "failed":
            codes = ", ".join(str(item.get("code")) for item in audit["issues"][:5])
            raise RuntimeError(f"data audit failed after refresh: {codes}")
        result: dict[str, object] = {
            "state": "healthy",
            "lastRunStartedAt": started.isoformat(),
            "lastRunCompletedAt": datetime.now(timezone.utc).isoformat(),
            "lastError": None,
            "candidateCount": catalog["sourceCount"],
            "projectCount": catalog["projectCount"],
            "signalCount": catalog.get("signalCount", 0),
            "dataAuditStatus": audit["status"],
            "dataAuditWarningCount": audit["warningCount"],
        }
    except Exception as error:
        result = {
            "state": "failed",
            "lastRunStartedAt": started.isoformat(),
            "lastRunCompletedAt": datetime.now(timezone.utc).isoformat(),
            "lastError": str(error),
        }
    finally:
        heartbeat_stop.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=2)

    if status_path:
        completed = datetime.now(timezone.utc)
        hour, minute = parse_clock(schedule_time)
        result.update(
            {
                "processId": os.getpid(),
                "heartbeatAt": completed.isoformat(),
                "schedule": {"time": schedule_time, "timezone": timezone_name},
                "nextRunAt": next_run_at(completed, hour, minute, timezone_name).isoformat(),
            }
        )
        _write_status(status_path, result)
    return result


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
        status = run_cycle(
            arguments.data_dir,
            analyze_top,
            status_path,
            arguments.at,
            arguments.timezone,
        )
        status["schedule"] = {"time": arguments.at, "timezone": arguments.timezone}
        status["nextRunAt"] = None
        _write_status(status_path, status)
        print(json.dumps(status, ensure_ascii=False))
        return

    stored_status = _read_status(status_path)
    last_status: dict[str, object] = {
        "state": stored_status.get("state", "scheduled"),
        "lastRunStartedAt": stored_status.get("lastRunStartedAt"),
        "lastRunCompletedAt": stored_status.get("lastRunCompletedAt"),
        "lastError": stored_status.get("lastError"),
    }
    catch_up = arguments.skip_initial and should_catch_up(
        datetime.now(timezone.utc),
        last_status.get("lastRunCompletedAt"),
        last_status.get("state"),
        hour,
        minute,
        arguments.timezone,
        latest_snapshot_at=committed_refresh_at(arguments.data_dir),
    )
    attempts_in_cycle = 0
    if not arguments.skip_initial or catch_up:
        last_status = run_cycle(
            arguments.data_dir,
            analyze_top,
            status_path,
            arguments.at,
            arguments.timezone,
        )
        attempts_in_cycle = 1 if last_status.get("state") == "failed" else 0

    while True:
        retrying = should_retry(last_status.get("state"), attempts_in_cycle)
        if retrying:
            target = datetime.now(timezone.utc) + timedelta(minutes=RETRY_DELAY_MINUTES)
        else:
            target = next_run_at(datetime.now(timezone.utc), hour, minute, arguments.timezone)
        status = {
            **last_status,
            "processId": os.getpid(),
            "heartbeatAt": datetime.now(timezone.utc).isoformat(),
            "schedule": {"time": arguments.at, "timezone": arguments.timezone},
            "nextRunAt": target.isoformat(),
            "retryAttempt": attempts_in_cycle + 1 if retrying else None,
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

        last_status = run_cycle(
            arguments.data_dir,
            analyze_top,
            status_path,
            arguments.at,
            arguments.timezone,
        )
        if last_status.get("state") == "failed":
            attempts_in_cycle = attempts_in_cycle + 1 if retrying else 1
        else:
            attempts_in_cycle = 0


if __name__ == "__main__":
    main()
