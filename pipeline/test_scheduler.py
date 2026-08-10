from __future__ import annotations

import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.analyze_repository import RemoteCloneLifecycleError
from pipeline.generations import CandidateGenerationError
from pipeline.runtime_settings import SCHEDULER_ALREADY_RUNNING_EXIT_CODE, RuntimeSettings
from pipeline.scheduler import (
    _run_scheduler,
    SchedulerAlreadyRunningError,
    committed_refresh_at,
    main as scheduler_main,
    next_run_at,
    parse_clock,
    run_cycle,
    scheduler_instance_lock,
    should_catch_up,
    should_retry,
)


def _hold_scheduler_lock(
    data_dir: str,
    lock_root: str,
    acquired_path: str,
    release_path: str,
) -> None:
    with scheduler_instance_lock(Path(data_dir), lock_root=Path(lock_root)):
        Path(acquired_path).write_text("acquired", encoding="utf-8")
        deadline = time.monotonic() + 10
        while not Path(release_path).exists() and time.monotonic() < deadline:
            time.sleep(0.05)


class SchedulerTests(unittest.TestCase):
    def test_cli_uses_external_data_directory_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = (Path(directory) / "external-data").resolve()
            status_path = Path(directory) / "scheduler-status.json"
            settings = RuntimeSettings("08:00", "Asia/Shanghai", 36)
            with (
                patch.dict(os.environ, {"RARDAR_DATA_DIR": str(data_dir)}, clear=False),
                patch.object(
                    sys,
                    "argv",
                    [
                        "pipeline.scheduler",
                        "--status-path",
                        str(status_path),
                        "--once",
                    ],
                ),
                patch("pipeline.scheduler.load_runtime_settings", return_value=settings),
                patch("pipeline.scheduler.scheduler_instance_lock") as lock,
                patch("pipeline.scheduler._run_scheduler") as run_scheduler,
            ):
                lock.return_value.__enter__.return_value = None
                lock.return_value.__exit__.return_value = False
                scheduler_main()

            lock.assert_called_once_with(data_dir)
            run_scheduler.assert_called_once()
            self.assertEqual(run_scheduler.call_args.args[0].data_dir, data_dir)

    def test_only_one_scheduler_can_own_a_canonical_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            lock_root = root / "locks"
            acquired = root / "acquired"
            release = root / "release"
            owner = multiprocessing.Process(
                target=_hold_scheduler_lock,
                args=(str(data_dir), str(lock_root), str(acquired), str(release)),
            )
            owner.start()
            try:
                deadline = time.monotonic() + 5
                while not acquired.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(acquired.exists(), "scheduler owner did not acquire its lock")
                with self.assertRaises(SchedulerAlreadyRunningError):
                    with scheduler_instance_lock(
                        data_dir / ".." / "data",
                        lock_root=lock_root,
                    ):
                        self.fail("a second scheduler acquired the canonical data directory")
            finally:
                release.write_text("release", encoding="utf-8")
                owner.join(timeout=5)
                if owner.is_alive():
                    owner.terminate()
                    owner.join(timeout=2)
            self.assertEqual(owner.exitcode, 0)

    def test_scheduler_lock_conflict_exits_before_status_or_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_path = root / "scheduler-status.json"
            arguments = [
                "pipeline.scheduler",
                "--data-dir",
                str(root / "data"),
                "--status-path",
                str(status_path),
                "--once",
            ]
            with (
                patch.object(sys, "argv", arguments),
                patch(
                    "pipeline.scheduler.scheduler_instance_lock",
                    side_effect=SchedulerAlreadyRunningError("already owned"),
                ),
                patch("pipeline.scheduler.refresh") as refresh_call,
            ):
                with self.assertRaises(SystemExit) as stopped:
                    scheduler_main()
            self.assertEqual(stopped.exception.code, SCHEDULER_ALREADY_RUNNING_EXIT_CODE)
            self.assertFalse(status_path.exists())
            refresh_call.assert_not_called()

    def test_clone_lifecycle_failure_is_non_retryable(self) -> None:
        lifecycle_error = RemoteCloneLifecycleError(
            "remote_clone_process_tree_cleanup_failed", "simulated"
        )

        def fail_refresh(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise CandidateGenerationError(
                "candidate_build_failed",
                "refresh candidate build failed",
                generation_id="candidate-1",
                stage="build",
            ) from lifecycle_error

        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "scheduler.json"
            with patch("pipeline.scheduler.refresh", side_effect=fail_refresh):
                result = run_cycle(Path("unused"), 0, status_path)
            stored = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["retryable"])
        self.assertEqual(
            result["remoteAnalysisErrorCode"],
            "remote_clone_process_tree_cleanup_failed",
        )
        self.assertFalse(stored["retryable"])
        self.assertEqual(
            stored["remoteAnalysisErrorCode"],
            "remote_clone_process_tree_cleanup_failed",
        )
        self.assertFalse(
            should_retry(
                result["state"],
                1,
                retryable=result["retryable"],
            )
        )

    def test_committed_refresh_allows_later_derived_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = "2026-07-10T00:00:02+00:00"
            artifacts = {
                root / "snapshots" / "latest.json": {"captured_at": captured},
                root / "catalog" / "latest.json": {"capturedAt": captured},
                root / "signals" / "latest.json": {"capturedAt": captured},
                root / "queues" / "codex.json": {"generatedAt": captured},
            }
            for path, payload in artifacts.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(committed_refresh_at(root), captured)
            (root / "queues" / "codex.json").write_text(
                json.dumps({"generatedAt": "2026-07-10T00:30:00+00:00"}),
                encoding="utf-8",
            )
            (root / "signals" / "latest.json").write_text(
                json.dumps({"capturedAt": "2026-07-10T00:20:00+00:00"}),
                encoding="utf-8",
            )
            self.assertEqual(committed_refresh_at(root), captured)

            (root / "queues" / "codex.json").write_text(
                json.dumps({"generatedAt": "2026-07-10T00:10:00+00:00"}),
                encoding="utf-8",
            )
            self.assertIsNone(committed_refresh_at(root))

            (root / "queues" / "codex.json").write_text(
                json.dumps({"generatedAt": captured}),
                encoding="utf-8",
            )
            (root / "signals" / "latest.json").write_text(
                json.dumps({"capturedAt": captured}),
                encoding="utf-8",
            )
            (root / "catalog" / "latest.json").write_text(
                json.dumps({"capturedAt": "2026-07-09T00:00:00+00:00"}),
                encoding="utf-8",
            )
            self.assertIsNone(committed_refresh_at(root))

    def test_present_pointer_is_strict_and_never_falls_back_to_flat_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = "2026-07-10T00:00:02+00:00"
            artifacts = {
                root / "snapshots/latest.json": {"captured_at": captured},
                root / "catalog/latest.json": {"capturedAt": captured},
                root / "signals/latest.json": {"capturedAt": captured},
                root / "queues/codex.json": {"generatedAt": captured},
            }
            for path, payload in artifacts.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload), encoding="utf-8")
            (root / "current.json").write_text("{}", encoding="utf-8")

            self.assertIsNone(committed_refresh_at(root))

    def test_cycle_publishes_running_state_before_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "scheduler.json"

            def inspect_running_state(*_args: object, **_kwargs: object) -> dict[str, object]:
                running = json.loads(status_path.read_text(encoding="utf-8"))
                self.assertEqual(running["state"], "running")
                self.assertIsNone(running["lastRunCompletedAt"])
                self.assertIsNotNone(running["heartbeatAt"])
                return {"sourceCount": 3, "projectCount": 2, "signalCount": 1}

            with (
                patch("pipeline.scheduler.refresh", side_effect=inspect_running_state),
                patch(
                    "pipeline.scheduler.audit_data",
                    return_value={
                        "status": "healthy",
                        "warningCount": 0,
                        "issues": [],
                        "observedProjectCount": 2,
                        "observedNetStarChange": 42,
                        "dailyTrackCounts": {"recentMomentum": 3, "longTerm": 2},
                        "historyCount": 1,
                        "successfulQueryCount": 6,
                        "failedQueryCount": 1,
                        "healthySourceCount": 5,
                        "failedSourceCount": 1,
                        "analysisFailureCount": 2,
                        "staticAnalysisRequiredCount": 2,
                    },
                ),
            ):
                result = run_cycle(Path(directory), 0, status_path)

            stored = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(result["state"], "healthy")
            self.assertEqual(stored["state"], "healthy")
            self.assertEqual(stored["lastSuccessfulRefreshAt"], stored["lastRunCompletedAt"])
            self.assertEqual(stored["candidateCount"], 3)
            self.assertEqual(stored["dataAuditStatus"], "healthy")
            self.assertEqual(stored["dataAuditSummary"]["observedNetStarChange"], 42)
            self.assertEqual(stored["dataAuditSummary"]["successfulQueryCount"], 6)
            self.assertEqual(stored["dataAuditSummary"]["failedQueryCount"], 1)
            self.assertEqual(stored["dataAuditSummary"]["failedSourceCount"], 1)
            self.assertEqual(stored["dataAuditSummary"]["analysisFailureCount"], 2)
            self.assertIsNotNone(stored["lastRunCompletedAt"])

    def test_failed_cycle_preserves_the_last_successful_refresh_timestamp(self) -> None:
        previous_success = "2026-07-09T00:02:00+00:00"
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "scheduler.json"
            status_path.write_text(
                json.dumps(
                    {
                        "state": "healthy",
                        "lastRunCompletedAt": previous_success,
                        "lastSuccessfulRefreshAt": previous_success,
                    }
                ),
                encoding="utf-8",
            )
            with patch("pipeline.scheduler.refresh", side_effect=RuntimeError("offline")):
                result = run_cycle(Path(directory), 0, status_path)

            stored = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["lastSuccessfulRefreshAt"], previous_success)
        self.assertEqual(stored["lastSuccessfulRefreshAt"], previous_success)

    def test_daemon_restart_preserves_the_last_successful_refresh_timestamp(self) -> None:
        previous_success = "2026-07-09T00:02:00+00:00"

        class StopLoop(RuntimeError):
            pass

        for has_new_field in (True, False):
            with self.subTest(has_new_field=has_new_field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                status_path = root / "scheduler.json"
                stored_status = {
                    "state": "healthy",
                    "lastRunCompletedAt": previous_success,
                    "retryable": True,
                }
                if has_new_field:
                    stored_status["lastSuccessfulRefreshAt"] = previous_success
                status_path.write_text(json.dumps(stored_status), encoding="utf-8")
                arguments = SimpleNamespace(
                    once=False,
                    skip_initial=True,
                    analyze_top=0,
                    data_dir=root,
                )
                captured = []

                def capture_status(_path: Path, payload: dict[str, object]) -> None:
                    captured.append(payload)
                    raise StopLoop()

                with (
                    patch("pipeline.scheduler.committed_refresh_at", return_value=None),
                    patch("pipeline.scheduler.should_catch_up", return_value=False),
                    patch(
                        "pipeline.scheduler.next_run_at",
                        return_value=datetime.now(timezone.utc) + timedelta(days=1),
                    ),
                    patch("pipeline.scheduler._write_status", side_effect=capture_status),
                ):
                    with self.assertRaises(StopLoop):
                        _run_scheduler(
                            arguments,
                            RuntimeSettings("08:00", "Asia/Shanghai", 36),
                            status_path,
                        )

                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0]["lastSuccessfulRefreshAt"], previous_success)

    def test_cycle_fails_when_committed_data_fails_audit(self) -> None:
        catalog = {"sourceCount": 3, "projectCount": 2, "signalCount": 1}
        with (
            patch("pipeline.scheduler.refresh", return_value=catalog),
            patch(
                "pipeline.scheduler.audit_data",
                return_value={
                    "status": "failed",
                    "warningCount": 0,
                    "issues": [{"code": "snapshot_count_mismatch"}],
                },
            ),
        ):
            result = run_cycle(Path("unused"), 0)

        self.assertEqual(result["state"], "failed")
        self.assertIn("snapshot_count_mismatch", str(result["lastError"]))

    def test_next_run_uses_shanghai_clock(self) -> None:
        now = datetime(2026, 7, 10, 1, tzinfo=timezone.utc)  # 09:00 Asia/Shanghai
        target = next_run_at(now, 8, 0, "Asia/Shanghai")
        self.assertEqual(target, datetime(2026, 7, 11, 0, tzinfo=timezone.utc))

    def test_future_time_can_run_same_day(self) -> None:
        now = datetime(2026, 7, 10, 1, tzinfo=timezone.utc)  # 09:00 Asia/Shanghai
        target = next_run_at(now, 10, 30, "Asia/Shanghai")
        self.assertEqual(target, datetime(2026, 7, 10, 2, 30, tzinfo=timezone.utc))

    def test_rejects_invalid_clock(self) -> None:
        for value in ("25:00", "8:00", "08:0", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_clock(value)

    def test_catches_up_incomplete_run_after_schedule(self) -> None:
        now = datetime(2026, 7, 10, 0, 5, tzinfo=timezone.utc)  # 08:05 Asia/Shanghai
        self.assertTrue(should_catch_up(now, None, "running", 8, 0, "Asia/Shanghai"))

    def test_does_not_repeat_completed_run(self) -> None:
        now = datetime(2026, 7, 10, 0, 5, tzinfo=timezone.utc)
        self.assertFalse(
            should_catch_up(
                now,
                "2026-07-10T00:03:00+00:00",
                "healthy",
                8,
                0,
                "Asia/Shanghai",
            )
        )

    def test_committed_snapshot_prevents_duplicate_catch_up_after_status_crash(self) -> None:
        now = datetime(2026, 7, 10, 0, 5, tzinfo=timezone.utc)
        self.assertFalse(
            should_catch_up(
                now,
                None,
                "running",
                8,
                0,
                "Asia/Shanghai",
                latest_snapshot_at="2026-07-10T00:00:02+00:00",
            )
        )

    def test_retries_failed_run_within_catch_up_window(self) -> None:
        now = datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc)
        self.assertTrue(
            should_catch_up(
                now,
                "2026-07-10T00:02:00+00:00",
                "failed",
                8,
                0,
                "Asia/Shanghai",
                latest_snapshot_at="2026-07-10T00:00:02+00:00",
            )
        )

    def test_nonretryable_failure_does_not_catch_up_in_same_window(self) -> None:
        now = datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc)
        self.assertFalse(
            should_catch_up(
                now,
                "2026-07-10T00:02:00+00:00",
                "failed",
                8,
                0,
                "Asia/Shanghai",
                retryable=False,
            )
        )

    def test_nonretryable_failure_from_previous_day_catches_up_for_new_cycle(self) -> None:
        now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
        self.assertTrue(
            should_catch_up(
                now,
                "2026-07-10T00:02:00+00:00",
                "failed",
                8,
                0,
                "Asia/Shanghai",
                retryable=False,
            )
        )

    def test_nonretryable_failure_without_trustworthy_completion_does_not_catch_up(self) -> None:
        now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
        for completed in (None, "not-a-timestamp"):
            with self.subTest(completed=completed):
                self.assertFalse(
                    should_catch_up(
                        now,
                        completed,
                        "failed",
                        8,
                        0,
                        "Asia/Shanghai",
                        retryable=False,
                    )
                )

    def test_does_not_catch_up_outside_window(self) -> None:
        now = datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc)  # 21:00 Asia/Shanghai
        self.assertFalse(should_catch_up(now, None, "scheduled", 8, 0, "Asia/Shanghai"))

    def test_failed_cycle_retries_twice_then_waits_for_next_day(self) -> None:
        self.assertTrue(should_retry("failed", 1))
        self.assertTrue(should_retry("failed", 2))
        self.assertFalse(should_retry("failed", 3))
        self.assertFalse(should_retry("healthy", 1))
        self.assertFalse(should_retry("failed", 1, retryable=False))


if __name__ == "__main__":
    unittest.main()
