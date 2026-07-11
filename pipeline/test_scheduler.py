from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.scheduler import committed_refresh_at, next_run_at, parse_clock, run_cycle, should_catch_up, should_retry


class SchedulerTests(unittest.TestCase):
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
            self.assertEqual(stored["candidateCount"], 3)
            self.assertEqual(stored["dataAuditStatus"], "healthy")
            self.assertEqual(stored["dataAuditSummary"]["observedNetStarChange"], 42)
            self.assertEqual(stored["dataAuditSummary"]["successfulQueryCount"], 6)
            self.assertEqual(stored["dataAuditSummary"]["failedQueryCount"], 1)
            self.assertEqual(stored["dataAuditSummary"]["failedSourceCount"], 1)
            self.assertEqual(stored["dataAuditSummary"]["analysisFailureCount"], 2)
            self.assertIsNotNone(stored["lastRunCompletedAt"])

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
        with self.assertRaises(ValueError):
            parse_clock("25:00")

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

    def test_does_not_catch_up_outside_window(self) -> None:
        now = datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc)  # 21:00 Asia/Shanghai
        self.assertFalse(should_catch_up(now, None, "scheduled", 8, 0, "Asia/Shanghai"))

    def test_failed_cycle_retries_twice_then_waits_for_next_day(self) -> None:
        self.assertTrue(should_retry("failed", 1))
        self.assertTrue(should_retry("failed", 2))
        self.assertFalse(should_retry("failed", 3))
        self.assertFalse(should_retry("healthy", 1))


if __name__ == "__main__":
    unittest.main()
