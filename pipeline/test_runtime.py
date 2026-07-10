from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.runtime import (
    acquire_manager_lock,
    default_runtime_dir,
    heartbeat_is_fresh,
    parse_node_version,
    release_manager_lock,
)


class RuntimeTests(unittest.TestCase):
    def test_manager_lock_allows_only_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "manager.lock"
            first = acquire_manager_lock(lock_path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(acquire_manager_lock(lock_path))
            finally:
                release_manager_lock(first)

            second = acquire_manager_lock(lock_path)
            self.assertIsNotNone(second)
            release_manager_lock(second)

    def test_parses_node_version(self) -> None:
        self.assertEqual(parse_node_version("v22.13.1\n"), (22, 13, 1))
        self.assertEqual(parse_node_version("22.14.0-beta"), (22, 14, 0))
        self.assertIsNone(parse_node_version("unknown"))

    def test_heartbeat_requires_recent_timestamp(self) -> None:
        now = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
        self.assertTrue(heartbeat_is_fresh((now - timedelta(seconds=20)).isoformat(), now))
        self.assertFalse(heartbeat_is_fresh((now - timedelta(seconds=60)).isoformat(), now))
        self.assertFalse(heartbeat_is_fresh(None, now))

    def test_runtime_directory_can_live_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {"RARDAR_RUNTIME_DIR": temporary}):
            self.assertEqual(default_runtime_dir(), Path(temporary).resolve())


if __name__ == "__main__":
    unittest.main()
