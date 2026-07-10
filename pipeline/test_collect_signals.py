from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from pipeline.collect_signals import (
    AI_RADAR_URL,
    DAILY_RANK_URL,
    HELLOGITHUB_RELEASE_URL,
    OFFICIAL_FEEDS,
    collect_signals,
)


class StubClient:
    def __init__(self):
        rss = b"""<?xml version='1.0'?><rss><channel><title>Demo</title><item><title>New AI agent release</title><link>https://example.com/item?utm_source=test</link><pubDate>Fri, 10 Jul 2026 11:00:00 GMT</pubDate></item></channel></rss>"""
        self.responses = {url: rss for _, _, url in OFFICIAL_FEEDS}
        self.responses[AI_RADAR_URL] = json.dumps(
            {
                "items": [
                    {
                        "title": "New AI agent release",
                        "primary_url": "https://example.com/item",
                        "latest_at": "2026-07-10T11:00:00Z",
                        "importance_score": 0.91,
                        "source_names": ["Demo Official"],
                        "source_count": 1,
                        "category": "official",
                        "reasons": ["official_source"],
                    }
                ]
            }
        ).encode()
        self.responses[DAILY_RANK_URL] = """## 2026.07.10 日榜排行
| 排名 | 项目名 | Star⭐ | 今日增长量 |
| 1 | [demo/repo](https://github.com/demo/repo)| 1.2k | 300 |
""".encode()
        self.responses[HELLOGITHUB_RELEASE_URL] = json.dumps(
            {
                "tag_name": "vol.123",
                "published_at": "2026-07-01T00:00:00Z",
                "html_url": "https://github.com/521xueweihan/HelloGitHub/releases/tag/vol.123",
            }
        ).encode()

    def get(self, url: str, accept: str = "*/*") -> bytes:
        return self.responses[url]


class CollectSignalsTests(unittest.TestCase):
    def test_merges_duplicate_official_and_aggregated_urls(self) -> None:
        payload = collect_signals(
            StubClient(),
            datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
        )

        matching = [item for item in payload["signals"] if item["url"].startswith("https://example.com/item")]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["kind"], "official")
        self.assertGreaterEqual(matching[0]["score"], 0.91)
        self.assertIn("Demo Official", matching[0]["sources"])

    def test_keeps_external_rank_as_verification_signal(self) -> None:
        payload = collect_signals(
            StubClient(),
            datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
        )

        ranked = next(item for item in payload["signals"] if item.get("repo") == "demo/repo")
        self.assertEqual(ranked["reportedDailyGrowth"], 300)
        self.assertIn("快照验证", ranked["summaryZh"])

    def test_reports_source_health_and_curated_release(self) -> None:
        payload = collect_signals(
            StubClient(),
            datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(payload["failedSourceCount"], 0)
        self.assertEqual(payload["healthySourceCount"], 6)
        self.assertTrue(any(item["source"] == "HelloGitHub" for item in payload["signals"]))


if __name__ == "__main__":
    unittest.main()
