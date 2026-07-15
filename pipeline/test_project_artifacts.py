from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline.project_artifacts as project_artifacts
from pipeline.project_artifacts import (
    ProjectArtifactError,
    adopt_candidate_project_identities,
    load_project_artifacts,
)
from pipeline.project_identity import identity_for_repository, legacy_slug_for_repository
from pipeline.schema_validation import ArtifactKind, load_validated_json


def _evidence(repository: str, *, version: int = 1, analyzed_at: str = "2026-07-15T00:00:00Z") -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": version,
        "repository": repository,
        "source": f"https://github.com/{repository}",
        "analyzed_at": analyzed_at,
        "scanned_files": 10,
        "language_files": {".py": 10},
        "indicators": {
            "readme": True,
            "license": True,
            "tests": True,
            "ci": True,
            "docker": False,
            "dependency_lock": True,
            "package_manifest": True,
            "examples": False,
            "docs": True,
            "environment_example": False,
        },
        "counts": {"test_files": 2, "todo_markers": 0},
        "license_hint": "MIT",
        "confidence": 80,
        "warnings": ["static inspection only; code was not executed"],
    }
    if version == 2:
        identity = identity_for_repository(repository)
        payload.update(
            projectIdVersion=identity.project_id_version,
            projectId=identity.project_id,
        )
    return payload


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _enrichment(repository: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": repository,
        "sourcePushedAt": "2026-07-14T00:00:00Z",
        "sourceAnalysisAt": "2026-07-15T00:00:00Z",
        "analyzedAt": "2026-07-15T01:00:00Z",
        "titleZh": "项目",
        "summaryZh": "稳定身份测试项目",
        "category": "开发工具",
        "capabilities": ["身份验证"],
        "taskTerms": ["stable-id"],
        "bestFor": "需要稳定身份的目录",
        "reusePlan": "先核对证据",
        "limitation": "未运行第三方代码",
        "evidenceSummary": "受控测试证据",
        "sourceUrl": f"https://github.com/{repository}#readme",
    }


def _to_v2(payload: dict[str, object]) -> dict[str, object]:
    repository = str(payload["repository"])
    identity = identity_for_repository(repository)
    return {
        **payload,
        "schemaVersion": 2,
        "projectIdVersion": identity.project_id_version,
        "projectId": identity.project_id,
    }


