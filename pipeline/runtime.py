"""Keep the local Rardar website and daily refresh scheduler alive."""

from __future__ import annotations

import argparse
import errno
import importlib.util
import json
import math
import ntpath
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

from pipeline.data_lock import manager_dir_lock_path
from pipeline.runtime_logging import StructuredLogger, new_run_id, process_run_id
from pipeline.runtime_settings import (
    DEFAULT_RUNTIME_STATUS_PORT,
    DEFAULT_SCHEDULE_AT,
    DEFAULT_SCHEDULE_TIMEZONE,
    DEFAULT_STALE_AFTER_HOURS,
    DEFAULT_VINEXT_PORT,
    MANAGER_ALREADY_RUNNING_EXIT_CODE,
    PERSISTENT_PATH_VARIABLES,
    RUNTIME_HOST,
    SCHEDULER_ALREADY_RUNNING_EXIT_CODE,
    TRENDING_PRODUCER_ENABLED_ENV,
    TRENDING_DISCOVER_ENABLED_ENV,
    RETENTION_ENABLED_ENV,
    RuntimeLayout,
    RuntimeSettings,
    RuntimeSettingsError,
    RuntimeTimezoneDatabaseError,
    VITE_ADDITIONAL_ALLOWED_HOSTS_ENV,
    default_runtime_dir as configured_runtime_dir,
    default_runtime_settings,
    load_runtime_layout,
    load_runtime_settings,
    validate_schedule_at,
    validate_schedule_timezone,
)


ROOT = Path(__file__).resolve().parents[1]


def default_runtime_dir() -> Path:
    """Backward-compatible public accessor for the validated runtime directory."""
    return configured_runtime_dir()


# Legacy module-level paths remain available for direct helper tests. CLI
# commands resolve the current environment at invocation time, so malformed
# configuration cannot cause import-time filesystem side effects.
RUNTIME_DIR = configured_runtime_dir({})
LOG_DIR = RUNTIME_DIR / "logs"
CONTROL_PATH = RUNTIME_DIR / "manager.json"
LOCK_PATH = RUNTIME_DIR / "manager.lock"
STATUS_PATH = RUNTIME_DIR / "status.json"
SCHEDULER_STATUS_PATH = RUNTIME_DIR / "scheduler-status.json"
LOCAL_URL = f"http://{RUNTIME_HOST}:{DEFAULT_VINEXT_PORT}/"
STATUS_HOST = RUNTIME_HOST
STATUS_PORT = DEFAULT_RUNTIME_STATUS_PORT
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
_status_allowed_origins = {
    f"http://{RUNTIME_HOST}:{DEFAULT_VINEXT_PORT}",
    f"http://localhost:{DEFAULT_VINEXT_PORT}",
}
_UNBOUND_SCHEDULER_PROCESS = object()
WEBSITE_ENVIRONMENT_ALLOWLIST = {
    VITE_ADDITIONAL_ALLOWED_HOSTS_ENV,
    "APPDATA",
    "CHOKIDAR_USEPOLLING",
    "CI",
    "CLOUDFLARE_VITE_FORCE_LOCAL",
    "CODEX_SANDBOX",
    "COMSPEC",
    "FORCE_COLOR",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOCALAPPDATA",
    "MINIFLARE_REGISTRY_PATH",
    "NODE_ENV",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONUTF8",
    "RARDAR_DATA_DIR",
    "RARDAR_DATA_LOCK_DIR",
    "RARDAR_HOME",
    "RARDAR_PYTHON",
    "RARDAR_RUNTIME_DIR",
    "RARDAR_RUNTIME_STATUS_PORT",
    "RARDAR_SCHEDULE_AT",
    "RARDAR_SCHEDULE_TIMEZONE",
    "RARDAR_STALE_AFTER_HOURS",
    "RARDAR_VINEXT_PORT",
    "RARDAR_VINEXT_STATE_DIR",
    "RARDAR_VITE_CACHE_DIR",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USERPROFILE",
    "WINDIR",
    "WRANGLER_LOG_PATH",
    "WRANGLER_REGISTRY_PATH",
    "WRANGLER_SEND_METRICS",
    "WRANGLER_WRITE_LOGS",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "no_proxy",
}
PROCESS_TREE_TERM_TIMEOUT = 8.0
PROCESS_TREE_KILL_TIMEOUT = 5.0
PROCESS_TREE_POLL_INTERVAL = 0.05


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


def manager_ownership_lock_path(layout: RuntimeLayout) -> Path:
    """Serialize managers by canonical data directory, not by login HOME."""
    return manager_dir_lock_path(layout.data_dir, layout.data_lock_dir)


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


def write_runtime_status(payload: dict[str, Any], path: Path | None = None) -> None:
    global _latest_status
    with _latest_status_lock:
        _latest_status = payload
    _write_json(path or STATUS_PATH, payload)


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
        if origin in _status_allowed_origins:
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


def start_status_server(
    host: str = STATUS_HOST,
    port: int = STATUS_PORT,
    website_port: int = DEFAULT_VINEXT_PORT,
) -> ThreadingHTTPServer:
    global _status_allowed_origins
    _status_allowed_origins = {
        f"http://{RUNTIME_HOST}:{website_port}",
        f"http://localhost:{website_port}",
    }
    server = LocalStatusServer((host, port), RuntimeStatusHandler)
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


