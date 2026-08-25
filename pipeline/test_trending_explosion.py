from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pipeline.generations as generation_module
import pipeline.trending_explosion as trending_explosion_module
from pipeline.audit_data import audit_data
from pipeline.collect_github import candidate_queries
from pipeline.derive_trending_explosion import derive_trending_explosion
from pipeline.generations import (
    GenerationConflictError,
    create_candidate_generation,
    finalize_candidate_generation,
    publish_candidate_generation,
    resolve_current_generation,
    rollback_to_generation,
)
from pipeline.schema_validation import ArtifactKind, strict_json_loads, validate_payload
from pipeline.test_generations import _seed_legacy
from pipeline.test_trending_observations import _bundle, _bundle_bytes, _observation
from pipeline.trending_explosion import (
    EXPLOSION_PATH,
    TrendingExplosionError,
    audit_trending_explosion_generation,
    build_trending_explosion_artifact,
    candidate_artifact_bytes,
    load_explosion_sources,
    validate_explosion_artifact,
    write_candidate_explosion,
)
from pipeline.trending_observations import (
    attach_bundle_digest,
    capture_path_for_scheduled_at,
)


WINDOW_END = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
WINDOW_START = WINDOW_END - timedelta(hours=24)
GENERATED_AT = WINDOW_END + timedelta(minutes=10)
BASE_PUBLISHED_AT = WINDOW_START - timedelta(hours=1)


def _write_capture(
    data_dir: Path,
    scheduled_at: datetime,
    observations: list[dict[str, object]],
    *,
    captured_at: datetime | None = None,
    failed_queries: int = 0,
    incomplete: bool = False,
    metadata_failures: list[dict[str, object]] | None = None,
    raw_bytes: bytes | None = None,
) -> tuple[Path, dict[str, object]]:
    captured = captured_at or scheduled_at + timedelta(minutes=5)
    bundle = _bundle(
        scheduled_at,
        captured,
        observations=observations,
        failed_queries=failed_queries,
        incomplete=incomplete,
        metadata_failures=metadata_failures,
    )
    path = capture_path_for_scheduled_at(data_dir, scheduled_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw_bytes if raw_bytes is not None else _bundle_bytes(bundle))
    return path, bundle


def _observation_at(
    repository_id: int,
    repository: str,
    scheduled_at: datetime,
    *,
    stars: int,
    disabled: bool = False,
    archived: bool = False,
    fork: bool = False,
    mirror_url: str | None = None,
) -> dict[str, object]:
    captured = scheduled_at + timedelta(minutes=5)
    item = _observation(
        repository_id,
        repository,
        captured,
        stars=stars,
        scheduled_at=scheduled_at,
    )
    item["disabled"] = disabled
    item["archived"] = archived
    item["fork"] = fork
    item["mirrorUrl"] = mirror_url
    return item


def _seed_generation(data_dir: Path) -> str:
    _seed_legacy(data_dir)
    candidate = create_candidate_generation(
        data_dir,
        "bootstrap",
        generation_id="base-generation",
        created_at=BASE_PUBLISHED_AT,
    )
    published = publish_candidate_generation(candidate, published_at=BASE_PUBLISHED_AT)
    assert published.current.generation_id is not None
    return published.current.generation_id


