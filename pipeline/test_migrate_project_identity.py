from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.migrate_project_identity import (
    ProjectIdentityMigrationError,
    migrate_project_identity,
)
from pipeline.project_identity import (
    PROJECT_ID_VERSION,
    legacy_slug_for_repository,
    project_id_for_repository,
)


def static_evidence(repository: str, *, version: int = 1) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": version,
        "repository": repository,
        "source": f"https://github.com/{repository}",
        "scanned_files": 4,
        "language_files": {"Python": 2},
        "indicators": {
            "readme": True,
            "license": True,
            "tests": True,
            "ci": False,
            "docker": False,
            "dependency_lock": False,
            "package_manifest": True,
            "examples": False,
            "docs": True,
            "environment_example": False,
        },
        "counts": {"test_files": 1, "todo_markers": 0},
        "license_hint": "MIT",
        "confidence": 75,
        "warnings": ["static inspection only; code was not executed"],
    }
    if version == 1:
        payload["analyzed_at"] = "2026-07-15T00:00:00Z"
    return payload


def project_enrichment(repository: str, *, version: int = 1) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": version,
        "repository": repository,
        "analyzedAt": "2026-07-15T01:00:00Z",
        "titleZh": "测试项目",
        "summaryZh": "用于验证稳定项目身份迁移。",
        "category": "开发工具",
        "capabilities": ["身份迁移"],
        "taskTerms": ["stable-id"],
        "bestFor": "需要稳定项目身份的目录",
        "reusePlan": "先验证证据，再隔离试用。",
        "limitation": "没有执行第三方代码。",
        "evidenceSummary": "来自受控迁移测试。",
        "sourceUrl": f"https://github.com/{repository}#readme",
    }
    if version == 1:
        payload.update(
            {
                "sourcePushedAt": "2026-07-14T23:00:00Z",
                "sourceAnalysisAt": "2026-07-15T00:00:00Z",
            }
        )
    return payload


def migrated(payload: dict[str, object]) -> dict[str, object]:
    repository = str(payload["repository"])
    return {
        **payload,
        "schemaVersion": 2,
        "projectIdVersion": PROJECT_ID_VERSION,
        "projectId": project_id_for_repository(repository),
    }


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def legacy_path(data_dir: Path, directory: str, repository: str) -> Path:
    return data_dir / directory / f"{legacy_slug_for_repository(repository)}.json"


def stable_path(data_dir: Path, directory: str, repository: str) -> Path:
    return data_dir / directory / f"{project_id_for_repository(repository)}.json"


