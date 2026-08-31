"""Deterministic fixed-phase schedule for Rardar's trending producer.

This module deliberately knows nothing about refresh, GitHub, generations, or
status persistence.  The managed Scheduler remains the sole owner of time and
uses these pure helpers to order the product events and bounded maintenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo


OBSERVATION_CADENCE_MINUTES = 120
OBSERVATION_PHASE_HOURS = tuple(range(0, 24, 2))
OBSERVATION_STARTUP_TOLERANCE_MINUTES = 10
EXPLOSION_SCHEDULE_AT = "08:00"

EventKind = Literal["observation", "refresh", "explosion", "discover", "retention"]
EVENT_PRIORITY: dict[EventKind, int] = {
    "observation": 0,
    "refresh": 1,
    "explosion": 2,
    "discover": 3,
    "retention": 4,
}


@dataclass(frozen=True, order=True)
class ScheduledEvent:
    """One intended event instant with deterministic same-phase priority."""

    scheduled_at: datetime
    priority: int
    kind: EventKind


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("scheduler instants must include a timezone")
    return value.astimezone(timezone.utc)


def _clock(value: str) -> tuple[int, int]:
    parts = value.split(":", 1)
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("schedule time must use HH:MM")
    hour, minute = (int(part) for part in parts)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("schedule time must use HH:MM")
    return hour, minute


def local_phase(
    local_day: date,
    hour: int,
    minute: int,
    timezone_name: str,
) -> datetime:
    """Return one local wall-clock phase as a UTC instant."""

    zone = ZoneInfo(timezone_name)
    return datetime.combine(local_day, time(hour, minute), tzinfo=zone).astimezone(
        timezone.utc
    )


def observation_phases_for_day(
    local_day: date,
    timezone_name: str,
) -> tuple[datetime, ...]:
    """Return all twelve fixed observation phases for one local date."""

    return tuple(
        local_phase(local_day, hour, 0, timezone_name)
        for hour in OBSERVATION_PHASE_HOURS
    )


def previous_observation_at(now: datetime, timezone_name: str) -> datetime:
    """Return the closest fixed phase at or before ``now``."""

    current = _utc(now)
    zone = ZoneInfo(timezone_name)
    local_now = current.astimezone(zone)
    hour = local_now.hour - local_now.hour % 2
    phase = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return phase.astimezone(timezone.utc)


def next_observation_at(now: datetime, timezone_name: str) -> datetime:
    """Return the first fixed observation phase strictly after ``now``."""

    previous = previous_observation_at(now, timezone_name)
    candidate = previous + timedelta(minutes=OBSERVATION_CADENCE_MINUTES)
    if candidate <= _utc(now):
        candidate += timedelta(minutes=OBSERVATION_CADENCE_MINUTES)
    return candidate


def startup_observation_catch_up(
    now: datetime,
    timezone_name: str,
    *,
    tolerance_minutes: int = OBSERVATION_STARTUP_TOLERANCE_MINUTES,
) -> datetime | None:
    """Return at most one recent phase eligible for startup observation."""

    current = _utc(now)
    phase = previous_observation_at(current, timezone_name)
    delay = current - phase
    if timedelta(0) <= delay <= timedelta(minutes=max(0, tolerance_minutes)):
        return phase
    return None


def next_daily_at(now: datetime, schedule_at: str, timezone_name: str) -> datetime:
    """Return the first daily wall-clock phase strictly after ``now``."""

    current = _utc(now)
    zone = ZoneInfo(timezone_name)
    local_now = current.astimezone(zone)
    hour, minute = _clock(schedule_at)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def scheduled_events_at(
    scheduled_at: datetime,
    *,
    refresh_at: str,
    timezone_name: str,
) -> tuple[ScheduledEvent, ...]:
    """Return deterministically ordered events due at one exact instant."""

    instant = _utc(scheduled_at)
    local = instant.astimezone(ZoneInfo(timezone_name))
    refresh_hour, refresh_minute = _clock(refresh_at)
    kinds: list[EventKind] = []
    if local.minute == 0 and local.second == 0 and local.microsecond == 0 and local.hour % 2 == 0:
        kinds.append("observation")
        kinds.append("discover")
    if (
        local.hour == refresh_hour
        and local.minute == refresh_minute
        and local.second == 0
        and local.microsecond == 0
    ):
        kinds.append("refresh")
    if (
        local.hour == 8
        and local.minute == 0
        and local.second == 0
        and local.microsecond == 0
    ):
        kinds.append("explosion")
        kinds.append("retention")
    return tuple(
        ScheduledEvent(instant, EVENT_PRIORITY[kind], kind)
        for kind in sorted(kinds, key=EVENT_PRIORITY.__getitem__)
    )


def next_scheduled_events(
    now: datetime,
    *,
    refresh_at: str,
    timezone_name: str,
) -> tuple[ScheduledEvent, ...]:
    """Return every event at the earliest future producer/runtime phase."""

    current = _utc(now)
    candidates = (
        next_observation_at(current, timezone_name),
        next_daily_at(current, refresh_at, timezone_name),
        next_daily_at(current, EXPLOSION_SCHEDULE_AT, timezone_name),
    )
    target = min(candidates)
    return scheduled_events_at(
        target,
        refresh_at=refresh_at,
        timezone_name=timezone_name,
    )


def first_exact_eligible_at(first_eight_capture_at: datetime) -> datetime:
    """Return the next local-day 08:00 endpoint after a first 08:00 capture."""

    instant = _utc(first_eight_capture_at)
    return instant + timedelta(hours=24)


__all__ = [
    "EVENT_PRIORITY",
    "EXPLOSION_SCHEDULE_AT",
    "OBSERVATION_CADENCE_MINUTES",
    "OBSERVATION_PHASE_HOURS",
    "OBSERVATION_STARTUP_TOLERANCE_MINUTES",
    "ScheduledEvent",
    "first_exact_eligible_at",
    "next_daily_at",
    "next_observation_at",
    "next_scheduled_events",
    "observation_phases_for_day",
    "previous_observation_at",
    "scheduled_events_at",
    "startup_observation_catch_up",
]
