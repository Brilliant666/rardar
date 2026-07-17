from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pipeline.generations as generation_module
from pipeline.codex_queue import build_codex_queue
from pipeline.generations import (
    CandidateGenerationError,
    CurrentGenerationError,
    GenerationConflictError,
    GenerationProtocolError,
    create_candidate_generation,
    fail_candidate_generation,
    finalize_candidate_generation,
    main,
    publish_candidate_generation,
    resolve_current_artifacts,
    resolve_current_generation,
    rollback_to_generation,
)
from pipeline.schema_validation import strict_json_dumps, strict_json_loads


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


class RefreshStubClient:
    def search(self, _query: str, per_page: int = 30):
        return [
            {
                "full_name": "demo/agent-tool",
                "html_url": "https://github.com/demo/agent-tool",
                "description": "AI developer workflow tool",
                "owner": {"login": "demo"},
                "language": "Python",
                "license": {"spdx_id": "MIT"},
                "topics": ["ai", "developer-tools"],
                "stargazers_count": 100,
                "forks_count": 20,
                "open_issues_count": 2,
                "created_at": "2026-07-01T00:00:00Z",
                "updated_at": "2026-07-10T00:00:00Z",
                "pushed_at": "2026-07-10T00:00:00Z",
                "default_branch": "main",
            }
        ]


def _supported_paths(root: Path) -> list[Path]:
    paths = [
        root / "snapshots/latest.json",
        root / "catalog/latest.json",
        root / "signals/latest.json",
        root / "queues/codex.json",
    ]
    optional = root / "signals/enrichment.json"
    if optional.exists():
        paths.append(optional)
    for directory in ("snapshots/history", "analysis", "enrichment"):
        paths.extend(sorted((root / directory).glob("*.json")))
    return [path for path in paths if path.exists()]


def _read(path: Path) -> dict[str, object]:
    payload = strict_json_loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(strict_json_dumps(payload) + "\n", encoding="utf-8")


def _seed_legacy(data_dir: Path) -> None:
    source = resolve_current_generation(REPOSITORY_ROOT / "data").root
    for path in _supported_paths(source):
        destination = data_dir / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)

    # Immutable checked-in generations retain their original bytes, including
    # historical CRLF snapshots.  Re-materialize this scratch legacy baseline
    # with the platform-native JSON writer used by refresh fixtures so the
    # byte-exact archive assertion does not depend on the host line ending.
    snapshot_path = data_dir / "snapshots/latest.json"
    _write(snapshot_path, _read(snapshot_path))

    # A checked-in source may already be a generation whose queue binds its
    # evidence to that immutable path.  A legacy fixture must bind to data/.
    catalog = _read(data_dir / "catalog/latest.json")
    signals = _read(data_dir / "signals/latest.json")
    existing = _read(data_dir / "queues/codex.json")
    generated_at = str(existing["generatedAt"])
    generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    scope = existing.get("scope")
    assert isinstance(scope, dict)
    queue = build_codex_queue(
        catalog,
        signals,
        data_dir / "enrichment",
        data_dir / "signals/enrichment.json",
        generated,
        int(scope.get("projectLimit", 5)),
        int(scope.get("signalLimit", 10)),
        input_data_prefix="data",
    )
    _write(data_dir / "queues/codex.json", queue)


def _candidate(data_dir: Path, identifier: str, operation: str = "derive"):
    return create_candidate_generation(
        data_dir,
        operation,  # type: ignore[arg-type]
        generation_id=identifier,
    )


def _ready_refresh_candidate(data_dir: Path, identifier: str):
    from pipeline.refresh import _refresh_candidate_tree

    current = resolve_current_generation(data_dir)
    snapshot = _read(current.root / "snapshots/latest.json")
    now = datetime.fromisoformat(str(snapshot["captured_at"]).replace("Z", "+00:00"))
    now += timedelta(minutes=1)
    signals = _read(current.root / "signals/latest.json")
    signals["capturedAt"] = now.isoformat()
    candidate = _candidate(data_dir, identifier, "refresh")
    with patch("pipeline.refresh.collect_signals", return_value=signals):
        _refresh_candidate_tree(
            candidate,
            now,
            14,
            30,
            0,
            RefreshStubClient(),
            None,
            True,
        )
    # The refresh protocol requires one byte-exact archive of the published
    # current snapshot.  JSON reserialization normalizes line endings on Linux,
    # while checked-out repository fixtures may contain CRLF bytes.  Restore the
    # exact source bytes before finalize hashes the ready candidate.
    source_snapshot = current.root / "snapshots/latest.json"
    captured_at = snapshot.get("captured_at")
    matching_archives = [
        path
        for path in (candidate.path / "snapshots/history").glob("*.json")
        if _read(path).get("captured_at") == captured_at
    ]
    if len(matching_archives) != 1:
        raise AssertionError(
            "refresh test fixture must contain exactly one current snapshot archive"
        )
    matching_archives[0].write_bytes(source_snapshot.read_bytes())
    finalize_candidate_generation(candidate)
    return candidate, current


def _rehash_ready_artifact(candidate, relative: str) -> None:
    manifest_path = candidate.path / "manifest.json"
    manifest = _read(manifest_path)
    artifact_path = candidate.path / relative
    manifest["hashes"][relative] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    _write(manifest_path, manifest)


def _published_growth_bytes(current) -> dict[str, bytes]:
    paths = [
        current.root / "snapshots/latest.json",
        *sorted((current.root / "snapshots/history").glob("*.json")),
    ]
    return {
        path.relative_to(current.root).as_posix(): path.read_bytes()
        for path in paths
    }


def _publish_two_generations(data_dir: Path):
    _seed_legacy(data_dir)
    first = publish_candidate_generation(
        _candidate(data_dir, "generation-1", "bootstrap")
    ).current
    second = publish_candidate_generation(
        _candidate(data_dir, "generation-2")
    ).current
    return first, second


