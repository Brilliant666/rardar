from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pipeline.resolve_project_artifact_conflict as resolver_module
from pipeline.audit_data import audit_current_data
from pipeline.generations import (
    create_candidate_generation,
    fail_candidate_generation,
    finalize_candidate_generation,
    publish_candidate_generation,
    resolve_current_generation,
)
from pipeline.project_identity import (
    legacy_slug_for_repository,
    project_id_for_repository,
)
from pipeline.rebuild_derived import _rebuild_derived_candidate, rebuild_derived
from pipeline.resolve_project_artifact_conflict import (
    BLOCKED,
    KEEP_STABLE,
    PROMOTE_LEGACY,
    ArtifactConflictResolutionError,
    _write_new_validated_flat_artifact,
    main,
    resolve_project_artifact_conflict,
)
from pipeline.schema_validation import (
    ArtifactKind,
    load_validated_json,
    strict_json_dumps,
)
from pipeline.test_generations import _seed_legacy


REPOSITORY = "n8n-io/n8n"
PROJECT_ID = project_id_for_repository(REPOSITORY)
EVIDENCE_REFERENCE = (
    "docs/iterations/2026-07-22-staging-artifact-conflict-resolution.md"
    "#n8n-analysis"
)
OPENHANDS_REPOSITORY = "OpenHands/OpenHands"
OPENHANDS_EVIDENCE_REFERENCE = (
    "docs/iterations/2026-07-22-staging-artifact-conflict-resolution.md"
    "#openhands-analysis"
)


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_json(path: Path, payload: dict[str, object]) -> bytes:
    source = (strict_json_dumps(payload) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source)
    return source


def _tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_time(value: object) -> datetime:
    assert isinstance(value, str)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _minimal_n8n_evidence(analyzed_at: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "source": "https://github.com/n8n-io/n8n",
        "scanned_files": 12,
        "language_files": {".ts": 8, ".json": 4},
        "indicators": {
            "readme": True,
            "license": True,
            "tests": True,
            "ci": True,
            "docker": True,
            "dependency_lock": True,
            "package_manifest": True,
            "examples": True,
            "docs": True,
            "environment_example": True,
        },
        "counts": {"test_files": 3, "todo_markers": 1},
        "license_hint": "Sustainable Use License",
        "confidence": 90,
        "warnings": ["minimal sanitized n8n-shaped regression fixture"],
        "analyzed_at": analyzed_at,
    }


class ProjectArtifactConflictResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._base_temporary = tempfile.TemporaryDirectory()
        cls.base_data = Path(cls._base_temporary.name) / "data"
        _seed_legacy(cls.base_data)
        bootstrap_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
        stable_at = datetime(2026, 7, 16, tzinfo=timezone.utc)
        bootstrap = create_candidate_generation(
            cls.base_data,
            "bootstrap",
            generation_id="resolver-bootstrap-v1",
            created_at=bootstrap_at,
        )
        publish_candidate_generation(bootstrap, published_at=bootstrap_at)

        stable = create_candidate_generation(
            cls.base_data,
            "derive",
            generation_id="resolver-stable-v2",
            created_at=stable_at,
            overlay_flat_staging=False,
        )
        candidate_legacy = (
            stable.path
            / "analysis"
            / f"{legacy_slug_for_repository(REPOSITORY)}.json"
        )
        _write_json(
            candidate_legacy,
            _minimal_n8n_evidence("2026-07-16T00:02:49.538331+00:00"),
        )
        _rebuild_derived_candidate(stable, stable_at)
        finalize_candidate_generation(stable)
        publish_candidate_generation(stable, published_at=stable_at)

        current = resolve_current_generation(cls.base_data)
        stable_path = current.root / "analysis" / f"{PROJECT_ID}.json"
        if not stable_path.is_file():
            raise AssertionError("n8n stable fixture is unavailable")
        failed = create_candidate_generation(
            cls.base_data,
            "derive",
            generation_id="resolver-failed-sentinel",
            created_at=stable_at + timedelta(minutes=1),
            overlay_flat_staging=False,
        )
        fail_candidate_generation(
            failed,
            "build",
            "preserve failed candidate evidence",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._base_temporary.cleanup()

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        self._environment = patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": str(self.root / "local-state"),
                "RARDAR_DATA_DIR": str(self.root / "data"),
                "RARDAR_RUNTIME_DIR": str(self.root / "runtime"),
            },
        )
        self._environment.start()
        self.data_dir = self.root / "data"
        self.archive_dir = self.root / "audit-archive"
        shutil.copytree(self.base_data, self.data_dir)

        self.current = resolve_current_generation(self.data_dir)
        self.stable_path = self.current.root / "analysis" / f"{PROJECT_ID}.json"
        self.stable_bytes = self.stable_path.read_bytes()
        self.stable_sha = _sha256(self.stable_bytes)
        self.stable_payload = load_validated_json(
            self.stable_path,
            ArtifactKind.STATIC_EVIDENCE,
            expected_repository=REPOSITORY,
        )
        self.legacy_path = (
            self.data_dir
            / "analysis"
            / f"{legacy_slug_for_repository(REPOSITORY)}.json"
        )
        stable_snapshot = _read_json(self.current.root / "snapshots" / "latest.json")
        stable_repository = next(
            item
            for item in stable_snapshot["repositories"]
            if isinstance(item, dict) and item.get("repo") == REPOSITORY
        )
        self.stable_source_pushed_at = str(stable_repository["pushed_at"])
        self._write_legacy(timedelta(days=-1))

    def tearDown(self) -> None:
        self._environment.stop()
        self._temporary.cleanup()

    def _write_legacy(self, offset: timedelta) -> None:
        payload = dict(self.stable_payload)
        payload["schemaVersion"] = 1
        payload.pop("projectIdVersion", None)
        payload.pop("projectId", None)
        analyzed_at = _parse_time(self.stable_payload["analyzed_at"]) + offset
        payload["analyzed_at"] = analyzed_at.astimezone(timezone.utc).isoformat()
        counts = dict(payload["counts"])
        counts["todo_markers"] = int(counts["todo_markers"]) + 1
        payload["counts"] = counts
        self.legacy_payload = payload
        self.legacy_bytes = _write_json(self.legacy_path, payload)
        self.legacy_sha = _sha256(self.legacy_bytes)
        stable_pushed = _parse_time(self.stable_source_pushed_at)
        self.legacy_source_pushed_at = (
            stable_pushed + offset
        ).astimezone(timezone.utc).isoformat()
        snapshot_path = self.data_dir / "snapshots" / "latest.json"
        snapshot = _read_json(snapshot_path)
        captured_at = analyzed_at - timedelta(minutes=1)
        snapshot["captured_at"] = captured_at.astimezone(timezone.utc).isoformat()
        repositories = snapshot["repositories"]
        assert isinstance(repositories, list)
        found = False
        for item in repositories:
            if isinstance(item, dict) and item.get("repo") == REPOSITORY:
                item["pushed_at"] = self.legacy_source_pushed_at
                item["captured_at"] = snapshot["captured_at"]
                found = True
        if not found:
            raise AssertionError("n8n snapshot fixture is unavailable")
        _write_json(snapshot_path, snapshot)

    def _prepare_analysis_conflict(
        self,
        repository: str,
        offset: timedelta,
    ) -> dict[str, object]:
        project_id = project_id_for_repository(repository)
        stable_path = self.current.root / "analysis" / f"{project_id}.json"
        stable_bytes = stable_path.read_bytes()
        stable_payload = load_validated_json(
            stable_path,
            ArtifactKind.STATIC_EVIDENCE,
            expected_repository=repository,
        )
        stable_snapshot = _read_json(self.current.root / "snapshots" / "latest.json")
        stable_item = next(
            item
            for item in stable_snapshot["repositories"]
            if isinstance(item, dict) and item.get("repo") == repository
        )
        stable_pushed = str(stable_item["pushed_at"])
        legacy_payload = dict(stable_payload)
        legacy_payload["schemaVersion"] = 1
        legacy_payload.pop("projectIdVersion", None)
        legacy_payload.pop("projectId", None)
        analyzed_at = _parse_time(stable_payload["analyzed_at"]) + offset
        legacy_payload["analyzed_at"] = analyzed_at.isoformat()
        counts = dict(legacy_payload["counts"])
        counts["test_files"] = int(counts["test_files"]) + 1
        legacy_payload["counts"] = counts
        legacy_path = (
            self.data_dir
            / "analysis"
            / f"{legacy_slug_for_repository(repository)}.json"
        )
        legacy_bytes = _write_json(legacy_path, legacy_payload)
        legacy_pushed = (_parse_time(stable_pushed) + offset).isoformat()

        flat_snapshot_path = self.data_dir / "snapshots" / "latest.json"
        flat_snapshot = _read_json(flat_snapshot_path)
        flat_snapshot["captured_at"] = (analyzed_at - timedelta(minutes=1)).isoformat()
        repositories = flat_snapshot["repositories"]
        assert isinstance(repositories, list)
        matched = False
        for item in repositories:
            if isinstance(item, dict) and item.get("repo") == repository:
                item["pushed_at"] = legacy_pushed
                item["captured_at"] = flat_snapshot["captured_at"]
                matched = True
        if not matched:
            raise AssertionError(f"snapshot fixture is unavailable for {repository}")
        _write_json(flat_snapshot_path, flat_snapshot)
        return {
            "repository": repository,
            "projectId": project_id,
            "stablePath": stable_path,
            "stableBytes": stable_bytes,
            "stableSha": _sha256(stable_bytes),
            "stablePushedAt": stable_pushed,
            "legacyPath": legacy_path,
            "legacyBytes": legacy_bytes,
            "legacySha": _sha256(legacy_bytes),
            "legacyPushedAt": legacy_pushed,
        }

    def _resolve(
        self,
        decision: str = KEEP_STABLE,
        *,
        apply: bool = False,
        repository: str = REPOSITORY,
        legacy_sha: str | None = None,
        stable_sha: str | None = None,
        archive_dir: Path | None = None,
        evidence_reference: str = EVIDENCE_REFERENCE,
    ) -> dict[str, object]:
        return resolve_project_artifact_conflict(
            self.data_dir,
            repository=repository,
            kind="analysis",
            decision=decision,
            expected_legacy_sha256=legacy_sha or self.legacy_sha,
            expected_stable_sha256=stable_sha or self.stable_sha,
            evidence_reference=evidence_reference,
            legacy_source_pushed_at=self.legacy_source_pushed_at,
            stable_source_pushed_at=self.stable_source_pushed_at,
            apply=apply,
            archive_dir=archive_dir or self.archive_dir,
        )

    def _next_publication_time(self) -> datetime:
        pointer = _read_json(self.data_dir / "current.json")
        return max(
            datetime.now(timezone.utc),
            _parse_time(pointer["publishedAt"]) + timedelta(seconds=1),
        )

    def test_default_dry_run_has_zero_data_or_archive_writes(self) -> None:
        before = _tree_bytes(self.data_dir)

        report = self._resolve()

        self.assertEqual(report["status"], "dry-run")
        self.assertFalse(report["apply"])
        self.assertEqual(_tree_bytes(self.data_dir), before)
        self.assertFalse(self.archive_dir.exists())

    def test_both_expected_hashes_are_strict_preconditions(self) -> None:
        before = _tree_bytes(self.data_dir)
        with self.assertRaises(ArtifactConflictResolutionError) as legacy_error:
            self._resolve(legacy_sha="0" * 64)
        self.assertEqual(legacy_error.exception.code, "legacy_sha256_mismatch")

        with self.assertRaises(ArtifactConflictResolutionError) as stable_error:
            self._resolve(stable_sha="1" * 64)
        self.assertEqual(
            stable_error.exception.code,
            "stable_reference_not_manifest_bound",
        )
        with self.assertRaises(ArtifactConflictResolutionError) as evidence_error:
            self._resolve(evidence_reference="https://example.test/token")
        self.assertEqual(
            evidence_error.exception.code,
            "invalid_evidence_reference",
        )
        with self.assertRaises(ArtifactConflictResolutionError) as missing_evidence:
            self._resolve(
                evidence_reference="docs/iterations/does-not-exist.md#review"
            )
        self.assertEqual(
            missing_evidence.exception.code,
            "evidence_reference_unavailable",
        )
        self.assertEqual(_tree_bytes(self.data_dir), before)
        self.assertFalse(self.archive_dir.exists())

    def test_repository_payload_mismatch_and_path_traversal_fail_closed(self) -> None:
        mismatched = dict(self.legacy_payload)
        mismatched["repository"] = "other/repository"
        mismatched["source"] = "https://github.com/other/repository"
        source = _write_json(self.legacy_path, mismatched)

        with self.assertRaises(ArtifactConflictResolutionError):
            self._resolve(legacy_sha=_sha256(source))
        with self.assertRaises(ArtifactConflictResolutionError):
            self._resolve(repository="../n8n")
        self.assertFalse(self.archive_dir.exists())

    def test_invalid_current_project_identity_is_rejected_structurally(self) -> None:
        relative = f"analysis/{PROJECT_ID}.json"
        payload = dict(self.stable_payload)
        payload["projectId"] = project_id_for_repository("other/repository")
        changed = _write_json(self.stable_path, payload)

        manifest_path = self.current.root / "manifest.json"
        manifest = _read_json(manifest_path)
        hashes = dict(manifest["hashes"])
        hashes[relative] = _sha256(changed)
        manifest["hashes"] = hashes
        manifest_bytes = _write_json(manifest_path, manifest)
        pointer_path = self.data_dir / "current.json"
        pointer = _read_json(pointer_path)
        pointer["manifestSha256"] = _sha256(manifest_bytes)
        _write_json(pointer_path, pointer)

        with self.assertRaises(ArtifactConflictResolutionError) as raised:
            self._resolve(stable_sha=_sha256(changed))
        self.assertEqual(raised.exception.code, "invalid_current_generation")
        self.assertFalse(self.archive_dir.exists())

    def test_archive_inside_data_is_rejected(self) -> None:
        with self.assertRaises(ArtifactConflictResolutionError) as inside:
            self._resolve(archive_dir=self.data_dir / "conflict-archive")
        self.assertEqual(inside.exception.code, "archive_inside_protected_tree")

    def test_archive_inside_detected_git_worktree_is_rejected(self) -> None:
        (self.root / ".git").write_text("gitdir: isolated-fixture\n", encoding="utf-8")
        with self.assertRaises(ArtifactConflictResolutionError) as raised:
            self._resolve(archive_dir=self.archive_dir)
        self.assertEqual(raised.exception.code, "archive_inside_protected_tree")

    def test_archive_filesystem_link_is_rejected(self) -> None:
        target = self.root / "real-archive"
        target.mkdir()
        linked = self.root / "linked-archive"
        try:
            os.symlink(target, linked, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"filesystem links are unavailable: {error}")
        with self.assertRaises(ArtifactConflictResolutionError) as linked_error:
            self._resolve(archive_dir=linked)
        self.assertEqual(linked_error.exception.code, "unsafe_archive_path")

    @unittest.skipUnless(os.name == "nt", "junction regression is Windows-specific")
    def test_archive_junction_is_rejected_without_touching_target(self) -> None:
        target = self.root / "junction-target"
        target.mkdir()
        sentinel = target / "sentinel.txt"
        sentinel.write_text("external target must remain unchanged\n", encoding="utf-8")
        before = sentinel.read_bytes()
        junction = self.root / "junction-archive"
        created = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(junction),
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            self.skipTest(f"junction creation is unavailable: {created.stderr}")
        try:
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(archive_dir=junction)
            self.assertEqual(raised.exception.code, "unsafe_archive_path")
            self.assertEqual(sentinel.read_bytes(), before)
        finally:
            if os.path.lexists(junction):
                os.rmdir(junction)

    def test_keep_stable_archives_exact_bytes_and_preserves_published_data(self) -> None:
        protected_before = {
            "current": (self.data_dir / "current.json").read_bytes(),
            "generations": _tree_bytes(self.data_dir / "generations"),
            "failedCandidate": _tree_bytes(
                self.data_dir
                / "generations"
                / ".candidates"
                / "resolver-failed-sentinel"
            ),
        }

        report = self._resolve(apply=True)

        self.assertEqual(report["status"], "applied")
        self.assertFalse(self.legacy_path.exists())
        self.assertEqual(Path(str(report["archivedArtifact"])).read_bytes(), self.legacy_bytes)
        self.assertEqual(Path(str(report["detachedArtifact"])).read_bytes(), self.legacy_bytes)
        audit = _read_json(Path(str(report["auditRecord"])))
        self.assertEqual(audit["state"], "resolved")
        self.assertEqual(audit["repository"], REPOSITORY)
        self.assertEqual(audit["artifactKind"], "analysis")
        self.assertEqual(audit["detachedArtifact"], "detached-legacy.json")
        self.assertEqual(audit["legacySha256"], self.legacy_sha)
        self.assertEqual(audit["stableSha256"], self.stable_sha)
        self.assertEqual(audit["stableReferenceGeneration"], self.current.generation_id)
        self.assertEqual(
            (self.data_dir / "current.json").read_bytes(),
            protected_before["current"],
        )
        self.assertEqual(
            _tree_bytes(self.data_dir / "generations"),
            protected_before["generations"],
        )
        self.assertEqual(
            _tree_bytes(
                self.data_dir
                / "generations"
                / ".candidates"
                / "resolver-failed-sentinel"
            ),
            protected_before["failedCandidate"],
        )
        self.assertEqual(self.stable_path.read_bytes(), self.stable_bytes)

        second = self._resolve(apply=True)
        self.assertEqual(second["status"], "no-op")
        self.assertTrue(second["idempotentNoop"])

        rebuild_derived(self.data_dir, self._next_publication_time())
        current = resolve_current_generation(self.data_dir)
        self.assertNotEqual(current.generation_id, self.current.generation_id)
        after_publication = self._resolve(apply=True)
        self.assertEqual(after_publication["status"], "no-op")

    def test_promote_newer_legacy_is_mechanical_audited_and_buildable(self) -> None:
        self._write_legacy(timedelta(days=1))
        generations_before = _tree_bytes(self.data_dir / "generations")
        current_before = (self.data_dir / "current.json").read_bytes()

        report = self._resolve(PROMOTE_LEGACY, apply=True)

        stable_flat = self.data_dir / "analysis" / f"{PROJECT_ID}.json"
        promoted = load_validated_json(
            stable_flat,
            ArtifactKind.STATIC_EVIDENCE,
            expected_repository=REPOSITORY,
        )
        expected = {
            **self.legacy_payload,
            "schemaVersion": 2,
            "projectIdVersion": 1,
            "projectId": PROJECT_ID,
        }
        self.assertEqual(report["status"], "applied")
        self.assertEqual(promoted, expected)
        self.assertFalse(self.legacy_path.exists())
        self.assertEqual(Path(str(report["archivedArtifact"])).read_bytes(), self.legacy_bytes)
        self.assertEqual(Path(str(report["detachedArtifact"])).read_bytes(), self.legacy_bytes)
        self.assertEqual(_tree_bytes(self.data_dir / "generations"), generations_before)
        self.assertEqual((self.data_dir / "current.json").read_bytes(), current_before)

        rebuild_derived(self.data_dir, self._next_publication_time())
        self.assertEqual(audit_current_data(self.data_dir)["status"], "healthy")

    def test_blocked_decision_is_zero_write_even_when_apply_is_requested(self) -> None:
        self._write_legacy(timedelta())
        before = _tree_bytes(self.data_dir)

        report = self._resolve(BLOCKED, apply=True)

        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["apply"])
        self.assertEqual(_tree_bytes(self.data_dir), before)
        self.assertFalse(self.archive_dir.exists())

    def test_equal_source_times_reject_keep_and_promote_without_writes(self) -> None:
        self._write_legacy(timedelta())
        before = _tree_bytes(self.data_dir)

        for decision in (KEEP_STABLE, PROMOTE_LEGACY):
            with self.subTest(decision=decision):
                with self.assertRaises(ArtifactConflictResolutionError) as raised:
                    self._resolve(decision, apply=True)
                self.assertEqual(
                    raised.exception.code,
                    "blocked_unprovable_time_order",
                )
        self.assertEqual(_tree_bytes(self.data_dir), before)
        self.assertFalse(self.archive_dir.exists())

    def test_mechanically_equal_facts_follow_normal_candidate_adoption(self) -> None:
        payload = dict(self.stable_payload)
        payload["schemaVersion"] = 1
        payload.pop("projectIdVersion", None)
        payload.pop("projectId", None)
        _write_json(self.legacy_path, payload)
        current_before = self.current.generation_id

        rebuild_derived(self.data_dir, self._next_publication_time())

        current = resolve_current_generation(self.data_dir)
        self.assertNotEqual(current.generation_id, current_before)
        self.assertFalse(
            (current.root / "analysis" / self.legacy_path.name).exists()
        )
        adopted = load_validated_json(
            current.root / "analysis" / f"{PROJECT_ID}.json",
            ArtifactKind.STATIC_EVIDENCE,
            expected_repository=REPOSITORY,
        )
        self.assertEqual(adopted, self.stable_payload)
        self.assertEqual(audit_current_data(self.data_dir)["status"], "healthy")

    def test_second_repository_is_resolved_without_name_specific_logic(self) -> None:
        fixture = self._prepare_analysis_conflict(
            OPENHANDS_REPOSITORY,
            timedelta(days=-1),
        )
        n8n_before = self.legacy_path.read_bytes()

        report = resolve_project_artifact_conflict(
            self.data_dir,
            repository=OPENHANDS_REPOSITORY,
            kind="analysis",
            decision=KEEP_STABLE,
            expected_legacy_sha256=str(fixture["legacySha"]),
            expected_stable_sha256=str(fixture["stableSha"]),
            evidence_reference=OPENHANDS_EVIDENCE_REFERENCE,
            legacy_source_pushed_at=str(fixture["legacyPushedAt"]),
            stable_source_pushed_at=str(fixture["stablePushedAt"]),
            apply=True,
            archive_dir=self.archive_dir,
        )

        self.assertEqual(report["status"], "applied")
        self.assertFalse(Path(fixture["legacyPath"]).exists())
        self.assertEqual(
            Path(str(report["archivedArtifact"])).read_bytes(),
            fixture["legacyBytes"],
        )
        self.assertEqual(self.legacy_path.read_bytes(), n8n_before)

    def test_project_enrichment_kind_uses_the_same_fail_closed_protocol(self) -> None:
        stable_path = self.current.root / "enrichment" / f"{PROJECT_ID}.json"
        stable_bytes = stable_path.read_bytes()
        stable = load_validated_json(
            stable_path,
            ArtifactKind.PROJECT_ENRICHMENT,
            expected_repository=REPOSITORY,
        )
        legacy = dict(stable)
        legacy["schemaVersion"] = 1
        legacy.pop("projectIdVersion", None)
        legacy.pop("projectId", None)
        legacy["analyzedAt"] = (
            _parse_time(stable["analyzedAt"]) - timedelta(minutes=1)
        ).isoformat()
        legacy["summaryZh"] = f"{legacy['summaryZh']}（旧证据）"
        legacy_path = (
            self.data_dir
            / "enrichment"
            / f"{legacy_slug_for_repository(REPOSITORY)}.json"
        )
        legacy_bytes = _write_json(legacy_path, legacy)

        report = resolve_project_artifact_conflict(
            self.data_dir,
            repository=REPOSITORY,
            kind="enrichment",
            decision=KEEP_STABLE,
            expected_legacy_sha256=_sha256(legacy_bytes),
            expected_stable_sha256=_sha256(stable_bytes),
            evidence_reference=EVIDENCE_REFERENCE,
            apply=True,
            archive_dir=self.archive_dir,
        )

        self.assertEqual(report["status"], "applied")
        self.assertFalse(legacy_path.exists())
        self.assertEqual(
            Path(str(report["archivedArtifact"])).read_bytes(),
            legacy_bytes,
        )

    def test_cleanup_interruption_retries_from_prepared_audit(self) -> None:
        with patch(
            "pipeline.resolve_project_artifact_conflict._remove_active_legacy",
            side_effect=OSError("simulated cleanup interruption"),
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "legacy_cleanup_failed")
        self.assertTrue(self.legacy_path.exists())
        records = list(self.archive_dir.rglob("resolution.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(_read_json(records[0])["state"], "prepared")

        retried = self._resolve(apply=True)
        self.assertEqual(retried["status"], "applied")
        self.assertFalse(self.legacy_path.exists())
        self.assertEqual(_read_json(records[0])["state"], "resolved")

    def test_final_audit_interruption_retries_after_source_cleanup(self) -> None:
        with patch(
            "pipeline.resolve_project_artifact_conflict._mark_resolved",
            side_effect=RuntimeError("simulated process interruption"),
        ):
            with self.assertRaises(RuntimeError):
                self._resolve(apply=True)

        self.assertFalse(self.legacy_path.exists())
        record = next(self.archive_dir.rglob("resolution.json"))
        self.assertEqual(_read_json(record)["state"], "prepared")

        rebuild_derived(self.data_dir, self._next_publication_time())
        retried = self._resolve(apply=True)
        self.assertEqual(retried["status"], "applied")
        self.assertEqual(_read_json(record)["state"], "resolved")

    def test_audit_record_rejects_extra_fields_and_boolean_schema_version(self) -> None:
        with patch(
            "pipeline.resolve_project_artifact_conflict._remove_active_legacy",
            side_effect=OSError("simulated cleanup interruption"),
        ):
            with self.assertRaises(ArtifactConflictResolutionError):
                self._resolve(apply=True)

        record = next(self.archive_dir.rglob("resolution.json"))
        payload = _read_json(record)
        payload["unexpected"] = "must not be persisted"
        _write_json(record, payload)
        with self.assertRaises(ArtifactConflictResolutionError) as extra:
            self._resolve(apply=True)
        self.assertEqual(extra.exception.code, "invalid_audit_record")

        payload.pop("unexpected")
        payload["schemaVersion"] = True
        _write_json(record, payload)
        with self.assertRaises(ArtifactConflictResolutionError) as boolean:
            self._resolve(apply=True)
        self.assertEqual(boolean.exception.code, "invalid_audit_record")
        self.assertTrue(self.legacy_path.exists())

    def test_non_equivalent_flat_stable_target_is_never_overwritten(self) -> None:
        target = self.data_dir / "analysis" / f"{PROJECT_ID}.json"
        conflicting = dict(self.stable_payload)
        counts = dict(conflicting["counts"])
        counts["test_files"] = int(counts["test_files"]) + 1
        conflicting["counts"] = counts
        before = _write_json(target, conflicting)

        with self.assertRaises(ArtifactConflictResolutionError) as raised:
            self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "flat_stable_conflict")
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(self.legacy_path.exists())
        self.assertFalse(self.archive_dir.exists())

    def test_stable_target_appearing_after_preflight_is_not_overwritten(self) -> None:
        self._write_legacy(timedelta(days=1))
        target = self.data_dir / "analysis" / f"{PROJECT_ID}.json"
        competing_bytes: bytes | None = None

        def create_competing_target(
            path: Path,
            kind: ArtifactKind,
            payload: dict[str, object],
            repository: str,
        ) -> None:
            nonlocal competing_bytes
            competing = dict(payload)
            counts = dict(competing["counts"])
            counts["test_files"] = int(counts["test_files"]) + 1
            competing["counts"] = counts
            competing_bytes = _write_json(path, competing)
            _write_new_validated_flat_artifact(path, kind, payload, repository)

        with patch(
            "pipeline.resolve_project_artifact_conflict."
            "_write_new_validated_flat_artifact",
            side_effect=create_competing_target,
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(PROMOTE_LEGACY, apply=True)

        self.assertEqual(raised.exception.code, "flat_stable_changed")
        self.assertIsNotNone(competing_bytes)
        self.assertEqual(target.read_bytes(), competing_bytes)
        self.assertTrue(self.legacy_path.exists())
        record = next(self.archive_dir.rglob("resolution.json"))
        self.assertEqual(_read_json(record)["state"], "prepared")

    def test_noop_fails_closed_if_bound_retained_generation_is_damaged(self) -> None:
        applied = self._resolve(apply=True)
        self.assertEqual(applied["status"], "applied")
        rebuild_derived(self.data_dir, self._next_publication_time())
        self.stable_path.write_text("{}\n", encoding="utf-8")

        with self.assertRaises(ArtifactConflictResolutionError) as raised:
            self._resolve(apply=True)

        self.assertEqual(
            raised.exception.code,
            "invalid_archived_stable_reference",
        )

    def test_promotion_write_failure_leaves_source_and_prepared_archive_retryable(
        self,
    ) -> None:
        self._write_legacy(timedelta(days=1))
        stable_flat = self.data_dir / "analysis" / f"{PROJECT_ID}.json"
        with patch(
            "pipeline.resolve_project_artifact_conflict."
            "_write_new_validated_flat_artifact",
            side_effect=OSError("simulated stable write failure"),
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(PROMOTE_LEGACY, apply=True)

        self.assertEqual(raised.exception.code, "stable_target_write_failed")
        self.assertTrue(self.legacy_path.exists())
        self.assertFalse(stable_flat.exists())
        record = next(self.archive_dir.rglob("resolution.json"))
        self.assertEqual(_read_json(record)["state"], "prepared")

        retried = self._resolve(PROMOTE_LEGACY, apply=True)
        self.assertEqual(retried["status"], "applied")
        self.assertTrue(stable_flat.exists())
        self.assertFalse(self.legacy_path.exists())

    def test_final_cleanup_detects_swapped_legacy_and_restores_new_bytes(self) -> None:
        original_verify = resolver_module._verify_active_legacy
        replacement_bytes: bytes | None = None

        def verify_then_swap(preflight: object) -> None:
            nonlocal replacement_bytes
            original_verify(preflight)  # type: ignore[arg-type]
            replacement = dict(self.legacy_payload)
            counts = dict(replacement["counts"])
            counts["todo_markers"] = int(counts["todo_markers"]) + 100
            replacement["counts"] = counts
            replacement_bytes = _write_json(self.legacy_path, replacement)

        with patch(
            "pipeline.resolve_project_artifact_conflict._verify_active_legacy",
            side_effect=verify_then_swap,
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "legacy_source_changed")
        self.assertIsNotNone(replacement_bytes)
        self.assertEqual(self.legacy_path.read_bytes(), replacement_bytes)
        self.assertFalse(any(self.legacy_path.parent.glob("*.rardar-quarantine")))
        record = next(self.archive_dir.rglob("resolution.json"))
        self.assertEqual(_read_json(record)["state"], "prepared")

    def test_keep_rechecks_late_non_equivalent_flat_stable_before_cleanup(self) -> None:
        original_prepare = resolver_module._ensure_prepared_record
        target = self.data_dir / "analysis" / f"{PROJECT_ID}.json"
        competing_bytes: bytes | None = None

        def prepare_then_replace(preflight: object) -> dict[str, object]:
            nonlocal competing_bytes
            prepared = original_prepare(preflight)  # type: ignore[arg-type]
            competing = dict(self.stable_payload)
            counts = dict(competing["counts"])
            counts["test_files"] = int(counts["test_files"]) + 77
            competing["counts"] = counts
            competing_bytes = _write_json(target, competing)
            return prepared

        with patch(
            "pipeline.resolve_project_artifact_conflict._ensure_prepared_record",
            side_effect=prepare_then_replace,
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "flat_stable_changed")
        self.assertTrue(self.legacy_path.exists())
        self.assertEqual(target.read_bytes(), competing_bytes)
        record = next(self.archive_dir.rglob("resolution.json"))
        self.assertEqual(_read_json(record)["state"], "prepared")

    def test_quarantine_detachment_interruption_is_retryable(self) -> None:
        with patch(
            "pipeline.resolve_project_artifact_conflict._detach_quarantined_legacy",
            side_effect=OSError("simulated detachment interruption"),
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "legacy_cleanup_failed")
        self.assertFalse(self.legacy_path.exists())
        quarantine = next(self.legacy_path.parent.glob("*.rardar-quarantine"))
        self.assertEqual(quarantine.read_bytes(), self.legacy_bytes)
        record = next(self.archive_dir.rglob("resolution.json"))
        self.assertEqual(_read_json(record)["state"], "prepared")

        retried = self._resolve(apply=True)
        self.assertEqual(retried["status"], "applied")
        self.assertFalse(quarantine.exists())
        self.assertEqual(_read_json(record)["state"], "resolved")

    def test_detachment_move_then_validation_interruption_is_retryable(self) -> None:
        original = resolver_module._read_safe_regular_snapshot
        interrupted = False

        def interrupt_first_detached_read(
            path: Path,
            directory: Path,
            *,
            code: str,
            label: str = "protected regular file",
        ) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
            nonlocal interrupted
            if not interrupted and path.name == "detached-legacy.json":
                interrupted = True
                raise RuntimeError("simulated interruption after detached move")
            return original(path, directory, code=code, label=label)

        with patch(
            "pipeline.resolve_project_artifact_conflict._read_safe_regular_snapshot",
            side_effect=interrupt_first_detached_read,
        ):
            with self.assertRaises(RuntimeError):
                self._resolve(apply=True)

        record = next(self.archive_dir.rglob("resolution.json"))
        detached = next(self.archive_dir.rglob("detached-legacy.json"))
        self.assertTrue(interrupted)
        self.assertFalse(self.legacy_path.exists())
        self.assertFalse(any(self.legacy_path.parent.glob("*.rardar-quarantine")))
        self.assertEqual(detached.read_bytes(), self.legacy_bytes)
        self.assertEqual(_read_json(record)["state"], "prepared")

        retried = self._resolve(apply=True)
        self.assertEqual(retried["status"], "applied")
        self.assertEqual(_read_json(record)["state"], "resolved")

    def test_archive_before_prepared_interruption_is_retryable(self) -> None:
        with patch(
            "pipeline.resolve_project_artifact_conflict._ensure_prepared_record",
            side_effect=RuntimeError("simulated record interruption"),
        ):
            with self.assertRaises(RuntimeError):
                self._resolve(apply=True)

        archived = next(self.archive_dir.rglob("legacy.json"))
        self.assertEqual(archived.read_bytes(), self.legacy_bytes)
        self.assertFalse(list(self.archive_dir.rglob("resolution.json")))
        self.assertTrue(self.legacy_path.exists())

        retried = self._resolve(apply=True)
        self.assertEqual(retried["status"], "applied")

    def test_prepared_retry_requires_a_healthy_current_generation(self) -> None:
        with patch(
            "pipeline.resolve_project_artifact_conflict._remove_active_legacy",
            side_effect=OSError("simulated cleanup interruption"),
        ):
            with self.assertRaises(ArtifactConflictResolutionError):
                self._resolve(apply=True)
        record = next(self.archive_dir.rglob("resolution.json"))
        self.assertEqual(_read_json(record)["state"], "prepared")
        before = self.legacy_path.read_bytes()
        (self.data_dir / "current.json").write_text("{}\n", encoding="utf-8")

        with self.assertRaises(ArtifactConflictResolutionError) as raised:
            self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "invalid_current_generation")
        self.assertEqual(self.legacy_path.read_bytes(), before)
        self.assertEqual(_read_json(record)["state"], "prepared")

    def test_resolved_promotion_requires_promoted_flat_postcondition(self) -> None:
        self._write_legacy(timedelta(days=1))
        applied = self._resolve(PROMOTE_LEGACY, apply=True)
        self.assertEqual(applied["status"], "applied")
        target = self.data_dir / "analysis" / f"{PROJECT_ID}.json"
        target.unlink()

        with self.assertRaises(ArtifactConflictResolutionError) as raised:
            self._resolve(PROMOTE_LEGACY, apply=True)

        self.assertEqual(raised.exception.code, "flat_stable_changed")

    def test_snapshot_repository_capture_must_match_snapshot_and_precede_analysis(
        self,
    ) -> None:
        snapshot_path = self.data_dir / "snapshots" / "latest.json"
        snapshot = _read_json(snapshot_path)
        repositories = snapshot["repositories"]
        assert isinstance(repositories, list)
        analyzed_at = _parse_time(self.legacy_payload["analyzed_at"])
        for item in repositories:
            if isinstance(item, dict) and item.get("repo") == REPOSITORY:
                item["captured_at"] = (analyzed_at + timedelta(seconds=1)).isoformat()
                break
        _write_json(snapshot_path, snapshot)
        before = _tree_bytes(self.data_dir)

        with self.assertRaises(ArtifactConflictResolutionError) as raised:
            self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "untrusted_source_version")
        self.assertEqual(_tree_bytes(self.data_dir), before)
        self.assertFalse(self.archive_dir.exists())

    def test_blocked_without_source_flags_or_snapshot_binding_is_cli_zero_write(
        self,
    ) -> None:
        snapshot_path = self.data_dir / "snapshots" / "latest.json"
        snapshot = _read_json(snapshot_path)
        repositories = snapshot["repositories"]
        assert isinstance(repositories, list)
        snapshot["repositories"] = [
            item
            for item in repositories
            if not isinstance(item, dict) or item.get("repo") != REPOSITORY
        ]
        snapshot["count"] = len(snapshot["repositories"])
        _write_json(snapshot_path, snapshot)
        before = _tree_bytes(self.data_dir)
        stdout = io.StringIO()
        stderr = io.StringIO()
        arguments = [
            "--data-dir",
            str(self.data_dir),
            "--repository",
            REPOSITORY,
            "--kind",
            "analysis",
            "--decision",
            BLOCKED,
            "--expected-legacy-sha256",
            self.legacy_sha,
            "--expected-stable-sha256",
            self.stable_sha,
            "--evidence-reference",
            EVIDENCE_REFERENCE,
            "--archive-dir",
            str(self.archive_dir),
            "--apply",
        ]

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(arguments)

        report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["apply"])
        self.assertIsNone(report["sourceVersions"])
        self.assertEqual(_tree_bytes(self.data_dir), before)
        self.assertFalse(self.archive_dir.exists())

    def test_prepared_retry_uses_frozen_legacy_version_after_snapshot_advance(
        self,
    ) -> None:
        with patch(
            "pipeline.resolve_project_artifact_conflict._mark_resolved",
            side_effect=RuntimeError("simulated final audit interruption"),
        ):
            with self.assertRaises(RuntimeError):
                self._resolve(apply=True)

        record = next(self.archive_dir.rglob("resolution.json"))
        frozen_versions = _read_json(record)["sourceVersions"]
        switched_at = self._next_publication_time()
        switched = create_candidate_generation(
            self.data_dir,
            "derive",
            generation_id="resolver-retry-current-v3",
            created_at=switched_at,
            overlay_flat_staging=False,
        )
        _rebuild_derived_candidate(switched, switched_at)
        finalize_candidate_generation(switched)
        publish_candidate_generation(switched, published_at=switched_at)

        snapshot_path = self.data_dir / "snapshots" / "latest.json"
        snapshot = _read_json(snapshot_path)
        advanced = (_parse_time(snapshot["captured_at"]) + timedelta(days=2)).isoformat()
        snapshot["captured_at"] = advanced
        repositories = snapshot["repositories"]
        assert isinstance(repositories, list)
        for item in repositories:
            if isinstance(item, dict):
                item["captured_at"] = advanced
                if item.get("repo") == REPOSITORY:
                    item["pushed_at"] = (
                        _parse_time(item["pushed_at"]) + timedelta(days=2)
                    ).isoformat()
        _write_json(snapshot_path, snapshot)

        retried = self._resolve(apply=True)

        self.assertEqual(retried["status"], "applied")
        self.assertEqual(_read_json(record)["state"], "resolved")
        self.assertEqual(_read_json(record)["sourceVersions"], frozen_versions)
        self.assertEqual(
            resolve_current_generation(self.data_dir).generation_id,
            "resolver-retry-current-v3",
        )
        self.assertEqual(audit_current_data(self.data_dir)["status"], "healthy")

    def test_keep_detects_target_change_after_quarantine_and_restores_legacy(
        self,
    ) -> None:
        original = resolver_module._verify_post_quarantine_authority
        target = self.data_dir / "analysis" / f"{PROJECT_ID}.json"
        current_before = (self.data_dir / "current.json").read_bytes()
        generations_before = _tree_bytes(self.data_dir / "generations")
        competing = dict(self.stable_payload)
        counts = dict(competing["counts"])
        counts["test_files"] = int(counts["test_files"]) + 91
        competing["counts"] = counts
        competing_bytes: bytes | None = None

        def replace_after_quarantine(preflight: object) -> None:
            nonlocal competing_bytes
            self.assertFalse(self.legacy_path.exists())
            self.assertTrue(os.path.lexists(preflight.legacy_quarantine_path))  # type: ignore[attr-defined]
            competing_bytes = _write_json(target, competing)
            original(preflight)  # type: ignore[arg-type]

        with patch(
            "pipeline.resolve_project_artifact_conflict."
            "_verify_post_quarantine_authority",
            side_effect=replace_after_quarantine,
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "flat_stable_changed")
        self.assertEqual(self.legacy_path.read_bytes(), self.legacy_bytes)
        self.assertFalse(any(self.legacy_path.parent.glob("*.rardar-quarantine")))
        self.assertEqual(target.read_bytes(), competing_bytes)
        self.assertEqual((self.data_dir / "current.json").read_bytes(), current_before)
        self.assertEqual(_tree_bytes(self.data_dir / "generations"), generations_before)
        self.assertEqual(
            _read_json(next(self.archive_dir.rglob("resolution.json")))["state"],
            "prepared",
        )

    def test_promote_detects_target_change_after_quarantine_and_restores_legacy(
        self,
    ) -> None:
        self._write_legacy(timedelta(days=1))
        original = resolver_module._verify_post_quarantine_authority
        target = self.data_dir / "analysis" / f"{PROJECT_ID}.json"
        competing = dict(self.stable_payload)
        counts = dict(competing["counts"])
        counts["todo_markers"] = int(counts["todo_markers"]) + 92
        competing["counts"] = counts
        competing_bytes: bytes | None = None

        def replace_after_quarantine(preflight: object) -> None:
            nonlocal competing_bytes
            self.assertFalse(self.legacy_path.exists())
            self.assertTrue(target.exists())
            competing_bytes = _write_json(target, competing)
            original(preflight)  # type: ignore[arg-type]

        with patch(
            "pipeline.resolve_project_artifact_conflict."
            "_verify_post_quarantine_authority",
            side_effect=replace_after_quarantine,
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(PROMOTE_LEGACY, apply=True)

        self.assertEqual(raised.exception.code, "flat_stable_changed")
        self.assertEqual(self.legacy_path.read_bytes(), self.legacy_bytes)
        self.assertFalse(any(self.legacy_path.parent.glob("*.rardar-quarantine")))
        self.assertEqual(target.read_bytes(), competing_bytes)
        self.assertEqual(
            _read_json(next(self.archive_dir.rglob("resolution.json")))["state"],
            "prepared",
        )

    def test_quarantine_move_never_replaces_a_late_destination(self) -> None:
        original = resolver_module._move_to_quarantine_no_replace
        late_bytes = b"late quarantine entry must survive\n"
        quarantine_path: Path | None = None

        def create_late_destination(source: Path, quarantine: Path) -> None:
            nonlocal quarantine_path
            quarantine_path = quarantine
            quarantine.write_bytes(late_bytes)
            original(source, quarantine)

        with patch(
            "pipeline.resolve_project_artifact_conflict."
            "_move_to_quarantine_no_replace",
            side_effect=create_late_destination,
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "legacy_quarantine_conflict")
        self.assertEqual(self.legacy_path.read_bytes(), self.legacy_bytes)
        self.assertIsNotNone(quarantine_path)
        self.assertEqual(quarantine_path.read_bytes(), late_bytes)  # type: ignore[union-attr]
        self.assertEqual(
            _read_json(next(self.archive_dir.rglob("resolution.json")))["state"],
            "prepared",
        )

    def test_quarantine_replacement_before_detachment_is_preserved(self) -> None:
        original = resolver_module._detach_quarantined_legacy
        unknown = b"unknown replacement must not be deleted\n"
        held: Path | None = None

        def replace_before_detachment(
            preflight: object,
            expected_bytes: bytes,
            expected_identity: tuple[int, int, int, int, int, int],
        ) -> None:
            nonlocal held
            path = preflight.legacy_quarantine_path  # type: ignore[attr-defined]
            held = path.with_name(f"{path.name}.reviewed-held")
            os.replace(path, held)
            path.write_bytes(unknown)
            original(preflight, expected_bytes, expected_identity)  # type: ignore[arg-type]

        with patch(
            "pipeline.resolve_project_artifact_conflict._detach_quarantined_legacy",
            side_effect=replace_before_detachment,
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "legacy_quarantine_changed")
        self.assertFalse(self.legacy_path.exists())
        self.assertIsNotNone(held)
        self.assertEqual(held.read_bytes(), self.legacy_bytes)  # type: ignore[union-attr]
        quarantine = next(self.legacy_path.parent.glob("*.rardar-quarantine"))
        self.assertEqual(quarantine.read_bytes(), unknown)
        self.assertEqual(
            Path(str(next(self.archive_dir.rglob("legacy.json")))).read_bytes(),
            self.legacy_bytes,
        )
        self.assertEqual(
            _read_json(next(self.archive_dir.rglob("resolution.json")))["state"],
            "prepared",
        )

    def test_evidence_regular_file_swap_is_rejected_by_file_identity(self) -> None:
        evidence_root = self.root / "evidence-worktree"
        document = evidence_root / "docs" / "iterations" / "review.md"
        replacement = document.with_name("replacement.md")
        document.parent.mkdir(parents=True)
        evidence_bytes = b"# Review\n\nSanitized authority evidence.\n"
        document.write_bytes(evidence_bytes)
        replacement.write_bytes(evidence_bytes)
        original_open = Path.open
        swapped = False

        def swap_before_open(path: Path, *args: object, **kwargs: object) -> object:
            nonlocal swapped
            if not swapped and os.path.normcase(str(path)) == os.path.normcase(str(document)):
                swapped = True
                os.replace(replacement, document)
            return original_open(path, *args, **kwargs)

        before = _tree_bytes(self.data_dir)
        with patch.object(resolver_module, "REPOSITORY_ROOT", evidence_root), patch.object(
            Path,
            "open",
            new=swap_before_open,
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(evidence_reference="docs/iterations/review.md#review")

        self.assertEqual(raised.exception.code, "unsafe_evidence_reference")
        self.assertTrue(swapped)
        self.assertEqual(document.read_bytes(), evidence_bytes)
        self.assertEqual(_tree_bytes(self.data_dir), before)
        self.assertFalse(self.archive_dir.exists())

    def test_snapshot_regular_file_swap_is_rejected_by_file_identity(self) -> None:
        snapshot_path = self.data_dir / "snapshots" / "latest.json"
        replacement = self.root / "replacement-snapshot.json"
        snapshot_bytes = snapshot_path.read_bytes()
        # A second trailing newline keeps the JSON payload valid while making
        # the replacement identity observable even on filesystems that expose
        # weak or zero inode values to Python.
        replacement.write_bytes(snapshot_bytes + b"\n")
        original_open = Path.open
        swapped = False

        def swap_before_open(path: Path, *args: object, **kwargs: object) -> object:
            nonlocal swapped
            if not swapped and resolver_module._same_path(path, snapshot_path):
                swapped = True
                os.replace(replacement, snapshot_path)
            return original_open(path, *args, **kwargs)

        before = _tree_bytes(self.data_dir)
        with patch.object(Path, "open", new=swap_before_open):
            try:
                self._resolve()
            except ArtifactConflictResolutionError as error:
                raised = error
            else:
                self.fail(f"snapshot swap was not rejected; swapped={swapped}")

        self.assertEqual(raised.code, "unsafe_source_snapshot")
        self.assertTrue(swapped)
        snapshot_path.write_bytes(snapshot_bytes)
        self.assertEqual(_tree_bytes(self.data_dir), before)
        self.assertFalse(self.archive_dir.exists())

    def test_evidence_change_after_quarantine_restores_reviewed_legacy(self) -> None:
        evidence_root = self.root / "evidence-worktree"
        document = evidence_root / "docs" / "iterations" / "review.md"
        document.parent.mkdir(parents=True)
        document.write_text("# Review\n\nInitial evidence.\n", encoding="utf-8")
        original = resolver_module._verify_post_quarantine_authority

        def rewrite_evidence(preflight: object) -> None:
            document.write_text("# Review\n\nChanged evidence.\n", encoding="utf-8")
            original(preflight)  # type: ignore[arg-type]

        with patch.object(resolver_module, "REPOSITORY_ROOT", evidence_root), patch(
            "pipeline.resolve_project_artifact_conflict."
            "_verify_post_quarantine_authority",
            side_effect=rewrite_evidence,
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(
                    apply=True,
                    evidence_reference="docs/iterations/review.md#review",
                )

        self.assertEqual(raised.exception.code, "evidence_reference_changed")
        self.assertEqual(self.legacy_path.read_bytes(), self.legacy_bytes)
        self.assertFalse(any(self.legacy_path.parent.glob("*.rardar-quarantine")))
        self.assertEqual(
            _read_json(next(self.archive_dir.rglob("resolution.json")))["state"],
            "prepared",
        )

    def test_source_snapshot_change_after_quarantine_restores_reviewed_legacy(
        self,
    ) -> None:
        original = resolver_module._verify_post_quarantine_authority
        snapshot_path = self.data_dir / "snapshots" / "latest.json"

        def advance_snapshot(preflight: object) -> None:
            snapshot = _read_json(snapshot_path)
            advanced = (
                _parse_time(snapshot["captured_at"]) + timedelta(seconds=1)
            ).isoformat()
            snapshot["captured_at"] = advanced
            repositories = snapshot["repositories"]
            assert isinstance(repositories, list)
            for item in repositories:
                if isinstance(item, dict):
                    item["captured_at"] = advanced
            _write_json(snapshot_path, snapshot)
            original(preflight)  # type: ignore[arg-type]

        with patch(
            "pipeline.resolve_project_artifact_conflict."
            "_verify_post_quarantine_authority",
            side_effect=advance_snapshot,
        ):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "source_version_changed")
        self.assertEqual(self.legacy_path.read_bytes(), self.legacy_bytes)
        self.assertFalse(any(self.legacy_path.parent.glob("*.rardar-quarantine")))
        self.assertEqual(
            _read_json(next(self.archive_dir.rglob("resolution.json")))["state"],
            "prepared",
        )

    def test_resolved_decision_requires_retained_detached_archive(self) -> None:
        report = self._resolve(apply=True)
        detached = Path(str(report["detachedArtifact"]))
        record = Path(str(report["auditRecord"]))
        record_before = record.read_bytes()
        detached.unlink()

        with self.assertRaises(ArtifactConflictResolutionError) as raised:
            self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "detached_archive_missing")
        self.assertEqual(record.read_bytes(), record_before)
        self.assertFalse(self.legacy_path.exists())

    def test_prepared_audit_record_late_swap_is_rejected_by_identity(self) -> None:
        with patch(
            "pipeline.resolve_project_artifact_conflict._remove_active_legacy",
            side_effect=OSError("simulated cleanup interruption"),
        ):
            with self.assertRaises(ArtifactConflictResolutionError):
                self._resolve(apply=True)
        record = next(self.archive_dir.rglob("resolution.json"))
        replacement = self.root / "replacement-resolution.json"
        replacement.write_bytes(record.read_bytes() + b"\n")
        original_open = Path.open
        swapped = False

        def swap_before_open(path: Path, *args: object, **kwargs: object) -> object:
            nonlocal swapped
            if not swapped and resolver_module._same_path(path, record):
                swapped = True
                os.replace(replacement, record)
            return original_open(path, *args, **kwargs)

        source_before = self.legacy_path.read_bytes()
        with patch.object(Path, "open", new=swap_before_open):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "unsafe_archive_path")
        self.assertTrue(swapped)
        self.assertEqual(self.legacy_path.read_bytes(), source_before)
        self.assertEqual(_read_json(record)["state"], "prepared")

    def test_archived_legacy_late_swap_is_rejected_by_identity(self) -> None:
        with patch(
            "pipeline.resolve_project_artifact_conflict._mark_resolved",
            side_effect=RuntimeError("simulated final audit interruption"),
        ):
            with self.assertRaises(RuntimeError):
                self._resolve(apply=True)
        archived = next(self.archive_dir.rglob("legacy.json"))
        replacement = self.root / "replacement-legacy.json"
        replacement.write_bytes(archived.read_bytes() + b"\n")
        original_open = Path.open
        swapped = False

        def swap_before_open(path: Path, *args: object, **kwargs: object) -> object:
            nonlocal swapped
            if not swapped and resolver_module._same_path(path, archived):
                swapped = True
                os.replace(replacement, archived)
            return original_open(path, *args, **kwargs)

        current_before = (self.data_dir / "current.json").read_bytes()
        with patch.object(Path, "open", new=swap_before_open):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(apply=True)

        self.assertEqual(raised.exception.code, "unsafe_archive_path")
        self.assertTrue(swapped)
        self.assertEqual((self.data_dir / "current.json").read_bytes(), current_before)
        self.assertEqual(
            _read_json(next(self.archive_dir.rglob("resolution.json")))["state"],
            "prepared",
        )

    def test_prepared_source_version_record_is_independently_revalidated(self) -> None:
        with patch(
            "pipeline.resolve_project_artifact_conflict._remove_active_legacy",
            side_effect=OSError("simulated cleanup interruption"),
        ):
            with self.assertRaises(ArtifactConflictResolutionError):
                self._resolve(apply=True)
        record = next(self.archive_dir.rglob("resolution.json"))
        original = record.read_bytes()
        payload = _read_json(record)
        versions = payload["sourceVersions"]
        assert isinstance(versions, dict)
        legacy = versions["legacy"]
        assert isinstance(legacy, dict)
        legacy["snapshotCapturedAt"] = (
            _parse_time(legacy["analyzedAt"]) + timedelta(seconds=1)
        ).isoformat()
        _write_json(record, payload)

        with self.assertRaises(ArtifactConflictResolutionError) as legacy_error:
            self._resolve(apply=True)
        self.assertEqual(legacy_error.exception.code, "audit_record_conflict")

        record.write_bytes(original)
        payload = _read_json(record)
        versions = payload["sourceVersions"]
        assert isinstance(versions, dict)
        stable = versions["stable"]
        assert isinstance(stable, dict)
        stable["snapshotCapturedAt"] = (
            _parse_time(stable["snapshotCapturedAt"]) + timedelta(seconds=1)
        ).isoformat()
        _write_json(record, payload)
        with self.assertRaises(ArtifactConflictResolutionError) as stable_error:
            self._resolve(apply=True)
        self.assertEqual(stable_error.exception.code, "audit_record_conflict")
        self.assertTrue(self.legacy_path.exists())

    def test_blocked_with_explicit_versions_allows_missing_snapshot(self) -> None:
        (self.data_dir / "snapshots" / "latest.json").unlink()
        before = _tree_bytes(self.data_dir)

        report = self._resolve(BLOCKED, apply=True)

        self.assertEqual(report["status"], "blocked")
        self.assertIsNone(report["sourceVersions"])
        self.assertEqual(_tree_bytes(self.data_dir), before)
        self.assertFalse(self.archive_dir.exists())

    def test_evidence_leaf_symlink_is_rejected(self) -> None:
        evidence_root = self.root / "evidence-symlink-worktree"
        document = evidence_root / "docs" / "iterations" / "review.md"
        target = evidence_root / "outside-review.md"
        document.parent.mkdir(parents=True)
        target.write_text("# Review\n\nExternal evidence.\n", encoding="utf-8")
        try:
            document.symlink_to(target)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"file symlink creation is unavailable: {error}")

        with patch.object(resolver_module, "REPOSITORY_ROOT", evidence_root):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                self._resolve(evidence_reference="docs/iterations/review.md#review")

        self.assertEqual(raised.exception.code, "unsafe_evidence_reference")
        self.assertFalse(self.archive_dir.exists())

    def test_snapshot_leaf_symlink_is_rejected_even_when_target_is_valid(self) -> None:
        snapshot_path = self.data_dir / "snapshots" / "latest.json"
        target = self.root / "external-valid-snapshot.json"
        target.write_bytes(snapshot_path.read_bytes())
        target_before = target.read_bytes()
        snapshot_path.unlink()
        try:
            snapshot_path.symlink_to(target)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"file symlink creation is unavailable: {error}")

        with self.assertRaises(ArtifactConflictResolutionError) as raised:
            self._resolve()

        self.assertEqual(raised.exception.code, "unsafe_source_snapshot")
        self.assertEqual(target.read_bytes(), target_before)
        self.assertFalse(self.archive_dir.exists())

    def test_safe_reader_detects_same_length_in_place_mutation(self) -> None:
        path = self.root / "same-length-evidence.md"
        path.write_bytes(b"AAAA")
        original_fstat = os.fstat
        mutated = False

        def mutate_after_first_fstat(fd: int) -> os.stat_result:
            nonlocal mutated
            metadata = original_fstat(fd)
            if not mutated:
                mutated = True
                path.write_bytes(b"BBBB")
            return metadata

        with patch("os.fstat", side_effect=mutate_after_first_fstat):
            with self.assertRaises(ArtifactConflictResolutionError) as raised:
                resolver_module._read_safe_regular_bytes(
                    path,
                    path.parent,
                    code="unsafe_evidence_reference",
                    label="test evidence",
                )

        self.assertEqual(raised.exception.code, "unsafe_evidence_reference")
        self.assertTrue(mutated)

    def test_cli_unexpected_failure_is_structured_json_without_traceback(self) -> None:
        stderr = io.StringIO()
        arguments = [
            "--data-dir",
            str(self.data_dir),
            "--repository",
            REPOSITORY,
            "--kind",
            "analysis",
            "--decision",
            KEEP_STABLE,
            "--expected-legacy-sha256",
            self.legacy_sha,
            "--expected-stable-sha256",
            self.stable_sha,
            "--evidence-reference",
            EVIDENCE_REFERENCE,
        ]
        with patch(
            "pipeline.resolve_project_artifact_conflict.resolve_project_artifact_conflict",
            side_effect=RuntimeError("secret text must not escape"),
        ), redirect_stderr(stderr):
            exit_code = main(arguments)

        payload = json.loads(stderr.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["errorCode"], "unexpected_resolution_failure")
        self.assertNotIn("secret text", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
