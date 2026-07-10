from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.refresh import _write_json_batch, refresh
from pipeline.scheduler import committed_refresh_at


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
    def test_successful_full_refresh_writes_a_consistent_commit_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            now = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
            signals = {
                "schemaVersion": 1,
                "capturedAt": now.isoformat(),
                "windowHours": 48,
                "signalCount": 0,
                "healthySourceCount": 0,
                "failedSourceCount": 0,
                "sourceStatus": [],
                "topSignals": [],
                "signals": [],
            }

            with patch("pipeline.refresh.collect_signals", return_value=signals):
                refresh(
                    data_dir,
                    now,
                    analyze_top=0,
                    client=StubClient(100),
                    collect_external_signals=True,
                )

            self.assertEqual(committed_refresh_at(data_dir), now.isoformat())

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

    def test_second_refresh_archives_previous_and_reports_observed_growth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            refresh(
                data_dir,
                datetime(2026, 7, 9, 12, tzinfo=timezone.utc),
                analyze_top=0,
                client=StubClient(100),
                collect_external_signals=False,
            )
            catalog = refresh(
                data_dir,
                datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
                analyze_top=0,
                client=StubClient(140),
                collect_external_signals=False,
            )

            history = list((data_dir / "snapshots" / "history").glob("*.json"))
            latest = json.loads((data_dir / "snapshots" / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(history), 1)
            self.assertEqual(latest["repositories"][0]["stars"], 140)
            self.assertEqual(catalog["projects"][0]["growthKind"], "observed")
            self.assertEqual(catalog["projects"][0]["growthValue"], 40)
            self.assertEqual(catalog["previousCapturedAt"], "2026-07-09T12:00:00+00:00")

    def test_failed_derived_collection_does_not_advance_snapshot_or_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            refresh(
                data_dir,
                datetime(2026, 7, 9, 12, tzinfo=timezone.utc),
                analyze_top=0,
                client=StubClient(100),
                collect_external_signals=False,
            )
            tracked_paths = [
                data_dir / "snapshots" / "latest.json",
                data_dir / "catalog" / "latest.json",
                data_dir / "queues" / "codex.json",
            ]
            before = {path: path.read_bytes() for path in tracked_paths}

            with patch("pipeline.refresh.collect_signals", side_effect=RuntimeError("signal parser failed")):
                with self.assertRaisesRegex(RuntimeError, "signal parser failed"):
                    refresh(
                        data_dir,
                        datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
                        analyze_top=0,
                        client=StubClient(140),
                        collect_external_signals=True,
                    )

            self.assertEqual({path: path.read_bytes() for path in tracked_paths}, before)
            self.assertEqual(list((data_dir / "snapshots" / "history").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
