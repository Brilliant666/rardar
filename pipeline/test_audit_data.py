from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from pipeline.audit_data import audit_data
from pipeline.codex_queue import build_codex_queue


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def snapshot_repository(
    repository: str,
    captured_at: str,
    stars: int,
    query: str,
) -> dict[str, object]:
    owner = repository.split("/", 1)[0]
    return {
        "repo": repository,
        "url": f"https://github.com/{repository}",
        "description": "Test repository",
        "owner": owner,
        "language": "Python",
        "license": "MIT",
        "topics": ["developer-tools"],
        "stars": stars,
        "forks": 10,
        "open_issues": 1,
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": captured_at,
        "pushed_at": captured_at,
        "default_branch": "main",
        "captured_at": captured_at,
        "candidate_query": query,
        "analysis_state": "pending",
    }


def signal_item(
    identifier: str,
    url: str,
    published_at: str,
    score: float = 0.8,
) -> dict[str, object]:
    return {
        "id": identifier,
        "kind": "official",
        "title": "Test signal",
        "summaryZh": "测试技术动态。",
        "url": url,
        "source": "Official News",
        "sourceUrl": "https://example.com/feed.xml",
        "publishedAt": published_at,
        "score": score,
        "evidence": ["official_feed"],
        "sources": ["Official News"],
    }


def source_status(identifier: str, url: str) -> dict[str, object]:
    return {
        "id": identifier,
        "name": "Official News",
        "url": url,
        "state": "healthy",
        "itemCount": 1,
        "latestItemAt": "2026-07-10T11:00:00+00:00",
        "error": None,
    }


def empty_queue(generated_at: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "generatedAt": generated_at,
        "scope": {"projectLimit": 5, "signalLimit": 10},
        "pendingCount": 0,
        "projectPendingCount": 0,
        "signalPendingCount": 0,
        "completedProjectCount": 0,
        "completedSignalCount": 0,
        "items": [],
    }


def catalog_project(
    repository: str,
    captured_at: str,
    stars: int,
    growth_kind: str,
    growth_value: int,
    **overrides: object,
) -> dict[str, object]:
    project: dict[str, object] = {
        "slug": repository.lower().replace("/", "--"),
        "repo": repository,
        "title": "Test project",
        "description": "Test catalog project",
        "category": "开发工具",
        "language": "Python",
        "license": "MIT",
        "stars": stars,
        "growthValue": growth_value,
        "growthLabel": f"Test growth {growth_value:+d}",
        "growthKind": growth_kind,
        "globalScore": 80,
        "reuseScore": 70,
        "momentumScore": 75,
        "enduranceScore": 50,
        "heatTrack": "recent_momentum",
        "heatLabel": "近期动量 · 测试",
        "longTermEvidenceKind": None,
        "heatObservationCount": 1,
        "heatObservationWindow": 1,
        "trend": f"{growth_value:+d}",
        "analysisState": "事实初筛",
        "sourcePushedAt": captured_at,
        "analysisAnalyzedAt": None,
        "enrichmentAnalyzedAt": None,
        "whyNow": "Test project is included in the current snapshot.",
        "recommendation": "了解",
        "fit": "Schema and audit tests.",
        "reusePlan": "Review the evidence before reuse.",
        "risk": "Synthetic test fixture; do not treat it as production evidence.",
        "capabilities": ["契约验证"],
        "taskTerms": ["schema", "audit"],
        "evidence": [
            {
                "label": "GitHub",
                "detail": "Test evidence",
                "href": f"https://github.com/{repository}",
            }
        ],
        "capturedAt": f"{captured_at[:10]} 20:00 CST",
    }
    project.update(overrides)
    return project


