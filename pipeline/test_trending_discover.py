from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.collect_github import candidate_queries
from pipeline.derive_trending_explosion import derive_trending_explosion
from pipeline.schema_validation import ArtifactKind, validate_payload
from pipeline.test_trending_explosion import _observation_at, _seed_generation, _write_capture
from pipeline.trending_explosion import CaptureSource
from pipeline.trending_discover import (
    LEGACY_POLICY_VERSION,
    POLICY_VERSION,
    TODAY_PUBLISHED_TOP_COUNT,
    V2_POLICY_VERSION,
    DiscoverSources,
    TrendingDiscoverError,
    _attach_payload_digest,
    audit_discover_store,
    build_discover_artifact,
    derive_trending_discover,
    load_discover_sources,
    resolve_current_discover,
    rollback_discover,
    validate_discover_artifact,
)


WINDOW_END = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)
GENERATED_AT = WINDOW_END + timedelta(minutes=20)


def _phase_observations(offset: int) -> list[dict[str, object]]:
    scheduled = WINDOW_END + timedelta(hours=offset)
    result = [_observation_at(1, "today/exact", scheduled, stars=1000 + offset + 26)]
    if offset >= -20:
        result.append(_observation_at(2, "discover/near", scheduled, stars=200 + offset + 20))
    if offset >= -10:
        result.extend(
            [
                _observation_at(3, "discover/rising", scheduled, stars=300 + (offset + 10) * 2),
                _observation_at(5, "discover/flat", scheduled, stars=500),
                _observation_at(6, "discover/falling", scheduled, stars=600 - (offset + 10)),
                _observation_at(
                    7,
                    "discover/disabled",
                    scheduled,
                    stars=700 + offset + 10,
                    disabled=offset == 0,
                ),
                _observation_at(
                    8,
                    "old-owner/renamed" if offset < -4 else "new-owner/renamed",
                    scheduled,
                    stars=800 + offset + 10,
                ),
                _observation_at(
                    10,
                    "collision/shared" if offset < -2 else "collision/original",
                    scheduled,
                    stars=100,
                ),
            ]
        )
    if offset >= -2:
        result.extend(
            [
                _observation_at(4, "discover/just", scheduled, stars=400 + offset + 2),
                _observation_at(9, "collision/shared", scheduled, stars=900 + offset + 2),
                _observation_at(
                    11,
                    "discover/forked-archive",
                    scheduled,
                    stars=1100 + offset + 2,
                    archived=True,
                    fork=True,
                ),
            ]
        )
    return result


def _seed_sources(data_dir: Path, *, degraded_latest: bool = False) -> None:
    for offset in range(-26, 1, 2):
        scheduled = WINDOW_END + timedelta(hours=offset)
        observations = _phase_observations(offset)
        if degraded_latest and offset == 0:
            query = candidate_queries(scheduled)[1]
            for item in observations:
                provenance = item["recalledBy"][0]
                provenance.update(
                    {"sourceKey": "query-02", "queryId": "query-02", "query": query}
                )
        _write_capture(
            data_dir,
            scheduled,
            observations,
            failed_queries=1 if degraded_latest and offset == 0 else 0,
            metadata_failures=(
                [
                    {
                        "githubRepositoryId": 999999,
                        "repository": "bad/source",
                        "errorCode": "http_500",
                        "errorMessage": "bounded",
                    }
                ]
                if degraded_latest and offset == 0
                else None
            ),
        )


def _seed_today(data_dir: Path) -> str:
    _seed_generation(data_dir)
    result = derive_trending_explosion(
        data_dir,
        WINDOW_END,
        generated_at=WINDOW_END + timedelta(minutes=10),
    )
    return str(result["generationId"])


