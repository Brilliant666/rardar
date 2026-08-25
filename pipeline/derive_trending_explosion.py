"""Publish one audited 24-hour GitHub explosion fact generation.

The command consumes only immutable TrendingCaptureBundle files.  It performs
no network work and keeps source preparation outside the short generation
publication lock; the existing generation compare-and-swap is the sole writer
of ``current.json``.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.generations import (
    CandidateGeneration,
    GenerationProtocolError,
    create_candidate_generation,
    fail_candidate_generation,
    finalize_candidate_generation,
    publish_candidate_generation,
    resolve_current_generation,
)
from pipeline.schema_validation import strict_json_loads
from pipeline.stable_read import StableReadError, stable_read
from pipeline.trending_explosion import (
    EXPLOSION_PATH,
    ExplosionSources,
    TrendingExplosionError,
    assert_derive_inputs_unchanged,
    build_trending_explosion_artifact,
    explosion_source_signature,
    load_explosion_sources,
    parse_generated_at,
    parse_window_end,
    validate_explosion_artifact,
    write_candidate_explosion,
)
from pipeline.trending_observations import capture_path_for_scheduled_at


def _read_current_artifact(current_root: Path) -> dict[str, Any] | None:
    path = current_root / EXPLOSION_PATH
    if not os.path.lexists(path):
        return None
    try:
        snapshot = stable_read(path)
        parsed = strict_json_loads(snapshot.content.decode("utf-8", errors="strict"))
        return validate_explosion_artifact(parsed)
    except (
        OSError,
        StableReadError,
        TrendingExplosionError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        raise TrendingExplosionError(
            "explosion_current_artifact_invalid",
            f"current explosion artifact is not trustworthy: {error}",
            stage="current",
        ) from None


def _source_signature(sources: ExplosionSources) -> tuple[str, str | None, str, str | None]:
    baseline = sources.baseline
    return (
        str(sources.current.payload["digest"]["value"]),
        str(baseline.payload["digest"]["value"]) if baseline is not None else None,
        sources.current.file_sha256,
        baseline.file_sha256 if baseline is not None else None,
    )


def _summary(
    *,
    state: str,
    generation_id: str | None,
    base_generation_id: str | None,
    artifact: dict[str, Any] | None,
    candidate_path: Path | None,
    published: bool,
    current_changed: bool,
) -> dict[str, Any]:
    window = artifact.get("window", {}) if artifact else {}
    source_captures = artifact.get("sourceCaptures", {}) if artifact else {}
    coverage = artifact.get("coverage", {}) if artifact else {}
    current_source = source_captures.get("current") or {}
    baseline_source = source_captures.get("baseline") or {}
    return {
        "state": state,
        "generationId": generation_id,
        "baseGenerationId": base_generation_id,
        "windowState": window.get("state"),
        "windowStartedAt": window.get("startedAt"),
        "windowEndedAt": window.get("endedAt"),
        "currentCaptureId": current_source.get("captureId"),
        "baselineCaptureId": baseline_source.get("captureId"),
        "coverageState": coverage.get("state"),
        "exactCount": len(artifact.get("exactRanked", [])) if artifact else 0,
        "pendingCount": len(artifact.get("pendingRanked", [])) if artifact else 0,
        "conflictCount": len(artifact.get("conflicts", [])) if artifact else 0,
        "candidatePath": str(candidate_path) if candidate_path is not None else None,
        "published": published,
        "currentChanged": current_changed,
    }


def _already_derived_summary(
    current_generation_id: str | None,
    artifact: dict[str, Any],
) -> dict[str, Any]:
    return _summary(
        state="already_derived",
        generation_id=current_generation_id,
        base_generation_id=current_generation_id,
        artifact=artifact,
        candidate_path=None,
        published=False,
        current_changed=False,
    )


def _check_existing_window(
    *,
    data_dir: Path,
    current_generation_id: str | None,
    existing: dict[str, Any] | None,
    requested_end: datetime,
) -> tuple[dict[str, Any] | None, ExplosionSources | None]:
    """Enforce temporal idempotency before any candidate is created.

    If raw endpoint bytes have aged out after a successful publication, the
    retained, fully audited generation remains sufficient to answer a replay
    with ``already_derived``.  When the raw endpoint is still present, both its
    payload and byte digest must match the frozen provenance.
    """

    if existing is None:
        return None, None
    existing_end = parse_window_end(existing["window"]["endedAt"])
    if existing_end > requested_end:
        raise TrendingExplosionError(
            "stale_explosion_window",
            "requested explosion window is older than the current artifact",
            stage="idempotency",
        )
    if existing_end < requested_end:
        return None, None

    current_capture_path = capture_path_for_scheduled_at(data_dir, requested_end)
    if not os.path.lexists(current_capture_path):
        return _already_derived_summary(current_generation_id, existing), None

    sources = load_explosion_sources(data_dir, requested_end)
    expected = explosion_source_signature(existing)
    actual = _source_signature(sources)

    # A retained baseline may have aged out independently of the current raw
    # endpoint.  Its frozen bytes remain authoritative, but every still-present
    # endpoint must match exactly.  A newly appearing baseline for an artifact
    # that previously had none is a source conflict, not an implicit rewrite.
    baseline_aged_out = expected[1] is not None and actual[1] is None
    current_matches = expected[0] == actual[0] and expected[2] == actual[2]
    baseline_matches = (
        baseline_aged_out
        or (expected[1] == actual[1] and expected[3] == actual[3])
    )
    if not current_matches or not baseline_matches:
        raise TrendingExplosionError(
            "explosion_source_conflict",
            "the same formal window now resolves to different endpoint source bytes",
            stage="idempotency",
        )
    return _already_derived_summary(current_generation_id, existing), sources


def derive_trending_explosion(
    data_dir: Path,
    window_end: str | datetime,
    *,
    dry_run: bool = False,
    generation_id: str | None = None,
    generated_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Derive, validate, and optionally publish one explosion generation."""

    canonical = Path(data_dir).expanduser().resolve()
    ended = parse_window_end(window_end)
    generated = parse_generated_at(generated_at)
    current = resolve_current_generation(canonical)
    existing = _read_current_artifact(current.root)
    already, prepared_sources = _check_existing_window(
        data_dir=canonical,
        current_generation_id=current.generation_id,
        existing=existing,
        requested_end=ended,
    )
    if already is not None:
        return already

    sources = prepared_sources or load_explosion_sources(canonical, ended)
    if dry_run:
        identifier = generation_id or (
            "dry-run-"
            + ended.strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + sources.current.file_sha256[:12]
        )
        artifact = build_trending_explosion_artifact(
            generation_id=identifier,
            window_end=ended,
            generated_at=generated,
            sources=sources,
        )
        return _summary(
            state="dry_run",
            generation_id=identifier,
            base_generation_id=current.generation_id,
            artifact=artifact,
            candidate_path=None,
            published=False,
            current_changed=False,
        )

    candidate: CandidateGeneration | None = None
    try:
        candidate = create_candidate_generation(
            canonical,
            "derive",
            generation_id=generation_id,
            created_at=generated,
            overlay_flat_staging=False,
        )
        if candidate.base_generation_id != current.generation_id:
            raise TrendingExplosionError(
                "stale_base_generation",
                "current generation changed before the explosion candidate was created",
                stage="conflict",
            )
        artifact = build_trending_explosion_artifact(
            generation_id=candidate.generation_id,
            window_end=ended,
            generated_at=generated,
            sources=sources,
        )
        write_candidate_explosion(candidate.path, artifact, sources)
        assert_derive_inputs_unchanged(current.root, candidate.path)
        finalize_candidate_generation(candidate)
    except Exception as error:
        if candidate is not None:
            try:
                fail_candidate_generation(candidate, "build", str(error))
            except GenerationProtocolError:
                pass
        raise

    publication = publish_candidate_generation(candidate, published_at=generated)
    published_generation = publication.current
    return _summary(
        state="derived",
        generation_id=published_generation.generation_id,
        base_generation_id=candidate.base_generation_id,
        artifact=artifact,
        candidate_path=candidate.path,
        published=True,
        current_changed=published_generation.generation_id != current.generation_id,
    )


def _blocked_summary(error: BaseException) -> dict[str, Any]:
    code = getattr(error, "code", "explosion_derivation_failed")
    stage = getattr(error, "stage", None) or "derive"
    generation_id = getattr(error, "generation_id", None)
    return {
        "state": "blocked",
        "generationId": generation_id,
        "baseGenerationId": None,
        "windowState": None,
        "windowStartedAt": None,
        "windowEndedAt": None,
        "currentCaptureId": None,
        "baselineCaptureId": None,
        "coverageState": None,
        "exactCount": 0,
        "pendingCount": 0,
        "conflictCount": 0,
        "candidatePath": None,
        "published": False,
        "currentChanged": False,
        "errorCode": str(code),
        "errorStage": str(stage),
        "error": str(error)[:1000],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Derive an audited 24-hour GitHub explosion generation"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--generation-id")
    parser.add_argument("--generated-at")
    arguments = parser.parse_args()
    try:
        result = derive_trending_explosion(
            arguments.data_dir,
            arguments.window_end,
            dry_run=arguments.dry_run,
            generation_id=arguments.generation_id,
            generated_at=arguments.generated_at,
        )
    except (GenerationProtocolError, TrendingExplosionError, OSError, ValueError) as error:
        result = _blocked_summary(error)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
