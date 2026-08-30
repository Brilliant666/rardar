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


SCHEMA_VERSION = 1
POLICY_VERSION = "trending-discover-v1"
DISCOVER_RELATIVE_ROOT = Path("artifacts/trending/discover/v1")
DISCOVER_FILE = "discover.json"
TODAY_SOURCE_FILE = "sources/today-explosion.json"
TODAY_MANIFEST_FILE = "sources/today-manifest.json"
MAX_SOURCE_CAPTURES = TRACKING_WINDOW_HOURS // 2 + 1
GENERATION_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STAGE_KEYS = {
    "just_discovered": "justDiscovered",
    "rising": "rising",
    "near_validation": "nearValidation",
}
STAGE_ORDER = ("just_discovered", "rising", "near_validation")


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


def _source_reference(source: CaptureSource, index: int) -> dict[str, Any]:
    return {
        "captureId": source.payload["captureId"],
        "scheduledAt": source.payload["scheduledAt"],
        "capturedAt": source.payload["capturedAt"],
        "coverageState": source.payload["coverageState"],
        "generationRelativePath": f"sources/capture-{index:02d}.json",
        "originalObservationPath": source.original_observation_path,
        "payloadDigestSha256": source.payload["digest"]["value"],
        "fileSha256": source.file_sha256,
    }


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
    stage_ids: list[int] = []
    for stage, key in STAGE_KEYS.items():
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
    inventory_paths = [item["generationRelativePath"] for item in artifact["sourceInventory"]]
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
    exact_ids = {int(item["githubRepositoryId"]) for item in sources.today.payload["exactRanked"]}
    name_ids: dict[str, set[int]] = {}
    for index in indexes:
        for repository_id, item in index.items():
            name_ids.setdefault(str(item["repository"]).casefold(), set()).add(repository_id)

    stages: dict[str, list[dict[str, Any]]] = {key: [] for key in STAGE_KEYS.values()}
    conflicts: list[dict[str, Any]] = []
    excluded_exact = 0
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
        if repository_id in exact_ids:
            excluded_exact += 1
            continue
        delta = int(current["totalStars"]) - int(first["totalStars"])
        if delta < 0:
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
        near_validation = consecutive_hours >= 20
        just_discovered = first_index >= max(0, len(sources.captures) - 2) or hours <= 4
        rising = len(observations) >= 2 and hours > 0 and delta > 0
        stage = (
            "near_validation"
            if near_validation
            else "just_discovered"
            if just_discovered
            else "rising"
            if rising
            else None
        )
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
        stages[STAGE_KEYS[stage]].append(item)

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
        "schemaVersion": SCHEMA_VERSION,
        "policyVersion": POLICY_VERSION,
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
            "sections": list(STAGE_ORDER),
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
            "excludedExactCount": excluded_exact,
        },
        "conflicts": conflicts,
        "sourceInventory": [
            _source_reference(source, index)
            for index, source in enumerate(sources.captures, start=1)
        ],
        "todayExplosionSource": _today_reference(sources.today),
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
    return {
        "schemaVersion": SCHEMA_VERSION,
        "policyVersion": POLICY_VERSION,
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


def _load_generation_sources(root: Path, artifact: dict[str, Any]) -> DiscoverSources:
    captures: list[CaptureSource] = []
    for reference in artifact["sourceInventory"]:
        path = root / reference["generationRelativePath"]
        payload, content, file_sha = _read_json(path, root, ArtifactKind.TRENDING_CAPTURE_BUNDLE)
        try:
            payload = validate_capture_bundle(payload, expected_capture_id=reference["captureId"])
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
                "discover_source_reference_mismatch", "source capture no longer matches its reference", stage="audit"
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
        return {
            "status": artifact["coverage"]["state"],
            "generationId": generation_id,
            "manifestSha256": manifest_sha,
            "latestCaptureId": artifact["latestCaptureId"],
            "publishedCount": artifact["coverage"]["publishedCount"],
            "conflictCount": artifact["coverage"]["conflictCount"],
            "stageCounts": {key: len(artifact["stages"][value]) for key, value in STAGE_KEYS.items()},
        }
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
        sources.latest.payload["captureId"]
        + sources.latest.file_sha256
        + sources.today.generation_id
        + sources.today.file_sha256
    ).encode("utf-8")
    suffix = _sha256(signature)[:12]
    return generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + suffix


def _same_sources(artifact: dict[str, Any], sources: DiscoverSources) -> bool:
    return (
        artifact.get("latestCaptureId") == sources.latest.payload["captureId"]
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
    return {
        "state": state,
        "generationId": artifact["discoverGenerationId"],
        "latestCaptureId": artifact["latestCaptureId"],
        "todayExplosionGenerationId": artifact["todayExplosionGenerationId"],
        "coverageState": artifact["coverage"]["state"],
        "stageCounts": {stage: len(artifact["stages"][key]) for stage, key in STAGE_KEYS.items()},
        "publishedCount": artifact["coverage"]["publishedCount"],
        "conflictCount": artifact["coverage"]["conflictCount"],
        "excludedExactCount": artifact["coverage"]["excludedExactCount"],
        "coverage": copy.deepcopy(artifact["coverage"]),
    }


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
        for index, source in enumerate(sources.captures, start=1):
            _atomic_write(candidate / f"sources/capture-{index:02d}.json", source.content)
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
                    "schemaVersion": SCHEMA_VERSION,
                    "policyVersion": POLICY_VERSION,
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
            "schemaVersion": SCHEMA_VERSION,
            "policyVersion": POLICY_VERSION,
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
