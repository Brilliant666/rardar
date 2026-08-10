"""Validated configuration shared by the managed runtime and scheduler."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_SCHEDULE_AT = "08:00"
DEFAULT_SCHEDULE_TIMEZONE = "Asia/Shanghai"
DEFAULT_STALE_AFTER_HOURS = 36
MAX_STALE_AFTER_HOURS = 24 * 365
SCHEDULER_ALREADY_RUNNING_EXIT_CODE = 3
_CLOCK_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_POSITIVE_INTEGER = re.compile(r"^[1-9]\d*$")


class RuntimeSettingsError(ValueError):
    """A managed-runtime setting is present but cannot be trusted."""


class RuntimeTimezoneDatabaseError(RuntimeSettingsError):
    """The configured timezone could not be resolved by this Python runtime."""


@dataclass(frozen=True)
class RuntimeSettings:
    schedule_at: str
    schedule_timezone: str
    stale_after_hours: int

    @property
    def stale_after_seconds(self) -> int:
        return self.stale_after_hours * 60 * 60


def default_runtime_settings() -> RuntimeSettings:
    """Return version-controlled defaults without consulting the timezone database."""
    return RuntimeSettings(
        schedule_at=DEFAULT_SCHEDULE_AT,
        schedule_timezone=DEFAULT_SCHEDULE_TIMEZONE,
        stale_after_hours=DEFAULT_STALE_AFTER_HOURS,
    )


def default_runtime_dir(environment: Mapping[str, str] | None = None) -> Path:
    source = os.environ if environment is None else environment
    configured = source.get("RARDAR_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = Path(source.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(source.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "Rardar" / "runtime"


def default_scheduler_status_path(environment: Mapping[str, str] | None = None) -> Path:
    return default_runtime_dir(environment) / "scheduler-status.json"


def validate_schedule_at(value: object) -> str:
    if not isinstance(value, str) or _CLOCK_PATTERN.fullmatch(value) is None:
        raise RuntimeSettingsError("RARDAR_SCHEDULE_AT must use canonical HH:MM in 24-hour time")
    return value


def validate_schedule_timezone(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise RuntimeSettingsError("RARDAR_SCHEDULE_TIMEZONE must be a valid IANA timezone")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        raise RuntimeTimezoneDatabaseError(
            "RARDAR_SCHEDULE_TIMEZONE could not be loaded; "
            "install the dependencies in requirements.txt or choose a valid IANA timezone"
        ) from None
    except ValueError:
        raise RuntimeSettingsError("RARDAR_SCHEDULE_TIMEZONE must be a valid IANA timezone") from None
    return value


def validate_stale_after_hours(value: object) -> int:
    if not isinstance(value, str) or _POSITIVE_INTEGER.fullmatch(value) is None:
        raise RuntimeSettingsError(
            f"RARDAR_STALE_AFTER_HOURS must be an integer from 1 to {MAX_STALE_AFTER_HOURS}"
        )
    parsed = int(value)
    if parsed > MAX_STALE_AFTER_HOURS:
        raise RuntimeSettingsError(
            f"RARDAR_STALE_AFTER_HOURS must be an integer from 1 to {MAX_STALE_AFTER_HOURS}"
        )
    return parsed


def load_runtime_settings(
    environment: Mapping[str, str] | None = None,
    *,
    schedule_at: str | None = None,
    schedule_timezone: str | None = None,
) -> RuntimeSettings:
    source = os.environ if environment is None else environment
    effective_at = schedule_at if schedule_at is not None else source.get(
        "RARDAR_SCHEDULE_AT", DEFAULT_SCHEDULE_AT
    )
    effective_timezone = schedule_timezone if schedule_timezone is not None else source.get(
        "RARDAR_SCHEDULE_TIMEZONE", DEFAULT_SCHEDULE_TIMEZONE
    )
    stale_hours = source.get("RARDAR_STALE_AFTER_HOURS", str(DEFAULT_STALE_AFTER_HOURS))
    return RuntimeSettings(
        schedule_at=validate_schedule_at(effective_at),
        schedule_timezone=validate_schedule_timezone(effective_timezone),
        stale_after_hours=validate_stale_after_hours(stale_hours),
    )
