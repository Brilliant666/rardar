from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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

    def test_existing_v2_is_authoritative_over_legacy_v1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "data/generations/.candidates/test"
            repository = "owner/repo"
            identity = identity_for_repository(repository)
            legacy = candidate / "analysis" / f"{legacy_slug_for_repository(repository)}.json"
            stable = candidate / "analysis" / f"{identity.project_id}.json"
            _write(legacy, _evidence(repository, analyzed_at="2026-07-14T00:00:00Z"))
            stable_payload = _evidence(
                repository,
                version=2,
                analyzed_at="2026-07-15T00:00:00Z",
            )
            _write(stable, stable_payload)

            result = adopt_candidate_project_identities(candidate)

            self.assertEqual(result, {"converted": 0, "removedLegacy": 1})
            self.assertFalse(legacy.exists())
            self.assertEqual(
                load_validated_json(stable, ArtifactKind.STATIC_EVIDENCE),
                stable_payload,
            )

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
