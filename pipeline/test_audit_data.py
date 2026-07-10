from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline.audit_data import audit_data


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class AuditDataTests(unittest.TestCase):
    def test_verifies_exact_observed_growth_and_heat_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous_at = "2026-07-09T12:00:00+00:00"
            captured = "2026-07-10T12:00:00+00:00"
            previous = {
                "captured_at": previous_at,
                "count": 1,
                "repositories": [{"repo": "demo/tool", "stars": 100}],
            }
            current = {
                "captured_at": captured,
                "count": 1,
                "repositories": [{"repo": "demo/tool", "stars": 120}],
            }
            project = {
                "repo": "demo/tool",
                "slug": "demo--tool",
                "stars": 120,
                "growthKind": "observed",
                "growthValue": 20,
                "momentumScore": 80,
                "enduranceScore": 90,
                "heatTrack": "long_term",
                "heatLabel": "长期高热 · 结构代理",
                "evidence": [{"href": "https://github.com/demo/tool"}],
            }
            catalog = {
                "capturedAt": captured,
                "sourceCount": 1,
                "projectCount": 1,
                "previousCapturedAt": previous_at,
                "dailyTrackCounts": {"recentMomentum": 0, "longTerm": 1},
                "projects": [project],
            }
            signals = {
                "capturedAt": captured,
                "windowHours": 48,
                "signalCount": 1,
                "healthySourceCount": 1,
                "failedSourceCount": 0,
                "sourceStatus": [
                    {"id": "official", "url": "https://example.com/feed", "state": "healthy"}
                ],
                "signals": [
                    {
                        "id": "signal-1",
                        "url": "https://example.com/news",
                        "publishedAt": captured,
                    }
                ],
            }
            queue = {
                "generatedAt": captured,
                "pendingCount": 0,
                "projectPendingCount": 0,
                "signalPendingCount": 0,
                "items": [],
            }
            write_json(root / "snapshots/history/previous.json", previous)
            write_json(root / "snapshots/latest.json", current)
            write_json(root / "catalog/latest.json", catalog)
            write_json(root / "signals/latest.json", signals)
            write_json(root / "queues/codex.json", queue)

            healthy = audit_data(root)
            project["growthValue"] = 19
            write_json(root / "catalog/latest.json", catalog)
            corrupted = audit_data(root)

        self.assertEqual(healthy["status"], "healthy")
        self.assertEqual(healthy["observedProjectCount"], 1)
        self.assertEqual(healthy["positiveGrowthProjectCount"], 1)
        self.assertEqual(healthy["observedNetStarChange"], 20)
        self.assertEqual(healthy["dailyTrackCounts"], {"recentMomentum": 0, "longTerm": 1})
        self.assertIn("observed_growth_mismatch", {item["code"] for item in corrupted["issues"]})

    def test_accepts_consistent_first_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = "2026-07-10T12:00:00+00:00"
            repository = {"repo": "demo/tool", "stars": 100}
            project = {
                "repo": "demo/tool",
                "slug": "demo--tool",
                "growthKind": "velocity_proxy",
                "stars": 100,
                "evidence": [{"href": "https://github.com/demo/tool"}],
            }
            signal = {
                "id": "signal-1",
                "url": "https://example.com/news",
                "publishedAt": "2026-07-10T11:00:00+00:00",
            }
            source = {
                "id": "official-news",
                "url": "https://example.com/feed.xml",
                "state": "healthy",
            }
            write_json(root / "snapshots/latest.json", {"captured_at": captured, "count": 1, "repositories": [repository]})
            write_json(root / "catalog/latest.json", {"capturedAt": captured, "sourceCount": 1, "projectCount": 1, "previousCapturedAt": None, "projects": [project]})
            write_json(
                root / "signals/latest.json",
                {
                    "capturedAt": captured,
                    "windowHours": 48,
                    "signalCount": 1,
                    "healthySourceCount": 1,
                    "failedSourceCount": 0,
                    "sourceStatus": [source],
                    "signals": [signal],
                },
            )
            write_json(root / "queues/codex.json", {"generatedAt": captured, "pendingCount": 0, "projectPendingCount": 0, "signalPendingCount": 0, "items": []})

            result = audit_data(root)

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["errorCount"], 0)

    def test_reports_count_time_url_and_window_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_at = "2026-07-10T12:00:00+00:00"
            write_json(root / "snapshots/latest.json", {"captured_at": snapshot_at, "count": "two", "repositories": [{"repo": "demo/tool"}]})
            write_json(root / "catalog/latest.json", {"capturedAt": "2026-07-10T11:00:00+00:00", "sourceCount": 1, "projectCount": 1, "projects": [{"repo": "demo/tool", "slug": "demo--tool", "evidence": [{"href": "javascript:alert(1)"}]}]})
            write_json(
                root / "signals/latest.json",
                {
                    "capturedAt": snapshot_at,
                    "windowHours": "broken",
                    "signalCount": 2,
                    "healthySourceCount": 1,
                    "failedSourceCount": 0,
                    "sourceStatus": [{"id": "bad", "url": "//missing-scheme", "state": "unknown"}],
                    "signals": [{"id": "one", "url": "data:text/plain,bad", "publishedAt": "2026-07-01T00:00:00+00:00"}],
                },
            )
            write_json(root / "queues/codex.json", {"generatedAt": snapshot_at, "pendingCount": 1, "projectPendingCount": 0, "signalPendingCount": 0, "items": []})

            result = audit_data(root)

        codes = {item["code"] for item in result["issues"]}
        self.assertEqual(result["status"], "failed")
        self.assertTrue(
            {
                "snapshot_count_mismatch",
                "catalog_snapshot_mismatch",
                "unsafe_evidence_url",
                "signal_count_mismatch",
                "unsafe_signal_url",
                "invalid_signal_window",
                "signal_outside_window",
                "healthy_source_count_mismatch",
                "unsafe_source_url",
                "invalid_source_state",
                "queue_count_mismatch",
            }.issubset(codes)
        )


if __name__ == "__main__":
    unittest.main()
