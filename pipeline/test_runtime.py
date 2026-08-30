from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from pipeline.runtime import (
    ManagedService,
    WebsiteHealth,
    _manager_status_matches_control,
    _run_manager,
    _runtime_child_environment,
    _scheduler_details,
    _stop_recorded_processes,
    _stopped_status,
    _terminate_windows_process_tree,
    _website_child_environment,
    _windows_taskkill,
    acquire_manager_lock,
    default_runtime_dir,
    heartbeat_is_fresh,
    missing_python_dependencies,
    manager_ownership_lock_path,
    parse_website_health,
    parse_node_version,
    python_dependencies_are_ready,
    release_manager_lock,
    run_manager,
    rotate_log,
    scheduler_heartbeat_state,
    show_status,
    start_manager,
    stop_manager,
)
from pipeline.runtime_settings import (
    MANAGER_ALREADY_RUNNING_EXIT_CODE,
    SCHEDULER_ALREADY_RUNNING_EXIT_CODE,
    RuntimeLayout,
    RuntimeSettings,
    RuntimeTimezoneDatabaseError,
)
from pipeline.generations import resolve_current_generation


ROOT = Path(__file__).resolve().parents[1]


def isolated_layout(root: Path, *, vinext_port: int = 43111, status_port: int = 43112) -> RuntimeLayout:
    return RuntimeLayout(
        home=ROOT,
        data_dir=(root / "data").resolve(),
        runtime_dir=(root / "runtime").resolve(),
        data_lock_dir=(root / "locks").resolve(),
        vinext_port=vinext_port,
        runtime_status_port=status_port,
    )


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def wait_until(predicate, timeout: float = 90) -> object:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for managed runtime: {last_error}")


