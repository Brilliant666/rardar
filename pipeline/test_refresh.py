from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.analyze_repository import StaticEvidence
from pipeline.audit_data import audit_data
from pipeline.generations import (
    CandidateGenerationError,
    create_candidate_generation,
    publish_candidate_generation,
    resolve_current_generation,
)
from pipeline.refresh import _write_json_batch, refresh
from pipeline.project_identity import identity_for_repository
from pipeline.scheduler import committed_refresh_at
from pipeline.schema_validation import ArtifactValidationError
from pipeline.test_generations import _seed_legacy


SEED_PUBLISHED_AT = datetime(2026, 7, 11, 1, tzinfo=timezone.utc)
FIRST_REFRESH_AT = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
SECOND_REFRESH_AT = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)


def _bootstrap(data_dir: Path) -> None:
    _seed_legacy(data_dir)
    candidate = create_candidate_generation(
        data_dir,
        "bootstrap",
        generation_id="seed-generation",
        created_at=SEED_PUBLISHED_AT,
    )
    publish_candidate_generation(candidate, published_at=SEED_PUBLISHED_AT)


def _signals(captured_at: datetime) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "capturedAt": captured_at.isoformat(),
        "windowHours": 48,
        "signalCount": 0,
        "healthySourceCount": 0,
        "failedSourceCount": 0,
        "sourceStatus": [],
        "topSignals": [],
        "signals": [],
    }


class StubClient:
    def __init__(self, stars: int):
        self.stars = stars

    def search(self, query: str, per_page: int = 30):
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


