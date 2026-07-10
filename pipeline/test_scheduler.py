from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pipeline.scheduler import next_run_at, parse_clock


class SchedulerTests(unittest.TestCase):
    def test_next_run_uses_shanghai_clock(self) -> None:
        now = datetime(2026, 7, 10, 1, tzinfo=timezone.utc)  # 09:00 Asia/Shanghai
        target = next_run_at(now, 8, 0, "Asia/Shanghai")
        self.assertEqual(target, datetime(2026, 7, 11, 0, tzinfo=timezone.utc))

    def test_future_time_can_run_same_day(self) -> None:
        now = datetime(2026, 7, 10, 1, tzinfo=timezone.utc)  # 09:00 Asia/Shanghai
        target = next_run_at(now, 10, 30, "Asia/Shanghai")
        self.assertEqual(target, datetime(2026, 7, 10, 2, 30, tzinfo=timezone.utc))

    def test_rejects_invalid_clock(self) -> None:
        with self.assertRaises(ValueError):
            parse_clock("25:00")


if __name__ == "__main__":
    unittest.main()
