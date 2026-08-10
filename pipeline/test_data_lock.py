from __future__ import annotations

import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.data_lock import data_dir_lock, data_dir_lock_path
from pipeline.runtime_settings import RuntimeSettingsError


def _hold_data_lock(data_dir: str, lock_root: str, acquired_path: str, release_path: str) -> None:
    with data_dir_lock(Path(data_dir), lock_root=Path(lock_root)):
        Path(acquired_path).write_text("locked", encoding="utf-8")
        while not Path(release_path).exists():
            time.sleep(0.01)


class DataDirectoryLockTests(unittest.TestCase):
    def test_canonical_paths_share_one_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            lock_root = root / "locks"

            self.assertEqual(
                data_dir_lock_path(data_dir, lock_root),
                data_dir_lock_path(data_dir / ".." / "data", lock_root),
            )

    def test_runtime_directory_override_does_not_split_the_data_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            lock_root = root / "shared-locks"
            with patch.dict(
                os.environ,
                {
                    "RARDAR_RUNTIME_DIR": str(root / "custom"),
                    "RARDAR_DATA_LOCK_DIR": str(lock_root),
                },
            ):
                customized = data_dir_lock_path(data_dir)
            with patch.dict(
                os.environ,
                {"RARDAR_DATA_LOCK_DIR": str(lock_root)},
                clear=False,
            ):
                os.environ.pop("RARDAR_RUNTIME_DIR", None)
                default = data_dir_lock_path(data_dir)

            self.assertEqual(customized, default)

    def test_explicit_lock_directory_prevents_home_based_split_brain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            data_dir = root / "data"
            data_dir.mkdir()
            shared = root / "shared-locks"
            paths = []
            for home in (root / "home-a", root / "home-b"):
                with patch.dict(
                    os.environ,
                    {
                        "HOME": str(home),
                        "LOCALAPPDATA": str(home / "local"),
                        "XDG_STATE_HOME": str(home / "state"),
                        "RARDAR_DATA_LOCK_DIR": str(shared),
                    },
                    clear=False,
                ):
                    paths.append(data_dir_lock_path(data_dir))

            self.assertEqual(paths[0], paths[1])
            self.assertEqual(paths[0].parent, shared)

    def test_relative_lock_directories_fail_before_creating_a_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory).resolve() / "data"
            data_dir.mkdir()
            with (
                patch.dict(os.environ, {"RARDAR_DATA_LOCK_DIR": "relative-locks"}),
                self.assertRaises(RuntimeSettingsError),
            ):
                data_dir_lock_path(data_dir)
            with self.assertRaises(RuntimeSettingsError):
                data_dir_lock_path(data_dir, Path("relative-locks"))

    def test_lock_excludes_a_second_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            lock_root = root / "locks"
            acquired_path = root / "acquired"
            release_path = root / "release"
            process = multiprocessing.get_context("spawn").Process(
                target=_hold_data_lock,
                args=(str(data_dir), str(lock_root), str(acquired_path), str(release_path)),
            )
            process.start()
            try:
                deadline = time.monotonic() + 10
                while not acquired_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(acquired_path.exists(), "child did not acquire the data lock")

                with self.assertRaises(TimeoutError):
                    with data_dir_lock(data_dir, lock_root=lock_root, timeout=0.15):
                        self.fail("a second process entered the locked data directory")
            finally:
                release_path.write_text("release", encoding="utf-8")
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)

            self.assertEqual(process.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
