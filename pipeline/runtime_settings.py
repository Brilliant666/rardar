"""Validated configuration shared by the managed runtime and scheduler."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_SCHEDULE_AT = "08:00"
DEFAULT_SCHEDULE_TIMEZONE = "Asia/Shanghai"
DEFAULT_STALE_AFTER_HOURS = 36
DEFAULT_TRENDING_PRODUCER_ENABLED = False
TRENDING_PRODUCER_ENABLED_ENV = "RARDAR_TRENDING_PRODUCER_ENABLED"
MAX_STALE_AFTER_HOURS = 24 * 365
SCHEDULER_ALREADY_RUNNING_EXIT_CODE = 3
MANAGER_ALREADY_RUNNING_EXIT_CODE = 4
DEFAULT_VINEXT_PORT = 3000
DEFAULT_RUNTIME_STATUS_PORT = 3002
RUNTIME_HOST = "127.0.0.1"
VITE_ADDITIONAL_ALLOWED_HOSTS_ENV = "__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS"
MAX_VITE_ADDITIONAL_ALLOWED_HOSTS = 8
MAX_DNS_HOSTNAME_LENGTH = 253
MAX_VITE_ALLOWED_HOSTS_CONFIGURATION_LENGTH = (
    MAX_VITE_ADDITIONAL_ALLOWED_HOSTS * MAX_DNS_HOSTNAME_LENGTH
    + MAX_VITE_ADDITIONAL_ALLOWED_HOSTS
    - 1
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PERSISTENT_PATH_VARIABLES = (
    "RARDAR_VINEXT_STATE_DIR",
    "RARDAR_VITE_CACHE_DIR",
    "WRANGLER_LOG_PATH",
    "WRANGLER_REGISTRY_PATH",
    "MINIFLARE_REGISTRY_PATH",
)
_CLOCK_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_POSITIVE_INTEGER = re.compile(r"^[1-9]\d*$")
_DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class RuntimeSettingsError(ValueError):
    """A managed-runtime setting is present but cannot be trusted."""


class RuntimeTimezoneDatabaseError(RuntimeSettingsError):
    """The configured timezone could not be resolved by this Python runtime."""


@dataclass(frozen=True)
class RuntimeSettings:
    schedule_at: str
    schedule_timezone: str
    stale_after_hours: int
    trending_producer_enabled: bool = DEFAULT_TRENDING_PRODUCER_ENABLED

    @property
    def stale_after_seconds(self) -> int:
        return self.stale_after_hours * 60 * 60


@dataclass(frozen=True)
class RuntimeLayout:
    """Canonical filesystem and loopback endpoints for one managed runtime."""

    home: Path
    data_dir: Path
    runtime_dir: Path
    data_lock_dir: Path
    vinext_port: int
    runtime_status_port: int

    @property
    def log_dir(self) -> Path:
        return self.runtime_dir / "logs"

    @property
    def control_path(self) -> Path:
        return self.runtime_dir / "manager.json"

    @property
    def status_path(self) -> Path:
        return self.runtime_dir / "status.json"

    @property
    def scheduler_status_path(self) -> Path:
        return self.runtime_dir / "scheduler-status.json"

    @property
    def website_url(self) -> str:
        return f"http://{RUNTIME_HOST}:{self.vinext_port}/"

    @property
    def status_url(self) -> str:
        return f"http://{RUNTIME_HOST}:{self.runtime_status_port}/status"


def default_runtime_settings() -> RuntimeSettings:
    """Return version-controlled defaults without consulting the timezone database."""
    return RuntimeSettings(
        schedule_at=DEFAULT_SCHEDULE_AT,
        schedule_timezone=DEFAULT_SCHEDULE_TIMEZONE,
        stale_after_hours=DEFAULT_STALE_AFTER_HOURS,
        trending_producer_enabled=DEFAULT_TRENDING_PRODUCER_ENABLED,
    )


def _platform_state_root(source: Mapping[str, str]) -> Path:
    if os.name == "nt":
        base = Path(source.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(source.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base.expanduser().resolve()


def validate_absolute_path(name: str, value: object) -> Path:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise RuntimeSettingsError(f"{name} must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeSettingsError(f"{name} must be an absolute path")
    return path.resolve()


def validate_port(name: str, value: object) -> int:
    if not isinstance(value, str) or _POSITIVE_INTEGER.fullmatch(value) is None:
        raise RuntimeSettingsError(f"{name} must be an integer from 1 to 65535")
    parsed = int(value)
    if parsed > 65535:
        raise RuntimeSettingsError(f"{name} must be an integer from 1 to 65535")
    return parsed


def default_runtime_dir(environment: Mapping[str, str] | None = None) -> Path:
    source = os.environ if environment is None else environment
    if "RARDAR_RUNTIME_DIR" in source:
        return validate_absolute_path("RARDAR_RUNTIME_DIR", source["RARDAR_RUNTIME_DIR"])
    return _platform_state_root(source) / "Rardar" / "runtime"


def default_scheduler_status_path(environment: Mapping[str, str] | None = None) -> Path:
    return default_runtime_dir(environment) / "scheduler-status.json"


def load_runtime_layout(
    environment: Mapping[str, str] | None = None,
    *,
    application_root: Path = REPOSITORY_ROOT,
) -> RuntimeLayout:
    """Resolve the strict deployment contract without creating any paths."""
    source = os.environ if environment is None else environment
    default_home = application_root.expanduser().resolve()
    home = (
        validate_absolute_path("RARDAR_HOME", source["RARDAR_HOME"])
        if "RARDAR_HOME" in source
        else default_home
    )
    data_dir = (
        validate_absolute_path("RARDAR_DATA_DIR", source["RARDAR_DATA_DIR"])
        if "RARDAR_DATA_DIR" in source
        else home / "data"
    )
    runtime_dir = default_runtime_dir(source)
    data_lock_dir = (
        validate_absolute_path("RARDAR_DATA_LOCK_DIR", source["RARDAR_DATA_LOCK_DIR"])
        if "RARDAR_DATA_LOCK_DIR" in source
        else _platform_state_root(source) / "Rardar" / "runtime" / "data-locks"
    )
    vinext_port = validate_port(
        "RARDAR_VINEXT_PORT", source.get("RARDAR_VINEXT_PORT", str(DEFAULT_VINEXT_PORT))
    )
    runtime_status_port = validate_port(
        "RARDAR_RUNTIME_STATUS_PORT",
        source.get("RARDAR_RUNTIME_STATUS_PORT", str(DEFAULT_RUNTIME_STATUS_PORT)),
    )
    if vinext_port == runtime_status_port:
        raise RuntimeSettingsError(
            "RARDAR_VINEXT_PORT and RARDAR_RUNTIME_STATUS_PORT must be different"
        )
    for name in PERSISTENT_PATH_VARIABLES:
        if name in source:
            validate_absolute_path(name, source[name])
    return RuntimeLayout(
        home=home,
        data_dir=data_dir.resolve(),
        runtime_dir=runtime_dir,
        data_lock_dir=data_lock_dir.resolve(),
        vinext_port=vinext_port,
        runtime_status_port=runtime_status_port,
    )


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


def validate_trending_producer_enabled(value: object) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise RuntimeSettingsError(
        f"{TRENDING_PRODUCER_ENABLED_ENV} must be exactly true or false"
    )


def validate_vite_additional_allowed_hosts(value: object | None) -> tuple[str, ...]:
    """Validate Vite's optional exact-host environment contract.

    Vite deliberately accepts leading-dot suffix patterns and normalizes its
    environment input by trimming and dropping empty entries.  The managed
    Rardar runtime is stricter: every configured value must already be a
    canonical, exact ASCII FQDN before Vite is allowed to see it.
    """

    if value is None:
        return ()
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_VITE_ALLOWED_HOSTS_CONFIGURATION_LENGTH
    ):
        raise RuntimeSettingsError(
            f"{VITE_ADDITIONAL_ALLOWED_HOSTS_ENV} must contain 1 to "
            f"{MAX_VITE_ADDITIONAL_ALLOWED_HOSTS} exact ASCII FQDNs"
        )

    hosts = value.split(",")
    if not 1 <= len(hosts) <= MAX_VITE_ADDITIONAL_ALLOWED_HOSTS:
        raise RuntimeSettingsError(
            f"{VITE_ADDITIONAL_ALLOWED_HOSTS_ENV} must contain 1 to "
            f"{MAX_VITE_ADDITIONAL_ALLOWED_HOSTS} exact ASCII FQDNs"
        )

    validated: list[str] = []
    seen: set[str] = set()
    for host in hosts:
        if (
            not host
            or not host.isascii()
            or host != host.lower()
            or any(character.isspace() for character in host)
            or len(host) > MAX_DNS_HOSTNAME_LENGTH
            or host.startswith(".")
            or host.endswith(".")
            or "." not in host
            or host == "localhost"
            or host.endswith(".localhost")
        ):
            raise RuntimeSettingsError(
                f"{VITE_ADDITIONAL_ALLOWED_HOSTS_ENV} accepts canonical exact ASCII FQDNs only"
            )
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise RuntimeSettingsError(
                f"{VITE_ADDITIONAL_ALLOWED_HOSTS_ENV} does not accept IP literals"
            )
        labels = host.split(".")
        if any(_DNS_LABEL_PATTERN.fullmatch(label) is None for label in labels):
            raise RuntimeSettingsError(
                f"{VITE_ADDITIONAL_ALLOWED_HOSTS_ENV} accepts canonical exact ASCII FQDNs only"
            )
        if host in seen:
            raise RuntimeSettingsError(
                f"{VITE_ADDITIONAL_ALLOWED_HOSTS_ENV} does not accept duplicate hostnames"
            )
        seen.add(host)
        validated.append(host)
    return tuple(validated)


def load_runtime_settings(
    environment: Mapping[str, str] | None = None,
    *,
    schedule_at: str | None = None,
    schedule_timezone: str | None = None,
) -> RuntimeSettings:
    source = os.environ if environment is None else environment
    validate_vite_additional_allowed_hosts(
        source[VITE_ADDITIONAL_ALLOWED_HOSTS_ENV]
        if VITE_ADDITIONAL_ALLOWED_HOSTS_ENV in source
        else None
    )
    effective_at = schedule_at if schedule_at is not None else source.get(
        "RARDAR_SCHEDULE_AT", DEFAULT_SCHEDULE_AT
    )
    effective_timezone = schedule_timezone if schedule_timezone is not None else source.get(
        "RARDAR_SCHEDULE_TIMEZONE", DEFAULT_SCHEDULE_TIMEZONE
    )
    stale_hours = source.get("RARDAR_STALE_AFTER_HOURS", str(DEFAULT_STALE_AFTER_HOURS))
    producer_enabled = validate_trending_producer_enabled(
        source.get(
            TRENDING_PRODUCER_ENABLED_ENV,
            "true" if DEFAULT_TRENDING_PRODUCER_ENABLED else "false",
        )
    )
    validated_at = validate_schedule_at(effective_at)
    validated_timezone = validate_schedule_timezone(effective_timezone)
    if producer_enabled and (
        validated_at != DEFAULT_SCHEDULE_AT
        or validated_timezone != DEFAULT_SCHEDULE_TIMEZONE
    ):
        raise RuntimeSettingsError(
            f"{TRENDING_PRODUCER_ENABLED_ENV}=true requires the fixed "
            f"{DEFAULT_SCHEDULE_AT} {DEFAULT_SCHEDULE_TIMEZONE} product schedule"
        )
    return RuntimeSettings(
        schedule_at=validated_at,
        schedule_timezone=validated_timezone,
        stale_after_hours=validate_stale_after_hours(stale_hours),
        trending_producer_enabled=producer_enabled,
    )
