from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class VerifyTimingPluginTests(unittest.TestCase):
    def test_pytest_timing_report_records_outcomes_and_slowest_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "pytest-timing.json"
            junit = root / "pytest-junit.xml"
            environment = dict(os.environ)
            environment["RARDAR_PYTEST_TIMING_PATH"] = str(report)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "pipeline/test_runtime_settings.py::RuntimeSettingsTests::test_defaults_preserve_the_existing_schedule_and_threshold",
                    "-q",
                    "-p",
                    "pipeline.pytest_timing",
                    f"--junitxml={junit}",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["collected"], 1)
            self.assertEqual(payload["passed"], 1)
            self.assertEqual(payload["failed"], 0)
            self.assertEqual(payload["skipped"], 0)
            self.assertEqual(payload["notRun"], 0)
            self.assertEqual(len(payload["topSlowTests"]), 1)
            self.assertIn(
                "test_defaults_preserve_the_existing_schedule_and_threshold",
                payload["topSlowTests"][0]["nodeId"],
            )
            self.assertTrue(junit.is_file())


if __name__ == "__main__":
    unittest.main()