def _runtime_child_environment(
    layout: RuntimeLayout,
    settings: RuntimeSettings,
    node: Path,
) -> dict[str, str]:
    """Freeze one validated contract for both managed child processes."""
    environment = os.environ.copy()
    environment["PATH"] = str(node.parent) + os.pathsep + environment.get("PATH", "")
    environment["PYTHONUNBUFFERED"] = "1"
    environment["RARDAR_PYTHON"] = sys.executable
    environment["RARDAR_HOME"] = str(layout.home)
    environment["RARDAR_DATA_DIR"] = str(layout.data_dir)
    environment["RARDAR_RUNTIME_DIR"] = str(layout.runtime_dir)
    environment["RARDAR_DATA_LOCK_DIR"] = str(layout.data_lock_dir)
    environment["RARDAR_VINEXT_PORT"] = str(layout.vinext_port)
    environment["RARDAR_RUNTIME_STATUS_PORT"] = str(layout.runtime_status_port)
    environment["RARDAR_SCHEDULE_AT"] = settings.schedule_at
    environment["RARDAR_SCHEDULE_TIMEZONE"] = settings.schedule_timezone
    environment["RARDAR_STALE_AFTER_HOURS"] = str(settings.stale_after_hours)
    environment[TRENDING_PRODUCER_ENABLED_ENV] = (
        "true" if settings.trending_producer_enabled else "false"
    )
    environment[TRENDING_DISCOVER_ENABLED_ENV] = (
        "true" if settings.trending_discover_enabled else "false"
    )
    environment[RETENTION_ENABLED_ENV] = (
        "true" if settings.retention_enabled else "false"
    )
    environment["RARDAR_RETENTION_CAPTURE_DAYS"] = str(settings.retention_capture_days)
    environment["RARDAR_RETENTION_GENERATION_DAYS"] = str(
        settings.retention_generation_days
    )
    environment["RARDAR_RETENTION_CANDIDATE_DAYS"] = str(
        settings.retention_candidate_days
    )
    environment["RARDAR_RETENTION_TEMP_HOURS"] = str(settings.retention_temp_hours)
    environment["RARDAR_STORAGE_WARNING_PERCENT"] = str(
        settings.storage_warning_percent
    )
    environment["RARDAR_STORAGE_HARD_PERCENT"] = str(settings.storage_hard_percent)
    environment["RARDAR_STORAGE_MINIMUM_FREE_BYTES"] = str(
        settings.storage_minimum_free_bytes
    )
    # Persistent state locations remain opt-in for local compatibility.  The
    # strict layout loader validates every explicitly supplied value, and this
    # copy preserves those exact deployment paths for Vinext/Wrangler.
    for name in PERSISTENT_PATH_VARIABLES:
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def _website_child_environment(environment: dict[str, str]) -> dict[str, str]:
    """Expose only the explicitly reviewed local-host contract to Vinext."""
    return {
        name: value
        for name, value in environment.items()
        if name in WEBSITE_ENVIRONMENT_ALLOWLIST
    }


def _runtime_preflight(
    *,
    service_mode: bool = False,
) -> tuple[RuntimeLayout, RuntimeSettings, Path] | None:
    """Validate all service configuration and dependencies before any writes."""
    try:
        layout = load_runtime_layout(application_root=ROOT)
        settings = load_runtime_settings()
    except RuntimeTimezoneDatabaseError as error:
        if not python_dependencies_are_ready():
            return None
        print(f"Rardar runtime configuration error: {error}")
        return None
    except RuntimeSettingsError as error:
        print(f"Rardar runtime configuration error: {error}")
        return None
    if not python_dependencies_are_ready():
        return None
    if service_mode and layout.home != ROOT.resolve():
        print(
            "RARDAR_HOME must resolve to the release that provides pipeline.runtime "
            f"({ROOT.resolve()})"
        )
        return None
    try:
        node = find_node()
    except RuntimeError as error:
        print(str(error))
        return None
    required_javascript_paths = (
        layout.home / "node_modules" / "vinext" / "dist" / "cli.js",
        layout.home / "node_modules" / "vite" / "bin" / "vite.js",
        layout.home / "vite.config.ts",
        layout.home / ".openai" / "hosting.json",
        layout.home / "app" / "runtime-readiness.mjs",
        layout.home / "build" / "published-data-bridge.ts",
        layout.home / "build" / "sites-vite-plugin.ts",
        layout.home / "worker" / "index.ts",
    )
    missing_javascript_path = next(
        (path for path in required_javascript_paths if not path.is_file()),
        None,
    )
    if not layout.home.is_dir() or missing_javascript_path is not None:
        print(
            "Rardar runtime dependencies are incomplete: "
            f"missing {missing_javascript_path}; install JavaScript dependencies in RARDAR_HOME"
        )
        return None
    return layout, settings, node


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


def process_matches(
    pid: int,
    markers: tuple[str, ...],
    *,
    require_all: bool = False,
) -> bool:
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
    matches = (marker.lower() in lowered for marker in markers)
    return all(matches) if require_all else any(matches)


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


def _stop_recorded_processes(
    status: dict[str, Any],
    include_manager: bool = True,
    *,
    layout: RuntimeLayout | None = None,
) -> None:
    targets: list[tuple[int, tuple[str, ...], bool]] = []
    manager_pid = status.get("managerPid")
    if include_manager and isinstance(manager_pid, int):
        targets.append(
            (
                manager_pid,
                (
                    "pipeline.runtime run",
                    "pipeline\\runtime.py run",
                    "pipeline.runtime service",
                    "pipeline\\runtime.py service",
                ),
                False,
            )
        )
    services = status.get("services") or {}
    website_pid = (services.get("website") or {}).get("pid")
    scheduler_pid = (services.get("scheduler") or {}).get("pid")
    if isinstance(website_pid, int):
        website_home = layout.home if layout is not None else ROOT.resolve()
        website_port = layout.vinext_port if layout is not None else DEFAULT_VINEXT_PORT
        targets.append(
            (
                website_pid,
                (
                    str(website_home / "node_modules" / "vite" / "bin" / "vite.js"),
                    "--configLoader",
                    "runner",
                    "--host",
                    RUNTIME_HOST,
                    "--port",
                    str(website_port),
                    "--strictPort",
                ),
                True,
            )
        )
    if isinstance(scheduler_pid, int):
        targets.append((scheduler_pid, ("pipeline.scheduler",), False))
    for pid, markers, require_all in targets:
        if process_matches(pid, markers, require_all=require_all):
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


def _posix_process_group_is_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise RuntimeError(
            f"cannot verify owned process group {process_group_id}: {error}"
        ) from error
    return True