def _fixture_observations() -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    baseline: list[dict[str, object]] = []
    current: list[dict[str, object]] = []
    partial: list[dict[str, object]] = []

    for repository_id in range(1, 26):
        baseline_name = (
            "old-owner/renamed" if repository_id == 1 else f"owner/project-{repository_id:02d}"
        )
        current_name = (
            "new-owner/renamed" if repository_id == 1 else f"owner/project-{repository_id:02d}"
        )
        baseline_stars = 1000 + repository_id
        # IDs 2 and 3 deliberately tie on both delta and totalStars so the
        # repository name is the final deterministic key.
        delta = 300 - repository_id
        total = baseline_stars + delta
        if repository_id in {2, 3}:
            total = 1500
            baseline_stars = 1200
        baseline.append(
            _observation_at(
                repository_id,
                baseline_name,
                WINDOW_START,
                stars=baseline_stars,
            )
        )
        current.append(
            _observation_at(
                repository_id,
                current_name,
                WINDOW_END,
                stars=total,
                archived=repository_id == 4,
                fork=repository_id == 5,
                mirror_url="https://mirror.example/project-6"
                if repository_id == 6
                else None,
            )
        )

    partial_at = WINDOW_END - timedelta(hours=12)
    for index, repository_id in enumerate(range(101, 105), start=1):
        partial.append(
            _observation_at(
                repository_id,
                f"pending/project-{index}",
                partial_at,
                stars=100 + index,
            )
        )
        current.append(
            _observation_at(
                repository_id,
                f"pending/project-{index}",
                WINDOW_END,
                stars=120 + index * 3,
            )
        )
    current.append(
        _observation_at(105, "pending/one-point", WINDOW_END, stars=999)
    )

    baseline.extend(
        [
            _observation_at(201, "conflict/disabled", WINDOW_START, stars=50),
            _observation_at(202, "conflict/decreased", WINDOW_START, stars=500),
            _observation_at(203, "conflict/disabled-two", WINDOW_START, stars=80),
        ]
    )
    current.extend(
        [
            _observation_at(
                201, "conflict/disabled", WINDOW_END, stars=60, disabled=True
            ),
            _observation_at(202, "conflict/decreased", WINDOW_END, stars=499),
            _observation_at(
                203, "conflict/disabled-two", WINDOW_END, stars=90, disabled=True
            ),
        ]
    )
    return baseline, partial, current


def _seed_exact_sources(data_dir: Path) -> tuple[Path, Path, Path]:
    baseline, partial, current = _fixture_observations()
    baseline_path, _ = _write_capture(data_dir, WINDOW_START, baseline)
    partial_path, _ = _write_capture(
        data_dir, WINDOW_END - timedelta(hours=12), partial
    )
    current_path, _ = _write_capture(data_dir, WINDOW_END, current)
    return baseline_path, partial_path, current_path


class TrendingExplosionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        _seed_exact_sources(self.data_dir)
        self.sources = load_explosion_sources(self.data_dir, WINDOW_END)
        self.artifact = build_trending_explosion_artifact(
            generation_id="fixture-generation",
            window_end=WINDOW_END,
            generated_at=GENERATED_AT,
            sources=self.sources,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_valid_contract_and_no_ai_fields(self) -> None:
        result = validate_payload(ArtifactKind.TRENDING_EXPLOSION, self.artifact)
        self.assertTrue(result.valid)
        self.assertEqual(self.artifact["policyVersion"], "trending-explosion-v1")
        self.assertNotIn("summaryZh", json.dumps(self.artifact))

    def test_naive_datetime_invalid_rank_and_negative_delta_are_rejected(self) -> None:
        for mutate in (
            lambda value: value.__setitem__("generatedAt", "2026-08-24T00:10:00"),
            lambda value: value["exactRanked"][0].__setitem__("rank", 0),
            lambda value: value["exactRanked"][0].__setitem__("observedStarDelta", -1),
        ):
            invalid = copy.deepcopy(self.artifact)
            mutate(invalid)
            self.assertFalse(
                validate_payload(ArtifactKind.TRENDING_EXPLOSION, invalid).valid
            )

    def test_duplicate_identity_and_illegal_source_path_are_rejected(self) -> None:
        duplicate = copy.deepcopy(self.artifact)
        duplicate["pendingRanked"][0]["githubRepositoryId"] = duplicate[
            "exactRanked"
        ][0]["githubRepositoryId"]
        with self.assertRaisesRegex(TrendingExplosionError, "unique repository IDs"):
            validate_explosion_artifact(duplicate)

        invalid_path = copy.deepcopy(self.artifact)
        invalid_path["sourceCaptures"]["current"]["generationRelativePath"] = (
            "../current.json"
        )
        self.assertFalse(
            validate_payload(ArtifactKind.TRENDING_EXPLOSION, invalid_path).valid
        )

    def test_subjective_ai_field_is_rejected_even_if_nested(self) -> None:
        invalid = copy.deepcopy(self.artifact)
        invalid["coverage"]["confidence"] = 0.9
        with self.assertRaises(TrendingExplosionError) as context:
            validate_explosion_artifact(invalid)
        self.assertEqual(context.exception.code, "explosion_schema_invalid")

    def test_repeated_pure_build_is_byte_equivalent(self) -> None:
        second = build_trending_explosion_artifact(
            generation_id="fixture-generation",
            window_end=WINDOW_END,
            generated_at=GENERATED_AT,
            sources=self.sources,
        )
        self.assertEqual(candidate_artifact_bytes(self.artifact), candidate_artifact_bytes(second))


class TrendingExplosionDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        _seed_exact_sources(self.data_dir)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _artifact(self, generation_id: str = "artifact-generation") -> dict[str, object]:
        sources = load_explosion_sources(self.data_dir, WINDOW_END)
        return build_trending_explosion_artifact(
            generation_id=generation_id,
            window_end=WINDOW_END,
            generated_at=GENERATED_AT,
            sources=sources,
        )

    def test_exact_ranking_is_complete_mechanical_and_stable(self) -> None:
        artifact = self._artifact()
        exact = artifact["exactRanked"]
        self.assertEqual(len(exact), 25)
        self.assertEqual([item["rank"] for item in exact], list(range(1, 26)))
        self.assertEqual(
            [item["observedStarDelta"] for item in exact],
            sorted((item["observedStarDelta"] for item in exact), reverse=True),
        )
        tied = [item for item in exact if item["githubRepositoryId"] in {2, 3}]
        self.assertEqual(
            [item["repository"] for item in tied],
            sorted(item["repository"] for item in tied),
        )
        self.assertGreater(len(exact), 20)
        self.assertEqual(artifact["coverage"]["exactEligibleCount"], 25)

    def test_pending_uses_real_partial_window_and_never_exact_rank(self) -> None:
        artifact = self._artifact()
        pending = artifact["pendingRanked"]
        self.assertEqual(len(pending), 5)
        self.assertTrue(all("rank" not in item for item in pending))
        partial = next(item for item in pending if item["githubRepositoryId"] == 101)
        self.assertEqual(partial["observedWindowHours"], 12)
        self.assertEqual(partial["observedWindowStarDelta"], 22)
        single = next(item for item in pending if item["githubRepositoryId"] == 105)
        self.assertIsNone(single["observedWindowHours"])
        self.assertIsNone(single["observedWindowStarDelta"])

    def test_rename_transfer_archived_fork_and_mirror_are_preserved(self) -> None:
        exact = self._artifact()["exactRanked"]
        by_id = {item["githubRepositoryId"]: item for item in exact}
        self.assertEqual(by_id[1]["repository"], "new-owner/renamed")
        self.assertEqual(by_id[1]["previousRepository"], "old-owner/renamed")
        self.assertTrue(by_id[4]["archived"])
        self.assertTrue(by_id[5]["fork"])
        self.assertEqual(by_id[6]["mirrorUrl"], "https://mirror.example/project-6")

    def test_negative_and_disabled_are_explicit_conflicts(self) -> None:
        artifact = self._artifact()
        self.assertEqual(len(artifact["conflicts"]), 3)
        reasons = [item["reason"] for item in artifact["conflicts"]]
        self.assertEqual(reasons.count("current_disabled"), 2)
        self.assertEqual(reasons.count("star_count_decreased"), 1)
        partition_ids = {
            item["githubRepositoryId"]
            for key in ("exactRanked", "pendingRanked", "conflicts")
            for item in artifact[key]
        }
        self.assertEqual(len(partition_ids), 33)

    def test_cross_capture_name_to_numeric_id_collision_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            baseline = [_observation_at(1, "owner/reused", WINDOW_START, stars=50)]
            current = [_observation_at(2, "owner/reused", WINDOW_END, stars=100)]
            _write_capture(root, WINDOW_START, baseline)
            _write_capture(root, WINDOW_END, current)
            artifact = build_trending_explosion_artifact(
                generation_id="identity-conflict-generation",
                window_end=WINDOW_END,
                generated_at=GENERATED_AT,
                sources=load_explosion_sources(root, WINDOW_END),
            )
            self.assertFalse(artifact["exactRanked"])
            self.assertFalse(artifact["pendingRanked"])
            self.assertEqual(artifact["conflicts"][0]["reason"], "source_identity_conflict")

    def test_rename_into_another_baseline_identity_is_explicit_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            baseline = [
                _observation_at(1, "owner/original", WINDOW_START, stars=50),
                _observation_at(2, "owner/reused", WINDOW_START, stars=60),
            ]
            current = [_observation_at(1, "owner/reused", WINDOW_END, stars=100)]
            _write_capture(root, WINDOW_START, baseline)
            _write_capture(root, WINDOW_END, current)
            artifact = build_trending_explosion_artifact(
                generation_id="rename-conflict-generation",
                window_end=WINDOW_END,
                generated_at=GENERATED_AT,
                sources=load_explosion_sources(root, WINDOW_END),
            )
            self.assertFalse(artifact["exactRanked"])
            self.assertFalse(artifact["pendingRanked"])
            self.assertEqual(
                artifact["conflicts"],
                [
                    {
                        "reason": "source_identity_conflict",
                        "githubRepositoryId": 1,
                        "repository": "owner/reused",
                        "currentStars": 100,
                        "baselineStars": 60,
                        "sourceCaptureIds": [
                            "trending-v1-20260823T000000Z",
                            "trending-v1-20260824T000000Z",
                        ],
                    }
                ],
            )

    def test_warming_up_and_baseline_missing_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            warming = Path(temporary) / "warming"
            current = [_observation_at(1, "owner/current", WINDOW_END, stars=100)]
            _write_capture(warming, WINDOW_END, current)
            warming_sources = load_explosion_sources(warming, WINDOW_END)
            self.assertEqual(warming_sources.window_state, "warming_up")

        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            witness_at = WINDOW_START - timedelta(hours=2)
            witness = [_observation_at(9, "owner/witness", witness_at, stars=50)]
            current = [_observation_at(1, "owner/current", WINDOW_END, stars=100)]
            _write_capture(missing, witness_at, witness)
            _write_capture(missing, WINDOW_END, current)
            missing_sources = load_explosion_sources(missing, WINDOW_END)
            self.assertEqual(missing_sources.window_state, "baseline_missing")
            artifact = build_trending_explosion_artifact(
                generation_id="missing-generation",
                window_end=WINDOW_END,
                generated_at=GENERATED_AT,
                sources=missing_sources,
            )
            self.assertEqual(artifact["coverage"]["state"], "degraded")
            self.assertEqual(artifact["pendingRanked"][0]["pendingReason"], "baseline_missing")

    def test_ineligible_baseline_is_pending_and_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            baseline = [_observation_at(1, "owner/one", WINDOW_START, stars=50)]
            current = [_observation_at(1, "owner/one", WINDOW_END, stars=100)]
            late = WINDOW_START + timedelta(minutes=20)
            baseline[0]["capturedAt"] = late.isoformat().replace("+00:00", "Z")
            _write_capture(root, WINDOW_START, baseline, captured_at=late)
            _write_capture(root, WINDOW_END, current)
            sources = load_explosion_sources(root, WINDOW_END)
            artifact = build_trending_explosion_artifact(
                generation_id="ineligible-generation",
                window_end=WINDOW_END,
                generated_at=GENERATED_AT,
                sources=sources,
            )
            self.assertEqual(artifact["window"]["state"], "baseline_missing")
            self.assertEqual(artifact["pendingRanked"][0]["pendingReason"], "baseline_ineligible")
            self.assertFalse(artifact["exactRanked"])

    def test_degraded_capture_propagates_coverage(self) -> None:
        baseline, partial, current = _fixture_observations()
        second_query = candidate_queries(WINDOW_START)[1]
        for item in baseline:
            item["recalledBy"] = [
                {
                    "source": "github_search",
                    "sourceKey": "query-02",
                    "sourceRank": 1,
                    "capturedAt": (WINDOW_START + timedelta(minutes=5))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "queryId": "query-02",
                    "query": second_query,
                    "page": 1,
                }
            ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            _write_capture(root, WINDOW_START, baseline, failed_queries=1)
            _write_capture(root, WINDOW_END - timedelta(hours=12), partial)
            _write_capture(root, WINDOW_END, current)
            artifact = build_trending_explosion_artifact(
                generation_id="degraded-generation",
                window_end=WINDOW_END,
                generated_at=GENERATED_AT,
                sources=load_explosion_sources(root, WINDOW_END),
            )
            self.assertEqual(artifact["coverage"]["state"], "degraded")
            self.assertEqual(artifact["coverage"]["baselineFailedQueryCount"], 1)

    def test_exact_limit_is_500_without_hidden_proxy_sort(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            baseline = [
                _observation_at(i, f"many/project-{i:03d}", WINDOW_START, stars=1000)
                for i in range(1, 501)
            ]
            current = [
                _observation_at(i, f"many/project-{i:03d}", WINDOW_END, stars=1000 + i)
                for i in range(1, 501)
            ]
            _write_capture(root, WINDOW_START, baseline)
            _write_capture(root, WINDOW_END, current)
            artifact = build_trending_explosion_artifact(
                generation_id="limit-generation",
                window_end=WINDOW_END,
                generated_at=GENERATED_AT,
                sources=load_explosion_sources(root, WINDOW_END),
            )
            self.assertEqual(len(artifact["exactRanked"]), 500)
            self.assertEqual(artifact["exactRanked"][0]["githubRepositoryId"], 500)
            self.assertEqual(artifact["exactRanked"][-1]["githubRepositoryId"], 1)


class TrendingExplosionSourceSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        self.baseline, self.partial, self.current = _fixture_observations()
        _write_capture(self.data_dir, WINDOW_START, self.baseline)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_corrupt_json_digest_and_window_eligibility_fail_closed(self) -> None:
        current_path, valid = _write_capture(self.data_dir, WINDOW_END, self.current)
        cases: list[bytes] = [b"{not-json\n"]
        digest_bad = copy.deepcopy(valid)
        digest_bad["observations"][0]["totalStars"] += 1
        cases.append(_bundle_bytes(digest_bad))
        eligibility_bad = copy.deepcopy(valid)
        eligibility_bad["windowEligible"] = False
        eligibility_bad = attach_bundle_digest(eligibility_bad)
        cases.append(_bundle_bytes(eligibility_bad))
        for content in cases:
            current_path.write_bytes(content)
            with self.assertRaises(TrendingExplosionError):
                load_explosion_sources(self.data_dir, WINDOW_END)

    def test_missing_and_legitimately_ineligible_current_have_stable_codes(self) -> None:
        missing = Path(self.temporary.name) / "missing"
        with self.assertRaises(TrendingExplosionError) as missing_error:
            load_explosion_sources(missing, WINDOW_END)
        self.assertEqual(missing_error.exception.code, "explosion_current_capture_missing")

        late = WINDOW_END + timedelta(minutes=20)
        late_current = copy.deepcopy(self.current)
        for item in late_current:
            item["capturedAt"] = late.isoformat().replace("+00:00", "Z")
            for provenance in item["recalledBy"]:
                provenance["capturedAt"] = late.isoformat().replace("+00:00", "Z")
        _write_capture(
            self.data_dir,
            WINDOW_END,
            late_current,
            captured_at=late,
        )
        with self.assertRaises(TrendingExplosionError) as ineligible_error:
            load_explosion_sources(self.data_dir, WINDOW_END)
        self.assertEqual(
            ineligible_error.exception.code, "explosion_current_capture_ineligible"
        )

    def test_capture_identity_path_mismatch_fails_closed(self) -> None:
        wrong_time = WINDOW_END - timedelta(hours=2)
        wrong = [
            _observation_at(1, "owner/wrong", wrong_time, stars=100)
        ]
        _, bundle = _write_capture(self.data_dir, wrong_time, wrong)
        current_path = capture_path_for_scheduled_at(self.data_dir, WINDOW_END)
        current_path.parent.mkdir(parents=True, exist_ok=True)
        current_path.write_bytes(_bundle_bytes(bundle))
        with self.assertRaises(TrendingExplosionError):
            load_explosion_sources(self.data_dir, WINDOW_END)

    def test_symlink_capture_is_rejected(self) -> None:
        target_root = Path(self.temporary.name) / "outside"
        target, bundle = _write_capture(target_root, WINDOW_END, self.current)
        link = capture_path_for_scheduled_at(self.data_dir, WINDOW_END)
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, link)
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        with self.assertRaises(TrendingExplosionError):
            load_explosion_sources(self.data_dir, WINDOW_END)
        self.assertTrue(bundle)

    @unittest.skipUnless(os.name == "nt", "junction control is Windows-specific")
    def test_junction_capture_parent_is_rejected(self) -> None:
        target_root = Path(self.temporary.name) / "junction-target"
        _write_capture(target_root, WINDOW_END, self.current)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.data_dir / "observations")
        link = self.data_dir / "observations"
        command = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target_root / "observations")],
            capture_output=True,
            text=True,
            check=False,
        )
        if command.returncode != 0:
            self.fail(f"junction fixture could not be created: {command.stderr or command.stdout}")
        with self.assertRaises(TrendingExplosionError):
            load_explosion_sources(self.data_dir, WINDOW_END)