class ProjectArtifactTests(unittest.TestCase):
    def test_candidate_adoption_converts_v1_and_removes_legacy_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "data/generations/.candidates/test"
            repository = "owner/repo"
            legacy = candidate / "analysis" / f"{legacy_slug_for_repository(repository)}.json"
            _write(legacy, _evidence(repository))

            result = adopt_candidate_project_identities(candidate)

            identity = identity_for_repository(repository)
            target = candidate / "analysis" / f"{identity.project_id}.json"
            self.assertEqual(result, {"converted": 1, "removedLegacy": 1})
            self.assertFalse(legacy.exists())
            payload = load_validated_json(target, ArtifactKind.STATIC_EVIDENCE)
            self.assertEqual(payload["projectId"], identity.project_id)
            self.assertEqual(payload["schemaVersion"], 2)

    def test_equivalent_existing_v2_is_kept_and_legacy_v1_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "data/generations/.candidates/test"
            repository = "owner/repo"
            identity = identity_for_repository(repository)
            legacy = candidate / "analysis" / f"{legacy_slug_for_repository(repository)}.json"
            stable = candidate / "analysis" / f"{identity.project_id}.json"
            legacy_payload = _evidence(repository)
            stable_payload = _to_v2(legacy_payload)
            _write(legacy, legacy_payload)
            _write(stable, stable_payload)
            stable_bytes = stable.read_bytes()

            with patch(
                "pipeline.project_artifacts.atomic_write_validated_json"
            ) as writer:
                result = adopt_candidate_project_identities(candidate)

            writer.assert_not_called()
            self.assertEqual(result, {"converted": 0, "removedLegacy": 1})
            self.assertFalse(legacy.exists())
            self.assertEqual(stable.read_bytes(), stable_bytes)
            self.assertEqual(
                load_validated_json(stable, ArtifactKind.STATIC_EVIDENCE),
                stable_payload,
            )

    def test_newer_v1_conflicts_with_older_v2_and_preserves_both(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "data/generations/.candidates/test"
            repository = "owner/newer-legacy"
            identity = identity_for_repository(repository)
            legacy = candidate / "analysis" / f"{legacy_slug_for_repository(repository)}.json"
            stable = candidate / "analysis" / f"{identity.project_id}.json"
            _write(legacy, _evidence(repository, analyzed_at="2026-07-15T00:00:00Z"))
            _write(
                stable,
                _evidence(
                    repository,
                    version=2,
                    analyzed_at="2026-07-14T00:00:00Z",
                ),
            )
            before = {legacy: legacy.read_bytes(), stable: stable.read_bytes()}

            with self.assertRaises(ProjectArtifactError) as raised:
                adopt_candidate_project_identities(candidate)

            self.assertEqual(
                raised.exception.code,
                "conflicting_project_artifact_versions",
            )
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_older_v1_conflicts_with_newer_v2_and_preserves_both(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "data/generations/.candidates/test"
            repository = "owner/newer-stable"
            identity = identity_for_repository(repository)
            legacy = candidate / "analysis" / f"{legacy_slug_for_repository(repository)}.json"
            stable = candidate / "analysis" / f"{identity.project_id}.json"
            _write(legacy, _evidence(repository, analyzed_at="2026-07-14T00:00:00Z"))
            _write(
                stable,
                _evidence(
                    repository,
                    version=2,
                    analyzed_at="2026-07-15T00:00:00Z",
                ),
            )
            before = {legacy: legacy.read_bytes(), stable: stable.read_bytes()}

            with self.assertRaises(ProjectArtifactError) as raised:
                adopt_candidate_project_identities(candidate)

            self.assertEqual(
                raised.exception.code,
                "conflicting_project_artifact_versions",
            )
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_enrichment_source_time_difference_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "data/generations/.candidates/test"
            repository = "owner/enrichment-conflict"
            identity = identity_for_repository(repository)
            legacy = candidate / "enrichment" / f"{legacy_slug_for_repository(repository)}.json"
            stable = candidate / "enrichment" / f"{identity.project_id}.json"
            legacy_payload = _enrichment(repository)
            stable_payload = _to_v2(legacy_payload)
            stable_payload["sourceAnalysisAt"] = "2026-07-15T00:30:00Z"
            _write(legacy, legacy_payload)
            _write(stable, stable_payload)
            before = {legacy: legacy.read_bytes(), stable: stable.read_bytes()}

            with self.assertRaises(ProjectArtifactError) as raised:
                adopt_candidate_project_identities(candidate)

            self.assertEqual(
                raised.exception.code,
                "conflicting_project_artifact_versions",
            )
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_one_conflict_prevents_writes_or_deletes_for_every_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "data/generations/.candidates/test"
            convertible_repository = "owner/convertible"
            conflict_repository = "owner/conflict"
            convertible = (
                candidate
                / "analysis"
                / f"{legacy_slug_for_repository(convertible_repository)}.json"
            )
            conflict_legacy = (
                candidate
                / "enrichment"
                / f"{legacy_slug_for_repository(conflict_repository)}.json"
            )
            conflict_identity = identity_for_repository(conflict_repository)
            conflict_stable = (
                candidate
                / "enrichment"
                / f"{conflict_identity.project_id}.json"
            )
            conflict_legacy_payload = _enrichment(conflict_repository)
            conflict_stable_payload = _to_v2(conflict_legacy_payload)
            conflict_stable_payload["sourcePushedAt"] = "2026-07-14T00:01:00Z"
            _write(convertible, _evidence(convertible_repository))
            _write(conflict_legacy, conflict_legacy_payload)
            _write(conflict_stable, conflict_stable_payload)
            before = {
                path: path.read_bytes()
                for path in (convertible, conflict_legacy, conflict_stable)
            }
            convertible_target = (
                candidate
                / "analysis"
                / f"{identity_for_repository(convertible_repository).project_id}.json"
            )

            with patch(
                "pipeline.project_artifacts.atomic_write_validated_json"
            ) as writer, patch(
                "pipeline.project_artifacts._remove_legacy_source"
            ) as remover:
                with self.assertRaises(ProjectArtifactError) as raised:
                    adopt_candidate_project_identities(candidate)

            self.assertEqual(
                raised.exception.code,
                "conflicting_project_artifact_versions",
            )
            writer.assert_not_called()
            remover.assert_not_called()
            self.assertFalse(convertible_target.exists())
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_target_write_interruption_keeps_all_sources_and_retry_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "data/generations/.candidates/test"
            analysis_repository = "owner/write-retry-analysis"
            enrichment_repository = "owner/write-retry-enrichment"
            analysis_source = (
                candidate
                / "analysis"
                / f"{legacy_slug_for_repository(analysis_repository)}.json"
            )
            enrichment_source = (
                candidate
                / "enrichment"
                / f"{legacy_slug_for_repository(enrichment_repository)}.json"
            )
            analysis_target = (
                candidate
                / "analysis"
                / f"{identity_for_repository(analysis_repository).project_id}.json"
            )
            enrichment_target = (
                candidate
                / "enrichment"
                / f"{identity_for_repository(enrichment_repository).project_id}.json"
            )
            _write(analysis_source, _evidence(analysis_repository))
            _write(enrichment_source, _enrichment(enrichment_repository))
            original_writer = project_artifacts.atomic_write_validated_json
            write_count = 0

            def interrupt_second_write(*args: object, **kwargs: object) -> object:
                nonlocal write_count
                write_count += 1
                if write_count == 2:
                    raise OSError("simulated target write interruption")
                return original_writer(*args, **kwargs)

            with patch(
                "pipeline.project_artifacts.atomic_write_validated_json",
                side_effect=interrupt_second_write,
            ):
                with self.assertRaises(ProjectArtifactError) as raised:
                    adopt_candidate_project_identities(candidate)

            self.assertEqual(
                raised.exception.code,
                "project_artifact_target_write_failed",
            )
            self.assertTrue(analysis_source.exists())
            self.assertTrue(enrichment_source.exists())
            self.assertTrue(analysis_target.exists())
            self.assertFalse(enrichment_target.exists())

            retry = adopt_candidate_project_identities(candidate)

            self.assertEqual(retry, {"converted": 1, "removedLegacy": 2})
            self.assertFalse(analysis_source.exists())
            self.assertFalse(enrichment_source.exists())
            self.assertTrue(analysis_target.exists())
            self.assertTrue(enrichment_target.exists())

    def test_source_cleanup_interruption_is_retryable_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "data/generations/.candidates/test"
            analysis_repository = "owner/cleanup-retry-analysis"
            enrichment_repository = "owner/cleanup-retry-enrichment"
            analysis_source = (
                candidate
                / "analysis"
                / f"{legacy_slug_for_repository(analysis_repository)}.json"
            )
            enrichment_source = (
                candidate
                / "enrichment"
                / f"{legacy_slug_for_repository(enrichment_repository)}.json"
            )
            analysis_target = (
                candidate
                / "analysis"
                / f"{identity_for_repository(analysis_repository).project_id}.json"
            )
            enrichment_target = (
                candidate
                / "enrichment"
                / f"{identity_for_repository(enrichment_repository).project_id}.json"
            )
            _write(analysis_source, _evidence(analysis_repository))
            _write(enrichment_source, _enrichment(enrichment_repository))
            original_remover = project_artifacts._remove_legacy_source
            removal_count = 0

            def interrupt_second_cleanup(*args: object, **kwargs: object) -> object:
                nonlocal removal_count
                removal_count += 1
                if removal_count == 2:
                    raise OSError("simulated source cleanup interruption")
                return original_remover(*args, **kwargs)

            with patch(
                "pipeline.project_artifacts._remove_legacy_source",
                side_effect=interrupt_second_cleanup,
            ):
                with self.assertRaises(ProjectArtifactError) as raised:
                    adopt_candidate_project_identities(candidate)

            self.assertEqual(
                raised.exception.code,
                "project_artifact_source_cleanup_failed",
            )
            self.assertFalse(analysis_source.exists())
            self.assertTrue(enrichment_source.exists())
            self.assertTrue(analysis_target.exists())
            self.assertTrue(enrichment_target.exists())

            retry = adopt_candidate_project_identities(candidate)

            self.assertEqual(retry, {"converted": 0, "removedLegacy": 1})
            self.assertFalse(enrichment_source.exists())
            self.assertTrue(analysis_target.exists())
            self.assertTrue(enrichment_target.exists())

    def test_candidate_directory_symlink_is_rejected_without_external_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "data/generations/.candidates/test"
            candidate.mkdir(parents=True)
            external = root / "external-analysis"
            repository = "owner/external-symlink"
            external_source = (
                external / f"{legacy_slug_for_repository(repository)}.json"
            )
            sentinel = external / "sentinel.txt"
            _write(external_source, _evidence(repository))
            sentinel.write_bytes(b"external sentinel\n")
            linked = candidate / "analysis"
            try:
                linked.symlink_to(external, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")
            before = {
                external_source: external_source.read_bytes(),
                sentinel: sentinel.read_bytes(),
            }

            with patch(
                "pipeline.project_artifacts.atomic_write_validated_json"
            ) as writer, patch(
                "pipeline.project_artifacts._remove_legacy_source"
            ) as remover:
                with self.assertRaises(ProjectArtifactError) as raised:
                    adopt_candidate_project_identities(candidate)

            self.assertEqual(
                raised.exception.code,
                "unsafe_project_artifact_directory",
            )
            writer.assert_not_called()
            remover.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    @unittest.skipUnless(os.name == "nt", "Windows junctions require Windows")
    def test_candidate_directory_junction_is_rejected_without_external_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "data/generations/.candidates/test"
            candidate.mkdir(parents=True)
            external = root / "external-analysis"
            repository = "owner/external-junction"
            external_source = (
                external / f"{legacy_slug_for_repository(repository)}.json"
            )
            sentinel = external / "sentinel.txt"
            _write(external_source, _evidence(repository))
            sentinel.write_bytes(b"external sentinel\n")
            linked = candidate / "analysis"
            created = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(linked),
                    str(external),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest(
                    "directory junctions are unavailable: "
                    f"{created.stderr or created.stdout}"
                )
            before = {
                external_source: external_source.read_bytes(),
                sentinel: sentinel.read_bytes(),
            }
            try:
                with patch(
                    "pipeline.project_artifacts.atomic_write_validated_json"
                ) as writer, patch(
                    "pipeline.project_artifacts._remove_legacy_source"
                ) as remover:
                    with self.assertRaises(ProjectArtifactError) as raised:
                        adopt_candidate_project_identities(candidate)

                self.assertEqual(
                    raised.exception.code,
                    "unsafe_project_artifact_directory",
                )
                writer.assert_not_called()
                remover.assert_not_called()
                self.assertEqual(
                    {path: path.read_bytes() for path in before},
                    before,
                )
            finally:
                if os.path.lexists(linked):
                    os.rmdir(linked)

    def test_candidate_artifact_symlink_is_rejected_without_external_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "data/generations/.candidates/test"
            analysis = candidate / "analysis"
            analysis.mkdir(parents=True)
            repository = "owner/external-file"
            external = root / "external.json"
            _write(external, _evidence(repository))
            linked = analysis / f"{legacy_slug_for_repository(repository)}.json"
            try:
                linked.symlink_to(external)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"file symlinks are unavailable: {error}")
            before = external.read_bytes()

            with patch(
                "pipeline.project_artifacts.atomic_write_validated_json"
            ) as writer, patch(
                "pipeline.project_artifacts._remove_legacy_source"
            ) as remover:
                with self.assertRaises(ProjectArtifactError) as raised:
                    adopt_candidate_project_identities(candidate)

            self.assertEqual(raised.exception.code, "unsafe_project_artifact_entry")
            writer.assert_not_called()
            remover.assert_not_called()
            self.assertEqual(external.read_bytes(), before)

    def test_candidate_root_symlink_is_rejected_without_external_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = root / "data/generations/.candidates"
            candidates.mkdir(parents=True)
            external = root / "external-candidate"
            repository = "owner/external-root"
            external_source = (
                external
                / "analysis"
                / f"{legacy_slug_for_repository(repository)}.json"
            )
            _write(external_source, _evidence(repository))
            linked = candidates / "test"
            try:
                linked.symlink_to(external, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")
            before = external_source.read_bytes()

            with patch(
                "pipeline.project_artifacts.atomic_write_validated_json"
            ) as writer, patch(
                "pipeline.project_artifacts._remove_legacy_source"
            ) as remover:
                with self.assertRaises(ProjectArtifactError) as raised:
                    adopt_candidate_project_identities(linked)

            self.assertEqual(raised.exception.code, "unsafe_candidate_root")
            writer.assert_not_called()
            remover.assert_not_called()
            self.assertEqual(external_source.read_bytes(), before)

    def test_loader_selects_explicit_v2_over_v0_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "analysis"
            repository = "owner/repo"
            identity = identity_for_repository(repository)
            legacy_payload = _evidence(repository, version=0)
            legacy_payload.pop("analyzed_at")
            _write(root / f"{legacy_slug_for_repository(repository)}.json", legacy_payload)
            stable_payload = _evidence(repository, version=2)
            _write(root / f"{identity.project_id}.json", stable_payload)

            selected = load_project_artifacts(root, ArtifactKind.STATIC_EVIDENCE)

            self.assertEqual(selected["owner/repo"], stable_payload)

    def test_legacy_slug_collision_is_unresolved_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "analysis"
            repository = "owner/foo.bar"
            _write(
                root / f"{legacy_slug_for_repository(repository)}.json",
                _evidence(repository),
            )

            with self.assertRaisesRegex(ProjectArtifactError, "unresolved legacy slug collision"):
                load_project_artifacts(
                    root,
                    ArtifactKind.STATIC_EVIDENCE,
                    expected_repositories=["owner/foo.bar", "owner/foo-bar"],
                )

    def test_adoption_refuses_retained_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retained = Path(directory) / "data/generations/ready-generation"
            with self.assertRaisesRegex(ProjectArtifactError, "restricted"):
                adopt_candidate_project_identities(retained)

    def test_v2_filename_payload_identity_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "analysis"
            payload = _evidence("owner/repo", version=2)
            wrong_name = identity_for_repository("owner/other").project_id
            path = root / f"{wrong_name}.json"
            _write(path, payload)

            with self.assertRaises(ValueError):
                load_project_artifacts(root, ArtifactKind.STATIC_EVIDENCE)

    def test_forged_well_formed_project_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "analysis"
            payload = _evidence("owner/repo", version=2)
            project_id = str(payload["projectId"])
            forged = project_id[:-1] + ("0" if project_id[-1] != "0" else "1")
            payload["projectId"] = forged
            _write(root / f"{forged}.json", payload)

            with self.assertRaises(ValueError):
                load_project_artifacts(root, ArtifactKind.STATIC_EVIDENCE)

    def test_candidate_adoption_rejects_cross_directory_legacy_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "data/generations/.candidates/test"
            analysis = candidate / "analysis/owner--foo-bar.json"
            enrichment = candidate / "enrichment/owner--foo-bar.json"
            _write(analysis, _evidence("owner/foo.bar"))
            _write(enrichment, _enrichment("owner/foo-bar"))
            before = {analysis: analysis.read_bytes(), enrichment: enrichment.read_bytes()}

            with self.assertRaisesRegex(ProjectArtifactError, "unresolved legacy slug collision"):
                adopt_candidate_project_identities(candidate)

            self.assertEqual(analysis.read_bytes(), before[analysis])
            self.assertEqual(enrichment.read_bytes(), before[enrichment])
            self.assertEqual(
                list((candidate / "analysis").glob("*.json")),
                [analysis],
            )
            self.assertEqual(
                list((candidate / "enrichment").glob("*.json")),
                [enrichment],
            )


if __name__ == "__main__":
    unittest.main()
