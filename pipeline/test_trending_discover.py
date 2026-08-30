from __future__ import annotations

import copy
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.collect_github import candidate_queries
from pipeline.derive_trending_explosion import derive_trending_explosion
from pipeline.schema_validation import ArtifactKind, validate_payload
from pipeline.test_trending_explosion import _observation_at, _seed_generation, _write_capture
from pipeline.trending_discover import (
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
        self.assertEqual(self.artifact["coverage"]["excludedExactCount"], 1)
        self.assertTrue(all(item["observedWindowHours"] <= 26.5 for items in stages.values() for item in items))
        self.assertNotIn("expected24h", str(self.artifact))

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

    def test_schema_rejects_ai_and_extrapolation_fields(self) -> None:
        for field in ("aiScore", "expected24hDelta"):
            invalid = copy.deepcopy(self.artifact)
            invalid[field] = 1
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

    def test_source_tamper_sort_tamper_and_pointer_symlink_fail_closed(self) -> None:
        derive_trending_discover(self.data_dir, generated_at=GENERATED_AT)
        current = resolve_current_discover(self.data_dir)
        source = current.root / "sources" / "capture-01.json"
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
        (current.root / "sources" / "capture-01.json").unlink()
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
