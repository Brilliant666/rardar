"""Audited near-real-time discovery facts derived from immutable observations.

Discover has its own publication cadence and therefore its own immutable
generation store.  It never mutates the daily generation, raw observations,
or D1, and it never calls GitHub or a model.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from pipeline.data_lock import data_dir_lock
from pipeline.generations import GenerationProtocolError, resolve_current_generation
from pipeline.schema_validation import (
    ArtifactKind,
    ArtifactValidationError,
    require_valid,
    strict_json_loads,
)
from pipeline.stable_read import StableReadError, stable_read
from pipeline.trending_explosion import (
    EXPLOSION_PATH,
    CaptureSource,
    TrendingExplosionError,
    _load_source_capture,
    validate_explosion_artifact,
)
from pipeline.trending_observations import (
    CADENCE_MINUTES,
    CAPTURE_ID_PATTERN,
    TRACKING_WINDOW_HOURS,
    TrendingObservationError,
    _walk_capture_paths,
    load_capture,
    parse_timestamp,
    validate_capture_bundle,
)


SCHEMA_VERSION = 3
POLICY_VERSION = "trending-discover-v3"
V2_SCHEMA_VERSION = 2
V2_POLICY_VERSION = "trending-discover-v2"
LEGACY_SCHEMA_VERSION = 1
LEGACY_POLICY_VERSION = "trending-discover-v1"
ABSOLUTE_GROWTH_GATE_STARS = 10
RELATIVE_GROWTH_GATE_PERCENT = 1.0
CONSECUTIVE_POSITIVE_INTERVAL_GATE = 2
RECENT_DISCOVERY_HOURS = 4
NEAR_VALIDATION_HOURS = 20
TODAY_PUBLISHED_TOP_COUNT = 20
OUTSIDE_RECENT_WINDOW_HOURS = 4
DISCOVER_RELATIVE_ROOT = Path("artifacts/trending/discover/v1")
DISCOVER_FILE = "discover.json"
TODAY_SOURCE_FILE = "sources/today-explosion.json"
TODAY_MANIFEST_FILE = "sources/today-manifest.json"
MAX_SOURCE_CAPTURES = TRACKING_WINDOW_HOURS // 2 + 1
GENERATION_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
LEGACY_STAGE_KEYS = {
    "just_discovered": "justDiscovered",
    "rising": "rising",
    "near_validation": "nearValidation",
}
STAGE_KEYS = {
    "just_discovered": "justDiscovered",
    "outside_today_momentum": "outsideTodayMomentum",
    "rising": "rising",
    "near_validation": "nearValidation",
}
LEGACY_STAGE_ORDER = ("just_discovered", "rising", "near_validation")
STAGE_ORDER = (
    "just_discovered",
    "outside_today_momentum",
    "rising",
    "near_validation",
)
SIGNAL_FACT_ORDER = (
    "first_seen_recently",
    "outside_today_top20",
    "exact_rank_available",
    "recent_absolute_growth",
    "recent_relative_growth",
    "continuous_recent_growth",
    "recent_acceleration",
    "continuous_positive_growth",
    "absolute_growth_gate",
    "relative_growth_gate",
    "awaiting_today_settlement",
)
V2_SUPPRESSION_REASONS = (
    "weak_absolute_growth",
    "weak_relative_growth",
    "no_continuous_growth",
    "already_in_today",
    "identity_conflict",
    "negative_growth",
    "disabled",
    "metadata_incomplete",
)
SUPPRESSION_REASONS = (
    "today_published",
    "weak_recent_absolute_growth",
    "weak_recent_relative_growth",
    "no_recent_continuous_growth",
    "no_recent_acceleration",
    "weak_pre_exact_growth",
    "already_exact_without_momentum",
    "identity_conflict",
    "negative_growth",
    "disabled",
    "metadata_incomplete",
)
V3_ROOT_FIELDS = (
    "todayExactCount",
    "todayPublishedTopCount",
    "todayPublishedCount",
    "todayPublishedSetDigest",
    "excludedPublishedCount",
    "exactOutsidePublishedEvaluatedCount",
    "preExactEvaluatedCount",
    "eligibilityCounts",
)
V3_COVERAGE_FIELDS = (
    "todayExactCount",
    "todayPublishedCount",
    "excludedPublishedCount",
    "exactOutsidePublishedEvaluatedCount",
    "preExactEvaluatedCount",
    "invalidCount",
)
V3_ITEM_FIELDS = (
    "eligibilityClass",
    "todayExactRank",
    "todayExact24hDelta",
    "recentWindowHours",
    "recentObservedStarDelta",
    "priorComparableWindowDelta",
    "accelerationDelta",
    "recentRelativeGrowthPercent",
)


class TrendingDiscoverError(RuntimeError):
    """A stable, bounded Discover derivation, publication, or audit error."""

    def __init__(self, code: str, message: str, *, stage: str = "derive") -> None:
        self.code = code
        self.stage = stage
        super().__init__(message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "stage": self.stage, "error": str(self)}


@dataclass(frozen=True)
class TodayExplosionSource:
    generation_id: str
    generation_manifest_sha256: str
    generation_manifest_content: bytes
    generation_manifest: dict[str, Any]
    content: bytes
    file_sha256: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class DiscoverSources:
    captures: tuple[CaptureSource, ...]
    today: TodayExplosionSource

    @property
    def latest(self) -> CaptureSource:
        return self.captures[-1]


@dataclass(frozen=True)
class ResolvedDiscoverGeneration:
    data_dir: Path
    generation_id: str
    root: Path
    pointer: dict[str, Any]
    manifest: dict[str, Any]
    artifact: dict[str, Any]


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TrendingDiscoverError(
            "discover_timezone_required",
            f"{field} must include an explicit timezone",
            stage="contract",
        )
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value, field="timestamp").isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _payload_digest(payload: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(payload)
    unsigned.pop("payloadDigest", None)
    return _sha256(_canonical_bytes(unsigned))


def _attach_payload_digest(payload: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result["payloadDigest"] = {
        "algorithm": "sha256",
        "value": _payload_digest(result),
    }
    return result


def discover_store_root(data_dir: Path) -> Path:
    return data_dir.expanduser().resolve() / DISCOVER_RELATIVE_ROOT


def _ensure_real_directories(data_dir: Path) -> Path:
    """Create only missing Discover ancestors and reject link/reparse traversal."""

    canonical = data_dir.expanduser().resolve()
    current = canonical
    for part in DISCOVER_RELATIVE_ROOT.parts:
        current = current / part
        try:
            current.mkdir(exist_ok=True)
            metadata = os.lstat(current)
        except OSError as error:
            raise TrendingDiscoverError(
                "discover_store_unavailable", f"Discover store cannot be created: {error}", stage="path"
            ) from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse(metadata)
        ):
            raise TrendingDiscoverError(
                "discover_unsafe_path", f"Discover store ancestor is unsafe: {current}", stage="path"
            )
    for relative in (Path("generations"), Path("generations/.candidates")):
        current = canonical / DISCOVER_RELATIVE_ROOT / relative
        try:
            current.mkdir(exist_ok=True)
            metadata = os.lstat(current)
        except OSError as error:
            raise TrendingDiscoverError(
                "discover_store_unavailable", f"Discover generation store is unavailable: {error}", stage="path"
            ) from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse(metadata)
        ):
            raise TrendingDiscoverError(
                "discover_unsafe_path", "Discover generation store may not be a link", stage="path"
            )
    return canonical / DISCOVER_RELATIVE_ROOT


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def _audit_store_ancestors(root: Path) -> None:
    """Reject unsafe store ancestors and abandoned atomic-pointer files."""

    for path in (root, root / "generations"):
        try:
            metadata = os.lstat(path)
        except OSError as error:
            raise TrendingDiscoverError(
                "discover_path_missing",
                f"Discover store path is unavailable: {path.name}: {error}",
                stage="path",
            ) from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or _is_reparse(metadata)
        ):
            raise TrendingDiscoverError(
                "discover_unsafe_path",
                f"Discover store path must be a real directory: {path.name}",
                stage="path",
            )
    try:
        entries = tuple(root.iterdir())
    except OSError as error:
        raise TrendingDiscoverError(
            "discover_store_unavailable", str(error), stage="path"
        ) from None
    if any(
        path.name.startswith(".current.json.") and path.name.endswith(".tmp")
        for path in entries
    ):
        raise TrendingDiscoverError(
            "discover_temporary_file_present",
            "an abandoned Discover pointer temporary file is present",
            stage="audit",
        )


def _require_regular(path: Path, root: Path) -> None:
    try:
        root_metadata = os.lstat(root)
    except OSError as error:
        raise TrendingDiscoverError(
            "discover_path_missing", f"Discover root is unavailable: {error}", stage="path"
        ) from None
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or _is_reparse(root_metadata)
    ):
        raise TrendingDiscoverError(
            "discover_unsafe_path", "Discover root must be a real directory", stage="path"
        )
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        raise TrendingDiscoverError(
            "discover_path_escape", f"path escapes Discover root: {path}", stage="path"
        ) from None
    current = root
    for part in relative.parts:
        current = current / part
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise TrendingDiscoverError(
                "discover_path_missing", f"Discover path is unavailable: {current}: {error}", stage="path"
            ) from None
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise TrendingDiscoverError(
                "discover_unsafe_path", f"Discover path may not traverse a link: {current}", stage="path"
            )
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode):
        raise TrendingDiscoverError(
            "discover_unsafe_path", f"expected a regular file: {path}", stage="path"
        )


def _read_json(path: Path, root: Path, kind: ArtifactKind) -> tuple[dict[str, Any], bytes, str]:
    _require_regular(path, root)
    try:
        snapshot = stable_read(path)
        payload = strict_json_loads(snapshot.content.decode("utf-8", errors="strict"))
        validated = require_valid(kind, payload, source_path=path)
    except (
        ArtifactValidationError,
        OSError,
        StableReadError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        raise TrendingDiscoverError(
            "discover_artifact_invalid",
            f"Discover artifact is not trustworthy: {path.name}: {error}",
            stage="read",
        ) from None
    return validated, snapshot.content, snapshot.sha256


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_generation_path(root: Path, generation_id: str) -> Path:
    if not isinstance(generation_id, str) or not GENERATION_ID_PATTERN.fullmatch(generation_id):
        raise TrendingDiscoverError(
            "discover_invalid_generation_id",
            f"unsafe Discover generation ID: {generation_id!r}",
            stage="path",
        )
    target = root / "generations" / generation_id
    try:
        target.absolute().relative_to((root / "generations").absolute())
    except ValueError:
        raise TrendingDiscoverError(
            "discover_path_escape", "Discover generation path escaped its store", stage="path"
        ) from None
    return target


def _load_today_source(data_dir: Path) -> TodayExplosionSource:
    try:
        current = resolve_current_generation(data_dir)
    except GenerationProtocolError as error:
        raise TrendingDiscoverError(
            "discover_today_generation_invalid", str(error), stage="source"
        ) from None
    if current.legacy or current.generation_id is None or current.pointer is None:
        raise TrendingDiscoverError(
            "discover_today_generation_missing",
            "Discover requires a verified published Today generation",
            stage="source",
        )
    target = current.root / EXPLOSION_PATH
    if not os.path.lexists(target):
        raise TrendingDiscoverError(
            "discover_today_explosion_missing",
            "current generation has no Today explosion artifact",
            stage="source",
        )
    try:
        manifest_snapshot = stable_read(current.root / "manifest.json")
        manifest = require_valid(
            ArtifactKind.GENERATION_MANIFEST,
            strict_json_loads(
                manifest_snapshot.content.decode("utf-8", errors="strict")
            ),
            source_path=current.root / "manifest.json",
        )
        snapshot = stable_read(target)
        payload = strict_json_loads(snapshot.content.decode("utf-8", errors="strict"))
        validated = validate_explosion_artifact(payload)
    except (
        ArtifactValidationError,
        OSError,
        StableReadError,
        TrendingExplosionError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        raise TrendingDiscoverError(
            "discover_today_explosion_invalid", str(error), stage="source"
        ) from None
    if validated["generationId"] != current.generation_id:
        raise TrendingDiscoverError(
            "discover_today_generation_mismatch",
            "Today explosion identity does not match the verified current generation",
            stage="source",
        )
    if (
        manifest_snapshot.sha256 != current.pointer["manifestSha256"]
        or manifest != current.manifest
        or manifest["generationId"] != current.generation_id
        or manifest["state"] != "ready"
        or EXPLOSION_PATH not in manifest["artifacts"]
        or manifest["hashes"].get(EXPLOSION_PATH) != snapshot.sha256
    ):
        raise TrendingDiscoverError(
            "discover_today_manifest_mismatch",
            "Today manifest does not bind the copied Explosion artifact",
            stage="source",
        )
    return TodayExplosionSource(
        generation_id=current.generation_id,
        generation_manifest_sha256=str(current.pointer["manifestSha256"]),
        generation_manifest_content=manifest_snapshot.content,
        generation_manifest=manifest,
        content=snapshot.content,
        file_sha256=snapshot.sha256,
        payload=validated,
    )


def load_discover_sources(data_dir: Path) -> DiscoverSources:
    """Stable-read the latest eligible capture, its 26h window, and Today."""

    eligible: list[tuple[datetime, str]] = []
    try:
        indexed_paths: list[tuple[datetime, Path]] = []
        for path in _walk_capture_paths(data_dir):
            match = CAPTURE_ID_PATTERN.fullmatch(path.stem)
            if match is None:
                raise TrendingObservationError(
                    "capture_identity_mismatch", f"capture filename cannot be parsed: {path.name}"
                )
            indexed_paths.append(
                (
                    datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(
                        tzinfo=timezone.utc
                    ),
                    path,
                )
            )
        if not indexed_paths:
            raise TrendingDiscoverError(
                "discover_eligible_capture_missing",
                "no Observation capture is available",
                stage="source",
            )
        payload_cache: dict[datetime, dict[str, Any]] = {}
        latest_slot: datetime | None = None
        for file_slot, path in sorted(indexed_paths, reverse=True):
            payload = load_capture(path)
            scheduled = parse_timestamp(payload["scheduledAt"], field="scheduledAt")
            if scheduled != file_slot:
                raise TrendingObservationError(
                    "capture_identity_mismatch",
                    "capture scheduledAt does not match its filename phase",
                )
            payload_cache[file_slot] = payload
            if payload["windowEligible"] is True:
                latest_slot = file_slot
                break
        if latest_slot is None:
            raise TrendingDiscoverError(
                "discover_eligible_capture_missing",
                "no eligible Observation capture is available",
                stage="source",
            )
        cutoff_slot = latest_slot - timedelta(hours=TRACKING_WINDOW_HOURS)
        for file_slot, path in indexed_paths:
            if not cutoff_slot <= file_slot <= latest_slot:
                continue
            payload = payload_cache.get(file_slot) or load_capture(path)
            scheduled = parse_timestamp(payload["scheduledAt"], field="scheduledAt")
            if scheduled != file_slot:
                raise TrendingObservationError(
                    "capture_identity_mismatch",
                    "capture scheduledAt does not match its filename phase",
                )
            if payload["windowEligible"] is True:
                eligible.append(
                    (scheduled, payload["captureId"])
                )
    except (OSError, TrendingObservationError, ValueError) as error:
        raise TrendingDiscoverError(
            "discover_observation_source_invalid", str(error), stage="source"
        ) from None
    if not eligible:
        raise TrendingDiscoverError(
            "discover_eligible_capture_missing",
            "no eligible Observation capture is available",
            stage="source",
        )
    eligible.sort()
    latest_at = eligible[-1][0]
    cutoff = latest_at - timedelta(hours=TRACKING_WINDOW_HOURS)
    selected = [scheduled for scheduled, _ in eligible if cutoff <= scheduled <= latest_at]
    if len(selected) > MAX_SOURCE_CAPTURES:
        raise TrendingDiscoverError(
            "discover_source_window_unbounded",
            "more than fourteen eligible captures were selected",
            stage="source",
        )
    sources: list[CaptureSource] = []
    for scheduled_at in selected:
        source = _load_source_capture(data_dir, scheduled_at)
        if source is None or source.payload["windowEligible"] is not True:
            raise TrendingDiscoverError(
                "discover_source_capture_changed",
                "an eligible capture disappeared or became ineligible during stable read",
                stage="source",
            )
        sources.append(source)
    return DiscoverSources(tuple(sources), _load_today_source(data_dir))


def _observation_index(source: CaptureSource) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in source.payload["observations"]:
        repository_id = int(item["githubRepositoryId"])
        if repository_id in result:
            raise TrendingDiscoverError(
                "discover_source_identity_conflict",
                f"duplicate repository ID in {source.payload['captureId']}",
                stage="derive",
            )
        result[repository_id] = item
    return result


def _source_reference(
    source: CaptureSource, index: int, *, policy_version: str
) -> dict[str, Any]:
    reference = {
        "captureId": source.payload["captureId"],
        "scheduledAt": source.payload["scheduledAt"],
        "capturedAt": source.payload["capturedAt"],
        "coverageState": source.payload["coverageState"],
        "originalObservationPath": source.original_observation_path,
        "payloadDigestSha256": source.payload["digest"]["value"],
        "fileSha256": source.file_sha256,
    }
    if policy_version != POLICY_VERSION:
        reference["generationRelativePath"] = f"sources/capture-{index:02d}.json"
    return reference


def _today_reference(today: TodayExplosionSource) -> dict[str, Any]:
    return {
        "generationId": today.generation_id,
        "generationManifestSha256": today.generation_manifest_sha256,
        "generationManifestRelativePath": TODAY_MANIFEST_FILE,
        "generationRelativePath": TODAY_SOURCE_FILE,
        "originalGenerationPath": f"generations/{today.generation_id}/{EXPLOSION_PATH}",
        "fileSha256": today.file_sha256,
        "windowEndedAt": today.payload["window"]["endedAt"],
        "exactCount": len(today.payload["exactRanked"]),
    }


def _evidence_digest(observations: Sequence[tuple[CaptureSource, dict[str, Any]]]) -> str:
    evidence = [
        {
            "captureId": source.payload["captureId"],
            "githubRepositoryId": item["githubRepositoryId"],
            "repository": item["repository"],
            "totalStars": item["totalStars"],
        }
        for source, item in observations
    ]
    return _sha256(_canonical_bytes(evidence))


def _consecutive_count(
    repository_id: int,
    sources: Sequence[CaptureSource],
    indexes: Sequence[dict[int, dict[str, Any]]],
) -> int:
    count = 0
    previous: datetime | None = None
    for source, index in reversed(list(zip(sources, indexes, strict=True))):
        if repository_id not in index:
            break
        if previous is not None and previous - source.scheduled_at != timedelta(minutes=CADENCE_MINUTES):
            break
        count += 1
        previous = source.scheduled_at
    return count


def _positive_interval_facts(
    observations: Sequence[tuple[CaptureSource, dict[str, Any]]],
) -> tuple[int, int, int | None]:
    """Return positive intervals, the longest positive run, and latest delta.

    Only adjacent scheduled 2-hour slots form an interval. A gap resets the
    run, so carry-forward membership cannot manufacture continuity.
    """

    positive_count = 0
    longest_run = 0
    current_run = 0
    latest_delta: int | None = None
    pairs = list(zip(observations, observations[1:], strict=False))
    for index, ((previous_source, previous), (current_source, current)) in enumerate(
        pairs
    ):
        if current_source.scheduled_at - previous_source.scheduled_at != timedelta(
            minutes=CADENCE_MINUTES
        ):
            current_run = 0
            if index == len(pairs) - 1:
                latest_delta = None
            continue
        interval_delta = int(current["totalStars"]) - int(previous["totalStars"])
        if index == len(pairs) - 1:
            latest_delta = interval_delta
        if interval_delta > 0:
            positive_count += 1
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0
    return positive_count, longest_run, latest_delta


def _ordered_signal_facts(values: set[str]) -> list[str]:
    return [value for value in SIGNAL_FACT_ORDER if value in values]


def _stage_keys(policy_version: str) -> dict[str, str]:
    return STAGE_KEYS if policy_version == POLICY_VERSION else LEGACY_STAGE_KEYS


def _stage_order(policy_version: str) -> tuple[str, ...]:
    return STAGE_ORDER if policy_version == POLICY_VERSION else LEGACY_STAGE_ORDER


def _today_sets(
    exact_ranked: Sequence[dict[str, Any]],
) -> tuple[dict[int, dict[str, Any]], set[int], str]:
    exact_by_id = {
        int(item["githubRepositoryId"]): item for item in exact_ranked
    }
    published = {
        repository_id
        for repository_id, item in exact_by_id.items()
        if int(item["rank"]) <= TODAY_PUBLISHED_TOP_COUNT
    }
    digest = _sha256(_canonical_bytes(sorted(published)))
    return exact_by_id, published, digest


def _bounded_window(
    observations: Sequence[tuple[CaptureSource, dict[str, Any]]],
    *,
    start: datetime,
    end: datetime,
) -> list[tuple[CaptureSource, dict[str, Any]]]:
    return [
        observation
        for observation in observations
        if start <= observation[0].scheduled_at <= end
    ]


def _comparable_window_facts(
    observations: Sequence[tuple[CaptureSource, dict[str, Any]]],
    *,
    latest_scheduled_at: datetime,
) -> tuple[int | None, int | None, int | None, float | None, int]:
    """Return audited recent/prior four-hour facts without extrapolation."""

    recent_start = latest_scheduled_at - timedelta(hours=OUTSIDE_RECENT_WINDOW_HOURS)
    prior_start = recent_start - timedelta(hours=OUTSIDE_RECENT_WINDOW_HOURS)
    recent = _bounded_window(
        observations, start=recent_start, end=latest_scheduled_at
    )
    prior = _bounded_window(observations, start=prior_start, end=recent_start)

    def complete_delta(
        values: Sequence[tuple[CaptureSource, dict[str, Any]]],
    ) -> int | None:
        expected_intervals = OUTSIDE_RECENT_WINDOW_HOURS * 60 // CADENCE_MINUTES
        if len(values) != expected_intervals + 1:
            return None
        if any(
            current[0].scheduled_at - previous[0].scheduled_at
            != timedelta(minutes=CADENCE_MINUTES)
            for previous, current in zip(values, values[1:], strict=False)
        ):
            return None
        return int(values[-1][1]["totalStars"]) - int(values[0][1]["totalStars"])

    recent_delta = complete_delta(recent)
    prior_delta = complete_delta(prior)
    acceleration = (
        recent_delta - prior_delta
        if recent_delta is not None and prior_delta is not None
        else None
    )
    recent_relative = (
        round(recent_delta / int(recent[0][1]["totalStars"]) * 100, 6)
        if recent_delta is not None
        and recent
        and int(recent[0][1]["totalStars"]) > 0
        else None
    )
    _, recent_consecutive_positive, _ = _positive_interval_facts(recent)
    return (
        recent_delta,
        prior_delta,
        acceleration,
        recent_relative,
        recent_consecutive_positive,
    )


def validate_discover_artifact(payload: object) -> dict[str, Any]:
    try:
        artifact = require_valid(ArtifactKind.TRENDING_DISCOVER, payload)
    except (ArtifactValidationError, TypeError, ValueError) as error:
        raise TrendingDiscoverError(
            "discover_schema_invalid", str(error), stage="schema"
        ) from None
    digest = artifact["payloadDigest"]
    if digest["value"] != _payload_digest(artifact):
        raise TrendingDiscoverError(
            "discover_payload_digest_mismatch", "Discover payload digest does not match", stage="audit"
        )
    policy_version = artifact["policyVersion"]
    if policy_version != POLICY_VERSION and (
        any(field in artifact for field in V3_ROOT_FIELDS)
        or any(field in artifact["coverage"] for field in V3_COVERAGE_FIELDS)
        or any(
            field in item
            for items in artifact["stages"].values()
            for item in items
            for field in V3_ITEM_FIELDS
        )
    ):
        raise TrendingDiscoverError(
            "discover_policy_contract_mismatch",
            "retained Discover generations may not contain v3-only fields",
            stage="audit",
        )
    if policy_version == LEGACY_POLICY_VERSION:
        if artifact["schemaVersion"] != LEGACY_SCHEMA_VERSION or any(
            field in artifact for field in ("signalPolicy", "suppressionSummary")
        ):
            raise TrendingDiscoverError(
                "discover_policy_contract_mismatch",
                "legacy Discover policy contains v2-only contract fields",
                stage="audit",
            )
    elif policy_version in {V2_POLICY_VERSION, POLICY_VERSION}:
        expected_schema = (
            SCHEMA_VERSION if policy_version == POLICY_VERSION else V2_SCHEMA_VERSION
        )
        if artifact["schemaVersion"] != expected_schema:
            raise TrendingDiscoverError(
                "discover_policy_contract_mismatch",
                "Discover policy and Artifact schema versions differ",
                stage="audit",
            )
        expected_policy = {
            "absoluteGrowthGateStars": ABSOLUTE_GROWTH_GATE_STARS,
            "relativeGrowthGatePercent": RELATIVE_GROWTH_GATE_PERCENT,
            "consecutivePositiveIntervalGate": CONSECUTIVE_POSITIVE_INTERVAL_GATE,
            "recentDiscoveryHours": RECENT_DISCOVERY_HOURS,
            "nearValidationHours": NEAR_VALIDATION_HOURS,
        }
        if policy_version == POLICY_VERSION:
            expected_policy.update(
                {
                    "todayPublishedTopCount": TODAY_PUBLISHED_TOP_COUNT,
                    "outsideRecentWindowHours": OUTSIDE_RECENT_WINDOW_HOURS,
                    "outsideRequiresAcceleration": True,
                }
            )
        if artifact["signalPolicy"] != expected_policy:
            raise TrendingDiscoverError(
                "discover_signal_policy_mismatch",
                "Discover signal policy constants differ from the audited implementation",
                stage="audit",
            )
    else:
        raise TrendingDiscoverError(
            "discover_policy_contract_mismatch",
            "unsupported Discover policy version",
            stage="audit",
        )
    stage_ids: list[int] = []
    stage_keys = _stage_keys(policy_version)
    if artifact["sortingPolicy"]["sections"] != list(_stage_order(policy_version)):
        raise TrendingDiscoverError(
            "discover_stage_order_invalid",
            "Discover stage order differs from its versioned policy",
            stage="audit",
        )
    for stage, key in stage_keys.items():
        items = artifact["stages"][key]
        if any(item["stage"] != stage for item in items):
            raise TrendingDiscoverError(
                "discover_stage_mismatch", f"{key} contains an item from another stage", stage="audit"
            )
        expected = sorted(
            items,
            key=lambda item: (
                -int(item["observedStarDelta"]),
                -int(item["totalStars"]),
                str(item["repository"]),
            ),
        )
        if items != expected:
            raise TrendingDiscoverError(
                "discover_sort_order_invalid", f"{key} is not deterministically ordered", stage="audit"
            )
        stage_ids.extend(int(item["githubRepositoryId"]) for item in items)
    conflict_ids = [int(item["githubRepositoryId"]) for item in artifact["conflicts"]]
    if len(stage_ids) != len(set(stage_ids)) or set(stage_ids) & set(conflict_ids):
        raise TrendingDiscoverError(
            "discover_duplicate_repository_id",
            "Discover stage and conflict partitions must be disjoint",
            stage="audit",
        )

    for stage, key in stage_keys.items():
        items = artifact["stages"][key]
        if policy_version == LEGACY_POLICY_VERSION and any(
            field in item
            for item in items
            for field in (
                "relativeGrowthPercent",
                "positiveIntervalCount",
                "consecutivePositiveIntervalCount",
                "latestIntervalDelta",
                "publishReasonCodes",
                "signalFacts",
            )
        ):
            raise TrendingDiscoverError(
                "discover_policy_contract_mismatch",
                "legacy Discover items contain v2-only signal facts",
                stage="audit",
            )
        if policy_version in {V2_POLICY_VERSION, POLICY_VERSION}:
            for item in items:
                reasons = item["publishReasonCodes"]
                facts = item["signalFacts"]
                if reasons != _ordered_signal_facts(set(reasons)) or facts != _ordered_signal_facts(
                    set(facts)
                ):
                    raise TrendingDiscoverError(
                        "discover_signal_fact_order_invalid",
                        "Discover signal facts must be unique and deterministically ordered",
                        stage="audit",
                    )
                if reasons != facts:
                    raise TrendingDiscoverError(
                        "discover_publish_reason_mismatch",
                        "published signal facts must exactly explain publication",
                        stage="audit",
                    )
                reason_set = set(reasons)
                gate_reasons = {"absolute_growth_gate", "relative_growth_gate"}
                if stage == "just_discovered":
                    valid = reason_set == {"first_seen_recently"}
                elif stage == "outside_today_momentum":
                    valid = (
                        policy_version == POLICY_VERSION
                        and {
                            "outside_today_top20",
                            "exact_rank_available",
                            "continuous_recent_growth",
                            "recent_acceleration",
                        }.issubset(reason_set)
                        and bool(
                            {"recent_absolute_growth", "recent_relative_growth"}
                            & reason_set
                        )
                    )
                elif stage == "rising":
                    valid = (
                        "continuous_positive_growth" in reason_set
                        and bool(gate_reasons & reason_set)
                        and reason_set.isdisjoint(
                            {"first_seen_recently", "awaiting_today_settlement"}
                        )
                    )
                else:
                    valid = (
                        "continuous_positive_growth" in reason_set
                        and bool(gate_reasons & reason_set)
                        and "awaiting_today_settlement" in reason_set
                        and "first_seen_recently" not in reason_set
                    )
                if not valid:
                    raise TrendingDiscoverError(
                        "discover_publish_reason_mismatch",
                        f"{stage} publish reasons do not match the policy",
                        stage="audit",
                    )
                if policy_version == POLICY_VERSION:
                    eligibility = item["eligibilityClass"]
                    if stage == "outside_today_momentum":
                        valid_eligibility = (
                            eligibility == "exact_outside_published"
                            and item["todayExactRank"] > TODAY_PUBLISHED_TOP_COUNT
                            and item["todayExact24hDelta"] is not None
                            and item["recentObservedStarDelta"] is not None
                            and item["priorComparableWindowDelta"] is not None
                            and item["accelerationDelta"] is not None
                            and item["accelerationDelta"] > 0
                        )
                    else:
                        valid_eligibility = (
                            eligibility == "pre_exact"
                            and item["todayExactRank"] is None
                            and item["todayExact24hDelta"] is None
                        )
                    if not valid_eligibility:
                        raise TrendingDiscoverError(
                            "discover_eligibility_class_mismatch",
                            "Discover stage and eligibility facts are inconsistent",
                            stage="audit",
                        )
    if policy_version == V2_POLICY_VERSION:
        summary = artifact["suppressionSummary"]
        coverage = artifact["coverage"]
        reasons = summary["reasons"]
        if (
            summary["candidateCount"] != coverage["candidateCount"]
            or summary["publishedCount"] != coverage["publishedCount"]
            or summary["publishedCount"] != len(stage_ids)
            or summary["suppressedExactCount"] != coverage["excludedExactCount"]
            or summary["suppressedExactCount"] != reasons["already_in_today"]
            or summary["conflictCount"] != coverage["conflictCount"]
            or summary["conflictCount"] != len(conflict_ids)
            or summary["conflictCount"]
            != reasons["identity_conflict"]
            + reasons["negative_growth"]
            + reasons["disabled"]
            or reasons["metadata_incomplete"] != coverage["metadataFailureCount"]
            or summary["stageEligibleCount"] < summary["publishedCount"]
            or summary["stageEligibleCount"]
            < summary["publishedCount"] + summary["suppressedWeakSignalCount"]
        ):
            raise TrendingDiscoverError(
                "discover_suppression_summary_mismatch",
                "Discover suppression summary does not reconcile with published facts",
                stage="audit",
            )
    if policy_version == POLICY_VERSION:
        summary = artifact["suppressionSummary"]
        coverage = artifact["coverage"]
        reasons = summary["reasons"]
        eligibility = artifact["eligibilityCounts"]
        outside_published = len(artifact["stages"]["outsideTodayMomentum"])
        if (
            artifact["todayPublishedTopCount"] != TODAY_PUBLISHED_TOP_COUNT
            or artifact["todayExactCount"]
            != artifact["todayExplosionSource"]["exactCount"]
            or artifact["todayPublishedCount"]
            != min(artifact["todayExactCount"], TODAY_PUBLISHED_TOP_COUNT)
            or artifact["excludedPublishedCount"]
            != eligibility["todayPublished"]
            or artifact["excludedPublishedCount"] != reasons["today_published"]
            or artifact["exactOutsidePublishedEvaluatedCount"]
            != eligibility["exactOutsidePublished"]
            or artifact["preExactEvaluatedCount"] != eligibility["preExact"]
            or eligibility["invalid"] != coverage["invalidCount"]
            or sum(eligibility.values()) != coverage["candidateCount"]
            or coverage["publishedCount"] != len(stage_ids)
            or coverage["publishedCount"] != summary["publishedCount"]
            or coverage["conflictCount"] != len(conflict_ids)
            or coverage["conflictCount"] != eligibility["invalid"]
            or coverage["conflictCount"]
            != reasons["identity_conflict"]
            + reasons["negative_growth"]
            + reasons["disabled"]
            or reasons["metadata_incomplete"] != coverage["metadataFailureCount"]
            or reasons["already_exact_without_momentum"]
            != artifact["exactOutsidePublishedEvaluatedCount"] - outside_published
            or summary["candidateCount"] != coverage["candidateCount"]
            or summary["excludedPublishedCount"]
            != artifact["excludedPublishedCount"]
            or summary["suppressedSignalCount"]
            != summary["candidateCount"]
            - summary["excludedPublishedCount"]
            - summary["conflictCount"]
            - summary["publishedCount"]
        ):
            raise TrendingDiscoverError(
                "discover_suppression_summary_mismatch",
                "Discover v3 eligibility and suppression facts do not reconcile",
                stage="audit",
            )
    inventory_path_field = (
        "originalObservationPath"
        if policy_version == POLICY_VERSION
        else "generationRelativePath"
    )
    inventory_paths = [
        item[inventory_path_field] for item in artifact["sourceInventory"]
    ]
    if len(inventory_paths) != len(set(inventory_paths)):
        raise TrendingDiscoverError(
            "discover_duplicate_source_path", "Discover source inventory paths must be unique", stage="audit"
        )
    return artifact


def build_discover_artifact(
    *,
    generation_id: str,
    generated_at: datetime,
    sources: DiscoverSources,
    policy_version: str = POLICY_VERSION,
) -> dict[str, Any]:
    """Pure deterministic Discover derivation from verified source bytes."""

    if not GENERATION_ID_PATTERN.fullmatch(generation_id):
        raise TrendingDiscoverError(
            "discover_invalid_generation_id", "invalid Discover generation identity", stage="contract"
        )
    if not sources.captures or len(sources.captures) > MAX_SOURCE_CAPTURES:
        raise TrendingDiscoverError(
            "discover_source_count_invalid", "Discover requires one to fourteen source captures", stage="source"
        )
    if list(sources.captures) != sorted(sources.captures, key=lambda source: source.scheduled_at):
        raise TrendingDiscoverError(
            "discover_source_order_invalid", "Discover source captures must be ascending", stage="source"
        )
    generated = _utc(generated_at, field="generated_at")
    latest = sources.latest
    if generated < latest.captured_at:
        raise TrendingDiscoverError(
            "discover_generated_before_source", "generatedAt predates the latest capture", stage="contract"
        )
    indexes = [_observation_index(source) for source in sources.captures]
    latest_index = indexes[-1]
    exact_by_id, published_ids, published_set_digest = _today_sets(
        sources.today.payload["exactRanked"]
    )
    exact_ids = set(exact_by_id)
    name_ids: dict[str, set[int]] = {}
    for index in indexes:
        for repository_id, item in index.items():
            name_ids.setdefault(str(item["repository"]).casefold(), set()).add(repository_id)

    if policy_version not in {
        LEGACY_POLICY_VERSION,
        V2_POLICY_VERSION,
        POLICY_VERSION,
    }:
        raise TrendingDiscoverError(
            "discover_policy_contract_mismatch",
            f"unsupported Discover policy version: {policy_version}",
            stage="contract",
        )
    schema_version = {
        LEGACY_POLICY_VERSION: LEGACY_SCHEMA_VERSION,
        V2_POLICY_VERSION: V2_SCHEMA_VERSION,
        POLICY_VERSION: SCHEMA_VERSION,
    }[policy_version]
    stage_keys = _stage_keys(policy_version)
    stages: dict[str, list[dict[str, Any]]] = {
        key: [] for key in stage_keys.values()
    }
    conflicts: list[dict[str, Any]] = []
    excluded_exact = 0
    excluded_published = 0
    exact_outside_evaluated = 0
    pre_exact_evaluated = 0
    stage_eligible_count = 0
    suppressed_weak_ids: set[int] = set()
    suppression_counts = {
        reason: 0
        for reason in (
            SUPPRESSION_REASONS
            if policy_version == POLICY_VERSION
            else V2_SUPPRESSION_REASONS
        )
    }
    for repository_id, current in latest_index.items():
        observations = [
            (source, index[repository_id])
            for source, index in zip(sources.captures, indexes, strict=True)
            if repository_id in index
        ]
        first_source, first = observations[0]
        capture_ids = [str(source.payload["captureId"]) for source, _ in observations]
        identity_conflict = any(
            len(name_ids[str(item["repository"]).casefold()]) != 1
            for _, item in observations
        )
        if identity_conflict:
            suppression_counts["identity_conflict"] += 1
            conflicts.append(
                {
                    "reason": "source_identity_conflict",
                    "githubRepositoryId": repository_id,
                    "repository": current["repository"],
                    "currentStars": current["totalStars"],
                    "baselineStars": first["totalStars"],
                    "sourceCaptureIds": capture_ids,
                }
            )
            continue
        if current["disabled"] is True:
            suppression_counts["disabled"] += 1
            conflicts.append(
                {
                    "reason": "current_disabled",
                    "githubRepositoryId": repository_id,
                    "repository": current["repository"],
                    "currentStars": current["totalStars"],
                    "baselineStars": first["totalStars"],
                    "sourceCaptureIds": capture_ids,
                }
            )
            continue
        if policy_version != POLICY_VERSION and repository_id in exact_ids:
            excluded_exact += 1
            suppression_counts["already_in_today"] += 1
            continue
        delta = int(current["totalStars"]) - int(first["totalStars"])
        if delta < 0:
            suppression_counts["negative_growth"] += 1
            conflicts.append(
                {
                    "reason": "star_count_decreased",
                    "githubRepositoryId": repository_id,
                    "repository": current["repository"],
                    "currentStars": current["totalStars"],
                    "baselineStars": first["totalStars"],
                    "sourceCaptureIds": capture_ids,
                }
            )
            continue
        eligibility_class: str | None = None
        today_exact = exact_by_id.get(repository_id)
        if policy_version == POLICY_VERSION:
            if repository_id in published_ids:
                excluded_published += 1
                suppression_counts["today_published"] += 1
                continue
            if today_exact is not None:
                eligibility_class = "exact_outside_published"
                exact_outside_evaluated += 1
            else:
                eligibility_class = "pre_exact"
                pre_exact_evaluated += 1
        seconds = (latest.captured_at - first_source.captured_at).total_seconds()
        if seconds < 0 or seconds > 27 * 3600:
            raise TrendingDiscoverError(
                "discover_observed_window_invalid", "actual observation window is outside the bounded source window", stage="derive"
            )
        hours = round(seconds / 3600, 6)
        first_index = sources.captures.index(first_source)
        consecutive_count = _consecutive_count(repository_id, sources.captures, indexes)
        consecutive_start = sources.captures[len(sources.captures) - consecutive_count]
        consecutive_hours = (
            latest.captured_at - consecutive_start.captured_at
        ).total_seconds() / 3600
        positive_intervals, consecutive_positive_intervals, latest_interval_delta = (
            _positive_interval_facts(observations)
        )
        near_validation = consecutive_hours >= NEAR_VALIDATION_HOURS
        just_discovered = (
            latest.scheduled_at - first_source.scheduled_at
            <= timedelta(hours=RECENT_DISCOVERY_HOURS)
        )
        rising = len(observations) >= 3 and hours > 0 and delta > 0
        relative_growth_percent: float | None = None
        signal_facts: list[str] = []
        if policy_version == LEGACY_POLICY_VERSION:
            legacy_rising = len(observations) >= 2 and hours > 0 and delta > 0
            stage = (
                "near_validation"
                if near_validation
                else "just_discovered"
                if first_index >= max(0, len(sources.captures) - 2) or hours <= 4
                else "rising"
                if legacy_rising
                else None
            )
        elif policy_version == V2_POLICY_VERSION:
            relative_growth_percent = (
                round(delta / int(first["totalStars"]) * 100, 6)
                if int(first["totalStars"]) > 0
                else None
            )
            absolute_gate = delta >= ABSOLUTE_GROWTH_GATE_STARS
            relative_gate = (
                relative_growth_percent is not None
                and relative_growth_percent >= RELATIVE_GROWTH_GATE_PERCENT
            )
            continuous_gate = (
                consecutive_positive_intervals >= CONSECUTIVE_POSITIVE_INTERVAL_GATE
            )
            quality_gate = (absolute_gate or relative_gate) and continuous_gate
            base_stage_eligible = just_discovered or near_validation or rising
            if base_stage_eligible:
                stage_eligible_count += 1
            if just_discovered:
                stage = "just_discovered"
                signal_facts = ["first_seen_recently"]
            elif near_validation and quality_gate:
                stage = "near_validation"
                facts = {"continuous_positive_growth", "awaiting_today_settlement"}
                if absolute_gate:
                    facts.add("absolute_growth_gate")
                if relative_gate:
                    facts.add("relative_growth_gate")
                signal_facts = _ordered_signal_facts(facts)
            elif rising and quality_gate:
                stage = "rising"
                facts = {"continuous_positive_growth"}
                if absolute_gate:
                    facts.add("absolute_growth_gate")
                if relative_gate:
                    facts.add("relative_growth_gate")
                signal_facts = _ordered_signal_facts(facts)
            else:
                stage = None
                if base_stage_eligible:
                    suppressed_weak_ids.add(repository_id)
                    if not absolute_gate:
                        suppression_counts["weak_absolute_growth"] += 1
                    if not relative_gate:
                        suppression_counts["weak_relative_growth"] += 1
                    if not continuous_gate:
                        suppression_counts["no_continuous_growth"] += 1
        else:
            relative_growth_percent = (
                round(delta / int(first["totalStars"]) * 100, 6)
                if int(first["totalStars"]) > 0
                else None
            )
            (
                recent_delta,
                prior_delta,
                acceleration_delta,
                recent_relative_growth,
                recent_consecutive_positive,
            ) = _comparable_window_facts(
                observations, latest_scheduled_at=latest.scheduled_at
            )
            if eligibility_class == "exact_outside_published":
                recent_absolute_gate = (
                    recent_delta is not None
                    and recent_delta >= ABSOLUTE_GROWTH_GATE_STARS
                )
                recent_relative_gate = (
                    recent_relative_growth is not None
                    and recent_relative_growth >= RELATIVE_GROWTH_GATE_PERCENT
                )
                recent_continuous_gate = (
                    recent_consecutive_positive
                    >= CONSECUTIVE_POSITIVE_INTERVAL_GATE
                )
                acceleration_gate = (
                    acceleration_delta is not None and acceleration_delta > 0
                )
                outside_gate = (
                    (recent_absolute_gate or recent_relative_gate)
                    and recent_continuous_gate
                    and acceleration_gate
                )
                stage_eligible_count += 1
                if outside_gate:
                    stage = "outside_today_momentum"
                    facts = {
                        "outside_today_top20",
                        "exact_rank_available",
                        "continuous_recent_growth",
                        "recent_acceleration",
                    }
                    if recent_absolute_gate:
                        facts.add("recent_absolute_growth")
                    if recent_relative_gate:
                        facts.add("recent_relative_growth")
                    signal_facts = _ordered_signal_facts(facts)
                else:
                    stage = None
                    suppressed_weak_ids.add(repository_id)
                    suppression_counts["already_exact_without_momentum"] += 1
                    if not recent_absolute_gate:
                        suppression_counts["weak_recent_absolute_growth"] += 1
                    if not recent_relative_gate:
                        suppression_counts["weak_recent_relative_growth"] += 1
                    if not recent_continuous_gate:
                        suppression_counts["no_recent_continuous_growth"] += 1
                    if not acceleration_gate:
                        suppression_counts["no_recent_acceleration"] += 1
            else:
                absolute_gate = delta >= ABSOLUTE_GROWTH_GATE_STARS
                relative_gate = (
                    relative_growth_percent is not None
                    and relative_growth_percent >= RELATIVE_GROWTH_GATE_PERCENT
                )
                continuous_gate = (
                    consecutive_positive_intervals
                    >= CONSECUTIVE_POSITIVE_INTERVAL_GATE
                )
                quality_gate = (absolute_gate or relative_gate) and continuous_gate
                base_stage_eligible = just_discovered or near_validation or rising
                if base_stage_eligible:
                    stage_eligible_count += 1
                if just_discovered:
                    stage = "just_discovered"
                    signal_facts = ["first_seen_recently"]
                elif near_validation and quality_gate:
                    stage = "near_validation"
                    facts = {
                        "continuous_positive_growth",
                        "awaiting_today_settlement",
                    }
                    if absolute_gate:
                        facts.add("absolute_growth_gate")
                    if relative_gate:
                        facts.add("relative_growth_gate")
                    signal_facts = _ordered_signal_facts(facts)
                elif rising and quality_gate:
                    stage = "rising"
                    facts = {"continuous_positive_growth"}
                    if absolute_gate:
                        facts.add("absolute_growth_gate")
                    if relative_gate:
                        facts.add("relative_growth_gate")
                    signal_facts = _ordered_signal_facts(facts)
                else:
                    stage = None
                    suppressed_weak_ids.add(repository_id)
                    suppression_counts["weak_pre_exact_growth"] += 1
        if stage is None:
            continue
        item = {
            "githubRepositoryId": repository_id,
            "repository": current["repository"],
            "url": current["htmlUrl"],
            "stage": stage,
            "firstSeenAt": first_source.payload["capturedAt"],
            "lastObservedAt": latest.payload["capturedAt"],
            "observedWindowStart": first_source.payload["capturedAt"],
            "observedWindowEnd": latest.payload["capturedAt"],
            "observedWindowHours": hours,
            "observedStarDelta": delta,
            "totalStars": current["totalStars"],
            "captureCount": len(observations),
            "consecutiveCaptureCount": consecutive_count,
            "language": current["primaryLanguage"],
            "topics": copy.deepcopy(current["topics"]),
            "license": current["licenseSpdxId"],
            "isFork": current["fork"],
            "isArchived": current["archived"],
            "isDisabled": False,
            "latestPushAt": current["pushedAt"],
            "sourceCaptureIds": capture_ids,
            "sourceEvidenceDigest": _evidence_digest(observations),
        }
        if policy_version in {V2_POLICY_VERSION, POLICY_VERSION}:
            item.update(
                {
                    "relativeGrowthPercent": relative_growth_percent,
                    "positiveIntervalCount": positive_intervals,
                    "consecutivePositiveIntervalCount": consecutive_positive_intervals,
                    "latestIntervalDelta": latest_interval_delta,
                    "publishReasonCodes": signal_facts,
                    "signalFacts": signal_facts,
                }
            )
        if policy_version == POLICY_VERSION:
            item.update(
                {
                    "eligibilityClass": eligibility_class,
                    "todayExactRank": (
                        int(today_exact["rank"]) if today_exact is not None else None
                    ),
                    "todayExact24hDelta": (
                        int(today_exact["observedStarDelta"])
                        if today_exact is not None
                        else None
                    ),
                    "recentWindowHours": min(OUTSIDE_RECENT_WINDOW_HOURS, hours),
                    "recentObservedStarDelta": recent_delta,
                    "priorComparableWindowDelta": prior_delta,
                    "accelerationDelta": acceleration_delta,
                    "recentRelativeGrowthPercent": recent_relative_growth,
                }
            )
        stages[stage_keys[stage]].append(item)

    for items in stages.values():
        items.sort(
            key=lambda item: (
                -int(item["observedStarDelta"]),
                -int(item["totalStars"]),
                str(item["repository"]),
            )
        )
    conflicts.sort(
        key=lambda item: (
            str(item["reason"]),
            str(item["repository"]),
            int(item["githubRepositoryId"]),
        )
    )
    published_count = sum(len(items) for items in stages.values())
    degraded = any(source.payload["coverageState"] == "degraded" for source in sources.captures)
    artifact = {
        "schemaVersion": schema_version,
        "policyVersion": policy_version,
        "discoverGenerationId": generation_id,
        "generatedAt": _timestamp(generated),
        "latestCaptureId": latest.payload["captureId"],
        "latestCaptureScheduledAt": latest.payload["scheduledAt"],
        "latestCaptureCapturedAt": latest.payload["capturedAt"],
        "sourceWindowStart": sources.captures[0].payload["capturedAt"],
        "sourceWindowEnd": latest.payload["capturedAt"],
        "sourceCaptureCount": len(sources.captures),
        "todayExplosionGenerationId": sources.today.generation_id,
        "todayExplosionDigest": sources.today.file_sha256,
        "updateCadenceMinutes": CADENCE_MINUTES,
        "sortingPolicy": {
            "sections": list(_stage_order(policy_version)),
            "withinStage": ["observedStarDelta DESC", "totalStars DESC", "repository ASC"],
        },
        "stages": stages,
        "coverage": {
            "state": "degraded" if degraded else "healthy",
            "querySuccessCount": latest.payload["successfulQueryCount"],
            "queryFailureCount": latest.payload["failedQueryCount"],
            "metadataFailureCount": latest.payload["metadataFailureCount"],
            "sourceCaptureCount": len(sources.captures),
            "candidateCount": len(latest_index),
            "publishedCount": published_count,
            "conflictCount": len(conflicts),
        },
        "conflicts": conflicts,
        "sourceInventory": [
            _source_reference(source, index, policy_version=policy_version)
            for index, source in enumerate(sources.captures, start=1)
        ],
        "todayExplosionSource": _today_reference(sources.today),
    }
    if policy_version != POLICY_VERSION:
        artifact["coverage"]["excludedExactCount"] = excluded_exact
    else:
        artifact.update(
            {
                "todayExactCount": len(exact_ids),
                "todayPublishedTopCount": TODAY_PUBLISHED_TOP_COUNT,
                "todayPublishedSetDigest": published_set_digest,
                "todayPublishedCount": len(published_ids),
                "excludedPublishedCount": excluded_published,
                "exactOutsidePublishedEvaluatedCount": exact_outside_evaluated,
                "preExactEvaluatedCount": pre_exact_evaluated,
                "eligibilityCounts": {
                    "todayPublished": excluded_published,
                    "exactOutsidePublished": exact_outside_evaluated,
                    "preExact": pre_exact_evaluated,
                    "invalid": len(conflicts),
                },
            }
        )
        artifact["coverage"].update(
            {
                "todayExactCount": len(exact_ids),
                "todayPublishedCount": len(published_ids),
                "excludedPublishedCount": excluded_published,
                "exactOutsidePublishedEvaluatedCount": exact_outside_evaluated,
                "preExactEvaluatedCount": pre_exact_evaluated,
                "invalidCount": len(conflicts),
            }
        )
    if policy_version in {V2_POLICY_VERSION, POLICY_VERSION}:
        suppression_counts["metadata_incomplete"] = int(
            latest.payload["metadataFailureCount"]
        )
        artifact["signalPolicy"] = {
            "absoluteGrowthGateStars": ABSOLUTE_GROWTH_GATE_STARS,
            "relativeGrowthGatePercent": RELATIVE_GROWTH_GATE_PERCENT,
            "consecutivePositiveIntervalGate": CONSECUTIVE_POSITIVE_INTERVAL_GATE,
            "recentDiscoveryHours": RECENT_DISCOVERY_HOURS,
            "nearValidationHours": NEAR_VALIDATION_HOURS,
        }
        if policy_version == POLICY_VERSION:
            artifact["signalPolicy"].update(
                {
                    "todayPublishedTopCount": TODAY_PUBLISHED_TOP_COUNT,
                    "outsideRecentWindowHours": OUTSIDE_RECENT_WINDOW_HOURS,
                    "outsideRequiresAcceleration": True,
                }
            )
            artifact["suppressionSummary"] = {
                "candidateCount": len(latest_index),
                "publishedCount": published_count,
                "suppressedSignalCount": len(suppressed_weak_ids),
                "excludedPublishedCount": excluded_published,
                "conflictCount": len(conflicts),
                "reasons": suppression_counts,
            }
        else:
            artifact["suppressionSummary"] = {
                "candidateCount": len(latest_index),
                "stageEligibleCount": stage_eligible_count,
                "publishedCount": published_count,
                "suppressedWeakSignalCount": len(suppressed_weak_ids),
                "suppressedExactCount": excluded_exact,
                "conflictCount": len(conflicts),
                "reasons": suppression_counts,
            }
    return validate_discover_artifact(_attach_payload_digest(artifact))


def _generation_artifacts(root: Path) -> dict[str, str]:
    files: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise TrendingDiscoverError(
                "discover_unsafe_path", f"Discover generation contains a link: {relative}", stage="manifest"
            )
        if stat.S_ISREG(metadata.st_mode):
            if path.name.startswith(".") and path.name.endswith(".tmp"):
                raise TrendingDiscoverError(
                    "discover_temporary_file_present", f"temporary file remains: {relative}", stage="manifest"
                )
            if relative != "manifest.json":
                files.append(path)
        elif stat.S_ISDIR(metadata.st_mode):
            if relative != "sources":
                raise TrendingDiscoverError(
                    "discover_generation_layout_invalid",
                    f"unexpected Discover generation directory: {relative}",
                    stage="manifest",
                )
        else:
            raise TrendingDiscoverError(
                "discover_unsafe_path", f"unsupported generation entry: {relative}", stage="manifest"
            )
    inventory: dict[str, str] = {}
    for path in sorted(files):
        _require_regular(path, root)
        try:
            inventory[path.relative_to(root).as_posix()] = stable_read(path).sha256
        except (OSError, StableReadError) as error:
            raise TrendingDiscoverError(
                "discover_artifact_unstable",
                f"generation artifact changed during inventory: {path.name}: {error}",
                stage="manifest",
            ) from None
    return inventory


def _manifest_payload(generation_id: str, artifact: dict[str, Any], artifacts: dict[str, str]) -> dict[str, Any]:
    payload = {
        "schemaVersion": artifact["schemaVersion"],
        "policyVersion": artifact["policyVersion"],
        "generationId": generation_id,
        "createdAt": artifact["generatedAt"],
        "state": "ready",
        "latestCaptureId": artifact["latestCaptureId"],
        "todayExplosionGenerationId": artifact["todayExplosionGenerationId"],
        "artifacts": artifacts,
        "audit": {
            "status": artifact["coverage"]["state"],
            "validatedSourceCount": artifact["sourceCaptureCount"] + 1,
            "publishedCount": artifact["coverage"]["publishedCount"],
            "conflictCount": artifact["coverage"]["conflictCount"],
        },
    }
    if artifact["policyVersion"] == V2_POLICY_VERSION:
        payload["audit"]["suppressedWeakSignalCount"] = artifact[
            "suppressionSummary"
        ]["suppressedWeakSignalCount"]
    elif artifact["policyVersion"] == POLICY_VERSION:
        payload["audit"].update(
            {
                "suppressedSignalCount": artifact["suppressionSummary"][
                    "suppressedSignalCount"
                ],
                "excludedPublishedCount": artifact["excludedPublishedCount"],
                "exactOutsidePublishedEvaluatedCount": artifact[
                    "exactOutsidePublishedEvaluatedCount"
                ],
                "outsideTodayMomentumCount": len(
                    artifact["stages"]["outsideTodayMomentum"]
                ),
            }
        )
    return payload


def _load_generation_sources(root: Path, artifact: dict[str, Any]) -> DiscoverSources:
    captures: list[CaptureSource] = []
    if artifact["policyVersion"] == POLICY_VERSION:
        data_candidates: list[Path] = []
        for candidate in root.parents:
            generation_store = candidate / DISCOVER_RELATIVE_ROOT / "generations"
            try:
                root.relative_to(generation_store)
            except ValueError:
                continue
            data_candidates.append(candidate)
        if len(data_candidates) != 1:
            raise TrendingDiscoverError(
                "discover_unsafe_path",
                "v3 generation is not rooted in the canonical Discover store",
                stage="audit",
            )
        data_dir = data_candidates[0]
        for index, reference in enumerate(artifact["sourceInventory"], start=1):
            scheduled_at = parse_timestamp(
                reference["scheduledAt"], field="scheduledAt"
            )
            try:
                source = _load_source_capture(data_dir, scheduled_at)
            except TrendingExplosionError as error:
                raise TrendingDiscoverError(
                    "discover_source_capture_invalid", str(error), stage="audit"
                ) from None
            if source is None:
                raise TrendingDiscoverError(
                    "discover_source_capture_missing",
                    f"canonical capture is missing: {reference['captureId']}",
                    stage="audit",
                )
            expected = _source_reference(
                source, index, policy_version=artifact["policyVersion"]
            )
            if reference != expected:
                raise TrendingDiscoverError(
                    "discover_source_reference_mismatch",
                    "canonical source capture no longer matches its descriptor",
                    stage="audit",
                )
            captures.append(source)
    else:
        for reference in artifact["sourceInventory"]:
            path = root / reference["generationRelativePath"]
            payload, content, file_sha = _read_json(
                path, root, ArtifactKind.TRENDING_CAPTURE_BUNDLE
            )
            try:
                payload = validate_capture_bundle(
                    payload, expected_capture_id=reference["captureId"]
                )
            except TrendingObservationError as error:
                raise TrendingDiscoverError(
                    "discover_source_capture_invalid", str(error), stage="audit"
                ) from None
            if (
                file_sha != reference["fileSha256"]
                or payload["digest"]["value"] != reference["payloadDigestSha256"]
                or payload["scheduledAt"] != reference["scheduledAt"]
                or payload["capturedAt"] != reference["capturedAt"]
            ):
                raise TrendingDiscoverError(
                    "discover_source_reference_mismatch",
                    "source capture no longer matches its reference",
                    stage="audit",
                )
            captures.append(
                CaptureSource(
                    path=path,
                    original_observation_path=reference["originalObservationPath"],
                    content=content,
                    file_sha256=file_sha,
                    payload=payload,
                )
            )
    today_ref = artifact["todayExplosionSource"]
    manifest_path = root / today_ref["generationManifestRelativePath"]
    manifest_payload, manifest_content, manifest_sha = _read_json(
        manifest_path, root, ArtifactKind.GENERATION_MANIFEST
    )
    today_path = root / today_ref["generationRelativePath"]
    _require_regular(today_path, root)
    try:
        snapshot = stable_read(today_path)
        today_payload = validate_explosion_artifact(
            strict_json_loads(snapshot.content.decode("utf-8", errors="strict"))
        )
    except (OSError, StableReadError, TrendingExplosionError, UnicodeDecodeError, ValueError) as error:
        raise TrendingDiscoverError(
            "discover_today_explosion_invalid", str(error), stage="audit"
        ) from None
    if (
        manifest_sha != today_ref["generationManifestSha256"]
        or manifest_payload["generationId"] != today_ref["generationId"]
        or manifest_payload["state"] != "ready"
        or EXPLOSION_PATH not in manifest_payload["artifacts"]
        or manifest_payload["hashes"].get(EXPLOSION_PATH) != snapshot.sha256
        or snapshot.sha256 != today_ref["fileSha256"]
        or today_payload["generationId"] != today_ref["generationId"]
        or today_payload["window"]["endedAt"] != today_ref["windowEndedAt"]
        or len(today_payload["exactRanked"]) != today_ref["exactCount"]
    ):
        raise TrendingDiscoverError(
            "discover_today_reference_mismatch", "Today exclusion source no longer matches its reference", stage="audit"
        )
    return DiscoverSources(
        tuple(captures),
        TodayExplosionSource(
            generation_id=today_ref["generationId"],
            generation_manifest_sha256=today_ref["generationManifestSha256"],
            generation_manifest_content=manifest_content,
            generation_manifest=manifest_payload,
            content=snapshot.content,
            file_sha256=snapshot.sha256,
            payload=today_payload,
        ),
    )


def audit_discover_generation(root: Path) -> dict[str, Any]:
    """Read-only complete recomputation of one immutable Discover generation."""

    generation_id = root.name
    try:
        manifest, manifest_content, manifest_sha = _read_json(
            root / "manifest.json", root, ArtifactKind.TRENDING_DISCOVER_MANIFEST
        )
        if manifest["generationId"] != generation_id:
            raise TrendingDiscoverError(
                "discover_generation_id_mismatch", "manifest identity does not match its directory", stage="audit"
            )
        inventory = _generation_artifacts(root)
        if inventory != manifest["artifacts"]:
            raise TrendingDiscoverError(
                "discover_manifest_inventory_mismatch", "manifest artifact hashes do not match", stage="audit"
            )
        artifact, _, _ = _read_json(root / DISCOVER_FILE, root, ArtifactKind.TRENDING_DISCOVER)
        artifact = validate_discover_artifact(artifact)
        if artifact["discoverGenerationId"] != generation_id:
            raise TrendingDiscoverError(
                "discover_generation_id_mismatch", "artifact identity does not match its directory", stage="audit"
            )
        sources = _load_generation_sources(root, artifact)
        rebuilt = build_discover_artifact(
            generation_id=generation_id,
            generated_at=parse_timestamp(artifact["generatedAt"], field="generatedAt"),
            sources=sources,
            policy_version=artifact["policyVersion"],
        )
        if rebuilt != artifact:
            raise TrendingDiscoverError(
                "discover_recomputation_mismatch", "Discover facts do not match source recomputation", stage="audit"
            )
        expected_manifest = _manifest_payload(generation_id, artifact, inventory)
        if manifest != expected_manifest:
            raise TrendingDiscoverError(
                "discover_manifest_semantics_mismatch", "manifest summary does not match Discover facts", stage="audit"
            )
        report = {
            "status": artifact["coverage"]["state"],
            "schemaVersion": artifact["schemaVersion"],
            "policyVersion": artifact["policyVersion"],
            "generationId": generation_id,
            "manifestSha256": manifest_sha,
            "latestCaptureId": artifact["latestCaptureId"],
            "publishedCount": artifact["coverage"]["publishedCount"],
            "conflictCount": artifact["coverage"]["conflictCount"],
            "stageCounts": {
                key: len(artifact["stages"][value])
                for key, value in _stage_keys(artifact["policyVersion"]).items()
            },
        }
        if artifact["policyVersion"] in {V2_POLICY_VERSION, POLICY_VERSION}:
            report["suppressionSummary"] = copy.deepcopy(artifact["suppressionSummary"])
        if artifact["policyVersion"] == POLICY_VERSION:
            report["eligibilityCounts"] = copy.deepcopy(
                artifact["eligibilityCounts"]
            )
        return report
    except TrendingDiscoverError:
        raise
    except Exception as error:
        raise TrendingDiscoverError(
            "discover_audit_failed", str(error), stage="audit"
        ) from None


def resolve_current_discover(data_dir: Path) -> ResolvedDiscoverGeneration:
    root = discover_store_root(data_dir)
    pointer_path = root / "current.json"
    if not os.path.lexists(pointer_path):
        raise TrendingDiscoverError(
            "discover_current_missing", "Discover current pointer is missing", stage="pointer"
        )
    _audit_store_ancestors(root)
    pointer, _, _ = _read_json(pointer_path, root, ArtifactKind.TRENDING_DISCOVER_CURRENT)
    generation_id = pointer["generationId"]
    target = _safe_generation_path(root, generation_id)
    if not target.is_dir():
        raise TrendingDiscoverError(
            "discover_generation_missing", "Discover current generation is missing", stage="pointer"
        )
    report = audit_discover_generation(target)
    if report["manifestSha256"] != pointer["manifestSha256"]:
        raise TrendingDiscoverError(
            "discover_manifest_digest_mismatch", "current pointer does not bind the generation manifest", stage="pointer"
        )
    manifest, _, _ = _read_json(target / "manifest.json", target, ArtifactKind.TRENDING_DISCOVER_MANIFEST)
    artifact, _, _ = _read_json(target / DISCOVER_FILE, target, ArtifactKind.TRENDING_DISCOVER)
    if (
        pointer["schemaVersion"] != manifest["schemaVersion"]
        or pointer["schemaVersion"] != artifact["schemaVersion"]
        or pointer["policyVersion"] != manifest["policyVersion"]
        or pointer["policyVersion"] != artifact["policyVersion"]
    ):
        raise TrendingDiscoverError(
            "discover_policy_contract_mismatch",
            "Discover pointer, manifest, and artifact contract versions differ",
            stage="pointer",
        )
    return ResolvedDiscoverGeneration(data_dir.resolve(), generation_id, target, pointer, manifest, artifact)


def audit_discover_store(data_dir: Path) -> dict[str, Any]:
    try:
        current = resolve_current_discover(data_dir)
        report = audit_discover_generation(current.root)
        return {**report, "current": True, "issues": []}
    except TrendingDiscoverError as error:
        return {
            "status": "failed",
            "generationId": None,
            "current": False,
            "issues": [{"code": error.code, "stage": error.stage, "message": str(error)}],
        }


def _new_generation_id(generated_at: datetime, sources: DiscoverSources) -> str:
    signature = (
        POLICY_VERSION
        + sources.latest.payload["captureId"]
        + sources.latest.file_sha256
        + sources.today.generation_id
        + sources.today.file_sha256
    ).encode("utf-8")
    suffix = _sha256(signature)[:12]
    return generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + suffix


def _same_sources(artifact: dict[str, Any], sources: DiscoverSources) -> bool:
    return (
        artifact.get("policyVersion") == POLICY_VERSION
        and artifact.get("latestCaptureId") == sources.latest.payload["captureId"]
        and artifact.get("todayExplosionGenerationId") == sources.today.generation_id
        and artifact.get("todayExplosionDigest") == sources.today.file_sha256
    )


def _require_today_source_current(data_dir: Path, expected: TodayExplosionSource) -> None:
    actual = _load_today_source(data_dir)
    if (
        actual.generation_id != expected.generation_id
        or actual.generation_manifest_sha256 != expected.generation_manifest_sha256
        or actual.file_sha256 != expected.file_sha256
    ):
        raise TrendingDiscoverError(
            "stale_today_exclusion",
            "Today exclusion generation changed during Discover derivation",
            stage="publish",
        )


def _summary(state: str, artifact: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "state": state,
        "generationId": artifact["discoverGenerationId"],
        "latestCaptureId": artifact["latestCaptureId"],
        "todayExplosionGenerationId": artifact["todayExplosionGenerationId"],
        "coverageState": artifact["coverage"]["state"],
        "stageCounts": {
            stage: len(artifact["stages"][key])
            for stage, key in _stage_keys(artifact["policyVersion"]).items()
        },
        "publishedCount": artifact["coverage"]["publishedCount"],
        "conflictCount": artifact["coverage"]["conflictCount"],
        "coverage": copy.deepcopy(artifact["coverage"]),
    }
    if artifact.get("policyVersion") != POLICY_VERSION:
        summary["excludedExactCount"] = artifact["coverage"]["excludedExactCount"]
    else:
        summary.update(
            {
                "todayExactCount": artifact["todayExactCount"],
                "todayPublishedCount": artifact["todayPublishedCount"],
                "excludedPublishedCount": artifact["excludedPublishedCount"],
                "exactOutsidePublishedEvaluatedCount": artifact[
                    "exactOutsidePublishedEvaluatedCount"
                ],
                "preExactEvaluatedCount": artifact["preExactEvaluatedCount"],
            }
        )
    if artifact.get("policyVersion") in {V2_POLICY_VERSION, POLICY_VERSION}:
        summary["suppressionSummary"] = copy.deepcopy(artifact["suppressionSummary"])
    return summary


def derive_trending_discover(
    data_dir: Path,
    *,
    generated_at: datetime | None = None,
    dry_run: bool = False,
    prepared_sources: DiscoverSources | None = None,
) -> dict[str, Any]:
    canonical = data_dir.expanduser().resolve()
    sources = prepared_sources or load_discover_sources(canonical)
    generated = _utc(generated_at or datetime.now(timezone.utc), field="generated_at")
    store = discover_store_root(canonical)
    base_id: str | None = None
    try:
        current = resolve_current_discover(canonical)
        base_id = current.generation_id
        if _same_sources(current.artifact, sources):
            _require_today_source_current(canonical, sources.today)
            return _summary("already_derived", current.artifact)
    except TrendingDiscoverError as error:
        if error.code != "discover_current_missing":
            raise
    generation_id = _new_generation_id(generated, sources)
    artifact = build_discover_artifact(
        generation_id=generation_id,
        generated_at=generated,
        sources=sources,
    )
    if dry_run:
        return _summary("dry_run", artifact)

    store = _ensure_real_directories(canonical)
    candidate = store / "generations" / ".candidates" / generation_id
    final = _safe_generation_path(store, generation_id)
    if os.path.lexists(candidate) or os.path.lexists(final):
        raise TrendingDiscoverError(
            "discover_generation_exists", "Discover generation path already exists", stage="publish"
        )
    try:
        (candidate / "sources").mkdir(parents=True, exist_ok=False)
        _atomic_write(candidate / DISCOVER_FILE, _canonical_bytes(artifact))
        if artifact["policyVersion"] != POLICY_VERSION:
            for index, source in enumerate(sources.captures, start=1):
                _atomic_write(
                    candidate / f"sources/capture-{index:02d}.json", source.content
                )
        _atomic_write(candidate / TODAY_SOURCE_FILE, sources.today.content)
        _atomic_write(
            candidate / TODAY_MANIFEST_FILE,
            sources.today.generation_manifest_content,
        )
        inventory = _generation_artifacts(candidate)
        manifest = _manifest_payload(generation_id, artifact, inventory)
        require_valid(ArtifactKind.TRENDING_DISCOVER_MANIFEST, manifest)
        _atomic_write(candidate / "manifest.json", _canonical_bytes(manifest))
        audit_discover_generation(candidate)

        with data_dir_lock(store):
            # Freeze the daily pointer only for the short source-CAS and atomic
            # publication section. No network or derivation runs under either lock.
            with data_dir_lock(canonical):
                _require_today_source_current(canonical, sources.today)
                current_id: str | None = None
                try:
                    current = resolve_current_discover(canonical)
                    current_id = current.generation_id
                    if _same_sources(current.artifact, sources):
                        shutil.rmtree(candidate)
                        return _summary("already_derived", current.artifact)
                except TrendingDiscoverError as error:
                    if error.code != "discover_current_missing":
                        raise
                if current_id != base_id:
                    raise TrendingDiscoverError(
                        "stale_discover_generation",
                        "Discover current pointer changed during derivation",
                        stage="publish",
                    )
                final.parent.mkdir(parents=True, exist_ok=True)
                if os.path.lexists(final):
                    raise TrendingDiscoverError(
                        "discover_generation_exists",
                        "Discover generation already exists",
                        stage="publish",
                    )
                os.replace(candidate, final)
                manifest_snapshot = stable_read(final / "manifest.json")
                pointer = {
                    "schemaVersion": artifact["schemaVersion"],
                    "policyVersion": artifact["policyVersion"],
                    "generationId": generation_id,
                    "publishedAt": _timestamp(max(generated, datetime.now(timezone.utc))),
                    "previousGenerationId": base_id,
                    "manifestSha256": manifest_snapshot.sha256,
                }
                require_valid(ArtifactKind.TRENDING_DISCOVER_CURRENT, pointer)
                _atomic_write(store / "current.json", _canonical_bytes(pointer))
        resolved = resolve_current_discover(canonical)
        return _summary("published", resolved.artifact)
    except Exception:
        if candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)
        raise


def rollback_discover(data_dir: Path, generation_id: str) -> dict[str, Any]:
    canonical = data_dir.expanduser().resolve()
    store = _ensure_real_directories(canonical)
    target = _safe_generation_path(store, generation_id)
    report = audit_discover_generation(target)
    with data_dir_lock(store):
        previous: str | None = None
        try:
            current = resolve_current_discover(canonical)
            previous = current.generation_id
        except TrendingDiscoverError:
            # Explicit rollback is the recovery boundary. The target was fully
            # audited above; damaged current metadata is never trusted or read
            # through a link and therefore contributes no previous ID.
            previous = None
        pointer = {
            "schemaVersion": report["schemaVersion"],
            "policyVersion": report["policyVersion"],
            "generationId": generation_id,
            "publishedAt": _timestamp(datetime.now(timezone.utc)),
            "previousGenerationId": previous,
            "manifestSha256": report["manifestSha256"],
        }
        require_valid(ArtifactKind.TRENDING_DISCOVER_CURRENT, pointer)
        _atomic_write(store / "current.json", _canonical_bytes(pointer))
    resolved = resolve_current_discover(canonical)
    return _summary("rolled_back", resolved.artifact)


__all__ = [
    "DISCOVER_RELATIVE_ROOT",
    "DiscoverSources",
    "POLICY_VERSION",
    "ResolvedDiscoverGeneration",
    "TodayExplosionSource",
    "TrendingDiscoverError",
    "audit_discover_generation",
    "audit_discover_store",
    "build_discover_artifact",
    "derive_trending_discover",
    "discover_store_root",
    "load_discover_sources",
    "resolve_current_discover",
    "rollback_discover",
    "validate_discover_artifact",
]
