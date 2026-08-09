"""Keep the local Rardar website and daily refresh scheduler alive."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pipeline.runtime_settings import (
    DEFAULT_SCHEDULE_AT,
    DEFAULT_SCHEDULE_TIMEZONE,
    DEFAULT_STALE_AFTER_HOURS,
    SCHEDULER_ALREADY_RUNNING_EXIT_CODE,
    RuntimeSettings,
    RuntimeSettingsError,
    RuntimeTimezoneDatabaseError,
    default_runtime_settings,
    load_runtime_settings,
    validate_schedule_at,
    validate_schedule_timezone,
)


ROOT = Path(__file__).resolve().parents[1]


def default_runtime_dir() -> Path:
    configured = os.environ.get("RARDAR_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "Rardar" / "runtime"


RUNTIME_DIR = default_runtime_dir()
LOG_DIR = RUNTIME_DIR / "logs"
CONTROL_PATH = RUNTIME_DIR / "manager.json"
LOCK_PATH = RUNTIME_DIR / "manager.lock"
STATUS_PATH = RUNTIME_DIR / "status.json"
SCHEDULER_STATUS_PATH = RUNTIME_DIR / "scheduler-status.json"
LOCAL_URL = "http://127.0.0.1:3000/"
STATUS_HOST = "127.0.0.1"
STATUS_PORT = 3002
MINIMUM_NODE = (22, 13, 0)
REQUIRED_PYTHON_MODULES = ("jsonschema", "tzdata")
PYTHON_DEPENDENCY_INSTALL_COMMAND = "python -m pip install -r requirements.txt"
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 2
SCHEDULER_HEARTBEAT_MAX_AGE = 125
SERVICE_STARTUP_GRACE = 90
WEBSITE_HEALTH_PATH = "/api/health"
WEBSITE_HEALTH_TIMEOUT = 2
WEBSITE_HEALTH_MAX_BYTES = 64 * 1024
GENERATION_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"
)
RFC3339_WITH_TIMEZONE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_latest_status: dict[str, Any] = {}
_latest_status_lock = threading.Lock()
_UNBOUND_SCHEDULER_PROCESS = object()


def acquire_manager_lock(path: Path = LOCK_PATH) -> Any | None:
    """Acquire a non-blocking process lock that survives PID/status races."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ImportError):
        handle.close()
        return None
    return handle


