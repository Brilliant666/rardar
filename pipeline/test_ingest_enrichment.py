from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline.ingest_enrichment import ingest_enrichment
from pipeline.schema_validation import ArtifactValidationError, load_validated_json


def project_enrichment(repository: str = "demo/tool") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": repository,
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

            self.assertEqual(target, (data_dir / "enrichment/demo--tool.json").resolve())
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
            official = data_dir / "enrichment/demo--tool.json"
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
            official = data_dir / "enrichment/demo--tool.json"
            official.parent.mkdir(parents=True)
            official.write_text(json.dumps(project_enrichment()), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be a draft"):
                ingest_enrichment(data_dir, "project", official)


if __name__ == "__main__":
    unittest.main()