def _wait_for_posix_process_group(
    process_group_id: int,
    process: subprocess.Popen[bytes],
    timeout: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        process.poll()
        if not _posix_process_group_is_alive(process_group_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(PROCESS_TREE_POLL_INTERVAL)


def _terminate_posix_process_group(
    process_group_id: int,
    process: subprocess.Popen[bytes],
) -> None:
    if process_group_id <= 0 or process_group_id == os.getpgrp():
        raise RuntimeError(f"refusing to terminate unsafe process group {process_group_id}")
    if not _posix_process_group_is_alive(process_group_id):
        process.poll()
        return
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        process.poll()
        return
    if _wait_for_posix_process_group(
        process_group_id,
        process,
        PROCESS_TREE_TERM_TIMEOUT,
    ):
        return
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        process.poll()
        return
    if not _wait_for_posix_process_group(
        process_group_id,
        process,
        PROCESS_TREE_KILL_TIMEOUT,
    ):
        raise RuntimeError(
            f"owned process group {process_group_id} survived SIGTERM and SIGKILL"
        )


def _windows_process_times_from_handle(process_handle: int) -> tuple[int, int | None]:
    """Return immutable Windows creation time and optional exit time for one handle."""

    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

    def as_integer(value: FileTime) -> int:
        return (int(value.high) << 32) | int(value.low)

    creation = FileTime()
    exit_time = FileTime()
    kernel_time = FileTime()
    user_time = FileTime()
    kernel32 = ctypes.windll.kernel32
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    if not kernel32.GetProcessTimes(
        wintypes.HANDLE(process_handle),
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        raise RuntimeError("cannot read the owned Windows process creation identity")
    creation_value = as_integer(creation)
    if creation_value <= 0:
        raise RuntimeError("owned Windows process returned an invalid creation identity")
    exit_value = as_integer(exit_time)
    return creation_value, exit_value if exit_value > 0 else None


def _windows_process_creation_time(process_id: int) -> int | None:
    """Read a live PID's creation identity without trusting the PID alone."""

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        return None
    try:
        try:
            creation_time, _ = _windows_process_times_from_handle(int(handle))
        except RuntimeError:
            return None
        return creation_time
    finally:
        kernel32.CloseHandle(handle)


def _windows_command_line_arguments(command_line: str) -> tuple[str, ...]:
    """Parse a Windows command line using the same platform quoting rules."""

    import ctypes
    from ctypes import wintypes

    argument_count = ctypes.c_int()
    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.argtypes = (
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    )
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    arguments = shell32.CommandLineToArgvW(command_line, ctypes.byref(argument_count))
    if not arguments:
        raise RuntimeError("cannot parse the Windows process command line")
    try:
        return tuple(arguments[index] for index in range(argument_count.value))
    finally:
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = (wintypes.HANDLE,)
        kernel32.LocalFree.restype = wintypes.HANDLE
        kernel32.LocalFree(ctypes.cast(arguments, wintypes.HANDLE))


def _windows_process_command_identity(
    process_id: int,
) -> tuple[int, str, tuple[str, ...]] | None:
    """Return direct parent, executable and exact argv tail for one Windows PID."""

    command = (
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {process_id}'; "
        "if ($null -ne $p) { "
        "$p | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine "
        "| ConvertTo-Json -Compress }"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    parent_id = payload.get("ParentProcessId")
    executable = payload.get("ExecutablePath")
    command_line = payload.get("CommandLine")
    if (
        not isinstance(parent_id, int)
        or isinstance(parent_id, bool)
        or not isinstance(executable, str)
        or not executable
        or not isinstance(command_line, str)
        or not command_line
    ):
        return None
    try:
        arguments = _windows_command_line_arguments(command_line)
    except RuntimeError:
        return None
    if not arguments:
        return None
    return (
        parent_id,
        ntpath.normcase(ntpath.abspath(executable)),
        tuple(arguments[1:]),
    )


def _windows_process_parent_map() -> dict[int, int]:
    command = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"cannot enumerate the owned Windows process tree: {error}") from error
    if completed.returncode != 0:
        raise RuntimeError("cannot enumerate the owned Windows process tree")
    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as error:
        raise RuntimeError("Windows process tree enumeration returned invalid JSON") from error
    rows = payload if isinstance(payload, list) else [payload]
    parents: dict[int, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        process_id = row.get("ProcessId")
        parent_id = row.get("ParentProcessId")
        if (
            isinstance(process_id, int)
            and not isinstance(process_id, bool)
            and isinstance(parent_id, int)
            and not isinstance(parent_id, bool)
        ):
            parents[process_id] = parent_id
    return parents


def _windows_owned_process_identities(
    root_process_id: int,
    root_creation_time: int,
    root_exit_time: int | None,
) -> dict[int, int]:
    """Snapshot only descendants whose creation chronology proves ownership."""

    parents = _windows_process_parent_map()
    current_root_creation = _windows_process_creation_time(root_process_id)
    if root_exit_time is None and current_root_creation != root_creation_time:
        raise RuntimeError("owned Windows process identity changed while its handle was active")
    owned: dict[int, int] = {}
    if current_root_creation == root_creation_time:
        owned[root_process_id] = root_creation_time
    parent_bounds = {root_process_id: (root_creation_time, root_exit_time)}
    frontier = {root_process_id}
    while frontier:
        children: set[int] = set()
        for process_id, parent_id in parents.items():
            if parent_id not in frontier or process_id in owned:
                continue
            creation_time = _windows_process_creation_time(process_id)
            if creation_time is None:
                raise RuntimeError(
                    f"cannot verify Windows child process creation identity for PID {process_id}"
                )
            parent_creation, parent_exit = parent_bounds[parent_id]
            if creation_time < parent_creation:
                continue
            if parent_exit is not None and creation_time > parent_exit:
                continue
            owned[process_id] = creation_time
            parent_bounds[process_id] = (creation_time, None)
            children.add(process_id)
        if not children:
            break
        frontier = children
    return owned


def _windows_taskkill(process_id: int, *, force: bool) -> None:
    command = ["taskkill", "/PID", str(process_id), "/T"]
    if force:
        command.append("/F")
    subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _matching_windows_processes(
    process_identities: dict[int, int],
) -> dict[int, int]:
    matching: dict[int, int] = {}
    for process_id, expected_creation_time in process_identities.items():
        current_creation_time = _windows_process_creation_time(process_id)
        if current_creation_time == expected_creation_time:
            matching[process_id] = expected_creation_time
        elif current_creation_time is None and process_is_alive(process_id):
            raise RuntimeError(
                f"cannot revalidate Windows process creation identity for PID {process_id}"
            )
    return matching


def _wait_for_windows_processes(
    process_identities: dict[int, int],
    timeout: float,
) -> dict[int, int]:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        alive = _matching_windows_processes(process_identities)
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(PROCESS_TREE_POLL_INTERVAL)


def _terminate_windows_process_tree(
    root_process_id: int,
    root_creation_time: int,
    root_exit_time: int | None,
) -> None:
    tracked = _windows_owned_process_identities(
        root_process_id,
        root_creation_time,
        root_exit_time,
    )
    if not tracked:
        return
    # `/PID /T` is used only after the PID's immutable creation identity still
    # matches the owned snapshot. Never select processes by name.
    ordered = [
        *([root_process_id] if root_process_id in tracked else []),
        *sorted(set(tracked) - {root_process_id}),
    ]
    for process_id in ordered:
        if _windows_process_creation_time(process_id) == tracked[process_id]:
            _windows_taskkill(process_id, force=False)
    alive = _wait_for_windows_processes(tracked, PROCESS_TREE_TERM_TIMEOUT)
    for process_id, creation_time in sorted(alive.items()):
        if _windows_process_creation_time(process_id) == creation_time:
            _windows_taskkill(process_id, force=True)
    survivors = _wait_for_windows_processes(alive, PROCESS_TREE_KILL_TIMEOUT)
    if survivors:
        raise RuntimeError(
            "owned Windows process tree survived taskkill: "
            + ", ".join(str(process_id) for process_id in sorted(survivors))
        )


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
    _process_group_id: int | None = None
    _process_tree_root_pid: int | None = None
    _process_tree_root_creation_time: int | None = None
    _output_thread: threading.Thread | None = None
    _structured_stdio: bool = False

    def _close_log(self) -> None:
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None
        if self._output_thread is not None:
            self._output_thread.join(timeout=2)
            self._output_thread = None

    def _forward_output(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        logger = StructuredLogger(self.name)
        try:
            for raw in iter(process.stdout.readline, b""):
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    logger.emit(
                        "process_output",
                        state="emitted",
                        message=text,
                        childProcessId=process.pid,
                    )
        finally:
            process.stdout.close()

    def cleanup_owned_process_tree(self) -> None:
        """Prove the previous process tree is gone before any replacement."""
        process = self.process
        if process is None:
            self._close_log()
            return
        try:
            if os.name == "nt":
                root_process_id = self._process_tree_root_pid
                root_creation_time = self._process_tree_root_creation_time
                if root_process_id is None or root_creation_time is None:
                    if process.poll() is not None:
                        return
                    raise RuntimeError(
                        "cannot stop an owned Windows process without its creation identity"
                    )
                process_handle = getattr(process, "_handle", None)
                if process_handle is None:
                    raise RuntimeError("owned Windows process handle is unavailable")
                recorded_creation_time, root_exit_time = _windows_process_times_from_handle(
                    int(process_handle)
                )
                if recorded_creation_time != root_creation_time:
                    raise RuntimeError("owned Windows process handle identity changed")
                _terminate_windows_process_tree(
                    root_process_id,
                    root_creation_time,
                    root_exit_time,
                )
                process.poll()
            elif self._process_group_id is not None:
                _terminate_posix_process_group(self._process_group_id, process)
            elif process.poll() is None:
                # Only directly injected test processes can reach this legacy
                # fallback. Processes started below always have a recorded PGID.
                process.terminate()
                try:
                    process.wait(timeout=PROCESS_TREE_TERM_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=PROCESS_TREE_KILL_TIMEOUT)
            self._process_group_id = None
            self._process_tree_root_pid = None
            self._process_tree_root_creation_time = None
        finally:
            self._close_log()

    def start(self, environment: dict[str, str]) -> None:
        restarting = self.process is not None
        if restarting:
            self.cleanup_owned_process_tree()
            self.restart_count += 1
        self._structured_stdio = bool(environment.get("JOURNAL_STREAM")) or (
            environment.get("RARDAR_STRUCTURED_LOG_STDIO") == "true"
        )
        stdout: Any
        stderr: Any
        if self._structured_stdio and self.name == "website":
            stdout = subprocess.PIPE
            stderr = subprocess.STDOUT
        elif self._structured_stdio:
            stdout = None
            stderr = None
        else:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            rotate_log(self.log_path)
            self._log_handle = self.log_path.open("ab")
            stdout = self._log_handle
            stderr = subprocess.STDOUT
        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        self.process = subprocess.Popen(
            self.command,
            cwd=Path(environment["RARDAR_HOME"]),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
        self._process_tree_root_pid = self.process.pid
        self._process_group_id = self.process.pid if os.name != "nt" else None
        if os.name == "nt":
            process_handle = getattr(self.process, "_handle", None)
            if process_handle is None:
                self.process.kill()
                self.process.wait(timeout=PROCESS_TREE_KILL_TIMEOUT)
                self.process = None
                self._close_log()
                raise RuntimeError("owned Windows process handle is unavailable")
            try:
                creation_time, _ = _windows_process_times_from_handle(int(process_handle))
            except RuntimeError:
                self.process.kill()
                self.process.wait(timeout=PROCESS_TREE_KILL_TIMEOUT)
                self.process = None
                self._process_tree_root_pid = None
                self._close_log()
                raise
            self._process_tree_root_creation_time = creation_time
        else:
            self._process_tree_root_creation_time = None
        if self._structured_stdio and self.name == "website":
            self._output_thread = threading.Thread(
                target=self._forward_output,
                name="rardar-website-log-forwarder",
                daemon=True,
            )
            self._output_thread.start()
        self.started_at = utc_now()
        if self.restart_count == 0:
            self.last_error = None
        StructuredLogger("manager").emit(
            "process_started",
            state="running",
            run_id=process_run_id(),
            component=self.name,
            childProcessId=self.process.pid,
            retryCount=self.restart_count,
        )

    def poll(self) -> int | None:
        return self.process.poll() if self.process else None

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            StructuredLogger("manager").emit(
                "process_stopping",
                state="stopping",
                run_id=process_run_id(),
                component=self.name,
                childProcessId=self.process.pid,
            )
        self.cleanup_owned_process_tree()
        StructuredLogger("manager").emit(
            "process_stopped",
            state="stopped",
            run_id=process_run_id(),
            component=self.name,
        )


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


def _trusted_windows_python_executables(command_executable: str) -> set[str]:
    candidates = {command_executable, getattr(sys, "_base_executable", command_executable)}
    return {
        ntpath.normcase(ntpath.abspath(candidate))
        for candidate in candidates
        if isinstance(candidate, str) and candidate
    }


def _scheduler_process_id_is_trusted(
    expected: object,
    reported: object,
    *,
    expected_creation_time: int | None = None,
    expected_command: tuple[str, ...] | None = None,
) -> bool:
    if expected is _UNBOUND_SCHEDULER_PROCESS:
        return True
    if not isinstance(expected, int) or not isinstance(reported, int):
        return False
    if expected == reported:
        if os.name != "nt":
            return True
        return (
            expected_creation_time is not None
            and _windows_process_creation_time(reported) == expected_creation_time
        )
    if os.name != "nt":
        return False
    # A Windows venv redirector can remain as the exact Popen child while the
    # base interpreter that executes pipeline.scheduler is its direct child.
    # Accept only that one launcher boundary, never an arbitrary descendant or
    # another process that happens to write the shared status path.
    if expected_creation_time is None or not expected_command:
        return False
    reported_creation_time = _windows_process_creation_time(reported)
    if (
        reported_creation_time is None
        or reported_creation_time < expected_creation_time
    ):
        return False
    command_identity = _windows_process_command_identity(reported)
    if command_identity is None:
        return False
    direct_parent, executable, arguments = command_identity
    if direct_parent != expected:
        return False
    if executable not in _trusted_windows_python_executables(expected_command[0]):
        return False
    return arguments == tuple(expected_command[1:])


def _public_producer_telemetry(value: object) -> dict[str, Any] | None:
    """Project Scheduler telemetry onto the reviewed, path-free public shape."""

    if not isinstance(value, dict):
        return None
    root_fields = (
        "enabled",
        "state",
        "nextObservationAt",
        "nextExplosionAt",
        "first08CaptureAt",
        "firstExactEligibleAt",
    )
    section_fields = {
        "observation": (
            "state",
            "cadenceMinutes",
            "timezone",
            "lastScheduledAt",
            "lastStartedAt",
            "lastCompletedAt",
            "lastCaptureId",
            "windowEligible",
            "coverageState",
            "successfulQueryCount",
            "failedQueryCount",
            "candidateCount",
            "observationCount",
            "metadataFailureCount",
            "carryForwardCount",
            "newRepositoryCount",
            "captureDelaySeconds",
            "retryCount",
            "lastErrorCode",
            "nextRunAt",
        ),
        "explosion": (
            "state",
            "scheduleAt",
            "timezone",
            "lastWindowEnd",
            "lastStartedAt",
            "lastCompletedAt",
            "generationId",
            "windowState",
            "coverageState",
            "exactCount",
            "pendingCount",
            "conflictCount",
            "lastErrorCode",
            "nextRunAt",
        ),
        "discover": (
            "enabled",
            "state",
            "lastScheduledAt",
            "startedAt",
            "completedAt",
            "latestCaptureId",
            "generationId",
            "stageCounts",
            "publishedCount",
            "conflictCount",
            "excludedExactCount",
            "todayExactCount",
            "todayPublishedCount",
            "excludedPublishedCount",
            "exactOutsidePublishedEvaluatedCount",
            "preExactEvaluatedCount",
            "suppressedSignalCount",
            "suppressionCounts",
            "coverage",
            "lastErrorCode",
            "nextExpectedAt",
        ),
        "retention": (
            "enabled",
            "state",
            "lastPlannedAt",
            "lastAppliedAt",
            "lastPlanDigest",
            "deletedFiles",
            "deletedBytes",
            "protectedFiles",
            "protectedBytes",
            "errorCode",
            "nextExpectedAt",
        ),
        "storage": (
            "usedPercent",
            "freeBytes",
            "warningThreshold",
            "hardThreshold",
            "minimumFreeBytes",
            "guardState",
            "errorCode",
        ),
    }

    def public_scalar(item: object) -> object | None:
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str) and len(item) <= 256 and "\n" not in item and "\r" not in item:
            return item
        return None

    nested_fields = {
        "stageCounts": (
            "just_discovered",
            "outside_today_momentum",
            "rising",
            "near_validation",
        ),
        "coverage": (
            "state",
            "querySuccessCount",
            "queryFailureCount",
            "metadataFailureCount",
            "sourceCaptureCount",
            "candidateCount",
            "publishedCount",
            "conflictCount",
            "excludedExactCount",
            "todayExactCount",
            "todayPublishedCount",
            "excludedPublishedCount",
            "exactOutsidePublishedEvaluatedCount",
            "preExactEvaluatedCount",
            "invalidCount",
        ),
        "suppressionCounts": (
            "today_published",
            "weak_recent_absolute_growth",
            "weak_recent_relative_growth",
            "no_recent_continuous_growth",
            "no_recent_acceleration",
            "weak_pre_exact_growth",
            "already_exact_without_momentum",
            "identity_conflict",
            "negative_growth",
            "disabled",
            "metadata_incomplete",
        ),
    }

    def public_value(name: str, item: object) -> object | None:
        scalar = public_scalar(item)
        if scalar is not None or item is None:
            return scalar
        allowed = nested_fields.get(name)
        if isinstance(item, dict) and allowed is not None:
            projected = {
                key: public_scalar(item.get(key))
                for key in allowed
                if key in item
            }
            return projected
        return None

    projected = {
        name: public_scalar(value.get(name))
        for name in root_fields
        if name in value
    }
    for section_name, fields in section_fields.items():
        section = value.get(section_name)
        if isinstance(section, dict):
            projected[section_name] = {
                name: public_value(name, section.get(name))
                for name in fields
                if name in section
            }
    return projected


def _scheduler_details(
    settings: RuntimeSettings | None = None,
    expected_process_id: int | None | object = _UNBOUND_SCHEDULER_PROCESS,
    *,
    status_path: Path | None = None,
    expected_process_creation_time: int | None = None,
    expected_process_command: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    stored = _read_json(status_path or SCHEDULER_STATUS_PATH) or {}
    resolved = settings or default_runtime_settings()
    reported_process_id = stored.get("processId")
    telemetry_trusted = _scheduler_process_id_is_trusted(
        expected_process_id,
        reported_process_id,
        expected_creation_time=expected_process_creation_time,
        expected_command=expected_process_command,
    )
    status = stored if telemetry_trusted else {}
    public_producer = _public_producer_telemetry(status.get("producer"))
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
        "producer": public_producer,
        "retention": (
            public_producer.get("retention")
            if isinstance(public_producer, dict)
            else None
        ),
        "storage": (
            public_producer.get("storage")
            if isinstance(public_producer, dict)
            else None
        ),
        "telemetryTrusted": telemetry_trusted,
        "reportedProcessId": reported_process_id,
    }


def _stopped_status(
    message: str = "本地运行管理器未启动",
    settings: RuntimeSettings | None = None,
    *,
    layout: RuntimeLayout | None = None,
    scheduler_status_path: Path | None = None,
) -> dict[str, Any]:
    resolved = settings or default_runtime_settings()
    local_url = layout.website_url if layout is not None else LOCAL_URL
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
        "retention": {
            "enabled": resolved.retention_enabled,
            "state": "stopped" if resolved.retention_enabled else "disabled",
            "lastPlannedAt": None,
            "lastAppliedAt": None,
            "lastPlanDigest": None,
            "deletedFiles": 0,
            "deletedBytes": 0,
            "protectedFiles": None,
            "protectedBytes": None,
            "errorCode": None,
            "nextExpectedAt": None,
        },
        "storage": {
            "usedPercent": None,
            "freeBytes": None,
            "warningThreshold": resolved.storage_warning_percent,
            "hardThreshold": resolved.storage_hard_percent,
            "minimumFreeBytes": resolved.storage_minimum_free_bytes,
            "guardState": "unknown",
            "errorCode": None,
        },
        "services": {
            "website": {"state": "stopped", "pid": None, "url": local_url},
            "scheduler": {
                "state": "stopped",
                "pid": None,
                **_scheduler_details(
                    resolved,
                    None,
                    status_path=scheduler_status_path or SCHEDULER_STATUS_PATH,
                ),
            },
        },
        "runtime": (
            {
                "host": RUNTIME_HOST,
                "home": str(layout.home),
                "dataDir": str(layout.data_dir),
                "runtimeDir": str(layout.runtime_dir),
                "dataLockDir": str(layout.data_lock_dir),
                "vinextPort": layout.vinext_port,
                "runtimeStatusPort": layout.runtime_status_port,
                "statusUrl": layout.status_url,
            }
            if layout is not None
            else None
        ),
    }


def _run_manager(
    layout: RuntimeLayout | None = None,
    settings: RuntimeSettings | None = None,
    node: Path | None = None,
    *,
    conflict_exit_code: int = 0,
) -> int:
    resolved_layout = layout or load_runtime_layout(application_root=ROOT)
    control_path = resolved_layout.control_path if layout is not None else CONTROL_PATH
    status_path = resolved_layout.status_path if layout is not None else STATUS_PATH
    scheduler_status_path = (
        resolved_layout.scheduler_status_path
        if layout is not None
        else SCHEDULER_STATUS_PATH
    )
    log_dir = resolved_layout.log_dir if layout is not None else LOG_DIR
    local_url = resolved_layout.website_url if layout is not None else LOCAL_URL
    current_pid = os.getpid()
    existing = _read_json(control_path) or {}
    existing_pid = existing.get("pid")
    if isinstance(existing_pid, int) and existing_pid != current_pid and process_is_alive(existing_pid):
        return conflict_exit_code

    resolved_settings = settings or load_runtime_settings()
    resolved_node = node or find_node()
    environment = _runtime_child_environment(
        resolved_layout,
        resolved_settings,
        resolved_node,
    )
    service_environments = {
        "website": _website_child_environment(environment),
        "scheduler": environment,
    }

    website = ManagedService(
        "website",
        [
            str(resolved_node),
            str(resolved_layout.home / "node_modules" / "vite" / "bin" / "vite.js"),
            "--configLoader",
            "runner",
            "--host",
            RUNTIME_HOST,
            "--port",
            str(resolved_layout.vinext_port),
            "--strictPort",
        ],
        log_dir / "website.log",
    )
    scheduler = ManagedService(
        "scheduler",
        [
            sys.executable,
            "-m",
            "pipeline.scheduler",
            "--data-dir",
            str(resolved_layout.data_dir),
            "--at",
            resolved_settings.schedule_at,
            "--timezone",
            resolved_settings.schedule_timezone,
            "--analyze-top",
            "5",
            "--status-path",
            str(scheduler_status_path),
            "--skip-initial",
        ],
        log_dir / "scheduler.log",
    )
    services = [website, scheduler]
    should_stop = False
    manager_logger = StructuredLogger("manager")
    manager_run_id = new_run_id()

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal should_stop
        if not should_stop:
            manager_logger.emit(
                "process_stopping",
                state="stopping",
                run_id=manager_run_id,
                component="manager",
            )
        should_stop = True

    def publish_status(payload: dict[str, Any]) -> None:
        if layout is None:
            write_runtime_status(payload)
        else:
            write_runtime_status(payload, status_path)

    def scheduler_telemetry(expected_process_id: int | None) -> dict[str, Any]:
        expected_creation_time = (
            getattr(scheduler, "_process_tree_root_creation_time", None)
            if expected_process_id is not None
            else None
        )
        expected_command = (
            tuple(scheduler.command) if expected_process_id is not None else None
        )
        if layout is None:
            return _scheduler_details(
                resolved_settings,
                expected_process_id,
                expected_process_creation_time=expected_creation_time,
                expected_process_command=expected_command,
            )
        return _scheduler_details(
            resolved_settings,
            expected_process_id,
            status_path=scheduler_status_path,
            expected_process_creation_time=expected_creation_time,
            expected_process_command=expected_command,
        )

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    status_server: ThreadingHTTPServer | None = None
    try:
        manager_logger.emit(
            "process_started",
            state="running",
            run_id=manager_run_id,
            component="manager",
            discoverEnabled=resolved_settings.trending_discover_enabled,
            retentionEnabled=resolved_settings.retention_enabled,
        )
        status_server = (
            start_status_server()
            if layout is None
            else start_status_server(
                RUNTIME_HOST,
                resolved_layout.runtime_status_port,
                resolved_layout.vinext_port,
            )
        )
        _write_json(
            control_path,
            {
                "pid": current_pid,
                "startedAt": utc_now(),
                "home": str(resolved_layout.home),
                "dataDir": str(resolved_layout.data_dir),
                "runtimeDir": str(resolved_layout.runtime_dir),
                "dataLockDir": str(resolved_layout.data_lock_dir),
                "vinextPort": resolved_layout.vinext_port,
                "runtimeStatusPort": resolved_layout.runtime_status_port,
            },
        )
        starting_status = _stopped_status(
            "网站与每日刷新正在启动",
            resolved_settings,
            layout=resolved_layout,
            scheduler_status_path=scheduler_status_path,
        )
        starting_status.update({"state": "starting", "managerPid": current_pid})
        starting_status["services"]["website"]["state"] = "starting"
        starting_status["services"]["scheduler"]["state"] = "starting"
        publish_status(starting_status)
        for service in services:
            service.start(service_environments[service.name])

        while not should_stop:
            for service in services:
                exit_code = service.poll()
                if exit_code is not None:
                    service.last_error = f"process exited with code {exit_code}"
                    service.cleanup_owned_process_tree()
                    if (
                        service.name == "scheduler"
                        and exit_code == SCHEDULER_ALREADY_RUNNING_EXIT_CODE
                    ):
                        service.last_error = "another scheduler owns the managed data directory"
                        continue
                    time.sleep(2)
                    service.start(service_environments[service.name])

            website_health = WebsiteHealth("starting")
            if website.poll() is None and port_is_open(RUNTIME_HOST, resolved_layout.vinext_port):
                website_health = probe_website_health(
                    RUNTIME_HOST,
                    resolved_layout.vinext_port,
                )
            website_state = website_health.state
            website.last_error = website_health.error
            if (
                website_health.state == "healthy"
                and (
                    website_health.schedule_at != resolved_settings.schedule_at
                    or website_health.schedule_timezone != resolved_settings.schedule_timezone
                    or website_health.stale_after_seconds != resolved_settings.stale_after_seconds
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
            scheduler_details = scheduler_telemetry(scheduler_process_id)
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
                scheduler.start(service_environments[scheduler.name])
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
            if (
                scheduler_details.get("telemetryTrusted") is True
                and isinstance(scheduler_details.get("reportedProcessId"), int)
            ):
                scheduler_payload["pid"] = scheduler_details["reportedProcessId"]
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
                    "staleAfterSeconds": resolved_settings.stale_after_seconds,
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
                    "at": resolved_settings.schedule_at,
                    "timezone": resolved_settings.schedule_timezone,
                    "nextRunAt": scheduler_details.get("nextRunAt"),
                },
                "retention": scheduler_details.get("retention"),
                "storage": scheduler_details.get("storage"),
                "services": {
                    "website": {
                        **_service_payload(website, website_state),
                        "url": local_url,
                        "generationId": website_health.generation_id,
                    },
                    "scheduler": scheduler_payload,
                },
                "runtime": {
                    "host": RUNTIME_HOST,
                    "home": str(resolved_layout.home),
                    "dataDir": str(resolved_layout.data_dir),
                    "runtimeDir": str(resolved_layout.runtime_dir),
                    "dataLockDir": str(resolved_layout.data_lock_dir),
                    "vinextPort": resolved_layout.vinext_port,
                    "runtimeStatusPort": resolved_layout.runtime_status_port,
                    "statusUrl": resolved_layout.status_url,
                },
            }
            publish_status(payload)
            time.sleep(10)
    finally:
        if status_server is not None:
            status_server.shutdown()
            status_server.server_close()
        for service in reversed(services):
            service.stop()
        publish_status(
            _stopped_status(
                "本地运行管理器已停止",
                resolved_settings,
                layout=resolved_layout,
                scheduler_status_path=scheduler_status_path,
            )
        )
        try:
            control_path.unlink(missing_ok=True)
        except OSError:
            pass
        manager_logger.emit(
            "process_stopped",
            state="stopped",
            run_id=manager_run_id,
            component="manager",
        )
    return 0


def run_manager(*, service_mode: bool = False) -> int:
    preflight = _runtime_preflight(service_mode=service_mode)
    if preflight is None:
        return 1
    layout, settings, node = preflight
    manager_lock = acquire_manager_lock(manager_ownership_lock_path(layout))
    if manager_lock is None:
        print("Another Rardar manager already owns this data directory")
        return MANAGER_ALREADY_RUNNING_EXIT_CODE if service_mode else 0
    try:
        try:
            return _run_manager(
                layout,
                settings,
                node,
                conflict_exit_code=(
                    MANAGER_ALREADY_RUNNING_EXIT_CODE if service_mode else 0
                ),
            )
        except RuntimeSettingsError as error:
            print(f"Rardar runtime configuration error: {error}")
            return 2
        except RuntimeError as error:
            print(str(error))
            return 1
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


def _active_runtime_layout(status: dict[str, Any]) -> tuple[object, ...] | None:
    runtime = status.get("runtime")
    if not isinstance(runtime, dict):
        return None
    return (
        runtime.get("host"),
        runtime.get("home"),
        runtime.get("dataDir"),
        runtime.get("runtimeDir"),
        runtime.get("dataLockDir"),
        runtime.get("vinextPort"),
        runtime.get("runtimeStatusPort"),
    )


def _requested_runtime_layout(layout: RuntimeLayout) -> tuple[object, ...]:
    return (
        RUNTIME_HOST,
        str(layout.home),
        str(layout.data_dir),
        str(layout.runtime_dir),
        str(layout.data_lock_dir),
        layout.vinext_port,
        layout.runtime_status_port,
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


def start_manager(
    open_browser: bool = False,
    *,
    layout: RuntimeLayout | None = None,
) -> int:
    try:
        requested_layout = layout or load_runtime_layout(application_root=ROOT)
        requested_settings = load_runtime_settings()
    except RuntimeTimezoneDatabaseError as error:
        if not python_dependencies_are_ready():
            return 1
        print(f"Rardar runtime configuration error: {error}")
        return 2
    except RuntimeSettingsError as error:
        print(f"Rardar runtime configuration error: {error}")
        return 2
    control_path = requested_layout.control_path
    status_path = requested_layout.status_path
    control = _read_json(control_path) or {}
    existing_status = _read_json(status_path) or {}
    manager_active = _manager_status_matches_control(existing_status, control)
    if manager_active:
        requested = (
            requested_settings.schedule_at,
            requested_settings.schedule_timezone,
            requested_settings.stale_after_seconds,
        )
        active_layout = _active_runtime_layout(existing_status)
        if (
            _active_runtime_settings(existing_status) != requested
            or (
                active_layout is not None
                and active_layout != _requested_runtime_layout(requested_layout)
            )
        ):
            print(
                "Rardar runtime configuration changes require "
                "npm run local:stop followed by npm run local:start; "
                "the active runtime was not changed"
            )
            return 1
        if existing_status.get("state") == "healthy":
            print(f"Rardar is already managed at {requested_layout.website_url}")
            if open_browser:
                webbrowser.open(requested_layout.website_url)
            return 0
        data = existing_status.get("data") or {}
        services = existing_status.get("services") or {}
        website_state = (services.get("website") or {}).get("state")
        scheduler_state = (services.get("scheduler") or {}).get("state")
        if (
            data.get("freshness") == "stale"
            and website_state == scheduler_state == "healthy"
        ):
            print(
                f"Rardar is running at {requested_layout.website_url}, "
                "but published data is stale"
            )
            return 0
        website = ((existing_status.get("services") or {}).get("website") or {})
        detail = website.get("lastError") or existing_status.get("message") or "health check failed"
        print(f"Rardar is managed but degraded: {_short_health_error(detail)}")
        return 1

    if not python_dependencies_are_ready():
        return 1

    try:
        node = find_node()
    except RuntimeError as error:
        print(str(error))
        return 1
    required_javascript_paths = (
        requested_layout.home / "node_modules" / "vinext" / "dist" / "cli.js",
        requested_layout.home / "node_modules" / "vite" / "bin" / "vite.js",
        requested_layout.home / "vite.config.ts",
        requested_layout.home / ".openai" / "hosting.json",
        requested_layout.home / "app" / "runtime-readiness.mjs",
        requested_layout.home / "build" / "published-data-bridge.ts",
        requested_layout.home / "build" / "sites-vite-plugin.ts",
        requested_layout.home / "worker" / "index.ts",
    )
    missing_javascript_path = next(
        (path for path in required_javascript_paths if not path.is_file()),
        None,
    )
    if missing_javascript_path is not None:
        print(
            "Rardar runtime dependencies are incomplete: "
            f"missing {missing_javascript_path}; install JavaScript dependencies in RARDAR_HOME"
        )
        return 1

    _stop_recorded_processes(existing_status, layout=requested_layout)
    control_path.unlink(missing_ok=True)

    requested_layout.log_dir.mkdir(parents=True, exist_ok=True)
    manager_log_path = requested_layout.log_dir / "manager.log"
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
    manager_environment = _runtime_child_environment(
        requested_layout,
        requested_settings,
        node,
    )
    try:
        manager_process = subprocess.Popen(
            command,
            cwd=requested_layout.home,
            env=manager_environment,
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
            cwd=requested_layout.home,
            env=manager_environment,
            stdin=subprocess.DEVNULL,
            stdout=manager_log,
            stderr=subprocess.STDOUT,
            creationflags=fallback_flags,
            close_fds=True,
        )
    manager_log.close()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = _read_json(status_path) or {}
        active_control = _read_json(control_path) or {}
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
            print(f"Rardar is running at {requested_layout.website_url}")
            if data_only_degraded:
                print(
                    "Warning: published data is STALE "
                    f"(threshold {requested_settings.stale_after_hours}h)"
                )
            if open_browser:
                webbrowser.open(requested_layout.website_url)
            return 0
        time.sleep(0.5)
    print(
        "Rardar manager started, but services are not healthy yet. "
        f"Check {requested_layout.log_dir}."
    )
    return 1


def stop_manager(*, layout: RuntimeLayout | None = None) -> int:
    try:
        resolved_layout = layout or load_runtime_layout(application_root=ROOT)
    except RuntimeSettingsError as error:
        print(f"Rardar runtime configuration error: {error}")
        return 2
    control_path = resolved_layout.control_path
    status_path = resolved_layout.status_path
    control = _read_json(control_path) or {}
    manager_pid = control.get("pid")
    status = _read_json(status_path) or {}
    if not isinstance(manager_pid, int) or not process_is_alive(manager_pid):
        _stop_recorded_processes(
            status,
            include_manager=False,
            layout=resolved_layout,
        )
        write_runtime_status(
            _stopped_status(
                layout=resolved_layout,
                scheduler_status_path=resolved_layout.scheduler_status_path,
            ),
            status_path,
        )
        control_path.unlink(missing_ok=True)
        print("Rardar is not running under the local manager")
        return 0

    if process_matches(
        manager_pid,
        (
            "pipeline.runtime run",
            "pipeline\\runtime.py run",
            "pipeline.runtime service",
            "pipeline\\runtime.py service",
        ),
    ):
        _terminate_process_tree(manager_pid)
    deadline = time.monotonic() + 10
    while process_is_alive(manager_pid) and time.monotonic() < deadline:
        time.sleep(0.25)
    _stop_recorded_processes(
        status,
        include_manager=False,
        layout=resolved_layout,
    )
    control_path.unlink(missing_ok=True)
    write_runtime_status(
        _stopped_status(
            "本地运行管理器已停止",
            layout=resolved_layout,
            scheduler_status_path=resolved_layout.scheduler_status_path,
        ),
        status_path,
    )
    print("Rardar local services stopped")
    return 0


def show_status(*, layout: RuntimeLayout | None = None) -> int:
    try:
        resolved_layout = layout or load_runtime_layout(application_root=ROOT)
    except RuntimeSettingsError as error:
        print(json.dumps({"state": "invalid", "error": str(error)}, ensure_ascii=False))
        return 2
    status = _read_json(resolved_layout.status_path) or _stopped_status(
        layout=resolved_layout,
        scheduler_status_path=resolved_layout.scheduler_status_path,
    )
    control = _read_json(resolved_layout.control_path) or {}
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
    subparsers.add_parser("service")
    arguments = parser.parse_args()

    if arguments.command == "start":
        raise SystemExit(start_manager(arguments.open))
    if arguments.command == "stop":
        raise SystemExit(stop_manager())
    if arguments.command == "status":
        raise SystemExit(show_status())
    raise SystemExit(run_manager(service_mode=arguments.command == "service"))


if __name__ == "__main__":
    main()
