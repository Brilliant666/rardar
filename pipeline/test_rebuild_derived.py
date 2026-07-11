from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.audit_data import audit_data
from pipeline.generations import (
    CandidateGenerationError,
    create_candidate_generation,
    resolve_current_generation,
)
from pipeline.rebuild_derived import _rebuild_derived_candidate, rebuild_derived
from pipeline.refresh import refresh
from pipeline.test_refresh import (
    FIRST_REFRESH_AT,
    SECOND_REFRESH_AT,
    _bootstrap,
    _signals,
)


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
            _bootstrap(data_dir)
            with patch("pipeline.refresh.collect_signals", return_value=_signals(FIRST_REFRESH_AT)):
                refresh(
                    data_dir,
                    FIRST_REFRESH_AT,
                    analyze_top=0,
                    client=StubClient(100),
                    collect_external_signals=True,
                )
            with patch("pipeline.refresh.collect_signals", return_value=_signals(SECOND_REFRESH_AT)):
                refresh(
                    data_dir,
                    SECOND_REFRESH_AT,
                    analyze_top=0,
                    client=StubClient(140),
                    collect_external_signals=True,
                )
            analysis = {
                "schemaVersion": 1,
                "repository": "demo/agent-tool",
                "source": "https://github.com/demo/agent-tool",
                "analyzed_at": "2026-07-13T12:20:00+00:00",
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
                "sourcePushedAt": "2026-07-10T00:00:00Z",
                "sourceAnalysisAt": "2026-07-13T12:20:00+00:00",
                "analyzedAt": "2026-07-13T12:30:00+00:00",
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
            previous_generation = resolve_current_generation(data_dir)
            pointer_before = (data_dir / "current.json").read_bytes()
            snapshot_path = previous_generation.root / "snapshots" / "latest.json"
            snapshot_before = snapshot_path.read_bytes()

            catalog, queue = rebuild_derived(
                data_dir,
                datetime(2026, 7, 13, 13, tzinfo=timezone.utc),
            )

            current = resolve_current_generation(data_dir)
            project = catalog["projects"][0]
            self.assertEqual(snapshot_path.read_bytes(), snapshot_before)
            self.assertEqual(
                (current.root / "snapshots/latest.json").read_bytes(),
                snapshot_before,
            )
            self.assertNotEqual((data_dir / "current.json").read_bytes(), pointer_before)
            self.assertEqual(catalog["capturedAt"], SECOND_REFRESH_AT.isoformat())
            self.assertEqual(catalog["previousCapturedAt"], FIRST_REFRESH_AT.isoformat())
            self.assertEqual(project["growthKind"], "observed")
            self.assertEqual(project["growthValue"], 40)
            self.assertEqual(project["analysisState"], "深度分析")
            self.assertGreaterEqual(project["heatObservationWindow"], 2)
            self.assertEqual(queue["projectPendingCount"], 0)
            self.assertEqual(audit_data(current.root)["status"], "healthy")
            analysis_path.unlink()
            still_healthy = audit_data(current.root)

        self.assertEqual(still_healthy["status"], "healthy")

    def test_derive_adds_consumer_required_signal_enrichment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _bootstrap(data_dir)
            pointer_before = (data_dir / "current.json").read_bytes()
            candidate = create_candidate_generation(
                data_dir,
                "derive",
                generation_id="derive-missing-signal-enrichment",
                created_at=datetime(2026, 7, 11, 2, tzinfo=timezone.utc),
            )
            (candidate.path / "signals/enrichment.json").unlink()

            _rebuild_derived_candidate(
                candidate,
                datetime(2026, 7, 11, 2, tzinfo=timezone.utc),
            )

            payload = json.loads(
                (candidate.path / "signals/enrichment.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertEqual(payload["model"], "none")
            self.assertEqual(payload["items"], {})
            self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)

    def test_failed_derive_marks_candidate_and_keeps_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _bootstrap(data_dir)
            pointer_before = (data_dir / "current.json").read_bytes()
            current_before = resolve_current_generation(data_dir).generation_id

            with patch(
                "pipeline.rebuild_derived._rebuild_derived_candidate",
                side_effect=RuntimeError("simulated derive build failure"),
            ):
                with self.assertRaises(CandidateGenerationError) as raised:
                    rebuild_derived(
                        data_dir,
                        datetime(2026, 7, 11, 2, tzinfo=timezone.utc),
                    )

            self.assertEqual(raised.exception.code, "candidate_build_failed")
            self.assertEqual(raised.exception.stage, "build")
            self.assertIsNotNone(raised.exception.generation_id)
            self.assertIsInstance(raised.exception.__cause__, RuntimeError)
            self.assertIn("simulated derive build failure", str(raised.exception))
            self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)
            self.assertEqual(resolve_current_generation(data_dir).generation_id, current_before)
            failed = list((data_dir / "generations/.candidates").glob("*/manifest.json"))
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0].parent.name, raised.exception.generation_id)
            manifest = json.loads(failed[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "failed")
            self.assertEqual(manifest["failureStage"], "build")

    def test_existing_generation_build_error_is_not_rewrapped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _bootstrap(data_dir)
            pointer_before = (data_dir / "current.json").read_bytes()

            def generation_failure(candidate, _now):
                raise CandidateGenerationError(
                    "upstream_generation_failure",
                    "simulated protocol failure",
                    generation_id=candidate.generation_id,
                    stage="build",
                )

            with patch(
                "pipeline.rebuild_derived._rebuild_derived_candidate",
                side_effect=generation_failure,
            ):
                with self.assertRaises(CandidateGenerationError) as raised:
                    rebuild_derived(
                        data_dir,
                        datetime(2026, 7, 11, 2, tzinfo=timezone.utc),
                    )

            self.assertEqual(raised.exception.code, "upstream_generation_failure")
            self.assertEqual(raised.exception.stage, "build")
            self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)


if __name__ == "__main__":
    unittest.main()