def _with_series(
    sources: DiscoverSources,
    definitions: list[tuple[int, str, list[int | None]]],
) -> DiscoverSources:
    captures: list[CaptureSource] = []
    for index, source in enumerate(sources.captures):
        payload = copy.deepcopy(source.payload)
        scheduled = datetime.fromisoformat(
            str(payload["scheduledAt"]).replace("Z", "+00:00")
        )
        for repository_id, repository, values in definitions:
            stars = values[index]
            if stars is not None:
                payload["observations"].append(
                    _observation_at(repository_id, repository, scheduled, stars=stars)
                )
        payload["observations"].sort(key=lambda item: int(item["githubRepositoryId"]))
        payload["candidateCount"] = len(payload["observations"])
        payload["observationCount"] = len(payload["observations"])
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        captures.append(
            CaptureSource(
                path=source.path,
                original_observation_path=source.original_observation_path,
                content=content,
                file_sha256=hashlib.sha256(content).hexdigest(),
                payload=payload,
            )
        )
    return DiscoverSources(tuple(captures), sources.today)


def _with_today_ranks(
    sources: DiscoverSources,
    ranked_projects: dict[int, tuple[int, str, int]],
    *,
    exact_count: int,
) -> DiscoverSources:
    template = copy.deepcopy(sources.today.payload["exactRanked"][0])
    ranked: list[dict[str, object]] = []
    used_ids = {repository_id for repository_id, _, _ in ranked_projects.values()}
    for rank in range(1, exact_count + 1):
        repository_id, repository, delta = ranked_projects.get(
            rank,
            (900_000 + rank, f"fixture/repository-{rank}", exact_count - rank),
        )
        while repository_id in used_ids and rank not in ranked_projects:
            repository_id += exact_count
        item = copy.deepcopy(template)
        item.update(
            {
                "rank": rank,
                "githubRepositoryId": repository_id,
                "repository": repository,
                "htmlUrl": f"https://github.com/{repository}",
                "observedStarDelta": max(0, delta),
                "totalStars": 10_000 + max(0, delta),
                "baselineStars": 10_000,
            }
        )
        ranked.append(item)
    payload = copy.deepcopy(sources.today.payload)
    payload["exactRanked"] = ranked
    return DiscoverSources(
        sources.captures,
        replace(sources.today, payload=payload),
    )


class TrendingDiscoverContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        _seed_sources(self.data_dir)
        self.today_generation = _seed_today(self.data_dir)
        self.sources = load_discover_sources(self.data_dir)
        self.artifact = build_discover_artifact(
            generation_id="discover-fixture",
            generated_at=GENERATED_AT,
            sources=self.sources,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_stage_model_today_exclusion_and_actual_windows(self) -> None:
        self.assertEqual(self.artifact["schemaVersion"], 3)
        self.assertEqual(self.artifact["policyVersion"], POLICY_VERSION)
        stages = self.artifact["stages"]
        self.assertEqual([item["githubRepositoryId"] for item in stages["nearValidation"]], [2])
        self.assertEqual(
            {item["githubRepositoryId"] for item in stages["justDiscovered"]}, {4, 11}
        )
        self.assertEqual(
            {item["githubRepositoryId"] for item in stages["rising"]}, {3, 8}
        )
        all_ids = {
            item["githubRepositoryId"]
            for items in stages.values()
            for item in items
        }
        self.assertNotIn(1, all_ids)
        self.assertEqual(self.artifact["todayPublishedTopCount"], TODAY_PUBLISHED_TOP_COUNT)
        self.assertEqual(self.artifact["excludedPublishedCount"], 1)
        self.assertEqual(self.artifact["todayExactCount"], 1)
        self.assertEqual(self.artifact["stages"]["outsideTodayMomentum"], [])
        self.assertTrue(all(item["observedWindowHours"] <= 26.5 for items in stages.values() for item in items))
        self.assertNotIn("expected24h", str(self.artifact))
        near = stages["nearValidation"][0]
        self.assertGreaterEqual(near["consecutivePositiveIntervalCount"], 2)
        self.assertIn("awaiting_today_settlement", near["publishReasonCodes"])
        self.assertEqual(near["publishReasonCodes"], near["signalFacts"])

    def test_signal_quality_gates_preserve_recent_and_small_relative_growth(self) -> None:
        length = len(self.sources.captures)
        sources = _with_series(
            self.sources,
            [
                (101, "d3/d3", [113_576, 113_578, 113_580] + [113_580] * (length - 3)),
                (102, "omnivore-app/omnivore", [16_223, 16_224, 16_225] + [16_225] * (length - 3)),
                (103, "kserve/kserve", [5_840, 5_841] + [5_841] * (length - 2)),
                (104, "small/relative", [None] * (length - 6) + [100, 101, 102, 102, 102, 102]),
                (105, "growth/intermittent", [1_000, 1_010] * 7),
                (106, "growth/final-only", [2_000] * (length - 1) + [2_020]),
                (107, "new/zero-growth", [None] * (length - 1) + [50]),
                (108, "new/disappeared", [None] * (length - 2) + [50, None]),
            ],
        )
        artifact = build_discover_artifact(
            generation_id="policy-fixture",
            generated_at=GENERATED_AT,
            sources=sources,
        )
        published = {
            item["repository"]: item
            for values in artifact["stages"].values()
            for item in values
        }
        for repository in ("d3/d3", "omnivore-app/omnivore", "kserve/kserve"):
            self.assertNotIn(repository, published)
        self.assertNotIn("growth/intermittent", published)
        self.assertNotIn("growth/final-only", published)
        self.assertNotIn("new/disappeared", published)
        relative = published["small/relative"]
        self.assertEqual(relative["stage"], "rising")
        self.assertIn("relative_growth_gate", relative["publishReasonCodes"])
        self.assertNotIn("absolute_growth_gate", relative["publishReasonCodes"])
        recent = published["new/zero-growth"]
        self.assertEqual(recent["stage"], "just_discovered")
        self.assertEqual(recent["publishReasonCodes"], ["first_seen_recently"])
        suppression = artifact["suppressionSummary"]
        self.assertGreaterEqual(suppression["suppressedSignalCount"], 5)
        self.assertGreaterEqual(suppression["reasons"]["weak_pre_exact_growth"], 5)

    def test_today_top20_boundary_and_outside_momentum_are_separate(self) -> None:
        length = len(self.sources.captures)
        accelerating = [100] * (length - 5) + [100, 101, 102, 108, 114]
        sources = _with_series(
            self.sources,
            [
                (101, "outside/accelerating", accelerating),
                (102, "outside/flat", [500] * length),
                (103, "today/rank-20", accelerating),
            ],
        )
        sources = _with_today_ranks(
            sources,
            {
                1: (1, "today/exact", 1_000),
                20: (103, "today/rank-20", 100),
                21: (101, "outside/accelerating", 9),
                485: (102, "outside/flat", 0),
            },
            exact_count=485,
        )
        artifact = build_discover_artifact(
            generation_id="top20-boundary-fixture",
            generated_at=GENERATED_AT,
            sources=sources,
        )

        outside = artifact["stages"]["outsideTodayMomentum"]
        self.assertEqual([item["githubRepositoryId"] for item in outside], [101])
        item = outside[0]
        self.assertEqual(item["eligibilityClass"], "exact_outside_published")
        self.assertEqual(item["todayExactRank"], 21)
        self.assertEqual(item["recentObservedStarDelta"], 12)
        self.assertEqual(item["priorComparableWindowDelta"], 2)
        self.assertEqual(item["accelerationDelta"], 10)
        self.assertIn("recent_acceleration", item["publishReasonCodes"])
        self.assertEqual(artifact["todayExactCount"], 485)
        self.assertEqual(artifact["todayPublishedCount"], 20)
        self.assertEqual(artifact["excludedPublishedCount"], 2)
        self.assertEqual(artifact["exactOutsidePublishedEvaluatedCount"], 2)
        self.assertEqual(
            artifact["suppressionSummary"]["reasons"][
                "already_exact_without_momentum"
            ],
            1,
        )
        all_ids = {
            entry["githubRepositoryId"]
            for values in artifact["stages"].values()
            for entry in values
        }
        self.assertNotIn(103, all_ids)
        self.assertNotIn(102, all_ids)

    def test_published_top_count_tamper_fails_closed(self) -> None:
        invalid = copy.deepcopy(self.artifact)
        invalid["todayPublishedTopCount"] = 21
        invalid = _attach_payload_digest(invalid)
        with self.assertRaises(TrendingDiscoverError) as context:
            validate_discover_artifact(invalid)
        self.assertEqual(context.exception.code, "discover_schema_invalid")

    def test_retained_v1_and_v2_remain_auditable(self) -> None:
        legacy = build_discover_artifact(
            generation_id="legacy-policy-fixture",
            generated_at=GENERATED_AT,
            sources=self.sources,
            policy_version=LEGACY_POLICY_VERSION,
        )
        self.assertEqual(legacy["schemaVersion"], 1)
        self.assertEqual(legacy["policyVersion"], LEGACY_POLICY_VERSION)
        self.assertNotIn("signalPolicy", legacy)
        self.assertNotIn("suppressionSummary", legacy)
        self.assertGreaterEqual(
            sum(len(values) for values in legacy["stages"].values()),
            sum(len(values) for values in self.artifact["stages"].values()),
        )
        v2 = build_discover_artifact(
            generation_id="v2-policy-fixture",
            generated_at=GENERATED_AT,
            sources=self.sources,
            policy_version=V2_POLICY_VERSION,
        )
        self.assertEqual(v2["schemaVersion"], 2)
        self.assertEqual(v2["policyVersion"], V2_POLICY_VERSION)
        self.assertNotIn("outsideTodayMomentum", v2["stages"])
        self.assertEqual(v2["coverage"]["excludedExactCount"], 1)

        for retained in (legacy, v2):
            with self.subTest(policy=retained["policyVersion"]):
                invalid = copy.deepcopy(retained)
                invalid["todayPublishedTopCount"] = TODAY_PUBLISHED_TOP_COUNT
                invalid = _attach_payload_digest(invalid)
                with self.assertRaises(TrendingDiscoverError) as context:
                    validate_discover_artifact(invalid)
                self.assertEqual(
                    context.exception.code, "discover_policy_contract_mismatch"
                )

                invalid = copy.deepcopy(retained)
                target = next(
                    item for items in invalid["stages"].values() for item in items
                )
                target["eligibilityClass"] = "pre_exact"
                invalid = _attach_payload_digest(invalid)
                with self.assertRaises(TrendingDiscoverError) as context:
                    validate_discover_artifact(invalid)
                self.assertEqual(
                    context.exception.code, "discover_policy_contract_mismatch"
                )

    def test_rename_fork_archive_negative_disabled_and_identity_conflict(self) -> None:
        items = [item for values in self.artifact["stages"].values() for item in values]
        renamed = next(item for item in items if item["githubRepositoryId"] == 8)
        self.assertEqual(renamed["repository"], "new-owner/renamed")
        forked = next(item for item in items if item["githubRepositoryId"] == 11)
        self.assertTrue(forked["isFork"])
        self.assertTrue(forked["isArchived"])
        reasons = {item["githubRepositoryId"]: item["reason"] for item in self.artifact["conflicts"]}
        self.assertEqual(reasons[6], "star_count_decreased")
        self.assertEqual(reasons[7], "current_disabled")
        self.assertEqual(reasons[9], "source_identity_conflict")

    def test_stage_priority_sorting_and_digest_are_enforced(self) -> None:
        near = self.artifact["stages"]["nearValidation"][0]
        self.assertEqual(near["stage"], "near_validation")
        invalid = copy.deepcopy(self.artifact)
        invalid["stages"]["nearValidation"][0]["stage"] = "rising"
        invalid = _attach_payload_digest(invalid)
        with self.assertRaises(TrendingDiscoverError) as context:
            validate_discover_artifact(invalid)
        self.assertEqual(context.exception.code, "discover_stage_mismatch")

        invalid = copy.deepcopy(self.artifact)
        invalid["stages"]["justDiscovered"].reverse()
        invalid = _attach_payload_digest(invalid)
        with self.assertRaises(TrendingDiscoverError) as context:
            validate_discover_artifact(invalid)
        self.assertEqual(context.exception.code, "discover_sort_order_invalid")

        invalid = copy.deepcopy(self.artifact)
        duplicate = copy.deepcopy(invalid["stages"]["nearValidation"][0])
        duplicate["stage"] = "rising"
        invalid["stages"]["rising"].append(duplicate)
        invalid["stages"]["rising"].sort(
            key=lambda item: (
                -int(item["observedStarDelta"]),
                -int(item["totalStars"]),
                str(item["repository"]),
            )
        )
        invalid = _attach_payload_digest(invalid)
        with self.assertRaises(TrendingDiscoverError) as context:
            validate_discover_artifact(invalid)
        self.assertEqual(context.exception.code, "discover_duplicate_repository_id")

        invalid = copy.deepcopy(self.artifact)
        invalid["payloadDigest"]["value"] = "0" * 64
        with self.assertRaises(TrendingDiscoverError) as context:
            validate_discover_artifact(invalid)
        self.assertEqual(context.exception.code, "discover_payload_digest_mismatch")

        invalid = copy.deepcopy(self.artifact)
        invalid["stages"]["nearValidation"][0]["publishReasonCodes"] = [
            "continuous_positive_growth",
            "absolute_growth_gate",
        ]
        invalid["stages"]["nearValidation"][0]["signalFacts"] = list(
            invalid["stages"]["nearValidation"][0]["publishReasonCodes"]
        )
        invalid = _attach_payload_digest(invalid)
        with self.assertRaises(TrendingDiscoverError) as context:
            validate_discover_artifact(invalid)
        self.assertEqual(context.exception.code, "discover_publish_reason_mismatch")

    def test_schema_rejects_ai_and_extrapolation_fields(self) -> None:
        for field in ("aiScore", "expected24hDelta"):
            invalid = copy.deepcopy(self.artifact)
            invalid[field] = 1
            self.assertFalse(validate_payload(ArtifactKind.TRENDING_DISCOVER, invalid).valid)
        invalid = copy.deepcopy(self.artifact)
        del invalid["suppressionSummary"]
        self.assertFalse(validate_payload(ArtifactKind.TRENDING_DISCOVER, invalid).valid)
        invalid = copy.deepcopy(self.artifact)
        target = next(items[0] for items in invalid["stages"].values() if items)
        del target["publishReasonCodes"]
        self.assertFalse(validate_payload(ArtifactKind.TRENDING_DISCOVER, invalid).valid)

    def test_degraded_query_and_metadata_coverage_is_published_as_degraded(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        try:
            data_dir = Path(temporary.name) / "data"
            _seed_sources(data_dir, degraded_latest=True)
            _seed_today(data_dir)
            artifact = build_discover_artifact(
                generation_id="degraded-fixture",
                generated_at=GENERATED_AT,
                sources=load_discover_sources(data_dir),
            )
            self.assertEqual(artifact["coverage"]["state"], "degraded")
            self.assertEqual(artifact["coverage"]["queryFailureCount"], 1)
            self.assertEqual(artifact["coverage"]["metadataFailureCount"], 1)
            self.assertEqual(
                artifact["suppressionSummary"]["reasons"]["metadata_incomplete"],
                1,
            )
        finally:
            temporary.cleanup()

    def test_today_tamper_fails_closed(self) -> None:
        generation = self.data_dir / "generations" / self.today_generation
        target = generation / "trending" / "explosion.json"
        target.write_bytes(target.read_bytes().replace(b'"exactRanked":', b'"exactRankedX":', 1))
        with self.assertRaises(TrendingDiscoverError) as context:
            load_discover_sources(self.data_dir)
        self.assertIn(context.exception.code, {"discover_today_generation_invalid", "discover_today_explosion_invalid"})


class TrendingDiscoverPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        _seed_sources(self.data_dir)
        _seed_today(self.data_dir)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_publish_audit_already_derived_and_rollback(self) -> None:
        first = derive_trending_discover(self.data_dir, generated_at=GENERATED_AT)
        self.assertEqual(first["state"], "published")
        current = resolve_current_discover(self.data_dir)
        self.assertEqual(current.generation_id, first["generationId"])
        self.assertEqual(list((current.root / "sources").glob("capture-*.json")), [])
        self.assertTrue(
            all(
                "generationRelativePath" not in reference
                for reference in current.artifact["sourceInventory"]
            )
        )
        self.assertIn(audit_discover_store(self.data_dir)["status"], {"healthy", "degraded"})
        second = derive_trending_discover(
            self.data_dir, generated_at=GENERATED_AT + timedelta(minutes=1)
        )
        self.assertEqual(second["state"], "already_derived")

        scheduled = WINDOW_END + timedelta(hours=2)
        observations = [
            _observation_at(
                int(item["githubRepositoryId"]),
                str(item["repository"]),
                scheduled,
                stars=int(item["totalStars"]) + 1,
                archived=bool(item["archived"]),
                fork=bool(item["fork"]),
                disabled=bool(item["disabled"]),
            )
            for item in load_discover_sources(self.data_dir).latest.payload["observations"]
        ]
        _write_capture(self.data_dir, scheduled, observations)
        newer = derive_trending_discover(
            self.data_dir, generated_at=scheduled + timedelta(minutes=20)
        )
        self.assertNotEqual(newer["generationId"], first["generationId"])
        rolled = rollback_discover(self.data_dir, first["generationId"])
        self.assertEqual(rolled["state"], "rolled_back")
        self.assertEqual(resolve_current_discover(self.data_dir).generation_id, first["generationId"])

    def test_published_set_digest_tamper_reaches_recomputation_gate(self) -> None:
        derive_trending_discover(self.data_dir, generated_at=GENERATED_AT)
        current = resolve_current_discover(self.data_dir)
        artifact_path = current.root / "discover.json"
        artifact = copy.deepcopy(current.artifact)
        artifact["todayPublishedSetDigest"] = "0" * 64
        artifact = _attach_payload_digest(artifact)

        def encoded(value: object) -> bytes:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")

        artifact_bytes = encoded(artifact)
        artifact_path.write_bytes(artifact_bytes)
        manifest_path = current.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["discover.json"] = hashlib.sha256(
            artifact_bytes
        ).hexdigest()
        manifest_bytes = encoded(manifest)
        manifest_path.write_bytes(manifest_bytes)
        pointer_path = current.root.parent.parent / "current.json"
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["manifestSha256"] = hashlib.sha256(manifest_bytes).hexdigest()
        pointer_path.write_bytes(encoded(pointer))

        report = audit_discover_store(self.data_dir)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(
            report["issues"][0]["code"], "discover_recomputation_mismatch"
        )

    def test_source_tamper_sort_tamper_and_pointer_symlink_fail_closed(self) -> None:
        derive_trending_discover(self.data_dir, generated_at=GENERATED_AT)
        current = resolve_current_discover(self.data_dir)
        source = self.data_dir / current.artifact["sourceInventory"][0][
            "originalObservationPath"
        ]
        source.write_bytes(source.read_bytes() + b" ")
        self.assertEqual(audit_discover_store(self.data_dir)["status"], "failed")

        # Re-seed in a new isolated store for pointer-link behavior.
        other = Path(self.temporary.name) / "other-data"
        _seed_sources(other)
        _seed_today(other)
        derive_trending_discover(other, generated_at=GENERATED_AT)
        pointer = other / "artifacts" / "trending" / "discover" / "v1" / "current.json"
        target = pointer.with_name("pointer-target.json")
        target.write_bytes(pointer.read_bytes())
        pointer.unlink()
        try:
            os.symlink(target, pointer)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        self.assertEqual(audit_discover_store(other)["issues"][0]["code"], "discover_unsafe_path")

    def test_source_missing_and_today_source_tamper_fail_audit(self) -> None:
        derive_trending_discover(self.data_dir, generated_at=GENERATED_AT)
        current = resolve_current_discover(self.data_dir)
        (
            self.data_dir
            / current.artifact["sourceInventory"][0]["originalObservationPath"]
        ).unlink()
        self.assertEqual(audit_discover_store(self.data_dir)["status"], "failed")

        other = Path(self.temporary.name) / "today-tamper"
        _seed_sources(other)
        _seed_today(other)
        derive_trending_discover(other, generated_at=GENERATED_AT)
        current = resolve_current_discover(other)
        target = current.root / "sources" / "today-explosion.json"
        original = target.read_bytes()
        target.write_bytes(target.read_bytes() + b" ")
        self.assertEqual(audit_discover_store(other)["status"], "failed")
        target.write_bytes(original)
        manifest = current.root / "sources" / "today-manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        self.assertEqual(audit_discover_store(other)["status"], "failed")

    def test_pointer_temp_fails_audit_and_explicit_rollback_recovers_current(self) -> None:
        first = derive_trending_discover(self.data_dir, generated_at=GENERATED_AT)
        store = self.data_dir / "artifacts" / "trending" / "discover" / "v1"
        temporary = store / ".current.json.crash.tmp"
        temporary.write_text("partial", encoding="utf-8")
        report = audit_discover_store(self.data_dir)
        self.assertEqual(report["issues"][0]["code"], "discover_temporary_file_present")
        temporary.unlink()

        pointer = store / "current.json"
        pointer.write_text("not-json", encoding="utf-8")
        rolled = rollback_discover(self.data_dir, str(first["generationId"]))
        self.assertEqual(rolled["state"], "rolled_back")
        resolved = resolve_current_discover(self.data_dir)
        self.assertEqual(resolved.generation_id, first["generationId"])
        (resolved.root / "unexpected-empty-directory").mkdir()
        report = audit_discover_store(self.data_dir)
        self.assertEqual(report["issues"][0]["code"], "discover_generation_layout_invalid")

    def test_pointer_race_is_fail_closed_and_winner_remains_healthy(self) -> None:
        gate = threading.Barrier(2)
        outcomes: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def publisher(offset: int) -> None:
            try:
                gate.wait(5)
                outcomes.append(
                    derive_trending_discover(
                        self.data_dir,
                        generated_at=GENERATED_AT + timedelta(seconds=offset),
                    )
                )
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=publisher, args=(offset,)) for offset in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertFalse(errors)
        self.assertEqual({outcome["state"] for outcome in outcomes}, {"published", "already_derived"})
        self.assertEqual(
            audit_discover_store(self.data_dir)["generationId"],
            next(outcome["generationId"] for outcome in outcomes if outcome["state"] == "published"),
        )

    def test_today_source_cas_failure_publishes_nothing_and_cleans_candidate(self) -> None:
        with patch(
            "pipeline.trending_discover._require_today_source_current",
            side_effect=TrendingDiscoverError(
                "stale_today_exclusion",
                "Today changed during derivation",
                stage="publish",
            ),
        ):
            with self.assertRaises(TrendingDiscoverError) as context:
                derive_trending_discover(self.data_dir, generated_at=GENERATED_AT)
        self.assertEqual(context.exception.code, "stale_today_exclusion")
        store = self.data_dir / "artifacts" / "trending" / "discover" / "v1"
        self.assertFalse((store / "current.json").exists())
        candidates = store / "generations" / ".candidates"
        self.assertEqual(list(candidates.iterdir()), [])
        retained = [path for path in (store / "generations").iterdir() if path.name != ".candidates"]
        self.assertEqual(retained, [])

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_generation_junction_is_rejected_without_following_target(self) -> None:
        derive_trending_discover(self.data_dir, generated_at=GENERATED_AT)
        current = resolve_current_discover(self.data_dir)
        target = Path(self.temporary.name) / "junction-target"
        os.replace(current.root, target)
        created = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(current.root), str(target)],
            capture_output=True,
            check=False,
            text=True,
        )
        if created.returncode != 0:
            os.replace(target, current.root)
            self.skipTest("junction creation is unavailable")
        try:
            report = audit_discover_store(self.data_dir)
            self.assertEqual(report["issues"][0]["code"], "discover_unsafe_path")
        finally:
            current.root.rmdir()
            os.replace(target, current.root)

    def test_dry_run_writes_nothing_and_has_no_d1_surface(self) -> None:
        before = sorted(path.relative_to(self.data_dir).as_posix() for path in self.data_dir.rglob("*"))
        result = derive_trending_discover(self.data_dir, generated_at=GENERATED_AT, dry_run=True)
        after = sorted(path.relative_to(self.data_dir).as_posix() for path in self.data_dir.rglob("*"))
        self.assertEqual(result["state"], "dry_run")
        self.assertEqual(before, after)
        self.assertNotIn("d1Writes", result)


if __name__ == "__main__":
    unittest.main()
