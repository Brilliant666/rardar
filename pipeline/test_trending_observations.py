from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pipeline.stable_read as stable_read_module
import pipeline.trending_observations as trending_observations_module
from pipeline.collect_github import candidate_queries
from pipeline.schema_validation import ArtifactKind, strict_json_loads, validate_payload
from pipeline.stable_read import StableReadError
from pipeline.trending_observations import (
    DEFAULT_LIMIT,
    SCHEDULE_TIMEZONE,
    TrendingObservationError,
    attach_bundle_digest,
    audit_observation_store,
    capture_id_for_scheduled_at,
    capture_path_for_scheduled_at,
    collect_capture_bundle,
    compute_bundle_digest,
    load_capture,
    nearest_scheduled_phase,
    observation_error_retryable,
    observer_instance_lock,
    parse_timestamp,
    parse_scheduled_at,
    run_observer,
    validate_capture_bundle,
    validate_observation,
    write_capture_create_only,
)


SCHEDULED = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
CAPTURED = SCHEDULED + timedelta(minutes=5)


def _metadata(
    repository_id: int,
    repository: str,
    *,
    stars: int = 100,
    forks: int = 10,
    issues: int = 2,
) -> dict[str, object]:
    return {
        "id": repository_id,
        "full_name": repository,
        "html_url": f"https://github.com/{repository}",
        "description": f"Description for {repository}",
        "stargazers_count": stars,
        "forks_count": forks,
        "open_issues_count": issues,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2026-08-23T23:00:00Z",
        "pushed_at": "2026-08-23T22:00:00Z",
        "default_branch": "main",
        "language": "Python",
        "topics": ["productivity", "ai"],
        "license": {"spdx_id": "MIT"},
        "archived": False,
        "disabled": False,
        "fork": False,
        "mirror_url": None,
    }


def _observation(
    repository_id: int,
    repository: str,
    captured_at: datetime,
    *,
    stars: int = 100,
    scheduled_at: datetime = SCHEDULED,
    query_index: int = 1,
) -> dict[str, object]:
    query = candidate_queries(scheduled_at)[query_index - 1]
    query_id = f"query-{query_index:02d}"
    return {
        "schemaVersion": 1,
        "githubRepositoryId": repository_id,
        "repository": repository,
        "htmlUrl": f"https://github.com/{repository}",
        "description": f"Description for {repository}",
        "capturedAt": captured_at.isoformat().replace("+00:00", "Z"),
        "totalStars": stars,
        "forks": 10,
        "openIssues": 2,
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2026-08-23T23:00:00Z",
        "pushedAt": "2026-08-23T22:00:00Z",
        "defaultBranch": "main",
        "primaryLanguage": "Python",
        "topics": ["ai", "productivity"],
        "licenseSpdxId": "MIT",
        "archived": False,
        "disabled": False,
        "fork": False,
        "mirrorUrl": None,
        "recalledBy": [
            {
                "source": "github_search",
                "sourceKey": query_id,
                "sourceRank": 1,
                "capturedAt": captured_at.isoformat().replace("+00:00", "Z"),
                "queryId": query_id,
                "query": query,
                "page": 1,
            }
        ],
    }


def _query_status(scheduled_at: datetime, *, failed: int = 0, incomplete: bool = False):
    statuses = []
    for index, query in enumerate(candidate_queries(scheduled_at), start=1):
        is_failed = index <= failed
        statuses.append(
            {
                "queryId": f"query-{index:02d}",
                "query": query,
                "state": "failed" if is_failed else "healthy",
                "resultCount": 0 if is_failed else (1 if index == failed + 1 else 0),
                "incompleteResults": incomplete if not is_failed and index == 1 else False,
                "errorCode": "fixture_failure" if is_failed else None,
                "errorMessage": "fixture query failed" if is_failed else None,
            }
        )
    return statuses


