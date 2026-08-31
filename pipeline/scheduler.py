"""Local daily scheduler for the complete Rardar refresh cycle."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from pipeline.analyze_repository import RemoteCloneLifecycleError
from pipeline.audit_data import audit_data
from pipeline.data_lock import data_dir_lock, data_dir_lock_path
from pipeline.derive_trending_explosion import derive_trending_explosion
from pipeline.trending_discover import TrendingDiscoverError, derive_trending_discover
from pipeline.generations import GenerationProtocolError, resolve_current_generation
from pipeline.producer_schedule import (
    EXPLOSION_SCHEDULE_AT,
    OBSERVATION_CADENCE_MINUTES,
    OBSERVATION_STARTUP_TOLERANCE_MINUTES,
    ScheduledEvent,
    first_exact_eligible_at,
    next_daily_at,
    next_observation_at,
    next_scheduled_events,
    startup_observation_catch_up,
)
from pipeline.refresh import refresh
from pipeline.retention import (
    RetentionError,
    StorageSnapshot,
    apply_retention_plan,
    audit_retention,
    create_retention_plan,
    recover_pending_retention_transactions,
    storage_snapshot,
    write_retention_plan,
)
from pipeline.runtime_logging import StructuredLogger, new_run_id, process_run_id
from pipeline.runtime_settings import (
    SCHEDULER_ALREADY_RUNNING_EXIT_CODE,
    RuntimeSettings,
    RuntimeSettingsError,
    default_scheduler_status_path,
    load_runtime_settings,
    validate_schedule_at,
)
from pipeline.trending_explosion import TrendingExplosionError
from pipeline.trending_observations import (
    DEFAULT_LIMIT as TRENDING_OBSERVATION_LIMIT,
    TrendingObservationError,
    capture_path_for_scheduled_at,
    load_capture,
    observation_error_retryable,
    run_observer,
)


MAX_REFRESH_ATTEMPTS = 3
RETRY_DELAY_MINUTES = 5
OBSERVATION_RETRY_DELAY_SECONDS = 30
STATUS_HEARTBEAT_SECONDS = 15
EXPLOSION_CATCH_UP_HOURS = 12
SCHEDULER_LOGGER = StructuredLogger("scheduler")

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


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


def _merge_status(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_status(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class SchedulerStatusStore:
    """Serialize in-process scheduler telemetry without losing sibling fields."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._payload: dict[str, Any] = dict(_read_status(path))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._payload)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            _merge_status(self._payload, patch)
            _write_status(self.path, self._payload)
            return copy.deepcopy(self._payload)

    def replace_refresh(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Replace top-level refresh telemetry while retaining Producer state."""

        with self._lock:
            producer = copy.deepcopy(self._payload.get("producer"))
            self._payload = copy.deepcopy(payload)
            if isinstance(producer, dict):
                self._payload["producer"] = producer
            _write_status(self.path, self._payload)
            return copy.deepcopy(self._payload)


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
    *,
    status_store: SchedulerStatusStore | None = None,
    clock: Clock | None = None,
) -> dict[str, object]:
    now = clock or (lambda: datetime.now(timezone.utc))
    started = now().astimezone(timezone.utc)
    store = status_store or (SchedulerStatusStore(status_path) if status_path else None)
    previous_status = store.snapshot() if store else {}
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
    if store:
        store.replace_refresh(running_status)

        def keep_heartbeat_fresh() -> None:
            while not heartbeat_stop.wait(15):
                heartbeat_at = now().astimezone(timezone.utc).isoformat()
                running_status["heartbeatAt"] = heartbeat_at
                try:
                    store.update({"heartbeatAt": heartbeat_at})
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
        completed_at = now().astimezone(timezone.utc).isoformat()
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
            "lastRunCompletedAt": now().astimezone(timezone.utc).isoformat(),
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

    if store:
        completed = now().astimezone(timezone.utc)
        hour, minute = parse_clock(schedule_time)
        result.update(
            {
                "processId": os.getpid(),
                "heartbeatAt": completed.isoformat(),
                "schedule": {"time": schedule_time, "timezone": timezone_name},
                "nextRunAt": next_run_at(completed, hour, minute, timezone_name).isoformat(),
            }
        )
        store.replace_refresh(result)
    return result


def _utc_now(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None:
        raise RuntimeError("scheduler clock returned a timezone-naive instant")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _default_producer_status(
    settings: RuntimeSettings,
    now: datetime,
) -> dict[str, Any]:
    enabled = settings.trending_producer_enabled
    discover_enabled = enabled and settings.trending_discover_enabled
    next_observation = (
        next_observation_at(now, settings.schedule_timezone) if enabled else None
    )
    next_explosion = (
        next_daily_at(now, EXPLOSION_SCHEDULE_AT, settings.schedule_timezone)
        if enabled
        else None
    )
    return {
        "enabled": enabled,
        "state": "warming_up" if enabled else "disabled",
        "nextObservationAt": _iso(next_observation) if next_observation else None,
        "nextExplosionAt": _iso(next_explosion) if next_explosion else None,
        "discover": {
            "enabled": discover_enabled,
            "state": "scheduled" if discover_enabled else "disabled",
            "lastScheduledAt": None,
            "startedAt": None,
            "completedAt": None,
            "latestCaptureId": None,
            "generationId": None,
            "stageCounts": None,
            "publishedCount": None,
            "conflictCount": None,
            "excludedExactCount": None,
            "todayExactCount": None,
            "todayPublishedCount": None,
            "excludedPublishedCount": None,
            "exactOutsidePublishedEvaluatedCount": None,
            "preExactEvaluatedCount": None,
            "suppressedSignalCount": None,
            "suppressionCounts": None,
            "coverage": None,
            "lastErrorCode": None,
            "nextExpectedAt": _iso(next_observation) if next_observation else None,
        },
        "retention": {
            "enabled": enabled and settings.retention_enabled,
            "state": "scheduled" if enabled and settings.retention_enabled else "disabled",
            "lastPlannedAt": None,
            "lastAppliedAt": None,
            "lastPlanDigest": None,
            "deletedFiles": 0,
            "deletedBytes": 0,
            "protectedFiles": None,
            "protectedBytes": None,
            "errorCode": None,
            "nextExpectedAt": (
                _iso(next_daily_at(now, EXPLOSION_SCHEDULE_AT, settings.schedule_timezone))
                if enabled and settings.retention_enabled
                else None
            ),
        },
        "storage": {
            "usedPercent": None,
            "freeBytes": None,
            "warningThreshold": settings.storage_warning_percent,
            "hardThreshold": settings.storage_hard_percent,
            "minimumFreeBytes": settings.storage_minimum_free_bytes,
            "guardState": "unknown",
            "errorCode": None,
        },
        "first08CaptureAt": None,
        "firstExactEligibleAt": None,
        "observation": {
            "state": "scheduled" if enabled else "disabled",
            "cadenceMinutes": OBSERVATION_CADENCE_MINUTES,
            "timezone": settings.schedule_timezone,
            "lastScheduledAt": None,
            "lastStartedAt": None,
            "lastCompletedAt": None,
            "lastCaptureId": None,
            "windowEligible": None,
            "coverageState": None,
            "successfulQueryCount": None,
            "failedQueryCount": None,
            "candidateCount": None,
            "observationCount": None,
            "metadataFailureCount": None,
            "carryForwardCount": None,
            "newRepositoryCount": None,
            "captureDelaySeconds": None,
            "retryCount": 0,
            "lastErrorCode": None,
            "nextRunAt": _iso(next_observation) if next_observation else None,
        },
        "explosion": {
            "state": "scheduled" if enabled else "disabled",
            "scheduleAt": EXPLOSION_SCHEDULE_AT,
            "timezone": settings.schedule_timezone,
            "lastWindowEnd": None,
            "lastStartedAt": None,
            "lastCompletedAt": None,
            "generationId": None,
            "windowState": None,
            "coverageState": None,
            "exactCount": None,
            "pendingCount": None,
            "conflictCount": None,
            "lastErrorCode": None,
            "nextRunAt": _iso(next_explosion) if next_explosion else None,
        },
    }


def _restore_producer_status(
    stored: object,
    settings: RuntimeSettings,
    now: datetime,
) -> dict[str, Any]:
    """Recover only the reviewed, path-free Producer telemetry fields."""

    status = _default_producer_status(settings, now)
    if not settings.trending_producer_enabled or not isinstance(stored, dict):
        return status
    scalar_types = (str, int, float, bool, type(None))
    for key in ("state", "first08CaptureAt", "firstExactEligibleAt"):
        value = stored.get(key)
        if isinstance(value, scalar_types):
            status[key] = value
    for section_name in ("observation", "explosion", "discover", "retention", "storage"):
        if section_name == "discover" and not settings.trending_discover_enabled:
            continue
        if section_name == "retention" and not settings.retention_enabled:
            continue
        section = stored.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in tuple(status[section_name]):
            if key in {
                "cadenceMinutes",
                "timezone",
                "scheduleAt",
                "nextRunAt",
                "enabled",
                "nextExpectedAt",
                "warningThreshold",
                "hardThreshold",
                "minimumFreeBytes",
            }:
                continue
            value = section.get(key)
            if isinstance(value, scalar_types) or (
                section_name == "discover" and isinstance(value, dict)
            ):
                status[section_name][key] = value
    return status


def _producer_summary_state(producer: dict[str, Any]) -> str:
    if producer.get("enabled") is not True:
        return "disabled"
    observation_state = producer["observation"].get("state")
    explosion_state = producer["explosion"].get("state")
    discover_state = producer["discover"].get("state")
    retention_state = producer["retention"].get("state")
    storage_state = producer["storage"].get("guardState")
    if observation_state in {"failed", "degraded", "skipped_overlap"}:
        return "degraded"
    if explosion_state in {"blocked", "degraded", "not_ready"}:
        return "degraded"
    if discover_state in {"failed", "degraded", "blocked"}:
        return "degraded"
    if retention_state in {"failed", "degraded"} or storage_state in {"warning", "blocked"}:
        return "degraded"
    if explosion_state in {"healthy", "already_derived"}:
        return "healthy"
    return "warming_up"


def _publish_producer(
    store: SchedulerStatusStore,
    producer: dict[str, Any],
) -> None:
    producer["state"] = _producer_summary_state(producer)
    store.update({"producer": producer})


def _update_storage_status(
    data_dir: Path,
    settings: RuntimeSettings,
    store: SchedulerStatusStore,
    producer: dict[str, Any],
    *,
    run_id: str | None = None,
) -> StorageSnapshot | None:
    storage = producer["storage"]
    previous = storage.get("guardState")
    try:
        measured = storage_snapshot(data_dir, settings)
    except OSError:
        storage.update(
            {
                "usedPercent": None,
                "freeBytes": None,
                "warningThreshold": settings.storage_warning_percent,
                "hardThreshold": settings.storage_hard_percent,
                "minimumFreeBytes": settings.storage_minimum_free_bytes,
                "guardState": "blocked",
                "errorCode": "storage_measurement_failed",
            }
        )
        if previous != "blocked":
            SCHEDULER_LOGGER.emit(
                "storage_guard_blocked",
                state="blocked",
                level="error",
                run_id=run_id,
                errorCode="storage_measurement_failed",
            )
        _publish_producer(store, producer)
        return None
    storage.update(
        {
            "usedPercent": measured.used_percent,
            "freeBytes": measured.free_bytes,
            "warningThreshold": measured.warning_threshold,
            "hardThreshold": measured.hard_threshold,
            "minimumFreeBytes": measured.minimum_free_bytes,
            "guardState": measured.guard_state,
            "errorCode": None,
        }
    )
    if measured.guard_state in {"warning", "blocked"} and previous != measured.guard_state:
        SCHEDULER_LOGGER.emit(
            "storage_warning",
            state=measured.guard_state,
            level="warning",
            run_id=run_id,
            diskUsedPercent=measured.used_percent,
            diskFreeBytes=measured.free_bytes,
        )
    _publish_producer(store, producer)
    return measured


def _mark_discover_disabled(
    scheduled_at: datetime,
    settings: RuntimeSettings,
    store: SchedulerStatusStore,
    producer: dict[str, Any],
) -> None:
    discover = producer["discover"]
    discover.update(
        {
            "enabled": False,
            "state": "disabled",
            "lastScheduledAt": _iso(scheduled_at),
            "startedAt": None,
            "completedAt": _iso(scheduled_at),
            "lastErrorCode": None,
            "nextExpectedAt": _iso(
                next_observation_at(scheduled_at, settings.schedule_timezone)
            ),
        }
    )
    _publish_producer(store, producer)
    SCHEDULER_LOGGER.emit(
        "discover_disabled",
        state="disabled",
        scheduledAt=_iso(scheduled_at),
    )


def _run_retention_phase(
    data_dir: Path,
    scheduled_at: datetime,
    settings: RuntimeSettings,
    store: SchedulerStatusStore,
    producer: dict[str, Any],
    *,
    clock: Clock,
) -> dict[str, Any] | None:
    retention = producer["retention"]
    if not settings.retention_enabled:
        retention.update({"enabled": False, "state": "disabled", "errorCode": None})
        _publish_producer(store, producer)
        return None
    scheduled_day = scheduled_at.astimezone(ZoneInfo(settings.schedule_timezone)).date()
    previous_applied = _parse_datetime(retention.get("lastAppliedAt"))
    if (
        previous_applied is not None
        and previous_applied.astimezone(ZoneInfo(settings.schedule_timezone)).date()
        == scheduled_day
        and retention.get("state") in {"healthy", "no_op", "already_completed"}
    ):
        retention["state"] = "already_completed"
        _publish_producer(store, producer)
        return {"state": "already_completed", "noOp": True}
    run_id = new_run_id()
    started = _utc_now(clock)
    retention.update({"state": "planning", "errorCode": None})
    _publish_producer(store, producer)
    try:
        recover_pending_retention_transactions(
            data_dir,
            runtime_dir=store.path.parent,
        )
        release_roots: tuple[Path, ...] = ()
        configured_home = os.environ.get("RARDAR_HOME")
        if configured_home:
            resolved_home = Path(configured_home).expanduser().resolve()
            if (
                resolved_home.parent.name == "releases"
                and len(resolved_home.name) == 40
                and all(character in "0123456789abcdef" for character in resolved_home.name)
            ):
                release_roots = (resolved_home.parent,)
        backup_roots = (
            (Path(os.environ["RARDAR_BACKUP_DIR"]).expanduser().resolve(),)
            if os.environ.get("RARDAR_BACKUP_DIR")
            else ()
        )
        plan = create_retention_plan(
            data_dir,
            settings,
            now=started,
            release_roots=release_roots,
            backup_roots=backup_roots,
        )
        plan_path = store.path.parent / "retention" / "latest-plan.json"
        write_retention_plan(plan_path, plan)
        retention.update(
            {
                "state": "planned",
                "lastPlannedAt": _iso(started),
                "lastPlanDigest": plan["planDigest"],
                "protectedFiles": plan["summary"]["protectedFiles"],
                "protectedBytes": plan["summary"]["protectedBytes"],
            }
        )
        _publish_producer(store, producer)
        SCHEDULER_LOGGER.emit(
            "retention_plan_created",
            state="completed",
            run_id=run_id,
            operationId=plan["planDigest"],
            candidateCount=plan["summary"]["plannedDeletions"],
        )
        SCHEDULER_LOGGER.emit(
            "retention_apply_started",
            state="running",
            run_id=run_id,
            operationId=plan["planDigest"],
        )
        with _producer_heartbeat(store, clock):
            result = apply_retention_plan(
                data_dir,
                plan,
                plan["planDigest"],
                settings,
                runtime_dir=store.path.parent,
            )
        audit = audit_retention(data_dir, settings, now=_utc_now(clock))
        if audit.get("status") != "healthy":
            raise RetentionError(
                str(audit.get("errorCode", "retention_audit_failed")),
                "retention post-apply audit failed",
            )
        completed = _utc_now(clock)
        no_op = bool(result.get("noOp"))
        retention.update(
            {
                "state": "no_op" if no_op else "healthy",
                "lastAppliedAt": _iso(completed),
                "deletedFiles": result.get("deletedFiles", 0),
                "deletedBytes": result.get("deletedBytes", 0),
                "errorCode": None,
                "nextExpectedAt": _iso(
                    next_daily_at(
                        completed,
                        EXPLOSION_SCHEDULE_AT,
                        settings.schedule_timezone,
                    )
                ),
            }
        )
        _update_storage_status(data_dir, settings, store, producer, run_id=run_id)
        SCHEDULER_LOGGER.emit(
            "retention_apply_completed",
            state=retention["state"],
            run_id=run_id,
            operationId=plan["planDigest"],
            completedAt=_iso(completed),
            durationMs=max(0, int((completed - started).total_seconds() * 1000)),
            retentionDeletedFiles=result.get("deletedFiles", 0),
            retentionDeletedBytes=result.get("deletedBytes", 0),
        )
        return result
    except (RetentionError, OSError, ValueError) as error:
        completed = _utc_now(clock)
        code = str(getattr(error, "code", "retention_apply_failed"))[:100]
        retention.update(
            {
                "state": "failed",
                "errorCode": code,
                "nextExpectedAt": _iso(
                    next_daily_at(
                        completed,
                        EXPLOSION_SCHEDULE_AT,
                        settings.schedule_timezone,
                    )
                ),
            }
        )
        _publish_producer(store, producer)
        SCHEDULER_LOGGER.emit(
            "retention_apply_failed",
            state="failed",
            level="error",
            run_id=run_id,
            errorCode=code,
            durationMs=max(0, int((completed - started).total_seconds() * 1000)),
        )
        return None


@contextmanager
def _producer_heartbeat(
    store: SchedulerStatusStore,
    clock: Clock,
) -> Iterator[None]:
    stop = threading.Event()

    def keep_fresh() -> None:
        emitted_at = 0.0
        while not stop.wait(STATUS_HEARTBEAT_SECONDS):
            try:
                heartbeat = _utc_now(clock)
                store.update({"heartbeatAt": _iso(heartbeat)})
                monotonic_now = time.monotonic()
                if monotonic_now - emitted_at >= 60:
                    SCHEDULER_LOGGER.emit(
                        "scheduler_heartbeat",
                        state="healthy",
                        run_id=process_run_id(),
                        completedAt=_iso(heartbeat),
                    )
                    emitted_at = monotonic_now
            except OSError:
                pass

    thread = threading.Thread(
        target=keep_fresh,
        name="rardar-producer-heartbeat",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)


def _wait_until(
    target: datetime,
    store: SchedulerStatusStore,
    *,
    clock: Clock,
    sleeper: Sleeper,
) -> None:
    target = target.astimezone(timezone.utc)
    while True:
        now = _utc_now(clock)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            return
        store.update({"heartbeatAt": _iso(now)})
        sleeper(min(STATUS_HEARTBEAT_SECONDS, remaining))


def _run_observation_phase(
    data_dir: Path,
    scheduled_at: datetime,
    settings: RuntimeSettings,
    store: SchedulerStatusStore,
    producer: dict[str, Any],
    *,
    clock: Clock,
    sleeper: Sleeper,
) -> dict[str, Any] | None:
    started = _utc_now(clock)
    run_id = new_run_id()
    SCHEDULER_LOGGER.emit(
        "observation_scheduled",
        state="scheduled",
        run_id=run_id,
        scheduledAt=_iso(scheduled_at),
    )
    SCHEDULER_LOGGER.emit(
        "observation_started",
        state="running",
        run_id=run_id,
        scheduledAt=_iso(scheduled_at),
        startedAt=_iso(started),
    )
    observation = producer["observation"]
    observation.update(
        {
            "state": "running",
            "lastScheduledAt": _iso(scheduled_at),
            "lastStartedAt": _iso(started),
            "lastCompletedAt": None,
            "lastErrorCode": None,
            "retryCount": 0,
        }
    )
    _publish_producer(store, producer)
    deadline = scheduled_at.astimezone(timezone.utc) + timedelta(
        minutes=OBSERVATION_STARTUP_TOLERANCE_MINUTES
    )
    attempts = 0
    while True:
        attempts += 1
        try:
            with _producer_heartbeat(store, clock):
                result = run_observer(
                    data_dir=data_dir,
                    scheduled_at=scheduled_at,
                    timezone_name=settings.schedule_timezone,
                    limit=TRENDING_OBSERVATION_LIMIT,
                    dry_run=False,
                    token=os.environ.get("GITHUB_TOKEN"),
                    clock=clock,
                )
        except TrendingObservationError as error:
            now = _utc_now(clock)
            retry_at = min(
                now + timedelta(seconds=OBSERVATION_RETRY_DELAY_SECONDS),
                deadline,
            )
            if attempts == 1 and observation_error_retryable(error) and now < deadline:
                observation.update(
                    {
                        "state": "retrying",
                        "retryCount": 1,
                        "lastErrorCode": error.code,
                    }
                )
                _publish_producer(store, producer)
                _wait_until(retry_at, store, clock=clock, sleeper=sleeper)
                continue
            observation.update(
                {
                    "state": "failed",
                    "lastCompletedAt": _iso(now),
                    "lastErrorCode": error.code,
                    "nextRunAt": _iso(
                        next_observation_at(now, settings.schedule_timezone)
                    ),
                }
            )
            producer["nextObservationAt"] = observation["nextRunAt"]
            _publish_producer(store, producer)
            SCHEDULER_LOGGER.emit(
                "observation_failed",
                state="failed",
                level="error",
                run_id=run_id,
                scheduledAt=_iso(scheduled_at),
                completedAt=_iso(now),
                durationMs=max(0, int((now - started).total_seconds() * 1000)),
                retryCount=observation["retryCount"],
                retryable=observation_error_retryable(error),
                errorCode=error.code,
            )
            return None
        except Exception:
            now = _utc_now(clock)
            observation.update(
                {
                    "state": "failed",
                    "lastCompletedAt": _iso(now),
                    "lastErrorCode": "observation_internal_error",
                    "nextRunAt": _iso(
                        next_observation_at(now, settings.schedule_timezone)
                    ),
                }
            )
            producer["nextObservationAt"] = observation["nextRunAt"]
            _publish_producer(store, producer)
            SCHEDULER_LOGGER.emit(
                "observation_failed",
                state="failed",
                level="error",
                run_id=run_id,
                scheduledAt=_iso(scheduled_at),
                completedAt=_iso(now),
                durationMs=max(0, int((now - started).total_seconds() * 1000)),
                retryCount=observation["retryCount"],
                retryable=False,
                errorCode="observation_internal_error",
            )
            return None
        break

    completed = _utc_now(clock)
    result_state = str(result.get("state"))
    coverage_state = result.get("coverageState")
    window_eligible = result.get("windowEligible")
    if result_state == "skipped_overlap":
        telemetry_state = "skipped_overlap"
    elif coverage_state == "degraded" or window_eligible is False:
        telemetry_state = "degraded"
    else:
        telemetry_state = "healthy"
    next_run = next_observation_at(completed, settings.schedule_timezone)
    observation.update(
        {
            "state": telemetry_state,
            "lastCompletedAt": _iso(completed),
            "lastCaptureId": result.get("captureId"),
            "windowEligible": window_eligible,
            "coverageState": coverage_state,
            "successfulQueryCount": result.get("successfulQueryCount"),
            "failedQueryCount": result.get("failedQueryCount"),
            "candidateCount": result.get("candidateCount"),
            "observationCount": result.get("observationCount"),
            "metadataFailureCount": result.get("metadataFailureCount"),
            "carryForwardCount": result.get("carryForwardCount"),
            "newRepositoryCount": result.get("newRepositoryCount"),
            "captureDelaySeconds": result.get("captureDelaySeconds"),
            "lastErrorCode": None,
            "nextRunAt": _iso(next_run),
        }
    )
    producer["nextObservationAt"] = observation["nextRunAt"]
    local = scheduled_at.astimezone(ZoneInfo(settings.schedule_timezone))
    if (
        local.hour == 8
        and local.minute == 0
        and window_eligible is True
        and producer.get("first08CaptureAt") is None
    ):
        producer["first08CaptureAt"] = _iso(scheduled_at)
        producer["firstExactEligibleAt"] = _iso(
            first_exact_eligible_at(scheduled_at)
        )
    _publish_producer(store, producer)
    if result_state == "skipped_overlap":
        SCHEDULER_LOGGER.emit(
            "scheduler_overlap_skipped",
            state="skipped",
            level="warning",
            run_id=run_id,
            scheduledAt=_iso(scheduled_at),
            errorCode="skipped_overlap",
        )
    SCHEDULER_LOGGER.emit(
        "observation_completed",
        state=telemetry_state,
        run_id=run_id,
        scheduledAt=_iso(scheduled_at),
        startedAt=_iso(started),
        completedAt=_iso(completed),
        durationMs=max(0, int((completed - started).total_seconds() * 1000)),
        captureId=result.get("captureId"),
        querySuccessCount=result.get("successfulQueryCount"),
        queryFailureCount=result.get("failedQueryCount"),
        candidateCount=result.get("candidateCount"),
        observationCount=result.get("observationCount"),
        metadataFailureCount=result.get("metadataFailureCount"),
        retryCount=observation["retryCount"],
    )
    return result


def _run_explosion_phase(
    data_dir: Path,
    window_end: datetime,
    settings: RuntimeSettings,
    store: SchedulerStatusStore,
    producer: dict[str, Any],
    *,
    clock: Clock,
) -> dict[str, Any] | None:
    started = _utc_now(clock)
    run_id = new_run_id()
    SCHEDULER_LOGGER.emit(
        "explosion_started",
        state="running",
        run_id=run_id,
        scheduledAt=_iso(window_end),
        startedAt=_iso(started),
    )
    explosion = producer["explosion"]
    explosion.update(
        {
            "state": "running",
            "lastWindowEnd": _iso(window_end),
            "lastStartedAt": _iso(started),
            "lastCompletedAt": None,
            "lastErrorCode": None,
        }
    )
    _publish_producer(store, producer)
    try:
        with _producer_heartbeat(store, clock):
            result = derive_trending_explosion(data_dir, window_end)
    except (GenerationProtocolError, TrendingExplosionError, OSError, ValueError) as error:
        completed = _utc_now(clock)
        code = str(getattr(error, "code", "explosion_derivation_failed"))[:100]
        explosion.update(
            {
                "state": "blocked",
                "lastCompletedAt": _iso(completed),
                "lastErrorCode": code,
                "nextRunAt": _iso(
                    next_daily_at(
                        completed,
                        EXPLOSION_SCHEDULE_AT,
                        settings.schedule_timezone,
                    )
                ),
            }
        )
        producer["nextExplosionAt"] = explosion["nextRunAt"]
        _publish_producer(store, producer)
        SCHEDULER_LOGGER.emit(
            "explosion_failed",
            state="blocked",
            level="error",
            run_id=run_id,
            scheduledAt=_iso(window_end),
            completedAt=_iso(completed),
            durationMs=max(0, int((completed - started).total_seconds() * 1000)),
            errorCode=code,
        )
        return None
    except Exception:
        completed = _utc_now(clock)
        explosion.update(
            {
                "state": "blocked",
                "lastCompletedAt": _iso(completed),
                "lastErrorCode": "explosion_internal_error",
                "nextRunAt": _iso(
                    next_daily_at(
                        completed,
                        EXPLOSION_SCHEDULE_AT,
                        settings.schedule_timezone,
                    )
                ),
            }
        )
        producer["nextExplosionAt"] = explosion["nextRunAt"]
        _publish_producer(store, producer)
        SCHEDULER_LOGGER.emit(
            "explosion_failed",
            state="blocked",
            level="error",
            run_id=run_id,
            scheduledAt=_iso(window_end),
            completedAt=_iso(completed),
            durationMs=max(0, int((completed - started).total_seconds() * 1000)),
            errorCode="explosion_internal_error",
        )
        return None

    completed = _utc_now(clock)
    window_state = result.get("windowState")
    if result.get("state") == "already_derived":
        telemetry_state = "already_derived"
    elif window_state == "exact":
        telemetry_state = "healthy"
    elif window_state in {"warming_up", "baseline_missing"}:
        telemetry_state = "warming_up"
    else:
        telemetry_state = "healthy"
    next_run = next_daily_at(
        completed,
        EXPLOSION_SCHEDULE_AT,
        settings.schedule_timezone,
    )
    explosion.update(
        {
            "state": telemetry_state,
            "lastCompletedAt": _iso(completed),
            "generationId": result.get("generationId"),
            "windowState": window_state,
            "coverageState": result.get("coverageState"),
            "exactCount": result.get("exactCount"),
            "pendingCount": result.get("pendingCount"),
            "conflictCount": result.get("conflictCount"),
            "lastErrorCode": None,
            "nextRunAt": _iso(next_run),
        }
    )
    producer["nextExplosionAt"] = explosion["nextRunAt"]
    _publish_producer(store, producer)
    SCHEDULER_LOGGER.emit(
        "explosion_completed",
        state=telemetry_state,
        run_id=run_id,
        scheduledAt=_iso(window_end),
        startedAt=_iso(started),
        completedAt=_iso(completed),
        durationMs=max(0, int((completed - started).total_seconds() * 1000)),
        generationId=result.get("generationId"),
        exactCount=result.get("exactCount"),
        pendingCount=result.get("pendingCount"),
        conflictCount=result.get("conflictCount"),
    )
    return result


def _run_discover_phase(
    data_dir: Path,
    scheduled_at: datetime,
    settings: RuntimeSettings,
    store: SchedulerStatusStore,
    producer: dict[str, Any],
    *,
    clock: Clock,
    capacity: StorageSnapshot | None = None,
) -> dict[str, Any] | None:
    """Derive Discover after the phase's core producer work, without coupling failure."""

    if not settings.trending_discover_enabled:
        _mark_discover_disabled(scheduled_at, settings, store, producer)
        return None
    started = _utc_now(clock)
    run_id = new_run_id()
    discover = producer["discover"]
    if capacity is None:
        capacity = _update_storage_status(
            data_dir,
            settings,
            store,
            producer,
            run_id=run_id,
        )
    if capacity is None or capacity.guard_state == "blocked":
        completed = _utc_now(clock)
        storage = producer["storage"]
        error_code = "discover_storage_guard"
        discover.update(
            {
                "state": "blocked",
                "lastScheduledAt": _iso(scheduled_at),
                "startedAt": None,
                "completedAt": _iso(completed),
                "lastErrorCode": error_code,
                "nextExpectedAt": _iso(
                    next_observation_at(completed, settings.schedule_timezone)
                ),
            }
        )
        _publish_producer(store, producer)
        SCHEDULER_LOGGER.emit(
            "storage_guard_blocked",
            state="blocked",
            level="warning",
            run_id=run_id,
            scheduledAt=_iso(scheduled_at),
            diskUsedPercent=storage.get("usedPercent"),
            diskFreeBytes=storage.get("freeBytes"),
            errorCode=error_code,
        )
        SCHEDULER_LOGGER.emit(
            "discover_failed",
            state="blocked",
            level="warning",
            run_id=run_id,
            scheduledAt=_iso(scheduled_at),
            errorCode=error_code,
        )
        return None
    SCHEDULER_LOGGER.emit(
        "discover_started",
        state="running",
        run_id=run_id,
        scheduledAt=_iso(scheduled_at),
        startedAt=_iso(started),
        diskUsedPercent=capacity.used_percent,
        diskFreeBytes=capacity.free_bytes,
    )
    discover.update(
        {
            "state": "running",
            "lastScheduledAt": _iso(scheduled_at),
            "startedAt": _iso(started),
            "completedAt": None,
            "lastErrorCode": None,
        }
    )
    _publish_producer(store, producer)
    try:
        with _producer_heartbeat(store, clock):
            result = derive_trending_discover(data_dir)
    except (GenerationProtocolError, TrendingDiscoverError, OSError, ValueError) as error:
        completed = _utc_now(clock)
        code = str(getattr(error, "code", "discover_derivation_failed"))[:100]
        discover.update(
            {
                "state": "failed",
                "completedAt": _iso(completed),
                "lastErrorCode": code,
                "nextExpectedAt": _iso(
                    next_observation_at(completed, settings.schedule_timezone)
                ),
            }
        )
        _publish_producer(store, producer)
        SCHEDULER_LOGGER.emit(
            "discover_failed",
            state="failed",
            level="error",
            run_id=run_id,
            scheduledAt=_iso(scheduled_at),
            completedAt=_iso(completed),
            durationMs=max(0, int((completed - started).total_seconds() * 1000)),
            errorCode=code,
        )
        return None
    except Exception:
        completed = _utc_now(clock)
        discover.update(
            {
                "state": "failed",
                "completedAt": _iso(completed),
                "lastErrorCode": "discover_internal_error",
                "nextExpectedAt": _iso(
                    next_observation_at(completed, settings.schedule_timezone)
                ),
            }
        )
        _publish_producer(store, producer)
        SCHEDULER_LOGGER.emit(
            "discover_failed",
            state="failed",
            level="error",
            run_id=run_id,
            scheduledAt=_iso(scheduled_at),
            completedAt=_iso(completed),
            durationMs=max(0, int((completed - started).total_seconds() * 1000)),
            errorCode="discover_internal_error",
        )
        return None
    completed = _utc_now(clock)
    coverage = result.get("coverage")
    suppression = result.get("suppressionSummary")
    suppression_reasons = (
        suppression.get("reasons") if isinstance(suppression, dict) else None
    )
    state = "degraded" if result.get("coverageState") == "degraded" else "healthy"
    discover.update(
        {
            "state": state,
            "completedAt": _iso(completed),
            "latestCaptureId": result.get("latestCaptureId"),
            "generationId": result.get("generationId"),
            "stageCounts": result.get("stageCounts"),
            "publishedCount": result.get("publishedCount"),
            "conflictCount": result.get("conflictCount"),
            "excludedExactCount": result.get("excludedExactCount"),
            "todayExactCount": result.get("todayExactCount"),
            "todayPublishedCount": result.get("todayPublishedCount"),
            "excludedPublishedCount": result.get("excludedPublishedCount"),
            "exactOutsidePublishedEvaluatedCount": result.get(
                "exactOutsidePublishedEvaluatedCount"
            ),
            "preExactEvaluatedCount": result.get("preExactEvaluatedCount"),
            "suppressedSignalCount": (
                suppression.get("suppressedSignalCount")
                if isinstance(suppression, dict)
                else None
            ),
            "suppressionCounts": (
                suppression_reasons if isinstance(suppression_reasons, dict) else None
            ),
            "coverage": coverage if isinstance(coverage, dict) else None,
            "lastErrorCode": None,
            "nextExpectedAt": _iso(
                next_observation_at(completed, settings.schedule_timezone)
            ),
        }
    )
    _publish_producer(store, producer)
    SCHEDULER_LOGGER.emit(
        "discover_completed",
        state=state,
        run_id=run_id,
        scheduledAt=_iso(scheduled_at),
        startedAt=_iso(started),
        completedAt=_iso(completed),
        durationMs=max(0, int((completed - started).total_seconds() * 1000)),
        captureId=result.get("latestCaptureId"),
        generationId=result.get("generationId"),
        discoverStageCounts=result.get("stageCounts"),
        candidateCount=(coverage.get("candidateCount") if isinstance(coverage, dict) else None),
        excludedPublishedCount=result.get("excludedPublishedCount"),
        suppressedCount=(
            suppression.get("suppressedSignalCount")
            if isinstance(suppression, dict)
            else None
        ),
    )
    return result


def _run_refresh_sequence(
    arguments: argparse.Namespace,
    settings: RuntimeSettings,
    store: SchedulerStatusStore,
    *,
    clock: Clock,
    sleeper: Sleeper,
) -> dict[str, object]:
    attempts = 0
    started = _utc_now(clock)
    run_id = new_run_id()
    SCHEDULER_LOGGER.emit(
        "refresh_started",
        state="running",
        run_id=run_id,
        startedAt=_iso(started),
    )
    while True:
        result = run_cycle(
            arguments.data_dir,
            max(0, min(arguments.analyze_top, 10)),
            store.path,
            settings.schedule_at,
            settings.schedule_timezone,
            status_store=store,
            clock=clock,
        )
        attempts += 1
        if not should_retry(
            result.get("state"),
            attempts,
            retryable=result.get("retryable", True),
        ):
            completed = _utc_now(clock)
            failed = result.get("state") == "failed"
            SCHEDULER_LOGGER.emit(
                "refresh_failed" if failed else "refresh_completed",
                state=str(result.get("state", "failed" if failed else "completed")),
                level="error" if failed else "info",
                run_id=run_id,
                startedAt=_iso(started),
                completedAt=_iso(completed),
                durationMs=max(0, int((completed - started).total_seconds() * 1000)),
                generationId=result.get("currentGenerationId"),
                candidateId=result.get("candidateGenerationId"),
                retryCount=attempts - 1,
                retryable=result.get("retryable"),
                errorCode=(
                    result.get("generationErrorCode")
                    or result.get("remoteAnalysisErrorCode")
                    or ("refresh_failed" if failed else None)
                ),
            )
            return result
        retry_at = _utc_now(clock) + timedelta(minutes=RETRY_DELAY_MINUTES)
        store.update(
            {
                "heartbeatAt": _iso(_utc_now(clock)),
                "nextRunAt": _iso(retry_at),
                "retryAttempt": attempts + 1,
            }
        )
        SCHEDULER_LOGGER.emit(
            "scheduler_tick",
            state="retrying",
            level="warning",
            scheduledAt=_iso(retry_at),
            retryCount=attempts + 1,
        )
        _wait_until(retry_at, store, clock=clock, sleeper=sleeper)


def _startup_explosion_window(
    now: datetime,
    settings: RuntimeSettings,
) -> datetime | None:
    local_now = now.astimezone(ZoneInfo(settings.schedule_timezone))
    target = local_now.replace(hour=8, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc
    )
    elapsed = now.astimezone(timezone.utc) - target
    if timedelta(0) <= elapsed <= timedelta(hours=EXPLOSION_CATCH_UP_HOURS):
        return target
    return None


def _eligible_capture_exists(data_dir: Path, scheduled_at: datetime) -> tuple[bool, str | None]:
    path = capture_path_for_scheduled_at(data_dir, scheduled_at)
    if not os.path.lexists(path):
        return False, "explosion_current_capture_missing"
    try:
        capture = load_capture(path)
    except (OSError, TrendingObservationError, ValueError):
        return False, "explosion_current_capture_invalid"
    if capture.get("windowEligible") is not True:
        return False, "explosion_current_capture_ineligible"
    return True, None


def _record_explosion_not_ready(
    window_end: datetime,
    error_code: str,
    settings: RuntimeSettings,
    store: SchedulerStatusStore,
    producer: dict[str, Any],
    now: datetime,
) -> None:
    next_run = next_daily_at(
        now,
        EXPLOSION_SCHEDULE_AT,
        settings.schedule_timezone,
    )
    producer["explosion"].update(
        {
            "state": "not_ready",
            "lastWindowEnd": _iso(window_end),
            "lastStartedAt": None,
            "lastCompletedAt": _iso(now),
            "generationId": None,
            "windowState": None,
            "coverageState": None,
            "exactCount": None,
            "pendingCount": None,
            "conflictCount": None,
            "lastErrorCode": error_code,
            "nextRunAt": _iso(next_run),
        }
    )
    producer["nextExplosionAt"] = _iso(next_run)
    _publish_producer(store, producer)


def _execute_scheduled_events(
    events: tuple[ScheduledEvent, ...],
    arguments: argparse.Namespace,
    settings: RuntimeSettings,
    store: SchedulerStatusStore,
    producer: dict[str, Any],
    *,
    clock: Clock,
    sleeper: Sleeper,
) -> dict[str, object] | None:
    """Execute one fixed phase serially in its declared priority order."""

    refresh_result: dict[str, object] | None = None
    SCHEDULER_LOGGER.emit(
        "scheduler_tick",
        state="running",
        run_id=process_run_id(),
        scheduledAt=_iso(events[0].scheduled_at),
        operations=[event.kind for event in events],
    )
    observation_completed = False
    explosion_required = any(event.kind == "explosion" for event in events)
    explosion_completed = not explosion_required
    for event in events:
        if event.kind == "observation":
            observation_completed = _run_observation_phase(
                arguments.data_dir,
                event.scheduled_at,
                settings,
                store,
                producer,
                clock=clock,
                sleeper=sleeper,
            ) is not None
        elif event.kind == "refresh":
            refresh_result = _run_refresh_sequence(
                arguments,
                settings,
                store,
                clock=clock,
                sleeper=sleeper,
            )
        elif event.kind == "explosion":
            explosion_completed = _run_explosion_phase(
                arguments.data_dir,
                event.scheduled_at,
                settings,
                store,
                producer,
                clock=clock,
            ) is not None
        elif event.kind == "discover":
            if not settings.trending_discover_enabled:
                _mark_discover_disabled(
                    event.scheduled_at,
                    settings,
                    store,
                    producer,
                )
            elif observation_completed and explosion_completed:
                capacity = _update_storage_status(
                    arguments.data_dir,
                    settings,
                    store,
                    producer,
                )
                if (
                    settings.retention_enabled
                    and producer["storage"].get("guardState") in {"warning", "blocked"}
                    and not any(item.kind == "retention" for item in events)
                ):
                    _run_retention_phase(
                        arguments.data_dir,
                        event.scheduled_at,
                        settings,
                        store,
                        producer,
                        clock=clock,
                    )
                    capacity = _update_storage_status(
                        arguments.data_dir,
                        settings,
                        store,
                        producer,
                    )
                _run_discover_phase(
                    arguments.data_dir,
                    event.scheduled_at,
                    settings,
                    store,
                    producer,
                    clock=clock,
                    capacity=capacity,
                )
        elif event.kind == "retention":
            _run_retention_phase(
                arguments.data_dir,
                event.scheduled_at,
                settings,
                store,
                producer,
                clock=clock,
            )
    return refresh_result


def _run_producer_scheduler(
    arguments: argparse.Namespace,
    settings: RuntimeSettings,
    status_path: Path,
    *,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
) -> None:
    now_fn = clock or (lambda: datetime.now(timezone.utc))
    sleep_fn = sleeper or time.sleep
    store = SchedulerStatusStore(status_path)
    now = _utc_now(now_fn)
    stored = store.snapshot()
    producer = _restore_producer_status(stored.get("producer"), settings, now)
    last_status: dict[str, object] = {
        "state": stored.get("state", "scheduled"),
        "lastRunStartedAt": stored.get("lastRunStartedAt"),
        "lastRunCompletedAt": stored.get("lastRunCompletedAt"),
        "lastSuccessfulRefreshAt": _last_successful_refresh_at(stored),
        "lastError": stored.get("lastError"),
        "retryable": stored.get("retryable", True),
    }
    store.update(
        {
            **last_status,
            "processId": os.getpid(),
            "heartbeatAt": _iso(now),
            "schedule": {
                "time": settings.schedule_at,
                "timezone": settings.schedule_timezone,
            },
            "nextRunAt": _iso(
                next_daily_at(now, settings.schedule_at, settings.schedule_timezone)
            ),
            "retryAttempt": None,
            "producer": producer,
        }
    )
    SCHEDULER_LOGGER.emit(
        "process_started",
        state="running",
        run_id=process_run_id(),
        component="scheduler",
    )
    SCHEDULER_LOGGER.emit(
        "scheduler_started",
        state="healthy",
        run_id=process_run_id(),
        scheduledAt=_iso(
            next_daily_at(now, settings.schedule_at, settings.schedule_timezone)
        ),
        discoverEnabled=settings.trending_discover_enabled,
        retentionEnabled=settings.retention_enabled,
    )
    _update_storage_status(arguments.data_dir, settings, store, producer)

    startup_now = _utc_now(now_fn)
    startup_phase = (
        startup_observation_catch_up(startup_now, settings.schedule_timezone)
        if arguments.skip_initial
        else None
    )
    startup_observation_result: dict[str, Any] | None = None
    startup_is_eight = bool(
        startup_phase is not None
        and startup_phase.astimezone(ZoneInfo(settings.schedule_timezone)).hour == 8
    )
    if startup_is_eight and startup_phase is not None:
        startup_observation_result = _run_observation_phase(
            arguments.data_dir,
            startup_phase,
            settings,
            store,
            producer,
            clock=now_fn,
            sleeper=sleep_fn,
        )

    refresh_catch_up = arguments.skip_initial and should_catch_up(
        now,
        last_status.get("lastRunCompletedAt"),
        last_status.get("state"),
        *parse_clock(settings.schedule_at),
        settings.schedule_timezone,
        latest_snapshot_at=committed_refresh_at(arguments.data_dir),
        retryable=last_status.get("retryable", True),
    )
    if not arguments.skip_initial or refresh_catch_up:
        last_status = _run_refresh_sequence(
            arguments,
            settings,
            store,
            clock=now_fn,
            sleeper=sleep_fn,
        )

    if startup_phase is not None and not startup_is_eight:
        startup_observation_result = _run_observation_phase(
            arguments.data_dir,
            startup_phase,
            settings,
            store,
            producer,
            clock=now_fn,
            sleeper=sleep_fn,
        )
        if startup_observation_result is not None:
            capacity = _update_storage_status(
                arguments.data_dir,
                settings,
                store,
                producer,
            )
            if (
                settings.retention_enabled
                and producer["storage"].get("guardState") in {"warning", "blocked"}
            ):
                _run_retention_phase(
                    arguments.data_dir,
                    startup_phase,
                    settings,
                    store,
                    producer,
                    clock=now_fn,
                )
                capacity = _update_storage_status(
                    arguments.data_dir,
                    settings,
                    store,
                    producer,
                )
            _run_discover_phase(
                arguments.data_dir,
                startup_phase,
                settings,
                store,
                producer,
                clock=now_fn,
                capacity=capacity,
            )

    explosion_window = _startup_explosion_window(_utc_now(now_fn), settings)
    if explosion_window is not None:
        eligible, error_code = _eligible_capture_exists(arguments.data_dir, explosion_window)
        if eligible:
            explosion_result = _run_explosion_phase(
                arguments.data_dir,
                explosion_window,
                settings,
                store,
                producer,
                clock=now_fn,
            )
            if explosion_result is not None:
                _run_discover_phase(
                    arguments.data_dir,
                    explosion_window,
                    settings,
                    store,
                    producer,
                    clock=now_fn,
                )
        elif error_code is not None:
            _record_explosion_not_ready(
                explosion_window,
                error_code,
                settings,
                store,
                producer,
                _utc_now(now_fn),
            )
        _run_retention_phase(
            arguments.data_dir,
            explosion_window,
            settings,
            store,
            producer,
            clock=now_fn,
        )

    while True:
        now = _utc_now(now_fn)
        events = next_scheduled_events(
            now,
            refresh_at=settings.schedule_at,
            timezone_name=settings.schedule_timezone,
        )
        target = events[0].scheduled_at
        next_observation = next_observation_at(now, settings.schedule_timezone)
        next_explosion = next_daily_at(
            now,
            EXPLOSION_SCHEDULE_AT,
            settings.schedule_timezone,
        )
        producer["observation"]["nextRunAt"] = _iso(next_observation)
        producer["explosion"]["nextRunAt"] = _iso(next_explosion)
        producer["nextObservationAt"] = _iso(next_observation)
        producer["nextExplosionAt"] = _iso(next_explosion)
        producer["discover"]["nextExpectedAt"] = _iso(next_observation)
        store.update(
            {
                "processId": os.getpid(),
                "heartbeatAt": _iso(now),
                "nextRunAt": _iso(
                    next_daily_at(now, settings.schedule_at, settings.schedule_timezone)
                ),
                "retryAttempt": None,
                "producer": producer,
            }
        )
        SCHEDULER_LOGGER.emit(
            "scheduler_tick",
            state="scheduled",
            run_id=process_run_id(),
            scheduledAt=_iso(target),
            operations=[event.kind for event in events],
        )
        _wait_until(target, store, clock=now_fn, sleeper=sleep_fn)
        refresh_result = _execute_scheduled_events(
            events,
            arguments,
            settings,
            store,
            producer,
            clock=now_fn,
            sleeper=sleep_fn,
        )
        if refresh_result is not None:
            last_status = refresh_result


def _run_scheduler(
    arguments: argparse.Namespace,
    settings: RuntimeSettings,
    status_path: Path,
) -> None:
    if settings.trending_producer_enabled and not arguments.once:
        _run_producer_scheduler(arguments, settings, status_path)
        return

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

    SCHEDULER_LOGGER.emit(
        "process_started",
        state="running",
        run_id=process_run_id(),
        component="scheduler",
    )
    SCHEDULER_LOGGER.emit(
        "scheduler_started",
        state="healthy",
        run_id=process_run_id(),
        scheduledAt=next_run_at(
            datetime.now(timezone.utc),
            hour,
            minute,
            schedule_timezone,
        ).isoformat(),
        discoverEnabled=False,
        retentionEnabled=False,
    )

    def logged_cycle() -> dict[str, object]:
        started = datetime.now(timezone.utc)
        run_id = new_run_id()
        SCHEDULER_LOGGER.emit(
            "refresh_started",
            state="running",
            run_id=run_id,
            startedAt=started.isoformat(),
        )
        try:
            result = run_cycle(
                arguments.data_dir,
                analyze_top,
                status_path,
                schedule_at,
                schedule_timezone,
            )
        except Exception:
            completed = datetime.now(timezone.utc)
            SCHEDULER_LOGGER.emit(
                "refresh_failed",
                state="failed",
                level="error",
                run_id=run_id,
                startedAt=started.isoformat(),
                completedAt=completed.isoformat(),
                durationMs=max(0, int((completed - started).total_seconds() * 1000)),
                errorCode="refresh_internal_error",
            )
            raise
        completed = datetime.now(timezone.utc)
        failed = result.get("state") == "failed"
        SCHEDULER_LOGGER.emit(
            "refresh_failed" if failed else "refresh_completed",
            state=str(result.get("state", "failed" if failed else "completed")),
            level="error" if failed else "info",
            run_id=run_id,
            startedAt=started.isoformat(),
            completedAt=completed.isoformat(),
            durationMs=max(0, int((completed - started).total_seconds() * 1000)),
            generationId=result.get("currentGenerationId"),
            candidateId=result.get("candidateGenerationId"),
            retryable=result.get("retryable"),
            errorCode=(
                result.get("generationErrorCode")
                or result.get("remoteAnalysisErrorCode")
                or ("refresh_failed" if failed else None)
            ),
        )
        return result

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
        last_status = logged_cycle()
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
        SCHEDULER_LOGGER.emit(
            "scheduler_tick",
            state="retrying" if retrying else "scheduled",
            level="warning" if retrying else "info",
            run_id=process_run_id(),
            scheduledAt=target.isoformat(),
            retryCount=attempts_in_cycle + 1 if retrying else 0,
        )

        while True:
            now = datetime.now(timezone.utc)
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                break
            status["heartbeatAt"] = now.isoformat()
            _write_status(status_path, status)
            SCHEDULER_LOGGER.emit(
                "scheduler_heartbeat",
                state="healthy",
                run_id=process_run_id(),
                completedAt=now.isoformat(),
            )
            time.sleep(min(60, remaining))

        last_status = logged_cycle()
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
            try:
                _run_scheduler(arguments, settings, status_path)
            finally:
                SCHEDULER_LOGGER.emit(
                    "process_stopping",
                    state="stopping",
                    run_id=process_run_id(),
                    component="scheduler",
                )
                SCHEDULER_LOGGER.emit(
                    "process_stopped",
                    state="stopped",
                    run_id=process_run_id(),
                    component="scheduler",
                )
    except SchedulerAlreadyRunningError as error:
        SCHEDULER_LOGGER.emit(
            "scheduler_overlap_skipped",
            state="skipped_overlap",
            level="warning",
            run_id=process_run_id(),
            errorCode="scheduler_already_running",
        )
        raise SystemExit(SCHEDULER_ALREADY_RUNNING_EXIT_CODE) from None
    except Exception as error:
        SCHEDULER_LOGGER.emit(
            "scheduler_failed",
            state="failed",
            level="critical",
            run_id=process_run_id(),
            errorCode=str(getattr(error, "code", "scheduler_unexpected_exit"))[:100],
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
