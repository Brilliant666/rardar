from __future__ import annotations

import json
import io
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.analyze_repository import RemoteCloneLifecycleError
from pipeline.generations import CandidateGenerationError
from pipeline.runtime_settings import SCHEDULER_ALREADY_RUNNING_EXIT_CODE, RuntimeSettings
from pipeline.producer_schedule import scheduled_events_at
from pipeline.scheduler import (
    SchedulerStatusStore,
    _default_producer_status,
    _execute_scheduled_events,
    _record_explosion_not_ready,
    _restore_producer_status,
    _run_discover_phase,
    _run_explosion_phase,
    _run_observation_phase,
    _run_producer_scheduler,
    _run_refresh_sequence,
    _run_scheduler,
    SchedulerAlreadyRunningError,
    committed_refresh_at,
    main as scheduler_main,
    next_run_at,
    parse_clock,
    run_cycle,
    scheduler_instance_lock,
    should_catch_up,
    should_retry,
)
from pipeline.trending_observations import TrendingObservationError
from pipeline.trending_explosion import TrendingExplosionError


def _hold_scheduler_lock(
    data_dir: str,
    lock_root: str,
    acquired_path: str,
    release_path: str,
) -> None:
    with scheduler_instance_lock(Path(data_dir), lock_root=Path(lock_root)):
        Path(acquired_path).write_text("acquired", encoding="utf-8")
        deadline = time.monotonic() + 10
        while not Path(release_path).exists() and time.monotonic() < deadline:
            time.sleep(0.05)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class SchedulerTests(unittest.TestCase):
    def test_status_store_keeps_refresh_producer_and_heartbeat_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scheduler.json"
            store = SchedulerStatusStore(path)
            producer = {"enabled": True, "observation": {"state": "healthy"}}
            store.update({"state": "healthy", "producer": producer})
            store.update({"heartbeatAt": "2026-08-26T04:00:15+00:00"})
            store.replace_refresh(
                {
                    "state": "running",
                    "lastRunStartedAt": "2026-08-26T00:00:00+00:00",
                }
            )
            store.update({"producer": {"observation": {"state": "degraded"}}})

            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["state"], "running")
        self.assertEqual(stored["lastRunStartedAt"], "2026-08-26T00:00:00+00:00")
        self.assertTrue(stored["producer"]["enabled"])
        self.assertEqual(stored["producer"]["observation"]["state"], "degraded")
        self.assertNotIn("heartbeatAt", stored)

    def test_refresh_cycle_keeps_nested_producer_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SchedulerStatusStore(root / "scheduler.json")
            store.update(
                {
                    "producer": {
                        "enabled": True,
                        "observation": {"state": "healthy"},
                    }
                }
            )
            with (
                patch(
                    "pipeline.scheduler.refresh",
                    return_value={"sourceCount": 3, "projectCount": 2, "signalCount": 1},
                ),
                patch(
                    "pipeline.scheduler.audit_data",
                    return_value={"status": "healthy", "warningCount": 0, "issues": []},
                ),
            ):
                result = run_cycle(root, 0, store.path, status_store=store)
            stored = store.snapshot()

        self.assertEqual(result["state"], "healthy")
        self.assertEqual(stored["state"], "healthy")
        self.assertEqual(stored["producer"]["observation"]["state"], "healthy")

    def test_producer_telemetry_is_recovered_without_unreviewed_paths(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        now = datetime(2026, 8, 26, 4, 1, tzinfo=timezone.utc)
        recovered = _restore_producer_status(
            {
                "state": "warming_up",
                "first08CaptureAt": "2026-08-26T00:00:00+00:00",
                "observation": {
                    "state": "healthy",
                    "lastCaptureId": "trending-v1-20260826T040000Z",
                    "capturePath": "C:/secret/path",
                },
                "explosion": {
                    "state": "warming_up",
                    "candidatePath": "C:/secret/candidate",
                },
            },
            settings,
            now,
        )
        serialized = json.dumps(recovered)
        self.assertEqual(recovered["observation"]["state"], "healthy")
        self.assertNotIn("capturePath", serialized)
        self.assertNotIn("candidatePath", serialized)

    def test_observation_retry_is_once_bounded_and_keeps_scheduled_at(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        phase = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)
        clock = MutableClock(phase + timedelta(minutes=1))
        transient = TrendingObservationError(
            "all_candidate_queries_failed",
            "Bearer fixture-secret",
            details={"retryable": True},
        )
        success = {
            "state": "captured",
            "captureId": "trending-v1-20260826T040000Z",
            "coverageState": "healthy",
            "windowEligible": True,
            "successfulQueryCount": 9,
            "failedQueryCount": 0,
            "candidateCount": 10,
            "observationCount": 10,
            "metadataFailureCount": 0,
            "carryForwardCount": 4,
            "newRepositoryCount": 6,
            "captureDelaySeconds": 90,
            "capturePath": "C:/must-not-reach-status.json",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SchedulerStatusStore(Path(directory) / "scheduler.json")
            producer = _default_producer_status(settings, clock())
            with (
                patch.dict(os.environ, {"GITHUB_TOKEN": "fixture-secret"}),
                patch(
                    "pipeline.scheduler.run_observer",
                    side_effect=[transient, success],
                ) as observer,
            ):
                result = _run_observation_phase(
                    Path(directory) / "data",
                    phase,
                    settings,
                    store,
                    producer,
                    clock=clock,
                    sleeper=clock.sleep,
                )
            stored = store.snapshot()

        self.assertEqual(result, success)
        self.assertEqual(observer.call_count, 2)
        self.assertTrue(
            all(call.kwargs["scheduled_at"] == phase for call in observer.call_args_list)
        )
        self.assertEqual(stored["producer"]["observation"]["retryCount"], 1)
        self.assertEqual(stored["producer"]["observation"]["carryForwardCount"], 4)
        serialized = json.dumps(stored)
        self.assertNotIn("fixture-secret", serialized)
        self.assertNotIn("capturePath", serialized)

    def test_nonretryable_observation_failure_is_redacted_and_does_not_retry(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        phase = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)
        clock = MutableClock(phase + timedelta(minutes=1))
        failure = TrendingObservationError(
            "github_token_required",
            "fixture-secret must never be logged",
        )
        with tempfile.TemporaryDirectory() as directory:
            store = SchedulerStatusStore(Path(directory) / "scheduler.json")
            producer = _default_producer_status(settings, clock())
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"GITHUB_TOKEN": "fixture-secret"}),
                patch("pipeline.scheduler.run_observer", side_effect=failure) as observer,
                redirect_stdout(output),
            ):
                result = _run_observation_phase(
                    Path(directory) / "data",
                    phase,
                    settings,
                    store,
                    producer,
                    clock=clock,
                    sleeper=clock.sleep,
                )
            stored = store.snapshot()

        self.assertIsNone(result)
        observer.assert_called_once()
        self.assertEqual(
            stored["producer"]["observation"]["lastErrorCode"],
            "github_token_required",
        )
        self.assertNotIn("fixture-secret", output.getvalue())
        self.assertNotIn("fixture-secret", json.dumps(stored))

    def test_same_phase_execution_order_is_deterministic_and_isolated(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        phase = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
        clock = MutableClock(phase)
        events = scheduled_events_at(
            phase,
            refresh_at="08:00",
            timezone_name="Asia/Shanghai",
        )
        arguments = SimpleNamespace(data_dir=Path("unused"), analyze_top=0)
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            store = SchedulerStatusStore(Path(directory) / "scheduler.json")
            producer = _default_producer_status(settings, clock())
            with (
                patch(
                    "pipeline.scheduler._run_observation_phase",
                    side_effect=lambda *_args, **_kwargs: calls.append("observation")
                    or {"state": "captured"},
                ),
                patch(
                    "pipeline.scheduler._run_refresh_sequence",
                    side_effect=lambda *_args, **_kwargs: calls.append("refresh")
                    or {"state": "failed"},
                ),
                patch(
                    "pipeline.scheduler._run_explosion_phase",
                    side_effect=lambda *_args, **_kwargs: calls.append("explosion")
                    or {"state": "published"},
                ),
                patch(
                    "pipeline.scheduler._run_discover_phase",
                    side_effect=lambda *_args, **_kwargs: calls.append("discover"),
                ),
            ):
                result = _execute_scheduled_events(
                    events,
                    arguments,
                    settings,
                    store,
                    producer,
                    clock=clock,
                    sleeper=clock.sleep,
                )

        self.assertEqual(calls, ["observation", "refresh", "explosion", "discover"])
        self.assertEqual(result, {"state": "failed"})

    def test_eight_o_clock_discover_requires_successful_explosion(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        phase = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
        clock = MutableClock(phase)
        events = scheduled_events_at(
            phase,
            refresh_at="08:00",
            timezone_name="Asia/Shanghai",
        )
        arguments = SimpleNamespace(data_dir=Path("unused"), analyze_top=0)
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            store = SchedulerStatusStore(Path(directory) / "scheduler.json")
            producer = _default_producer_status(settings, clock())
            with (
                patch(
                    "pipeline.scheduler._run_observation_phase",
                    return_value={"state": "captured"},
                ),
                patch(
                    "pipeline.scheduler._run_refresh_sequence",
                    return_value={"state": "healthy"},
                ),
                patch("pipeline.scheduler._run_explosion_phase", return_value=None),
                patch(
                    "pipeline.scheduler._run_discover_phase",
                    side_effect=lambda *_args, **_kwargs: calls.append("discover"),
                ),
            ):
                _execute_scheduled_events(
                    events,
                    arguments,
                    settings,
                    store,
                    producer,
                    clock=clock,
                    sleeper=clock.sleep,
                )
        self.assertEqual(calls, [])

    def test_observation_failure_does_not_block_refresh_or_explosion(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        phase = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
        clock = MutableClock(phase)
        events = scheduled_events_at(
            phase,
            refresh_at="08:00",
            timezone_name="Asia/Shanghai",
        )
        arguments = SimpleNamespace(data_dir=Path("unused"), analyze_top=0)
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            store = SchedulerStatusStore(Path(directory) / "scheduler.json")
            producer = _default_producer_status(settings, clock())
            with (
                patch(
                    "pipeline.scheduler.run_observer",
                    side_effect=TrendingObservationError(
                        "github_token_required",
                        "missing token",
                    ),
                ),
                patch(
                    "pipeline.scheduler._run_refresh_sequence",
                    side_effect=lambda *_args, **_kwargs: calls.append("refresh")
                    or {"state": "healthy"},
                ),
                patch(
                    "pipeline.scheduler._run_explosion_phase",
                    side_effect=lambda *_args, **_kwargs: calls.append("explosion"),
                ),
                redirect_stdout(io.StringIO()),
            ):
                result = _execute_scheduled_events(
                    events,
                    arguments,
                    settings,
                    store,
                    producer,
                    clock=clock,
                    sleeper=clock.sleep,
                )
            stored = store.snapshot()

        self.assertEqual(calls, ["refresh", "explosion"])
        self.assertEqual(result, {"state": "healthy"})
        self.assertEqual(stored["producer"]["observation"]["state"], "failed")
        self.assertEqual(
            stored["producer"]["observation"]["lastErrorCode"],
            "github_token_required",
        )

    def test_discover_failure_is_isolated_and_redacted_in_telemetry(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        phase = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
        clock = MutableClock(phase)
        with tempfile.TemporaryDirectory() as directory:
            store = SchedulerStatusStore(Path(directory) / "scheduler.json")
            producer = _default_producer_status(settings, clock())
            with (
                patch(
                    "pipeline.scheduler.derive_trending_discover",
                    side_effect=RuntimeError("secret absolute path must not escape"),
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                result = _run_discover_phase(
                    Path(directory) / "data",
                    phase,
                    settings,
                    store,
                    producer,
                    clock=clock,
                )
            stored = store.snapshot()["producer"]["discover"]
        self.assertIsNone(result)
        self.assertEqual(stored["state"], "failed")
        self.assertEqual(stored["lastErrorCode"], "discover_internal_error")
        self.assertNotIn("secret absolute path", output.getvalue())

    def test_refresh_sequence_keeps_three_attempts_and_five_minute_delays(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        clock = MutableClock(datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc))
        arguments = SimpleNamespace(data_dir=Path("unused"), analyze_top=0)
        failure = {"state": "failed", "retryable": True}
        with tempfile.TemporaryDirectory() as directory:
            store = SchedulerStatusStore(Path(directory) / "scheduler.json")
            with patch(
                "pipeline.scheduler.run_cycle",
                side_effect=[dict(failure), dict(failure), dict(failure)],
            ) as cycle:
                result = _run_refresh_sequence(
                    arguments,
                    settings,
                    store,
                    clock=clock,
                    sleeper=clock.sleep,
                )

        self.assertEqual(result["state"], "failed")
        self.assertEqual(cycle.call_count, 3)
        self.assertEqual(clock(), datetime(2026, 8, 27, 0, 10, tzinfo=timezone.utc))

    def test_remote_clone_nonretryable_refresh_is_attempted_once(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        clock = MutableClock(datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc))
        arguments = SimpleNamespace(data_dir=Path("unused"), analyze_top=0)
        with tempfile.TemporaryDirectory() as directory:
            store = SchedulerStatusStore(Path(directory) / "scheduler.json")
            with patch(
                "pipeline.scheduler.run_cycle",
                return_value={"state": "failed", "retryable": False},
            ) as cycle:
                result = _run_refresh_sequence(
                    arguments,
                    settings,
                    store,
                    clock=clock,
                    sleeper=clock.sleep,
                )
        self.assertEqual(result, {"state": "failed", "retryable": False})
        cycle.assert_called_once()

    def test_explosion_warmup_and_failure_are_telemetry_not_scheduler_exit(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        window_end = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
        clock = MutableClock(window_end + timedelta(minutes=2))
        warmup = {
            "state": "derived",
            "generationId": "generation-warmup",
            "windowState": "warming_up",
            "coverageState": "warming_up",
            "exactCount": 0,
            "pendingCount": 10,
            "conflictCount": 0,
            "candidatePath": "C:/must-not-reach-status",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SchedulerStatusStore(Path(directory) / "scheduler.json")
            producer = _default_producer_status(settings, clock())
            with patch(
                "pipeline.scheduler.derive_trending_explosion",
                return_value=warmup,
            ):
                result = _run_explosion_phase(
                    Path(directory) / "data",
                    window_end,
                    settings,
                    store,
                    producer,
                    clock=clock,
                )
            stored = store.snapshot()
            self.assertEqual(result, warmup)
            self.assertEqual(stored["producer"]["explosion"]["state"], "warming_up")
            self.assertNotIn("candidatePath", json.dumps(stored))

            already = {
                **warmup,
                "state": "already_derived",
                "windowState": "exact",
                "coverageState": "healthy",
            }
            with patch(
                "pipeline.scheduler.derive_trending_explosion",
                return_value=already,
            ):
                repeated = _run_explosion_phase(
                    Path(directory) / "data",
                    window_end,
                    settings,
                    store,
                    producer,
                    clock=clock,
                )
            stored = store.snapshot()
            self.assertEqual(repeated, already)
            self.assertEqual(
                stored["producer"]["explosion"]["state"],
                "already_derived",
            )
            self.assertEqual(stored["producer"]["state"], "healthy")

            output = io.StringIO()
            with (
                patch(
                    "pipeline.scheduler.derive_trending_explosion",
                    side_effect=TrendingExplosionError(
                        "explosion_current_capture_missing",
                        "C:/private/path must not pass",
                    ),
                ),
                redirect_stdout(output),
            ):
                blocked = _run_explosion_phase(
                    Path(directory) / "data",
                    window_end,
                    settings,
                    store,
                    producer,
                    clock=clock,
                )
            stored = store.snapshot()

        self.assertIsNone(blocked)
        self.assertEqual(stored["producer"]["explosion"]["state"], "blocked")
        self.assertEqual(
            stored["producer"]["explosion"]["lastErrorCode"],
            "explosion_current_capture_missing",
        )
        self.assertNotIn("private/path", output.getvalue())

    def test_startup_not_ready_does_not_retain_prior_explosion_result(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        window_end = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
        now = window_end + timedelta(hours=1)
        with tempfile.TemporaryDirectory() as directory:
            store = SchedulerStatusStore(Path(directory) / "scheduler.json")
            producer = _default_producer_status(settings, now)
            producer["explosion"].update(
                {
                    "generationId": "prior-generation",
                    "windowState": "exact",
                    "coverageState": "healthy",
                    "exactCount": 20,
                }
            )
            _record_explosion_not_ready(
                window_end,
                "explosion_current_capture_missing",
                settings,
                store,
                producer,
                now,
            )
            explosion = store.snapshot()["producer"]["explosion"]

        self.assertEqual(explosion["state"], "not_ready")
        self.assertIsNone(explosion["generationId"])
        self.assertIsNone(explosion["windowState"])
        self.assertIsNone(explosion["coverageState"])
        self.assertIsNone(explosion["exactCount"])

    def test_startup_observation_catch_up_runs_at_nine_minutes_not_eleven(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        phase = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)
        arguments = SimpleNamespace(
            data_dir=Path("unused"),
            analyze_top=0,
            once=False,
            skip_initial=True,
        )

        class StopLoop(RuntimeError):
            pass

        for minutes, expected_calls in ((9, 1), (11, 0)):
            with self.subTest(minutes=minutes), tempfile.TemporaryDirectory() as directory:
                clock = MutableClock(phase + timedelta(minutes=minutes))
                status_path = Path(directory) / "scheduler.json"
                with (
                    patch("pipeline.scheduler.should_catch_up", return_value=False),
                    patch(
                        "pipeline.scheduler._run_observation_phase"
                    ) as observation,
                    patch("pipeline.scheduler._run_discover_phase") as discover,
                    patch(
                        "pipeline.scheduler._eligible_capture_exists",
                        return_value=(False, "explosion_current_capture_missing"),
                    ),
                    patch("pipeline.scheduler._wait_until", side_effect=StopLoop()),
                ):
                    with self.assertRaises(StopLoop):
                        _run_producer_scheduler(
                            arguments,
                            settings,
                            status_path,
                            clock=clock,
                            sleeper=clock.sleep,
                        )
                self.assertEqual(observation.call_count, expected_calls)
                self.assertEqual(discover.call_count, expected_calls)
                if expected_calls:
                    self.assertEqual(observation.call_args.args[1], phase)

    def test_eight_o_clock_startup_catch_up_preserves_producer_order(self) -> None:
        settings = RuntimeSettings("08:00", "Asia/Shanghai", 36, True)
        phase = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
        clock = MutableClock(phase + timedelta(minutes=5))
        arguments = SimpleNamespace(
            data_dir=Path("unused"),
            analyze_top=0,
            once=False,
            skip_initial=True,
        )
        calls: list[str] = []

        class StopLoop(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "scheduler.json"
            with (
                patch("pipeline.scheduler.should_catch_up", return_value=True),
                patch(
                    "pipeline.scheduler._run_observation_phase",
                    side_effect=lambda *_args, **_kwargs: calls.append("observation")
                    or {"state": "captured"},
                ),
                patch(
                    "pipeline.scheduler._run_refresh_sequence",
                    side_effect=lambda *_args, **_kwargs: calls.append("refresh")
                    or {"state": "healthy"},
                ),
                patch(
                    "pipeline.scheduler._eligible_capture_exists",
                    return_value=(True, None),
                ),
                patch(
                    "pipeline.scheduler._run_explosion_phase",
                    side_effect=lambda *_args, **_kwargs: calls.append("explosion")
                    or {"state": "published"},
                ),
                patch(
                    "pipeline.scheduler._run_discover_phase",
                    side_effect=lambda *_args, **_kwargs: calls.append("discover")
                    or {"state": "published"},
                ),
                patch("pipeline.scheduler._wait_until", side_effect=StopLoop()),
            ):
                with self.assertRaises(StopLoop):
                    _run_producer_scheduler(
                        arguments,
                        settings,
                        status_path,
                        clock=clock,
                        sleeper=clock.sleep,
                    )
        self.assertEqual(calls, ["observation", "refresh", "explosion", "discover"])

    def test_cli_uses_external_data_directory_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = (Path(directory) / "external-data").resolve()
            status_path = Path(directory) / "scheduler-status.json"
            settings = RuntimeSettings("08:00", "Asia/Shanghai", 36)
            with (
                patch.dict(os.environ, {"RARDAR_DATA_DIR": str(data_dir)}, clear=False),
                patch.object(
                    sys,
                    "argv",
                    [
                        "pipeline.scheduler",
                        "--status-path",
                        str(status_path),
                        "--once",
                    ],
                ),
                patch("pipeline.scheduler.load_runtime_settings", return_value=settings),
                patch("pipeline.scheduler.scheduler_instance_lock") as lock,
                patch("pipeline.scheduler._run_scheduler") as run_scheduler,
            ):
                lock.return_value.__enter__.return_value = None
                lock.return_value.__exit__.return_value = False
                scheduler_main()

            lock.assert_called_once_with(data_dir)
            run_scheduler.assert_called_once()
            self.assertEqual(run_scheduler.call_args.args[0].data_dir, data_dir)

    def test_only_one_scheduler_can_own_a_canonical_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            lock_root = root / "locks"
            acquired = root / "acquired"
            release = root / "release"
            owner = multiprocessing.Process(
                target=_hold_scheduler_lock,
                args=(str(data_dir), str(lock_root), str(acquired), str(release)),
            )
            owner.start()
            try:
                deadline = time.monotonic() + 5
                while not acquired.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(acquired.exists(), "scheduler owner did not acquire its lock")
                with self.assertRaises(SchedulerAlreadyRunningError):
                    with scheduler_instance_lock(
                        data_dir / ".." / "data",
                        lock_root=lock_root,
                    ):
                        self.fail("a second scheduler acquired the canonical data directory")
            finally:
                release.write_text("release", encoding="utf-8")
                owner.join(timeout=5)
                if owner.is_alive():
                    owner.terminate()
                    owner.join(timeout=2)
            self.assertEqual(owner.exitcode, 0)

    def test_scheduler_lock_conflict_exits_before_status_or_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_path = root / "scheduler-status.json"
            arguments = [
                "pipeline.scheduler",
                "--data-dir",
                str(root / "data"),
                "--status-path",
                str(status_path),
                "--once",
            ]
            with (
                patch.object(sys, "argv", arguments),
                patch(
                    "pipeline.scheduler.scheduler_instance_lock",
                    side_effect=SchedulerAlreadyRunningError("already owned"),
                ),
                patch("pipeline.scheduler.refresh") as refresh_call,
            ):
                with self.assertRaises(SystemExit) as stopped:
                    scheduler_main()
            self.assertEqual(stopped.exception.code, SCHEDULER_ALREADY_RUNNING_EXIT_CODE)
            self.assertFalse(status_path.exists())
            refresh_call.assert_not_called()

    def test_clone_lifecycle_failure_is_non_retryable(self) -> None:
        lifecycle_error = RemoteCloneLifecycleError(
            "remote_clone_process_tree_cleanup_failed", "simulated"
        )

        def fail_refresh(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise CandidateGenerationError(
                "candidate_build_failed",
                "refresh candidate build failed",
                generation_id="candidate-1",
                stage="build",
            ) from lifecycle_error

        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "scheduler.json"
            with patch("pipeline.scheduler.refresh", side_effect=fail_refresh):
                result = run_cycle(Path("unused"), 0, status_path)
            stored = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["retryable"])
        self.assertEqual(
            result["remoteAnalysisErrorCode"],
            "remote_clone_process_tree_cleanup_failed",
        )
        self.assertFalse(stored["retryable"])
        self.assertEqual(
            stored["remoteAnalysisErrorCode"],
            "remote_clone_process_tree_cleanup_failed",
        )
        self.assertFalse(
            should_retry(
                result["state"],
                1,
                retryable=result["retryable"],
            )
        )

    def test_committed_refresh_allows_later_derived_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = "2026-07-10T00:00:02+00:00"
            artifacts = {
                root / "snapshots" / "latest.json": {"captured_at": captured},
                root / "catalog" / "latest.json": {"capturedAt": captured},
                root / "signals" / "latest.json": {"capturedAt": captured},
                root / "queues" / "codex.json": {"generatedAt": captured},
            }
            for path, payload in artifacts.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(committed_refresh_at(root), captured)
            (root / "queues" / "codex.json").write_text(
                json.dumps({"generatedAt": "2026-07-10T00:30:00+00:00"}),
                encoding="utf-8",
            )
            (root / "signals" / "latest.json").write_text(
                json.dumps({"capturedAt": "2026-07-10T00:20:00+00:00"}),
                encoding="utf-8",
            )
            self.assertEqual(committed_refresh_at(root), captured)

            (root / "queues" / "codex.json").write_text(
                json.dumps({"generatedAt": "2026-07-10T00:10:00+00:00"}),
                encoding="utf-8",
            )
            self.assertIsNone(committed_refresh_at(root))

            (root / "queues" / "codex.json").write_text(
                json.dumps({"generatedAt": captured}),
                encoding="utf-8",
            )
            (root / "signals" / "latest.json").write_text(
                json.dumps({"capturedAt": captured}),
                encoding="utf-8",
            )
            (root / "catalog" / "latest.json").write_text(
                json.dumps({"capturedAt": "2026-07-09T00:00:00+00:00"}),
                encoding="utf-8",
            )
            self.assertIsNone(committed_refresh_at(root))

    def test_present_pointer_is_strict_and_never_falls_back_to_flat_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = "2026-07-10T00:00:02+00:00"
            artifacts = {
                root / "snapshots/latest.json": {"captured_at": captured},
                root / "catalog/latest.json": {"capturedAt": captured},
                root / "signals/latest.json": {"capturedAt": captured},
                root / "queues/codex.json": {"generatedAt": captured},
            }
            for path, payload in artifacts.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload), encoding="utf-8")
            (root / "current.json").write_text("{}", encoding="utf-8")

            self.assertIsNone(committed_refresh_at(root))

    def test_cycle_publishes_running_state_before_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "scheduler.json"

            def inspect_running_state(*_args: object, **_kwargs: object) -> dict[str, object]:
                running = json.loads(status_path.read_text(encoding="utf-8"))
                self.assertEqual(running["state"], "running")
                self.assertIsNone(running["lastRunCompletedAt"])
                self.assertIsNotNone(running["heartbeatAt"])
                return {"sourceCount": 3, "projectCount": 2, "signalCount": 1}

            with (
                patch("pipeline.scheduler.refresh", side_effect=inspect_running_state),
                patch(
                    "pipeline.scheduler.audit_data",
                    return_value={
                        "status": "healthy",
                        "warningCount": 0,
                        "issues": [],
                        "observedProjectCount": 2,
                        "observedNetStarChange": 42,
                        "dailyTrackCounts": {"recentMomentum": 3, "longTerm": 2},
                        "historyCount": 1,
                        "successfulQueryCount": 6,
                        "failedQueryCount": 1,
                        "healthySourceCount": 5,
                        "failedSourceCount": 1,
                        "analysisFailureCount": 2,
                        "staticAnalysisRequiredCount": 2,
                    },
                ),
            ):
                result = run_cycle(Path(directory), 0, status_path)

            stored = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(result["state"], "healthy")
            self.assertEqual(stored["state"], "healthy")
            self.assertEqual(stored["lastSuccessfulRefreshAt"], stored["lastRunCompletedAt"])
            self.assertEqual(stored["candidateCount"], 3)
            self.assertEqual(stored["dataAuditStatus"], "healthy")
            self.assertEqual(stored["dataAuditSummary"]["observedNetStarChange"], 42)
            self.assertEqual(stored["dataAuditSummary"]["successfulQueryCount"], 6)
            self.assertEqual(stored["dataAuditSummary"]["failedQueryCount"], 1)
            self.assertEqual(stored["dataAuditSummary"]["failedSourceCount"], 1)
            self.assertEqual(stored["dataAuditSummary"]["analysisFailureCount"], 2)
            self.assertIsNotNone(stored["lastRunCompletedAt"])

    def test_failed_cycle_preserves_the_last_successful_refresh_timestamp(self) -> None:
        previous_success = "2026-07-09T00:02:00+00:00"
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "scheduler.json"
            status_path.write_text(
                json.dumps(
                    {
                        "state": "healthy",
                        "lastRunCompletedAt": previous_success,
                        "lastSuccessfulRefreshAt": previous_success,
                    }
                ),
                encoding="utf-8",
            )
            with patch("pipeline.scheduler.refresh", side_effect=RuntimeError("offline")):
                result = run_cycle(Path(directory), 0, status_path)

            stored = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["lastSuccessfulRefreshAt"], previous_success)
        self.assertEqual(stored["lastSuccessfulRefreshAt"], previous_success)

    def test_daemon_restart_preserves_the_last_successful_refresh_timestamp(self) -> None:
        previous_success = "2026-07-09T00:02:00+00:00"

        class StopLoop(RuntimeError):
            pass

        for has_new_field in (True, False):
            with self.subTest(has_new_field=has_new_field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                status_path = root / "scheduler.json"
                stored_status = {
                    "state": "healthy",
                    "lastRunCompletedAt": previous_success,
                    "retryable": True,
                }
                if has_new_field:
                    stored_status["lastSuccessfulRefreshAt"] = previous_success
                status_path.write_text(json.dumps(stored_status), encoding="utf-8")
                arguments = SimpleNamespace(
                    once=False,
                    skip_initial=True,
                    analyze_top=0,
                    data_dir=root,
                )
                captured = []

                def capture_status(_path: Path, payload: dict[str, object]) -> None:
                    captured.append(payload)
                    raise StopLoop()

                with (
                    patch("pipeline.scheduler.committed_refresh_at", return_value=None),
                    patch("pipeline.scheduler.should_catch_up", return_value=False),
                    patch(
                        "pipeline.scheduler.next_run_at",
                        return_value=datetime.now(timezone.utc) + timedelta(days=1),
                    ),
                    patch("pipeline.scheduler._write_status", side_effect=capture_status),
                ):
                    with self.assertRaises(StopLoop):
                        _run_scheduler(
                            arguments,
                            RuntimeSettings("08:00", "Asia/Shanghai", 36),
                            status_path,
                        )

                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0]["lastSuccessfulRefreshAt"], previous_success)

    def test_cycle_fails_when_committed_data_fails_audit(self) -> None:
        catalog = {"sourceCount": 3, "projectCount": 2, "signalCount": 1}
        with (
            patch("pipeline.scheduler.refresh", return_value=catalog),
            patch(
                "pipeline.scheduler.audit_data",
                return_value={
                    "status": "failed",
                    "warningCount": 0,
                    "issues": [{"code": "snapshot_count_mismatch"}],
                },
            ),
        ):
            result = run_cycle(Path("unused"), 0)

        self.assertEqual(result["state"], "failed")
        self.assertIn("snapshot_count_mismatch", str(result["lastError"]))

    def test_next_run_uses_shanghai_clock(self) -> None:
        now = datetime(2026, 7, 10, 1, tzinfo=timezone.utc)  # 09:00 Asia/Shanghai
        target = next_run_at(now, 8, 0, "Asia/Shanghai")
        self.assertEqual(target, datetime(2026, 7, 11, 0, tzinfo=timezone.utc))

    def test_future_time_can_run_same_day(self) -> None:
        now = datetime(2026, 7, 10, 1, tzinfo=timezone.utc)  # 09:00 Asia/Shanghai
        target = next_run_at(now, 10, 30, "Asia/Shanghai")
        self.assertEqual(target, datetime(2026, 7, 10, 2, 30, tzinfo=timezone.utc))

    def test_rejects_invalid_clock(self) -> None:
        for value in ("25:00", "8:00", "08:0", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_clock(value)

    def test_catches_up_incomplete_run_after_schedule(self) -> None:
        now = datetime(2026, 7, 10, 0, 5, tzinfo=timezone.utc)  # 08:05 Asia/Shanghai
        self.assertTrue(should_catch_up(now, None, "running", 8, 0, "Asia/Shanghai"))

    def test_does_not_repeat_completed_run(self) -> None:
        now = datetime(2026, 7, 10, 0, 5, tzinfo=timezone.utc)
        self.assertFalse(
            should_catch_up(
                now,
                "2026-07-10T00:03:00+00:00",
                "healthy",
                8,
                0,
                "Asia/Shanghai",
            )
        )

    def test_committed_snapshot_prevents_duplicate_catch_up_after_status_crash(self) -> None:
        now = datetime(2026, 7, 10, 0, 5, tzinfo=timezone.utc)
        self.assertFalse(
            should_catch_up(
                now,
                None,
                "running",
                8,
                0,
                "Asia/Shanghai",
                latest_snapshot_at="2026-07-10T00:00:02+00:00",
            )
        )

    def test_retries_failed_run_within_catch_up_window(self) -> None:
        now = datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc)
        self.assertTrue(
            should_catch_up(
                now,
                "2026-07-10T00:02:00+00:00",
                "failed",
                8,
                0,
                "Asia/Shanghai",
                latest_snapshot_at="2026-07-10T00:00:02+00:00",
            )
        )

    def test_nonretryable_failure_does_not_catch_up_in_same_window(self) -> None:
        now = datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc)
        self.assertFalse(
            should_catch_up(
                now,
                "2026-07-10T00:02:00+00:00",
                "failed",
                8,
                0,
                "Asia/Shanghai",
                retryable=False,
            )
        )

    def test_nonretryable_failure_from_previous_day_catches_up_for_new_cycle(self) -> None:
        now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
        self.assertTrue(
            should_catch_up(
                now,
                "2026-07-10T00:02:00+00:00",
                "failed",
                8,
                0,
                "Asia/Shanghai",
                retryable=False,
            )
        )

    def test_nonretryable_failure_without_trustworthy_completion_does_not_catch_up(self) -> None:
        now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
        for completed in (None, "not-a-timestamp"):
            with self.subTest(completed=completed):
                self.assertFalse(
                    should_catch_up(
                        now,
                        completed,
                        "failed",
                        8,
                        0,
                        "Asia/Shanghai",
                        retryable=False,
                    )
                )

    def test_does_not_catch_up_outside_window(self) -> None:
        now = datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc)  # 21:00 Asia/Shanghai
        self.assertFalse(should_catch_up(now, None, "scheduled", 8, 0, "Asia/Shanghai"))

    def test_failed_cycle_retries_twice_then_waits_for_next_day(self) -> None:
        self.assertTrue(should_retry("failed", 1))
        self.assertTrue(should_retry("failed", 2))
        self.assertFalse(should_retry("failed", 3))
        self.assertFalse(should_retry("healthy", 1))
        self.assertFalse(should_retry("failed", 1, retryable=False))


if __name__ == "__main__":
    unittest.main()
