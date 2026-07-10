from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from pipeline.runtime import heartbeat_is_fresh, parse_node_version


class RuntimeTests(unittest.TestCase):
    def test_parses_node_version(self) -> None:
        self.assertEqual(parse_node_version("v22.13.1\n"), (22, 13, 1))
        self.assertEqual(parse_node_version("22.14.0-beta"), (22, 14, 0))
        self.assertIsNone(parse_node_version("unknown"))

    def test_heartbeat_requires_recent_timestamp(self) -> None:
        now = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
        self.assertTrue(heartbeat_is_fresh((now - timedelta(seconds=20)).isoformat(), now))
        self.assertFalse(heartbeat_is_fresh((now - timedelta(seconds=60)).isoformat(), now))
        self.assertFalse(heartbeat_is_fresh(None, now))


if __name__ == "__main__":
    unittest.main()
