from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.ingest_enrichment import ingest_enrichment
from pipeline.project_identity import identity_for_repository, project_id_for_repository
from pipeline.schema_validation import ArtifactValidationError, load_validated_json


def project_enrichment(repository: str = "demo/tool") -> dict[str, object]:
    identity = identity_for_repository(repository)
    return {
        "schemaVersion": 2,
        "projectIdVersion": identity.project_id_version,
        "projectId": identity.project_id,
        "repository": repository,
        "sourcePushedAt": "2026-07-10T23:00:00Z",
        "sourceAnalysisAt": "2026-07-10T23:30:00Z",
        "analyzedAt": "2026-07-11T00:00:00Z",
        "titleZh": "演示工具",
        "summaryZh": "用于验证正式画像写入边界。",
        "category": "开发工具",
        "capabilities": ["契约验证"],
        "taskTerms": ["schema"],
        "bestFor": "需要结构化验证的项目",
        "reusePlan": "先核对证据，再决定复用。",
        "limitation": "没有执行第三方代码。",
        "evidenceSummary": "来自受控测试样例。",
        "sourceUrl": "https://github.com/demo/tool#readme",
    }


class EnrichmentIngestTests(unittest.TestCase):
    def test_valid_project_draft_is_published_to_repository_identity_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            draft = root / "draft.json"
            payload = project_enrichment()
            draft.write_text(json.dumps(payload), encoding="utf-8")

            target = ingest_enrichment(data_dir, "project", draft)

            self.assertEqual(
                target,
                (data_dir / "enrichment" / f"{project_id_for_repository('demo/tool')}.json").resolve(),
            )
            self.assertEqual(load_validated_json(target), payload)
            self.assertTrue(draft.exists())

    def test_valid_signal_draft_is_published_to_fixed_signal_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            draft = root / "signal-draft.json"
            payload = {
                "schemaVersion": 1,
                "generatedAt": "2026-07-11T00:00:00Z",
                "model": "local-codex",
                "items": {
                    "https://example.com/news": {
                        "titleZh": "动态",
                        "takeawayZh": "事实摘要",
                        "whyItMattersZh": "影响判断",
                        "categoryZh": "AI",
                        "analyzedAt": "2026-07-11T00:00:00Z",
                        "sourcePublishedAt": "2026-07-10T23:00:00Z",
                    }
                },
            }
            draft.write_text(json.dumps(payload), encoding="utf-8")

            target = ingest_enrichment(data_dir, "signal", draft)

            self.assertEqual(target, (data_dir / "signals/enrichment.json").resolve())
            self.assertEqual(load_validated_json(target), payload)

    def test_invalid_draft_does_not_replace_existing_official_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            official = data_dir / "enrichment" / f"{project_id_for_repository('demo/tool')}.json"
            official.parent.mkdir(parents=True)
            existing = project_enrichment()
            official.write_text(json.dumps(existing), encoding="utf-8")
            before = official.read_bytes()
            draft = root / "invalid.json"
            invalid = project_enrichment()
            invalid["capabilities"] = "not-an-array"
            draft.write_text(json.dumps(invalid), encoding="utf-8")

            with self.assertRaises(ArtifactValidationError):
                ingest_enrichment(data_dir, "project", draft)

            self.assertEqual(official.read_bytes(), before)

    def test_official_path_cannot_be_used_as_its_own_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            official = data_dir / "enrichment" / f"{project_id_for_repository('demo/tool')}.json"
            official.parent.mkdir(parents=True)
            official.write_text(json.dumps(project_enrichment()), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be a draft"):
                ingest_enrichment(data_dir, "project", official)

    def test_draft_in_data_tmp_is_rejected_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            draft = data_dir / "tmp/draft.json"
            draft.parent.mkdir(parents=True)
            draft.write_text(json.dumps(project_enrichment()), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "outside the official data directory"):
                ingest_enrichment(data_dir, "project", draft)

            self.assertFalse((data_dir / "enrichment" / f"{project_id_for_repository('demo/tool')}.json").exists())

    def test_other_enrichment_file_inside_data_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            draft = data_dir / "enrichment/other.json"
            draft.parent.mkdir(parents=True)
            draft.write_text(json.dumps(project_enrichment()), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "outside the official data directory"):
                ingest_enrichment(data_dir, "project", draft)

            self.assertFalse((data_dir / "enrichment" / f"{project_id_for_repository('demo/tool')}.json").exists())

    def test_resolved_parent_traversal_into_data_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            actual_draft = data_dir / "tmp/draft.json"
            actual_draft.parent.mkdir(parents=True)
            actual_draft.write_text(json.dumps(project_enrichment()), encoding="utf-8")
            traversing_path = data_dir / ".." / "data" / "tmp/draft.json"

            with self.assertRaisesRegex(ValueError, "outside the official data directory"):
                ingest_enrichment(data_dir, "project", traversing_path)

    def test_symlink_resolving_into_data_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            actual_draft = data_dir / "tmp/draft.json"
            actual_draft.parent.mkdir(parents=True)
            actual_draft.write_text(json.dumps(project_enrichment()), encoding="utf-8")
            linked_draft = root / "linked-draft.json"
            try:
                linked_draft.symlink_to(actual_draft)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"file symlinks are unavailable: {error}")

            with self.assertRaisesRegex(ValueError, "outside the official data directory"):
                ingest_enrichment(data_dir, "project", linked_draft)

    def test_resolved_symlink_target_inside_data_is_rejected_portably(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            target = data_dir / "tmp/draft.json"
            target.parent.mkdir(parents=True)
            linked_draft = root / "linked-draft.json"
            resolved_target = target.resolve()
            original_resolve = Path.resolve

            def resolve_path(path: Path, strict: bool = False) -> Path:
                if path == linked_draft:
                    return resolved_target
                return original_resolve(path, strict=strict)

            with (
                patch.object(Path, "resolve", new=resolve_path),
                patch("pipeline.ingest_enrichment._read_draft") as read_draft,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "outside the official data directory",
                ):
                    ingest_enrichment(data_dir, "project", linked_draft)

            read_draft.assert_not_called()

    def test_data_directory_itself_cannot_be_used_as_a_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            data_dir.mkdir()

            with self.assertRaisesRegex(ValueError, "outside the official data directory"):
                ingest_enrichment(data_dir, "project", data_dir)

    def test_legacy_project_draft_cannot_be_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            draft = root / "legacy-project.json"
            payload = project_enrichment()
            payload["schemaVersion"] = 0
            payload.pop("sourcePushedAt")
            payload.pop("sourceAnalysisAt")
            payload.pop("projectIdVersion")
            payload.pop("projectId")
            draft.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "legacy v0/v1"):
                ingest_enrichment(data_dir, "project", draft)

            self.assertFalse((data_dir / "enrichment" / f"{project_id_for_repository('demo/tool')}.json").exists())

    def test_project_draft_cannot_bind_future_static_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            draft = root / "future-evidence.json"
            payload = project_enrichment()
            payload["sourceAnalysisAt"] = "2026-07-11T00:00:01Z"
            payload["analyzedAt"] = "2026-07-11T00:00:00Z"
            draft.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "analyzedAt cannot precede sourceAnalysisAt",
            ):
                ingest_enrichment(data_dir, "project", draft)

            self.assertFalse((data_dir / "enrichment" / f"{project_id_for_repository('demo/tool')}.json").exists())


if __name__ == "__main__":
    unittest.main()
