from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pipeline.collect_github import candidate_queries, collect


class StubClient:
    def search(self, query: str, per_page: int = 30):
        return [
            {
                "full_name": "demo/repo",
                "html_url": "https://github.com/demo/repo",
                "description": "Demo",
                "owner": {"login": "demo"},
                "language": "Python",
                "license": {"spdx_id": "MIT"},
                "topics": ["productivity"],
                "stargazers_count": 120,
                "forks_count": 10,
                "open_issues_count": 2,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-07-01T00:00:00Z",
                "pushed_at": "2026-07-01T00:00:00Z",
                "default_branch": "main",
            }
        ]


class CollectGitHubTests(unittest.TestCase):
    def test_queries_include_new_and_maintained_projects(self) -> None:
        queries = candidate_queries(datetime(2026, 7, 10, tzinfo=timezone.utc))
        self.assertTrue(any("created:>=2026-06-26" in query for query in queries))
        self.assertTrue(any("pushed:>=2026-07-03" in query for query in queries))

    def test_deduplicates_repositories_across_queries(self) -> None:
        snapshot = collect(StubClient(), datetime(2026, 7, 10, tzinfo=timezone.utc))
        self.assertEqual(snapshot["count"], 1)
        self.assertEqual(snapshot["repositories"][0]["repo"], "demo/repo")
        self.assertIn(" | ", snapshot["repositories"][0]["candidate_query"])


if __name__ == "__main__":
    unittest.main()
