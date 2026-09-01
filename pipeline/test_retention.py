from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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

    def _candidate(
        self,
        index: int,
        *,
        state: str = "failed",
        created_at: datetime | None = None,
    ) -> Path:
        identifier = f"candidate-{index:02d}"
        path = self.data / "generations" / ".candidates" / identifier
        path.mkdir(parents=True)
        created = created_at or (
            datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
        )
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

    def _retained_generation(
        self,
        identifier: str,
        *,
        created_at: datetime,
        operation: str,
        discover: bool = False,
    ) -> Path:
        if discover:
            path = (
                self.data
                / "artifacts"
                / "trending"
                / "discover"
                / "v1"
                / "generations"
                / identifier
            )
        else:
            path = self.data / "generations" / identifier
        path.mkdir(parents=True)
        artifacts = ["trending/explosion.json"] if operation == "derive" else []
        manifest = {
            "schemaVersion": 1,
            "generationId": identifier,
            "createdAt": created_at.isoformat(),
            "operation": operation,
            "state": "ready",
            "artifacts": artifacts,
        }
        (path / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        (path / "artifact.json").write_text("{}", encoding="utf-8")
        return path

    def _write_capture(self, age_days: int, *, historical_days: int = 45) -> Path:
        scheduled = NOW - timedelta(days=age_days)
        _, capture = write_capture_create_only(
            self.data,
            _bundle(
                scheduled_at=scheduled,
                captured_at=scheduled + timedelta(minutes=1),
            ),
        )
        if historical_days != 45:
            payload = json.loads(capture.read_text(encoding="utf-8"))
            captured = datetime.fromisoformat(payload["capturedAt"].replace("Z", "+00:00"))
            payload["retention"]["retentionDays"] = historical_days
            payload["retention"]["retainUntil"] = (
                captured + timedelta(days=historical_days)
            ).isoformat().replace("+00:00", "Z")
            from pipeline.trending_observations import attach_bundle_digest

            payload = attach_bundle_digest(payload)
            capture.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return capture

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

    def test_recovery_conflict_preserves_both_staged_and_recreated_evidence(self) -> None:
        candidates = self._expired_candidates()
        plan = create_retention_plan(self.data, self.settings, now=NOW)

        def hard_interruption(source: Path, target: Path) -> None:
            os.replace(source, target)
            raise KeyboardInterrupt("fixture hard interruption after staging")

        with self.assertRaises(KeyboardInterrupt):
            apply_retention_plan(
                self.data,
                plan,
                plan["planDigest"],
                self.settings,
                runtime_dir=self.runtime,
                mover=hard_interruption,
            )
        transaction = next(self.data.glob(".retention-transaction-*"))
        moved_source = next(path for path in candidates if not path.exists())
        relative = moved_source.relative_to(self.data)
        staged = transaction / "staged" / relative
        shutil.copytree(staged, moved_source)
        recreated_bytes = (moved_source / "diagnostic.json").read_bytes()
        staged_bytes = (staged / "diagnostic.json").read_bytes()

        with self.assertRaises(RetentionError) as raised:
            recover_pending_retention_transactions(
                self.data,
                runtime_dir=self.runtime,
            )

        self.assertEqual(raised.exception.code, "retention_recovery_conflict")
        self.assertEqual((moved_source / "diagnostic.json").read_bytes(), recreated_bytes)
        self.assertEqual((staged / "diagnostic.json").read_bytes(), staged_bytes)
        self.assertTrue(transaction.exists())

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

    def test_capture_retention_honors_age_references_and_historical_commitments(self) -> None:
        referenced = self._write_capture(60)
        expired = self._write_capture(46)
        recent = self._write_capture(44)
        historical = self._write_capture(47, historical_days=90)
        reference = referenced.relative_to(self.data).as_posix()
        with patch("pipeline.retention._capture_references", return_value={reference}):
            plan = create_retention_plan(self.data, self.settings, now=NOW)
        targets = {item["relativePath"] for item in plan["deletions"]}
        protected = {item["relativePath"] for item in plan["protected"]}
        self.assertIn(reference, protected)
        self.assertNotIn(reference, targets)
        self.assertIn(expired.relative_to(self.data).as_posix(), targets)
        self.assertNotIn(recent.relative_to(self.data).as_posix(), targets)
        self.assertNotIn(historical.relative_to(self.data).as_posix(), targets)

    def test_refresh_explosion_and_discover_use_distinct_retention_windows(self) -> None:
        refresh = [
            self._retained_generation(
                f"retention-refresh-{index}",
                created_at=NOW - timedelta(days=31, minutes=-index),
                operation="refresh",
            )
            for index in range(4)
        ]
        refresh_recent = self._retained_generation(
            "retention-refresh-recent",
            created_at=NOW - timedelta(days=29),
            operation="refresh",
        )
        explosion = [
            self._retained_generation(
                f"retention-explosion-{index}",
                created_at=NOW - timedelta(days=31, minutes=-index),
                operation="derive",
            )
            for index in range(4)
        ]
        explosion_recent = self._retained_generation(
            "retention-explosion-recent",
            created_at=NOW - timedelta(days=29),
            operation="derive",
        )
        discover = [
            self._retained_generation(
                f"retention-discover-{index}",
                created_at=NOW - timedelta(days=15, minutes=-index),
                operation="discover",
                discover=True,
            )
            for index in range(4)
        ]
        discover_recent = self._retained_generation(
            "retention-discover-recent",
            created_at=NOW - timedelta(days=13),
            operation="discover",
            discover=True,
        )
        verified = SimpleNamespace(manifest_sha256="a" * 64)
        discover_report = {"manifestSha256": "b" * 64, "status": "healthy"}
        with (
            patch("pipeline.retention.verify_retained_generation", return_value=verified),
            patch("pipeline.retention.audit_discover_generation", return_value=discover_report),
            patch("pipeline.retention._capture_references", return_value=set()),
        ):
            plan = create_retention_plan(self.data, self.settings, now=NOW)
        targets = {item["relativePath"] for item in plan["deletions"]}
        self.assertIn(refresh[0].relative_to(self.data).as_posix(), targets)
        self.assertNotIn(refresh_recent.relative_to(self.data).as_posix(), targets)
        self.assertIn(explosion[0].relative_to(self.data).as_posix(), targets)
        self.assertNotIn(explosion_recent.relative_to(self.data).as_posix(), targets)
        self.assertIn(discover[0].relative_to(self.data).as_posix(), targets)
        self.assertNotIn(discover_recent.relative_to(self.data).as_posix(), targets)

    def test_candidate_windows_preserve_latest_ten_and_apply_state_specific_age(self) -> None:
        failed_old = self._candidate(0, created_at=NOW - timedelta(days=4))
        for index in range(1, 11):
            self._candidate(index, created_at=NOW - timedelta(days=2, minutes=-index))
        ready_old = self._candidate(20, state="ready", created_at=NOW - timedelta(days=8))
        ready_recent = None
        for index in range(21, 31):
            ready_recent = self._candidate(
                index,
                state="ready",
                created_at=NOW - timedelta(days=6, minutes=-index),
            )
        plan = create_retention_plan(self.data, self.settings, now=NOW)
        targets = {item["relativePath"] for item in plan["deletions"]}
        self.assertIn(failed_old.relative_to(self.data).as_posix(), targets)
        self.assertIn(ready_old.relative_to(self.data).as_posix(), targets)
        self.assertIsNotNone(ready_recent)
        self.assertNotIn(ready_recent.relative_to(self.data).as_posix(), targets)

    def test_single_expired_candidate_is_protected_as_latest_diagnostic(self) -> None:
        failed = self._candidate(0, created_at=NOW - timedelta(days=4))
        ready = self._candidate(1, state="ready", created_at=NOW - timedelta(days=8))
        plan = create_retention_plan(self.data, self.settings, now=NOW)
        protected = {item["relativePath"] for item in plan["protected"]}
        self.assertIn(failed.relative_to(self.data).as_posix(), protected)
        self.assertIn(ready.relative_to(self.data).as_posix(), protected)

    def test_temporary_cutoff_is_strict_and_building_candidate_is_protected(self) -> None:
        root = self.data / "artifacts" / "trending"
        root.mkdir(parents=True, exist_ok=True)
        expired = root / "expired.partial"
        boundary = root / "boundary.tmp"
        expired.write_text("expired", encoding="utf-8")
        boundary.write_text("boundary", encoding="utf-8")
        expired_time = (NOW - timedelta(hours=25)).timestamp()
        boundary_time = (NOW - timedelta(hours=24)).timestamp()
        os.utime(expired, (expired_time, expired_time))
        os.utime(boundary, (boundary_time, boundary_time))
        active = self._candidate(99, state="building", created_at=NOW - timedelta(days=2))
        nested = active / "work.partial"
        nested.write_text("active", encoding="utf-8")
        os.utime(nested, (expired_time, expired_time))
        plan = create_retention_plan(self.data, self.settings, now=NOW)
        targets = {item["relativePath"] for item in plan["deletions"]}
        self.assertIn(expired.relative_to(self.data).as_posix(), targets)
        self.assertNotIn(boundary.relative_to(self.data).as_posix(), targets)
        self.assertNotIn(nested.relative_to(self.data).as_posix(), targets)

    def test_external_operator_release_and_backup_roots_are_audit_only(self) -> None:
        external = Path(self.temporary.name) / "external"
        release = external / "releases"
        backup = external / "backups"
        operator = external / "operator"
        for root in (release, backup, operator):
            root.mkdir(parents=True)
            (root / "evidence.txt").write_text("evidence", encoding="utf-8")
        plan = create_retention_plan(
            self.data,
            self.settings,
            now=NOW,
            release_roots=(release,),
            backup_roots=(backup,),
            operator_artifact_roots=(operator,),
        )
        self.assertFalse(plan["externalAuditOnly"]["automaticDeletion"])
        self.assertEqual(plan["externalAuditOnly"]["releaseDirectories"]["files"], 1)
        self.assertEqual(plan["externalAuditOnly"]["deploymentBackups"]["files"], 1)
        self.assertEqual(plan["externalAuditOnly"]["operatorArtifacts"]["files"], 1)
        self.assertTrue(all((root / "evidence.txt").exists() for root in (release, backup, operator)))

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
