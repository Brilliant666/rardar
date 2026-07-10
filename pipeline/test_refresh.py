from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.refresh import refresh


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


if __name__ == "__main__":
    unittest.main()