def release_manager_lock(handle: Any) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def rotate_log(path: Path, max_bytes: int = MAX_LOG_BYTES, backup_count: int = LOG_BACKUP_COUNT) -> None:
    """Bound append-only runtime logs while retaining recent history."""
    try:
        if backup_count < 1 or path.stat().st_size <= max(1, max_bytes):
            return
        oldest = path.with_name(f"{path.name}.{backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(backup_count - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                source.replace(path.with_name(f"{path.name}.{index + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except (FileNotFoundError, OSError):
        # Log maintenance must never prevent the local services from starting.
        return


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_runtime_status(payload: dict[str, Any]) -> None:
    global _latest_status
    with _latest_status_lock:
        _latest_status = payload
    _write_json(STATUS_PATH, payload)


class RuntimeStatusHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler uses HTTP method names.
        if self.path.split("?", 1)[0] != "/status":
            self.send_error(404)
            return
        with _latest_status_lock:
            payload = dict(_latest_status)
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        origin = self.headers.get("Origin")
        if origin in {"http://127.0.0.1:3000", "http://localhost:3000"}:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_arguments: Any) -> None:
        return


class LocalStatusServer(ThreadingHTTPServer):
    allow_reuse_address = True


def start_status_server() -> ThreadingHTTPServer:
    server = LocalStatusServer((STATUS_HOST, STATUS_PORT), RuntimeStatusHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="rardar-status", daemon=True)
    thread.start()
    return server


def parse_node_version(value: str) -> tuple[int, int, int] | None:
    cleaned = value.strip().removeprefix("v").split("-", 1)[0]
    parts = cleaned.split(".")
    if len(parts) < 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _node_version(path: Path) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_node_version(result.stdout)


def find_node() -> Path:
    candidates: list[Path] = []
    configured = os.environ.get("RARDAR_NODE")
    if configured:
        candidates.append(Path(configured))
    discovered = shutil.which("node")
    if discovered:
        candidates.append(Path(discovered))

    if os.name == "nt":
        fnm_dir = Path(os.environ.get("FNM_DIR", Path.home() / "AppData" / "Roaming" / "fnm"))
        candidates.extend(fnm_dir.glob("node-versions/v*/installation/node.exe"))

    valid: list[tuple[tuple[int, int, int], Path]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()) if candidate.exists() else str(candidate)
        if key in seen or not candidate.exists():
            continue
        seen.add(key)
        version = _node_version(candidate)
        if version and version >= MINIMUM_NODE:
            valid.append((version, candidate.resolve()))

    if not valid:
        required = ".".join(str(value) for value in MINIMUM_NODE)
        raise RuntimeError(f"Rardar requires Node.js {required} or newer")
    return max(valid, key=lambda item: item[0])[1]


def missing_python_dependencies(
    required_modules: tuple[str, ...] = REQUIRED_PYTHON_MODULES,
) -> tuple[str, ...]:
    """Return Python modules required by managed services but unavailable here."""
    missing: list[str] = []
    for module_name in required_modules:
        try:
            available = importlib.util.find_spec(module_name) is not None
        except (ImportError, AttributeError, ValueError):
            available = False
        if not available:
            missing.append(module_name)
    return tuple(missing)


def python_dependencies_are_ready() -> bool:
    missing = missing_python_dependencies()
    if not missing:
        return True
    print(f"Rardar is missing required Python dependencies: {', '.join(missing)}")
    print(f"Install them before starting local services: {PYTHON_DEPENDENCY_INSTALL_COMMAND}")
    return False


def process_is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def process_matches(pid: int, markers: tuple[str, ...]) -> bool:
    if not process_is_alive(pid):
        return False
    try:
        if os.name == "nt":
            query = f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine"
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", query],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            command_line = result.stdout
        else:
            command_line = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return False
    lowered = command_line.lower()
    return any(marker.lower() in lowered for marker in markers)


def _terminate_process_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _stop_recorded_processes(status: dict[str, Any], include_manager: bool = True) -> None:
    targets: list[tuple[int, tuple[str, ...]]] = []
    manager_pid = status.get("managerPid")
    if include_manager and isinstance(manager_pid, int):
        targets.append((manager_pid, ("pipeline.runtime run", "pipeline\\runtime.py run")))
    services = status.get("services") or {}
    website_pid = (services.get("website") or {}).get("pid")
    scheduler_pid = (services.get("scheduler") or {}).get("pid")
    if isinstance(website_pid, int):
        targets.append((website_pid, ("vinext",)))
    if isinstance(scheduler_pid, int):
        targets.append((scheduler_pid, ("pipeline.scheduler",)))
    for pid, markers in targets:
        if process_matches(pid, markers):
            _terminate_process_tree(pid)


def heartbeat_is_fresh(checked_at: str | None, now: datetime | None = None, maximum_age: int = 35) -> bool:
    if not checked_at:
        return False
    try:
        checked = datetime.fromisoformat(checked_at)
    except ValueError:
        return False
    reference = now or datetime.now(timezone.utc)
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return 0 <= (reference - checked.astimezone(timezone.utc)).total_seconds() <= maximum_age


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def scheduler_heartbeat_state(
    heartbeat_at: str | None,
    started_at: str | None,
    now: datetime | None = None,
) -> str:
    """Distinguish scheduler startup from a live process with a stale heartbeat."""
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    heartbeat = _parse_utc_timestamp(heartbeat_at)
    started = _parse_utc_timestamp(started_at)
    heartbeat_belongs_to_process = not started or bool(heartbeat and heartbeat >= started)
    if heartbeat_belongs_to_process and heartbeat_is_fresh(
        heartbeat_at,
        reference,
        maximum_age=SCHEDULER_HEARTBEAT_MAX_AGE,
    ):
        return "healthy"
    if started:
        uptime = (reference - started).total_seconds()
        if 0 <= uptime <= SERVICE_STARTUP_GRACE:
            return "starting"
    return "stale"


def port_is_open(host: str = "127.0.0.1", port: int = 3000) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@dataclass(frozen=True)
class WebsiteHealth:
    state: str
    generation_id: str | None = None
    error: str | None = None
    data_freshness: str | None = None
    snapshot_captured_at: str | None = None
    snapshot_age_seconds: float | None = None
    stale_after_seconds: int | None = None
    schedule_at: str | None = None
    schedule_timezone: str | None = None


def _short_health_error(value: object, limit: int = 240) -> str:
    message = " ".join(str(value).split()).strip()
    return (message or "website health check failed")[:limit]


def parse_website_health(status: int, body: bytes) -> WebsiteHealth:
    if len(body) > WEBSITE_HEALTH_MAX_BYTES:
        return WebsiteHealth("degraded", error="health response exceeded 65536 bytes")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return WebsiteHealth("degraded", error=f"health endpoint returned invalid JSON (HTTP {status})")
    if not isinstance(payload, dict):
        return WebsiteHealth("degraded", error=f"health endpoint returned a non-object (HTTP {status})")
    if status != 200:
        detail = payload.get("error") or payload.get("message") or f"HTTP {status}"
        return WebsiteHealth(
            "degraded",
            error=_short_health_error(f"health endpoint returned HTTP {status}: {detail}"),
        )
    generation_id = payload.get("generationId")
    data = payload.get("data")
    schedule = payload.get("schedule")
    if (
        payload.get("schemaVersion") != 1
        or payload.get("status") not in {"healthy", "degraded"}
        or not isinstance(generation_id, str)
        or GENERATION_ID_PATTERN.fullmatch(generation_id) is None
        or not isinstance(data, dict)
        or not isinstance(schedule, dict)
    ):
        return WebsiteHealth("degraded", error="health endpoint returned an invalid contract")
    freshness = data.get("freshness")
    snapshot_captured_at = data.get("snapshotCapturedAt")
    age_seconds = data.get("ageSeconds")
    stale_after_seconds = data.get("staleAfterSeconds")
    if (
        freshness not in {"fresh", "stale"}
        or not isinstance(snapshot_captured_at, str)
        or isinstance(age_seconds, bool)
        or not isinstance(age_seconds, (int, float))
        or not math.isfinite(age_seconds)
        or age_seconds < 0
        or isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, int)
        or stale_after_seconds < 1
    ):
        return WebsiteHealth("degraded", error="health endpoint returned an invalid contract")
    captured = (
        _parse_utc_timestamp(snapshot_captured_at)
        if RFC3339_WITH_TIMEZONE_PATTERN.fullmatch(snapshot_captured_at)
        else None
    )
    if captured is None:
        return WebsiteHealth("degraded", error="health endpoint returned an invalid contract")
    observed_age = (datetime.now(timezone.utc) - captured).total_seconds()
    if observed_age < -300 or abs(max(0, observed_age) - float(age_seconds)) > 15:
        return WebsiteHealth("degraded", error="health endpoint returned an invalid contract")
    # Validate freshness against the age measured by the Worker for this
    # response. The manager clock is necessarily a little later and may cross
    # the exact threshold while the response is in flight.
    expected_freshness = "fresh" if float(age_seconds) <= stale_after_seconds else "stale"
    expected_status = "healthy" if expected_freshness == "fresh" else "degraded"
    expected_reason = None if expected_freshness == "fresh" else "published_data_stale"
    try:
        schedule_at = validate_schedule_at(schedule.get("at"))
        schedule_timezone = validate_schedule_timezone(schedule.get("timezone"))
    except RuntimeSettingsError:
        return WebsiteHealth("degraded", error="health endpoint returned an invalid contract")
    if (
        freshness != expected_freshness
        or payload.get("status") != expected_status
        or payload.get("reason") != expected_reason
    ):
        return WebsiteHealth("degraded", error="health endpoint returned an invalid contract")
    return WebsiteHealth(
        "healthy",
        generation_id=generation_id,
        data_freshness=freshness,
        snapshot_captured_at=snapshot_captured_at,
        snapshot_age_seconds=float(age_seconds),
        stale_after_seconds=stale_after_seconds,
        schedule_at=schedule_at,
        schedule_timezone=schedule_timezone,
    )


