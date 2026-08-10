"""Local daily scheduler for the complete Rardar refresh cycle."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from pipeline.analyze_repository import RemoteCloneLifecycleError
from pipeline.audit_data import audit_data
from pipeline.data_lock import data_dir_lock, data_dir_lock_path
from pipeline.generations import GenerationProtocolError, resolve_current_generation
from pipeline.refresh import refresh
from pipeline.runtime_settings import (
    SCHEDULER_ALREADY_RUNNING_EXIT_CODE,
    RuntimeSettings,
    RuntimeSettingsError,
    default_scheduler_status_path,
    load_runtime_settings,
    validate_schedule_at,
)


MAX_REFRESH_ATTEMPTS = 3
RETRY_DELAY_MINUTES = 5


class SchedulerAlreadyRunningError(RuntimeError):
    """The canonical data directory already has a scheduler owner."""


def scheduler_instance_lock_path(data_dir: Path, lock_root: Path | None = None) -> Path:
    writer_lock = data_dir_lock_path(data_dir, lock_root=lock_root)
    return writer_lock.parent / "scheduler-instances" / writer_lock.name


@contextmanager
def scheduler_instance_lock(
    data_dir: Path,
    *,
    lock_root: Path | None = None,
) -> Iterator[None]:
    lock_path = scheduler_instance_lock_path(data_dir, lock_root)
    guard = data_dir_lock(data_dir, lock_root=lock_path.parent, timeout=0)
    try:
        guard.__enter__()
    except TimeoutError as error:
        raise SchedulerAlreadyRunningError(
            f"a Rardar scheduler already owns data directory: {data_dir.expanduser().resolve()}"
        ) from error
    try:
        yield
    finally:
        guard.__exit__(None, None, None)


def parse_clock(value: str) -> tuple[int, int]:
    try:
        canonical = validate_schedule_at(value)
    except RuntimeSettingsError as error:
        raise ValueError(str(error)) from None
    hour_text, minute_text = canonical.split(":", 1)
    hour, minute = int(hour_text), int(minute_text)
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


def _last_successful_refresh_at(status: dict[str, object]) -> str | None:
    value = status.get("lastSuccessfulRefreshAt")
    if _parse_datetime(value) is None and status.get("state") == "healthy":
        value = status.get("lastRunCompletedAt")
    return value if isinstance(value, str) and _parse_datetime(value) is not None else None


def should_catch_up(
    now: datetime,
    last_run_completed_at: object,
    last_state: object,
    hour: int,
    minute: int,
    timezone_name: str,
    window_hours: int = 12,
    latest_snapshot_at: object = None,
    retryable: object = True,
) -> bool:
    """Return whether today's missed or failed scheduled run should resume."""
    now = now.astimezone(timezone.utc)
    target = scheduled_run_for_local_day(now, hour, minute, timezone_name)
    elapsed = now - target
    if elapsed.total_seconds() < 0 or elapsed > timedelta(hours=max(1, window_hours)):
        return False
    # A failed run never advances current. Most failures can build a fresh
    # candidate in the same catch-up window. An unresolved remote-analysis
    # lifecycle must not retry in that cycle, but it must not poison later
    # scheduled days; only a trustworthy completion before today's target can
    # establish that the non-retryable failure belongs to an older cycle.
    if last_state == "failed":
        if retryable is not False:
            return True
        completed = _parse_datetime(last_run_completed_at)
        return completed is not None and completed < target
    committed_snapshot = _parse_datetime(latest_snapshot_at)
    if committed_snapshot and target <= committed_snapshot <= now + timedelta(hours=2):
        return False
    completed = _parse_datetime(last_run_completed_at)
    return completed is None or completed < target


def should_retry(
    last_state: object,
    attempts_in_cycle: int,
    max_attempts: int = MAX_REFRESH_ATTEMPTS,
    *,
    retryable: object = True,
) -> bool:
    return (
        retryable is not False
        and last_state == "failed"
        and 0 < attempts_in_cycle < max(1, max_attempts)
    )


