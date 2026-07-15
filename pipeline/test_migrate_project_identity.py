from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.migrate_project_identity import (
    ProjectIdentityMigrationError,
    main,
    migrate_project_identity,
)
from pipeline.project_identity import (
    PROJECT_ID_VERSION,
    legacy_slug_for_repository,
    project_id_for_repository,
)
from pipeline.schema_validation import ArtifactKind, load_validated_json


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


def downgraded(payload: dict[str, object]) -> dict[str, object]:
    result = dict(payload)
    result["schemaVersion"] = 1
    result.pop("projectIdVersion", None)
    result.pop("projectId", None)
    return result


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def legacy_path(data_dir: Path, directory: str, repository: str) -> Path:
    return data_dir / directory / f"{legacy_slug_for_repository(repository)}.json"


def stable_path(data_dir: Path, directory: str, repository: str) -> Path:
    return data_dir / directory / f"{project_id_for_repository(repository)}.json"


def tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


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

    def test_to_legacy_v1_defaults_to_dry_run_and_cli_requires_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            repository = "Owner/reverse-dry-run"
            analysis_source = stable_path(data_dir, "analysis", repository)
            enrichment_source = stable_path(data_dir, "enrichment", repository)
            write_json(analysis_source, migrated(static_evidence(repository)))
            write_json(enrichment_source, migrated(project_enrichment(repository)))
            before = {
                analysis_source: analysis_source.read_bytes(),
                enrichment_source: enrichment_source.read_bytes(),
            }

            report = migrate_project_identity(data_dir, to_legacy_v1=True)

            self.assertEqual(report["status"], "dry-run")
            self.assertEqual(report["direction"], "to-legacy-v1")
            self.assertEqual(report["migrationCount"], 2)
            self.assertEqual(
                {item["status"] for item in report["items"]},
                {"would_downgrade"},
            )
            self.assertEqual(
                {path: path.read_bytes() for path in before},
                before,
            )
            self.assertFalse(legacy_path(data_dir, "analysis", repository).exists())
            self.assertFalse(legacy_path(data_dir, "enrichment", repository).exists())

            with patch("builtins.print"):
                exit_code = main(
                    ["--data-dir", str(data_dir), "--to-legacy-v1"]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                {path: path.read_bytes() for path in before},
                before,
            )

            with patch("builtins.print"):
                apply_exit_code = main(
                    [
                        "--data-dir",
                        str(data_dir),
                        "--to-legacy-v1",
                        "--apply",
                    ]
                )
            self.assertEqual(apply_exit_code, 0)
            self.assertFalse(analysis_source.exists())
            self.assertFalse(enrichment_source.exists())
            self.assertTrue(legacy_path(data_dir, "analysis", repository).exists())
            self.assertTrue(legacy_path(data_dir, "enrichment", repository).exists())

    def test_to_legacy_v1_apply_is_lossless_schema_valid_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            repository = "Owner/reverse-apply"
            analysis_payload = migrated(static_evidence(repository))
            enrichment_payload = migrated(project_enrichment(repository))
            analysis_source = stable_path(data_dir, "analysis", repository)
            enrichment_source = stable_path(data_dir, "enrichment", repository)
            write_json(analysis_source, analysis_payload)
            write_json(enrichment_source, enrichment_payload)

            current = data_dir / "current.json"
            generations = data_dir / "generations"
            write_json(current, {"sentinel": "current"})
            write_json(
                generations / "retained/manifest.json",
                {"sentinel": "retained manifest"},
            )
            write_json(
                generations / ".candidates/candidate/manifest.json",
                {"sentinel": "candidate manifest"},
            )
            current_before = current.read_bytes()
            generations_before = tree_bytes(generations)

            first = migrate_project_identity(
                data_dir,
                apply=True,
                to_legacy_v1=True,
            )

            analysis_target = legacy_path(data_dir, "analysis", repository)
            enrichment_target = legacy_path(data_dir, "enrichment", repository)
            self.assertEqual(first["status"], "applied")
            self.assertEqual(first["direction"], "to-legacy-v1")
            self.assertEqual(first["migrationCount"], 2)
            self.assertFalse(analysis_source.exists())
            self.assertFalse(enrichment_source.exists())
            self.assertEqual(
                json.loads(analysis_target.read_text(encoding="utf-8")),
                downgraded(analysis_payload),
            )
            self.assertEqual(
                json.loads(enrichment_target.read_text(encoding="utf-8")),
                downgraded(enrichment_payload),
            )
            self.assertEqual(
                load_validated_json(analysis_target, ArtifactKind.STATIC_EVIDENCE)[
                    "schemaVersion"
                ],
                1,
            )
            self.assertEqual(
                load_validated_json(
                    enrichment_target,
                    ArtifactKind.PROJECT_ENRICHMENT,
                )["schemaVersion"],
                1,
            )
            self.assertEqual(current.read_bytes(), current_before)
            self.assertEqual(tree_bytes(generations), generations_before)
            target_bytes = (analysis_target.read_bytes(), enrichment_target.read_bytes())

            with patch(
                "pipeline.migrate_project_identity.atomic_write_validated_json"
            ) as writer, patch(
                "pipeline.migrate_project_identity._remove_source"
            ) as remover:
                second = migrate_project_identity(
                    data_dir,
                    apply=True,
                    to_legacy_v1=True,
                )

            writer.assert_not_called()
            remover.assert_not_called()
            self.assertEqual(second["migrationCount"], 0)
            self.assertEqual(second["equivalentTargetCount"], 0)
            self.assertEqual(second["alreadyCurrentCount"], 2)
            self.assertEqual(
                (analysis_target.read_bytes(), enrichment_target.read_bytes()),
                target_bytes,
            )
            self.assertEqual(current.read_bytes(), current_before)
            self.assertEqual(tree_bytes(generations), generations_before)

    def test_to_legacy_v1_collision_fails_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            dotted = "owner/foo.bar"
            dashed = "owner/foo-bar"
            self.assertEqual(
                legacy_slug_for_repository(dotted),
                legacy_slug_for_repository(dashed),
            )
            dotted_source = stable_path(data_dir, "analysis", dotted)
            dashed_source = stable_path(data_dir, "enrichment", dashed)
            write_json(dotted_source, migrated(static_evidence(dotted)))
            write_json(dashed_source, migrated(project_enrichment(dashed)))
            before = {
                dotted_source: dotted_source.read_bytes(),
                dashed_source: dashed_source.read_bytes(),
            }

            with patch(
                "pipeline.migrate_project_identity.atomic_write_validated_json"
            ) as writer, patch(
                "pipeline.migrate_project_identity._remove_source"
            ) as remover:
                with self.assertRaises(ProjectIdentityMigrationError) as raised:
                    migrate_project_identity(
                        data_dir,
                        apply=True,
                        to_legacy_v1=True,
                    )

            self.assertEqual(raised.exception.code, "unresolved_legacy_collision")
            writer.assert_not_called()
            remover.assert_not_called()
            self.assertEqual(
                {path: path.read_bytes() for path in before},
                before,
            )
            self.assertFalse(legacy_path(data_dir, "analysis", dotted).exists())
            self.assertFalse(legacy_path(data_dir, "enrichment", dashed).exists())

    def test_to_legacy_v1_non_equivalent_target_aborts_all_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            safe_repository = "owner/safe"
            conflict_repository = "owner/conflict"
            safe_source = stable_path(data_dir, "analysis", safe_repository)
            conflict_source = stable_path(
                data_dir,
                "enrichment",
                conflict_repository,
            )
            conflict_target = legacy_path(
                data_dir,
                "enrichment",
                conflict_repository,
            )
            write_json(safe_source, migrated(static_evidence(safe_repository)))
            conflict_payload = migrated(project_enrichment(conflict_repository))
            write_json(conflict_source, conflict_payload)
            conflicting_legacy = downgraded(conflict_payload)
            conflicting_legacy["titleZh"] = "different valid content"
            write_json(conflict_target, conflicting_legacy)
            before = {
                path: path.read_bytes()
                for path in (safe_source, conflict_source, conflict_target)
            }

            with patch(
                "pipeline.migrate_project_identity.atomic_write_validated_json"
            ) as writer, patch(
                "pipeline.migrate_project_identity._remove_source"
            ) as remover:
                with self.assertRaises(ProjectIdentityMigrationError) as raised:
                    migrate_project_identity(
                        data_dir,
                        apply=True,
                        to_legacy_v1=True,
                    )

            self.assertEqual(raised.exception.code, "target_conflict")
            writer.assert_not_called()
            remover.assert_not_called()
            self.assertEqual(
                {path: path.read_bytes() for path in before},
                before,
            )
            self.assertFalse(legacy_path(data_dir, "analysis", safe_repository).exists())

    def test_to_legacy_v1_enrichment_source_time_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            repository = "owner/time-conflict"
            stable_payload = migrated(project_enrichment(repository))
            source = stable_path(data_dir, "enrichment", repository)
            target = legacy_path(data_dir, "enrichment", repository)
            write_json(source, stable_payload)
            conflicting_target = downgraded(stable_payload)
            conflicting_target["sourceAnalysisAt"] = "2026-07-14T23:59:59Z"
            write_json(target, conflicting_target)
            before = (source.read_bytes(), target.read_bytes())

            with self.assertRaises(ProjectIdentityMigrationError) as raised:
                migrate_project_identity(
                    data_dir,
                    apply=True,
                    to_legacy_v1=True,
                )

            self.assertEqual(raised.exception.code, "target_conflict")
            self.assertEqual((source.read_bytes(), target.read_bytes()), before)

    def test_to_legacy_v1_equivalent_target_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            repository = "owner/equivalent-reverse"
            stable_payload = migrated(static_evidence(repository))
            source = stable_path(data_dir, "analysis", repository)
            target = legacy_path(data_dir, "analysis", repository)
            write_json(source, stable_payload)
            write_json(target, downgraded(stable_payload))
            target_before = target.read_bytes()

            with patch(
                "pipeline.migrate_project_identity.atomic_write_validated_json"
            ) as writer:
                report = migrate_project_identity(
                    data_dir,
                    apply=True,
                    to_legacy_v1=True,
                )

            writer.assert_not_called()
            self.assertEqual(report["equivalentTargetCount"], 1)
            self.assertFalse(source.exists())
            self.assertEqual(target.read_bytes(), target_before)

    def test_to_legacy_v1_write_interruption_keeps_sources_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            repository = "owner/write-retry"
            analysis_source = stable_path(data_dir, "analysis", repository)
            enrichment_source = stable_path(data_dir, "enrichment", repository)
            write_json(analysis_source, migrated(static_evidence(repository)))
            write_json(enrichment_source, migrated(project_enrichment(repository)))

            from pipeline import migrate_project_identity as migration_module

            real_writer = migration_module.atomic_write_validated_json
            call_count = 0

            def interrupted_writer(*args: object, **kwargs: object) -> object:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise OSError("simulated write interruption")
                return real_writer(*args, **kwargs)

            with patch(
                "pipeline.migrate_project_identity.atomic_write_validated_json",
                side_effect=interrupted_writer,
            ):
                with self.assertRaises(ProjectIdentityMigrationError) as interrupted:
                    migrate_project_identity(
                        data_dir,
                        apply=True,
                        to_legacy_v1=True,
                    )

            self.assertEqual(interrupted.exception.code, "target_write_failed")
            self.assertTrue(analysis_source.exists())
            self.assertTrue(enrichment_source.exists())
            self.assertTrue(legacy_path(data_dir, "analysis", repository).exists())
            self.assertFalse(legacy_path(data_dir, "enrichment", repository).exists())

            retry = migrate_project_identity(
                data_dir,
                apply=True,
                to_legacy_v1=True,
            )

            self.assertEqual(retry["equivalentTargetCount"], 1)
            self.assertFalse(analysis_source.exists())
            self.assertFalse(enrichment_source.exists())

    def test_to_legacy_v1_cleanup_interruption_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            repository = "owner/cleanup-retry"
            analysis_source = stable_path(data_dir, "analysis", repository)
            enrichment_source = stable_path(data_dir, "enrichment", repository)
            write_json(analysis_source, migrated(static_evidence(repository)))
            write_json(enrichment_source, migrated(project_enrichment(repository)))

            from pipeline import migrate_project_identity as migration_module

            real_remove = migration_module._remove_source
            call_count = 0

            def interrupted_remove(action: object) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise OSError("simulated cleanup interruption")
                real_remove(action)

            with patch(
                "pipeline.migrate_project_identity._remove_source",
                side_effect=interrupted_remove,
            ):
                with self.assertRaises(ProjectIdentityMigrationError) as interrupted:
                    migrate_project_identity(
                        data_dir,
                        apply=True,
                        to_legacy_v1=True,
                    )

            self.assertEqual(interrupted.exception.code, "source_cleanup_failed")
            self.assertFalse(analysis_source.exists())
            self.assertTrue(enrichment_source.exists())
            self.assertTrue(legacy_path(data_dir, "analysis", repository).exists())
            self.assertTrue(legacy_path(data_dir, "enrichment", repository).exists())

            migrate_project_identity(
                data_dir,
                apply=True,
                to_legacy_v1=True,
            )
            self.assertFalse(enrichment_source.exists())
            final = migrate_project_identity(
                data_dir,
                apply=True,
                to_legacy_v1=True,
            )
            self.assertEqual(final["migrationCount"], 0)
            self.assertEqual(final["equivalentTargetCount"], 0)

    def test_to_legacy_v1_rejects_linked_source_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            repository = "owner/linked-reverse"
            external = root / "external.json"
            write_json(external, migrated(static_evidence(repository)))
            source = stable_path(data_dir, "analysis", repository)
            source.parent.mkdir(parents=True)
            try:
                source.symlink_to(external)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"file symlinks are unavailable: {error}")
            before = external.read_bytes()

            with self.assertRaises(ProjectIdentityMigrationError) as raised:
                migrate_project_identity(
                    data_dir,
                    apply=True,
                    to_legacy_v1=True,
                )

            self.assertEqual(raised.exception.code, "unsafe_staging_entry")
            self.assertEqual(external.read_bytes(), before)
            self.assertFalse(legacy_path(data_dir, "analysis", repository).exists())

    def test_to_legacy_v1_rejects_real_windows_junction_without_following_it(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows directory junction behavior is Windows-specific")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            data_dir.mkdir()
            repository = "owner/junction-reverse"
            external = root / "external-analysis"
            external.mkdir()
            external_source = stable_path(root, "external-analysis", repository)
            write_json(external_source, migrated(static_evidence(repository)))

            current = data_dir / "current.json"
            generations = data_dir / "generations"
            write_json(current, {"sentinel": "current"})
            write_json(
                generations / "retained/manifest.json",
                {"sentinel": "retained"},
            )
            external_before = tree_bytes(external)
            current_before = current.read_bytes()
            generations_before = tree_bytes(generations)

            junction = data_dir / "analysis"
            created = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(junction),
                    str(external),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0 or not os.path.lexists(junction):
                if os.path.lexists(junction):
                    junction.rmdir()
                self.skipTest(
                    "Windows junction creation is unavailable: "
                    f"exit={created.returncode} stderr={created.stderr.strip()!r}"
                )

            try:
                with self.assertRaises(ProjectIdentityMigrationError) as raised:
                    migrate_project_identity(
                        data_dir,
                        apply=True,
                        to_legacy_v1=True,
                    )

                self.assertEqual(
                    raised.exception.code,
                    "unsafe_staging_directory",
                )
                self.assertEqual(tree_bytes(external), external_before)
                self.assertEqual(current.read_bytes(), current_before)
                self.assertEqual(tree_bytes(generations), generations_before)
            finally:
                # The path and its parent are fixed inside this test's temporary
                # root. rmdir removes the junction directory entry only and
                # never recursively traverses or deletes the external target.
                if os.path.lexists(junction):
                    self.assertEqual(junction.parent, data_dir)
                    junction.rmdir()

            self.assertFalse(os.path.lexists(junction))
            self.assertEqual(tree_bytes(external), external_before)

    def test_to_legacy_v1_rejects_generation_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            generation = Path(temporary) / "data/generations/retained"
            repository = "owner/reverse-protected"
            source = stable_path(generation, "analysis", repository)
            write_json(generation / "manifest.json", {"state": "ready"})
            write_json(source, migrated(static_evidence(repository)))
            before = source.read_bytes()

            with self.assertRaises(ProjectIdentityMigrationError) as raised:
                migrate_project_identity(
                    generation / "nested/..",
                    apply=True,
                    to_legacy_v1=True,
                )

            self.assertEqual(raised.exception.code, "protected_generation")
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(legacy_path(generation, "analysis", repository).exists())

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
