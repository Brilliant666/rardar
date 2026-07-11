from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.codex_queue import build_codex_queue


class CodexQueueTests(unittest.TestCase):
    def test_only_incomplete_items_enter_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            enrichment_dir = root / "enrichment"
            analysis_dir = root / "analysis"
            enrichment_dir.mkdir()
            analysis_dir.mkdir()
            complete_project = {
                "repository": "owner/complete",
                "analyzedAt": "2026-07-10T12:00:00Z",
                "titleZh": "完整项目",
                "summaryZh": "摘要",
                "capabilities": ["能力"],
                "taskTerms": ["任务"],
                "bestFor": "适用任务",
                "reusePlan": "复用",
                "limitation": "风险",
                "evidenceSummary": "证据",
                "sourceUrl": "https://github.com/owner/complete#readme",
            }
            (enrichment_dir / "owner--complete.json").write_text(json.dumps(complete_project), encoding="utf-8")
            (analysis_dir / "owner--complete.json").write_text(
                json.dumps(
                    {
                        "repository": "owner/complete",
                        "analyzed_at": "2026-07-10T12:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            signal_path = root / "signals.json"
            signal_path.write_text(
                json.dumps(
                    {
                        "generatedAt": "2026-07-10T12:00:00Z",
                        "items": {
                            "https://complete.example": {
                                "titleZh": "标题",
                                "takeawayZh": "要点",
                                "whyItMattersZh": "价值",
                                "categoryZh": "分类",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            queue = build_codex_queue(
                {"projects": [{"repo": "owner/complete"}, {"repo": "owner/pending"}]},
                {
                    "signals": [
                        {
                            "id": "complete",
                            "url": "https://complete.example",
                            "publishedAt": "2026-07-10T11:00:00Z",
                        },
                        {
                            "id": "pending",
                            "url": "https://pending.example",
                            "title": "Pending",
                            "publishedAt": "2026-07-10T11:00:00Z",
                        },
                    ]
                },
                enrichment_dir,
                signal_path,
                datetime(2026, 7, 11, tzinfo=timezone.utc),
            )

        self.assertEqual(queue["pendingCount"], 2)
        self.assertEqual(queue["projectPendingCount"], 1)
        self.assertEqual(queue["signalPendingCount"], 1)
        self.assertEqual({item["id"] for item in queue["items"]}, {"project:owner--pending", "signal:pending"})
        self.assertIn("safety", queue["items"][0])
        project_item = next(item for item in queue["items"] if item["kind"] == "project")
        self.assertEqual(project_item["evidenceState"], "static_analysis_required")
        self.assertNotIn("data/analysis/owner--pending.json", project_item["inputPaths"])

    def test_updated_signal_url_reenters_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            enrichment_dir = root / "enrichment"
            analysis_dir = root / "analysis"
            enrichment_dir.mkdir()
            analysis_dir.mkdir()
            signal_path = root / "signals.json"
            signal_path.write_text(
                json.dumps(
                    {
                        "generatedAt": "2026-07-10T12:00:00Z",
                        "items": {
                            "https://same.example": {
                                "titleZh": "旧标题",
                                "takeawayZh": "旧要点",
                                "whyItMattersZh": "旧影响",
                                "categoryZh": "旧分类",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            queue = build_codex_queue(
                {"projects": []},
                {
                    "signals": [
                        {
                            "id": "updated",
                            "url": "https://same.example",
                            "title": "Updated",
                            "publishedAt": "2026-07-11T09:00:00Z",
                        }
                    ]
                },
                enrichment_dir,
                signal_path,
                datetime(2026, 7, 11, 10, tzinfo=timezone.utc),
            )

        self.assertEqual(queue["signalPendingCount"], 1)
        self.assertIn("更新的发布时间", queue["items"][0]["reason"])
        self.assertEqual(queue["items"][0]["sourcePublishedAt"], "2026-07-11T09:00:00Z")
        self.assertIn("analyzedAt", queue["items"][0]["requiredFields"])

    def test_stale_complete_project_reenters_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            enrichment_dir = root / "enrichment"
            analysis_dir = root / "analysis"
            enrichment_dir.mkdir()
            analysis_dir.mkdir()
            enrichment = {
                "repository": "owner/project",
                "analyzedAt": "2026-07-10T08:00:00Z",
                "titleZh": "项目",
                "summaryZh": "摘要",
                "capabilities": ["能力"],
                "taskTerms": ["任务"],
                "bestFor": "适用任务",
                "reusePlan": "复用",
                "limitation": "风险",
                "evidenceSummary": "证据",
                "sourceUrl": "https://github.com/owner/project#readme",
            }
            (enrichment_dir / "owner--project.json").write_text(json.dumps(enrichment), encoding="utf-8")
            (analysis_dir / "owner--project.json").write_text(
                json.dumps(
                    {
                        "repository": "owner/project",
                        "analyzed_at": "2026-07-10T10:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            signal_path = root / "signals.json"
            signal_path.write_text('{"items": {}}', encoding="utf-8")

            queue = build_codex_queue(
                {"projects": [{"repo": "owner/project", "sourcePushedAt": "2026-07-10T09:00:00Z"}]},
                {"signals": []},
                enrichment_dir,
                signal_path,
                datetime(2026, 7, 11, tzinfo=timezone.utc),
            )

        self.assertEqual(queue["projectPendingCount"], 1)
        self.assertIn("新推送", queue["items"][0]["reason"])
        self.assertEqual(queue["items"][0]["previousAnalyzedAt"], "2026-07-10T08:00:00Z")

    def test_current_static_analysis_marks_project_evidence_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            enrichment_dir = root / "enrichment"
            analysis_dir = root / "analysis"
            enrichment_dir.mkdir()
            analysis_dir.mkdir()
            catalog = {
                "projects": [
                    {
                        "repo": "owner/project",
                        "title": "Project",
                        "sourcePushedAt": "2026-07-10T09:00:00Z",
                    }
                ]
            }
            analysis = {
                "repository": "owner/project",
                "analyzed_at": "2026-07-10T10:00:00Z",
            }
            (analysis_dir / "owner--project.json").write_text(
                json.dumps(analysis),
                encoding="utf-8",
            )

            queue = build_codex_queue(
                catalog,
                {"signals": []},
                enrichment_dir,
                root / "signal-enrichment.json",
                datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
            )

        self.assertEqual(queue["projectPendingCount"], 1)
        self.assertEqual(queue["items"][0]["evidenceState"], "ready")
        self.assertIn("data/analysis/owner--project.json", queue["items"][0]["inputPaths"])


if __name__ == "__main__":
    unittest.main()