class TrendingExplosionSelfContainedModeTests(unittest.TestCase):
    def _write_and_audit(
        self,
        data_dir: Path,
        generation_root: Path,
    ) -> dict[str, object]:
        sources = load_explosion_sources(data_dir, WINDOW_END)
        artifact = build_trending_explosion_artifact(
            generation_id=generation_root.name,
            window_end=WINDOW_END,
            generated_at=GENERATED_AT,
            sources=sources,
        )
        generation_root.mkdir(parents=True)
        write_candidate_explosion(generation_root, artifact, sources)
        self.assertEqual(
            audit_trending_explosion_generation(generation_root)["status"], "healthy"
        )
        shutil.rmtree(data_dir / "observations")
        self.assertEqual(
            audit_trending_explosion_generation(generation_root)["status"], "healthy"
        )
        return artifact

    def test_warming_up_artifact_is_self_contained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            current = [_observation_at(1, "owner/warming", WINDOW_END, stars=100)]
            _write_capture(data, WINDOW_END, current)
            artifact = self._write_and_audit(data, root / "warming-generation")
            self.assertEqual(artifact["window"]["state"], "warming_up")

    def test_baseline_missing_artifact_freezes_coverage_witness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            witness_at = WINDOW_START - timedelta(hours=2)
            witness = [_observation_at(7, "owner/witness", witness_at, stars=30)]
            current = [_observation_at(1, "owner/current", WINDOW_END, stars=100)]
            _write_capture(data, witness_at, witness)
            _write_capture(data, WINDOW_END, current)
            generation_root = root / "missing-generation"
            artifact = self._write_and_audit(data, generation_root)
            self.assertEqual(artifact["window"]["state"], "baseline_missing")
            self.assertTrue(
                (generation_root / "trending/sources/coverage-witness.json").is_file()
            )

    def test_degraded_exact_artifact_is_self_contained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            baseline = [_observation_at(1, "owner/one", WINDOW_START, stars=50)]
            current = [_observation_at(1, "owner/one", WINDOW_END, stars=100)]
            _write_capture(data, WINDOW_START, baseline, incomplete=True)
            _write_capture(data, WINDOW_END, current)
            artifact = self._write_and_audit(data, root / "degraded-generation")
            self.assertEqual(artifact["window"]["state"], "exact")
            self.assertEqual(artifact["coverage"]["state"], "degraded")

class TrendingExplosionGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "data"
        self.base_generation = _seed_generation(self.data_dir)
        self.baseline_path, self.partial_path, self.current_path = _seed_exact_sources(
            self.data_dir
        )
        self.base_root = resolve_current_generation(self.data_dir).root
        self.base_fact_hashes = self._fact_hashes(self.base_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _fact_hashes(root: Path) -> dict[str, str]:
        paths = [
            root / "snapshots/latest.json",
            root / "catalog/latest.json",
            root / "signals/latest.json",
            root / "signals/enrichment.json",
            *sorted((root / "snapshots/history").glob("*.json")),
            *sorted((root / "analysis").glob("*.json")),
            *sorted((root / "enrichment").glob("*.json")),
        ]
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
            if path.exists()
        }

    def _publish(self, identifier: str = "explosion-generation") -> dict[str, object]:
        return derive_trending_explosion(
            self.data_dir,
            WINDOW_END,
            generation_id=identifier,
            generated_at=GENERATED_AT,
        )

    def test_dry_run_is_deterministic_and_changes_nothing(self) -> None:
        pointer_before = (self.data_dir / "current.json").read_bytes()
        generations_before = sorted(
            path.name for path in (self.data_dir / "generations").iterdir()
        )
        first = derive_trending_explosion(
            self.data_dir,
            WINDOW_END,
            dry_run=True,
            generation_id="dry-run-generation",
            generated_at=GENERATED_AT,
        )
        second = derive_trending_explosion(
            self.data_dir,
            WINDOW_END,
            dry_run=True,
            generation_id="dry-run-generation",
            generated_at=GENERATED_AT,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "dry_run")
        self.assertEqual((self.data_dir / "current.json").read_bytes(), pointer_before)
        self.assertEqual(
            sorted(path.name for path in (self.data_dir / "generations").iterdir()),
            generations_before,
        )

    def test_publish_has_manifest_inventory_and_preserves_business_facts(self) -> None:
        result = self._publish()
        self.assertEqual(result["state"], "derived")
        current = resolve_current_generation(self.data_dir)
        self.assertEqual(current.generation_id, "explosion-generation")
        manifest = current.manifest
        assert manifest is not None
        self.assertEqual(manifest["operation"], "derive")
        self.assertIn("trending/explosion.json", manifest["artifacts"])
        self.assertIn("trending/sources/current.json", manifest["artifacts"])
        self.assertIn("trending/sources/baseline.json", manifest["artifacts"])
        self.assertEqual(self._fact_hashes(current.root), self.base_fact_hashes)
        self.assertEqual(
            (current.root / "trending/sources/current.json").read_bytes(),
            self.current_path.read_bytes(),
        )
        self.assertEqual(
            (current.root / "trending/sources/baseline.json").read_bytes(),
            self.baseline_path.read_bytes(),
        )
        self.assertEqual(audit_data(current.root)["status"], "healthy")

    def test_already_derived_stale_window_and_same_window_conflict(self) -> None:
        self._publish()
        retained_count = len(list((self.data_dir / "generations").iterdir()))
        repeated = derive_trending_explosion(
            self.data_dir,
            WINDOW_END,
            generation_id="unused-generation",
            generated_at=GENERATED_AT + timedelta(minutes=1),
        )
        self.assertEqual(repeated["state"], "already_derived")
        self.assertEqual(len(list((self.data_dir / "generations").iterdir())), retained_count)

        with self.assertRaises(TrendingExplosionError) as stale:
            derive_trending_explosion(
                self.data_dir,
                WINDOW_END - timedelta(hours=24),
                generated_at=GENERATED_AT + timedelta(minutes=2),
            )
        self.assertEqual(stale.exception.code, "stale_explosion_window")

        changed = strict_json_loads(self.current_path.read_text(encoding="utf-8"))
        changed["observations"][0]["totalStars"] += 1
        changed = attach_bundle_digest(changed)
        self.current_path.write_bytes(_bundle_bytes(changed))
        with self.assertRaises(TrendingExplosionError) as conflict:
            derive_trending_explosion(
                self.data_dir,
                WINDOW_END,
                generated_at=GENERATED_AT + timedelta(minutes=3),
            )
        self.assertEqual(conflict.exception.code, "explosion_source_conflict")

    def test_source_retention_independent_audit_and_rollback(self) -> None:
        self._publish()
        explosion_generation = resolve_current_generation(self.data_dir)
        shutil.rmtree(self.data_dir / "observations")
        self.assertEqual(
            audit_trending_explosion_generation(explosion_generation.root)["status"],
            "healthy",
        )

        later_at = GENERATED_AT + timedelta(minutes=2)
        later = create_candidate_generation(
            self.data_dir,
            "derive",
            generation_id="later-generation",
            created_at=later_at,
            overlay_flat_staging=False,
        )
        finalize_candidate_generation(later)
        publish_candidate_generation(later, published_at=later_at)
        carried = resolve_current_generation(self.data_dir)
        self.assertTrue((carried.root / "trending/explosion.json").is_file())
        carried_artifact = strict_json_loads(
            (carried.root / "trending/explosion.json").read_text(encoding="utf-8")
        )
        self.assertEqual(carried_artifact["generationId"], "later-generation")
        self.assertEqual(
            audit_trending_explosion_generation(carried.root)["status"], "healthy"
        )
        repeated = derive_trending_explosion(
            self.data_dir,
            WINDOW_END,
            generation_id="unused-after-carry",
            generated_at=later_at + timedelta(seconds=1),
        )
        self.assertEqual(repeated["state"], "already_derived")
        self.assertEqual(repeated["generationId"], "later-generation")
        rolled_back = rollback_to_generation(
            self.data_dir,
            "explosion-generation",
            published_at=later_at + timedelta(minutes=1),
        )
        self.assertTrue(rolled_back.rolled_back)
        self.assertEqual(rolled_back.current.generation_id, "explosion-generation")
        self.assertEqual(
            audit_trending_explosion_generation(rolled_back.current.root)["status"],
            "healthy",
        )

    def test_generation_local_source_and_ranking_tamper_are_detected(self) -> None:
        self._publish()
        root = resolve_current_generation(self.data_dir).root
        source = root / "trending/sources/current.json"
        source.write_bytes(source.read_bytes() + b" ")
        self.assertEqual(audit_trending_explosion_generation(root)["status"], "failed")

        # Use a fresh independent fixture because retained generations are
        # intentionally immutable and this test is a negative control.
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data"
            _seed_generation(data)
            _seed_exact_sources(data)
            derive_trending_explosion(
                data,
                WINDOW_END,
                generation_id="ranking-generation",
                generated_at=GENERATED_AT,
            )
            ranking_root = resolve_current_generation(data).root
            artifact_path = ranking_root / EXPLOSION_PATH
            artifact = strict_json_loads(artifact_path.read_text(encoding="utf-8"))
            artifact["exactRanked"][0]["observedStarDelta"] += 1
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
            self.assertEqual(
                audit_trending_explosion_generation(ranking_root)["status"],
                "failed",
            )

    def test_cas_loser_keeps_winner_and_diagnostic_candidate(self) -> None:
        real_publish = generation_module.publish_candidate_generation
        winner_id = "concurrent-winner"

        def publish_winner_then_original(candidate, *, published_at=None):
            winner_time = GENERATED_AT + timedelta(microseconds=1)
            winner = create_candidate_generation(
                self.data_dir,
                "derive",
                generation_id=winner_id,
                created_at=winner_time,
                overlay_flat_staging=False,
            )
            finalize_candidate_generation(winner)
            real_publish(winner, published_at=winner_time)
            return real_publish(candidate, published_at=published_at)

        with patch(
            "pipeline.derive_trending_explosion.publish_candidate_generation",
            side_effect=publish_winner_then_original,
        ):
            with self.assertRaises(GenerationConflictError) as context:
                self._publish("cas-loser")
        self.assertEqual(context.exception.code, "stale_base_generation")
        self.assertEqual(resolve_current_generation(self.data_dir).generation_id, winner_id)
        loser = self.data_dir / "generations/.candidates/cas-loser"
        self.assertTrue(loser.is_dir())
        self.assertEqual(strict_json_loads((loser / "manifest.json").read_text())["state"], "ready")

    def test_source_copy_interruption_never_switches_current(self) -> None:
        pointer_before = (self.data_dir / "current.json").read_bytes()
        real_write = trending_explosion_module._atomic_write_bytes

        def interrupt_baseline(path: Path, content: bytes, root: Path) -> None:
            if path.as_posix().endswith("trending/sources/baseline.json"):
                raise TrendingExplosionError(
                    "fixture_write_interrupted",
                    "simulated source copy interruption",
                    stage="write",
                )
            real_write(path, content, root)

        with patch(
            "pipeline.trending_explosion._atomic_write_bytes",
            side_effect=interrupt_baseline,
        ):
            with self.assertRaises(TrendingExplosionError):
                self._publish("interrupted-generation")
        self.assertEqual((self.data_dir / "current.json").read_bytes(), pointer_before)
        failed = self.data_dir / "generations/.candidates/interrupted-generation/manifest.json"
        self.assertEqual(strict_json_loads(failed.read_text())["state"], "failed")

    def test_old_generation_without_explosion_remains_valid(self) -> None:
        current = resolve_current_generation(self.data_dir)
        self.assertFalse((current.root / "trending").exists())
        self.assertEqual(audit_data(current.root)["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