def probe_website_health(
    host: str = "127.0.0.1",
    port: int = 3000,
    timeout: float = WEBSITE_HEALTH_TIMEOUT,
) -> WebsiteHealth:
    connection = HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(
            "GET",
            WEBSITE_HEALTH_PATH,
            headers={"Accept": "application/json", "Cache-Control": "no-store"},
        )
        response = connection.getresponse()
        body = response.read(WEBSITE_HEALTH_MAX_BYTES + 1)
        return parse_website_health(response.status, body)
    except (OSError, TimeoutError, HTTPException) as error:
        return WebsiteHealth("degraded", error=_short_health_error(error))
    finally:
        connection.close()


@dataclass
class ManagedService:
    name: str
    command: list[str]
    log_path: Path
    process: subprocess.Popen[bytes] | None = None
    started_at: str | None = None
    restart_count: int = 0
    last_error: str | None = None
    _log_handle: Any = None

    def start(self, environment: dict[str, str]) -> None:
        if self.process is not None:
            self.restart_count += 1
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        rotate_log(self.log_path)
        self._log_handle = self.log_path.open("ab")
        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        self.process = subprocess.Popen(
            self.command,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
        self.started_at = utc_now()
        if self.restart_count == 0:
            self.last_error = None

    def poll(self) -> int | None:
        return self.process.poll() if self.process else None

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None


def _service_payload(service: ManagedService, state: str) -> dict[str, Any]:
    return {
        "state": state,
        "pid": service.process.pid if service.process and service.process.poll() is None else None,
        "startedAt": service.started_at,
        "restartCount": service.restart_count,
        "lastError": service.last_error,
    }


def _effective_schedule(settings: RuntimeSettings) -> dict[str, str]:
    return {"time": settings.schedule_at, "timezone": settings.schedule_timezone}


def _scheduler_details(
    settings: RuntimeSettings | None = None,
    expected_process_id: int | None | object = _UNBOUND_SCHEDULER_PROCESS,
) -> dict[str, Any]:
    stored = _read_json(SCHEDULER_STATUS_PATH) or {}
    resolved = settings or default_runtime_settings()
    reported_process_id = stored.get("processId")
    telemetry_trusted = expected_process_id is _UNBOUND_SCHEDULER_PROCESS or (
        isinstance(expected_process_id, int) and reported_process_id == expected_process_id
    )
    status = stored if telemetry_trusted else {}
    return {
        "refreshState": status.get("state", "scheduled"),
        # The manager configuration is authoritative. The scheduler status is
        # telemetry and can never reconfigure a running child.
        "schedule": _effective_schedule(resolved),
        "nextRunAt": status.get("nextRunAt"),
        "lastRunStartedAt": status.get("lastRunStartedAt"),
        "lastRunCompletedAt": status.get("lastRunCompletedAt"),
        "lastSuccessfulRefreshAt": status.get("lastSuccessfulRefreshAt"),
        "lastError": status.get("lastError"),
        "retryAttempt": status.get("retryAttempt"),
        "heartbeatAt": status.get("heartbeatAt"),
        "dataAuditStatus": status.get("dataAuditStatus"),
        "dataAuditWarningCount": status.get("dataAuditWarningCount"),
        "dataAuditSummary": status.get("dataAuditSummary"),
        "currentGenerationId": status.get("currentGenerationId"),
        "candidateGenerationId": status.get("candidateGenerationId"),
        "generationStage": status.get("generationStage"),
        "generationErrorCode": status.get("generationErrorCode"),
        "retryable": status.get("retryable", True),
        "remoteAnalysisErrorCode": status.get("remoteAnalysisErrorCode"),
        "telemetryTrusted": telemetry_trusted,
        "reportedProcessId": reported_process_id,
    }


def _stopped_status(
    message: str = "本地运行管理器未启动",
    settings: RuntimeSettings | None = None,
) -> dict[str, Any]:
    resolved = settings or default_runtime_settings()
    return {
        "schemaVersion": 1,
        "state": "stopped",
        "checkedAt": utc_now(),
        "message": message,
        "managerPid": None,
        "data": {
            "freshness": "invalid",
            "currentGenerationId": None,
            "snapshotCapturedAt": None,
            "snapshotAgeSeconds": None,
            "staleAfterSeconds": resolved.stale_after_seconds,
            "lastSuccessfulRefreshAt": None,
            "lastSuccessfulSnapshotAt": None,
            "warning": None,
        },
        "schedule": {
            "at": resolved.schedule_at,
            "timezone": resolved.schedule_timezone,
            "nextRunAt": None,
        },
        "services": {
            "website": {"state": "stopped", "pid": None, "url": LOCAL_URL},
            "scheduler": {
                "state": "stopped",
                "pid": None,
                **_scheduler_details(resolved, None),
            },
        },
    }


def _run_manager() -> int:
    current_pid = os.getpid()
    existing = _read_json(CONTROL_PATH) or {}
    existing_pid = existing.get("pid")
    if isinstance(existing_pid, int) and existing_pid != current_pid and process_is_alive(existing_pid):
        return 0

    settings = load_runtime_settings()
    node = find_node()
    environment = os.environ.copy()
    environment["PATH"] = str(node.parent) + os.pathsep + environment.get("PATH", "")
    environment["PYTHONUNBUFFERED"] = "1"
    # The local Vinext host invokes the strict Python historical-generation
    # verifier only for Stable Identity adoption.  Pin it to the same
    # interpreter that passed the manager dependency preflight instead of
    # relying on a potentially different global ``python`` on PATH.
    environment["RARDAR_PYTHON"] = sys.executable
    environment["RARDAR_SCHEDULE_AT"] = settings.schedule_at
    environment["RARDAR_SCHEDULE_TIMEZONE"] = settings.schedule_timezone
    environment["RARDAR_STALE_AFTER_HOURS"] = str(settings.stale_after_hours)

    website = ManagedService(
        "website",
        [str(node), str(ROOT / "node_modules" / "vinext" / "dist" / "cli.js"), "dev", "--hostname", "127.0.0.1"],
        LOG_DIR / "website.log",
    )
    scheduler = ManagedService(
        "scheduler",
        [
            sys.executable,
            "-m",
            "pipeline.scheduler",
            "--data-dir",
            "data",
            "--at",
            settings.schedule_at,
            "--timezone",
            settings.schedule_timezone,
            "--analyze-top",
            "5",
            "--status-path",
            str(SCHEDULER_STATUS_PATH),
            "--skip-initial",
        ],
        LOG_DIR / "scheduler.log",
    )
    services = [website, scheduler]
    should_stop = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal should_stop
        should_stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    _write_json(CONTROL_PATH, {"pid": current_pid, "startedAt": utc_now()})

    starting_status = _stopped_status("网站与每日刷新正在启动", settings)
    starting_status.update({"state": "starting", "managerPid": current_pid})
    starting_status["services"]["website"]["state"] = "starting"
    starting_status["services"]["scheduler"]["state"] = "starting"
    write_runtime_status(starting_status)
    status_server = start_status_server()
    try:
        for service in services:
            service.start(environment)

        while not should_stop:
            for service in services:
                exit_code = service.poll()
                if exit_code is not None:
                    service.last_error = f"process exited with code {exit_code}"
                    if service._log_handle:
                        service._log_handle.close()
                        service._log_handle = None
                    if (
                        service.name == "scheduler"
                        and exit_code == SCHEDULER_ALREADY_RUNNING_EXIT_CODE
                    ):
                        service.last_error = "another scheduler owns the managed data directory"
                        continue
                    time.sleep(2)
                    service.start(environment)

            website_health = WebsiteHealth("starting")
            if website.poll() is None and port_is_open():
                website_health = probe_website_health()
            website_state = website_health.state
            website.last_error = website_health.error
            if (
                website_health.state == "healthy"
                and (
                    website_health.schedule_at != settings.schedule_at
                    or website_health.schedule_timezone != settings.schedule_timezone
                    or website_health.stale_after_seconds != settings.stale_after_seconds
                )
            ):
                website_health = WebsiteHealth(
                    "degraded",
                    error="health endpoint reported runtime settings that differ from the manager",
                )
                website_state = "degraded"
                website.last_error = website_health.error
            scheduler_process_id = (
                scheduler.process.pid
                if scheduler.process is not None and scheduler.poll() is None
                else None
            )
            scheduler_details = _scheduler_details(settings, scheduler_process_id)
            scheduler_exit_code = scheduler.poll()
            scheduler_state = (
                scheduler_heartbeat_state(
                    scheduler_details.get("heartbeatAt"),
                    scheduler.started_at,
                )
                if scheduler_exit_code is None
                else (
                    "blocked"
                    if scheduler_exit_code == SCHEDULER_ALREADY_RUNNING_EXIT_CODE
                    else "restarting"
                )
            )
            if scheduler_state == "stale":
                scheduler.last_error = "scheduler heartbeat became stale"
                scheduler.stop()
                scheduler.start(environment)
                scheduler_state = "restarting"
            data_freshness = website_health.data_freshness or "invalid"
            services_healthy = website_state == scheduler_state == "healthy"
            overall_state = (
                "healthy"
                if services_healthy and data_freshness == "fresh"
                else "degraded"
            )
            scheduler_payload = _service_payload(scheduler, scheduler_state)
            scheduler_payload.update(scheduler_details)
            scheduler_payload["processError"] = scheduler.last_error
            payload = {
                "schemaVersion": 1,
                "state": overall_state,
                "checkedAt": utc_now(),
                "message": (
                    "网站、每日刷新与已发布数据均正常"
                    if overall_state == "healthy"
                    else (
                        "网站与调度正常，但已发布数据超过新鲜度阈值"
                        if services_healthy and data_freshness == "stale"
                        else (
                        "网站健康检查失败，进程保持运行并等待数据恢复"
                        if website_state == "degraded"
                        else "服务正在启动或恢复"
                        )
                    )
                ),
                "managerPid": current_pid,
                "data": {
                    "freshness": data_freshness,
                    "currentGenerationId": website_health.generation_id,
                    "snapshotCapturedAt": website_health.snapshot_captured_at,
                    "snapshotAgeSeconds": website_health.snapshot_age_seconds,
                    "staleAfterSeconds": settings.stale_after_seconds,
                    "lastSuccessfulRefreshAt": scheduler_details.get(
                        "lastSuccessfulRefreshAt"
                    ),
                    "lastSuccessfulSnapshotAt": website_health.snapshot_captured_at,
                    "warning": (
                        "Data freshness: STALE"
                        if data_freshness == "stale"
                        else None
                    ),
                },
                "schedule": {
                    "at": settings.schedule_at,
                    "timezone": settings.schedule_timezone,
                    "nextRunAt": scheduler_details.get("nextRunAt"),
                },
                "services": {
                    "website": {
                        **_service_payload(website, website_state),
                        "url": LOCAL_URL,
                        "generationId": website_health.generation_id,
                    },
                    "scheduler": scheduler_payload,
                },
            }
            write_runtime_status(payload)
            time.sleep(10)
    finally:
        status_server.shutdown()
        status_server.server_close()
        for service in reversed(services):
            service.stop()
        write_runtime_status(_stopped_status("本地运行管理器已停止", settings))
        try:
            CONTROL_PATH.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def run_manager() -> int:
    manager_lock = acquire_manager_lock()
    if manager_lock is None:
        return 0
    try:
        try:
            return _run_manager()
        except RuntimeSettingsError as error:
            print(f"Rardar runtime configuration error: {error}")
            return 2
    finally:
        release_manager_lock(manager_lock)


def _active_runtime_settings(status: dict[str, Any]) -> tuple[object, object, object]:
    schedule = status.get("schedule") if isinstance(status.get("schedule"), dict) else {}
    services = status.get("services") if isinstance(status.get("services"), dict) else {}
    scheduler = services.get("scheduler") if isinstance(services.get("scheduler"), dict) else {}
    legacy_schedule = (
        scheduler.get("schedule") if isinstance(scheduler.get("schedule"), dict) else {}
    )
    data = status.get("data") if isinstance(status.get("data"), dict) else {}
    return (
        schedule.get("at", legacy_schedule.get("time", DEFAULT_SCHEDULE_AT)),
        schedule.get(
            "timezone", legacy_schedule.get("timezone", DEFAULT_SCHEDULE_TIMEZONE)
        ),
        data.get("staleAfterSeconds", DEFAULT_STALE_AFTER_HOURS * 60 * 60),
    )


def _manager_status_matches_control(
    status: dict[str, Any],
    control: dict[str, Any],
) -> bool:
    manager_pid = status.get("managerPid")
    return (
        isinstance(manager_pid, int)
        and control.get("pid") == manager_pid
        and process_is_alive(manager_pid)
        and heartbeat_is_fresh(status.get("checkedAt"))
    )


def start_manager(open_browser: bool = False) -> int:
    try:
        requested_settings = load_runtime_settings()
    except RuntimeTimezoneDatabaseError as error:
        if not python_dependencies_are_ready():
            return 1
        print(f"Rardar runtime configuration error: {error}")
        return 2
    except RuntimeSettingsError as error:
        print(f"Rardar runtime configuration error: {error}")
        return 2
    control = _read_json(CONTROL_PATH) or {}
    existing_status = _read_json(STATUS_PATH) or {}
    manager_active = _manager_status_matches_control(existing_status, control)
    if manager_active:
        requested = (
            requested_settings.schedule_at,
            requested_settings.schedule_timezone,
            requested_settings.stale_after_seconds,
        )
        if _active_runtime_settings(existing_status) != requested:
            print(
                "Rardar runtime configuration changes require "
                "npm run local:stop followed by npm run local:start; "
                "the active runtime was not changed"
            )
            return 1
        if existing_status.get("state") == "healthy":
            print(f"Rardar is already managed at {LOCAL_URL}")
            if open_browser:
                webbrowser.open(LOCAL_URL)
            return 0
        data = existing_status.get("data") or {}
        services = existing_status.get("services") or {}
        website_state = (services.get("website") or {}).get("state")
        scheduler_state = (services.get("scheduler") or {}).get("state")
        if (
            data.get("freshness") == "stale"
            and website_state == scheduler_state == "healthy"
        ):
            print(f"Rardar is running at {LOCAL_URL}, but published data is stale")
            return 0
        website = ((existing_status.get("services") or {}).get("website") or {})
        detail = website.get("lastError") or existing_status.get("message") or "health check failed"
        print(f"Rardar is managed but degraded: {_short_health_error(detail)}")
        return 1

    if not python_dependencies_are_ready():
        return 1

    _stop_recorded_processes(existing_status)
    CONTROL_PATH.unlink(missing_ok=True)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    manager_log_path = LOG_DIR / "manager.log"
    rotate_log(manager_log_path)
    manager_log = manager_log_path.open("ab")
    creation_flags = 0
    if os.name == "nt":
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB keeps the manager alive after a launcher exits.
        )
    command = [sys.executable, "-m", "pipeline.runtime", "run"]
    try:
        manager_process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=manager_log,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
            close_fds=True,
        )
    except OSError:
        if os.name != "nt":
            raise
        fallback_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        manager_process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=manager_log,
            stderr=subprocess.STDOUT,
            creationflags=fallback_flags,
            close_fds=True,
        )
    manager_log.close()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = _read_json(STATUS_PATH) or {}
        active_control = _read_json(CONTROL_PATH) or {}
        status_data = status.get("data") or {}
        status_services = status.get("services") or {}
        data_only_degraded = (
            status.get("state") == "degraded"
            and status_data.get("freshness") == "stale"
            and (status_services.get("website") or {}).get("state") == "healthy"
            and (status_services.get("scheduler") or {}).get("state") == "healthy"
        )
        if (
            (status.get("state") == "healthy" or data_only_degraded)
            and status.get("managerPid") == manager_process.pid
            and active_control.get("pid") == manager_process.pid
            and heartbeat_is_fresh(status.get("checkedAt"))
        ):
            print(f"Rardar is running at {LOCAL_URL}")
            if data_only_degraded:
                print(
                    "Warning: published data is STALE "
                    f"(threshold {requested_settings.stale_after_hours}h)"
                )
            if open_browser:
                webbrowser.open(LOCAL_URL)
            return 0
        time.sleep(0.5)
    print("Rardar manager started, but services are not healthy yet. Check data/runtime/logs.")
    return 1