class ProjectIdentityMigrationTests(unittest.TestCase):
    def test_default_dry_run_never_touches_sources_current_or_generations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            repository = "Owner/foo.bar"
            analysis_source = legacy_path(data_dir, "analysis", repository)
            enrichment_source = legacy_path(data_dir, "enrichment", repository)
            write_json(analysis_source, static_evidence(repository))
            write_json(enrichment_source, project_enrichment(repository))
            current = data_dir / "current.json"
            generation = data_dir / "generations/retained/analysis/sentinel.json"
            write_json(current, {"sentinel": "current"})
            write_json(generation, {"sentinel": "retained"})
            before_current = current.read_bytes()
            before_generation = generation.read_bytes()

            report = migrate_project_identity(data_dir)

            self.assertEqual(report["status"], "dry-run")
            self.assertEqual(report["migrationCount"], 2)
            self.assertTrue(analysis_source.exists())
            self.assertTrue(enrichment_source.exists())
            self.assertFalse(stable_path(data_dir, "analysis", repository).exists())
            self.assertFalse(stable_path(data_dir, "enrichment", repository).exists())
            self.assertEqual(current.read_bytes(), before_current)
            self.assertEqual(generation.read_bytes(), before_generation)

    def test_apply_migrates_v1_payloads_and_second_apply_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            repository = "Owner/foo.bar"
            analysis_payload = static_evidence(repository)
            enrichment_payload = project_enrichment(repository)
            analysis_source = legacy_path(data_dir, "analysis", repository)
            enrichment_source = legacy_path(data_dir, "enrichment", repository)
            write_json(analysis_source, analysis_payload)
            write_json(enrichment_source, enrichment_payload)

            first = migrate_project_identity(data_dir, apply=True)

            analysis_target = stable_path(data_dir, "analysis", repository)
            enrichment_target = stable_path(data_dir, "enrichment", repository)
            self.assertEqual(first["status"], "applied")
            self.assertEqual(first["migrationCount"], 2)
            self.assertFalse(analysis_source.exists())
            self.assertFalse(enrichment_source.exists())
            self.assertEqual(
                json.loads(analysis_target.read_text(encoding="utf-8")),
                migrated(analysis_payload),
            )
            self.assertEqual(
                json.loads(enrichment_target.read_text(encoding="utf-8")),
                migrated(enrichment_payload),
            )
            before = (analysis_target.read_bytes(), enrichment_target.read_bytes())

            second = migrate_project_identity(data_dir, apply=True)

            self.assertEqual(second["migrationCount"], 0)
            self.assertEqual(second["equivalentTargetCount"], 0)
            self.assertEqual(second["alreadyCurrentCount"], 2)
            self.assertEqual(
                (analysis_target.read_bytes(), enrichment_target.read_bytes()),
                before,
            )

    def test_v0_artifacts_remain_explicit_unmodified_legacy_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            repository = "owner/legacy"
            analysis_source = legacy_path(data_dir, "analysis", repository)
            enrichment_source = legacy_path(data_dir, "enrichment", repository)
            write_json(analysis_source, static_evidence(repository, version=0))
            write_json(enrichment_source, project_enrichment(repository, version=0))
            before = (analysis_source.read_bytes(), enrichment_source.read_bytes())

            report = migrate_project_identity(data_dir, apply=True)

            self.assertEqual(report["legacyUnmigratedCount"], 2)
            self.assertEqual(report["migrationCount"], 0)
            self.assertEqual(
                {item["status"] for item in report["items"]},
                {"legacy_v0_unmigrated"},
            )
            self.assertEqual(
                (analysis_source.read_bytes(), enrichment_source.read_bytes()),
                before,
            )
            self.assertFalse(stable_path(data_dir, "analysis", repository).exists())
            self.assertFalse(stable_path(data_dir, "enrichment", repository).exists())

    def test_unresolved_legacy_collision_is_fatal_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            dotted = "owner/foo.bar"
            dashed = "owner/foo-bar"
            self.assertEqual(
                legacy_slug_for_repository(dotted),
                legacy_slug_for_repository(dashed),
            )
            dotted_source = legacy_path(data_dir, "analysis", dotted)
            dashed_source = legacy_path(data_dir, "enrichment", dashed)
            write_json(dotted_source, static_evidence(dotted))
            write_json(dashed_source, project_enrichment(dashed))
            before = (dotted_source.read_bytes(), dashed_source.read_bytes())

            with patch(
                "pipeline.migrate_project_identity.atomic_write_validated_json"
            ) as writer:
                with self.assertRaises(ProjectIdentityMigrationError) as raised:
                    migrate_project_identity(data_dir, apply=True)

            self.assertEqual(raised.exception.code, "unresolved_legacy_collision")
            writer.assert_not_called()
            self.assertEqual(
                (dotted_source.read_bytes(), dashed_source.read_bytes()),
                before,
            )
            self.assertFalse(stable_path(data_dir, "analysis", dotted).exists())
            self.assertFalse(stable_path(data_dir, "enrichment", dashed).exists())

    def test_conflicting_target_fails_full_preflight_with_zero_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            first_repository = "owner/first"
            conflict_repository = "owner/conflict"
            first_source = legacy_path(data_dir, "analysis", first_repository)
            conflict_source = legacy_path(data_dir, "enrichment", conflict_repository)
            first_payload = static_evidence(first_repository)
            conflict_payload = project_enrichment(conflict_repository)
            write_json(first_source, first_payload)
            write_json(conflict_source, conflict_payload)
            conflict_target = stable_path(data_dir, "enrichment", conflict_repository)
            conflicting_payload = migrated(conflict_payload)
            conflicting_payload["titleZh"] = "已有的不同内容"
            write_json(conflict_target, conflicting_payload)
            before = {
                path: path.read_bytes()
                for path in (first_source, conflict_source, conflict_target)
            }

            with patch(
                "pipeline.migrate_project_identity.atomic_write_validated_json"
            ) as writer:
                with self.assertRaises(ProjectIdentityMigrationError) as raised:
                    migrate_project_identity(data_dir, apply=True)

            self.assertEqual(raised.exception.code, "target_conflict")
            writer.assert_not_called()
            self.assertFalse(stable_path(data_dir, "analysis", first_repository).exists())
            self.assertEqual(
                {path: path.read_bytes() for path in before},
                before,
            )

    def test_equivalent_target_is_not_rewritten_and_legacy_source_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            repository = "owner/equivalent"
            payload = static_evidence(repository)
            source = legacy_path(data_dir, "analysis", repository)
            target = stable_path(data_dir, "analysis", repository)
            write_json(source, payload)
            write_json(target, migrated(payload))
            target_before = target.read_bytes()

            with patch(
                "pipeline.migrate_project_identity.atomic_write_validated_json"
            ) as writer:
                report = migrate_project_identity(data_dir, apply=True)

            writer.assert_not_called()
            self.assertEqual(report["equivalentTargetCount"], 1)
            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), target_before)

    def test_interrupted_cleanup_is_safely_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            repository = "owner/retry"
            analysis_source = legacy_path(data_dir, "analysis", repository)
            enrichment_source = legacy_path(data_dir, "enrichment", repository)
            write_json(analysis_source, static_evidence(repository))
            write_json(enrichment_source, project_enrichment(repository))

            with patch(
                "pipeline.migrate_project_identity._remove_source",
                side_effect=OSError("simulated interruption"),
            ):
                with self.assertRaises(ProjectIdentityMigrationError) as interrupted:
                    migrate_project_identity(data_dir, apply=True)

            self.assertEqual(interrupted.exception.code, "source_cleanup_failed")

            # All atomic targets are durable before source cleanup begins.
            self.assertTrue(stable_path(data_dir, "analysis", repository).exists())
            self.assertTrue(stable_path(data_dir, "enrichment", repository).exists())
            self.assertTrue(analysis_source.exists())
            self.assertTrue(enrichment_source.exists())

            retry = migrate_project_identity(data_dir, apply=True)

            self.assertEqual(retry["migrationCount"], 0)
            self.assertEqual(retry["equivalentTargetCount"], 2)
            self.assertFalse(analysis_source.exists())
            self.assertFalse(enrichment_source.exists())
            final = migrate_project_identity(data_dir, apply=True)
            self.assertEqual(final["alreadyCurrentCount"], 2)

    def test_staging_symlink_is_rejected_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            data_dir.mkdir()
            external = root / "external-analysis"
            external.mkdir()
            sentinel = external / "sentinel.json"
            sentinel.write_text('{"outside": true}\n', encoding="utf-8")
            linked = data_dir / "analysis"
            try:
                linked.symlink_to(external, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")
            before = sentinel.read_bytes()

            with self.assertRaises(ProjectIdentityMigrationError) as raised:
                migrate_project_identity(data_dir, apply=True)

            self.assertEqual(raised.exception.code, "unsafe_staging_directory")
            self.assertEqual(sentinel.read_bytes(), before)

    def test_retained_generation_root_is_never_a_valid_migration_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            generation = Path(temporary) / "data/generations/generation-1"
            repository = "owner/retained"
            write_json(generation / "manifest.json", {"state": "ready"})
            source = legacy_path(generation, "analysis", repository)
            write_json(source, static_evidence(repository))
            before = source.read_bytes()

            with self.assertRaises(ProjectIdentityMigrationError) as raised:
                migrate_project_identity(generation, apply=True)

            self.assertEqual(raised.exception.code, "protected_generation")
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(stable_path(generation, "analysis", repository).exists())

    def test_nested_directory_inside_retained_generation_is_never_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            nested = (
                Path(temporary)
                / "data/generations/generation-1/nested/staging-copy"
            )
            repository = "owner/retained-nested"
            source = legacy_path(nested, "analysis", repository)
            write_json(source, static_evidence(repository))
            before = source.read_bytes()

            with self.assertRaises(ProjectIdentityMigrationError) as raised:
                migrate_project_identity(nested, apply=True)

            self.assertEqual(raised.exception.code, "protected_generation")
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(stable_path(nested, "analysis", repository).exists())

    def test_actual_project_id_collision_fails_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            first = "owner/one"
            second = "another/two"
            first_path = legacy_path(data_dir, "analysis", first)
            second_path = legacy_path(data_dir, "analysis", second)
            write_json(first_path, static_evidence(first))
            write_json(second_path, static_evidence(second))
            before = {
                first_path: first_path.read_bytes(),
                second_path: second_path.read_bytes(),
            }
            forced_id = project_id_for_repository(first)

            with patch(
                "pipeline.migrate_project_identity.project_id_for_repository",
                return_value=forced_id,
            ):
                with self.assertRaises(ProjectIdentityMigrationError) as raised:
                    migrate_project_identity(data_dir, apply=True)

            self.assertEqual(raised.exception.code, "project_id_collision")
            self.assertEqual(first_path.read_bytes(), before[first_path])
            self.assertEqual(second_path.read_bytes(), before[second_path])


if __name__ == "__main__":
    unittest.main()
