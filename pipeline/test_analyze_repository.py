from __future__ import annotations

import ctypes
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import stat
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from pipeline import analyze_repository as analyzer
from pipeline.analyze_repository import (
    ArchiveExtractionSummary,
    RemoteCloneLifecycleError,
    _analyze_remote_in_temporary_root,
    _download_source_archive,
    _evidence_payload,
    _extract_source_archive,
    _git_environment,
    _is_test_file,
    _run_bounded_clone,
    _validate_repo,
    analyze_path,
    analyze_remote,
)


def _pid_is_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _wait_for_pid_exit(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_running(pid)


def _terminate_test_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _terminate_test_pid(pid: int) -> None:
    if not _pid_is_running(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
    else:
        os.kill(pid, analyzer.signal.SIGKILL)
    _wait_for_pid_exit(pid)


def _zip_payload(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return output.getvalue()


class _FakeResponse:
    def __init__(self, payload: bytes, *, content_length: str | None = None) -> None:
        self._stream = io.BytesIO(payload)
        self.headers = {} if content_length is None else {"Content-Length": content_length}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self._stream.close()

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


class _InterruptingReader:
    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped
        self._reads = 0

    def __enter__(self) -> _InterruptingReader:
        return self

    def __exit__(self, *_args: object) -> None:
        self._wrapped.close()

    def read(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._reads > 1:
            raise OSError("simulated archive read interruption")
        return self._wrapped.read(size)


class AnalyzeRepositoryTests(unittest.TestCase):
    def test_local_payload_keeps_required_nullable_license_fact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = analyze_path(Path(directory), "local")

        payload = _evidence_payload(evidence)
        self.assertIn("license_hint", payload)
        self.assertIsNone(payload["license_hint"])
        self.assertNotIn("projectIdVersion", payload)
        self.assertNotIn("projectId", payload)

    def test_remote_repository_validation_preserves_literal_dot_git_name(self) -> None:
        self.assertEqual(_validate_repo("owner/repo.git"), "owner/repo.git")

    def test_remote_repository_validation_does_not_guess_from_url(self) -> None:
        with self.assertRaises(ValueError):
            _validate_repo("https://github.com/owner/repo.git")

    def test_symbolic_links_cannot_escape_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            outside = Path(directory) / "outside.py"
            outside.write_text("# TODO secret outside checkout", encoding="utf-8")
            link = root / "linked.py"
            try:
                link.symlink_to(outside)
            except OSError:
                # Windows may require elevated symlink privileges. Keep the
                # policy test deterministic by making this path report itself
                # as a link while retaining a normal file underneath.
                link.write_text(outside.read_text(encoding="utf-8"), encoding="utf-8")
                path_type = type(link)
                original_is_symlink = path_type.is_symlink

                def report_link(path: Path) -> bool:
                    return path.name == link.name or original_is_symlink(path)

                with patch.object(path_type, "is_symlink", new=report_link):
                    evidence = analyze_path(root, "demo/repo")
            else:
                evidence = analyze_path(root, "demo/repo")

            self.assertEqual(evidence.scanned_files, 0)
            self.assertEqual(evidence.counts["todo_markers"], 0)
            self.assertNotIn(".py", evidence.language_files)

    def test_remote_clone_ignores_user_git_rewrites_and_prompts(self) -> None:
        environment = _git_environment()
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertTrue(environment["GIT_CONFIG_GLOBAL"])

    def test_extracts_static_evidence_without_running_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("# Demo", encoding="utf-8")
            (root / "LICENSE").write_text("MIT License", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname='demo'", encoding="utf-8")
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_demo.py").write_text("# TODO: add edge case", encoding="utf-8")
            (root / "latest.json").write_text("{}", encoding="utf-8")
            (root / "debug.log").write_text("test should be ignored", encoding="utf-8")

            evidence = analyze_path(root, "demo/repo")

            self.assertEqual(evidence.repository, "demo/repo")
            self.assertTrue(evidence.indicators["readme"])
            self.assertTrue(evidence.indicators["license"])
            self.assertTrue(evidence.indicators["tests"])
            self.assertEqual(evidence.counts["test_files"], 1)
            self.assertNotIn(".log", evidence.language_files)
            self.assertEqual(evidence.license_hint, "MIT")
            self.assertEqual(evidence.counts["todo_markers"], 1)
            self.assertIn("static inspection only; code was not executed", evidence.warnings)

    def test_test_file_detection_avoids_latest_false_positive(self) -> None:
        self.assertFalse(_is_test_file("data/latest.json"))
        self.assertTrue(_is_test_file("tests/demo.py"))
        self.assertTrue(_is_test_file("src/widget.test.ts"))
        self.assertTrue(_is_test_file("pkg/worker_test.go"))

    def test_bounded_archive_extraction_strips_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "source.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("demo-main/README.md", "# Demo")
                archive.writestr("demo-main/tests/test_demo.py", "def test_demo(): pass")
                archive.writestr("demo-main/image.png", b"ignored")
                link = zipfile.ZipInfo("demo-main/linked.py")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, "tests/test_demo.py")
            checkout = root / "checkout"

            _extract_source_archive(archive_path, checkout)

            self.assertEqual((checkout / "README.md").read_text(encoding="utf-8"), "# Demo")
            self.assertTrue((checkout / "tests/test_demo.py").exists())
            self.assertFalse((checkout / "image.png").exists())
            self.assertFalse((checkout / "linked.py").exists())

    def test_archive_extraction_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("demo-main/../../escape.txt", "unsafe")

            with self.assertRaisesRegex(RuntimeError, "unsafe source archive path"):
                _extract_source_archive(archive_path, root / "checkout")

            self.assertFalse((root / "escape.txt").exists())

    def test_archive_tail_traversal_is_rejected_before_checkout_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "unsafe-tail.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("demo-main/README.md", "# Demo")
                archive.writestr("demo-main/src/app.py", "print('safe')")
                archive.writestr("demo-main/docs/../../escape.txt", "unsafe")
            checkout = root / "checkout"

            with self.assertRaisesRegex(RuntimeError, "unsafe source archive path"):
                _extract_source_archive(archive_path, checkout)

            self.assertFalse(os.path.lexists(checkout))
            self.assertFalse((root / "escape.txt").exists())

    def test_filtered_and_symlink_members_still_receive_full_path_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for kind in ("filtered", "symlink"):
                with self.subTest(kind=kind):
                    archive_path = root / f"{kind}.zip"
                    with zipfile.ZipFile(archive_path, "w") as archive:
                        archive.writestr("demo-main/README.md", "# Demo")
                        if kind == "filtered":
                            archive.writestr("demo-main/docs/../../escape.png", b"unsafe")
                        else:
                            link = zipfile.ZipInfo("demo-main/docs/../../escape.py")
                            link.create_system = 3
                            link.external_attr = (stat.S_IFLNK | 0o777) << 16
                            archive.writestr(link, "README.md")
                    checkout = root / f"{kind}-checkout"
                    with self.assertRaisesRegex(RuntimeError, "unsafe source archive path"):
                        _extract_source_archive(archive_path, checkout)
                    self.assertFalse(os.path.lexists(checkout))
                    self.assertEqual(list(root.glob(f".{checkout.name}.partial-*")), [])

    def test_archive_selection_is_deterministic_and_independent_of_member_order(self) -> None:
        entries = [
            ("demo-main/z.py", b"z"),
            ("demo-main/a.py", b"a"),
            ("demo-main/m.py", b"m"),
            ("demo-main/b.py", b"b"),
            ("demo-main/image.png", b"ignored"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.zip"
            second = root / "second.zip"
            first.write_bytes(_zip_payload(entries))
            second.write_bytes(_zip_payload(list(reversed(entries))))

            with patch.object(analyzer, "MAX_FILES", 3):
                first_summary = _extract_source_archive(first, root / "first-checkout")
                second_summary = _extract_source_archive(second, root / "second-checkout")

            self.assertEqual(first_summary.eligible_files, 4)
            self.assertEqual(first_summary.selected_files, 3)
            self.assertTrue(first_summary.truncated)
            self.assertEqual(first_summary, ArchiveExtractionSummary(root / "first-checkout", 5, 4, 3))
            expected = ["a.py", "b.py", "m.py"]
            self.assertEqual(
                sorted(path.name for path in (root / "first-checkout").iterdir()), expected
            )
            self.assertEqual(
                sorted(path.name for path in (root / "second-checkout").iterdir()), expected
            )

    def test_archive_with_more_than_legacy_25000_files_uses_bounded_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "large.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
                for index in range(25_001):
                    archive.writestr(f"demo-main/src/file-{index:05d}.py", b"")

            with patch.object(analyzer, "MAX_FILES", 3):
                summary = _extract_source_archive(archive_path, root / "checkout")

            self.assertEqual(summary.total_members, 25_001)
            self.assertEqual(summary.eligible_files, 25_001)
            self.assertEqual(summary.selected_files, 3)
            self.assertEqual(
                sorted(path.name for path in (root / "checkout" / "src").iterdir()),
                ["file-00000.py", "file-00001.py", "file-00002.py"],
            )

    def test_archive_member_cap_counts_directories_skipped_files_and_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "too-many-members.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("demo-main/", b"")
                archive.writestr("demo-main/image.png", b"ignored")
                link = zipfile.ZipInfo("demo-main/link.py")
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, "README.md")
                archive.writestr("demo-main/README.md", b"# Demo")

            with (
                patch.object(analyzer, "MAX_ARCHIVE_MEMBERS", 3),
                self.assertRaisesRegex(RuntimeError, "exceeds 3 members"),
            ):
                _extract_source_archive(archive_path, root / "checkout")

            self.assertFalse(os.path.lexists(root / "checkout"))

    def test_archive_preflight_rejects_nul_from_original_filename(self) -> None:
        item = zipfile.ZipInfo("demo-main/a\x00b.py")

        class FakeArchive:
            @staticmethod
            def infolist() -> list[zipfile.ZipInfo]:
                return [item]

        with self.assertRaisesRegex(RuntimeError, "unsafe source archive path"):
            analyzer._preflight_source_archive(FakeArchive())

    def test_archive_rejects_ambiguous_roots_duplicates_and_file_directory_collisions(self) -> None:
        cases = {
            "mixed-roots": [
                ("demo-main/README.md", b"demo"),
                ("other-main/app.py", b"other"),
            ],
            "root-case-collision": [
                ("Demo-main/README.md", b"demo"),
                ("demo-main/app.py", b"other"),
            ],
            "casefold-duplicate": [
                ("demo-main/Readme.md", b"one"),
                ("demo-main/README.md", b"two"),
            ],
            "nfc-duplicate": [
                ("demo-main/caf\u00e9.py", b"one"),
                ("demo-main/cafe\u0301.py", b"two"),
            ],
            "file-directory-collision": [
                ("demo-main/pkg", b"file"),
                ("demo-main/pkg/module.py", b"nested"),
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, entries in cases.items():
                with self.subTest(name=name):
                    archive_path = root / f"{name}.zip"
                    archive_path.write_bytes(_zip_payload(entries))
                    checkout = root / f"{name}-checkout"
                    with self.assertRaises(RuntimeError):
                        _extract_source_archive(archive_path, checkout)
                    self.assertFalse(os.path.lexists(checkout))

    def test_archive_selected_byte_cap_is_enforced_before_checkout_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "oversize.zip"
            archive_path.write_bytes(
                _zip_payload(
                    [
                        ("demo-main/a.py", b"123"),
                        ("demo-main/b.py", b"456"),
                    ]
                )
            )

            with (
                patch.object(analyzer, "MAX_EXTRACTED_BYTES", 5),
                self.assertRaisesRegex(RuntimeError, "exceed 5 extracted bytes"),
            ):
                _extract_source_archive(archive_path, root / "checkout")

            self.assertFalse(os.path.lexists(root / "checkout"))

    def test_unselected_archive_member_is_never_opened_or_counted_against_payload_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "selection.zip"
            archive_path.write_bytes(
                _zip_payload(
                    [
                        ("demo-main/a.py", b"a"),
                        ("demo-main/z.py", b"z" * 100),
                    ]
                )
            )
            opened: list[str] = []
            original_open = zipfile.ZipFile.open

            def record_open(
                archive: zipfile.ZipFile, item: object, *args: object, **kwargs: object
            ) -> object:
                opened.append(item.filename if isinstance(item, zipfile.ZipInfo) else str(item))
                return original_open(archive, item, *args, **kwargs)

            with (
                patch.object(analyzer, "MAX_FILES", 1),
                patch.object(analyzer, "MAX_EXTRACTED_BYTES", 5),
                patch.object(zipfile.ZipFile, "open", new=record_open),
            ):
                summary = _extract_source_archive(archive_path, root / "checkout")

            self.assertEqual(summary.eligible_files, 2)
            self.assertEqual(summary.selected_files, 1)
            self.assertEqual(opened, ["demo-main/a.py"])
            self.assertFalse((root / "checkout" / "z.py").exists())

    def test_large_selected_member_is_fully_checked_but_materialized_as_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "large-text.zip"
            archive_path.write_bytes(_zip_payload([("demo-main/large.py", b"0123456789")]))

            with patch.object(analyzer, "MAX_TEXT_BYTES", 4):
                _extract_source_archive(archive_path, root / "checkout")

            self.assertEqual((root / "checkout" / "large.py").read_bytes(), b"")

    def test_corrupt_large_placeholder_member_is_rejected_by_crc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "corrupt.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("demo-main/large.py", b"0123456789")
            raw = bytearray(archive_path.read_bytes())
            offset = raw.index(b"0123456789")
            raw[offset] ^= 0x01
            archive_path.write_bytes(raw)

            with (
                patch.object(analyzer, "MAX_TEXT_BYTES", 4),
                self.assertRaises(zipfile.BadZipFile),
            ):
                _extract_source_archive(archive_path, root / "checkout")

            self.assertFalse(os.path.lexists(root / "checkout"))
            self.assertEqual(list(root.glob(".checkout.partial-*")), [])

    def test_archive_read_interruption_removes_partial_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "interrupted.zip"
            archive_path.write_bytes(_zip_payload([("demo-main/app.py", b"print('demo')")]))
            original_open = zipfile.ZipFile.open

            def interrupting_open(
                archive: zipfile.ZipFile, item: object, *args: object, **kwargs: object
            ) -> object:
                return _InterruptingReader(original_open(archive, item, *args, **kwargs))

            with (
                patch.object(zipfile.ZipFile, "open", new=interrupting_open),
                self.assertRaisesRegex(OSError, "simulated archive read interruption"),
            ):
                _extract_source_archive(archive_path, root / "checkout")

            self.assertFalse(os.path.lexists(root / "checkout"))
            self.assertEqual(list(root.glob(".checkout.partial-*")), [])

    def test_archive_publish_failure_removes_staging_and_never_exposes_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "source.zip"
            archive_path.write_bytes(_zip_payload([("demo-main/app.py", b"safe")]))

            with (
                patch.object(analyzer.os, "replace", side_effect=OSError("simulated replace failure")),
                self.assertRaisesRegex(OSError, "simulated replace failure"),
            ):
                _extract_source_archive(archive_path, root / "checkout")

            self.assertFalse(os.path.lexists(root / "checkout"))
            self.assertEqual(list(root.glob(".checkout.partial-*")), [])

    def test_archive_staging_identity_failure_is_cleaned_without_exposing_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "source.zip"
            archive_path.write_bytes(_zip_payload([("demo-main/app.py", b"safe")]))
            original_lstat = analyzer.os.lstat
            failed = False

            def fail_first_staging_lstat(path: object, *args: object, **kwargs: object) -> object:
                nonlocal failed
                candidate = Path(path)
                if not failed and candidate.name.startswith(".checkout.partial-"):
                    failed = True
                    raise OSError("simulated staging identity failure")
                return original_lstat(path, *args, **kwargs)

            with (
                patch.object(analyzer.os, "lstat", new=fail_first_staging_lstat),
                self.assertRaisesRegex(OSError, "simulated staging identity failure"),
            ):
                _extract_source_archive(archive_path, root / "checkout")

            self.assertTrue(failed)
            self.assertFalse(os.path.lexists(root / "checkout"))
            self.assertEqual(list(root.glob(".checkout.partial-*")), [])

    def test_preexisting_late_staging_collision_is_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "source.zip"
            archive_path.write_bytes(_zip_payload([("demo-main/app.py", b"safe")]))
            fixed_uuid = analyzer.uuid.UUID(int=0)
            staging = root / f".checkout.partial-{fixed_uuid.hex}"
            staging.mkdir()
            sentinel = staging / "sentinel.txt"
            sentinel.write_bytes(b"external")

            with (
                patch.object(analyzer.uuid, "uuid4", return_value=fixed_uuid),
                self.assertRaises(FileExistsError),
            ):
                _extract_source_archive(archive_path, root / "checkout")

            self.assertEqual(sentinel.read_bytes(), b"external")
            self.assertFalse(os.path.lexists(root / "checkout"))

    def test_existing_checkout_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "source.zip"
            archive_path.write_bytes(_zip_payload([("demo-main/app.py", b"safe")]))
            checkout = root / "checkout"
            checkout.mkdir()
            sentinel = checkout / "sentinel.txt"
            sentinel.write_bytes(b"existing")

            with self.assertRaisesRegex(RuntimeError, "checkout already exists"):
                _extract_source_archive(archive_path, checkout)

            self.assertEqual(sentinel.read_bytes(), b"existing")

    def test_archive_download_limits_remove_partial_file(self) -> None:
        payload = b"0123456789"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(analyzer, "MAX_ARCHIVE_BYTES", 5),
                patch.object(
                    analyzer.urllib.request,
                    "urlopen",
                    return_value=_FakeResponse(payload, content_length="10"),
                ),
                self.assertRaisesRegex(RuntimeError, "exceeds 5 download bytes"),
            ):
                _download_source_archive("demo/repo", root)
            self.assertFalse(os.path.lexists(root / "source.zip.part"))
            self.assertFalse(os.path.lexists(root / "source.zip"))

            with (
                patch.object(analyzer, "MAX_ARCHIVE_BYTES", 5),
                patch.object(
                    analyzer.urllib.request,
                    "urlopen",
                    return_value=_FakeResponse(payload),
                ),
                self.assertRaisesRegex(RuntimeError, "exceeds 5 download bytes"),
            ):
                _download_source_archive("demo/repo", root)
            self.assertFalse(os.path.lexists(root / "source.zip.part"))
            self.assertFalse(os.path.lexists(root / "source.zip"))

    def test_late_external_partial_download_collision_is_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partial = root / "source.zip.part"

            class CollidingResponse(_FakeResponse):
                def __enter__(self) -> CollidingResponse:
                    partial.write_bytes(b"external")
                    return super().__enter__()

            with (
                patch.object(
                    analyzer.urllib.request,
                    "urlopen",
                    return_value=CollidingResponse(b"archive"),
                ),
                self.assertRaises(FileExistsError),
            ):
                _download_source_archive("demo/repo", root)

            self.assertEqual(partial.read_bytes(), b"external")
            self.assertFalse(os.path.lexists(root / "source.zip"))

    def test_successful_archive_download_atomically_publishes_zip_and_checkout(self) -> None:
        payload = _zip_payload([("demo-main/README.md", b"# Demo")])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                analyzer.urllib.request,
                "urlopen",
                return_value=_FakeResponse(payload, content_length=str(len(payload))),
            ):
                summary = _download_source_archive("demo/repo", root)

            self.assertEqual(summary.selected_files, 1)
            self.assertTrue((root / "source.zip").is_file())
            self.assertFalse(os.path.lexists(root / "source.zip.part"))
            self.assertEqual((summary.checkout / "README.md").read_bytes(), b"# Demo")

    def test_bounded_clone_timeout_terminates_only_its_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "descendants.json"
            grandchild_code = "import time; time.sleep(60)"
            child_code = (
                "import json, os, subprocess, sys, time; "
                "grandchild=subprocess.Popen([sys.executable, '-c', sys.argv[2]], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "handle=open(sys.argv[1], 'w', encoding='utf-8'); "
                "json.dump({'child': os.getpid(), 'grandchild': grandchild.pid}, handle); "
                "handle.flush(); os.fsync(handle.fileno()); handle.close(); time.sleep(60)"
            )
            root_code = (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1], sys.argv[3]], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "time.sleep(60)"
            )
            sibling = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            descendants: dict[str, int] = {}
            try:
                message = _run_bounded_clone(
                    [
                        sys.executable,
                        "-c",
                        root_code,
                        str(pid_path),
                        child_code,
                        grandchild_code,
                    ],
                    dict(os.environ),
                    timeout_seconds=1.5,
                )
                self.assertIn("timed out after 1.5 seconds", str(message))
                self.assertTrue(pid_path.is_file())
                descendants = json.loads(pid_path.read_text(encoding="utf-8"))
                self.assertTrue(_wait_for_pid_exit(descendants["child"]))
                self.assertTrue(_wait_for_pid_exit(descendants["grandchild"]))
                self.assertIsNone(sibling.poll())
            finally:
                _terminate_test_process(sibling)
                for pid in descendants.values():
                    _terminate_test_pid(pid)

    def test_successful_contained_process_has_no_lingering_job_members(self) -> None:
        result = _run_bounded_clone(
            [sys.executable, "-c", "raise SystemExit(0)"],
            dict(os.environ),
            timeout_seconds=5,
        )

        self.assertIsNone(result)

    def test_unexpected_platform_cleanup_error_is_mapped_to_lifecycle_failure(self) -> None:
        process = Mock(pid=12345)
        helper_name = (
            "_terminate_clone_tree_windows"
            if os.name == "nt"
            else "_terminate_clone_group_posix"
        )
        with (
            patch.object(analyzer, helper_name, side_effect=OSError("cleanup probe failed")),
            self.assertRaises(RemoteCloneLifecycleError) as raised,
        ):
            analyzer._terminate_clone_tree(process, timeout_seconds=1)

        self.assertEqual(raised.exception.code, "remote_clone_process_tree_cleanup_failed")
        self.assertIn("cleanup probe failed", str(raised.exception))

    @unittest.skipUnless(hasattr(os, "killpg"), "POSIX process groups are unavailable")
    def test_posix_tree_cleanup_escalates_from_term_to_kill(self) -> None:
        process = Mock(pid=12345)
        process.wait.return_value = 0
        with (
            patch.object(analyzer.os, "killpg") as kill_group,
            patch.object(analyzer, "_process_group_exists", return_value=True),
            patch.object(
                analyzer,
                "_wait_for_process_group_exit",
                side_effect=[False, False, True],
            ),
        ):
            analyzer._terminate_clone_group_posix(process, time.monotonic() + 1)

        self.assertEqual(
            kill_group.call_args_list,
            [
                unittest.mock.call(12345, analyzer.signal.SIGTERM),
                unittest.mock.call(12345, analyzer.signal.SIGKILL),
            ],
        )

    def test_nonzero_clone_root_cleans_lingering_descendants_before_return(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "descendant.json"
            child_code = (
                "import json, os, sys, time; "
                "handle=open(sys.argv[1], 'w', encoding='utf-8'); "
                "json.dump({'child': os.getpid()}, handle); handle.flush(); "
                "os.fsync(handle.fileno()); handle.close(); time.sleep(60)"
            )
            root_code = (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "time.sleep(0.3); raise SystemExit(7)"
            )

            child_pid: int | None = None
            try:
                result = _run_bounded_clone(
                    [sys.executable, "-c", root_code, str(pid_path), child_code],
                    dict(os.environ),
                    timeout_seconds=5,
                )

                self.assertEqual(result, "shallow clone failed with exit code 7")
                child_pid = json.loads(pid_path.read_text(encoding="utf-8"))["child"]
                self.assertTrue(_wait_for_pid_exit(child_pid))
            finally:
                if child_pid is None and pid_path.is_file():
                    child_pid = json.loads(pid_path.read_text(encoding="utf-8"))["child"]
                if child_pid is not None:
                    _terminate_test_pid(child_pid)

    def test_successful_clone_root_with_lingering_descendant_fails_closed_after_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "descendant.json"
            child_code = (
                "import json, os, sys, time; "
                "handle=open(sys.argv[1], 'w', encoding='utf-8'); "
                "json.dump({'child': os.getpid()}, handle); handle.flush(); "
                "os.fsync(handle.fileno()); handle.close(); time.sleep(60)"
            )
            root_code = (
                "import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "time.sleep(0.3)"
            )

            child_pid: int | None = None
            try:
                with self.assertRaises(RemoteCloneLifecycleError) as raised:
                    _run_bounded_clone(
                        [sys.executable, "-c", root_code, str(pid_path), child_code],
                        dict(os.environ),
                        timeout_seconds=5,
                    )

                self.assertEqual(raised.exception.code, "remote_clone_unexpected_descendants")
                child_pid = json.loads(pid_path.read_text(encoding="utf-8"))["child"]
                self.assertTrue(_wait_for_pid_exit(child_pid))
            finally:
                if child_pid is None and pid_path.is_file():
                    child_pid = json.loads(pid_path.read_text(encoding="utf-8"))["child"]
                if child_pid is not None:
                    _terminate_test_pid(child_pid)

    @unittest.skipUnless(os.name == "nt", "Windows Job Objects are unavailable")
    def test_windows_job_resume_failure_is_contained_and_structured(self) -> None:
        with (
            patch.object(
                analyzer,
                "_resume_windows_process",
                side_effect=OSError("resume failed"),
            ),
            self.assertRaises(RemoteCloneLifecycleError) as raised,
        ):
            analyzer._spawn_clone(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                dict(os.environ),
            )

        self.assertEqual(raised.exception.code, "remote_clone_containment_failed")
        self.assertIn("resume failed", str(raised.exception))

    def test_windows_job_setup_close_failure_is_lifecycle_fatal(self) -> None:
        kernel32 = Mock()
        kernel32.CreateJobObjectW.return_value = 123
        kernel32.SetInformationJobObject.return_value = False
        kernel32.CloseHandle.return_value = False
        with (
            patch.object(analyzer, "_windows_kernel32", return_value=kernel32),
            patch.object(
                analyzer,
                "_windows_error",
                side_effect=lambda operation: OSError(f"{operation} failed"),
            ),
            self.assertRaises(RemoteCloneLifecycleError) as raised,
        ):
            analyzer._create_windows_job()

        self.assertEqual(raised.exception.code, "remote_clone_process_tree_cleanup_failed")
        self.assertIn("CloseHandle failed", str(raised.exception))

    def test_spawn_failure_with_unclosed_job_handle_is_lifecycle_fatal(self) -> None:
        with (
            patch.object(analyzer.os, "name", "nt"),
            patch.object(analyzer, "_create_windows_job", return_value=(Mock(), 123)),
            patch.object(analyzer.subprocess, "Popen", side_effect=OSError("spawn failed")),
            patch.object(
                analyzer,
                "_close_windows_handle",
                side_effect=OSError("job handle close failed"),
            ),
            self.assertRaises(RemoteCloneLifecycleError) as raised,
        ):
            _run_bounded_clone(["git", "clone"], {}, timeout_seconds=1)

        self.assertEqual(raised.exception.code, "remote_clone_process_tree_cleanup_failed")
        self.assertIn("job handle close failed", str(raised.exception))

    def test_resume_and_cleanup_failure_never_becomes_spawn_fallback(self) -> None:
        process = Mock(pid=12345)
        process._handle = 67890
        kernel32 = Mock()
        kernel32.AssignProcessToJobObject.return_value = True
        with (
            patch.object(analyzer.os, "name", "nt"),
            patch.object(analyzer, "_create_windows_job", return_value=(kernel32, 123)),
            patch.object(analyzer.subprocess, "Popen", return_value=process),
            patch.object(
                analyzer,
                "_resume_windows_process",
                side_effect=OSError("resume failed"),
            ),
            patch.object(
                analyzer,
                "_terminate_clone_tree_windows",
                side_effect=OSError("cleanup failed"),
            ),
            self.assertRaises(RemoteCloneLifecycleError) as raised,
        ):
            _run_bounded_clone(["git", "clone"], {}, timeout_seconds=1)

        self.assertEqual(raised.exception.code, "remote_clone_process_tree_cleanup_failed")
        self.assertIn("cleanup failed", str(raised.exception))

    @unittest.skipUnless(os.name == "nt", "Windows Job Objects are unavailable")
    def test_windows_job_query_failure_is_lifecycle_fatal(self) -> None:
        with (
            patch.object(
                analyzer,
                "_windows_job_active_processes",
                side_effect=OSError("query failed"),
            ),
            self.assertRaises(RemoteCloneLifecycleError) as raised,
        ):
            _run_bounded_clone(
                [sys.executable, "-c", "raise SystemExit(0)"],
                dict(os.environ),
                timeout_seconds=5,
            )

        self.assertEqual(raised.exception.code, "remote_clone_process_tree_cleanup_failed")
        self.assertIn("query failed", str(raised.exception))

    def test_spawn_failure_is_safe_to_use_archive_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def provide_archive(
                _repository: str, temporary_root: Path
            ) -> ArchiveExtractionSummary:
                checkout = temporary_root / "archive-repo"
                checkout.mkdir()
                (checkout / "README.md").write_text("# Demo", encoding="utf-8")
                return ArchiveExtractionSummary(checkout, 1, 1, 1)

            with (
                patch.object(analyzer.subprocess, "Popen", side_effect=OSError("git unavailable")),
                patch.object(analyzer, "_download_source_archive", side_effect=provide_archive) as download,
            ):
                evidence = _analyze_remote_in_temporary_root("demo/repo", root)

            download.assert_called_once_with("demo/repo", root)
            self.assertTrue(
                any("shallow clone could not start: git unavailable" in item for item in evidence.warnings)
            )

    @unittest.skipUnless(os.name == "nt", "Windows readonly file attributes are unavailable")
    def test_partial_checkout_cleanup_repairs_owned_readonly_git_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checkout = root / "repo"
            pack = checkout / ".git" / "objects" / "pack" / "pack-demo.idx"
            pack.parent.mkdir(parents=True)
            pack.write_bytes(b"promisor index")
            os.chmod(pack, stat.S_IREAD)
            self.assertTrue(
                getattr(os.lstat(pack), "st_file_attributes", 0)
                & analyzer._WINDOWS_FILE_ATTRIBUTE_READONLY
            )

            analyzer._remove_partial_checkout(checkout, root)

            self.assertFalse(os.path.lexists(checkout))

    @unittest.skipUnless(os.name == "nt", "Windows readonly file attributes are unavailable")
    def test_public_analyzer_final_cleanup_removes_owned_readonly_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retained = Path(directory).resolve() / "owned-analysis"

            def create_temp_root(*_args: object, **_kwargs: object) -> str:
                retained.mkdir()
                return str(retained)

            def provide_evidence(repository: str, temporary_root: Path) -> object:
                checkout = temporary_root / "repo"
                pack = checkout / ".git" / "objects" / "pack" / "pack-demo.pack"
                pack.parent.mkdir(parents=True)
                pack.write_bytes(b"promisor pack")
                os.chmod(pack, stat.S_IREAD)
                return analyze_path(checkout, repository)

            with (
                patch.object(analyzer.tempfile, "mkdtemp", side_effect=create_temp_root),
                patch.object(
                    analyzer,
                    "_analyze_remote_in_temporary_root",
                    side_effect=provide_evidence,
                ),
            ):
                evidence = analyze_remote("demo/repo")

            self.assertEqual(evidence.repository, "demo/repo")
            self.assertFalse(os.path.lexists(retained))

    def test_owned_tree_does_not_chmod_nonreadonly_access_denial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "owned"
            root.mkdir()
            target = root / "target.txt"
            target.write_bytes(b"preserve")
            identity = analyzer._owned_directory_identity(root)

            def deny_removal(
                _root: object,
                *args: object,
                **kwargs: object,
            ) -> None:
                onerror = kwargs["onerror"]
                error = PermissionError(13, "simulated access denial", str(target), 5)
                onerror(os.unlink, str(target), (PermissionError, error, None))

            with (
                patch.object(analyzer.shutil, "rmtree", new=deny_removal),
                patch.object(analyzer.os, "chmod") as chmod,
                self.assertRaises(PermissionError),
            ):
                analyzer._remove_owned_tree(
                    root,
                    identity,
                    allow_leaf_symlinks=True,
                )

            chmod.assert_not_called()
            self.assertEqual(target.read_bytes(), b"preserve")

    def test_owned_tree_rejects_root_identity_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "owned"
            held = parent / "held"
            root.mkdir()
            (root / "original.txt").write_bytes(b"original")
            identity = analyzer._owned_directory_identity(root)
            original_preflight = analyzer._preflight_owned_tree

            def replace_after_preflight(path: Path, *, allow_leaf_symlinks: bool) -> None:
                original_preflight(path, allow_leaf_symlinks=allow_leaf_symlinks)
                path.rename(held)
                path.mkdir()
                (path / "replacement.txt").write_bytes(b"replacement")

            with (
                patch.object(
                    analyzer,
                    "_preflight_owned_tree",
                    side_effect=replace_after_preflight,
                ),
                self.assertRaisesRegex(RuntimeError, "changed identity"),
            ):
                analyzer._remove_owned_tree(
                    root,
                    identity,
                    allow_leaf_symlinks=True,
                )

            self.assertEqual((root / "replacement.txt").read_bytes(), b"replacement")
            self.assertEqual((held / "original.txt").read_bytes(), b"original")

    @unittest.skipUnless(os.name == "nt", "Windows hardlink attributes are unavailable")
    def test_owned_tree_never_chmods_readonly_external_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "owned"
            root.mkdir()
            outside = parent / "outside.pack"
            outside.write_bytes(b"shared pack")
            linked = root / "linked.pack"
            try:
                os.link(outside, linked)
            except OSError as error:
                self.skipTest(f"hardlinks are unavailable: {error}")
            os.chmod(outside, stat.S_IREAD)
            identity = analyzer._owned_directory_identity(root)
            try:
                with self.assertRaises(PermissionError):
                    analyzer._remove_owned_tree(
                        root,
                        identity,
                        allow_leaf_symlinks=True,
                    )

                self.assertEqual(outside.read_bytes(), b"shared pack")
                self.assertTrue(
                    getattr(os.lstat(outside), "st_file_attributes", 0)
                    & analyzer._WINDOWS_FILE_ATTRIBUTE_READONLY
                )
                self.assertTrue(os.path.lexists(linked))
            finally:
                os.chmod(outside, stat.S_IWRITE)
                if os.path.lexists(linked):
                    linked.unlink()
                if os.path.lexists(root):
                    root.rmdir()

    def test_owned_tree_unlinks_leaf_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "owned"
            root.mkdir()
            outside = parent / "outside.txt"
            outside.write_bytes(b"outside")
            link = root / "outside-link"
            try:
                link.symlink_to(outside)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"file symlinks are unavailable: {error}")
            identity = analyzer._owned_directory_identity(root)

            analyzer._remove_owned_tree(
                root,
                identity,
                allow_leaf_symlinks=True,
            )

            self.assertFalse(os.path.lexists(root))
            self.assertEqual(outside.read_bytes(), b"outside")

    @unittest.skipUnless(os.name == "nt", "Windows junctions are unavailable")
    def test_owned_tree_rejects_junction_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "owned"
            root.mkdir()
            outside = parent / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_bytes(b"outside")
            junction = root / "junction"
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
            if created.returncode != 0 or not os.path.lexists(junction):
                self.skipTest("directory junctions are unavailable")
            identity = analyzer._owned_directory_identity(root)
            try:
                with self.assertRaisesRegex(RuntimeError, "reparse point"):
                    analyzer._remove_owned_tree(
                        root,
                        identity,
                        allow_leaf_symlinks=True,
                    )

                self.assertEqual(sentinel.read_bytes(), b"outside")
                self.assertTrue(os.path.lexists(junction))
            finally:
                if os.path.lexists(junction):
                    junction.rmdir()
                if os.path.lexists(root):
                    root.rmdir()

    def test_public_analyzer_retains_and_reports_temp_root_for_unresolved_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retained = Path(directory) / "retained-analysis"

            def create_temp_root(*_args: object, **_kwargs: object) -> str:
                retained.mkdir()
                return str(retained)

            failure = RemoteCloneLifecycleError(
                "remote_clone_process_tree_cleanup_failed", "simulated"
            )
            with (
                patch.object(analyzer.tempfile, "mkdtemp", side_effect=create_temp_root),
                patch.object(
                    analyzer,
                    "_analyze_remote_in_temporary_root",
                    side_effect=failure,
                ),
                self.assertRaises(RemoteCloneLifecycleError) as raised,
            ):
                analyze_remote("demo/repo")

            self.assertTrue(retained.is_dir())
            self.assertEqual(raised.exception.retained_temporary_root, str(retained))
            self.assertIn(f"retained temporary root: {retained}", str(raised.exception))
            retained.rmdir()

    def test_temp_cleanup_failure_keeps_prior_error_in_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retained = Path(directory) / "cleanup-failure"

            def create_temp_root(*_args: object, **_kwargs: object) -> str:
                retained.mkdir()
                return str(retained)

            with (
                patch.object(analyzer.tempfile, "mkdtemp", side_effect=create_temp_root),
                patch.object(
                    analyzer,
                    "_analyze_remote_in_temporary_root",
                    side_effect=RuntimeError("original archive failure"),
                ),
                patch.object(analyzer.shutil, "rmtree", side_effect=OSError("cleanup denied")),
                self.assertRaises(RemoteCloneLifecycleError) as raised,
            ):
                analyze_remote("demo/repo")

            self.assertEqual(raised.exception.code, "remote_analysis_temporary_cleanup_failed")
            self.assertIn("cleanup denied", str(raised.exception))
            self.assertIn("original archive failure", str(raised.exception))
            self.assertEqual(raised.exception.retained_temporary_root, str(retained))
            self.assertIn(f"retained temporary root: {retained}", str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, OSError)
            retained.rmdir()

    def test_lifecycle_cleanup_failure_never_uses_archive_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure = RemoteCloneLifecycleError(
                "remote_clone_process_tree_cleanup_failed", "simulated"
            )
            with (
                patch.object(analyzer, "_run_bounded_clone", side_effect=failure),
                patch.object(analyzer, "_download_source_archive") as download,
                self.assertRaises(RemoteCloneLifecycleError) as raised,
            ):
                _analyze_remote_in_temporary_root("demo/repo", root)

            self.assertEqual(raised.exception.code, "remote_clone_process_tree_cleanup_failed")
            download.assert_not_called()

    def test_confirmed_clone_failure_uses_archive_and_reports_deterministic_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def provide_archive(
                _repository: str, temporary_root: Path
            ) -> ArchiveExtractionSummary:
                checkout = temporary_root / "archive-repo"
                checkout.mkdir()
                (checkout / "README.md").write_text("# Demo", encoding="utf-8")
                return ArchiveExtractionSummary(checkout, 8, 5, 3)

            with (
                patch.object(
                    analyzer,
                    "_run_bounded_clone",
                    return_value="shallow clone failed with exit code 1",
                ),
                patch.object(analyzer, "_download_source_archive", side_effect=provide_archive) as download,
            ):
                evidence = _analyze_remote_in_temporary_root("demo/repo", root)

            download.assert_called_once_with("demo/repo", root)
            self.assertEqual(evidence.schemaVersion, 2)
            self.assertIn(
                "shallow clone failed with exit code 1; inspected a bounded official GitHub source archive instead",
                evidence.warnings,
            )
            self.assertIn(
                "official source archive deterministic selection capped at 3 of 5 eligible files",
                evidence.warnings,
            )

    def test_successful_clone_never_uses_archive_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def successful_clone(command: list[str], _environment: dict[str, str]) -> None:
                checkout = Path(command[-1])
                checkout.mkdir()
                (checkout / "README.md").write_text("# Demo", encoding="utf-8")
                return None

            with (
                patch.object(analyzer, "_run_bounded_clone", side_effect=successful_clone),
                patch.object(analyzer, "_download_source_archive") as download,
            ):
                evidence = _analyze_remote_in_temporary_root("demo/repo", root)

            download.assert_not_called()
            self.assertEqual(evidence.repository, "demo/repo")
            self.assertFalse(any("official GitHub source archive" in item for item in evidence.warnings))

    def test_unsafe_partial_checkout_prevents_archive_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo").write_bytes(b"unexpected")
            with (
                patch.object(
                    analyzer,
                    "_run_bounded_clone",
                    return_value="shallow clone failed with exit code 1",
                ),
                patch.object(analyzer, "_download_source_archive") as download,
                self.assertRaises(RemoteCloneLifecycleError) as raised,
            ):
                _analyze_remote_in_temporary_root("demo/repo", root)

            self.assertEqual(raised.exception.code, "remote_clone_checkout_cleanup_failed")
            download.assert_not_called()
            self.assertEqual((root / "repo").read_bytes(), b"unexpected")


if __name__ == "__main__":
    unittest.main()