def _remote_clone_lifecycle_error(error: BaseException) -> RemoteCloneLifecycleError | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, RemoteCloneLifecycleError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


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
    """Return the capture time only for one verified published generation."""
    if os.path.lexists(data_dir / "current.json"):
        try:
            root = resolve_current_generation(data_dir).root
        except GenerationProtocolError:
            return None
    else:
        # Pre-generation tests and local legacy trees keep their old coherent
        # four-file marker. Once a pointer exists, strict generation
        # resolution above is mandatory and never falls back to these files.
        root = data_dir
    snapshot_at = _read_status(root / "snapshots" / "latest.json").get("captured_at")
    catalog_at = _read_status(root / "catalog" / "latest.json").get("capturedAt")
    signal_at = _read_status(root / "signals" / "latest.json").get("capturedAt")
    queue_at = _read_status(root / "queues" / "codex.json").get("generatedAt")
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
    previous_status = _read_status(status_path) if status_path else {}
    last_successful_refresh_at = _last_successful_refresh_at(previous_status)
    heartbeat_stop = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    running_status: dict[str, object] = {
        "state": "running",
        "lastRunStartedAt": started.isoformat(),
        "lastRunCompletedAt": None,
        "lastSuccessfulRefreshAt": last_successful_refresh_at,
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
        if os.path.lexists(data_dir / "current.json"):
            # The direct audit below is the semantic gate for this status
            # update; avoid running the same full audit twice.
            published = resolve_current_generation(data_dir, verify_audit=False)
            published_root = published.root
            current_generation_id = published.generation_id
        else:
            published_root = data_dir
            current_generation_id = None
        audit = audit_data(published_root)
        if audit["status"] == "failed":
            codes = ", ".join(str(item.get("code")) for item in audit["issues"][:5])
            raise RuntimeError(f"data audit failed after refresh: {codes}")
        completed_at = datetime.now(timezone.utc).isoformat()
        result: dict[str, object] = {
            "state": "healthy",
            "lastRunStartedAt": started.isoformat(),
            "lastRunCompletedAt": completed_at,
            "lastSuccessfulRefreshAt": completed_at,
            "lastError": None,
            "candidateCount": catalog["sourceCount"],
            "projectCount": catalog["projectCount"],
            "signalCount": catalog.get("signalCount", 0),
            "dataAuditStatus": audit["status"],
            "dataAuditWarningCount": audit["warningCount"],
            "currentGenerationId": current_generation_id,
            "dataAuditSummary": {
                "observedProjectCount": audit.get("observedProjectCount", 0),
                "observedNetStarChange": audit.get("observedNetStarChange", 0),
                "dailyTrackCounts": audit.get("dailyTrackCounts"),
                "historyCount": audit.get("historyCount", 0),
                "successfulQueryCount": audit.get("successfulQueryCount"),
                "failedQueryCount": audit.get("failedQueryCount"),
                "healthySourceCount": audit.get("healthySourceCount"),
                "failedSourceCount": audit.get("failedSourceCount"),
                "analysisFailureCount": audit.get("analysisFailureCount", 0),
                "staticAnalysisRequiredCount": audit.get("staticAnalysisRequiredCount", 0),
            },
        }
    except Exception as error:
        lifecycle_error = _remote_clone_lifecycle_error(error)
        result = {
            "state": "failed",
            "lastRunStartedAt": started.isoformat(),
            "lastRunCompletedAt": datetime.now(timezone.utc).isoformat(),
            "lastSuccessfulRefreshAt": last_successful_refresh_at,
            "lastError": str(error),
            "retryable": lifecycle_error is None,
        }
        if lifecycle_error is not None:
            result["remoteAnalysisErrorCode"] = lifecycle_error.code
        if isinstance(error, GenerationProtocolError):
            result.update(
                {
                    "generationErrorCode": error.code,
                    "candidateGenerationId": error.generation_id,
                    "generationStage": error.stage,
                }
            )
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


def _run_scheduler(
    arguments: argparse.Namespace,
    settings: RuntimeSettings,
    status_path: Path,
) -> None:
    schedule_at = settings.schedule_at
    schedule_timezone = settings.schedule_timezone
    hour, minute = parse_clock(schedule_at)
    analyze_top = max(0, min(arguments.analyze_top, 10))

    if arguments.once:
        status = run_cycle(
            arguments.data_dir,
            analyze_top,
            status_path,
            schedule_at,
            schedule_timezone,
        )
        status["schedule"] = {"time": schedule_at, "timezone": schedule_timezone}
        status["nextRunAt"] = None
        _write_status(status_path, status)
        print(json.dumps(status, ensure_ascii=False))
        return

    stored_status = _read_status(status_path)
    last_status: dict[str, object] = {
        "state": stored_status.get("state", "scheduled"),
        "lastRunStartedAt": stored_status.get("lastRunStartedAt"),
        "lastRunCompletedAt": stored_status.get("lastRunCompletedAt"),
        "lastSuccessfulRefreshAt": _last_successful_refresh_at(stored_status),
        "lastError": stored_status.get("lastError"),
        "retryable": stored_status.get("retryable", True),
    }
    catch_up = arguments.skip_initial and should_catch_up(
        datetime.now(timezone.utc),
        last_status.get("lastRunCompletedAt"),
        last_status.get("state"),
        hour,
        minute,
        schedule_timezone,
        latest_snapshot_at=committed_refresh_at(arguments.data_dir),
        retryable=last_status.get("retryable", True),
    )
    attempts_in_cycle = 0
    if not arguments.skip_initial or catch_up:
        last_status = run_cycle(
            arguments.data_dir,
            analyze_top,
            status_path,
            schedule_at,
            schedule_timezone,
        )
        attempts_in_cycle = 1 if last_status.get("state") == "failed" else 0

    while True:
        retrying = should_retry(
            last_status.get("state"),
            attempts_in_cycle,
            retryable=last_status.get("retryable", True),
        )
        if retrying:
            target = datetime.now(timezone.utc) + timedelta(minutes=RETRY_DELAY_MINUTES)
        else:
            target = next_run_at(datetime.now(timezone.utc), hour, minute, schedule_timezone)
        status = {
            **last_status,
            "processId": os.getpid(),
            "heartbeatAt": datetime.now(timezone.utc).isoformat(),
            "schedule": {"time": schedule_at, "timezone": schedule_timezone},
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
            schedule_at,
            schedule_timezone,
        )
        if last_status.get("state") == "failed":
            attempts_in_cycle = attempts_in_cycle + 1 if retrying else 1
        else:
            attempts_in_cycle = 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Rardar refresh every day")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("RARDAR_DATA_DIR", "data")),
    )
    parser.add_argument("--at", help="local daily time in HH:MM; overrides RARDAR_SCHEDULE_AT")
    parser.add_argument("--timezone", help="IANA timezone; overrides RARDAR_SCHEDULE_TIMEZONE")
    parser.add_argument("--analyze-top", type=int, default=5)
    parser.add_argument("--status-path", type=Path, help="write scheduler heartbeat outside the data snapshot tree")
    parser.add_argument("--skip-initial", action="store_true", help="wait until the next scheduled time before the first refresh")
    parser.add_argument("--once", action="store_true", help="run one refresh and exit")
    arguments = parser.parse_args()

    try:
        settings = load_runtime_settings(
            schedule_at=arguments.at,
            schedule_timezone=arguments.timezone,
        )
    except RuntimeSettingsError as error:
        parser.error(str(error))
    status_path = arguments.status_path or default_scheduler_status_path()
    try:
        with scheduler_instance_lock(arguments.data_dir):
            _run_scheduler(arguments, settings, status_path)
    except SchedulerAlreadyRunningError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(SCHEDULER_ALREADY_RUNNING_EXIT_CODE) from None


if __name__ == "__main__":
    main()