def stop_manager() -> int:
    control = _read_json(CONTROL_PATH) or {}
    manager_pid = control.get("pid")
    status = _read_json(STATUS_PATH) or {}
    if not isinstance(manager_pid, int) or not process_is_alive(manager_pid):
        _stop_recorded_processes(status, include_manager=False)
        write_runtime_status(_stopped_status())
        CONTROL_PATH.unlink(missing_ok=True)
        print("Rardar is not running under the local manager")
        return 0

    if process_matches(manager_pid, ("pipeline.runtime run", "pipeline\\runtime.py run")):
        _terminate_process_tree(manager_pid)
    deadline = time.monotonic() + 10
    while process_is_alive(manager_pid) and time.monotonic() < deadline:
        time.sleep(0.25)
    _stop_recorded_processes(status, include_manager=False)
    CONTROL_PATH.unlink(missing_ok=True)
    write_runtime_status(_stopped_status("本地运行管理器已停止"))
    print("Rardar local services stopped")
    return 0


def show_status() -> int:
    status = _read_json(STATUS_PATH) or _stopped_status()
    control = _read_json(CONTROL_PATH) or {}
    if not _manager_status_matches_control(status, control):
        status = {**status, "state": "stale", "message": "运行心跳已过期，服务状态不可信"}
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status.get("state") == "healthy" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the local Rardar website and scheduler")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--open", action="store_true", help="open the local URL in the default browser")
    subparsers.add_parser("stop")
    subparsers.add_parser("status")
    subparsers.add_parser("run")
    arguments = parser.parse_args()

    if arguments.command == "start":
        raise SystemExit(start_manager(arguments.open))
    if arguments.command == "stop":
        raise SystemExit(stop_manager())
    if arguments.command == "status":
        raise SystemExit(show_status())
    raise SystemExit(run_manager())


if __name__ == "__main__":
    main()
