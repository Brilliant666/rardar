from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.runtime import (
    _scheduler_details,
    acquire_manager_lock,
    default_runtime_dir,
    heartbeat_is_fresh,
    missing_python_dependencies,
    parse_node_version,
    python_dependencies_are_ready,
    release_manager_lock,
    rotate_log,
    scheduler_heartbeat_state,
    start_manager,
)


class RuntimeTests(unittest.TestCase):
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

    def test_python_dependency_preflight_succeeds_when_modules_are_available(self) -> None:
        with patch("pipeline.runtime.missing_python_dependencies", return_value=()):
            self.assertTrue(python_dependencies_are_ready())


if __name__ == "__main__":
    unittest.main()
