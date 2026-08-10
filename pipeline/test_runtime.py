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
    _manager_status_matches_control,
    _run_manager,
    _scheduler_details,
    _stopped_status,
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
    show_status,
    start_manager,
    stop_manager,
)
from pipeline.runtime_settings import (
    SCHEDULER_ALREADY_RUNNING_EXIT_CODE,
    RuntimeTimezoneDatabaseError,
)


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
        self.assertIsNone(details["heartbeatAt"])
        self.assertIsNone(details["nextRunAt"])
        self.assertIsNone(details["lastSuccessfulRefreshAt"])

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
            control_path = root / "manager.json"
            status_path = root / "status.json"
            with (
                patch("pipeline.runtime.CONTROL_PATH", control_path),
                patch("pipeline.runtime.STATUS_PATH", status_path),
                patch("pipeline.runtime._read_json", return_value={}),
                patch(
                    "pipeline.runtime.load_runtime_settings",
                    side_effect=AssertionError("stop must not load timezone data"),
                ),
            ):
                self.assertEqual(stop_manager(), 0)
            stopped = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(stopped["state"], "stopped")
            self.assertEqual(stopped["schedule"]["timezone"], "Asia/Shanghai")
            with (
                patch("pipeline.runtime.load_runtime_settings", side_effect=AssertionError),
                patch("builtins.print") as print_status,
            ):
                self.assertEqual(show_status(), 1)
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

        def capture_status(payload) -> None:
            website = (payload.get("services") or {}).get("website") or {}
            if website.get("state") == "degraded":
                raise StopLoop()

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                "os.environ",
                {
                    "RARDAR_SCHEDULE_AT": "06:45",
                    "RARDAR_SCHEDULE_TIMEZONE": "Europe/Berlin",
                    "RARDAR_STALE_AFTER_HOURS": "48",
                },
            ),
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
        self.assertIn("06:45", scheduler.command)
        self.assertIn("Europe/Berlin", scheduler.command)
        self.assertEqual(scheduler.environment["RARDAR_SCHEDULE_AT"], "06:45")
        self.assertEqual(scheduler.environment["RARDAR_STALE_AFTER_HOURS"], "48")

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

    def test_python_dependency_preflight_succeeds_when_modules_are_available(self) -> None:
        with patch("pipeline.runtime.missing_python_dependencies", return_value=()):
            self.assertTrue(python_dependencies_are_ready())


if __name__ == "__main__":
    unittest.main()
