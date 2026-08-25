"""Audited, self-contained 24-hour GitHub explosion facts.

The observation ledger remains append-only and external to generations.  This
module stable-reads the bounded endpoint/partial captures, derives only
mechanical facts, and freezes every source byte needed to audit the result in a
retained generation.  It never calls GitHub, runs AI, or mutates the raw store.
"""

from __future__ import annotations

import copy
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from pipeline.schema_validation import (
    ArtifactKind,
    ArtifactValidationError,
    require_valid,
    strict_json_dumps,
    strict_json_loads,
)
from pipeline.stable_read import StableReadError, stable_read
from pipeline.trending_observations import (
    RETENTION_DAYS,
    SCHEDULE_TIMEZONE,
    WINDOW_TOLERANCE_SECONDS,
    TrendingObservationError,
    capture_id_for_scheduled_at,
    capture_path_for_scheduled_at,
    load_capture,
    parse_timestamp,
    validate_capture_bundle,
)


SCHEMA_VERSION = 1
POLICY_VERSION = "trending-explosion-v1"
WINDOW_HOURS = 24
CADENCE_HOURS = 2
EXACT_LIMIT = 500
PENDING_LIMIT = 500
EXPLOSION_PATH = "trending/explosion.json"
CURRENT_SOURCE_PATH = "trending/sources/current.json"
BASELINE_SOURCE_PATH = "trending/sources/baseline.json"
COVERAGE_WITNESS_PATH = "trending/sources/coverage-witness.json"
GENERATION_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"
)
AI_FIELDS = {
    "summaryZh",
    "coreCapabilities",
    "whyTrendingHypothesis",
    "confidence",
    "model",
    "reasoningEffort",
    "reuseRecommendation",
    "engineeringReadiness",
}


class TrendingExplosionError(RuntimeError):
    """A stable, bounded error from explosion derivation or audit."""

    def __init__(self, code: str, message: str, *, stage: str = "derive") -> None:
        self.code = code
        self.stage = stage
        super().__init__(message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "stage": self.stage, "error": str(self)}


@dataclass(frozen=True)
class CaptureSource:
    """One stable-read capture and the exact bytes that must be frozen."""

    path: Path
    original_observation_path: str
    content: bytes
    file_sha256: str
    payload: dict[str, Any]

    @property
    def scheduled_at(self) -> datetime:
        return parse_timestamp(self.payload["scheduledAt"], field="scheduledAt")

    @property
    def captured_at(self) -> datetime:
        return parse_timestamp(self.payload["capturedAt"], field="capturedAt")


@dataclass(frozen=True)
class ExplosionSources:
    current: CaptureSource
    baseline: CaptureSource | None
    partial: tuple[CaptureSource, ...]
    coverage_witness: CaptureSource | None
    window_state: str


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TrendingExplosionError(
            "explosion_timezone_required",
            f"{field} must include an explicit timezone",
            stage="contract",
        )
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value, field="timestamp").isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def parse_window_end(value: str | datetime) -> datetime:
    """Validate the formal 08:00 Asia/Shanghai fixed two-hour publication phase."""

    if isinstance(value, datetime):
        parsed = _utc(value, field="window_end")
    else:
        try:
            parsed = parse_timestamp(value, field="window_end")
        except TrendingObservationError as error:
            raise TrendingExplosionError(
                "explosion_invalid_window_end", str(error), stage="contract"
            ) from None
    local = parsed.astimezone(ZoneInfo(SCHEDULE_TIMEZONE))
    if (
        local.hour != 8
        or local.minute != 0
        or local.second != 0
        or local.microsecond != 0
    ):
        raise TrendingExplosionError(
            "explosion_invalid_window_end",
            "window_end must be exactly 08:00 Asia/Shanghai on a fixed two-hour phase",
            stage="contract",
        )
    return parsed


