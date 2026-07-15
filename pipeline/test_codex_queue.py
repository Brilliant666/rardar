from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.codex_queue import build_codex_queue
from pipeline.project_identity import identity_for_repository
from pipeline.schema_validation import ArtifactKind, validate_payload


def static_evidence(repository: str, analyzed_at: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": repository,
        "source": f"https://github.com/{repository}",
        "analyzed_at": analyzed_at,
        "scanned_files": 10,
        "language_files": {".py": 10},
        "indicators": {
            "readme": True,
            "license": True,
            "tests": True,
            "ci": False,
            "docker": False,
            "dependency_lock": False,
            "package_manifest": True,
            "examples": False,
            "docs": False,
            "environment_example": False,
        },
        "counts": {"test_files": 1, "todo_markers": 0},
        "license_hint": "MIT",
        "confidence": 70,
        "warnings": ["static inspection only; code was not executed"],
    }


def signal_enrichment(
    items: dict[str, dict[str, str]],
    generated_at: str = "2026-07-10T12:00:00Z",
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "generatedAt": generated_at,
        "model": "test-codex",
        "items": items,
    }


class CodexQueueTests(unittest.TestCase):
    def test_only_incomplete_items_enter_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            enrichment_dir = root / "enrichment"
            analysis_dir = root / "analysis"
            enrichment_dir.mkdir()
            analysis_dir.mkdir()
            complete_project = {
                "schemaVersion": 1,
                "repository": "owner/complete",
                "sourcePushedAt": "2026-07-10T10:00:00Z",
                "sourceAnalysisAt": "2026-07-10T12:00:00Z",
                "analyzedAt": "2026-07-10T12:00:00Z",
                "titleZh": "完整项目",
                "summaryZh": "摘要",
                "category": "开发工具",
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
                json.dumps(static_evidence("owner/complete", "2026-07-10T12:00:00Z")),
                encoding="utf-8",
            )
            signal_path = root / "signals.json"
            signal_path.write_text(
                json.dumps(
                    signal_enrichment(
                        {
                            "https://complete.example": {
                                "titleZh": "标题",
                                "takeawayZh": "要点",
                                "whyItMattersZh": "价值",
                                "categoryZh": "分类",
                            }
                        }
                    )
                ),
                encoding="utf-8",
            )
            queue = build_codex_queue(
                {
                    "projects": [
                        {
                            "repo": "owner/complete",
                            "sourcePushedAt": "2026-07-10T10:00:00Z",
                        },
                        {
                            "repo": "owner/pending",
                            "sourcePushedAt": "2026-07-10T10:00:00Z",
                        },
                    ]
                },
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
        self.assertEqual(project_item["sourcePushedAt"], "2026-07-10T10:00:00Z")
        self.assertIsNone(project_item["sourceAnalysisAt"])
        self.assertNotIn("data/analysis/owner--pending.json", project_item["inputPaths"])
        self.assertTrue(validate_payload(ArtifactKind.CODEX_QUEUE, queue).valid)

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
                    signal_enrichment(
                        {
                            "https://same.example": {
                                "titleZh": "旧标题",
                                "takeawayZh": "旧要点",
                                "whyItMattersZh": "旧影响",
                                "categoryZh": "旧分类",
                            }
                        }
                    )
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
                "schemaVersion": 1,
                "repository": "owner/project",
                "sourcePushedAt": "2026-07-10T08:00:00Z",
                "sourceAnalysisAt": "2026-07-10T10:00:00Z",
                "analyzedAt": "2026-07-10T12:00:00Z",
                "titleZh": "项目",
                "summaryZh": "摘要",
                "category": "开发工具",
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
                json.dumps(static_evidence("owner/project", "2026-07-10T10:00:00Z")),
                encoding="utf-8",
            )
            signal_path = root / "signals.json"
            signal_path.write_text(json.dumps(signal_enrichment({})), encoding="utf-8")

            queue = build_codex_queue(
                {"projects": [{"repo": "owner/project", "sourcePushedAt": "2026-07-10T09:00:00Z"}]},
                {"signals": []},
                enrichment_dir,
                signal_path,
                datetime(2026, 7, 11, tzinfo=timezone.utc),
            )

        self.assertEqual(queue["projectPendingCount"], 1)
        self.assertIn("新推送", queue["items"][0]["reason"])
        self.assertEqual(queue["items"][0]["previousAnalyzedAt"], "2026-07-10T12:00:00Z")

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
            analysis = static_evidence("owner/project", "2026-07-10T10:00:00Z")
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
        self.assertEqual(queue["items"][0]["sourcePushedAt"], "2026-07-10T09:00:00Z")
        self.assertEqual(queue["items"][0]["sourceAnalysisAt"], "2026-07-10T10:00:00Z")
        self.assertIn("sourcePushedAt", queue["items"][0]["requiredFields"])
        self.assertIn("sourceAnalysisAt", queue["items"][0]["requiredFields"])
        self.assertIn("原样复制", queue["items"][0]["safety"])
        self.assertIn("不能自行生成、推算或改写", queue["items"][0]["safety"])
        self.assertIn("data/analysis/owner--project.json", queue["items"][0]["inputPaths"])
        self.assertTrue(validate_payload(ArtifactKind.CODEX_QUEUE, queue).valid)

    def test_stale_static_analysis_time_remains_explicit_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            enrichment_dir = root / "enrichment"
            analysis_dir = root / "analysis"
            enrichment_dir.mkdir()
            analysis_dir.mkdir()
            (analysis_dir / "owner--project.json").write_text(
                json.dumps(static_evidence("owner/project", "2026-07-10T08:00:00Z")),
                encoding="utf-8",
            )

            queue = build_codex_queue(
                {
                    "projects": [
                        {
                            "repo": "owner/project",
                            "sourcePushedAt": "2026-07-10T09:00:00Z",
                        }
                    ]
                },
                {"signals": []},
                enrichment_dir,
                root / "signal-enrichment.json",
                datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
            )

        self.assertEqual(queue["items"][0]["evidenceState"], "static_analysis_required")
        self.assertEqual(queue["items"][0]["sourceAnalysisAt"], "2026-07-10T08:00:00Z")
        self.assertTrue(validate_payload(ArtifactKind.CODEX_QUEUE, queue).valid)

    def test_generation_queue_inputs_bind_to_immutable_evidence_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            enrichment_dir = root / "enrichment"
            analysis_dir = root / "analysis"
            enrichment_dir.mkdir()
            analysis_dir.mkdir()
            (analysis_dir / "owner--project.json").write_text(
                json.dumps(static_evidence("owner/project", "2026-07-10T10:00:00Z")),
                encoding="utf-8",
            )

            queue = build_codex_queue(
                {
                    "projects": [
                        {
                            "repo": "owner/project",
                            "sourcePushedAt": "2026-07-10T09:00:00Z",
                        }
                    ]
                },
                {
                    "signals": [
                        {
                            "id": "signal",
                            "url": "https://signal.example",
                            "title": "Signal",
                            "publishedAt": "2026-07-10T09:00:00Z",
                        }
                    ]
                },
                enrichment_dir,
                root / "signal-enrichment.json",
                datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
                input_data_prefix="data/generations/20260710T120000Z-a1b2c3d4",
            )

        project = next(item for item in queue["items"] if item["kind"] == "project")
        signal = next(item for item in queue["items"] if item["kind"] == "signal")
        self.assertEqual(
            project["inputPaths"],
            [
                "data/generations/20260710T120000Z-a1b2c3d4/analysis/owner--project.json",
                "data/generations/20260710T120000Z-a1b2c3d4/catalog/latest.json",
            ],
        )
        self.assertEqual(
            signal["inputPaths"],
            ["data/generations/20260710T120000Z-a1b2c3d4/signals/latest.json"],
        )
        self.assertEqual(project["outputPath"], "data/enrichment/owner--project.json")
        self.assertTrue(validate_payload(ArtifactKind.CODEX_QUEUE, queue).valid)

    def test_queue_v2_uses_stable_identity_paths_and_canonical_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            enrichment_dir = root / "enrichment"
            analysis_dir = root / "analysis"
            enrichment_dir.mkdir()
            analysis_dir.mkdir()
            identity = identity_for_repository("Owner/Repo")
            evidence = static_evidence("owner/repo", "2026-07-15T00:00:00Z")
            evidence.update(
                schemaVersion=2,
                projectIdVersion=1,
                projectId=identity.project_id,
            )
            (analysis_dir / f"{identity.project_id}.json").write_text(
                json.dumps(evidence),
                encoding="utf-8",
            )

            queue = build_codex_queue(
                {
                    "schemaVersion": 3,
                    "projectIdVersion": 1,
                    "projects": [
                        {
                            "repo": "Owner/Repo",
                            "projectIdVersion": 1,
                            "projectId": identity.project_id,
                            "title": "Repo",
                            "sourcePushedAt": "2026-07-14T00:00:00Z",
                        }
                    ],
                },
                {"signals": []},
                enrichment_dir,
                root / "signal-enrichment.json",
                datetime(2026, 7, 15, 1, tzinfo=timezone.utc),
                input_data_prefix="data/generations/stable-generation",
            )

            project = queue["items"][0]
            self.assertEqual(queue["schemaVersion"], 2)
            self.assertEqual(queue["projectIdVersion"], 1)
            self.assertEqual(project["id"], f"project:{identity.project_id}")
            self.assertEqual(project["projectId"], identity.project_id)
            self.assertEqual(project["evidenceState"], "ready")
            self.assertEqual(
                project["inputPaths"],
                [
                    f"data/generations/stable-generation/analysis/{identity.project_id}.json",
                    "data/generations/stable-generation/catalog/latest.json",
                ],
            )
            self.assertEqual(
                project["outputPath"],
                f"data/enrichment/{identity.project_id}.json",
            )
            self.assertTrue(validate_payload(ArtifactKind.CODEX_QUEUE, queue).valid)

            project["inputPaths"] = [
                "data/generations/stable-generation/catalog/latest.json"
            ]
            self.assertFalse(validate_payload(ArtifactKind.CODEX_QUEUE, queue).valid)


if __name__ == "__main__":
    unittest.main()