def catalog_snapshot(
    captured_at: str,
    projects: list[dict[str, object]],
    *,
    previous_captured_at: str | None,
    observation_window: int,
    growth_mode: str,
    codex_pending_count: int = 0,
) -> dict[str, object]:
    daily = projects[:5]
    return {
        "schemaVersion": 1,
        "capturedAt": captured_at,
        "sourceCount": len(projects),
        "queryFailureCount": 0,
        "projectCount": len(projects),
        "deepAnalysisCount": sum(
            project["analysisState"] == "深度分析" for project in projects
        ),
        "pendingDeepAnalysis": [
            project["repo"]
            for project in daily
            if project["analysisState"] != "深度分析"
        ],
        "dailyTrackCounts": {
            "recentMomentum": sum(
                project["heatTrack"] == "recent_momentum" for project in daily
            ),
            "longTerm": sum(project["heatTrack"] == "long_term" for project in daily),
        },
        "heatHistory": {
            "snapshotCount": observation_window,
            "maximumSnapshotCount": 30,
            "minimumPersistenceSnapshots": 7,
            "verifiedLongTermCount": sum(
                project["longTermEvidenceKind"] == "multi_snapshot"
                for project in projects
            ),
        },
        "growthMode": growth_mode,
        "notice": "Synthetic catalog fixture for audit tests.",
        "projects": projects,
        "previousCapturedAt": previous_captured_at,
        "codexPendingCount": codex_pending_count,
    }


