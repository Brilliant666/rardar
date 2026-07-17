from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.runtime import (
    WebsiteHealth,
    _run_manager,
    _scheduler_details,
    acquire_manager_lock,
    default_runtime_dir,
    heartbeat_is_fresh,
    missing_python_dependencies,
    parse_website_health,
    parse_node_version,
    python_dependencies_are_ready,
    release_manager_lock,
    rotate_log,
    scheduler_heartbeat_state,
    start_manager,
)


class RuntimeTests(unittest.TestCase):
    def test_website_health_requires_a_valid_generation_response(self) -> None:
        healthy = parse_website_health(
            200,
            json.dumps({"status": "healthy", "generationId": "generation-2"}).encode(),
        )
        self.assertEqual(healthy, WebsiteHealth("healthy", "generation-2"))

        cases = (
            (200, {"status": "healthy"}),
            (200, {"status": "healthy", "generationId": "../escape"}),
            (200, {"status": "degraded", "generationId": "generation-2"}),
        )
        for status, payload in cases:
            with self.subTest(payload=payload):
                result = parse_website_health(status, json.dumps(payload).encode())
                self.assertEqual(result.state, "degraded")
                self.assertIsNone(result.generation_id)
                self.assertIn("invalid contract", str(result.error))

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
            "dataAuditStatus": "degraded",
            "dataAuditWarningCount": 2,
            "dataAuditSummary": {"observedProjectCount": 30},
        }
        with patch("pipeline.runtime._read_json", return_value=status):
            details = _scheduler_details()

        self.assertEqual(details["dataAuditStatus"], "degraded")
        self.assertEqual(details["dataAuditWarningCount"], 2)
        self.assertEqual(details["dataAuditSummary"], {"observedProjectCount": 30})

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

    def test_local_start_keeps_an_existing_degraded_manager_without_restarting(self) -> None:
        degraded = {
            "state": "degraded",
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

        def capture_status(payload) -> None:
            website = (payload.get("services") or {}).get("website") or {}
            if website.get("state") == "degraded":
                raise StopLoop()

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("pipeline.runtime.ManagedService", FakeService),
            patch("pipeline.runtime.find_node", return_value=Path("node")),
            patch("pipeline.runtime._read_json", return_value={}),
            patch("pipeline.runtime._write_json"),
            patch("pipeline.runtime.write_runtime_status", side_effect=capture_status),
            patch("pipeline.runtime.start_status_server", return_value=FakeStatusServer()),
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
            patch("pipeline.runtime.CONTROL_PATH", Path(temporary) / "manager.json"),
        ):
            with self.assertRaises(StopLoop):
                _run_manager()

        website, scheduler = services
        self.assertEqual(website.start_count, 1)
        self.assertEqual(scheduler.start_count, 1)
        self.assertEqual(website.restart_count, 0)
        self.assertEqual(website.stop_count, 1)
        self.assertEqual(website.environment["RARDAR_PYTHON"], sys.executable)
        self.assertEqual(scheduler.environment["RARDAR_PYTHON"], sys.executable)

    def test_python_dependency_preflight_succeeds_when_modules_are_available(self) -> None:
        with patch("pipeline.runtime.missing_python_dependencies", return_value=()):
            self.assertTrue(python_dependencies_are_ready())


if __name__ == "__main__":
    unittest.main()
