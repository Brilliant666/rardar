from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pipeline.codex_queue import build_codex_queue


class CodexQueueTests(unittest.TestCase):
    def test_only_incomplete_items_enter_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            enrichment_dir = root / "enrichment"
            enrichment_dir.mkdir()
            complete_project = {
                "repository": "owner/complete",
                "titleZh": "完整项目",
                "summaryZh": "摘要",
                "capabilities": ["能力"],
                "taskTerms": ["任务"],
                "reusePlan": "复用",
                "limitation": "风险",
            }
            (enrichment_dir / "owner--complete.json").write_text(json.dumps(complete_project), encoding="utf-8")
            signal_path = root / "signals.json"
            signal_path.write_text(
                json.dumps(
                    {
                        "items": {
                            "https://complete.example": {
                                "titleZh": "标题",
                                "takeawayZh": "要点",
                                "whyItMattersZh": "价值",
                                "categoryZh": "分类",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            queue = build_codex_queue(
                {"projects": [{"repo": "owner/complete"}, {"repo": "owner/pending"}]},
                {
                    "signals": [
                        {"id": "complete", "url": "https://complete.example"},
                        {"id": "pending", "url": "https://pending.example", "title": "Pending"},
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


if __name__ == "__main__":
    unittest.main()