class AuditDataTests(unittest.TestCase):
    def test_verifies_exact_observed_growth_and_heat_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous_at = "2026-07-09T12:00:00+00:00"
            captured = "2026-07-10T12:00:00+00:00"
            previous = {
                "schema_version": 1,
                "captured_at": previous_at,
                "queries": ["stars:>=1"],
                "count": 1,
                "repositories": [
                    snapshot_repository("demo/tool", previous_at, 100, "stars:>=1")
                ],
            }
            current = {
                "schema_version": 1,
                "captured_at": captured,
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
                "repositories": [
                    snapshot_repository("demo/tool", captured, 120, "stars:>=1")
                ],
            }
            project = catalog_project(
                "demo/tool",
                captured,
                120,
                "observed",
                20,
                momentumScore=80,
                enduranceScore=90,
                heatTrack="long_term",
                heatLabel="长期高热 · 结构代理",
                longTermEvidenceKind="structural_proxy",
                heatObservationCount=2,
                heatObservationWindow=2,
            )
            catalog = catalog_snapshot(
                captured,
                [project],
                previous_captured_at=previous_at,
                observation_window=2,
                growth_mode="observed",
            )
            signals = {
                "schemaVersion": 1,
                "capturedAt": captured,
                "windowHours": 48,
                "signalCount": 1,
                "healthySourceCount": 1,
                "failedSourceCount": 0,
                "sourceStatus": [
                    source_status("official", "https://example.com/feed")
                ],
                "topSignals": [signal_item("signal-1", "https://example.com/news", captured)],
                "signals": [signal_item("signal-1", "https://example.com/news", captured)],
            }
            queue = empty_queue(captured)
            queue["scope"] = {"projectLimit": 0, "signalLimit": 0}
            write_json(root / "snapshots/history/previous.json", previous)
            write_json(root / "snapshots/latest.json", current)
            write_json(root / "catalog/latest.json", catalog)
            write_json(root / "signals/latest.json", signals)
            write_json(root / "queues/codex.json", queue)

            healthy = audit_data(root)
            project["growthValue"] = 19
            project["heatObservationCount"] = 1
            write_json(root / "catalog/latest.json", catalog)
            corrupted = audit_data(root)

        self.assertEqual(healthy["status"], "healthy")
        self.assertEqual(healthy["observedProjectCount"], 1)
        self.assertEqual(healthy["positiveGrowthProjectCount"], 1)
        self.assertEqual(healthy["observedNetStarChange"], 20)
        self.assertEqual(healthy["dailyTrackCounts"], {"recentMomentum": 0, "longTerm": 1})
        self.assertIn("observed_growth_mismatch", {item["code"] for item in corrupted["issues"]})
        self.assertIn("heat_observation_mismatch", {item["code"] for item in corrupted["issues"]})

    def test_accepts_consistent_first_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = "2026-07-10T12:00:00+00:00"
            query = "pushed:>=2026-07-01 stars:>=500 archived:false fork:false"
            repository = snapshot_repository("demo/tool", captured, 100, query)
            project = catalog_project(
                "demo/tool",
                captured,
                100,
                "velocity_proxy",
                100,
            )
            signal = signal_item(
                "signal-1",
                "https://example.com/news",
                "2026-07-10T11:00:00+00:00",
            )
            source = source_status("official-news", "https://example.com/feed.xml")
            snapshot = {
                "schema_version": 1,
                "captured_at": captured,
                "queries": [query],
                "query_status": [
                    {"query": query, "state": "healthy", "item_count": 1, "error": None}
                ],
                "successful_query_count": 1,
                "failed_query_count": 0,
                "count": 1,
                "repositories": [repository],
            }
            write_json(root / "snapshots/latest.json", snapshot)
            catalog_payload = catalog_snapshot(
                captured,
                [project],
                previous_captured_at=None,
                observation_window=1,
                growth_mode="first_observation_proxy",
            )
            signals_payload = {
                "schemaVersion": 1,
                "capturedAt": captured,
                "windowHours": 48,
                "signalCount": 1,
                "healthySourceCount": 1,
                "failedSourceCount": 0,
                "sourceStatus": [source],
                "topSignals": [signal],
                "signals": [signal],
            }
            write_json(root / "signals/latest.json", signals_payload)
            queue_payload = build_codex_queue(
                catalog_payload,
                signals_payload,
                root / "enrichment",
                root / "signals/enrichment.json",
                datetime.fromisoformat(captured),
            )
            catalog_payload["codexPendingCount"] = queue_payload["pendingCount"]
            write_json(root / "catalog/latest.json", catalog_payload)
            write_json(root / "queues/codex.json", queue_payload)

            result = audit_data(root)
            catalog_payload["analysisFailures"] = [
                {"repository": "demo/tool", "error": "clone timed out"}
            ]
            source["state"] = "failed"
            source["itemCount"] = 0
            source["latestItemAt"] = None
            source["error"] = "rate limited"
            signals_payload["healthySourceCount"] = 0
            signals_payload["failedSourceCount"] = 1
            write_json(root / "catalog/latest.json", catalog_payload)
            write_json(root / "signals/latest.json", signals_payload)
            degraded_coverage = audit_data(root)
            failed_query = "topic:productivity stars:>=50 archived:false fork:false"
            snapshot["queries"].append(failed_query)
            snapshot["query_status"].append(
                {
                    "query": failed_query,
                    "state": "failed",
                    "item_count": 0,
                    "error": "rate limited",
                }
            )
            snapshot["failed_query_count"] = 1
            catalog_payload["queryFailureCount"] = 1
            write_json(root / "snapshots/latest.json", snapshot)
            write_json(root / "catalog/latest.json", catalog_payload)
            partial_query_failure = audit_data(root)
            queue_payload = json.loads((root / "queues/codex.json").read_text(encoding="utf-8"))
            queue_payload["items"].reverse()
            write_json(root / "queues/codex.json", queue_payload)
            stale_queue = audit_data(root)
            snapshot["query_status"][0] = {
                "query": "stars:>=999999",
                "state": "failed",
                "item_count": 0,
                "error": "rate limited",
            }
            snapshot["successful_query_count"] = 0
            snapshot["failed_query_count"] = 2
            repository["candidate_query"] = "stars:>=999999"
            write_json(root / "snapshots/latest.json", snapshot)
            corrupted_queries = audit_data(root)

        self.assertEqual(result["status"], "healthy", result["issues"])
        self.assertEqual(result["errorCount"], 0)
        self.assertEqual(result["successfulQueryCount"], 1)
        self.assertEqual(result["failedQueryCount"], 0)
        self.assertEqual(degraded_coverage["status"], "degraded")
        degraded_codes = {item["code"] for item in degraded_coverage["issues"]}
        self.assertIn("partial_static_analysis_failure", degraded_codes)
        self.assertIn("partial_signal_source_failure", degraded_codes)
        self.assertEqual(partial_query_failure["status"], "degraded")
        self.assertIn(
            "partial_query_failure",
            {item["code"] for item in partial_query_failure["issues"]},
        )
        self.assertIn("stale_queue_items", {item["code"] for item in stale_queue["issues"]})
        corrupted_query_codes = {item["code"] for item in corrupted_queries["issues"]}
        self.assertIn("query_status_coverage_mismatch", corrupted_query_codes)
        self.assertIn("catalog_query_failure_count_mismatch", corrupted_query_codes)
        self.assertIn("unknown_candidate_query", corrupted_query_codes)

    def test_reports_schema_valid_count_time_and_window_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_at = "2026-07-10T12:00:00+00:00"
            query = "stars:>=1"
            repository = snapshot_repository("demo/tool", snapshot_at, 100, query)
            snapshot = {
                "schema_version": 1,
                "captured_at": snapshot_at,
                "queries": [query],
                "query_status": [
                    {"query": query, "state": "healthy", "item_count": 1, "error": None}
                ],
                "successful_query_count": 1,
                "failed_query_count": 0,
                "count": 2,
                "repositories": [repository],
            }
            project = catalog_project(
                "demo/tool",
                snapshot_at,
                100,
                "velocity_proxy",
                100,
            )
            catalog = catalog_snapshot(
                "2026-07-10T11:00:00+00:00",
                [project],
                previous_captured_at=None,
                observation_window=1,
                growth_mode="first_observation_proxy",
            )
            signal = signal_item(
                "signal-1",
                "https://example.com/news",
                "2026-07-01T00:00:00+00:00",
            )
            signals = {
                "schemaVersion": 1,
                "capturedAt": snapshot_at,
                "windowHours": 1,
                "signalCount": 2,
                "healthySourceCount": 0,
                "failedSourceCount": 0,
                "sourceStatus": [source_status("official", "https://example.com/feed")],
                "topSignals": [signal],
                "signals": [signal],
            }
            queue = empty_queue(snapshot_at)
            queue["scope"] = {"projectLimit": 0, "signalLimit": 0}
            queue["pendingCount"] = 1

            write_json(root / "snapshots/latest.json", snapshot)
            write_json(root / "catalog/latest.json", catalog)
            write_json(root / "signals/latest.json", signals)
            write_json(root / "queues/codex.json", queue)

            result = audit_data(root)

        codes = {item["code"] for item in result["issues"]}
        self.assertEqual(result["status"], "failed")
        self.assertTrue(
            {
                "snapshot_count_mismatch",
                "catalog_snapshot_mismatch",
                "signal_count_mismatch",
                "signal_outside_window",
                "healthy_source_count_mismatch",
                "queue_count_mismatch",
            }.issubset(codes)
        )

    def test_rejects_non_finite_signal_payloads_before_semantic_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = "2026-07-10T12:00:00+00:00"
            write_json(
                root / "snapshots/latest.json",
                {"captured_at": captured, "count": 0, "repositories": []},
            )
            write_json(
                root / "catalog/latest.json",
                {
                    "capturedAt": captured,
                    "sourceCount": 0,
                    "projectCount": 0,
                    "projects": [],
                },
            )
            write_json(
                root / "signals/latest.json",
                {
                    "capturedAt": captured,
                    "windowHours": 48,
                    "signalCount": 3,
                    "healthySourceCount": 1,
                    "failedSourceCount": 0,
                    "sourceStatus": [
                        {"id": "source", "url": "https://example.com/feed", "state": "healthy"}
                    ],
                    "signals": [
                        {
                            "id": "nan",
                            "url": "https://example.com/nan",
                            "publishedAt": captured,
                            "score": float("nan"),
                        },
                        {
                            "id": "infinite",
                            "url": "https://example.com/infinite",
                            "publishedAt": captured,
                            "score": float("inf"),
                        },
                        {
                            "id": "too-high",
                            "url": "https://example.com/high",
                            "publishedAt": captured,
                            "score": 1.01,
                        },
                    ],
                },
            )
            write_json(
                root / "queues/codex.json",
                {
                    "generatedAt": captured,
                    "pendingCount": 0,
                    "projectPendingCount": 0,
                    "signalPendingCount": 0,
                    "items": [],
                },
            )

            result = audit_data(root)

        matching = [item for item in result["issues"] if item["code"] == "invalid_artifact"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(matching), 1)
        self.assertIn("non-finite JSON number", matching[0]["detail"])


if __name__ == "__main__":
    unittest.main()
