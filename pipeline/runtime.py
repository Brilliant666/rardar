"""Keep the local Rardar website and daily refresh scheduler alive."""

from __future__ import annotations

import argparse
import json
import os
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


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
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 2
SCHEDULER_HEARTBEAT_MAX_AGE = 125
SERVICE_STARTUP_GRACE = 90
_latest_status: dict[str, Any] = {}
_latest_status_lock = threading.Lock()


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


def _scheduler_details() -> dict[str, Any]:
    status = _read_json(SCHEDULER_STATUS_PATH) or {}
    return {
        "refreshState": status.get("state", "scheduled"),
        "schedule": status.get("schedule", {"time": "08:00", "timezone": "Asia/Shanghai"}),
        "nextRunAt": status.get("nextRunAt"),
        "lastRunStartedAt": status.get("lastRunStartedAt"),
        "lastRunCompletedAt": status.get("lastRunCompletedAt"),
        "lastError": status.get("lastError"),
        "retryAttempt": status.get("retryAttempt"),
        "heartbeatAt": status.get("heartbeatAt"),
        "dataAuditStatus": status.get("dataAuditStatus"),
        "dataAuditWarningCount": status.get("dataAuditWarningCount"),
        "dataAuditSummary": status.get("dataAuditSummary"),
    }


def _stopped_status(message: str = "本地运行管理器未启动") -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "state": "stopped",
        "checkedAt": utc_now(),
        "message": message,
        "managerPid": None,
        "services": {
            "website": {"state": "stopped", "pid": None, "url": LOCAL_URL},
            "scheduler": {
                "state": "stopped",
                "pid": None,
                **_scheduler_details(),
            },
        },
    }


def _run_manager() -> int:
    current_pid = os.getpid()
    existing = _read_json(CONTROL_PATH) or {}
    existing_pid = existing.get("pid")
    if isinstance(existing_pid, int) and existing_pid != current_pid and process_is_alive(existing_pid):
        return 0

    node = find_node()
    environment = os.environ.copy()
    environment["PATH"] = str(node.parent) + os.pathsep + environment.get("PATH", "")
    environment["PYTHONUNBUFFERED"] = "1"

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
            "08:00",
            "--timezone",
            "Asia/Shanghai",
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

    starting_status = _stopped_status("网站与每日刷新正在启动")
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
                    time.sleep(2)
                    service.start(environment)

            website_state = "healthy" if website.poll() is None and port_is_open() else "starting"
            scheduler_details = _scheduler_details()
            scheduler_state = (
                scheduler_heartbeat_state(
                    scheduler_details.get("heartbeatAt"),
                    scheduler.started_at,
                )
                if scheduler.poll() is None
                else "restarting"
            )
            if scheduler_state == "stale":
                scheduler.last_error = "scheduler heartbeat became stale"
                scheduler.stop()
                scheduler.start(environment)
                scheduler_state = "restarting"
            overall_state = "healthy" if website_state == scheduler_state == "healthy" else "degraded"
            scheduler_payload = _service_payload(scheduler, scheduler_state)
            scheduler_payload.update(scheduler_details)
            scheduler_payload["processError"] = scheduler.last_error
            payload = {
                "schemaVersion": 1,
                "state": overall_state,
                "checkedAt": utc_now(),
                "message": "网站与每日刷新均由本地管理器看护" if overall_state == "healthy" else "服务正在启动或恢复",
                "managerPid": current_pid,
                "services": {
                    "website": {**_service_payload(website, website_state), "url": LOCAL_URL},
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
        write_runtime_status(_stopped_status("本地运行管理器已停止"))
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
        return _run_manager()
    finally:
        release_manager_lock(manager_lock)


def start_manager(open_browser: bool = False) -> int:
    control = _read_json(CONTROL_PATH) or {}
    manager_pid = control.get("pid")
    existing_status = _read_json(STATUS_PATH) or {}
    manager_healthy = (
        isinstance(manager_pid, int)
        and process_is_alive(manager_pid)
        and heartbeat_is_fresh(existing_status.get("checkedAt"))
    )
    if manager_healthy:
        print(f"Rardar is already managed at {LOCAL_URL}")
        if open_browser:
            webbrowser.open(LOCAL_URL)
        return 0

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
        subprocess.Popen(
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
        subprocess.Popen(
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
        if status.get("state") == "healthy" and heartbeat_is_fresh(status.get("checkedAt")):
            print(f"Rardar is running at {LOCAL_URL}")
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
    manager_pid = status.get("managerPid")
    fresh = heartbeat_is_fresh(status.get("checkedAt"))
    manager_alive = isinstance(manager_pid, int) and process_is_alive(manager_pid)
    if not fresh or not manager_alive:
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
