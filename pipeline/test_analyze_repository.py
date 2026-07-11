from __future__ import annotations

import tempfile
import unittest
import stat
import zipfile
from pathlib import Path
from unittest.mock import patch

from pipeline.analyze_repository import (
    _extract_source_archive,
    _git_environment,
    _is_test_file,
    analyze_path,
)


class AnalyzeRepositoryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