class RefreshTests(unittest.TestCase):
    def test_refresh_publishes_identity_v1_artifacts_catalog_and_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _bootstrap(data_dir)
            identity = identity_for_repository("demo/agent-tool")
            evidence = StaticEvidence(
                repository="demo/agent-tool",
                source="https://github.com/demo/agent-tool",
                analyzed_at=FIRST_REFRESH_AT.isoformat(),
                scanned_files=10,
                language_files={".py": 10},
                indicators={
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
                counts={"test_files": 2, "todo_markers": 0},
                license_hint="MIT",
                confidence=80,
                schemaVersion=2,
                projectIdVersion=1,
                projectId=identity.project_id,
                warnings=["static inspection only; code was not executed"],
            )

            with (
                patch("pipeline.refresh.analyze_remote", return_value=evidence),
                patch("pipeline.refresh.collect_signals", return_value=_signals(FIRST_REFRESH_AT)),
            ):
                refresh(
                    data_dir,
                    FIRST_REFRESH_AT,
                    analyze_top=1,
                    client=StubClient(100),
                    collect_external_signals=True,
                )

            current = resolve_current_generation(data_dir)
            catalog = json.loads(
                (current.root / "catalog/latest.json").read_text(encoding="utf-8")
            )
            queue = json.loads(
                (current.root / "queues/codex.json").read_text(encoding="utf-8")
            )
            analysis_path = current.root / "analysis" / f"{identity.project_id}.json"
            self.assertTrue(analysis_path.is_file())
            self.assertEqual(catalog["schemaVersion"], 3)
            self.assertEqual(catalog["projects"][0]["projectId"], identity.project_id)
            self.assertEqual(queue["schemaVersion"], 2)
            self.assertEqual(queue["projectIdVersion"], 1)
            self.assertEqual(audit_data(current.root)["errorCount"], 0)

    def test_successful_full_refresh_writes_a_consistent_commit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _bootstrap(data_dir)
            pointer_before = (data_dir / "current.json").read_bytes()

            with patch("pipeline.refresh.collect_signals", return_value=_signals(FIRST_REFRESH_AT)):
                refresh(
                    data_dir,
                    FIRST_REFRESH_AT,
                    analyze_top=0,
                    client=StubClient(100),
                    collect_external_signals=True,
                )

            current = resolve_current_generation(data_dir)
            self.assertNotEqual((data_dir / "current.json").read_bytes(), pointer_before)
            self.assertNotEqual(current.generation_id, "seed-generation")
            self.assertTrue((current.root / "signals/enrichment.json").is_file())
            signal_enrichment = json.loads(
                (current.root / "signals/enrichment.json").read_text(encoding="utf-8")
            )
            self.assertEqual(signal_enrichment["schemaVersion"], 1)
            self.assertIsInstance(signal_enrichment["items"], dict)
            self.assertEqual(committed_refresh_at(data_dir), FIRST_REFRESH_AT.isoformat())

    def test_json_batch_rolls_back_replacement_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text('{"version": 1}\n', encoding="utf-8")
            second.write_text('{"version": 1}\n', encoding="utf-8")
            path_type = type(first)
            original_replace = path_type.replace

            def fail_second_staged_replace(source: Path, target: Path) -> Path:
                if source.name.startswith(f".{second.name}.") and source.name.endswith(".tmp"):
                    raise OSError("simulated replace failure")
                return original_replace(source, target)

            with patch.object(path_type, "replace", new=fail_second_staged_replace):
                with self.assertRaisesRegex(OSError, "simulated replace failure"):
                    _write_json_batch([(first, {"version": 2}), (second, {"version": 2})])

            self.assertEqual(json.loads(first.read_text(encoding="utf-8")), {"version": 1})
            self.assertEqual(json.loads(second.read_text(encoding="utf-8")), {"version": 1})
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["first.json", "second.json"])

    def test_json_batch_rejects_non_finite_values_before_replacing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            path.write_text('{"score": 0.5}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "JSON compliant"):
                _write_json_batch([(path, {"score": float("nan")})])

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"score": 0.5})

    def test_second_refresh_archives_previous_and_reports_observed_growth(self) -> None:
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
                catalog = refresh(
                    data_dir,
                    SECOND_REFRESH_AT,
                    analyze_top=0,
                    client=StubClient(140),
                    collect_external_signals=True,
                )

            current = resolve_current_generation(data_dir)
            history = list((current.root / "snapshots" / "history").glob("*.json"))
            latest = json.loads(
                (current.root / "snapshots" / "latest.json").read_text(encoding="utf-8")
            )
            history_payloads = [
                json.loads(path.read_text(encoding="utf-8")) for path in history
            ]
            self.assertEqual(
                sum(item.get("captured_at") == FIRST_REFRESH_AT.isoformat() for item in history_payloads),
                1,
            )
            self.assertEqual(latest["repositories"][0]["stars"], 140)
            self.assertEqual(catalog["projects"][0]["growthKind"], "observed")
            self.assertEqual(catalog["projects"][0]["growthValue"], 40)
            self.assertEqual(catalog["previousCapturedAt"], FIRST_REFRESH_AT.isoformat())
            self.assertEqual(catalog["projects"][0]["heatObservationCount"], 2)
            self.assertGreaterEqual(catalog["projects"][0]["heatObservationWindow"], 2)
            self.assertEqual(
                catalog["heatHistory"]["snapshotCount"],
                catalog["projects"][0]["heatObservationWindow"],
            )

    def test_failed_derived_collection_does_not_advance_snapshot_or_catalog(self) -> None:
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
            pointer_before = (data_dir / "current.json").read_bytes()
            current_before = resolve_current_generation(data_dir)
            tracked_paths = [
                current_before.root / "snapshots" / "latest.json",
                current_before.root / "catalog" / "latest.json",
                current_before.root / "queues" / "codex.json",
            ]
            before = {path: path.read_bytes() for path in tracked_paths}

            with patch("pipeline.refresh.collect_signals", side_effect=RuntimeError("signal parser failed")):
                with self.assertRaises(CandidateGenerationError) as raised:
                    refresh(
                        data_dir,
                        SECOND_REFRESH_AT,
                        analyze_top=0,
                        client=StubClient(140),
                        collect_external_signals=True,
                    )

            self.assertEqual(raised.exception.code, "candidate_build_failed")
            self.assertEqual(raised.exception.stage, "build")
            self.assertIsNotNone(raised.exception.generation_id)
            self.assertIsInstance(raised.exception.__cause__, RuntimeError)
            self.assertIn("signal parser failed", str(raised.exception))
            self.assertEqual({path: path.read_bytes() for path in tracked_paths}, before)
            self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)
            self.assertEqual(resolve_current_generation(data_dir).generation_id, current_before.generation_id)
            failed_candidates = list((data_dir / "generations/.candidates").glob("*/manifest.json"))
            self.assertEqual(len(failed_candidates), 1)
            self.assertEqual(
                failed_candidates[0].parent.name,
                raised.exception.generation_id,
            )
            self.assertEqual(
                json.loads(failed_candidates[0].read_text(encoding="utf-8"))["state"],
                "failed",
            )

    def test_invalid_collector_snapshot_is_rejected_before_static_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            _bootstrap(data_dir)
            pointer_before = (data_dir / "current.json").read_bytes()
            invalid_snapshot = {
                "schema_version": 1,
                "captured_at": "2026-07-10T12:00:00Z",
                "queries": ["stars:>=1"],
                "query_status": [
                    {
                        "query": "stars:>=1",
                        "state": "healthy",
                        "item_count": 1,
                        "error": None,
                    }
                ],
                "successful_query_count": 1,
                "failed_query_count": 0,
                "count": 1,
                "repositories": [{"repo": "demo/tool", "owner": None}],
            }

            with (
                patch("pipeline.refresh.collect", return_value=invalid_snapshot),
                patch("pipeline.refresh.analyze_remote") as analyze_remote,
            ):
                with self.assertRaises(CandidateGenerationError) as raised:
                    refresh(
                        data_dir,
                        FIRST_REFRESH_AT,
                        analyze_top=1,
                        client=StubClient(100),
                        collect_external_signals=False,
                    )

            self.assertEqual(raised.exception.code, "candidate_build_failed")
            self.assertEqual(raised.exception.stage, "build")
            self.assertIsNotNone(raised.exception.generation_id)
            self.assertIsInstance(raised.exception.__cause__, ArtifactValidationError)
            analyze_remote.assert_not_called()
            self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)


if __name__ == "__main__":
    unittest.main()