def parse_generated_at(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return _utc(value, field="generated_at")
    try:
        return parse_timestamp(value, field="generated_at")
    except TrendingObservationError as error:
        raise TrendingExplosionError(
            "explosion_invalid_generated_at", str(error), stage="contract"
        ) from None


def _original_observation_path(scheduled_at: datetime) -> str:
    scheduled = _utc(scheduled_at, field="scheduledAt")
    capture_id = capture_id_for_scheduled_at(scheduled)
    return (
        "observations/trending/v1/captures/"
        f"{scheduled:%Y/%m/%d}/{capture_id}.json"
    )


def _load_source_capture(data_dir: Path, scheduled_at: datetime) -> CaptureSource | None:
    """Return semantics and bytes from one no-follow, stable source read.

    ``load_capture`` first validates every existing path component.  The second
    stable read is then parsed and validated again, and its exact bytes—not a
    serialization of the parsed object—become the generation source copy.
    """

    target = capture_path_for_scheduled_at(data_dir, scheduled_at)
    if not os.path.lexists(target):
        return None
    try:
        load_capture(target)
        snapshot = stable_read(target)
        payload = strict_json_loads(snapshot.content.decode("utf-8", errors="strict"))
        validated = validate_capture_bundle(
            payload,
            expected_capture_id=capture_id_for_scheduled_at(scheduled_at),
            expected_path=target,
        )
    except (
        ArtifactValidationError,
        OSError,
        StableReadError,
        TrendingObservationError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        raise TrendingExplosionError(
            "explosion_source_capture_invalid",
            f"source capture is not trustworthy for {capture_id_for_scheduled_at(scheduled_at)}: {error}",
            stage="source",
        ) from None
    actual_schedule = parse_timestamp(validated["scheduledAt"], field="scheduledAt")
    if actual_schedule != _utc(scheduled_at, field="scheduledAt"):
        raise TrendingExplosionError(
            "explosion_source_capture_mismatch",
            "source capture scheduledAt does not match its requested phase",
            stage="source",
        )
    return CaptureSource(
        path=target,
        original_observation_path=_original_observation_path(actual_schedule),
        content=snapshot.content,
        file_sha256=snapshot.sha256,
        payload=validated,
    )


def _partial_sources(data_dir: Path, window_start: datetime, window_end: datetime) -> tuple[CaptureSource, ...]:
    sources: list[CaptureSource] = []
    scheduled = window_start + timedelta(hours=CADENCE_HOURS)
    while scheduled < window_end:
        try:
            source = _load_source_capture(data_dir, scheduled)
        except TrendingExplosionError as error:
            raise TrendingExplosionError(
                "explosion_partial_capture_invalid",
                str(error),
                stage="source",
            ) from None
        if source is not None and source.payload["windowEligible"] is True:
            sources.append(source)
        scheduled += timedelta(hours=CADENCE_HOURS)
    if len(sources) > 11:
        raise TrendingExplosionError(
            "explosion_partial_scan_unbounded",
            "more than eleven intermediate two-hour captures were selected",
            stage="source",
        )
    return tuple(sources)


def _coverage_witness(data_dir: Path, window_start: datetime) -> CaptureSource | None:
    """Find the nearest valid slot before T-24h with a strict 90-day bound."""

    maximum_slots = RETENTION_DAYS * (24 // CADENCE_HOURS)
    scheduled = window_start - timedelta(hours=CADENCE_HOURS)
    for _ in range(maximum_slots):
        try:
            source = _load_source_capture(data_dir, scheduled)
        except TrendingExplosionError as error:
            raise TrendingExplosionError(
                "explosion_coverage_witness_invalid",
                str(error),
                stage="source",
            ) from None
        if source is not None:
            return source
        scheduled -= timedelta(hours=CADENCE_HOURS)
    return None


def load_explosion_sources(data_dir: Path, window_end: datetime) -> ExplosionSources:
    """Load only the bounded source set required by one 24-hour artifact."""

    ended = parse_window_end(window_end)
    started = ended - timedelta(hours=WINDOW_HOURS)
    try:
        current = _load_source_capture(data_dir, ended)
    except TrendingExplosionError as error:
        raise TrendingExplosionError(
            "explosion_current_capture_invalid", str(error), stage="source"
        ) from None
    if current is None:
        raise TrendingExplosionError(
            "explosion_current_capture_missing",
            f"no capture exists for the current endpoint {capture_id_for_scheduled_at(ended)}",
            stage="source",
        )
    if current.payload["windowEligible"] is not True:
        raise TrendingExplosionError(
            "explosion_current_capture_ineligible",
            "the current endpoint is outside the ten-minute observation tolerance",
            stage="source",
        )

    try:
        baseline = _load_source_capture(data_dir, started)
    except TrendingExplosionError as error:
        raise TrendingExplosionError(
            "explosion_baseline_capture_invalid", str(error), stage="source"
        ) from None
    partial = _partial_sources(data_dir, started, ended)
    witness: CaptureSource | None = None
    if baseline is not None and baseline.payload["windowEligible"] is True:
        state = "exact"
    elif baseline is not None:
        state = "baseline_missing"
    else:
        witness = _coverage_witness(data_dir, started)
        state = "baseline_missing" if witness is not None else "warming_up"
    return ExplosionSources(current, baseline, partial, witness, state)


def _source_reference(source: CaptureSource, generation_relative_path: str) -> dict[str, Any]:
    return {
        "captureId": source.payload["captureId"],
        "scheduledAt": source.payload["scheduledAt"],
        "capturedAt": source.payload["capturedAt"],
        "coverageState": source.payload["coverageState"],
        "generationRelativePath": generation_relative_path,
        "originalObservationPath": source.original_observation_path,
        "payloadDigestSha256": source.payload["digest"]["value"],
        "fileSha256": source.file_sha256,
    }


def _observation_index(source: CaptureSource | None) -> dict[int, dict[str, Any]]:
    if source is None:
        return {}
    result: dict[int, dict[str, Any]] = {}
    for item in source.payload["observations"]:
        repository_id = int(item["githubRepositoryId"])
        if repository_id in result:
            raise TrendingExplosionError(
                "explosion_source_identity_conflict",
                f"duplicate GitHub repository ID in {source.payload['captureId']}",
                stage="derive",
            )
        result[repository_id] = item
    return result


def _first_observation(
    repository_id: int,
    current: CaptureSource,
    partial: Sequence[CaptureSource],
) -> tuple[CaptureSource, dict[str, Any]]:
    candidates: list[tuple[CaptureSource, dict[str, Any]]] = []
    for source in [*partial, current]:
        item = _observation_index(source).get(repository_id)
        if item is not None:
            candidates.append((source, item))
    if not candidates:
        raise TrendingExplosionError(
            "explosion_pending_source_missing",
            f"current repository ID {repository_id} has no bounded source observation",
            stage="derive",
        )
    candidates.sort(key=lambda pair: (pair[0].scheduled_at, pair[0].payload["captureId"]))
    return candidates[0]


def _exact_item(
    current: dict[str, Any],
    baseline: dict[str, Any],
    current_source: CaptureSource,
    baseline_source: CaptureSource,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    previous_repository = (
        baseline["repository"]
        if baseline["repository"].casefold() != current["repository"].casefold()
        else None
    )
    return {
        "rank": 0,
        "githubRepositoryId": current["githubRepositoryId"],
        "repository": current["repository"],
        "previousRepository": previous_repository,
        "htmlUrl": current["htmlUrl"],
        "totalStars": current["totalStars"],
        "baselineStars": baseline["totalStars"],
        "observedStarDelta": current["totalStars"] - baseline["totalStars"],
        "windowStartedAt": _timestamp(window_start),
        "windowEndedAt": _timestamp(window_end),
        "currentCapturedAt": current_source.payload["capturedAt"],
        "baselineCapturedAt": baseline_source.payload["capturedAt"],
        "createdAt": current["createdAt"],
        "updatedAt": current["updatedAt"],
        "pushedAt": current["pushedAt"],
        "defaultBranch": current["defaultBranch"],
        "primaryLanguage": current["primaryLanguage"],
        "topics": copy.deepcopy(current["topics"]),
        "licenseSpdxId": current["licenseSpdxId"],
        "archived": current["archived"],
        "disabled": False,
        "fork": current["fork"],
        "mirrorUrl": current["mirrorUrl"],
        "currentRecalledBy": copy.deepcopy(current["recalledBy"]),
        "baselineRecalledBy": copy.deepcopy(baseline["recalledBy"]),
        "state": "exact_window",
    }


def _pending_item(
    current: dict[str, Any],
    current_source: CaptureSource,
    partial: Sequence[CaptureSource],
    pending_reason: str,
) -> dict[str, Any]:
    first_source, first = _first_observation(
        int(current["githubRepositoryId"]), current_source, partial
    )
    if first_source.payload["captureId"] == current_source.payload["captureId"]:
        observed_hours: float | None = None
        observed_delta: int | None = None
    else:
        seconds = (current_source.captured_at - first_source.captured_at).total_seconds()
        if seconds < 0 or seconds > WINDOW_HOURS * 3600:
            raise TrendingExplosionError(
                "explosion_partial_window_invalid",
                "pending observation window is negative or exceeds 24 hours",
                stage="derive",
            )
        observed_hours = round(seconds / 3600, 6)
        observed_delta = int(current["totalStars"]) - int(first["totalStars"])
    return {
        "pendingRank": 0,
        "pendingReason": pending_reason,
        "githubRepositoryId": current["githubRepositoryId"],
        "repository": current["repository"],
        "htmlUrl": current["htmlUrl"],
        "totalStars": current["totalStars"],
        "firstSeenAt": first_source.payload["capturedAt"],
        "observedWindowStartedAt": first_source.payload["capturedAt"],
        "observedWindowEndedAt": current_source.payload["capturedAt"],
        "observedWindowHours": observed_hours,
        "observedWindowStarDelta": observed_delta,
        "currentCapturedAt": current_source.payload["capturedAt"],
        "firstObservationCaptureId": first_source.payload["captureId"],
        "pushedAt": current["pushedAt"],
        "primaryLanguage": current["primaryLanguage"],
        "topics": copy.deepcopy(current["topics"]),
        "archived": current["archived"],
        "fork": current["fork"],
        "mirrorUrl": current["mirrorUrl"],
        "currentRecalledBy": copy.deepcopy(current["recalledBy"]),
    }


def _conflict_item(
    reason: str,
    current: dict[str, Any],
    current_source: CaptureSource,
    baseline: dict[str, Any] | None,
    baseline_source: CaptureSource | None,
) -> dict[str, Any]:
    capture_ids: list[str] = []
    if baseline is not None and baseline_source is not None:
        capture_ids.append(str(baseline_source.payload["captureId"]))
    capture_ids.append(str(current_source.payload["captureId"]))
    return {
        "reason": reason,
        "githubRepositoryId": current["githubRepositoryId"],
        "repository": current["repository"],
        "currentStars": current["totalStars"],
        "baselineStars": baseline["totalStars"] if baseline is not None else None,
        "sourceCaptureIds": capture_ids,
    }


def _contains_ai_field(payload: object) -> str | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in AI_FIELDS:
                return key
            nested = _contains_ai_field(value)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for value in payload:
            nested = _contains_ai_field(value)
            if nested is not None:
                return nested
    return None


def validate_explosion_artifact(payload: object) -> dict[str, Any]:
    try:
        artifact = require_valid(ArtifactKind.TRENDING_EXPLOSION, payload)
    except (ArtifactValidationError, TypeError, ValueError) as error:
        raise TrendingExplosionError(
            "explosion_schema_invalid",
            f"TrendingExplosionArtifact failed Schema validation: {error}",
            stage="schema",
        ) from None
    banned = _contains_ai_field(artifact)
    if banned is not None:
        raise TrendingExplosionError(
            "explosion_ai_field_forbidden",
            f"fact artifact contains subjective field {banned!r}",
            stage="schema",
        )
    ended = parse_window_end(str(artifact["window"]["endedAt"]))
    started = parse_timestamp(artifact["window"]["startedAt"], field="window.startedAt")
    if ended - started != timedelta(hours=WINDOW_HOURS):
        raise TrendingExplosionError(
            "explosion_window_mismatch",
            "window endpoints must be exactly 24 hours apart",
            stage="audit",
        )
    all_ids = [
        int(item["githubRepositoryId"])
        for group in (artifact["exactRanked"], artifact["pendingRanked"], artifact["conflicts"])
        for item in group
    ]
    if len(all_ids) != len(set(all_ids)):
        raise TrendingExplosionError(
            "explosion_duplicate_repository_id",
            "exact, pending, and conflict partitions must use unique repository IDs",
            stage="audit",
        )
    source_paths = [
        item["generationRelativePath"]
        for item in [
            artifact["sourceCaptures"]["current"],
            artifact["sourceCaptures"]["baseline"],
            *artifact["sourceCaptures"]["partial"],
            artifact["sourceCaptures"]["coverageWitness"],
        ]
        if item is not None
    ]
    if len(source_paths) != len(set(source_paths)):
        raise TrendingExplosionError(
            "explosion_duplicate_source_path",
            "generation-local source capture paths must be unique",
            stage="audit",
        )
    return artifact


def build_trending_explosion_artifact(
    *,
    generation_id: str,
    window_end: datetime,
    generated_at: datetime,
    sources: ExplosionSources,
) -> dict[str, Any]:
    """Pure mechanical derivation from already validated capture sources."""

    if not isinstance(generation_id, str) or not GENERATION_ID_PATTERN.fullmatch(generation_id):
        raise TrendingExplosionError(
            "explosion_invalid_generation_id",
            f"unsafe candidate generation ID: {generation_id!r}",
            stage="contract",
        )
    ended = parse_window_end(window_end)
    started = ended - timedelta(hours=WINDOW_HOURS)
    generated = _utc(generated_at, field="generated_at")
    if generated < sources.current.captured_at:
        raise TrendingExplosionError(
            "explosion_generated_before_source",
            "generated_at cannot predate the current source capture",
            stage="contract",
        )
    if sources.current.scheduled_at != ended or sources.current.payload["windowEligible"] is not True:
        raise TrendingExplosionError(
            "explosion_current_capture_mismatch",
            "current source is not the eligible window endpoint",
            stage="derive",
        )
    if sources.baseline is not None and sources.baseline.scheduled_at != started:
        raise TrendingExplosionError(
            "explosion_baseline_capture_mismatch",
            "baseline source is not the T-24h endpoint",
            stage="derive",
        )
    partial_schedules = [source.scheduled_at for source in sources.partial]
    if partial_schedules != sorted(partial_schedules) or len(partial_schedules) != len(
        set(partial_schedules)
    ):
        raise TrendingExplosionError(
            "explosion_partial_capture_order_invalid",
            "partial capture phases must be unique and ascending",
            stage="derive",
        )
    if any(
        not (started < source.scheduled_at < ended)
        or source.payload["windowEligible"] is not True
        for source in sources.partial
    ):
        raise TrendingExplosionError(
            "explosion_partial_capture_mismatch",
            "partial sources must be eligible fixed phases strictly inside the window",
            stage="derive",
        )
    expected_state = (
        "exact"
        if sources.baseline is not None and sources.baseline.payload["windowEligible"] is True
        else "baseline_missing"
        if sources.baseline is not None or sources.coverage_witness is not None
        else "warming_up"
    )
    if sources.window_state != expected_state:
        raise TrendingExplosionError(
            "explosion_window_state_mismatch",
            "window state does not match baseline and coverage evidence",
            stage="derive",
        )
    if sources.coverage_witness is not None and sources.coverage_witness.scheduled_at >= started:
        raise TrendingExplosionError(
            "explosion_coverage_witness_mismatch",
            "coverage witness must precede the T-24h baseline slot",
            stage="derive",
        )

    current_by_id = _observation_index(sources.current)
    baseline_by_id = (
        _observation_index(sources.baseline)
        if sources.baseline is not None and sources.baseline.payload["windowEligible"] is True
        else {}
    )
    baseline_by_name = {
        str(item["repository"]).casefold(): item for item in baseline_by_id.values()
    }
    exact: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for repository_id, current in current_by_id.items():
        baseline = baseline_by_id.get(repository_id)
        name_match = baseline_by_name.get(str(current["repository"]).casefold())
        if (
            name_match is not None
            and int(name_match["githubRepositoryId"]) != repository_id
        ):
            conflicts.append(
                _conflict_item(
                    "source_identity_conflict",
                    current,
                    sources.current,
                    name_match,
                    sources.baseline,
                )
            )
            continue
        if current["disabled"] is True:
            conflicts.append(
                _conflict_item(
                    "current_disabled", current, sources.current, baseline, sources.baseline
                )
            )
            continue
        if baseline is not None:
            if int(current["totalStars"]) < int(baseline["totalStars"]):
                conflicts.append(
                    _conflict_item(
                        "star_count_decreased",
                        current,
                        sources.current,
                        baseline,
                        sources.baseline,
                    )
                )
            else:
                assert sources.baseline is not None
                exact.append(
                    _exact_item(
                        current,
                        baseline,
                        sources.current,
                        sources.baseline,
                        started,
                        ended,
                    )
                )
            continue
        pending_reason = (
            "baseline_ineligible"
            if sources.baseline is not None
            and sources.baseline.payload["windowEligible"] is not True
            else "baseline_missing"
            if sources.window_state == "baseline_missing"
            else "first_seen"
        )
        pending.append(
            _pending_item(current, sources.current, sources.partial, pending_reason)
        )

    exact.sort(
        key=lambda item: (
            -int(item["observedStarDelta"]),
            -int(item["totalStars"]),
            str(item["repository"]),
        )
    )
    exact_eligible_count = len(exact)
    exact = exact[:EXACT_LIMIT]
    for rank, item in enumerate(exact, start=1):
        item["rank"] = rank

    pending.sort(
        key=lambda item: (
            0 if item["observedWindowStarDelta"] is not None else 1,
            -int(item["observedWindowStarDelta"] or 0),
            -int(item["totalStars"]),
            str(item["repository"]),
        )
    )
    pending_eligible_count = len(pending)
    pending = pending[:PENDING_LIMIT]
    for rank, item in enumerate(pending, start=1):
        item["pendingRank"] = rank
    conflicts.sort(
        key=lambda item: (
            str(item["reason"]),
            str(item["repository"]),
            int(item["githubRepositoryId"]),
        )
    )

    baseline_payload = sources.baseline.payload if sources.baseline is not None else None
    source_refs = {
        "current": _source_reference(sources.current, CURRENT_SOURCE_PATH),
        "baseline": (
            _source_reference(sources.baseline, BASELINE_SOURCE_PATH)
            if sources.baseline is not None
            else None
        ),
        "partial": [
            _source_reference(source, f"trending/sources/partial-{index:02d}.json")
            for index, source in enumerate(sources.partial, start=1)
        ],
        "coverageWitness": (
            _source_reference(sources.coverage_witness, COVERAGE_WITNESS_PATH)
            if sources.coverage_witness is not None
            else None
        ),
    }
    all_sources = [
        sources.current,
        *([sources.baseline] if sources.baseline is not None else []),
        *sources.partial,
        *([sources.coverage_witness] if sources.coverage_witness is not None else []),
    ]
    degraded = sources.window_state != "exact" or any(
        source.payload["coverageState"] == "degraded" for source in all_sources
    )
    artifact: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "policyVersion": POLICY_VERSION,
        "generationId": generation_id,
        "generatedAt": _timestamp(generated),
        "scheduleTimezone": SCHEDULE_TIMEZONE,
        "window": {
            "state": sources.window_state,
            "startedAt": _timestamp(started),
            "endedAt": _timestamp(ended),
            "durationHours": WINDOW_HOURS,
            "toleranceSeconds": WINDOW_TOLERANCE_SECONDS,
        },
        "rankingPolicy": {
            "primary": "observedStarDelta DESC",
            "tieBreakers": ["totalStars DESC", "repository ASC"],
            "exactLimit": EXACT_LIMIT,
            "pendingLimit": PENDING_LIMIT,
        },
        "sourceCaptures": source_refs,
        "coverage": {
            "state": "degraded" if degraded else "healthy",
            "currentSuccessfulQueryCount": sources.current.payload["successfulQueryCount"],
            "currentFailedQueryCount": sources.current.payload["failedQueryCount"],
            "currentMetadataFailureCount": sources.current.payload["metadataFailureCount"],
            "baselineSuccessfulQueryCount": (
                baseline_payload["successfulQueryCount"] if baseline_payload is not None else None
            ),
            "baselineFailedQueryCount": (
                baseline_payload["failedQueryCount"] if baseline_payload is not None else None
            ),
            "baselineMetadataFailureCount": (
                baseline_payload["metadataFailureCount"] if baseline_payload is not None else None
            ),
            "exactEligibleCount": exact_eligible_count,
            "exactPublishedCount": len(exact),
            "pendingEligibleCount": pending_eligible_count,
            "pendingPublishedCount": len(pending),
            "conflictCount": len(conflicts),
        },
        "exactRanked": exact,
        "pendingRanked": pending,
        "conflicts": conflicts,
    }
    return validate_explosion_artifact(artifact)


def _safe_generation_source(
    path: Path,
    root: Path,
    *,
    directory: bool = False,
) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise TrendingExplosionError(
            "explosion_unsafe_generation_path",
            f"source path escapes candidate generation: {path}",
            stage="write",
        ) from None
    if ".." in relative.parts:
        raise TrendingExplosionError(
            "explosion_unsafe_generation_path",
            f"source path is unsafe: {path}",
            stage="write",
        )
    current = root
    for index, part in enumerate(relative.parts):
        current /= part
        if not os.path.lexists(current):
            continue
        metadata = os.lstat(current)
        reparse = bool(
            int(getattr(metadata, "st_file_attributes", 0))
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
        is_leaf = index == len(relative.parts) - 1
        expected_type = (
            stat.S_ISDIR(metadata.st_mode)
            if not is_leaf or directory
            else stat.S_ISREG(metadata.st_mode)
        )
        if not expected_type or reparse or stat.S_ISLNK(metadata.st_mode):
            raise TrendingExplosionError(
                "explosion_unsafe_generation_path",
                f"generation source path contains an unsafe filesystem object: {current}",
                stage="write",
            )


def _atomic_write_bytes(path: Path, content: bytes, root: Path) -> None:
    _safe_generation_source(path.parent, root, directory=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_generation_source(path.parent, root, directory=True)
    _safe_generation_source(path, root)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as error:
        raise TrendingExplosionError(
            "explosion_candidate_write_failed",
            f"candidate artifact could not be written atomically: {path}: {error}",
            stage="write",
        ) from None
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def write_candidate_explosion(
    candidate_root: Path,
    artifact: dict[str, Any],
    sources: ExplosionSources,
) -> None:
    """Write validated artifact JSON and byte-exact source copies to a candidate."""

    root = Path(candidate_root).resolve()
    validate_explosion_artifact(artifact)
    writes: list[tuple[str, bytes]] = [(CURRENT_SOURCE_PATH, sources.current.content)]
    if sources.baseline is not None:
        writes.append((BASELINE_SOURCE_PATH, sources.baseline.content))
    for index, source in enumerate(sources.partial, start=1):
        writes.append((f"trending/sources/partial-{index:02d}.json", source.content))
    if sources.coverage_witness is not None:
        writes.append((COVERAGE_WITNESS_PATH, sources.coverage_witness.content))
    declared = {
        item["generationRelativePath"]
        for item in [
            artifact["sourceCaptures"]["current"],
            artifact["sourceCaptures"]["baseline"],
            *artifact["sourceCaptures"]["partial"],
            artifact["sourceCaptures"]["coverageWitness"],
        ]
        if item is not None
    }
    if declared != {relative for relative, _ in writes}:
        raise TrendingExplosionError(
            "explosion_source_inventory_mismatch",
            "artifact source references do not match source bytes selected for publication",
            stage="write",
        )
    sources_dir = root / "trending" / "sources"
    if sources_dir.exists():
        _safe_generation_source(sources_dir, root, directory=True)
        for existing in sources_dir.glob("*.json"):
            if existing.relative_to(root).as_posix() not in declared:
                _safe_generation_source(existing, root)
                existing.unlink()
    for relative, content in writes:
        _atomic_write_bytes(root / Path(relative), content, root)
        copied = stable_read(root / Path(relative))
        if copied.content != content:
            raise TrendingExplosionError(
                "explosion_source_copy_mismatch",
                f"generation source copy is not byte-exact: {relative}",
                stage="write",
            )
    serialized = (strict_json_dumps(artifact) + "\n").encode("utf-8")
    _atomic_write_bytes(root / EXPLOSION_PATH, serialized, root)


def _input_fact_paths(root: Path) -> list[Path]:
    fixed = [
        root / "snapshots/latest.json",
        root / "catalog/latest.json",
        root / "signals/latest.json",
        root / "signals/enrichment.json",
    ]
    paths = [path for path in fixed if path.exists()]
    for directory in ("snapshots/history", "analysis", "enrichment"):
        paths.extend(sorted((root / directory).glob("*.json")))
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def assert_derive_inputs_unchanged(base_root: Path, candidate_root: Path) -> None:
    """Ensure the explosion derive changed no existing business facts."""

    base = Path(base_root).resolve()
    candidate = Path(candidate_root).resolve()
    base_inventory = {
        path.relative_to(base).as_posix(): stable_read(path).sha256
        for path in _input_fact_paths(base)
    }
    candidate_inventory = {
        path.relative_to(candidate).as_posix(): stable_read(path).sha256
        for path in _input_fact_paths(candidate)
    }
    if base_inventory != candidate_inventory:
        raise TrendingExplosionError(
            "explosion_derive_changed_business_facts",
            "explosion derive changed snapshot, history, Catalog, Signals, analysis, or enrichment",
            stage="candidate",
        )


def _load_generation_source(
    root: Path,
    reference: dict[str, Any],
    expected_relative: str,
) -> CaptureSource:
    if reference.get("generationRelativePath") != expected_relative:
        raise TrendingExplosionError(
            "explosion_source_path_mismatch",
            f"source reference must use {expected_relative}",
            stage="audit",
        )
    path = root / Path(expected_relative)
    try:
        _safe_generation_source(path, root)
        snapshot = stable_read(path, expected_sha256=reference["fileSha256"])
        payload = strict_json_loads(snapshot.content.decode("utf-8", errors="strict"))
        validated = validate_capture_bundle(
            payload,
            expected_capture_id=reference["captureId"],
        )
    except (
        ArtifactValidationError,
        OSError,
        StableReadError,
        TrendingObservationError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        raise TrendingExplosionError(
            "explosion_generation_source_invalid",
            f"generation-local capture {expected_relative} is invalid: {error}",
            stage="audit",
        ) from None
    scheduled = parse_timestamp(validated["scheduledAt"], field="scheduledAt")
    expected_original = _original_observation_path(scheduled)
    expected_fields = {
        "captureId": validated["captureId"],
        "scheduledAt": validated["scheduledAt"],
        "capturedAt": validated["capturedAt"],
        "coverageState": validated["coverageState"],
        "generationRelativePath": expected_relative,
        "originalObservationPath": expected_original,
        "payloadDigestSha256": validated["digest"]["value"],
        "fileSha256": snapshot.sha256,
    }
    if reference != expected_fields:
        raise TrendingExplosionError(
            "explosion_source_reference_mismatch",
            f"source reference does not match generation-local bytes: {expected_relative}",
            stage="audit",
        )
    return CaptureSource(
        path=path,
        original_observation_path=expected_original,
        content=snapshot.content,
        file_sha256=snapshot.sha256,
        payload=validated,
    )


def _load_generation_sources(root: Path, artifact: dict[str, Any]) -> ExplosionSources:
    references = artifact["sourceCaptures"]
    current = _load_generation_source(root, references["current"], CURRENT_SOURCE_PATH)
    baseline = (
        _load_generation_source(root, references["baseline"], BASELINE_SOURCE_PATH)
        if references["baseline"] is not None
        else None
    )
    partial: list[CaptureSource] = []
    for index, reference in enumerate(references["partial"], start=1):
        partial.append(
            _load_generation_source(
                root, reference, f"trending/sources/partial-{index:02d}.json"
            )
        )
    witness = (
        _load_generation_source(
            root, references["coverageWitness"], COVERAGE_WITNESS_PATH
        )
        if references["coverageWitness"] is not None
        else None
    )
    expected_files = {
        reference["generationRelativePath"]
        for reference in [
            references["current"],
            references["baseline"],
            *references["partial"],
            references["coverageWitness"],
        ]
        if reference is not None
    }
    source_dir = root / "trending" / "sources"
    if not source_dir.exists():
        actual_files: set[str] = set()
    else:
        _safe_generation_source(source_dir, root, directory=True)
        actual_files = set()
        try:
            entries = list(os.scandir(source_dir))
        except OSError as error:
            raise TrendingExplosionError(
                "explosion_source_inventory_unreadable",
                f"generation-local source directory is unreadable: {error}",
                stage="audit",
            ) from None
        for entry in entries:
            path = Path(entry.path)
            _safe_generation_source(path, root)
            if not entry.name.endswith(".json"):
                raise TrendingExplosionError(
                    "explosion_source_inventory_mismatch",
                    "generation-local source directory contains an unsupported entry",
                    stage="audit",
                )
            actual_files.add(path.relative_to(root).as_posix())
    if actual_files != expected_files:
        raise TrendingExplosionError(
            "explosion_source_inventory_mismatch",
            "generation-local capture files do not match artifact source references",
            stage="audit",
        )
    return ExplosionSources(current, baseline, tuple(partial), witness, artifact["window"]["state"])


def audit_trending_explosion_generation(root: Path) -> dict[str, Any]:
    """Read-only full recomputation of an optional generation artifact."""

    generation_root = Path(root).resolve()
    artifact_path = generation_root / EXPLOSION_PATH
    source_dir = generation_root / "trending" / "sources"
    artifact_exists = os.path.lexists(artifact_path)
    source_exists = os.path.lexists(source_dir)
    if not artifact_exists and not source_exists:
        return {
            "present": False,
            "status": "not_present",
            "exactCount": 0,
            "pendingCount": 0,
            "conflictCount": 0,
            "issues": [],
        }
    issues: list[dict[str, str]] = []
    artifact: dict[str, Any] | None = None
    try:
        if not artifact_exists or not source_exists:
            raise TrendingExplosionError(
                "explosion_incomplete_generation",
                "explosion artifact and source directory must appear together",
                stage="audit",
            )
        _safe_generation_source(artifact_path, generation_root)
        snapshot = stable_read(artifact_path)
        parsed = strict_json_loads(snapshot.content.decode("utf-8", errors="strict"))
        artifact = validate_explosion_artifact(parsed)
        if artifact["generationId"] != generation_root.name:
            raise TrendingExplosionError(
                "explosion_generation_id_mismatch",
                "artifact generationId does not match its generation directory",
                stage="audit",
            )
        sources = _load_generation_sources(generation_root, artifact)
        rebuilt = build_trending_explosion_artifact(
            generation_id=generation_root.name,
            window_end=parse_window_end(artifact["window"]["endedAt"]),
            generated_at=parse_generated_at(artifact["generatedAt"]),
            sources=sources,
        )
        if artifact != rebuilt:
            raise TrendingExplosionError(
                "explosion_recomputation_mismatch",
                "artifact ranking, pending facts, conflicts, coverage, or provenance differ from source recomputation",
                stage="audit",
            )
    except (
        OSError,
        StableReadError,
        TrendingExplosionError,
        TrendingObservationError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        code = error.code if isinstance(error, TrendingExplosionError) else "explosion_audit_failed"
        issues.append({"severity": "error", "code": code, "detail": str(error)})
    return {
        "present": True,
        "status": "failed" if issues else "healthy",
        "exactCount": len(artifact.get("exactRanked", [])) if artifact else 0,
        "pendingCount": len(artifact.get("pendingRanked", [])) if artifact else 0,
        "conflictCount": len(artifact.get("conflicts", [])) if artifact else 0,
        "windowState": artifact.get("window", {}).get("state") if artifact else None,
        "coverageState": artifact.get("coverage", {}).get("state") if artifact else None,
        "issues": issues,
    }


def explosion_source_signature(artifact: dict[str, Any]) -> tuple[str, str | None, str, str | None]:
    """Return both payload and raw-byte identities for idempotency checks."""

    current = artifact["sourceCaptures"]["current"]
    baseline = artifact["sourceCaptures"]["baseline"]
    return (
        str(current["payloadDigestSha256"]),
        str(baseline["payloadDigestSha256"]) if baseline is not None else None,
        str(current["fileSha256"]),
        str(baseline["fileSha256"]) if baseline is not None else None,
    )


def candidate_artifact_bytes(artifact: dict[str, Any]) -> bytes:
    """Expose the deterministic artifact serialization used by dry-run tests."""

    validate_explosion_artifact(artifact)
    return (strict_json_dumps(artifact) + "\n").encode("utf-8")


__all__ = [
    "BASELINE_SOURCE_PATH",
    "CURRENT_SOURCE_PATH",
    "EXPLOSION_PATH",
    "POLICY_VERSION",
    "CaptureSource",
    "ExplosionSources",
    "TrendingExplosionError",
    "assert_derive_inputs_unchanged",
    "audit_trending_explosion_generation",
    "build_trending_explosion_artifact",
    "candidate_artifact_bytes",
    "explosion_source_signature",
    "load_explosion_sources",
    "parse_generated_at",
    "parse_window_end",
    "validate_explosion_artifact",
    "write_candidate_explosion",
]
