from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from pipeline.producer_schedule import (
    OBSERVATION_PHASE_HOURS,
    first_exact_eligible_at,
    next_observation_at,
    next_scheduled_events,
    observation_phases_for_day,
    scheduled_events_at,
    startup_observation_catch_up,
)


class ProducerScheduleTests(unittest.TestCase):
    def test_one_local_day_has_the_twelve_fixed_phases(self) -> None:
        phases = observation_phases_for_day(date(2026, 8, 26), "Asia/Shanghai")
        self.assertEqual(len(phases), 12)
        self.assertEqual(
            tuple(phase.astimezone(timezone(timedelta(hours=8))).hour for phase in phases),
            OBSERVATION_PHASE_HOURS,
        )
        self.assertEqual(len(set(phases)), 12)

    def test_next_observation_crosses_local_midnight_without_repeating(self) -> None:
        now = datetime(2026, 8, 26, 15, 59, 59, tzinfo=timezone.utc)
        self.assertEqual(
            next_observation_at(now, "Asia/Shanghai"),
            datetime(2026, 8, 26, 16, 0, tzinfo=timezone.utc),
        )
        exact = datetime(2026, 8, 26, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(
            next_observation_at(exact, "Asia/Shanghai"),
            datetime(2026, 8, 26, 18, 0, tzinfo=timezone.utc),
        )

    def test_eight_o_clock_order_is_observation_refresh_explosion(self) -> None:
        phase = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
        events = scheduled_events_at(
            phase,
            refresh_at="08:00",
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual([event.kind for event in events], ["observation", "refresh", "explosion"])
        self.assertEqual(len({(event.scheduled_at, event.kind) for event in events}), 3)

    def test_next_events_never_mix_future_and_past_phases(self) -> None:
        now = datetime(2026, 8, 26, 23, 59, 59, tzinfo=timezone.utc)
        events = next_scheduled_events(
            now,
            refresh_at="08:00",
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual([event.kind for event in events], ["observation", "refresh", "explosion"])
        self.assertTrue(all(event.scheduled_at > now for event in events))
        self.assertEqual(len({event.scheduled_at for event in events}), 1)

    def test_startup_catch_up_accepts_nine_minutes_but_not_eleven(self) -> None:
        phase = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(
            startup_observation_catch_up(
                phase + timedelta(minutes=9),
                "Asia/Shanghai",
            ),
            phase,
        )
        self.assertIsNone(
            startup_observation_catch_up(
                phase + timedelta(minutes=11),
                "Asia/Shanghai",
            )
        )

    def test_startup_catch_up_returns_only_the_closest_past_phase(self) -> None:
        now = datetime(2026, 8, 26, 4, 7, tzinfo=timezone.utc)
        phase = startup_observation_catch_up(now, "Asia/Shanghai")
        self.assertEqual(phase, datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc))

    def test_first_exact_endpoint_is_mechanical_t_plus_twenty_four_hours(self) -> None:
        first = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(first_exact_eligible_at(first), first + timedelta(hours=24))


if __name__ == "__main__":
    unittest.main()
