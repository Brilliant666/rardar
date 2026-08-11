from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline.stable_read as stable_read_module
from pipeline.stable_read import StableReadError, stable_read


class StableReadTests(unittest.TestCase):
    def _read_while_mutating(
        self,
        path: Path,
        mutate,
        *,
        max_attempts: int = 1,
    ):
        ready_to_mutate = threading.Event()
        mutation_done = threading.Event()
        writer_errors: list[BaseException] = []
        original = stable_read_module._read_regular_snapshot
        snapshots = 0

        def controlled_snapshot(target: Path):
            nonlocal snapshots
            snapshot = original(target)
            snapshots += 1
            if snapshots == 1:
                ready_to_mutate.set()
                if not mutation_done.wait(5):
                    raise AssertionError("writer did not complete the synchronized mutation")
            return snapshot

        def writer() -> None:
            try:
                if not ready_to_mutate.wait(5):
                    raise AssertionError("reader did not complete snapshot A")
                mutate()
            except BaseException as error:
                writer_errors.append(error)
            finally:
                mutation_done.set()

        thread = threading.Thread(target=writer, name="stable-read-test-writer")
        thread.start()
        try:
            with patch.object(
                stable_read_module,
                "_read_regular_snapshot",
                side_effect=controlled_snapshot,
            ):
                result = stable_read(path, max_attempts=max_attempts)
        finally:
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "mutation writer did not terminate")
        if writer_errors:
            raise writer_errors[0]
        return result

    def test_same_inode_same_length_in_place_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.bin"
            path.write_bytes(b"AAAA")
            inode_before = path.stat().st_ino

            def mutate() -> None:
                with path.open("r+b") as handle:
                    handle.write(b"BBBB")
                    handle.flush()
                    os.fsync(handle.fileno())

            with self.assertRaises(StableReadError) as raised:
                self._read_while_mutating(path, mutate)

            self.assertEqual(raised.exception.reason, "concurrent_change")
            self.assertEqual(path.stat().st_ino, inode_before)
            self.assertEqual(path.read_bytes(), b"BBBB")

    def test_same_length_mutation_with_restored_mtime_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.bin"
            path.write_bytes(b"AAAA")
            before = path.stat()

            def mutate() -> None:
                with path.open("r+b") as handle:
                    handle.write(b"BBBB")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

            with self.assertRaises(StableReadError) as raised:
                self._read_while_mutating(path, mutate)

            self.assertEqual(raised.exception.reason, "concurrent_change")
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_unchanged_content_passes_with_content_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.bin"
            content = b"stable evidence\n"
            path.write_bytes(content)

            result = stable_read(path)

            self.assertEqual(result.content, content)
            self.assertEqual(result.sha256, hashlib.sha256(content).hexdigest())

    def test_atomic_replace_retries_to_one_complete_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "current.json"
            replacement = root / "replacement.json"
            old = b'{"generationId":"old"}\n'
            new = b'{"generationId":"new"}\n'
            path.write_bytes(old)
            replacement.write_bytes(new)

            result = self._read_while_mutating(
                path,
                lambda: os.replace(replacement, path),
                max_attempts=2,
            )

            self.assertIn(result.content, {old, new})
            self.assertEqual(result.content, new)

    def test_symlink_swap_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "evidence.bin"
            held = root / "held.bin"
            target = root / "target.bin"
            path.write_bytes(b"AAAA")
            target.write_bytes(b"BBBB")

            def mutate() -> None:
                os.replace(path, held)
                try:
                    path.symlink_to(target)
                except (NotImplementedError, OSError):
                    os.replace(held, path)
                    raise unittest.SkipTest("file symlinks are unavailable")

            with self.assertRaises(StableReadError) as raised:
                self._read_while_mutating(path, mutate, max_attempts=2)

            self.assertEqual(raised.exception.reason, "unsafe_type")

    def test_delete_and_recreate_fails_closed_without_pointer_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.bin"
            path.write_bytes(b"AAAA")

            def mutate() -> None:
                path.unlink()
                path.write_bytes(b"BBBB")

            with self.assertRaises(StableReadError) as raised:
                self._read_while_mutating(path, mutate)

            self.assertEqual(raised.exception.reason, "concurrent_change")

    def test_expected_sha256_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.json"
            path.write_bytes(b"{}\n")

            with self.assertRaises(StableReadError) as raised:
                stable_read(path, expected_sha256="0" * 64)

            self.assertEqual(raised.exception.reason, "digest_mismatch")


if __name__ == "__main__":
    unittest.main()