def loopback_http_status(port: int, path: str) -> int:
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path, headers={"Cache-Control": "no-store"})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def port_is_closed(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return False
    except OSError:
        return True


def health_payload(
    *,
    captured_at: str,
    age_seconds: float,
    freshness: str = "fresh",
    generation_id: str = "generation-2",
) -> dict[str, object]:
    stale = freshness == "stale"
    return {
        "schemaVersion": 1,
        "status": "degraded" if stale else "healthy",
        **({"reason": "published_data_stale"} if stale else {}),
        "generationId": generation_id,
        "data": {
            "freshness": freshness,
            "snapshotCapturedAt": captured_at,
            "ageSeconds": age_seconds,
            "staleAfterSeconds": 129600,
        },
        "schedule": {"at": "08:00", "timezone": "Asia/Shanghai"},
    }


class RuntimeTests(unittest.TestCase):
    def test_website_health_requires_a_valid_generation_response(self) -> None:
        captured_at = datetime.now(timezone.utc).isoformat()
        healthy = parse_website_health(
            200,
            json.dumps(health_payload(captured_at=captured_at, age_seconds=0)).encode(),
        )
        self.assertEqual(healthy.state, "healthy")
        self.assertEqual(healthy.generation_id, "generation-2")
        self.assertEqual(healthy.data_freshness, "fresh")

        cases = (
            (200, {"status": "healthy"}),
            (200, health_payload(captured_at=captured_at, age_seconds=0, generation_id="../escape")),
            (
                200,
                {
                    **health_payload(captured_at=captured_at, age_seconds=0),
                    "status": "degraded",
                    "reason": "published_data_stale",
                },
            ),
        )
        for status, payload in cases:
            with self.subTest(payload=payload):
                result = parse_website_health(status, json.dumps(payload).encode())
                self.assertEqual(result.state, "degraded")
                self.assertIsNone(result.generation_id)
                self.assertIn("invalid contract", str(result.error))

    def test_website_health_accepts_integrity_valid_stale_data_without_failing_the_service(self) -> None:
        captured = datetime.now(timezone.utc) - timedelta(hours=49)
        result = parse_website_health(
            200,
            json.dumps(
                health_payload(
                    captured_at=captured.isoformat(),
                    age_seconds=49 * 3600,
                    freshness="stale",
                )
            ).encode(),
        )
        self.assertEqual(result.state, "healthy")
        self.assertEqual(result.generation_id, "generation-2")
        self.assertEqual(result.data_freshness, "stale")
        self.assertEqual(result.stale_after_seconds, 129600)

    def test_website_health_keeps_the_worker_boundary_decision_during_transit(self) -> None:
        boundary_capture = datetime.now(timezone.utc) - timedelta(seconds=129600)
        fresh = parse_website_health(
            200,
            json.dumps(
                health_payload(
                    captured_at=boundary_capture.isoformat(),
                    age_seconds=129600,
                    freshness="fresh",
                )
            ).encode(),
        )
        self.assertEqual(fresh.state, "healthy")
        self.assertEqual(fresh.data_freshness, "fresh")

        stale_capture = datetime.now(timezone.utc) - timedelta(seconds=129600.001)
        stale = parse_website_health(
            200,
            json.dumps(
                health_payload(
                    captured_at=stale_capture.isoformat(),
                    age_seconds=129600.001,
                    freshness="stale",
                )
            ).encode(),
        )
        self.assertEqual(stale.state, "healthy")
        self.assertEqual(stale.data_freshness, "stale")

    def test_website_health_reports_bounded_http_and_json_errors(self) -> None:
        failed = parse_website_health(
            503,
            json.dumps({"status": "degraded", "error": "x" * 500}).encode(),
        )
        self.assertEqual(failed.state, "degraded")
        self.assertIsNone(failed.generation_id)
        self.assertLessEqual(len(str(failed.error)), 240)
        self.assertIn("HTTP 503", str(failed.error))

        invalid = parse_website_health(200, b"not-json")
        self.assertEqual(invalid.state, "degraded")
        self.assertIn("invalid JSON", str(invalid.error))

        oversized = parse_website_health(200, b"x" * (64 * 1024 + 1))
        self.assertEqual(oversized.state, "degraded")
        self.assertIn("exceeded", str(oversized.error))

    def test_scheduler_heartbeat_distinguishes_startup_and_stale_processes(self) -> None:
        now = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
        self.assertEqual(
            scheduler_heartbeat_state(
                (now - timedelta(seconds=60)).isoformat(),
                (now - timedelta(hours=1)).isoformat(),
                now,
            ),
            "healthy",
        )
        self.assertEqual(
            scheduler_heartbeat_state(None, (now - timedelta(seconds=30)).isoformat(), now),
            "starting",
        )
        self.assertEqual(
            scheduler_heartbeat_state(
                (now - timedelta(seconds=1)).isoformat(),
                now.isoformat(),
                now,
            ),
            "starting",
        )
        self.assertEqual(
            scheduler_heartbeat_state(
                (now - timedelta(seconds=130)).isoformat(),
                (now - timedelta(minutes=5)).isoformat(),
                now,
            ),
            "stale",
        )

    def test_scheduler_details_exposes_data_audit_state(self) -> None:
        status = {
            "state": "healthy",
            "schedule": {"time": "23:59", "timezone": "UTC"},
            "dataAuditStatus": "degraded",
            "dataAuditWarningCount": 2,
            "dataAuditSummary": {"observedProjectCount": 30},
            "retryable": False,
            "remoteAnalysisErrorCode": "remote_clone_process_tree_cleanup_failed",
        }
        with patch("pipeline.runtime._read_json", return_value=status):
            details = _scheduler_details()

        self.assertEqual(details["dataAuditStatus"], "degraded")
        self.assertEqual(details["dataAuditWarningCount"], 2)
        self.assertEqual(details["dataAuditSummary"], {"observedProjectCount": 30})
        self.assertEqual(details["schedule"], {"time": "08:00", "timezone": "Asia/Shanghai"})
        self.assertFalse(details["retryable"])
        self.assertEqual(
            details["remoteAnalysisErrorCode"],
            "remote_clone_process_tree_cleanup_failed",
        )

    def test_scheduler_details_exposes_only_path_free_producer_telemetry(self) -> None:
        status = {
            "producer": {
                "enabled": True,
                "state": "warming_up",
                "nextObservationAt": "2026-08-26T08:00:00+00:00",
                "secret": "must-not-pass",
                "observation": {
                    "state": "healthy",
                    "lastCaptureId": "trending-v1-20260826T060000Z",
                    "capturePath": "/var/lib/rardar/data/observations/private.json",
                    "authorization": "Bearer must-not-pass",
                },
                "explosion": {
                    "state": "warming_up",
                    "candidatePath": "/var/lib/rardar/data/generations/candidate",
                },
                "discover": {
                    "state": "degraded",
                    "generationId": "discover-generation",
                    "stageCounts": {
                        "just_discovered": 2,
                        "rising": 3,
                        "near_validation": 1,
                        "candidatePath": "/var/lib/rardar/private/candidate",
                    },
                    "coverage": {
                        "state": "degraded",
                        "querySuccessCount": 5,
                        "queryFailureCount": 1,
                        "upstreamError": "Bearer must-not-pass",
                    },
                    "sourcePath": "/var/lib/rardar/data/artifacts/private.json",
                },
            }
        }
        with patch("pipeline.runtime._read_json", return_value=status):
            details = _scheduler_details()

        producer = details["producer"]
        self.assertTrue(producer["enabled"])
        self.assertEqual(producer["observation"]["state"], "healthy")
        self.assertEqual(producer["discover"]["stageCounts"]["rising"], 3)
        self.assertEqual(producer["discover"]["coverage"]["queryFailureCount"], 1)
        serialized = json.dumps(producer)
        self.assertNotIn("capturePath", serialized)
        self.assertNotIn("candidatePath", serialized)
        self.assertNotIn("sourcePath", serialized)
        self.assertNotIn("upstreamError", serialized)
        self.assertNotIn("must-not-pass", serialized)

    def test_producer_flag_and_token_only_reach_scheduler_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = isolated_layout(Path(directory).resolve())
            settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
            with patch.dict(os.environ, {"GITHUB_TOKEN": "scheduler-only-secret"}):
                scheduler = _runtime_child_environment(
                    layout,
                    settings,
                    Path(directory) / "node" / "node.exe",
                )
                website = _website_child_environment(scheduler)

        self.assertEqual(
            scheduler["RARDAR_TRENDING_PRODUCER_ENABLED"],
            "true",
        )
        self.assertEqual(scheduler["GITHUB_TOKEN"], "scheduler-only-secret")
        self.assertNotIn("RARDAR_TRENDING_PRODUCER_ENABLED", website)
        self.assertNotIn("GITHUB_TOKEN", website)

    def test_scheduler_details_rejects_telemetry_from_another_process(self) -> None:
        status = {
            "processId": 41,
            "state": "healthy",
            "heartbeatAt": datetime.now(timezone.utc).isoformat(),
            "nextRunAt": "2026-08-11T00:00:00+00:00",
        }
        with patch("pipeline.runtime._read_json", return_value=status):
            details = _scheduler_details(expected_process_id=42)

        self.assertFalse(details["telemetryTrusted"])
        self.assertEqual(details["reportedProcessId"], 41)
        self.assertEqual(details["refreshState"], "scheduled")
        self.assertIsNone(details["heartbeatAt"])
        self.assertIsNone(details["nextRunAt"])

    def test_scheduler_details_rejects_telemetry_when_no_managed_child_exists(self) -> None:
        status = {
            "processId": 41,
            "state": "healthy",
            "heartbeatAt": datetime.now(timezone.utc).isoformat(),
            "nextRunAt": "2026-08-11T00:00:00+00:00",
            "lastSuccessfulRefreshAt": "2026-08-10T00:02:00+00:00",
        }
        with patch("pipeline.runtime._read_json", return_value=status):
            details = _scheduler_details(expected_process_id=None)

        self.assertFalse(details["telemetryTrusted"])
        self.assertEqual(details["reportedProcessId"], 41)

    def test_scheduler_details_accepts_only_the_windows_venv_redirector_direct_child(self) -> None:
        expected_command = (
            r"C:\venv\Scripts\python.exe",
            "-m",
            "pipeline.scheduler",
            "--data-dir",
            r"C:\rardar-data",
        )
        status = {
            "state": "scheduled",
            "processId": 43,
            "heartbeatAt": "2026-08-10T00:00:00+00:00",
            "nextRunAt": "2026-08-11T00:00:00+00:00",
        }
        with patch("pipeline.runtime._read_json", return_value=status), patch(
            "pipeline.runtime.os.name", "nt"
        ), patch(
            "pipeline.runtime._windows_process_creation_time", return_value=120
        ), patch(
            "pipeline.runtime._windows_process_command_identity",
            return_value=(
                42,
                r"c:\venv\scripts\python.exe",
                expected_command[1:],
            ),
        ):
            details = _scheduler_details(
                expected_process_id=42,
                expected_process_creation_time=100,
                expected_process_command=expected_command,
            )
        self.assertTrue(details["telemetryTrusted"])
        self.assertEqual(details["reportedProcessId"], 43)

        with patch("pipeline.runtime._read_json", return_value=status), patch(
            "pipeline.runtime.os.name", "nt"
        ), patch(
            "pipeline.runtime._windows_process_creation_time", return_value=120
        ), patch(
            "pipeline.runtime._windows_process_command_identity",
            return_value=(99, r"c:\venv\scripts\python.exe", expected_command[1:]),
        ):
            details = _scheduler_details(
                expected_process_id=42,
                expected_process_creation_time=100,
                expected_process_command=expected_command,
            )
        self.assertFalse(details["telemetryTrusted"])
        self.assertIsNone(details["heartbeatAt"])
        self.assertIsNone(details["nextRunAt"])
        self.assertIsNone(details["lastSuccessfulRefreshAt"])

    def test_windows_scheduler_redirect_child_rejects_older_creation_identity(self) -> None:
        status = {"state": "scheduled", "processId": 43}
        command = (r"C:\venv\Scripts\python.exe", "-m", "pipeline.scheduler")
        with patch("pipeline.runtime._read_json", return_value=status), patch(
            "pipeline.runtime.os.name", "nt"
        ), patch(
            "pipeline.runtime._windows_process_creation_time", return_value=99
        ), patch(
            "pipeline.runtime._windows_process_command_identity"
        ) as command_identity:
            details = _scheduler_details(
                expected_process_id=42,
                expected_process_creation_time=100,
                expected_process_command=command,
            )
        self.assertFalse(details["telemetryTrusted"])
        command_identity.assert_not_called()

    def test_windows_scheduler_rejects_reused_launcher_pid(self) -> None:
        status = {"state": "scheduled", "processId": 42}
        command = (r"C:\venv\Scripts\python.exe", "-m", "pipeline.scheduler")
        with patch("pipeline.runtime._read_json", return_value=status), patch(
            "pipeline.runtime.os.name", "nt"
        ), patch(
            "pipeline.runtime._windows_process_creation_time", return_value=101
        ):
            details = _scheduler_details(
                expected_process_id=42,
                expected_process_creation_time=100,
                expected_process_command=command,
            )

        self.assertFalse(details["telemetryTrusted"])
        self.assertIsNone(details["heartbeatAt"])

    def test_windows_scheduler_redirect_child_rejects_wrong_executable(self) -> None:
        status = {"state": "scheduled", "processId": 43}
        command = (r"C:\venv\Scripts\python.exe", "-m", "pipeline.scheduler")
        with patch("pipeline.runtime._read_json", return_value=status), patch(
            "pipeline.runtime.os.name", "nt"
        ), patch(
            "pipeline.runtime._windows_process_creation_time", return_value=120
        ), patch(
            "pipeline.runtime._windows_process_command_identity",
            return_value=(42, r"c:\windows\system32\cmd.exe", command[1:]),
        ):
            details = _scheduler_details(
                expected_process_id=42,
                expected_process_creation_time=100,
                expected_process_command=command,
            )
        self.assertFalse(details["telemetryTrusted"])

    def test_windows_scheduler_redirect_child_rejects_marker_as_argument(self) -> None:
        status = {"state": "scheduled", "processId": 43}
        command = (r"C:\venv\Scripts\python.exe", "-m", "pipeline.scheduler")
        with patch("pipeline.runtime._read_json", return_value=status), patch(
            "pipeline.runtime.os.name", "nt"
        ), patch(
            "pipeline.runtime._windows_process_creation_time", return_value=120
        ), patch(
            "pipeline.runtime._windows_process_command_identity",
            return_value=(
                42,
                r"c:\venv\scripts\python.exe",
                ("-c", "print('pipeline.scheduler')"),
            ),
        ):
            details = _scheduler_details(
                expected_process_id=42,
                expected_process_creation_time=100,
                expected_process_command=command,
            )
        self.assertFalse(details["telemetryTrusted"])

    def test_runtime_logs_rotate_with_bounded_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "website.log"
            log_path.write_bytes(b"first-version")
            rotate_log(log_path, max_bytes=5, backup_count=2)
            self.assertFalse(log_path.exists())
            self.assertEqual((Path(temporary) / "website.log.1").read_bytes(), b"first-version")

            log_path.write_bytes(b"second-version")
            rotate_log(log_path, max_bytes=5, backup_count=2)
            self.assertEqual((Path(temporary) / "website.log.1").read_bytes(), b"second-version")
            self.assertEqual((Path(temporary) / "website.log.2").read_bytes(), b"first-version")

    def test_manager_lock_allows_only_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "manager.lock"
            first = acquire_manager_lock(lock_path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(acquire_manager_lock(lock_path))
            finally:
                release_manager_lock(first)

            second = acquire_manager_lock(lock_path)
            self.assertIsNotNone(second)
            release_manager_lock(second)

    def test_windows_tree_cleanup_uses_only_exact_pid_tree_commands(self) -> None:
        with patch("pipeline.runtime.subprocess.run") as run:
            _windows_taskkill(9123, force=False)
            _windows_taskkill(9123, force=True)

        first = run.call_args_list[0].args[0]
        second = run.call_args_list[1].args[0]
        self.assertEqual(first, ["taskkill", "/PID", "9123", "/T"])
        self.assertEqual(second, ["taskkill", "/PID", "9123", "/T", "/F"])
        self.assertNotIn("/IM", first + second)

    def test_windows_tree_cleanup_does_not_kill_a_reused_launcher_pid(self) -> None:
        creation_times = {9123: 200, 9124: 210}
        with patch(
            "pipeline.runtime._windows_process_parent_map",
            return_value={9123: 1, 9124: 9123},
        ), patch(
            "pipeline.runtime._windows_process_creation_time",
            side_effect=lambda process_id: creation_times.get(process_id),
        ), patch("pipeline.runtime._windows_taskkill") as taskkill:
            _terminate_windows_process_tree(
                9123,
                root_creation_time=100,
                root_exit_time=150,
            )

        taskkill.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX process-group ownership test")
    def test_restart_reaps_orphaned_child_group_without_touching_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            child_pid_path = root / "child.pid"
            environment = {
                **os.environ,
                "RARDAR_HOME": str(ROOT),
                "RARDAR_TEST_CHILD_PID": str(child_pid_path),
            }
            leader_program = (
                "import os,pathlib,subprocess,sys;"
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(120)']);"
                "pathlib.Path(os.environ['RARDAR_TEST_CHILD_PID']).write_text("
                "str(child.pid),encoding='utf-8')"
            )
            sibling = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            service = ManagedService(
                "orphan-fixture",
                [sys.executable, "-c", leader_program],
                root / "service.log",
            )
            try:
                service.start(environment)
                first_leader = service.process
                self.assertIsNotNone(first_leader)
                wait_until(child_pid_path.exists, timeout=10)
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                first_leader.wait(timeout=10)
                self.assertTrue(pid_is_alive(child_pid))
                self.assertTrue(pid_is_alive(sibling.pid))

                service.command = [sys.executable, "-c", "import time; time.sleep(120)"]
                service.start(environment)

                self.assertEqual(service.restart_count, 1)
                self.assertNotEqual(service.process.pid, first_leader.pid)
                wait_until(lambda: not pid_is_alive(child_pid), timeout=10)
                self.assertTrue(pid_is_alive(sibling.pid))
                service.stop()
                self.assertTrue(pid_is_alive(sibling.pid))
            finally:
                try:
                    service.stop()
                except RuntimeError:
                    pass
                if sibling.poll() is None:
                    os.killpg(sibling.pid, signal.SIGKILL)
                    sibling.wait(timeout=10)

    def test_parses_node_version(self) -> None:
        self.assertEqual(parse_node_version("v22.13.1\n"), (22, 13, 1))
        self.assertEqual(parse_node_version("22.14.0-beta"), (22, 14, 0))
        self.assertIsNone(parse_node_version("unknown"))

    def test_heartbeat_requires_recent_timestamp(self) -> None:
        now = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
        self.assertTrue(heartbeat_is_fresh((now - timedelta(seconds=20)).isoformat(), now))
        self.assertFalse(heartbeat_is_fresh((now - timedelta(seconds=60)).isoformat(), now))
        self.assertFalse(heartbeat_is_fresh(None, now))

    def test_runtime_directory_can_live_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {"RARDAR_RUNTIME_DIR": temporary}):
            self.assertEqual(default_runtime_dir(), Path(temporary).resolve())

    def test_stale_runtime_cleanup_matches_the_direct_vite_runner_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = isolated_layout(Path(temporary).resolve(), vinext_port=43131)
            status = {
                "services": {
                    "website": {"pid": 101},
                    "scheduler": {"pid": 102},
                }
            }
            with (
                patch("pipeline.runtime.process_matches", return_value=True) as matches,
                patch("pipeline.runtime._terminate_process_tree") as terminate,
            ):
                _stop_recorded_processes(
                    status,
                    include_manager=False,
                    layout=layout,
                )

        self.assertEqual(matches.call_count, 2)
        website_call = matches.call_args_list[0]
        self.assertEqual(website_call.args[0], 101)
        self.assertEqual(website_call.kwargs, {"require_all": True})
        website_markers = website_call.args[1]
        self.assertIn(
            str(ROOT / "node_modules" / "vite" / "bin" / "vite.js"),
            website_markers,
        )
        self.assertIn("--configLoader", website_markers)
        self.assertIn("runner", website_markers)
        self.assertIn("43131", website_markers)
        self.assertEqual(
            [entry.args[0] for entry in terminate.call_args_list],
            [101, 102],
        )

    def test_python_dependency_probe_reports_unavailable_modules(self) -> None:
        with patch("pipeline.runtime.importlib.util.find_spec", side_effect=[object(), None]):
            self.assertEqual(missing_python_dependencies(("available", "missing")), ("missing",))

    def test_local_start_stops_before_spawning_when_python_dependency_is_missing(self) -> None:
        with (
            patch("pipeline.runtime._read_json", return_value={}),
            patch("pipeline.runtime.missing_python_dependencies", return_value=("jsonschema",)),
            patch("pipeline.runtime._stop_recorded_processes") as stop_processes,
            patch("pipeline.runtime.subprocess.Popen") as spawn_process,
            patch("builtins.print") as print_message,
        ):
            exit_code = start_manager()

        self.assertEqual(exit_code, 1)
        stop_processes.assert_not_called()
        spawn_process.assert_not_called()
        output = "\n".join(str(call.args[0]) for call in print_message.call_args_list)
        self.assertIn("jsonschema", output)
        self.assertIn("python -m pip install -r requirements.txt", output)

    def test_local_start_rejects_invalid_runtime_configuration_before_side_effects(self) -> None:
        with (
            patch.dict("os.environ", {"RARDAR_SCHEDULE_AT": "25:00"}),
            patch("pipeline.runtime._read_json") as read_status,
            patch("pipeline.runtime._stop_recorded_processes") as stop_processes,
            patch("pipeline.runtime.subprocess.Popen") as spawn_process,
            patch("pipeline.runtime.write_runtime_status") as write_status,
        ):
            exit_code = start_manager()
        self.assertEqual(exit_code, 2)
        read_status.assert_not_called()
        stop_processes.assert_not_called()
        spawn_process.assert_not_called()
        write_status.assert_not_called()

    def test_local_start_rejects_invalid_vite_allowed_hosts_before_side_effects(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {"__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS": ".cosflow.icu"},
            ),
            patch("pipeline.runtime._read_json") as read_status,
            patch("pipeline.runtime._stop_recorded_processes") as stop_processes,
            patch("pipeline.runtime.subprocess.Popen") as spawn_process,
            patch("pipeline.runtime.write_runtime_status") as write_status,
        ):
            exit_code = start_manager()
        self.assertEqual(exit_code, 2)
        read_status.assert_not_called()
        stop_processes.assert_not_called()
        spawn_process.assert_not_called()
        write_status.assert_not_called()

    def test_local_start_reports_missing_timezone_data_before_any_side_effects(self) -> None:
        error = RuntimeTimezoneDatabaseError("timezone database unavailable")
        with (
            patch("pipeline.runtime.load_runtime_settings", side_effect=error),
            patch("pipeline.runtime.missing_python_dependencies", return_value=("tzdata",)),
            patch("pipeline.runtime._read_json") as read_status,
            patch("pipeline.runtime._stop_recorded_processes") as stop_processes,
            patch("pipeline.runtime.subprocess.Popen") as spawn_process,
            patch("pipeline.runtime.write_runtime_status") as write_status,
            patch("builtins.print") as print_message,
        ):
            exit_code = start_manager()

        self.assertEqual(exit_code, 1)
        read_status.assert_not_called()
        stop_processes.assert_not_called()
        spawn_process.assert_not_called()
        write_status.assert_not_called()
        output = "\n".join(str(call.args[0]) for call in print_message.call_args_list)
        self.assertIn("tzdata", output)
        self.assertIn("python -m pip install -r requirements.txt", output)

    def test_unknown_timezone_with_dependencies_installed_is_a_configuration_error(self) -> None:
        error = RuntimeTimezoneDatabaseError("unknown IANA timezone")
        with (
            patch("pipeline.runtime.load_runtime_settings", side_effect=error),
            patch("pipeline.runtime.missing_python_dependencies", return_value=()),
            patch("pipeline.runtime._read_json") as read_status,
            patch("pipeline.runtime._stop_recorded_processes") as stop_processes,
            patch("pipeline.runtime.subprocess.Popen") as spawn_process,
            patch("builtins.print") as print_message,
        ):
            exit_code = start_manager()

        self.assertEqual(exit_code, 2)
        read_status.assert_not_called()
        stop_processes.assert_not_called()
        spawn_process.assert_not_called()
        output = "\n".join(str(call.args[0]) for call in print_message.call_args_list)
        self.assertIn("configuration error", output)

    def test_stopped_status_and_stop_do_not_require_timezone_data(self) -> None:
        with patch(
            "pipeline.runtime.load_runtime_settings",
            side_effect=AssertionError("stopped status must not load timezone data"),
        ):
            status = _stopped_status()
        self.assertEqual(status["schedule"]["at"], "08:00")
        self.assertEqual(status["schedule"]["timezone"], "Asia/Shanghai")
        self.assertFalse(status["services"]["scheduler"]["telemetryTrusted"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = isolated_layout(root)
            control_path = layout.control_path
            status_path = layout.status_path
            with (
                patch("pipeline.runtime._read_json", return_value={}),
                patch(
                    "pipeline.runtime.load_runtime_settings",
                    side_effect=AssertionError("stop must not load timezone data"),
                ),
            ):
                self.assertEqual(stop_manager(layout=layout), 0)
            stopped = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(stopped["state"], "stopped")
            self.assertEqual(stopped["schedule"]["timezone"], "Asia/Shanghai")
            with (
                patch("pipeline.runtime.load_runtime_settings", side_effect=AssertionError),
                patch("builtins.print") as print_status,
            ):
                self.assertEqual(show_status(layout=layout), 1)
            structured = json.loads(print_status.call_args.args[0])
            self.assertEqual(structured["state"], "stale")
            self.assertEqual(structured["schedule"]["at"], "08:00")

    def test_local_start_keeps_an_existing_degraded_manager_without_restarting(self) -> None:
        degraded = {
            "state": "degraded",
            "managerPid": 123,
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "services": {
                "website": {
                    "state": "degraded",
                    "lastError": "health endpoint returned HTTP 503",
                }
            },
        }
        with (
            patch("pipeline.runtime._read_json", side_effect=[{"pid": 123}, degraded]),
            patch("pipeline.runtime.process_is_alive", return_value=True),
            patch("pipeline.runtime._stop_recorded_processes") as stop_processes,
            patch("pipeline.runtime.subprocess.Popen") as spawn_process,
            patch("builtins.print") as print_message,
        ):
            exit_code = start_manager()

        self.assertEqual(exit_code, 1)
        stop_processes.assert_not_called()
        spawn_process.assert_not_called()
        output = "\n".join(str(call.args[0]) for call in print_message.call_args_list)
        self.assertIn("managed but degraded", output)
        self.assertIn("HTTP 503", output)

    def test_local_start_accepts_data_only_degraded_without_spawning_a_second_manager(self) -> None:
        stale = {
            "state": "degraded",
            "managerPid": 123,
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "data": {"freshness": "stale"},
            "services": {
                "website": {"state": "healthy"},
                "scheduler": {"state": "healthy"},
            },
        }
        with (
            patch("pipeline.runtime._read_json", side_effect=[{"pid": 123}, stale]),
            patch("pipeline.runtime.process_is_alive", return_value=True),
            patch("pipeline.runtime._stop_recorded_processes") as stop_processes,
            patch("pipeline.runtime.subprocess.Popen") as spawn_process,
        ):
            exit_code = start_manager()
        self.assertEqual(exit_code, 0)
        stop_processes.assert_not_called()
        spawn_process.assert_not_called()

    def test_running_manager_requires_stop_start_before_configuration_changes(self) -> None:
        active = {
            "state": "healthy",
            "managerPid": 123,
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "schedule": {"at": "08:00", "timezone": "Asia/Shanghai"},
            "data": {"staleAfterSeconds": 36 * 3600},
        }
        with (
            patch.dict(
                "os.environ",
                {
                    "RARDAR_SCHEDULE_AT": "09:30",
                    "RARDAR_SCHEDULE_TIMEZONE": "Asia/Shanghai",
                    "RARDAR_STALE_AFTER_HOURS": "36",
                },
            ),
            patch("pipeline.runtime._read_json", side_effect=[{"pid": 123}, active]),
            patch("pipeline.runtime.process_is_alive", return_value=True),
            patch("pipeline.runtime.python_dependencies_are_ready") as dependencies,
            patch("pipeline.runtime._stop_recorded_processes") as stop_processes,
            patch("pipeline.runtime.subprocess.Popen") as spawn_process,
            patch("builtins.print") as print_message,
        ):
            exit_code = start_manager()

        self.assertEqual(exit_code, 1)
        dependencies.assert_not_called()
        stop_processes.assert_not_called()
        spawn_process.assert_not_called()
        output = "\n".join(str(call.args[0]) for call in print_message.call_args_list)
        self.assertIn("require npm run local:stop", output)

    def test_local_status_keeps_json_output_and_signals_stale_data(self) -> None:
        stale = {
            "schemaVersion": 1,
            "state": "degraded",
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "managerPid": 123,
            "message": "网站与调度正常，但已发布数据超过新鲜度阈值",
            "data": {
                "freshness": "stale",
                "warning": "Data freshness: STALE",
                "snapshotAgeSeconds": 49 * 3600,
                "staleAfterSeconds": 36 * 3600,
            },
            "services": {},
        }
        with (
            patch("pipeline.runtime._read_json", side_effect=[stale, {"pid": 123}]),
            patch("pipeline.runtime.process_is_alive", return_value=True),
            patch("builtins.print") as output,
        ):
            exit_code = show_status()
        self.assertEqual(exit_code, 1)
        self.assertEqual(output.call_count, 1)
        parsed = json.loads(output.call_args.args[0])
        self.assertEqual(parsed["data"]["freshness"], "stale")
        self.assertEqual(parsed["data"]["warning"], "Data freshness: STALE")

    def test_manager_status_requires_matching_control_and_status_pids(self) -> None:
        status = {
            "managerPid": 123,
            "checkedAt": datetime.now(timezone.utc).isoformat(),
        }
        with patch("pipeline.runtime.process_is_alive", return_value=True):
            self.assertTrue(_manager_status_matches_control(status, {"pid": 123}))
            self.assertFalse(_manager_status_matches_control(status, {"pid": 456}))

    def test_manager_does_not_restart_a_live_website_for_http_failure(self) -> None:
        class StopLoop(RuntimeError):
            pass

        class FakeProcess:
            pid = 42

            @staticmethod
            def poll() -> None:
                return None

        services = []

        class FakeService:
            def __init__(self, name, command, log_path):
                self.name = name
                self.command = command
                self.log_path = log_path
                self.process = FakeProcess()
                self.started_at = None
                self.restart_count = 0
                self.last_error = None
                self._log_handle = None
                self.start_count = 0
                self.stop_count = 0
                self.environment = None
                services.append(self)

            def start(self, environment) -> None:
                self.start_count += 1
                self.started_at = datetime.now(timezone.utc).isoformat()
                self.environment = dict(environment)

            def poll(self) -> None:
                return None

            def stop(self) -> None:
                self.stop_count += 1

        class FakeStatusServer:
            def shutdown(self) -> None:
                return

            def server_close(self) -> None:
                return

        def capture_status(payload, *_arguments) -> None:
            website = (payload.get("services") or {}).get("website") or {}
            if website.get("state") == "degraded":
                raise StopLoop()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            layout = isolated_layout(root, vinext_port=43121, status_port=43122)
            persistent = {
                "RARDAR_SCHEDULE_AT": "06:45",
                "RARDAR_SCHEDULE_TIMEZONE": "Europe/Berlin",
                "RARDAR_STALE_AFTER_HOURS": "48",
                "RARDAR_VINEXT_STATE_DIR": str(root / "vinext-state"),
                "RARDAR_VITE_CACHE_DIR": str(root / "vite-cache"),
                "WRANGLER_LOG_PATH": str(root / "wrangler-logs"),
                "WRANGLER_REGISTRY_PATH": str(root / "wrangler-registry"),
                "MINIFLARE_REGISTRY_PATH": str(root / "miniflare-registry"),
                "CLOUDFLARE_VITE_FORCE_LOCAL": "true",
                "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS": "rardar.cosflow.icu",
                "HOME": str(root / "user-home"),
                "TEMP": str(root / "temp"),
                "XDG_CACHE_HOME": str(root / "xdg-cache"),
                "GITHUB_TOKEN": "scheduler-secret",
                "OPENAI_API_KEY": "analysis-secret",
                "UNREVIEWED_SERVICE_TOKEN": "unknown-token",
                "INTERNAL_CLIENT_SECRET": "unknown-secret",
                "DATABASE_URL": "sqlite://must-not-reach-website",
            }
            with (
                patch.dict("os.environ", persistent),
                patch("pipeline.runtime.ManagedService", FakeService),
                patch("pipeline.runtime.find_node", return_value=Path("node")),
                patch("pipeline.runtime._read_json", return_value={}),
                patch("pipeline.runtime._write_json"),
                patch("pipeline.runtime.write_runtime_status", side_effect=capture_status),
                patch("pipeline.runtime.start_status_server", return_value=FakeStatusServer()) as status_server,
                patch("pipeline.runtime.signal.signal"),
                patch("pipeline.runtime.port_is_open", return_value=True),
                patch(
                    "pipeline.runtime.probe_website_health",
                    return_value=WebsiteHealth("degraded", error="HTTP 503"),
                ),
                patch(
                    "pipeline.runtime._scheduler_details",
                    return_value={"heartbeatAt": datetime.now(timezone.utc).isoformat()},
                ),
                patch("pipeline.runtime.scheduler_heartbeat_state", return_value="healthy"),
            ):
                with self.assertRaises(StopLoop):
                    _run_manager(layout=layout)
            status_server.assert_called_once_with("127.0.0.1", 43122, 43121)

        website, scheduler = services
        self.assertEqual(website.start_count, 1)
        self.assertEqual(scheduler.start_count, 1)
        self.assertEqual(website.restart_count, 0)
        self.assertEqual(website.stop_count, 1)
        self.assertEqual(website.environment["RARDAR_PYTHON"], sys.executable)
        self.assertEqual(scheduler.environment["RARDAR_PYTHON"], sys.executable)
        self.assertIn("06:45", scheduler.command)
        self.assertIn("Europe/Berlin", scheduler.command)
        self.assertEqual(scheduler.environment["RARDAR_SCHEDULE_AT"], "06:45")
        self.assertEqual(scheduler.environment["RARDAR_STALE_AFTER_HOURS"], "48")
        self.assertEqual(scheduler.environment["RARDAR_TRENDING_PRODUCER_ENABLED"], "false")
        self.assertEqual(website.environment["RARDAR_DATA_DIR"], str(layout.data_dir))
        self.assertEqual(scheduler.environment["RARDAR_DATA_DIR"], str(layout.data_dir))
        self.assertEqual(website.environment["RARDAR_VINEXT_PORT"], "43121")
        self.assertEqual(website.environment["RARDAR_RUNTIME_STATUS_PORT"], "43122")
        self.assertEqual(
            website.environment["__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS"],
            "rardar.cosflow.icu",
        )
        self.assertEqual(
            website.environment["RARDAR_VINEXT_STATE_DIR"],
            persistent["RARDAR_VINEXT_STATE_DIR"],
        )
        self.assertNotIn("GITHUB_TOKEN", website.environment)
        self.assertNotIn("OPENAI_API_KEY", website.environment)
        self.assertNotIn("UNREVIEWED_SERVICE_TOKEN", website.environment)
        self.assertNotIn("INTERNAL_CLIENT_SECRET", website.environment)
        self.assertNotIn("DATABASE_URL", website.environment)
        self.assertNotIn("RARDAR_TRENDING_PRODUCER_ENABLED", website.environment)
        self.assertEqual(website.environment["CLOUDFLARE_VITE_FORCE_LOCAL"], "true")
        self.assertEqual(website.environment["HOME"], persistent["HOME"])
        self.assertEqual(website.environment["TEMP"], persistent["TEMP"])
        self.assertEqual(website.environment["XDG_CACHE_HOME"], persistent["XDG_CACHE_HOME"])
        self.assertEqual(scheduler.environment["GITHUB_TOKEN"], "scheduler-secret")
        self.assertEqual(scheduler.environment["OPENAI_API_KEY"], "analysis-secret")
        self.assertEqual(scheduler.environment["UNREVIEWED_SERVICE_TOKEN"], "unknown-token")
        self.assertIn(str(layout.data_dir), scheduler.command)
        self.assertIn(str(layout.scheduler_status_path), scheduler.command)

    def test_manager_blocks_scheduler_lock_conflict_without_trusting_owner_telemetry(self) -> None:
        class StopLoop(RuntimeError):
            pass

        services = []

        class FakeProcess:
            def __init__(self, pid: int, exit_code: int | None) -> None:
                self.pid = pid
                self.exit_code = exit_code

            def poll(self) -> int | None:
                return self.exit_code

        class FakeService:
            def __init__(self, name, command, log_path):
                self.name = name
                self.command = command
                self.log_path = log_path
                self.process = FakeProcess(
                    42 if name == "website" else 43,
                    None if name == "website" else SCHEDULER_ALREADY_RUNNING_EXIT_CODE,
                )
                self.started_at = None
                self.restart_count = 0
                self.last_error = None
                self._log_handle = None
                self.start_count = 0
                self.stop_count = 0
                services.append(self)

            def start(self, _environment) -> None:
                self.start_count += 1
                self.started_at = datetime.now(timezone.utc).isoformat()

            def poll(self) -> int | None:
                return self.process.poll()

            def stop(self) -> None:
                self.stop_count += 1

            def cleanup_owned_process_tree(self) -> None:
                return

        class FakeStatusServer:
            def shutdown(self) -> None:
                return

            def server_close(self) -> None:
                return

        blocked_payloads = []

        def capture_status(payload) -> None:
            scheduler = (payload.get("services") or {}).get("scheduler") or {}
            if scheduler.get("state") == "blocked":
                blocked_payloads.append(payload)
                raise StopLoop()

        foreign_status = {
            "processId": 999,
            "state": "healthy",
            "heartbeatAt": datetime.now(timezone.utc).isoformat(),
            "nextRunAt": "2026-08-11T00:00:00+00:00",
            "lastSuccessfulRefreshAt": "2026-08-10T00:02:00+00:00",
        }
        website_health = WebsiteHealth(
            "healthy",
            generation_id="generation-2",
            data_freshness="fresh",
            snapshot_captured_at=datetime.now(timezone.utc).isoformat(),
            snapshot_age_seconds=0,
            stale_after_seconds=129600,
            schedule_at="08:00",
            schedule_timezone="Asia/Shanghai",
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("pipeline.runtime.ManagedService", FakeService),
            patch("pipeline.runtime.find_node", return_value=Path("node")),
            patch(
                "pipeline.runtime._read_json",
                side_effect=lambda path: foreign_status
                if path == Path(temporary) / "scheduler-status.json"
                else {},
            ),
            patch("pipeline.runtime._write_json"),
            patch("pipeline.runtime.write_runtime_status", side_effect=capture_status),
            patch("pipeline.runtime.start_status_server", return_value=FakeStatusServer()),
            patch("pipeline.runtime.signal.signal"),
            patch("pipeline.runtime.port_is_open", return_value=True),
            patch("pipeline.runtime.probe_website_health", return_value=website_health),
            patch("pipeline.runtime.CONTROL_PATH", Path(temporary) / "manager.json"),
            patch(
                "pipeline.runtime.SCHEDULER_STATUS_PATH",
                Path(temporary) / "scheduler-status.json",
            ),
        ):
            with self.assertRaises(StopLoop):
                _run_manager()

        self.assertEqual(len(blocked_payloads), 1)
        payload = blocked_payloads[0]
        scheduler_payload = payload["services"]["scheduler"]
        self.assertEqual(scheduler_payload["state"], "blocked")
        self.assertIsNone(scheduler_payload["pid"])
        self.assertFalse(scheduler_payload["telemetryTrusted"])
        self.assertIsNone(payload["schedule"]["nextRunAt"])
        self.assertIsNone(payload["data"]["lastSuccessfulRefreshAt"])
        self.assertEqual(services[1].start_count, 1)

    def test_service_rejects_invalid_configuration_before_lock_or_file_writes(self) -> None:
        with (
            patch.dict(os.environ, {"RARDAR_DATA_DIR": "relative-data"}),
            patch("pipeline.runtime.acquire_manager_lock") as acquire_lock,
            patch("pipeline.runtime._write_json") as write_json,
            patch("pipeline.runtime.subprocess.Popen") as spawn,
        ):
            exit_code = run_manager(service_mode=True)

        self.assertNotEqual(exit_code, 0)
        acquire_lock.assert_not_called()
        write_json.assert_not_called()
        spawn.assert_not_called()

    def test_service_rejects_invalid_vite_hosts_before_lock_or_child_spawn(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS": "true"},
            ),
            patch("pipeline.runtime.acquire_manager_lock") as acquire_lock,
            patch("pipeline.runtime._write_json") as write_json,
            patch("pipeline.runtime.subprocess.Popen") as spawn,
        ):
            exit_code = run_manager(service_mode=True)

        self.assertNotEqual(exit_code, 0)
        acquire_lock.assert_not_called()
        write_json.assert_not_called()
        spawn.assert_not_called()

    def test_service_conflict_is_nonzero_while_local_run_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            layout = isolated_layout(Path(temporary).resolve())
            preflight = (
                layout,
                RuntimeSettings("08:00", "Asia/Shanghai", 36),
                Path("node"),
            )
            expected_lock = manager_ownership_lock_path(layout)
            with (
                patch("pipeline.runtime._runtime_preflight", return_value=preflight),
                patch("pipeline.runtime.acquire_manager_lock", return_value=None) as acquire_lock,
            ):
                self.assertEqual(
                    run_manager(service_mode=True),
                    MANAGER_ALREADY_RUNNING_EXIT_CODE,
                )
                self.assertEqual(run_manager(), 0)
            acquire_lock.assert_called_with(expected_lock)

    def test_service_requires_home_to_match_the_imported_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            environment = {
                "RARDAR_HOME": str(root / "other-release"),
                "RARDAR_DATA_DIR": str(root / "data"),
                "RARDAR_RUNTIME_DIR": str(root / "runtime"),
                "RARDAR_DATA_LOCK_DIR": str(root / "locks"),
                "RARDAR_VINEXT_PORT": "43131",
                "RARDAR_RUNTIME_STATUS_PORT": "43132",
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("pipeline.runtime.python_dependencies_are_ready", return_value=True),
                patch("pipeline.runtime.acquire_manager_lock") as acquire_lock,
            ):
                self.assertNotEqual(run_manager(service_mode=True), 0)
            acquire_lock.assert_not_called()

    def test_manager_ownership_lock_follows_data_not_home_or_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            common_data = root / "shared-data"
            common_locks = root / "shared-locks"
            first = RuntimeLayout(
                root / "release-a",
                common_data,
                root / "runtime-a",
                common_locks,
                43141,
                43142,
            )
            second = RuntimeLayout(
                root / "release-b",
                common_data,
                root / "runtime-b",
                common_locks,
                43143,
                43144,
            )
            self.assertEqual(
                manager_ownership_lock_path(first),
                manager_ownership_lock_path(second),
            )

    def test_sigterm_handler_stops_both_owned_children(self) -> None:
        handlers = {}
        services = []
        statuses = []

        class FakeProcess:
            def __init__(self, pid) -> None:
                self.pid = pid

            @staticmethod
            def poll() -> None:
                return None

        class FakeService:
            def __init__(self, name, command, log_path) -> None:
                self.name = name
                self.command = command
                self.log_path = log_path
                self.process = FakeProcess(51 if name == "website" else 52)
                self.started_at = None
                self.restart_count = 0
                self.last_error = None
                self._log_handle = None
                self.stop_count = 0
                services.append(self)

            def start(self, _environment) -> None:
                self.started_at = datetime.now(timezone.utc).isoformat()
                if self.name == "scheduler":
                    handlers[signal.SIGTERM](signal.SIGTERM, None)

            def poll(self) -> None:
                return None

            def stop(self) -> None:
                self.stop_count += 1

        class FakeStatusServer:
            def shutdown(self) -> None:
                return

            def server_close(self) -> None:
                return

        def register_handler(signum, handler) -> None:
            handlers[signum] = handler

        with tempfile.TemporaryDirectory() as temporary:
            layout = isolated_layout(Path(temporary).resolve(), vinext_port=43151, status_port=43152)
            with (
                patch("pipeline.runtime.ManagedService", FakeService),
                patch("pipeline.runtime._read_json", return_value={}),
                patch("pipeline.runtime._write_json"),
                patch(
                    "pipeline.runtime.write_runtime_status",
                    side_effect=lambda payload, *_args: statuses.append(payload),
                ),
                patch("pipeline.runtime.start_status_server", return_value=FakeStatusServer()),
                patch("pipeline.runtime.signal.signal", side_effect=register_handler),
            ):
                exit_code = _run_manager(
                    layout,
                    RuntimeSettings("23:59", "UTC", 36),
                    Path("node"),
                )

        self.assertEqual(exit_code, 0)
        self.assertIn(signal.SIGTERM, handlers)
        self.assertEqual([service.stop_count for service in services], [1, 1])
        self.assertEqual(statuses[-1]["state"], "stopped")
        self.assertEqual(statuses[0]["runtime"]["statusUrl"], "http://127.0.0.1:43152/status")

    @unittest.skipIf(os.name == "nt", "POSIX SIGTERM lifecycle is exercised on Ubuntu CI")
    def test_service_process_lifecycle_isolated_from_primary_runtime(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the managed runtime lifecycle")

        with tempfile.TemporaryDirectory(prefix="rardar-service-lifecycle-") as temporary:
            root = Path(temporary).resolve()
            data_dir = root / "data"
            runtime_dir = root / "runtime"
            lock_dir = root / "locks"
            source = resolve_current_generation(ROOT / "data")
            self.assertIsNotNone(source.generation_id)
            target_generation = data_dir / "generations" / str(source.generation_id)
            target_generation.parent.mkdir(parents=True)
            shutil.copytree(source.root, target_generation)
            shutil.copy2(ROOT / "data" / "current.json", data_dir / "current.json")
            pointer_before = (data_dir / "current.json").read_bytes()

            vinext_port = free_loopback_port()
            while vinext_port in {3000, 3002}:
                vinext_port = free_loopback_port()
            status_port = free_loopback_port()
            while status_port in {3000, 3002, vinext_port}:
                status_port = free_loopback_port()

            persistent_paths = {
                "RARDAR_VINEXT_STATE_DIR": root / "vinext-state",
                "RARDAR_VITE_CACHE_DIR": root / "vite-cache",
                "WRANGLER_LOG_PATH": root / "wrangler-logs",
                "WRANGLER_REGISTRY_PATH": root / "wrangler-registry",
                "MINIFLARE_REGISTRY_PATH": root / "miniflare-registry",
            }
            for path in persistent_paths.values():
                path.mkdir(parents=True)
            scheduled = (datetime.now(timezone.utc) + timedelta(hours=6)).strftime("%H:%M")
            environment = os.environ.copy()
            for name in tuple(environment):
                if name.startswith("RARDAR_") or name in {
                    "WRANGLER_LOG_PATH",
                    "WRANGLER_REGISTRY_PATH",
                    "MINIFLARE_REGISTRY_PATH",
                }:
                    environment.pop(name, None)
            environment.update(
                {
                    "RARDAR_HOME": str(ROOT),
                    "RARDAR_DATA_DIR": str(data_dir),
                    "RARDAR_RUNTIME_DIR": str(runtime_dir),
                    "RARDAR_DATA_LOCK_DIR": str(lock_dir),
                    "RARDAR_VINEXT_PORT": str(vinext_port),
                    "RARDAR_RUNTIME_STATUS_PORT": str(status_port),
                    "RARDAR_SCHEDULE_AT": scheduled,
                    "RARDAR_SCHEDULE_TIMEZONE": "UTC",
                    "RARDAR_STALE_AFTER_HOURS": "8760",
                    "RARDAR_NODE": str(Path(node).resolve()),
                    "WRANGLER_WRITE_LOGS": "false",
                    **{name: str(path) for name, path in persistent_paths.items()},
                }
            )
            seen_children: set[int] = set()
            processes: list[subprocess.Popen[bytes]] = []
            log_path = root / "service.log"

            def start_service() -> subprocess.Popen[bytes]:
                log = log_path.open("ab")
                process = subprocess.Popen(
                    [sys.executable, "-m", "pipeline.runtime", "service"],
                    cwd=ROOT,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                log.close()
                processes.append(process)
                return process

            def active_status(process: subprocess.Popen[bytes]) -> dict[str, object] | bool:
                if process.poll() is not None:
                    raise AssertionError(
                        f"service exited with {process.returncode}: "
                        f"{log_path.read_text(encoding='utf-8', errors='replace')}"
                    )
                try:
                    status = json.loads(
                        (runtime_dir / "status.json").read_text(encoding="utf-8")
                    )
                except (FileNotFoundError, json.JSONDecodeError):
                    return False
                services = status.get("services") or {}
                website_pid = (services.get("website") or {}).get("pid")
                scheduler_pid = (services.get("scheduler") or {}).get("pid")
                if (
                    status.get("managerPid") != process.pid
                    or not isinstance(website_pid, int)
                    or not isinstance(scheduler_pid, int)
                    or website_pid == scheduler_pid
                    or loopback_http_status(status_port, "/status") != 200
                    or loopback_http_status(vinext_port, "/api/health") != 200
                ):
                    return False
                seen_children.update((website_pid, scheduler_pid))
                return status

            def terminate_service(process: subprocess.Popen[bytes]) -> None:
                process.terminate()
                process.wait(timeout=30)
                self.assertEqual(process.returncode, 0)
                wait_until(lambda: port_is_closed(vinext_port), timeout=30)
                wait_until(lambda: port_is_closed(status_port), timeout=30)

            try:
                first = start_service()
                first_status = wait_until(lambda: active_status(first))
                self.assertIsInstance(first_status, dict)
                first_services = first_status["services"]
                first_pids = (
                    first_services["website"]["pid"],
                    first_services["scheduler"]["pid"],
                )
                duplicate = subprocess.run(
                    [sys.executable, "-m", "pipeline.runtime", "service"],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(duplicate.returncode, MANAGER_ALREADY_RUNNING_EXIT_CODE)
                unchanged = json.loads(
                    (runtime_dir / "status.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    (
                        unchanged["services"]["website"]["pid"],
                        unchanged["services"]["scheduler"]["pid"],
                    ),
                    first_pids,
                )

                terminate_service(first)
                self.assertTrue(all(not pid_is_alive(pid) for pid in first_pids))
                self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)

                second = start_service()
                second_status = wait_until(lambda: active_status(second))
                second_services = second_status["services"]
                second_pids = (
                    second_services["website"]["pid"],
                    second_services["scheduler"]["pid"],
                )
                self.assertNotEqual(second.pid, first.pid)
                self.assertTrue(set(first_pids).isdisjoint(second_pids))
                self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)
                terminate_service(second)
                self.assertTrue(all(not pid_is_alive(pid) for pid in second_pids))
            finally:
                for process in processes:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=10)
                for pid in seen_children:
                    if pid_is_alive(pid):
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_python_dependency_preflight_succeeds_when_modules_are_available(self) -> None:
        with patch("pipeline.runtime.missing_python_dependencies", return_value=()):
            self.assertTrue(python_dependencies_are_ready())


if __name__ == "__main__":
    unittest.main()