class GenerationProtocolTests(unittest.TestCase):
    def test_signal_enrichment_is_required_for_legacy_and_generation_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            (data_dir / "signals/enrichment.json").unlink()

            with self.assertRaises(CurrentGenerationError) as raised:
                resolve_current_generation(data_dir)

            self.assertEqual(raised.exception.code, "legacy_data_unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            candidate = _candidate(data_dir, "missing-signal-enrichment", "bootstrap")
            (candidate.path / "signals/enrichment.json").unlink()

            with self.assertRaises(CandidateGenerationError) as candidate_error:
                finalize_candidate_generation(candidate)

            self.assertEqual(candidate_error.exception.code, "missing_required_artifact")
            failed = _read(candidate.path / "manifest.json")
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["failureStage"], "manifest")

    def test_successful_candidate_schema_audit_and_atomic_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)

            candidate = _candidate(data_dir, "generation-1", "bootstrap")
            ready = finalize_candidate_generation(candidate)
            result = publish_candidate_generation(candidate)

            self.assertEqual(ready["state"], "ready")
            self.assertIn(ready["audit"]["status"], {"healthy", "degraded"})
            self.assertGreater(ready["audit"]["validatedCount"], 0)
            self.assertEqual(result.current.generation_id, "generation-1")
            self.assertFalse(result.current.legacy)
            self.assertFalse(candidate.path.exists())
            self.assertTrue((data_dir / "generations/generation-1/manifest.json").is_file())
            pointer = _read(data_dir / "current.json")
            self.assertEqual(pointer["generationId"], "generation-1")
            self.assertIsNone(pointer["previousGenerationId"])

    def test_schema_failure_retains_candidate_and_never_switches_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            current = resolve_current_generation(data_dir)
            pointer_before = (data_dir / "current.json").read_bytes()
            baseline = _published_growth_bytes(current)
            candidate = _candidate(data_dir, "schema-failure")
            catalog_path = candidate.path / "catalog/latest.json"
            catalog = _read(catalog_path)
            catalog["projects"] = "not-an-array"
            _write(catalog_path, catalog)

            with self.assertRaises(CandidateGenerationError) as raised:
                finalize_candidate_generation(candidate)

            self.assertEqual(raised.exception.code, "schema_validation_failed")
            self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)
            after = resolve_current_generation(data_dir)
            self.assertEqual(after.generation_id, "generation-1")
            self.assertEqual(_published_growth_bytes(after), baseline)
            failed = _read(candidate.path / "manifest.json")
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["failureStage"], "schema_validation")

    def test_public_failure_is_diagnostic_idempotent_and_never_mutates_ready_or_final(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            failed_candidate = _candidate(data_dir, "failed-once", "bootstrap")

            first = fail_candidate_generation(failed_candidate, "build", "simulated failure")
            second = fail_candidate_generation(failed_candidate, "audit", "must not replace first")

            self.assertEqual(first, second)
            self.assertEqual(second["failureStage"], "build")
            with self.assertRaises(CandidateGenerationError) as invalid_stage:
                fail_candidate_generation(failed_candidate, "unknown", "bad stage")
            self.assertEqual(invalid_stage.exception.code, "invalid_failure_stage")

            ready_candidate = _candidate(data_dir, "ready-safe", "bootstrap")
            ready = finalize_candidate_generation(ready_candidate)
            self.assertEqual(
                fail_candidate_generation(ready_candidate, "publish", "ignored"),
                ready,
            )
            publish_candidate_generation(ready_candidate)
            final_manifest = fail_candidate_generation(
                ready_candidate,
                "publish",
                "also ignored",
            )
            self.assertEqual(final_manifest["state"], "ready")

    def test_clone_failure_leaves_an_early_valid_failure_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            with patch(
                "pipeline.generations._copy_supported_artifacts",
                side_effect=OSError("simulated clone interruption"),
            ):
                with self.assertRaises(CandidateGenerationError) as raised:
                    _candidate(data_dir, "early-failure", "bootstrap")

            self.assertEqual(raised.exception.code, "candidate_write_failed")
            manifest = _read(
                data_dir / "generations/.candidates/early-failure/manifest.json"
            )
            self.assertEqual(manifest["state"], "failed")
            self.assertEqual(manifest["failureStage"], "build")
            self.assertIn("simulated clone interruption", manifest["error"])

    def test_cross_file_audit_failure_does_not_switch_or_advance_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            current = resolve_current_generation(data_dir)
            pointer_before = (data_dir / "current.json").read_bytes()
            baseline = _published_growth_bytes(current)
            candidate = _candidate(data_dir, "audit-failure")
            catalog_path = candidate.path / "catalog/latest.json"
            catalog = _read(catalog_path)
            projects = catalog["projects"]
            assert isinstance(projects, list) and isinstance(projects[0], dict)
            projects[0]["stars"] = int(projects[0]["stars"]) + 1
            _write(catalog_path, catalog)

            with self.assertRaises(CandidateGenerationError) as raised:
                finalize_candidate_generation(candidate)

            self.assertEqual(raised.exception.code, "audit_failed")
            self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)
            after = resolve_current_generation(data_dir)
            self.assertEqual(after.generation_id, "generation-1")
            self.assertEqual(_published_growth_bytes(after), baseline)
            failed = _read(candidate.path / "manifest.json")
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(failed["failureStage"], "audit")

    def test_pointer_replace_interruption_keeps_previous_generation_and_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            first = _candidate(data_dir, "generation-1", "bootstrap")
            publish_candidate_generation(first)
            previous_pointer = (data_dir / "current.json").read_bytes()
            previous_snapshot = resolve_current_generation(data_dir).root.joinpath(
                "snapshots/latest.json"
            ).read_bytes()
            second = _candidate(data_dir, "generation-2")
            finalize_candidate_generation(second)

            def fail_pointer(source: Path, target: Path) -> None:
                if target.name == "current.json":
                    raise OSError("simulated pointer interruption")
                os.replace(source, target)

            with patch("pipeline.generations._replace_file", side_effect=fail_pointer):
                with self.assertRaises(CandidateGenerationError) as raised:
                    publish_candidate_generation(second)

            self.assertEqual(raised.exception.code, "pointer_write_failed")
            self.assertEqual((data_dir / "current.json").read_bytes(), previous_pointer)
            current = resolve_current_generation(data_dir)
            self.assertEqual(current.generation_id, "generation-1")
            self.assertEqual(
                (current.root / "snapshots/latest.json").read_bytes(), previous_snapshot
            )
            self.assertTrue((data_dir / "generations/generation-2").is_dir())

            retried = publish_candidate_generation(second)
            self.assertEqual(retried.current.generation_id, "generation-2")

    def test_inventory_read_interruptions_are_structured_and_never_switch_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            original_inventory = generation_module._artifact_inventory

            with patch(
                "pipeline.generations._artifact_inventory",
                side_effect=OSError("simulated inventory interruption"),
            ):
                with self.assertRaises(CurrentGenerationError) as resolver_error:
                    resolve_current_generation(data_dir)
            self.assertEqual(resolver_error.exception.code, "artifact_inventory_failed")

            second = _candidate(data_dir, "generation-2")
            finalize_candidate_generation(second)
            pointer_before = (data_dir / "current.json").read_bytes()

            def fail_second(root: Path, *, require_complete: bool = False):
                if Path(root).name == "generation-2":
                    raise OSError("simulated candidate inventory interruption")
                return original_inventory(root, require_complete=require_complete)

            with patch("pipeline.generations._artifact_inventory", side_effect=fail_second):
                with self.assertRaises(CandidateGenerationError) as publish_error:
                    publish_candidate_generation(second)
            self.assertEqual(publish_error.exception.code, "artifact_inventory_failed")
            self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)

            publish_candidate_generation(second)
            rollback_pointer = (data_dir / "current.json").read_bytes()

            def fail_first(root: Path, *, require_complete: bool = False):
                if Path(root).name == "generation-1":
                    raise OSError("simulated rollback inventory interruption")
                return original_inventory(root, require_complete=require_complete)

            with patch("pipeline.generations._artifact_inventory", side_effect=fail_first):
                with self.assertRaises(CandidateGenerationError) as rollback_error:
                    rollback_to_generation(data_dir, "generation-1")
            self.assertEqual(rollback_error.exception.code, "artifact_inventory_failed")
            self.assertEqual((data_dir / "current.json").read_bytes(), rollback_pointer)

    def test_successful_pointer_switch_has_no_fallible_post_write_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            second = _candidate(data_dir, "generation-2")
            finalize_candidate_generation(second)
            current = resolve_current_generation(data_dir)

            with patch(
                "pipeline.generations.resolve_current_generation",
                side_effect=[current, AssertionError("unexpected post-write resolve")],
            ) as resolver:
                published = publish_candidate_generation(second)

            self.assertEqual(resolver.call_count, 1)
            self.assertEqual(published.current.generation_id, "generation-2")
            pointer = _read(data_dir / "current.json")
            self.assertEqual(pointer["generationId"], "generation-2")

            current = published.current
            with patch(
                "pipeline.generations.resolve_current_generation",
                side_effect=[current, AssertionError("unexpected post-rollback resolve")],
            ) as resolver:
                rolled_back = rollback_to_generation(data_dir, "generation-1")

            self.assertEqual(resolver.call_count, 1)
            self.assertEqual(rolled_back.current.generation_id, "generation-1")

    def test_two_real_publishers_compete_and_only_one_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            candidates = [_candidate(data_dir, "generation-2"), _candidate(data_dir, "generation-3")]
            for candidate in candidates:
                finalize_candidate_generation(candidate)
            barrier = threading.Barrier(2)

            def publish(candidate):
                barrier.wait(timeout=10)
                try:
                    return ("published", publish_candidate_generation(candidate).current.generation_id)
                except GenerationConflictError as error:
                    return (error.code, candidate.generation_id)

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(publish, candidates))

            self.assertEqual(sum(state == "published" for state, _ in outcomes), 1)
            self.assertEqual(sum(state == "stale_base_generation" for state, _ in outcomes), 1)
            self.assertIn(resolve_current_generation(data_dir).generation_id, {"generation-2", "generation-3"})

    def test_stale_base_and_old_refresh_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            stale = _candidate(data_dir, "stale-candidate")
            winner = _candidate(data_dir, "winner")
            finalize_candidate_generation(stale)
            publish_candidate_generation(winner)

            with self.assertRaises(GenerationConflictError) as stale_error:
                publish_candidate_generation(stale)
            self.assertEqual(stale_error.exception.code, "stale_base_generation")

            old_refresh = _candidate(data_dir, "old-refresh", "refresh")
            finalize_candidate_generation(old_refresh)
            with self.assertRaises(GenerationConflictError) as old_error:
                publish_candidate_generation(old_refresh)
            self.assertEqual(old_error.exception.code, "stale_generation")
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "winner")

    def test_refresh_publication_requires_a_byte_exact_continuous_history_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            healthy, _ = _ready_refresh_candidate(data_dir, "healthy-refresh")

            published = publish_candidate_generation(healthy)

            self.assertEqual(published.current.generation_id, "healthy-refresh")

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            candidate, current = _ready_refresh_candidate(data_dir, "wrong-previous")
            pointer_before = (data_dir / "current.json").read_bytes()
            catalog_path = candidate.path / "catalog/latest.json"
            catalog = _read(catalog_path)
            catalog["previousCapturedAt"] = None
            _write(catalog_path, catalog)
            _rehash_ready_artifact(candidate, "catalog/latest.json")

            with self.assertRaises(GenerationConflictError) as previous_error:
                publish_candidate_generation(candidate)

            self.assertEqual(
                previous_error.exception.code,
                "refresh_previous_capture_mismatch",
            )
            self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)
            self.assertEqual(resolve_current_generation(data_dir).generation_id, current.generation_id)

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            candidate, current = _ready_refresh_candidate(data_dir, "wrong-base-archive")
            pointer_before = (data_dir / "current.json").read_bytes()
            base_snapshot = _read(current.root / "snapshots/latest.json")
            archived_path = next(
                path
                for path in sorted((candidate.path / "snapshots/history").glob("*.json"))
                if _read(path).get("captured_at") == base_snapshot["captured_at"]
            )
            archived = _read(archived_path)
            repositories = archived["repositories"]
            assert isinstance(repositories, list) and isinstance(repositories[0], dict)
            repositories[0]["stars"] = int(repositories[0]["stars"]) + 1
            _write(archived_path, archived)
            archived_relative = archived_path.relative_to(candidate.path).as_posix()
            _rehash_ready_artifact(candidate, archived_relative)

            with self.assertRaises(GenerationConflictError) as archive_error:
                publish_candidate_generation(candidate)

            self.assertEqual(
                archive_error.exception.code,
                "refresh_base_snapshot_not_archived",
            )
            self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            candidate, current = _ready_refresh_candidate(data_dir, "changed-history")
            pointer_before = (data_dir / "current.json").read_bytes()
            existing_history = next(
                path for path in sorted((current.root / "snapshots/history").glob("*.json"))
            )
            relative = existing_history.relative_to(current.root)
            candidate_history_path = candidate.path / relative
            history_payload = _read(candidate_history_path)
            repositories = history_payload["repositories"]
            assert isinstance(repositories, list) and isinstance(repositories[0], dict)
            repositories[0]["stars"] = int(repositories[0]["stars"]) + 1
            _write(candidate_history_path, history_payload)
            _rehash_ready_artifact(candidate, relative.as_posix())

            with self.assertRaises(GenerationConflictError) as history_error:
                publish_candidate_generation(candidate)

            self.assertEqual(history_error.exception.code, "refresh_history_changed")
            self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            candidate, current = _ready_refresh_candidate(data_dir, "deleted-history")
            pointer_before = (data_dir / "current.json").read_bytes()
            existing_history = next(
                path for path in sorted((current.root / "snapshots/history").glob("*.json"))
            )
            relative = existing_history.relative_to(current.root).as_posix()
            (candidate.path / relative).unlink()
            manifest_path = candidate.path / "manifest.json"
            manifest = _read(manifest_path)
            manifest["artifacts"].remove(relative)
            manifest["hashes"].pop(relative)
            manifest["audit"]["validatedCount"] = int(
                manifest["audit"]["validatedCount"]
            ) - 1
            _write(manifest_path, manifest)

            with self.assertRaises(GenerationConflictError) as deletion_error:
                publish_candidate_generation(candidate)

            self.assertEqual(deletion_error.exception.code, "refresh_history_changed")
            self.assertEqual((data_dir / "current.json").read_bytes(), pointer_before)

    def test_current_page_inputs_and_growth_baseline_resolve_from_one_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))

            current, artifacts = resolve_current_artifacts(
                data_dir,
                (
                    "catalog/latest.json",
                    "signals/latest.json",
                    "queues/codex.json",
                    "snapshots/latest.json",
                ),
            )

            self.assertEqual(current.generation_id, "generation-1")
            self.assertTrue(all(path.is_relative_to(current.root) for path in artifacts.values()))
            queue = _read(artifacts["queues/codex.json"])
            for item in queue["items"]:
                self.assertTrue(
                    all(
                        value.startswith("data/generations/generation-1/")
                        for value in item["inputPaths"]
                    )
                )

    def test_manifest_integrity_and_unsafe_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            candidate = _candidate(data_dir, "tampered", "bootstrap")
            finalize_candidate_generation(candidate)
            catalog_path = candidate.path / "catalog/latest.json"
            catalog_path.write_bytes(catalog_path.read_bytes() + b" ")

            with self.assertRaises(CandidateGenerationError) as digest_error:
                publish_candidate_generation(candidate)
            self.assertEqual(digest_error.exception.code, "integrity_mismatch")
            self.assertFalse((data_dir / "current.json").exists())

            unsafe = _candidate(data_dir, "unsafe", "bootstrap")
            finalize_candidate_generation(unsafe)
            manifest_path = unsafe.path / "manifest.json"
            manifest = _read(manifest_path)
            manifest["artifacts"].append("../escape.json")
            manifest["hashes"]["../escape.json"] = "0" * 64
            _write(manifest_path, manifest)
            with self.assertRaises(GenerationProtocolError) as path_error:
                publish_candidate_generation(unsafe)
            self.assertEqual(path_error.exception.code, "unsafe_artifact_path")

    def test_manifest_digest_mismatch_is_a_structured_current_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            pointer_path = data_dir / "current.json"
            pointer = _read(pointer_path)
            pointer["manifestSha256"] = "0" * 64
            _write(pointer_path, pointer)

            with self.assertRaises(CurrentGenerationError) as raised:
                resolve_current_generation(data_dir)

            self.assertEqual(raised.exception.code, "manifest_digest_mismatch")
            self.assertEqual(raised.exception.generation_id, "generation-1")
            self.assertEqual(raised.exception.stage, "integrity")

    def test_pointer_and_manifest_reject_non_rfc3339_timezone_forms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            pointer_path = data_dir / "current.json"
            pointer = _read(pointer_path)
            pointer["publishedAt"] = str(pointer["publishedAt"]).replace("+00:00", "+0000")
            _write(pointer_path, pointer)

            with self.assertRaises(CurrentGenerationError) as pointer_error:
                resolve_current_generation(data_dir)
            self.assertEqual(pointer_error.exception.code, "invalid_timestamp")

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            published = publish_candidate_generation(
                _candidate(data_dir, "generation-1", "bootstrap")
            )
            manifest_path = published.current.root / "manifest.json"
            manifest = _read(manifest_path)
            manifest["createdAt"] = str(manifest["createdAt"]).replace("+00:00", "+0000")
            _write(manifest_path, manifest)
            pointer_path = data_dir / "current.json"
            pointer = _read(pointer_path)
            pointer["manifestSha256"] = hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest()
            _write(pointer_path, pointer)

            with self.assertRaises(CurrentGenerationError) as manifest_error:
                resolve_current_generation(data_dir)
            self.assertEqual(manifest_error.exception.code, "invalid_timestamp")

    def test_missing_manifest_is_a_structured_fail_closed_current_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            published = publish_candidate_generation(
                _candidate(data_dir, "generation-1", "bootstrap")
            )
            (published.current.root / "manifest.json").unlink()

            with self.assertRaises(CurrentGenerationError) as raised:
                resolve_current_generation(data_dir)

            self.assertEqual(raised.exception.code, "manifest_invalid")
            self.assertEqual(raised.exception.generation_id, "generation-1")

    def test_generation_containers_cannot_escape_through_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            _seed_legacy(data_dir)
            (data_dir / "generations").mkdir()
            outside = root / "outside-candidates"
            outside.mkdir()
            link = data_dir / "generations/.candidates"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")

            with self.assertRaises(GenerationProtocolError) as raised:
                _candidate(data_dir, "escaped", "bootstrap")

            self.assertEqual(raised.exception.code, "unsafe_symlink")
            self.assertFalse((outside / "escaped").exists())

    def test_broken_current_symlink_is_not_treated_as_absent_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            pointer = data_dir / "current.json"
            try:
                pointer.symlink_to(data_dir / "missing-pointer-target.json")
            except OSError as error:
                self.skipTest(f"file symlinks are unavailable: {error}")

            with self.assertRaises(CurrentGenerationError) as raised:
                resolve_current_generation(data_dir)

            self.assertEqual(raised.exception.code, "unsafe_symlink")

    def test_current_resolver_can_skip_only_semantic_audit_not_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))

            with patch(
                "pipeline.generations._audit_generation",
                side_effect=AssertionError("audit must be skipped"),
            ):
                resolved = resolve_current_generation(data_dir, verify_audit=False)
            self.assertEqual(resolved.generation_id, "generation-1")

            root = resolved.root
            catalog_path = root / "catalog/latest.json"
            catalog = _read(catalog_path)
            catalog["projects"] = "not-an-array"
            _write(catalog_path, catalog)
            manifest_path = root / "manifest.json"
            manifest = _read(manifest_path)
            manifest["hashes"]["catalog/latest.json"] = hashlib.sha256(
                catalog_path.read_bytes()
            ).hexdigest()
            _write(manifest_path, manifest)
            pointer_path = data_dir / "current.json"
            pointer = _read(pointer_path)
            pointer["manifestSha256"] = hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest()
            _write(pointer_path, pointer)

            with self.assertRaises(CurrentGenerationError) as schema_error:
                resolve_current_generation(data_dir, verify_audit=False)
            self.assertEqual(schema_error.exception.code, "schema_validation_failed")

    def test_flat_staging_only_overlays_strictly_newer_versioned_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            current = resolve_current_generation(data_dir)

            cases: list[tuple[Path, str]] = []
            analysis_path = next(
                path
                for path in sorted((current.root / "analysis").glob("*.json"))
                if _read(path).get("analyzed_at")
            )
            cases.append((analysis_path.relative_to(current.root), "analyzed_at"))
            enrichment_path = next(
                path
                for path in sorted((current.root / "enrichment").glob("*.json"))
                if _read(path).get("schemaVersion") == 1 and _read(path).get("analyzedAt")
            )
            cases.append((enrichment_path.relative_to(current.root), "analyzedAt"))
            cases.append((Path("signals/enrichment.json"), "generatedAt"))

            for index, (relative, time_field) in enumerate(cases):
                with self.subTest(relative=relative.as_posix()):
                    target_payload = _read(current.root / relative)
                    flat_path = data_dir / relative
                    stale_payload = dict(target_payload)
                    stale_payload[time_field] = target_payload[time_field]
                    _write(flat_path, stale_payload)
                    stale = _candidate(data_dir, f"stale-overlay-{index}")
                    self.assertEqual(_read(stale.path / relative), target_payload)

                    legacy_payload = dict(target_payload)
                    legacy_payload["schemaVersion"] = 0
                    legacy_payload.pop(time_field, None)
                    _write(flat_path, legacy_payload)
                    legacy = _candidate(data_dir, f"legacy-overlay-{index}")
                    self.assertEqual(_read(legacy.path / relative), target_payload)

                    newer_payload = dict(target_payload)
                    newer_payload["schemaVersion"] = 1
                    newer_payload[time_field] = "2099-01-01T00:00:00+00:00"
                    _write(flat_path, newer_payload)
                    newer = _candidate(data_dir, f"newer-overlay-{index}")
                    self.assertEqual(
                        _read(newer.path / relative)[time_field],
                        "2099-01-01T00:00:00+00:00",
                    )

    def test_explicit_rollback_revalidates_and_atomically_repoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            publish_candidate_generation(_candidate(data_dir, "generation-1", "bootstrap"))
            publish_candidate_generation(_candidate(data_dir, "generation-2"))

            pointer_before_unsafe_id = (data_dir / "current.json").read_bytes()
            with self.assertRaises(GenerationProtocolError) as unsafe_id:
                rollback_to_generation(data_dir, "../escape")
            self.assertEqual(unsafe_id.exception.code, "invalid_generation_id")
            self.assertEqual(
                (data_dir / "current.json").read_bytes(),
                pointer_before_unsafe_id,
            )

            current_pointer = _read(data_dir / "current.json")
            same_time = datetime.fromisoformat(
                str(current_pointer["publishedAt"]).replace("Z", "+00:00")
            )
            pointer_before_stale_time = (data_dir / "current.json").read_bytes()
            with self.assertRaises(GenerationConflictError) as stale_time:
                rollback_to_generation(
                    data_dir,
                    "generation-1",
                    published_at=same_time,
                )
            self.assertEqual(stale_time.exception.code, "stale_publication_time")
            self.assertEqual(
                (data_dir / "current.json").read_bytes(),
                pointer_before_stale_time,
            )
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "generation-2")

            result = rollback_to_generation(data_dir, "generation-1")

            self.assertTrue(result.rolled_back)
            self.assertEqual(result.current.generation_id, "generation-1")
            pointer = _read(data_dir / "current.json")
            self.assertEqual(pointer["generationId"], "generation-1")
            self.assertEqual(pointer["previousGenerationId"], "generation-2")

    def test_recovery_rollback_only_preserves_trusted_pointer_times(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            first, _ = _publish_two_generations(data_dir)
            pointer_path = data_dir / "current.json"
            broken_pointer = _read(pointer_path)
            broken_pointer["manifestSha256"] = "0" * 64
            recovery_now = datetime(2030, 1, 1, tzinfo=timezone.utc)
            within_drift = recovery_now + timedelta(minutes=2)
            drift_boundary = recovery_now + generation_module.RECOVERY_POINTER_FUTURE_DRIFT
            cases = (
                (
                    "normal-past",
                    recovery_now - timedelta(days=1),
                    recovery_now,
                    True,
                ),
                (
                    "small-future",
                    within_drift,
                    within_drift + timedelta(microseconds=1),
                    True,
                ),
                (
                    "drift-boundary",
                    drift_boundary,
                    drift_boundary + timedelta(microseconds=1),
                    True,
                ),
                (
                    "outside-drift",
                    drift_boundary + timedelta(microseconds=1),
                    recovery_now,
                    False,
                ),
                (
                    "far-future",
                    datetime(2099, 1, 1, tzinfo=timezone.utc),
                    recovery_now,
                    False,
                ),
            )
            real_utc_timestamp = generation_module._utc_timestamp

            for label, old_time, expected_time, must_advance in cases:
                with self.subTest(case=label):
                    broken_pointer["publishedAt"] = old_time.isoformat()
                    _write(pointer_path, broken_pointer)

                    with patch(
                        "pipeline.generations._utc_timestamp",
                        side_effect=lambda value=None: real_utc_timestamp(
                            recovery_now if value is None else value
                        ),
                    ):
                        result = rollback_to_generation(data_dir, "generation-1")

                    self.assertEqual(result.current.generation_id, "generation-1")
                    recovered = _read(pointer_path)
                    recovered_time = datetime.fromisoformat(
                        str(recovered["publishedAt"]).replace("Z", "+00:00")
                    )
                    self.assertEqual(recovered_time, expected_time)
                    if must_advance:
                        self.assertGreater(recovered_time, old_time)
                    else:
                        self.assertLess(recovered_time, old_time)
                    self.assertEqual(recovered["previousGenerationId"], "generation-2")
                    self.assertEqual(
                        recovered["manifestSha256"],
                        hashlib.sha256((first.root / "manifest.json").read_bytes()).hexdigest(),
                    )
                    self.assertEqual(
                        resolve_current_generation(data_dir).generation_id,
                        "generation-1",
                    )

            max_time = datetime.max.replace(tzinfo=timezone.utc)
            overflow_recovery_now = max_time - timedelta(minutes=1)
            broken_pointer["publishedAt"] = max_time.isoformat()
            _write(pointer_path, broken_pointer)
            with patch(
                "pipeline.generations._utc_timestamp",
                side_effect=lambda value=None: real_utc_timestamp(
                    overflow_recovery_now if value is None else value
                ),
            ):
                result = rollback_to_generation(data_dir, "generation-1")

            self.assertEqual(result.current.generation_id, "generation-1")
            recovered = _read(pointer_path)
            self.assertEqual(recovered["publishedAt"], overflow_recovery_now.isoformat())
            self.assertEqual(recovered["previousGenerationId"], "generation-2")
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "generation-1")

    def test_far_future_recovery_allows_immediate_derive_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _publish_two_generations(data_dir)
            pointer_path = data_dir / "current.json"
            pointer = _read(pointer_path)
            pointer["publishedAt"] = "2099-01-01T00:00:00+00:00"
            pointer["manifestSha256"] = "0" * 64
            _write(pointer_path, pointer)
            recovery_now = datetime(2030, 1, 1, tzinfo=timezone.utc)
            real_utc_timestamp = generation_module._utc_timestamp

            with patch(
                "pipeline.generations._utc_timestamp",
                side_effect=lambda value=None: real_utc_timestamp(
                    recovery_now if value is None else value
                ),
            ):
                rollback_to_generation(data_dir, "generation-1")

            recovered = _read(pointer_path)
            self.assertEqual(recovered["publishedAt"], recovery_now.isoformat())
            publisher = _candidate(data_dir, "generation-3")
            finalize_candidate_generation(publisher)
            next_publication_time = recovery_now + timedelta(seconds=1)
            with patch(
                "pipeline.generations._utc_timestamp",
                side_effect=lambda value=None: real_utc_timestamp(
                    next_publication_time if value is None else value
                ),
            ):
                published = publish_candidate_generation(publisher)

            self.assertEqual(published.current.generation_id, "generation-3")
            self.assertEqual(
                _read(pointer_path)["publishedAt"],
                next_publication_time.isoformat(),
            )
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "generation-3")

    def test_recovery_rollback_repairs_missing_current_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _, second = _publish_two_generations(data_dir)
            (second.root / "manifest.json").unlink()

            rollback_to_generation(data_dir, "generation-1")

            pointer = _read(data_dir / "current.json")
            self.assertEqual(pointer["generationId"], "generation-1")
            self.assertEqual(pointer["previousGenerationId"], "generation-2")
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "generation-1")

    def test_recovery_rollback_repairs_missing_current_generation_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _, second = _publish_two_generations(data_dir)
            shutil.rmtree(second.root)

            rollback_to_generation(data_dir, "generation-1")

            pointer = _read(data_dir / "current.json")
            self.assertEqual(pointer["generationId"], "generation-1")
            self.assertEqual(pointer["previousGenerationId"], "generation-2")
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "generation-1")

    def test_recovery_rollback_repairs_tampered_current_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _, second = _publish_two_generations(data_dir)
            artifact = second.root / "catalog/latest.json"
            artifact.write_bytes(artifact.read_bytes() + b" ")

            rollback_to_generation(data_dir, "generation-1")

            pointer = _read(data_dir / "current.json")
            self.assertEqual(pointer["generationId"], "generation-1")
            self.assertEqual(pointer["previousGenerationId"], "generation-2")
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "generation-1")

    def test_recovery_rollback_repairs_invalid_json_without_flat_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _publish_two_generations(data_dir)
            pointer_path = data_dir / "current.json"
            pointer_path.write_bytes(b"{not-json")
            before = datetime.now(timezone.utc)

            with patch(
                "pipeline.generations._legacy_generation",
                side_effect=AssertionError("rollback must not read flat data"),
            ):
                rollback_to_generation(data_dir, "generation-1")
            after = datetime.now(timezone.utc)

            pointer = _read(pointer_path)
            recovered_time = datetime.fromisoformat(
                str(pointer["publishedAt"]).replace("Z", "+00:00")
            )
            self.assertEqual(pointer["generationId"], "generation-1")
            self.assertIsNone(pointer["previousGenerationId"])
            self.assertGreaterEqual(recovered_time, before)
            self.assertLessEqual(recovered_time, after)
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "generation-1")

    def test_recovery_rollback_extracts_safe_pointer_fields_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _publish_two_generations(data_dir)
            pointer_path = data_dir / "current.json"
            pointer = _read(pointer_path)
            pointer["publishedAt"] = "2099-01-01T00:00:00"
            pointer["manifestSha256"] = "0" * 64
            _write(pointer_path, pointer)
            before = datetime.now(timezone.utc)

            rollback_to_generation(data_dir, "generation-1")

            after = datetime.now(timezone.utc)
            recovered = _read(pointer_path)
            recovered_time = datetime.fromisoformat(
                str(recovered["publishedAt"]).replace("Z", "+00:00")
            )
            self.assertEqual(recovered["previousGenerationId"], "generation-2")
            self.assertGreaterEqual(recovered_time, before)
            self.assertLessEqual(recovered_time, after)
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "generation-1")

    def test_recovery_rollback_never_reads_or_preserves_current_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            _publish_two_generations(data_dir)
            pointer_path = data_dir / "current.json"
            outside_pointer = root / "outside-current.json"
            _write(
                outside_pointer,
                {
                    "schemaVersion": 1,
                    "generationId": "attacker-generation",
                    "publishedAt": "2099-01-01T00:00:00+00:00",
                    "previousGenerationId": None,
                    "manifestSha256": "0" * 64,
                },
            )
            outside_bytes = outside_pointer.read_bytes()
            pointer_path.unlink()
            try:
                pointer_path.symlink_to(outside_pointer)
            except OSError as error:
                self.skipTest(f"file symlinks are unavailable: {error}")

            rollback_to_generation(data_dir, "generation-1")

            self.assertFalse(pointer_path.is_symlink())
            self.assertTrue(pointer_path.is_file())
            self.assertEqual(outside_pointer.read_bytes(), outside_bytes)
            pointer = _read(pointer_path)
            self.assertIsNone(pointer["previousGenerationId"])
            self.assertLess(
                datetime.fromisoformat(str(pointer["publishedAt"]).replace("Z", "+00:00")),
                datetime(2099, 1, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "generation-1")

    def test_recovery_rollback_portably_ignores_linked_pointer_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _publish_two_generations(data_dir)
            pointer_path = data_dir / "current.json"
            _write(
                pointer_path,
                {
                    "schemaVersion": 1,
                    "generationId": "attacker-generation",
                    "publishedAt": "2099-01-01T00:00:00+00:00",
                    "previousGenerationId": None,
                    "manifestSha256": "0" * 64,
                },
            )
            original_link_check = generation_module._is_filesystem_link
            original_read_object = generation_module._read_object

            def report_pointer_as_link(path: Path) -> bool:
                return Path(path).name == "current.json" or original_link_check(path)

            def reject_pointer_read(path: Path, *, code: str, stage: str):
                if Path(path).name == "current.json":
                    raise AssertionError("a linked current pointer must never be read")
                return original_read_object(path, code=code, stage=stage)

            with patch(
                "pipeline.generations._is_filesystem_link",
                side_effect=report_pointer_as_link,
            ), patch(
                "pipeline.generations._read_object",
                side_effect=reject_pointer_read,
            ):
                rollback_to_generation(data_dir, "generation-1")

            pointer = _read(pointer_path)
            self.assertIsNone(pointer["previousGenerationId"])
            self.assertLess(
                datetime.fromisoformat(str(pointer["publishedAt"]).replace("Z", "+00:00")),
                datetime(2099, 1, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "generation-1")

    def test_recovery_rollback_rejects_damaged_target_without_touching_pointer(self) -> None:
        cases = (
            ("integrity", "integrity_mismatch"),
            ("schema", "schema_validation_failed"),
            ("audit", "audit_failed"),
        )
        for corruption, expected_code in cases:
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as temporary:
                data_dir = Path(temporary) / "data"
                first, _ = _publish_two_generations(data_dir)
                catalog_path = first.root / "catalog/latest.json"
                if corruption == "integrity":
                    catalog_path.write_bytes(catalog_path.read_bytes() + b" ")
                else:
                    catalog = _read(catalog_path)
                    if corruption == "schema":
                        catalog["projects"] = "not-an-array"
                    else:
                        projects = catalog["projects"]
                        assert isinstance(projects, list) and isinstance(projects[0], dict)
                        projects[0]["stars"] = int(projects[0]["stars"]) + 1
                    _write(catalog_path, catalog)
                    manifest_path = first.root / "manifest.json"
                    manifest = _read(manifest_path)
                    manifest["hashes"]["catalog/latest.json"] = hashlib.sha256(
                        catalog_path.read_bytes()
                    ).hexdigest()
                    _write(manifest_path, manifest)

                pointer_path = data_dir / "current.json"
                pointer_path.write_bytes(b"{broken-current")
                pointer_before = pointer_path.read_bytes()
                with patch(
                    "pipeline.generations._atomic_write_json",
                    wraps=generation_module._atomic_write_json,
                ) as pointer_writer:
                    with self.assertRaises(CandidateGenerationError) as raised:
                        rollback_to_generation(data_dir, "generation-1")

                self.assertEqual(raised.exception.code, expected_code)
                self.assertEqual(raised.exception.generation_id, "generation-1")
                self.assertEqual(raised.exception.as_dict()["code"], expected_code)
                pointer_writer.assert_not_called()
                self.assertEqual(pointer_path.read_bytes(), pointer_before)

    def test_recovery_rollback_rechecks_target_immediately_before_pointer_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            first, _ = _publish_two_generations(data_dir)
            pointer_path = data_dir / "current.json"
            pointer_path.write_bytes(b"{broken-current")
            pointer_before = pointer_path.read_bytes()
            artifact = first.root / "catalog/latest.json"
            original_metadata_reader = generation_module._read_recovery_pointer_metadata

            def tamper_after_full_validation(pointer: Path, canonical: Path):
                metadata = original_metadata_reader(pointer, canonical)
                artifact.write_bytes(artifact.read_bytes() + b" ")
                return metadata

            with patch(
                "pipeline.generations._read_recovery_pointer_metadata",
                side_effect=tamper_after_full_validation,
            ):
                with self.assertRaises(CandidateGenerationError) as raised:
                    rollback_to_generation(data_dir, "generation-1")

            self.assertEqual(raised.exception.code, "integrity_mismatch")
            self.assertEqual(pointer_path.read_bytes(), pointer_before)

    def test_recovery_rollback_serializes_with_a_normal_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _publish_two_generations(data_dir)
            publisher = _candidate(data_dir, "generation-3")
            finalize_candidate_generation(publisher)
            pointer_path = data_dir / "current.json"
            pointer = _read(pointer_path)
            pointer["manifestSha256"] = "0" * 64
            _write(pointer_path, pointer)

            entered_target_validation = threading.Event()
            release_target_validation = threading.Event()
            rollback_thread = threading.local()
            original_verify = generation_module._verify_manifest_integrity

            def blocking_verify(root: Path, generation_id: str, *, verify_audit: bool):
                if (
                    getattr(rollback_thread, "active", False)
                    and generation_id == "generation-1"
                    and verify_audit
                ):
                    entered_target_validation.set()
                    self.assertTrue(release_target_validation.wait(10))
                return original_verify(root, generation_id, verify_audit=verify_audit)

            def run_rollback():
                rollback_thread.active = True
                return rollback_to_generation(data_dir, "generation-1")

            with patch(
                "pipeline.generations._verify_manifest_integrity",
                side_effect=blocking_verify,
            ), ThreadPoolExecutor(max_workers=2) as executor:
                rollback_future = executor.submit(run_rollback)
                self.assertTrue(entered_target_validation.wait(10))
                publish_future = executor.submit(publish_candidate_generation, publisher)
                try:
                    with self.assertRaises(FutureTimeoutError):
                        publish_future.result(timeout=0.2)
                finally:
                    release_target_validation.set()
                rolled_back = rollback_future.result(timeout=30)
                with self.assertRaises(GenerationConflictError) as publish_error:
                    publish_future.result(timeout=30)

            self.assertEqual(rolled_back.current.generation_id, "generation-1")
            self.assertEqual(publish_error.exception.code, "stale_base_generation")
            self.assertEqual(_read(pointer_path)["generationId"], "generation-1")
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "generation-1")

    def test_recovered_generation_is_readable_by_every_published_data_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _publish_two_generations(data_dir)
            (data_dir / "current.json").write_bytes(b"{not-json")

            rollback_to_generation(data_dir, "generation-1")

            self.assertEqual(resolve_current_generation(data_dir).generation_id, "generation-1")

            for module in ("pipeline.schema_validation", "pipeline.audit_data"):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        module,
                        "--data-dir",
                        str(data_dir),
                    ],
                    cwd=REPOSITORY_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                report = json.loads(completed.stdout)
                self.assertNotEqual(report["status"], "failed")
                self.assertEqual(report["errorCount"], 0)
                if module == "pipeline.audit_data":
                    self.assertEqual(report["generationId"], "generation-1")

            node = shutil.which("node")
            self.assertIsNotNone(node, "Node.js is required by the repository contract")
            loader = (REPOSITORY_ROOT / "app/published-data-loader.mjs").as_uri()
            script = (
                f"import {{ loadPublishedBundle }} from {json.dumps(loader)};"
                "const bundle = loadPublishedBundle(process.argv[1]);"
                "process.stdout.write(bundle.generationId);"
            )
            completed = subprocess.run(
                [str(node), "--input-type=module", "--eval", script, str(data_dir)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "generation-1")

    def test_cli_bootstrap_status_publish_and_rollback_use_the_same_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _seed_legacy(data_dir)
            output = io.StringIO()
            errors = io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                exit_code = main(
                    [
                        "--data-dir",
                        str(data_dir),
                        "bootstrap",
                        "--generation-id",
                        "cli-generation-1",
                    ]
                )
            self.assertEqual(exit_code, 0, errors.getvalue())
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "cli-generation-1")

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--data-dir", str(data_dir), "status"]), 0)
            status = strict_json_loads(output.getvalue())
            self.assertEqual(status["generationId"], "cli-generation-1")

            _candidate(data_dir, "cli-generation-2")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "--data-dir",
                            str(data_dir),
                            "publish",
                            "cli-generation-2",
                        ]
                    ),
                    0,
                )
            published = strict_json_loads(output.getvalue())
            self.assertTrue(published["publicationRetried"])
            self.assertEqual(published["generationId"], "cli-generation-2")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "--data-dir",
                            str(data_dir),
                            "rollback",
                            "cli-generation-1",
                        ]
                    ),
                    0,
                )
            self.assertEqual(resolve_current_generation(data_dir).generation_id, "cli-generation-1")


if __name__ == "__main__":
    unittest.main()
