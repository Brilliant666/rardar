from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.audit_data import audit_data
from pipeline.rebuild_derived import rebuild_derived
from pipeline.refresh import refresh


class StubClient:
    def __init__(self, stars: int):
        self.stars = stars

    def search(self, _query: str, per_page: int = 30):
        return [
            {
                "full_name": "demo/agent-tool",
                "html_url": "https://github.com/demo/agent-tool",
                "description": "AI developer workflow tool",
                "owner": {"login": "demo"},
                "language": "Python",
                "license": {"spdx_id": "MIT"},
                "topics": ["ai", "developer-tools"],
                "stargazers_count": self.stars,
                "forks_count": 20,
                "open_issues_count": 2,
                "created_at": "2026-07-01T00:00:00Z",
                "updated_at": "2026-07-10T00:00:00Z",
                "pushed_at": "2026-07-10T00:00:00Z",
                "default_branch": "main",
            }
        ]


class RebuildDerivedTests(unittest.TestCase):
    def test_applies_enrichment_without_advancing_or_losing_growth_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            refresh(
                data_dir,
                datetime(2026, 7, 9, 12, tzinfo=timezone.utc),
                analyze_top=0,
                client=StubClient(100),
                collect_external_signals=False,
            )
            refresh(
                data_dir,
                datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
                analyze_top=0,
                client=StubClient(140),
                collect_external_signals=False,
            )
            signals = {
                "schemaVersion": 1,
                "capturedAt": "2026-07-10T12:00:00+00:00",
                "windowHours": 48,
                "signalCount": 0,
                "healthySourceCount": 0,
                "failedSourceCount": 0,
                "sourceStatus": [],
                "topSignals": [],
                "signals": [],
            }
            signals_path = data_dir / "signals" / "latest.json"
            signals_path.parent.mkdir(parents=True, exist_ok=True)
            signals_path.write_text(json.dumps(signals), encoding="utf-8")
            analysis = {
                "schemaVersion": 1,
                "repository": "demo/agent-tool",
                "source": "https://github.com/demo/agent-tool",
                "analyzed_at": "2026-07-10T12:20:00+00:00",
                "scanned_files": 120,
                "language_files": {".py": 120},
                "confidence": 85,
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
                "counts": {"test_files": 8, "todo_markers": 0},
                "license_hint": "MIT",
                "warnings": ["static inspection only; code was not executed"],
            }
            analysis_path = data_dir / "analysis" / "demo--agent-tool.json"
            analysis_path.parent.mkdir(parents=True, exist_ok=True)
            analysis_path.write_text(json.dumps(analysis), encoding="utf-8")
            enrichment = {
                "schemaVersion": 1,
                "repository": "demo/agent-tool",
                "analyzedAt": "2026-07-10T12:30:00+00:00",
                "titleZh": "开发工作流工具",
                "summaryZh": "用于自动化开发工作流。",
                "category": "开发工具",
                "capabilities": ["工作流自动化"],
                "taskTerms": ["开发", "自动化"],
                "bestFor": "需要减少重复开发步骤的团队",
                "reusePlan": "先复用工作流定义",
                "limitation": "尚未执行仓库代码",
                "evidenceSummary": "依据 README 与只读静态证据",
                "sourceUrl": "https://github.com/demo/agent-tool",
            }
            enrichment_path = data_dir / "enrichment" / "demo--agent-tool.json"
            enrichment_path.parent.mkdir(parents=True, exist_ok=True)
            enrichment_path.write_text(json.dumps(enrichment), encoding="utf-8")
            snapshot_path = data_dir / "snapshots" / "latest.json"
            snapshot_before = snapshot_path.read_bytes()

            catalog, queue = rebuild_derived(
                data_dir,
                datetime(2026, 7, 10, 13, tzinfo=timezone.utc),
            )

            project = catalog["projects"][0]
            self.assertEqual(snapshot_path.read_bytes(), snapshot_before)
            self.assertEqual(catalog["capturedAt"], "2026-07-10T12:00:00+00:00")
            self.assertEqual(catalog["previousCapturedAt"], "2026-07-09T12:00:00+00:00")
            self.assertEqual(project["growthKind"], "observed")
            self.assertEqual(project["growthValue"], 40)
            self.assertEqual(project["analysisState"], "深度分析")
            self.assertEqual(project["heatObservationWindow"], 2)
            self.assertEqual(queue["projectPendingCount"], 0)
            self.assertEqual(audit_data(data_dir)["status"], "healthy")
            analysis_path.unlink()
            corrupted = audit_data(data_dir)

        self.assertIn(
            "deep_analysis_without_current_evidence",
            {item["code"] for item in corrupted["issues"]},
        )


if __name__ == "__main__":
    unittest.main()
