from __future__ import annotations

import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from pipeline.runtime_settings import (
    RuntimeSettingsError,
    RuntimeTimezoneDatabaseError,
    default_runtime_settings,
    load_runtime_settings,
)


class RuntimeSettingsTests(unittest.TestCase):
    def test_defaults_preserve_the_existing_schedule_and_threshold(self) -> None:
        settings = load_runtime_settings({})
        self.assertEqual(settings.schedule_at, "08:00")
        self.assertEqual(settings.schedule_timezone, "Asia/Shanghai")
        self.assertEqual(settings.stale_after_hours, 36)
        self.assertEqual(settings.stale_after_seconds, 129600)

    def test_version_controlled_defaults_do_not_require_timezone_data(self) -> None:
        with patch(
            "pipeline.runtime_settings.validate_schedule_timezone",
            side_effect=AssertionError("timezone validation must not run"),
        ):
            settings = default_runtime_settings()
        self.assertEqual(
            (settings.schedule_at, settings.schedule_timezone, settings.stale_after_hours),
            ("08:00", "Asia/Shanghai", 36),
        )

    def test_timezone_database_failure_has_a_distinct_error_type(self) -> None:
        with (
            patch(
                "pipeline.runtime_settings.ZoneInfo",
                side_effect=ZoneInfoNotFoundError("timezone unavailable"),
            ),
            self.assertRaises(RuntimeTimezoneDatabaseError),
        ):
            load_runtime_settings({})

    def test_environment_and_explicit_scheduler_overrides_are_validated(self) -> None:
        environment = {
            "RARDAR_SCHEDULE_AT": "09:25",
            "RARDAR_SCHEDULE_TIMEZONE": "Europe/Berlin",
            "RARDAR_STALE_AFTER_HOURS": "48",
        }
        configured = load_runtime_settings(environment)
        self.assertEqual(
            (configured.schedule_at, configured.schedule_timezone, configured.stale_after_hours),
            ("09:25", "Europe/Berlin", 48),
        )
        overridden = load_runtime_settings(
            environment,
            schedule_at="06:30",
            schedule_timezone="America/New_York",
        )
        self.assertEqual(
            (overridden.schedule_at, overridden.schedule_timezone, overridden.stale_after_hours),
            ("06:30", "America/New_York", 48),
        )

    def test_invalid_schedule_and_threshold_fail_closed(self) -> None:
        invalid = (
            {"RARDAR_SCHEDULE_AT": "8:00"},
            {"RARDAR_SCHEDULE_AT": "24:00"},
            {"RARDAR_SCHEDULE_AT": ""},
            {"RARDAR_SCHEDULE_TIMEZONE": "Not/A_Zone"},
            {"RARDAR_SCHEDULE_TIMEZONE": " Asia/Shanghai"},
            {"RARDAR_STALE_AFTER_HOURS": "0"},
            {"RARDAR_STALE_AFTER_HOURS": "36.5"},
            {"RARDAR_STALE_AFTER_HOURS": "8761"},
        )
        for environment in invalid:
            with self.subTest(environment=environment):
                with self.assertRaises(RuntimeSettingsError):
                    load_runtime_settings(environment)


if __name__ == "__main__":
    unittest.main()
