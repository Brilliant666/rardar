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
    for section_name in ("observation", "explosion"):
        section = stored.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in tuple(status[section_name]):
            if key in {"cadenceMinutes", "timezone", "scheduleAt", "nextRunAt"}:
                continue
            value = section.get(key)
            if isinstance(value, scalar_types):
                status[section_name][key] = value
    return status


def _producer_summary_state(producer: dict[str, Any]) -> str:
    if producer.get("enabled") is not True:
        return "disabled"
    observation_state = producer["observation"].get("state")
    explosion_state = producer["explosion"].get("state")
    if observation_state in {"failed", "degraded", "skipped_overlap"}:
        return "degraded"
    if explosion_state in {"blocked", "degraded", "not_ready"}:
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


@contextmanager
def _producer_heartbeat(
    store: SchedulerStatusStore,
    clock: Clock,
) -> Iterator[None]:
    stop = threading.Event()

    def keep_fresh() -> None:
        while not stop.wait(STATUS_HEARTBEAT_SECONDS):
            try:
                store.update({"heartbeatAt": _iso(_utc_now(clock))})
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
            print(f"Rardar observation failed: {error.code}", flush=True)
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
            print("Rardar observation failed: observation_internal_error", flush=True)
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
        print(f"Rardar explosion derive blocked: {code}", flush=True)
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
        print("Rardar explosion derive blocked: explosion_internal_error", flush=True)
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
            return result
        retry_at = _utc_now(clock) + timedelta(minutes=RETRY_DELAY_MINUTES)
        store.update(
            {
                "heartbeatAt": _iso(_utc_now(clock)),
                "nextRunAt": _iso(retry_at),
                "retryAttempt": attempts + 1,
            }
        )
        print(f"next Rardar refresh retry: {_iso(retry_at)}", flush=True)
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
    for event in events:
        if event.kind == "observation":
            _run_observation_phase(
                arguments.data_dir,
                event.scheduled_at,
                settings,
                store,
                producer,
                clock=clock,
                sleeper=sleeper,
            )
        elif event.kind == "refresh":
            refresh_result = _run_refresh_sequence(
                arguments,
                settings,
                store,
                clock=clock,
                sleeper=sleeper,
            )
        elif event.kind == "explosion":
            _run_explosion_phase(
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

    startup_now = _utc_now(now_fn)
    startup_phase = (
        startup_observation_catch_up(startup_now, settings.schedule_timezone)
        if arguments.skip_initial
        else None
    )
    if startup_phase is not None:
        _run_observation_phase(
            arguments.data_dir,
            startup_phase,
            settings,
            store,
            producer,
            clock=now_fn,
            sleeper=sleep_fn,
        )

    explosion_window = _startup_explosion_window(_utc_now(now_fn), settings)
    if explosion_window is not None:
        eligible, error_code = _eligible_capture_exists(arguments.data_dir, explosion_window)
        if eligible:
            _run_explosion_phase(
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
        print(
            "next Rardar events: "
            f"{_iso(target)} "
            + ",".join(event.kind for event in events),
            flush=True,
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
