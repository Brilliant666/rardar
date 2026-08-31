from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.retention import (
    RetentionError,
    _plan_digest,
    apply_retention_plan,
    audit_retention,
    create_retention_plan,
    recover_pending_retention_transactions,
    require_discover_storage_capacity,
    storage_snapshot,
)
from pipeline.runtime_settings import RuntimeSettings
from pipeline.test_trending_observations import _bundle
from pipeline.trending_observations import write_capture_create_only


REPOSITORY_DATA = Path(__file__).resolve().parents[1] / "data"
NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


class _Usage(tuple):
    __slots__ = ()

    @property
    def total(self):
        return self[0]

    @property
    def used(self):
        return self[1]

    @property
    def free(self):
        return self[2]


class RetentionProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.data = root / "data"
        self.runtime = root / "runtime"
        shutil.copytree(REPOSITORY_DATA, self.data)
        self.settings = RuntimeSettings("08:00", "Asia/Shanghai", 36)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _candidate(self, index: int, *, state: str = "failed") -> Path:
        identifier = f"candidate-{index:02d}"
        path = self.data / "generations" / ".candidates" / identifier
        path.mkdir(parents=True)
        created = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
        manifest = {
            "schemaVersion": 1,
            "generationId": identifier,
            "createdAt": created.isoformat(),
            "baseGenerationId": None,
            "operation": "refresh",
            "state": state,
            "failureStage": "build" if state == "failed" else None,
            "error": "fixture" if state == "failed" else None,
            "artifacts": [],
            "hashes": {},
            "audit": None,
        }
        (path / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        (path / "diagnostic.json").write_text(
            json.dumps({"candidate": index}), encoding="utf-8"
        )
        return path

    def _expired_candidates(self) -> list[Path]:
        return [self._candidate(index) for index in range(12)]

    def test_plan_is_deterministic_read_only_and_protects_current_previous_and_latest(self) -> None:
        candidates = self._expired_candidates()
        current_before = (self.data / "current.json").read_bytes()
        plan_a = create_retention_plan(self.data, self.settings, now=NOW)
        plan_b = create_retention_plan(self.data, self.settings, now=NOW)
        self.assertEqual(plan_a["planDigest"], plan_b["planDigest"])
        self.assertEqual((self.data / "current.json").read_bytes(), current_before)
        targets = {item["relativePath"] for item in plan_a["deletions"]}
        self.assertEqual(
            targets,
            {
                candidates[0].relative_to(self.data).as_posix(),
                candidates[1].relative_to(self.data).as_posix(),
            },
        )
        protected = {item["relativePath"] for item in plan_a["protected"]}
        pointer = json.loads(current_before)
        self.assertIn(f"generations/{pointer['generationId']}", protected)
        self.assertIn(f"generations/{pointer['previousGenerationId']}", protected)
        self.assertTrue(all(path.exists() for path in candidates))

    def test_apply_is_digest_bound_transactional_idempotent_and_counts_bytes(self) -> None:
        candidates = self._expired_candidates()
        plan = create_retention_plan(self.data, self.settings, now=NOW)
        expected_files = sum(item["fileCount"] for item in plan["deletions"])
        expected_bytes = sum(item["bytes"] for item in plan["deletions"])
        result = apply_retention_plan(
            self.data,
            plan,
            plan["planDigest"],
            self.settings,
            runtime_dir=self.runtime,
        )
        self.assertEqual(result["deletedTargets"], 2)
        self.assertEqual(result["deletedFiles"], expected_files)
        self.assertEqual(result["deletedBytes"], expected_bytes)
        self.assertFalse(candidates[0].exists())
        self.assertFalse(candidates[1].exists())
        self.assertTrue(all(path.exists() for path in candidates[2:]))
        repeated = apply_retention_plan(
            self.data,
            plan,
            plan["planDigest"],
            self.settings,
            runtime_dir=self.runtime,
        )
        self.assertEqual(repeated["state"], "already_applied")
        self.assertTrue(repeated["noOp"])
        self.assertEqual(audit_retention(self.data, self.settings, now=NOW)["status"], "healthy")

    def test_apply_rejects_digest_path_escape_and_changed_target(self) -> None:
        self._expired_candidates()
        plan = create_retention_plan(self.data, self.settings, now=NOW)
        with self.assertRaisesRegex(RetentionError, "exact plan digest"):
            apply_retention_plan(
                self.data,
                plan,
                "0" * 64,
                self.settings,
                runtime_dir=self.runtime,
            )
        escaped = json.loads(json.dumps(plan))
        escaped["deletions"][0]["relativePath"] = "../outside"
        escaped["planDigest"] = _plan_digest(escaped)
        with self.assertRaises(RetentionError) as raised:
            apply_retention_plan(
                self.data,
                escaped,
                escaped["planDigest"],
                self.settings,
                runtime_dir=self.runtime,
            )
        self.assertEqual(raised.exception.code, "retention_unsafe_path")
        target = self.data / plan["deletions"][0]["relativePath"] / "diagnostic.json"
        target.write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaises(RetentionError) as raised:
            apply_retention_plan(
                self.data,
                plan,
                plan["planDigest"],
                self.settings,
                runtime_dir=self.runtime,
            )
        self.assertIn(
            raised.exception.code,
            {"retention_protected_set_changed", "retention_target_changed"},
        )

    def test_apply_rejects_rehashed_but_structurally_invalid_plan(self) -> None:
        candidates = self._expired_candidates()
        plan = create_retention_plan(self.data, self.settings, now=NOW)
        invalid = json.loads(json.dumps(plan))
        invalid["deletions"][0]["fileCount"] = "2"
        invalid["planDigest"] = _plan_digest(invalid)
        with self.assertRaises(RetentionError) as raised:
            apply_retention_plan(
                self.data,
                invalid,
                invalid["planDigest"],
                self.settings,
                runtime_dir=self.runtime,
            )
        self.assertEqual(raised.exception.code, "retention_plan_invalid")
        self.assertTrue(all(path.exists() for path in candidates))
        mismatched = RuntimeSettings(
            "08:00",
            "Asia/Shanghai",
            36,
            retention_candidate_days=8,
        )
        with self.assertRaises(RetentionError) as raised:
            apply_retention_plan(
                self.data,
                plan,
                plan["planDigest"],
                mismatched,
                runtime_dir=self.runtime,
            )
        self.assertEqual(raised.exception.code, "retention_policy_mismatch")
        self.assertTrue(all(path.exists() for path in candidates))

    def test_staging_failure_rolls_back_every_source_and_retry_succeeds(self) -> None:
        candidates = self._expired_candidates()
        plan = create_retention_plan(self.data, self.settings, now=NOW)
        calls = 0

        def interrupted(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fixture interruption")
            os.replace(source, target)

        with self.assertRaises(RetentionError) as raised:
            apply_retention_plan(
                self.data,
                plan,
                plan["planDigest"],
                self.settings,
                runtime_dir=self.runtime,
                mover=interrupted,
            )
        self.assertEqual(raised.exception.code, "retention_apply_failed")
        self.assertTrue(all(path.exists() for path in candidates))
        self.assertFalse(any(self.data.glob(".retention-transaction-*")))
        result = apply_retention_plan(
            self.data,
            plan,
            plan["planDigest"],
            self.settings,
            runtime_dir=self.runtime,
        )
        self.assertEqual(result["state"], "completed")

    def test_hard_interruption_is_recovered_before_retry(self) -> None:
        candidates = self._expired_candidates()
        plan = create_retention_plan(self.data, self.settings, now=NOW)
        calls = 0

        def hard_interruption(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("fixture hard interruption")
            os.replace(source, target)

        with self.assertRaises(KeyboardInterrupt):
            apply_retention_plan(
                self.data,
                plan,
                plan["planDigest"],
                self.settings,
                runtime_dir=self.runtime,
                mover=hard_interruption,
            )
        self.assertTrue(any(self.data.glob(".retention-transaction-*")))
        with self.assertRaises(RetentionError) as raised:
            create_retention_plan(self.data, self.settings, now=NOW)
        self.assertEqual(raised.exception.code, "retention_transaction_pending")
        recovered = recover_pending_retention_transactions(
            self.data,
            runtime_dir=self.runtime,
        )
        self.assertEqual(recovered["recoveredTransactions"], 1)
        self.assertTrue(all(path.exists() for path in candidates))
        self.assertFalse(any(self.data.glob(".retention-transaction-*")))
        result = apply_retention_plan(
            self.data,
            plan,
            plan["planDigest"],
            self.settings,
            runtime_dir=self.runtime,
        )
        self.assertEqual(result["state"], "completed")

    def test_corrupt_receipt_never_commits_an_interrupted_deletion(self) -> None:
        candidates = self._expired_candidates()
        plan = create_retention_plan(self.data, self.settings, now=NOW)
        calls = 0

        def hard_interruption(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("fixture hard interruption")
            os.replace(source, target)

        with self.assertRaises(KeyboardInterrupt):
            apply_retention_plan(
                self.data,
                plan,
                plan["planDigest"],
                self.settings,
                runtime_dir=self.runtime,
                mover=hard_interruption,
            )
        receipt = (
            self.runtime
            / "retention"
            / "receipts"
            / f"{plan['planDigest']}.json"
        )
        receipt.parent.mkdir(parents=True)
        receipt.write_text("{}", encoding="utf-8")

        with self.assertRaises(RetentionError) as raised:
            recover_pending_retention_transactions(
                self.data,
                runtime_dir=self.runtime,
            )
        self.assertEqual(raised.exception.code, "retention_plan_invalid")
        self.assertTrue(all(path.exists() for path in candidates))
        self.assertFalse(any(self.data.glob(".retention-transaction-*")))

    def test_active_candidate_and_referenced_capture_are_protected(self) -> None:
        active = self._candidate(99, state="building")
        scheduled = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        _, capture = write_capture_create_only(
            self.data,
            _bundle(
                scheduled_at=scheduled,
                captured_at=scheduled + timedelta(minutes=1),
            ),
        )
        reference = capture.relative_to(self.data).as_posix()
        with patch("pipeline.retention._capture_references", return_value={reference}):
            plan = create_retention_plan(self.data, self.settings, now=NOW)
        protected = {item["relativePath"] for item in plan["protected"]}
        self.assertIn(active.relative_to(self.data).as_posix(), protected)
        self.assertIn(reference, protected)
        self.assertNotIn(reference, {item["relativePath"] for item in plan["deletions"]})

    def test_link_or_hardlink_in_scanned_store_fails_closed(self) -> None:
        candidate_root = self.data / "generations" / ".candidates"
        candidate_root.mkdir(parents=True, exist_ok=True)
        link = candidate_root / "linked-candidate"
        try:
            os.symlink(self.data / "generations", link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("filesystem does not allow a test symlink")
        with self.assertRaises(RetentionError) as raised:
            create_retention_plan(self.data, self.settings, now=NOW)
        self.assertEqual(raised.exception.code, "retention_unsafe_link")

    def test_storage_thresholds_warn_and_only_hard_guard_discover(self) -> None:
        gib = 1024 * 1024 * 1024
        warning = storage_snapshot(
            self.data,
            self.settings,
            disk_usage=lambda _path: _Usage((100 * gib, 85 * gib, 15 * gib)),
        )
        self.assertEqual(warning.guard_state, "warning")
        healthy = require_discover_storage_capacity(
            self.data,
            self.settings,
            disk_usage=lambda _path: _Usage((100 * gib, 85 * gib, 15 * gib)),
        )
        self.assertEqual(healthy.guard_state, "warning")
        for usage in (
            _Usage((100 * gib, 90 * gib, 10 * gib)),
            _Usage((100 * gib, 80 * gib, 7 * gib)),
        ):
            with self.subTest(usage=usage), self.assertRaises(RetentionError) as raised:
                require_discover_storage_capacity(
                    self.data,
                    self.settings,
                    disk_usage=lambda _path, value=usage: value,
                )
            self.assertEqual(raised.exception.code, "discover_storage_guard")


if __name__ == "__main__":
    unittest.main()
