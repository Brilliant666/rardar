from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pipeline.analyze_repository import _git_environment, _is_test_file, analyze_path


class AnalyzeRepositoryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