def _bundle(
    scheduled_at: datetime = SCHEDULED,
    captured_at: datetime = CAPTURED,
    *,
    observations: list[dict[str, object]] | None = None,
    failed_queries: int = 0,
    incomplete: bool = False,
    metadata_failures: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    observations = observations or [
        _observation(
            1,
            "owner/one",
            captured_at,
            scheduled_at=scheduled_at,
            query_index=failed_queries + 1,
        )
    ]
    metadata_failures = metadata_failures or []
    statuses = _query_status(scheduled_at, failed=failed_queries, incomplete=incomplete)
    delay = (captured_at - scheduled_at).total_seconds()
    degraded = failed_queries > 0 or incomplete or bool(metadata_failures)
    payload = {
        "schemaVersion": 1,
        "policyVersion": "trending-observation-v1",
        "captureId": capture_id_for_scheduled_at(scheduled_at),
        "scheduleTimezone": "Asia/Shanghai",
        "cadenceMinutes": 120,
        "scheduledAt": scheduled_at.isoformat().replace("+00:00", "Z"),
        "capturedAt": captured_at.isoformat().replace("+00:00", "Z"),
        "captureDelaySeconds": delay,
        "windowEligible": abs(delay) <= 600,
        "coverageState": "degraded" if degraded else "healthy",
        "successfulQueryCount": 9 - failed_queries,
        "failedQueryCount": failed_queries,
        "candidateCount": len(observations) + len(metadata_failures),
        "observationCount": len(observations),
        "metadataFailureCount": len(metadata_failures),
        "queryStatus": statuses,
        "metadataFailures": metadata_failures,
        "observations": observations,
        "retention": {
            "retentionClass": "raw_2h_observation",
            "retentionDays": 45,
            "retainUntil": (captured_at + timedelta(days=45))
            .isoformat()
            .replace("+00:00", "Z"),
        },
    }
    return validate_capture_bundle(attach_bundle_digest(payload))


def _bundle_bytes(bundle: dict[str, object]) -> bytes:
    return json.dumps(
        bundle,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8") + b"\n"


class FakeGitHubClient:
    def __init__(
        self,
        *,
        search_items: dict[int, list[dict[str, object]]] | None = None,
        search_failures: set[int] | None = None,
        incomplete: set[int] | None = None,
        metadata: dict[int, dict[str, object] | BaseException] | None = None,
        token_in_error: str | None = None,
    ) -> None:
        self.search_items = (
            {1: [{"id": 1, "full_name": "owner/one"}]}
            if search_items is None
            else search_items
        )
        self.search_failures = search_failures or set()
        self.incomplete = incomplete or set()
        self.metadata = {1: _metadata(1, "owner/one")} if metadata is None else metadata
        self.token_in_error = token_in_error
        self.search_calls: list[str] = []
        self.metadata_calls: list[int] = []

    def search_response(self, query: str, *, per_page: int, page: int):
        self.search_calls.append(query)
        index = len(self.search_calls)
        if index in self.search_failures:
            suffix = f" Bearer {self.token_in_error}" if self.token_in_error else ""
            raise RuntimeError(f"query failed{suffix}")
        items = copy.deepcopy(self.search_items.get(index, []))
        return {
            "items": items,
            "incomplete_results": index in self.incomplete,
            "total_count": len(items),
        }

    def repository(self, github_repository_id: int):
        self.metadata_calls.append(github_repository_id)
        value = self.metadata.get(github_repository_id)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise RuntimeError("metadata unavailable")
        return copy.deepcopy(value)


class NoCallClient:
    def search_response(self, *args, **kwargs):
        raise AssertionError("GitHub Search must not be called")

    def repository(self, *args, **kwargs):
        raise AssertionError("GitHub metadata must not be called")


class TrendingObservationContractTests(unittest.TestCase):
    def test_valid_contracts_and_digest(self) -> None:
        bundle = _bundle()
        self.assertTrue(
            validate_payload(ArtifactKind.TRENDING_OBSERVATION, bundle["observations"][0]).valid
        )
        self.assertTrue(
            validate_payload(ArtifactKind.TRENDING_CAPTURE_BUNDLE, bundle).valid
        )
        self.assertEqual(bundle["digest"]["value"], compute_bundle_digest(bundle))

    def test_observation_rejects_negative_stars_nonpositive_id_and_naive_times(self) -> None:
        base = _observation(1, "owner/one", CAPTURED)
        for field, value in (
            ("totalStars", -1),
            ("githubRepositoryId", 0),
            ("capturedAt", "2026-08-24T00:05:00"),
        ):
            candidate = copy.deepcopy(base)
            candidate[field] = value
            self.assertFalse(
                validate_payload(ArtifactKind.TRENDING_OBSERVATION, candidate).valid,
                field,
            )

    def test_observation_rejects_bad_repository_url_and_non_fact_fields(self) -> None:
        for mutation in (
            {"repository": "owner/../escape"},
            {"htmlUrl": "https://example.com/owner/one"},
            {"whyTrending": "AI says so"},
            {"observedStarDelta": 10},
        ):
            candidate = _observation(1, "owner/one", CAPTURED)
            candidate.update(mutation)
            with self.assertRaises(TrendingObservationError):
                validate_observation(candidate)

    def test_duplicate_json_keys_and_digest_tampering_fail(self) -> None:
        with self.assertRaises(ValueError):
            strict_json_loads('{"schemaVersion":1,"schemaVersion":1}')
        bundle = _bundle()
        bundle["observations"][0]["totalStars"] = 999
        with self.assertRaisesRegex(TrendingObservationError, "digest"):
            validate_capture_bundle(bundle)

    def test_non_finite_numbers_fail_validation_and_canonical_serializing(self) -> None:
        observation = _observation(1, "owner/one", CAPTURED)
        observation["totalStars"] = float("inf")
        self.assertFalse(
            validate_payload(ArtifactKind.TRENDING_OBSERVATION, observation).valid
        )
        bundle = _bundle()
        bundle["captureDelaySeconds"] = float("nan")
        self.assertFalse(
            validate_payload(ArtifactKind.TRENDING_CAPTURE_BUNDLE, bundle).valid
        )

    def test_schedule_is_timezone_aware_and_fixed_phase(self) -> None:
        with self.assertRaises(TrendingObservationError):
            parse_scheduled_at("2026-08-24T08:00:00")
        with self.assertRaises(TrendingObservationError):
            parse_scheduled_at("2026-08-24T09:00:00+08:00")
        self.assertEqual(
            nearest_scheduled_phase(datetime(2026, 8, 24, 1, 5, tzinfo=timezone.utc)),
            datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc),
        )


class TrendingRecallAndMetadataTests(unittest.TestCase):
    def test_reuses_all_nine_queries_and_merges_provenance(self) -> None:
        client = FakeGitHubClient(
            search_items={
                1: [{"id": 1, "full_name": "owner/one"}],
                2: [{"id": 1, "full_name": "owner/one"}],
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            bundle = collect_capture_bundle(
                data_dir=data,
                scheduled_at=SCHEDULED,
                client=client,
                token="fixture-token",
                clock=lambda: CAPTURED,
            )
        self.assertEqual(client.search_calls, candidate_queries(SCHEDULED))
        self.assertEqual(len(bundle["queryStatus"]), 9)
        sources = bundle["observations"][0]["recalledBy"]
        self.assertEqual([source["queryId"] for source in sources], ["query-01", "query-02"])

    def test_all_queries_failed_creates_no_bundle(self) -> None:
        client = FakeGitHubClient(search_failures=set(range(1, 10)))
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            with self.assertRaisesRegex(TrendingObservationError, "all nine") as raised:
                collect_capture_bundle(
                    data_dir=data,
                    scheduled_at=SCHEDULED,
                    client=client,
                    token="fixture-token",
                    clock=lambda: CAPTURED,
                )
            self.assertEqual(raised.exception.code, "all_candidate_queries_failed")
            self.assertFalse(observation_error_retryable(raised.exception))
            self.assertFalse((data / "observations").exists())

    def test_only_explicit_all_source_network_failures_are_retryable(self) -> None:
        retryable = TrendingObservationError(
            "all_candidate_queries_failed",
            "all failed",
            details={
                "errorCodes": ["github_network_error", "github_http_503"],
                "retryable": True,
            },
        )
        nonretryable = TrendingObservationError(
            "all_candidate_queries_failed",
            "all failed",
            details={
                "errorCodes": ["github_http_401"],
                "retryable": False,
            },
        )
        self.assertTrue(observation_error_retryable(retryable))
        self.assertFalse(observation_error_retryable(nonretryable))

    def test_partial_failure_and_incomplete_results_are_degraded(self) -> None:
        for client in (
            FakeGitHubClient(search_failures={9}),
            FakeGitHubClient(incomplete={1}),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                bundle = collect_capture_bundle(
                    data_dir=Path(temporary) / "data",
                    scheduled_at=SCHEDULED,
                    client=client,
                    token="fixture-token",
                    clock=lambda: CAPTURED,
                )
            self.assertEqual(bundle["coverageState"], "degraded")

    def test_repository_metadata_is_fact_authority(self) -> None:
        client = FakeGitHubClient(
            search_items={
                1: [
                    {
                        "id": 1,
                        "full_name": "owner/one",
                        "stargazers_count": 1,
                    }
                ]
            },
            metadata={1: _metadata(1, "owner/one", stars=4321)},
        )
        with tempfile.TemporaryDirectory() as temporary:
            bundle = collect_capture_bundle(
                data_dir=Path(temporary) / "data",
                scheduled_at=SCHEDULED,
                client=client,
                token="fixture-token",
                clock=lambda: CAPTURED,
            )
        self.assertEqual(bundle["observations"][0]["totalStars"], 4321)

    def test_partial_metadata_failure_is_recorded_and_redacted(self) -> None:
        token = "super-secret-token"
        client = FakeGitHubClient(
            search_items={
                1: [
                    {"id": 1, "full_name": "owner/one"},
                    {"id": 2, "full_name": "owner/two"},
                ]
            },
            metadata={
                1: _metadata(1, "owner/one"),
                2: RuntimeError(f"Bearer {token}"),
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            bundle = collect_capture_bundle(
                data_dir=Path(temporary) / "data",
                scheduled_at=SCHEDULED,
                client=client,
                token=token,
                clock=lambda: CAPTURED,
            )
        self.assertEqual(bundle["coverageState"], "degraded")
        self.assertEqual(bundle["metadataFailureCount"], 1)
        serialized = json.dumps(bundle)
        self.assertNotIn(token, serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_all_metadata_failure_creates_no_bundle(self) -> None:
        client = FakeGitHubClient(metadata={1: RuntimeError("unavailable")})
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(TrendingObservationError) as raised:
                collect_capture_bundle(
                    data_dir=Path(temporary) / "data",
                    scheduled_at=SCHEDULED,
                    client=client,
                    token="fixture-token",
                    clock=lambda: CAPTURED,
                )
            self.assertEqual(raised.exception.code, "all_repository_metadata_failed")

    def test_token_is_required_without_anonymous_fallback(self) -> None:
        for token in (None, "   "):
            with self.subTest(token=token), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(TrendingObservationError) as raised:
                    run_observer(
                        data_dir=Path(temporary) / "data",
                        scheduled_at=SCHEDULED,
                        timezone_name=SCHEDULE_TIMEZONE,
                        limit=500,
                        dry_run=False,
                        token=token,
                        client=NoCallClient(),
                    )
                self.assertEqual(raised.exception.code, "github_token_required")


class TrendingIdentityTests(unittest.TestCase):
    def test_same_id_with_two_names_in_one_capture_fails(self) -> None:
        cases = (
            {
                1: [
                    {"id": 1, "full_name": "owner/old"},
                    {"id": 1, "full_name": "owner/new"},
                ]
            },
            {
                1: [{"id": 1, "full_name": "owner/old"}],
                2: [{"id": 1, "full_name": "owner/new"}],
            },
        )
        for search_items in cases:
            with self.subTest(search_items=search_items), tempfile.TemporaryDirectory() as temporary:
                client = FakeGitHubClient(
                    search_items=search_items,
                    metadata={1: _metadata(1, "owner/new")},
                )
                with self.assertRaises(TrendingObservationError) as raised:
                    collect_capture_bundle(
                        data_dir=Path(temporary) / "data",
                        scheduled_at=SCHEDULED,
                        client=client,
                        token="fixture-token",
                        clock=lambda: CAPTURED,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "repository_identity_changed_during_capture",
                )

    def test_same_name_with_two_ids_in_one_capture_fails(self) -> None:
        client = FakeGitHubClient(
            search_items={
                1: [
                    {"id": 1, "full_name": "owner/name"},
                    {"id": 2, "full_name": "OWNER/NAME"},
                ]
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(TrendingObservationError) as raised:
                collect_capture_bundle(
                    data_dir=Path(temporary) / "data",
                    scheduled_at=SCHEDULED,
                    client=client,
                    token="fixture-token",
                    clock=lambda: CAPTURED,
                )
            self.assertEqual(raised.exception.code, "repository_name_identity_collision")

    def test_rename_or_transfer_across_captures_preserves_numeric_identity(self) -> None:
        prior_schedule = SCHEDULED - timedelta(hours=2)
        prior_capture = prior_schedule + timedelta(minutes=5)
        prior = _bundle(
            prior_schedule,
            prior_capture,
            observations=[
                _observation(
                    1,
                    "old-owner/name",
                    prior_capture,
                    scheduled_at=prior_schedule,
                )
            ],
        )
        client = FakeGitHubClient(
            search_items={},
            metadata={1: _metadata(1, "new-owner/name")},
        )
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            write_capture_create_only(data, prior)
            current = collect_capture_bundle(
                data_dir=data,
                scheduled_at=SCHEDULED,
                client=client,
                token="fixture-token",
                clock=lambda: CAPTURED,
            )
        self.assertEqual(current["observations"][0]["githubRepositoryId"], 1)
        self.assertEqual(current["observations"][0]["repository"], "new-owner/name")
        self.assertEqual(
            current["observations"][0]["recalledBy"][0]["source"],
            "recent_observation_carry_forward",
        )


class TrendingCarryForwardTests(unittest.TestCase):
    def test_26_hour_boundary_is_inclusive_and_older_capture_is_excluded(self) -> None:
        exact_schedule = SCHEDULED - timedelta(hours=26)
        exact_capture = exact_schedule + timedelta(minutes=5)
        older_schedule = SCHEDULED - timedelta(hours=28)
        older_capture = older_schedule + timedelta(minutes=5)
        exact = _bundle(
            exact_schedule,
            exact_capture,
            observations=[
                _observation(
                    1,
                    "owner/exact",
                    exact_capture,
                    scheduled_at=exact_schedule,
                )
            ],
        )
        older = _bundle(
            older_schedule,
            older_capture,
            observations=[
                _observation(
                    2,
                    "owner/older",
                    older_capture,
                    scheduled_at=older_schedule,
                )
            ],
        )
        client = FakeGitHubClient(
            search_items={},
            metadata={1: _metadata(1, "owner/exact"), 2: _metadata(2, "owner/older")},
        )
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            write_capture_create_only(data, older)
            write_capture_create_only(data, exact)
            current = collect_capture_bundle(
                data_dir=data,
                scheduled_at=SCHEDULED,
                client=client,
                token="fixture-token",
                clock=lambda: CAPTURED,
            )
        self.assertEqual([item["githubRepositoryId"] for item in current["observations"]], [1])
        self.assertEqual(client.metadata_calls, [1])

    def test_carry_forward_has_priority_over_new_search_candidates(self) -> None:
        prior_schedule = SCHEDULED - timedelta(hours=2)
        prior_capture = prior_schedule + timedelta(minutes=1)
        prior = _bundle(
            prior_schedule,
            prior_capture,
            observations=[
                _observation(
                    1,
                    "owner/carried-one",
                    prior_capture,
                    scheduled_at=prior_schedule,
                ),
                _observation(
                    2,
                    "owner/carried-two",
                    prior_capture,
                    scheduled_at=prior_schedule,
                ),
            ],
        )
        client = FakeGitHubClient(
            search_items={1: [{"id": 3, "full_name": "owner/new"}]},
            metadata={
                1: _metadata(1, "owner/carried-one"),
                2: _metadata(2, "owner/carried-two"),
                3: _metadata(3, "owner/new"),
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            write_capture_create_only(data, prior)
            current = collect_capture_bundle(
                data_dir=data,
                scheduled_at=SCHEDULED,
                client=client,
                token="fixture-token",
                limit=2,
                clock=lambda: CAPTURED,
            )
        self.assertEqual([item["githubRepositoryId"] for item in current["observations"]], [1, 2])
        self.assertEqual(client.metadata_calls, [1, 2])

    def test_more_than_500_carry_forward_candidates_fails_before_api(self) -> None:
        first_schedule = SCHEDULED - timedelta(hours=4)
        first_capture = first_schedule + timedelta(minutes=1)
        second_schedule = SCHEDULED - timedelta(hours=2)
        second_capture = second_schedule + timedelta(minutes=1)
        first = _bundle(
            first_schedule,
            first_capture,
            observations=[
                _observation(
                    index,
                    f"owner/repo-{index}",
                    first_capture,
                    scheduled_at=first_schedule,
                )
                for index in range(1, 501)
            ],
        )
        second = _bundle(
            second_schedule,
            second_capture,
            observations=[
                _observation(
                    501,
                    "owner/repo-501",
                    second_capture,
                    scheduled_at=second_schedule,
                )
            ],
        )
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            write_capture_create_only(data, first)
            write_capture_create_only(data, second)
            with self.assertRaises(TrendingObservationError) as raised:
                collect_capture_bundle(
                    data_dir=data,
                    scheduled_at=SCHEDULED,
                    client=NoCallClient(),
                    token="fixture-token",
                    limit=DEFAULT_LIMIT,
                    clock=lambda: CAPTURED,
                )
        self.assertEqual(raised.exception.code, "tracking_capacity_exceeded")


class TrendingAppendOnlyTests(unittest.TestCase):
    def test_idempotent_second_run_does_not_call_github(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            locks = Path(temporary) / "locks"
            first = run_observer(
                data_dir=data,
                scheduled_at=SCHEDULED,
                timezone_name=SCHEDULE_TIMEZONE,
                limit=500,
                dry_run=False,
                token="fixture-token",
                client=FakeGitHubClient(),
                clock=lambda: CAPTURED,
                lock_root=locks,
            )
            second = run_observer(
                data_dir=data,
                scheduled_at=SCHEDULED,
                timezone_name=SCHEDULE_TIMEZONE,
                limit=500,
                dry_run=False,
                token=None,
                client=NoCallClient(),
                clock=lambda: CAPTURED,
                lock_root=locks,
            )
        self.assertEqual(first["state"], "captured")
        self.assertEqual(second["state"], "already_captured")

    def test_corrupt_existing_target_fails_closed_without_api_or_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            target = capture_path_for_scheduled_at(data, SCHEDULED)
            target.parent.mkdir(parents=True)
            original = b'{"corrupt":true}\n'
            target.write_bytes(original)
            with self.assertRaises(TrendingObservationError) as raised:
                run_observer(
                    data_dir=data,
                    scheduled_at=SCHEDULED,
                    timezone_name=SCHEDULE_TIMEZONE,
                    limit=500,
                    dry_run=False,
                    token="fixture-token",
                    client=NoCallClient(),
                )
            self.assertEqual(raised.exception.code, "existing_capture_invalid")
            self.assertEqual(target.read_bytes(), original)

    def test_valid_bundle_with_different_slot_at_target_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            target = capture_path_for_scheduled_at(data, SCHEDULED)
            target.parent.mkdir(parents=True)
            other = _bundle(SCHEDULED - timedelta(hours=2), CAPTURED - timedelta(hours=2))
            original = json.dumps(other, sort_keys=True).encode()
            target.write_bytes(original)
            with self.assertRaises(TrendingObservationError) as raised:
                write_capture_create_only(data, _bundle())
            self.assertEqual(raised.exception.code, "existing_capture_invalid")
            self.assertEqual(target.read_bytes(), original)

    def test_existing_digest_mismatch_fails_closed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            target = capture_path_for_scheduled_at(data, SCHEDULED)
            target.parent.mkdir(parents=True)
            corrupt = copy.deepcopy(_bundle())
            corrupt["digest"]["value"] = "0" * 64
            original = _bundle_bytes(corrupt)
            target.write_bytes(original)

            with self.assertRaises(TrendingObservationError) as raised:
                write_capture_create_only(data, _bundle())

            self.assertEqual(raised.exception.code, "existing_capture_invalid")
            self.assertEqual(target.read_bytes(), original)

    def test_symlink_target_is_rejected_without_touching_link_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            target = capture_path_for_scheduled_at(data, SCHEDULED)
            target.parent.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_bytes(b"outside")
            try:
                target.symlink_to(outside)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            with self.assertRaises(TrendingObservationError):
                write_capture_create_only(data, _bundle())
            self.assertEqual(outside.read_bytes(), b"outside")

    def test_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unsafe = Path(temporary) / "data" / ".." / "escape"
            with self.assertRaises(TrendingObservationError) as raised:
                capture_path_for_scheduled_at(unsafe, SCHEDULED)
            self.assertEqual(raised.exception.code, "unsafe_observation_path")

    def test_interrupted_atomic_create_leaves_no_capture_or_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            target = capture_path_for_scheduled_at(data, SCHEDULED)
            with patch(
                "pipeline.trending_observations.os.link",
                side_effect=OSError("simulated interruption"),
            ):
                with self.assertRaises(TrendingObservationError) as raised:
                    write_capture_create_only(data, _bundle())
            self.assertEqual(raised.exception.code, "atomic_create_failed")
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_fsync_interruption_leaves_no_capture_or_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            target = capture_path_for_scheduled_at(data, SCHEDULED)
            with patch(
                "pipeline.trending_observations.os.fsync",
                side_effect=OSError("simulated fsync interruption"),
            ):
                with self.assertRaises(TrendingObservationError) as raised:
                    write_capture_create_only(data, _bundle())
            self.assertEqual(raised.exception.code, "capture_write_failed")
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_concurrent_create_has_one_winner_and_no_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            bundle = _bundle()
            barrier = threading.Barrier(2)

            def publish():
                barrier.wait()
                return write_capture_create_only(data, bundle)[0]

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: publish(), range(2)))
            self.assertEqual(sorted(outcomes), ["already_captured", "captured"])
            target = capture_path_for_scheduled_at(data, SCHEDULED)
            self.assertEqual(load_capture(target)["captureId"], bundle["captureId"])
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_concurrent_loser_settles_after_winner_unlinks_hardlink_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            bundle = _bundle()
            target = capture_path_for_scheduled_at(data, SCHEDULED)
            link_barrier = threading.Barrier(2)
            winner_linked = threading.Event()
            loser_first_snapshot = threading.Event()
            winner_unlinked = threading.Event()
            real_link = os.link
            real_unlink = Path.unlink
            real_snapshot = stable_read_module._read_regular_snapshot
            loser_target_snapshots = 0
            injected_change = False
            outcomes: dict[str, str] = {}
            errors: dict[str, BaseException] = {}

            def controlled_link(source, destination, *args, **kwargs):
                link_barrier.wait(timeout=5)
                if threading.current_thread().name == "capture-winner":
                    result = real_link(source, destination, *args, **kwargs)
                    winner_linked.set()
                    return result
                if not winner_linked.wait(5):
                    raise AssertionError("winner did not publish the hard link")
                return real_link(source, destination, *args, **kwargs)

            def controlled_unlink(path: Path, *args, **kwargs):
                if (
                    threading.current_thread().name == "capture-winner"
                    and path.name.endswith(".tmp")
                ):
                    if not loser_first_snapshot.wait(5):
                        raise AssertionError("loser did not start its target read")
                    result = real_unlink(path, *args, **kwargs)
                    winner_unlinked.set()
                    return result
                return real_unlink(path, *args, **kwargs)

            def controlled_snapshot(path: Path):
                nonlocal loser_target_snapshots, injected_change
                snapshot = real_snapshot(path)
                if (
                    threading.current_thread().name == "capture-loser"
                    and Path(path) == target
                ):
                    loser_target_snapshots += 1
                    if loser_target_snapshots == 1:
                        loser_first_snapshot.set()
                        if not winner_unlinked.wait(5):
                            raise AssertionError("winner did not unlink its hard-link source")
                    elif loser_target_snapshots == 2 and not injected_change:
                        injected_change = True
                        raise StableReadError(
                            "concurrent_change",
                            target,
                            "hard-link source unlink changed target inode metadata",
                            retryable=True,
                        )
                return snapshot

            def publish(label: str) -> None:
                try:
                    outcomes[label] = write_capture_create_only(data, bundle)[0]
                except BaseException as error:
                    errors[label] = error

            with (
                patch("pipeline.trending_observations.os.link", new=controlled_link),
                patch.object(Path, "unlink", new=controlled_unlink),
                patch.object(
                    stable_read_module,
                    "_read_regular_snapshot",
                    new=controlled_snapshot,
                ),
            ):
                threads = [
                    threading.Thread(
                        target=publish,
                        args=("winner",),
                        name="capture-winner",
                    ),
                    threading.Thread(
                        target=publish,
                        args=("loser",),
                        name="capture-loser",
                    ),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, {})
            self.assertEqual(outcomes, {"winner": "captured", "loser": "already_captured"})
            self.assertTrue(injected_change)
            self.assertEqual(load_capture(target)["captureId"], bundle["captureId"])
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_delete_and_recreate_during_settlement_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            bundle = _bundle()
            _, target = write_capture_create_only(data, bundle)
            original = target.read_bytes()
            original_inode = target.stat().st_ino
            replacement = target.with_name("replacement.json")
            replacement.write_bytes(original)
            replacement_inode = replacement.stat().st_ino
            self.assertNotEqual(original_inode, replacement_inode)
            real_stable_read = trending_observations_module.stable_read
            calls = 0

            def replace_during_read(path: Path, *args, **kwargs):
                nonlocal calls
                if Path(path) == target:
                    calls += 1
                    target.unlink()
                    replacement.rename(target)
                    raise StableReadError(
                        "concurrent_change",
                        target,
                        "target was deleted and recreated",
                        retryable=True,
                    )
                return real_stable_read(path, *args, **kwargs)

            with patch(
                "pipeline.trending_observations.stable_read",
                new=replace_during_read,
            ):
                with self.assertRaises(TrendingObservationError) as raised:
                    write_capture_create_only(data, bundle)

            self.assertEqual(raised.exception.code, "existing_capture_invalid")
            self.assertEqual(calls, 1)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(load_capture(target)["captureId"], bundle["captureId"])

    def test_same_length_in_place_rewrite_during_settlement_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            bundle = _bundle()
            _, target = write_capture_create_only(data, bundle)
            original = target.read_bytes()
            before = target.stat()
            changed = copy.deepcopy(bundle)
            changed["observations"][0]["totalStars"] += 1
            changed = validate_capture_bundle(attach_bundle_digest(changed))
            replacement = _bundle_bytes(changed)
            self.assertEqual(len(replacement), len(original))
            real_stable_read = trending_observations_module.stable_read
            calls = 0

            def mutate_during_read(path: Path, *args, **kwargs):
                nonlocal calls
                if Path(path) == target:
                    calls += 1
                    with target.open("r+b") as handle:
                        handle.write(replacement)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
                    raise StableReadError(
                        "concurrent_change",
                        target,
                        "same-length in-place mutation",
                        retryable=True,
                    )
                return real_stable_read(path, *args, **kwargs)

            with patch(
                "pipeline.trending_observations.stable_read",
                new=mutate_during_read,
            ):
                with self.assertRaises(TrendingObservationError) as raised:
                    write_capture_create_only(data, bundle)

            self.assertEqual(raised.exception.code, "existing_capture_invalid")
            self.assertEqual(calls, 1)
            self.assertEqual(target.stat().st_ino, before.st_ino)
            self.assertEqual(load_capture(target)["observations"][0]["totalStars"], 101)

    def test_persistent_hardlink_metadata_changes_exhaust_settlement_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            bundle = _bundle()
            _, target = write_capture_create_only(data, bundle)
            original = target.read_bytes()
            aliases = [target.with_name(f"settlement-{index}.link") for index in range(4)]
            for alias in aliases:
                os.link(target, alias)
            real_stable_read = trending_observations_module.stable_read
            calls = 0

            def remain_unstable(path: Path, *args, **kwargs):
                nonlocal calls
                if Path(path) == target:
                    aliases[calls].unlink()
                    calls += 1
                    raise StableReadError(
                        "concurrent_change",
                        target,
                        "hard-link metadata kept changing",
                        retryable=True,
                    )
                return real_stable_read(path, *args, **kwargs)

            with patch(
                "pipeline.trending_observations.stable_read",
                new=remain_unstable,
            ):
                with self.assertRaises(TrendingObservationError) as raised:
                    write_capture_create_only(data, bundle)

            self.assertEqual(raised.exception.code, "capture_create_settlement_failed")
            self.assertEqual(
                raised.exception.details,
                {"attempts": trending_observations_module._CREATE_SETTLEMENT_MAX_ATTEMPTS},
            )
            self.assertEqual(calls, 4)
            self.assertLess(
                sum(trending_observations_module._CREATE_SETTLEMENT_BACKOFF_SECONDS),
                0.25,
            )
            self.assertEqual(target.read_bytes(), original)
            self.assertTrue(all(not alias.exists() for alias in aliases))

    def test_target_disappearing_during_settlement_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            bundle = _bundle()
            _, target = write_capture_create_only(data, bundle)
            real_stable_read = trending_observations_module.stable_read
            calls = 0

            def disappear_during_read(path: Path, *args, **kwargs):
                nonlocal calls
                if Path(path) == target:
                    calls += 1
                    target.unlink()
                    raise StableReadError(
                        "concurrent_change",
                        target,
                        "target disappeared",
                        retryable=True,
                    )
                return real_stable_read(path, *args, **kwargs)

            with patch(
                "pipeline.trending_observations.stable_read",
                new=disappear_during_read,
            ):
                with self.assertRaises(TrendingObservationError) as raised:
                    write_capture_create_only(data, bundle)

            self.assertEqual(raised.exception.code, "existing_capture_invalid")
            self.assertEqual(calls, 1)
            self.assertFalse(target.exists())

    def test_non_concurrent_stable_read_failure_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            bundle = _bundle()
            _, target = write_capture_create_only(data, bundle)
            calls = 0

            def unavailable(path: Path, *args, **kwargs):
                nonlocal calls
                calls += 1
                raise StableReadError(
                    "unavailable",
                    Path(path),
                    "simulated permanent IO failure",
                    retryable=False,
                )

            with patch("pipeline.trending_observations.stable_read", new=unavailable):
                with self.assertRaises(TrendingObservationError) as raised:
                    write_capture_create_only(data, bundle)

            self.assertEqual(raised.exception.code, "existing_capture_invalid")
            self.assertEqual(calls, 1)
            self.assertEqual(target.read_bytes(), _bundle_bytes(bundle))

    def test_create_only_500_round_stress_has_exact_outcomes(self) -> None:
        exceptions: list[BaseException] = []
        outcomes: list[list[str]] = []
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            with ThreadPoolExecutor(max_workers=2) as pool:
                for index in range(500):
                    scheduled = SCHEDULED + timedelta(hours=2 * index)
                    captured = CAPTURED + timedelta(hours=2 * index)
                    bundle = _bundle(scheduled, captured)
                    barrier = threading.Barrier(2)

                    def publish() -> str:
                        barrier.wait(timeout=5)
                        return write_capture_create_only(data, bundle)[0]

                    futures = [pool.submit(publish) for _ in range(2)]
                    round_outcomes: list[str] = []
                    for future in futures:
                        try:
                            round_outcomes.append(future.result(timeout=10))
                        except BaseException as error:
                            exceptions.append(error)
                    outcomes.append(sorted(round_outcomes))
                    target = capture_path_for_scheduled_at(data, scheduled)
                    self.assertEqual(load_capture(target)["captureId"], bundle["captureId"])

            self.assertEqual(exceptions, [])
            self.assertEqual(
                outcomes,
                [["already_captured", "captured"] for _ in range(500)],
            )
            self.assertEqual(list(data.rglob("*.tmp")), [])
            self.assertEqual(
                len(list(data.rglob("trending-v1-*.json"))),
                500,
            )

    def test_new_slot_never_changes_prior_capture_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            first = _bundle()
            _, first_path = write_capture_create_only(data, first)
            original = first_path.read_bytes()
            later_schedule = SCHEDULED + timedelta(hours=2)
            later_capture = CAPTURED + timedelta(hours=2)
            second = _bundle(later_schedule, later_capture)
            write_capture_create_only(data, second)
            self.assertEqual(first_path.read_bytes(), original)


class TrendingObserverLockTests(unittest.TestCase):
    def test_overlap_skips_without_api_and_lock_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            locks = Path(temporary) / "locks"
            with observer_instance_lock(data, lock_root=locks, clock=lambda: CAPTURED):
                result = run_observer(
                    data_dir=data,
                    scheduled_at=SCHEDULED,
                    timezone_name=SCHEDULE_TIMEZONE,
                    limit=500,
                    dry_run=False,
                    token="fixture-token",
                    client=NoCallClient(),
                    clock=lambda: CAPTURED,
                    lock_root=locks,
                )
            self.assertEqual(result["state"], "skipped_overlap")
            self.assertFalse(result["captured"])
            with observer_instance_lock(data, lock_root=locks, clock=lambda: CAPTURED):
                pass


class TrendingAuditTests(unittest.TestCase):
    def test_healthy_and_degraded_store_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            _, first_path = write_capture_create_only(data, _bundle())
            original = first_path.read_bytes()
            healthy = audit_observation_store(data)
            self.assertEqual(healthy["status"], "healthy")
            self.assertEqual(healthy["captureCount"], 1)
            self.assertEqual(first_path.read_bytes(), original)
            later = _bundle(
                SCHEDULED + timedelta(hours=2),
                CAPTURED + timedelta(hours=2),
                failed_queries=1,
            )
            write_capture_create_only(data, later)
            degraded = audit_observation_store(data)
            self.assertEqual(degraded["status"], "degraded")
            self.assertEqual(degraded["degradedCaptureCount"], 1)

    def _tamper(self, mutation) -> dict[str, object]:
        bundle = _bundle()
        mutation(bundle)
        return attach_bundle_digest(bundle)

    def test_audit_detects_count_and_retention_mismatches(self) -> None:
        mutations = [
            lambda bundle: bundle.update({"observationCount": 2}),
            lambda bundle: bundle["retention"].update(
                {"retainUntil": (CAPTURED + timedelta(days=44)).isoformat()}
            ),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                data = Path(temporary) / "data"
                target = capture_path_for_scheduled_at(data, SCHEDULED)
                target.parent.mkdir(parents=True)
                target.write_text(json.dumps(self._tamper(mutation)), encoding="utf-8")
                report = audit_observation_store(data)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["issueCount"], 1)

    def test_historical_90_day_capture_remains_valid(self) -> None:
        bundle = _bundle()
        historical = json.loads(json.dumps(bundle))
        captured = parse_timestamp(historical["capturedAt"], field="capturedAt")
        historical["retention"] = {
            "retentionClass": "raw_2h_observation",
            "retentionDays": 90,
            "retainUntil": (captured + timedelta(days=90)).isoformat().replace(
                "+00:00", "Z"
            ),
        }
        self.assertEqual(
            validate_capture_bundle(attach_bundle_digest(historical))["retention"][
                "retentionDays"
            ],
            90,
        )

    def test_audit_detects_digest_corruption_and_duplicate_keys(self) -> None:
        for content in (
            b'{"schemaVersion":1,"schemaVersion":1}',
            json.dumps({**_bundle(), "digest": {"algorithm": "sha256", "value": "0" * 64}}).encode(),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                data = Path(temporary) / "data"
                target = capture_path_for_scheduled_at(data, SCHEDULED)
                target.parent.mkdir(parents=True)
                target.write_bytes(content)
                report = audit_observation_store(data)
                self.assertEqual(report["status"], "failed")

    def test_audit_detects_residual_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            target = capture_path_for_scheduled_at(data, SCHEDULED)
            target.parent.mkdir(parents=True)
            (target.parent / ".capture.json.deadbeef.tmp").write_text("partial")
            report = audit_observation_store(data)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["issues"][0]["code"], "temporary_file_residual")

    def test_audit_cross_checks_carry_forward_provenance(self) -> None:
        prior_schedule = SCHEDULED - timedelta(hours=2)
        prior_capture = prior_schedule + timedelta(minutes=5)
        prior = _bundle(
            prior_schedule,
            prior_capture,
            observations=[
                _observation(
                    1,
                    "owner/one",
                    prior_capture,
                    scheduled_at=prior_schedule,
                )
            ],
        )
        current_observation = _observation(1, "owner/one", CAPTURED)
        current_observation["recalledBy"] = [
            {
                "source": "recent_observation_carry_forward",
                "sourceKey": prior["captureId"],
                "sourceRank": 2,
                "capturedAt": prior["capturedAt"],
            }
        ]
        current = _bundle(observations=[current_observation])
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            write_capture_create_only(data, prior)
            write_capture_create_only(data, current)
            report = audit_observation_store(data)
            self.assertEqual(report["status"], "failed")
            self.assertIn(
                "carry_forward_reference_invalid",
                {issue["code"] for issue in report["issues"]},
            )

    def test_audit_rejects_a_capture_that_omits_recent_repository_ids(self) -> None:
        prior_schedule = SCHEDULED - timedelta(hours=2)
        prior_capture = prior_schedule + timedelta(minutes=5)
        prior = _bundle(
            prior_schedule,
            prior_capture,
            observations=[
                _observation(
                    1,
                    "owner/prior",
                    prior_capture,
                    scheduled_at=prior_schedule,
                )
            ],
        )
        current = _bundle(
            observations=[_observation(2, "owner/current", CAPTURED)]
        )
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            write_capture_create_only(data, prior)
            write_capture_create_only(data, current)
            report = audit_observation_store(data)
            self.assertEqual(report["status"], "failed")
            self.assertIn(
                "carry_forward_candidate_missing",
                {issue["code"] for issue in report["issues"]},
            )

    def test_audit_rejects_symlink_or_reparse_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            captures = data / "observations" / "trending" / "v1" / "captures"
            captures.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            try:
                (captures / "2026").symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            report = audit_observation_store(data)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["issues"][0]["code"], "unsafe_observation_path")

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior is Windows-specific")
    def test_audit_rejects_windows_junction_without_following_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            captures = data / "observations" / "trending" / "v1" / "captures"
            captures.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            marker = outside / "marker.txt"
            marker.write_text("outside", encoding="utf-8")
            junction = captures / "2026"
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0 or not os.path.lexists(junction):
                self.skipTest("Windows directory junction creation is unavailable")
            try:
                report = audit_observation_store(data)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["issues"][0]["code"], "unsafe_observation_path")
                self.assertEqual(marker.read_text(encoding="utf-8"), "outside")
            finally:
                if os.path.lexists(junction):
                    junction.rmdir()


class TrendingIsolationTests(unittest.TestCase):
    def test_observer_only_adds_observation_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            generation = data / "generations" / "retained" / "manifest.json"
            generation.parent.mkdir(parents=True)
            generation.write_bytes(b"retained-generation")
            current = data / "current.json"
            current.write_bytes(b"current-pointer")
            d1 = root / "state" / "rardar.sqlite"
            d1.parent.mkdir()
            d1.write_bytes(b"sqlite-fixture")
            before = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (generation, current, d1)
            }
            result = run_observer(
                data_dir=data,
                scheduled_at=SCHEDULED,
                timezone_name=SCHEDULE_TIMEZONE,
                limit=500,
                dry_run=False,
                token="fixture-token",
                client=FakeGitHubClient(),
                clock=lambda: CAPTURED,
                lock_root=root / "locks",
            )
            after = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (generation, current, d1)
            }
            self.assertEqual(result["state"], "captured")
            self.assertEqual(before, after)

    def test_dry_run_does_not_create_data_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            result = run_observer(
                data_dir=data,
                scheduled_at=SCHEDULED,
                timezone_name=SCHEDULE_TIMEZONE,
                limit=500,
                dry_run=True,
                token="fixture-token",
                client=FakeGitHubClient(),
                clock=lambda: CAPTURED,
                lock_root=root / "locks",
            )
            self.assertEqual(result["state"], "dry_run")
            self.assertFalse(data.exists())


if __name__ == "__main__":
    unittest.main()
