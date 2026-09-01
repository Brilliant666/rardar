from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from pipeline.runtime_settings import (
    RuntimeSettingsError,
    RuntimeTimezoneDatabaseError,
    default_runtime_settings,
    load_runtime_layout,
    load_runtime_settings,
    validate_vite_additional_allowed_hosts,
    validate_trending_discover_enabled,
    validate_trending_producer_enabled,
)


class RuntimeSettingsTests(unittest.TestCase):
    def test_defaults_preserve_the_existing_schedule_and_threshold(self) -> None:
        settings = load_runtime_settings({})
        self.assertEqual(settings.schedule_at, "08:00")
        self.assertEqual(settings.schedule_timezone, "Asia/Shanghai")
        self.assertEqual(settings.stale_after_hours, 36)
        self.assertEqual(settings.stale_after_seconds, 129600)
        self.assertFalse(settings.trending_producer_enabled)
        self.assertFalse(settings.trending_discover_enabled)
        self.assertFalse(settings.retention_enabled)
        self.assertEqual(settings.retention_capture_days, 45)
        self.assertEqual(settings.retention_generation_days, 30)
        self.assertEqual(settings.retention_discover_generation_days, 14)
        self.assertEqual(settings.retention_failed_candidate_days, 3)
        self.assertEqual(settings.retention_candidate_days, 7)
        self.assertEqual(settings.retention_candidate_latest_count, 10)
        self.assertEqual(settings.retention_temp_hours, 24)
        self.assertEqual(settings.storage_warning_percent, 85)
        self.assertEqual(settings.storage_hard_percent, 90)
        self.assertEqual(settings.storage_minimum_free_bytes, 8 * 1024**3)

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
        self.assertFalse(settings.trending_producer_enabled)
        self.assertFalse(settings.trending_discover_enabled)

    def test_trending_producer_flag_is_exact_and_fail_closed(self) -> None:
        self.assertTrue(validate_trending_producer_enabled("true"))
        self.assertFalse(validate_trending_producer_enabled("false"))
        for invalid in (None, "", "TRUE", "False", "1", True, " true"):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeSettingsError):
                validate_trending_producer_enabled(invalid)

        enabled = load_runtime_settings({"RARDAR_TRENDING_PRODUCER_ENABLED": "true"})
        self.assertTrue(enabled.trending_producer_enabled)

    def test_enabled_producer_requires_the_fixed_product_schedule(self) -> None:
        for environment in (
            {
                "RARDAR_TRENDING_PRODUCER_ENABLED": "true",
                "RARDAR_SCHEDULE_AT": "09:00",
            },
            {
                "RARDAR_TRENDING_PRODUCER_ENABLED": "true",
                "RARDAR_SCHEDULE_TIMEZONE": "UTC",
            },
        ):
            with self.subTest(environment=environment), self.assertRaises(RuntimeSettingsError):
                load_runtime_settings(environment)

    def test_discover_is_an_independent_default_off_fail_closed_flag(self) -> None:
        for value in (None, "", "false"):
            with self.subTest(value=value):
                self.assertFalse(validate_trending_discover_enabled(value))
        self.assertTrue(validate_trending_discover_enabled("true"))
        for invalid in ("TRUE", "False", "1", " true", True):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeSettingsError):
                validate_trending_discover_enabled(invalid)
        enabled = load_runtime_settings(
            {
                "RARDAR_TRENDING_PRODUCER_ENABLED": "true",
                "RARDAR_TRENDING_DISCOVER_ENABLED": "true",
            }
        )
        self.assertTrue(enabled.trending_discover_enabled)
        with self.assertRaises(RuntimeSettingsError):
            load_runtime_settings({"RARDAR_TRENDING_DISCOVER_ENABLED": "true"})

    def test_retention_and_storage_contracts_fail_closed_before_start(self) -> None:
        configured = load_runtime_settings(
            {
                "RARDAR_TRENDING_PRODUCER_ENABLED": "true",
                "RARDAR_RETENTION_ENABLED": "true",
                "RARDAR_RETENTION_CAPTURE_DAYS": "120",
                "RARDAR_RETENTION_DISCOVER_GENERATION_DAYS": "21",
                "RARDAR_RETENTION_FAILED_CANDIDATE_DAYS": "4",
                "RARDAR_RETENTION_CANDIDATE_DAYS": "8",
                "RARDAR_RETENTION_CANDIDATE_LATEST_COUNT": "12",
                "RARDAR_STORAGE_WARNING_PERCENT": "80",
                "RARDAR_STORAGE_HARD_PERCENT": "92",
                "RARDAR_STORAGE_MINIMUM_FREE_BYTES": "1024",
            }
        )
        self.assertTrue(configured.retention_enabled)
        self.assertEqual(configured.retention_capture_days, 120)
        self.assertEqual(configured.retention_discover_generation_days, 21)
        self.assertEqual(configured.retention_failed_candidate_days, 4)
        self.assertEqual(configured.retention_candidate_days, 8)
        self.assertEqual(configured.retention_candidate_latest_count, 12)
        invalid = (
            {"RARDAR_RETENTION_ENABLED": "true"},
            {"RARDAR_RETENTION_ENABLED": "yes"},
            {"RARDAR_RETENTION_CAPTURE_DAYS": "0"},
            {"RARDAR_RETENTION_CAPTURE_DAYS": "30"},
            {
                "RARDAR_RETENTION_CAPTURE_DAYS": "45",
                "RARDAR_RETENTION_DISCOVER_GENERATION_DAYS": "46",
            },
            {
                "RARDAR_RETENTION_CAPTURE_DAYS": "45",
                "RARDAR_RETENTION_DISCOVER_GENERATION_DAYS": "45",
            },
            {"RARDAR_RETENTION_FAILED_CANDIDATE_DAYS": "0"},
            {"RARDAR_RETENTION_CANDIDATE_DAYS": "0"},
            {"RARDAR_RETENTION_CANDIDATE_LATEST_COUNT": "0"},
            {"RARDAR_RETENTION_TEMP_HOURS": "0"},
            {"RARDAR_STORAGE_WARNING_PERCENT": "90", "RARDAR_STORAGE_HARD_PERCENT": "90"},
            {"RARDAR_STORAGE_MINIMUM_FREE_BYTES": "-1"},
        )
        for environment in invalid:
            with self.subTest(environment=environment), self.assertRaises(RuntimeSettingsError):
                load_runtime_settings(environment)

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

    def test_vite_additional_allowed_hosts_accepts_only_exact_canonical_fqdns(self) -> None:
        valid = {
            None: (),
            "rardar.cosflow.icu": ("rardar.cosflow.icu",),
            "rardar.cosflow.icu,preview.cosflow.icu": (
                "rardar.cosflow.icu",
                "preview.cosflow.icu",
            ),
            "build-2.preview123.example": ("build-2.preview123.example",),
        }
        for raw, expected in valid.items():
            with self.subTest(valid=raw):
                self.assertEqual(validate_vite_additional_allowed_hosts(raw), expected)

        label_too_long = f"{'a' * 64}.example"
        hostname_too_long = ".".join(("a" * 63,) * 4)
        invalid = (
            "",
            "true",
            "*",
            ".cosflow.icu",
            "*.cosflow.icu",
            ".com",
            "https://rardar.cosflow.icu",
            "http://rardar.cosflow.icu",
            "rardar.cosflow.icu:443",
            "rardar.cosflow.icu/path",
            "rardar.cosflow.icu?x=1",
            "rardar.cosflow.icu#x",
            "user@rardar.cosflow.icu",
            "127.0.0.1",
            "::1",
            "localhost",
            "preview.localhost",
            "rardar",
            "RARDAR.cosflow.icu",
            " rardar.cosflow.icu",
            "rardar.cosflow.icu ",
            "rardar.cosflow.icu,,preview.cosflow.icu",
            "rardar.cosflow.icu,",
            "rardar.cosflow.icu,rardar.cosflow.icu",
            "rädar.cosflow.icu",
            "rardar.cosflow.icu\n",
            label_too_long,
            hostname_too_long,
            "-rardar.cosflow.icu",
            "rardar-.cosflow.icu",
            ",".join(f"host-{index}.example.com" for index in range(9)),
        )
        for raw in invalid:
            with self.subTest(invalid=raw), self.assertRaises(RuntimeSettingsError):
                validate_vite_additional_allowed_hosts(raw)

    def test_runtime_settings_validate_the_optional_vite_host_contract(self) -> None:
        configured = load_runtime_settings(
            {"__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS": "rardar.cosflow.icu"}
        )
        self.assertEqual(configured.schedule_at, "08:00")
        with self.assertRaises(RuntimeSettingsError):
            load_runtime_settings(
                {"__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS": ".cosflow.icu"}
            )

    def test_layout_defaults_keep_source_root_data_and_loopback_ports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            layout = load_runtime_layout({}, application_root=root)

        self.assertEqual(layout.home, root)
        self.assertEqual(layout.data_dir, root / "data")
        self.assertTrue(layout.runtime_dir.is_absolute())
        self.assertTrue(layout.data_lock_dir.is_absolute())
        self.assertEqual(layout.vinext_port, 3000)
        self.assertEqual(layout.runtime_status_port, 3002)
        self.assertEqual(layout.website_url, "http://127.0.0.1:3000/")
        self.assertEqual(layout.status_url, "http://127.0.0.1:3002/status")

    def test_layout_accepts_absolute_paths_and_distinct_custom_ports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            paths = {
                "RARDAR_HOME": str(root / "home"),
                "RARDAR_DATA_DIR": str(root / "data"),
                "RARDAR_RUNTIME_DIR": str(root / "runtime"),
                "RARDAR_DATA_LOCK_DIR": str(root / "locks"),
                "RARDAR_VINEXT_STATE_DIR": str(root / "state"),
                "RARDAR_VITE_CACHE_DIR": str(root / "cache"),
                "WRANGLER_LOG_PATH": str(root / "logs"),
                "WRANGLER_REGISTRY_PATH": str(root / "wrangler-registry"),
                "MINIFLARE_REGISTRY_PATH": str(root / "miniflare-registry"),
                "RARDAR_VINEXT_PORT": "43111",
                "RARDAR_RUNTIME_STATUS_PORT": "43112",
            }
            layout = load_runtime_layout(paths, application_root=root / "ignored")

        self.assertEqual(layout.home, root / "home")
        self.assertEqual(layout.data_dir, root / "data")
        self.assertEqual(layout.runtime_dir, root / "runtime")
        self.assertEqual(layout.data_lock_dir, root / "locks")
        self.assertEqual(layout.vinext_port, 43111)
        self.assertEqual(layout.runtime_status_port, 43112)

    def test_layout_rejects_relative_empty_and_malformed_contract_values(self) -> None:
        invalid = (
            {"RARDAR_HOME": "relative/home"},
            {"RARDAR_DATA_DIR": "data"},
            {"RARDAR_RUNTIME_DIR": "runtime"},
            {"RARDAR_DATA_LOCK_DIR": "locks"},
            {"RARDAR_VINEXT_STATE_DIR": "state"},
            {"RARDAR_VITE_CACHE_DIR": ""},
            {"WRANGLER_LOG_PATH": " logs"},
            {"RARDAR_VINEXT_PORT": "0"},
            {"RARDAR_VINEXT_PORT": "06500"},
            {"RARDAR_VINEXT_PORT": "65536"},
            {"RARDAR_RUNTIME_STATUS_PORT": "3002.0"},
            {"RARDAR_VINEXT_PORT": "4000", "RARDAR_RUNTIME_STATUS_PORT": "4000"},
        )
        for environment in invalid:
            with self.subTest(environment=environment), self.assertRaises(RuntimeSettingsError):
                load_runtime_layout(environment)


if __name__ == "__main__":
    unittest.main()
