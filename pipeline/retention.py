"""Auditable, digest-bound retention for Rardar's append-only data stores."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from pipeline.data_lock import _try_lock, _unlock, data_dir_lock
from pipeline.generations import (
    GenerationProtocolError,
    resolve_current_generation,
    verify_retained_generation,
)
from pipeline.runtime_logging import StructuredLogger, new_run_id
from pipeline.runtime_settings import (
    RuntimeSettings,
    RuntimeSettingsError,
    default_runtime_dir,
    load_runtime_settings,
)
from pipeline.stable_read import StableReadError, stable_read
from pipeline.trending_discover import (
    DISCOVER_RELATIVE_ROOT,
    TrendingDiscoverError,
    audit_discover_generation,
    resolve_current_discover,
)
from pipeline.trending_observations import (
    TrendingObservationError,
    audit_observation_store,
    load_capture,
    observer_lock_path,
)


RETENTION_POLICY_VERSION = "rardar-retention-v2"
RETENTION_PLAN_SCHEMA_VERSION = 1
CAPTURE_ROOT = Path("observations/trending/v1/captures")
UNIFIED_GENERATION_ROOT = Path("generations")
UNIFIED_CANDIDATE_ROOT = Path("generations/.candidates")
DISCOVER_GENERATION_ROOT = DISCOVER_RELATIVE_ROOT / "generations"
DISCOVER_CANDIDATE_ROOT = DISCOVER_GENERATION_ROOT / ".candidates"
NEWEST_READY_PER_TYPE = 3
_GENERATION_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_CAPTURE_REFERENCE = re.compile(
    r"^observations/trending/v1/captures/[0-9]{4}/(?:0[1-9]|1[0-2])/"
    r"(?:0[1-9]|[12][0-9]|3[01])/trending-v1-[0-9]{8}T[0-9]{6}Z\.json$"
)
_TEMP_NAME = re.compile(r"(?:^\.|\.)(?:tmp|temp|partial)(?:\.|$)", re.IGNORECASE)
_TRANSACTION_NAME = re.compile(r"^\.retention-transaction-([0-9a-f]{64})$")


class RetentionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class StorageSnapshot:
    used_percent: int
    free_bytes: int
    total_bytes: int
    warning_threshold: int
    hard_threshold: int
    minimum_free_bytes: int
    guard_state: str


def _parse_time(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise RetentionError("retention_invalid_timestamp", f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RetentionError("retention_invalid_timestamp", f"{field} is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RetentionError("retention_invalid_timestamp", f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RetentionError("retention_invalid_timestamp", "retention clock must include a timezone")
    return value.astimezone(timezone.utc).isoformat()


def _link(metadata: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0)) & reparse
    )


def _require_real_directory(path: Path, *, code: str) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise RetentionError(code, f"required retention directory is unavailable: {error}") from None
    if _link(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise RetentionError(code, "retention directory must be a real directory")


def _canonical_data_dir(data_dir: Path) -> Path:
    raw = data_dir.expanduser().absolute()
    _require_real_directory(raw, code="retention_unsafe_data_root")
    canonical = raw.resolve()
    if canonical != raw.resolve(strict=True):
        raise RetentionError("retention_unsafe_data_root", "data root could not be resolved")
    return canonical


def _safe_relative(value: object) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RetentionError("retention_unsafe_path", "retention path must be canonical relative POSIX")
    relative = Path(value)
    if relative.is_absolute() or value != relative.as_posix() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise RetentionError("retention_unsafe_path", "retention path escapes the data root")
    return relative


def _contained(root: Path, relative: Path) -> Path:
    target = root
    for part in relative.parts:
        target = target / part
        if os.path.lexists(target):
            metadata = os.lstat(target)
            if _link(metadata):
                raise RetentionError(
                    "retention_unsafe_link",
                    "retention path contains a link or junction",
                )
    try:
        target.absolute().relative_to(root.absolute())
    except ValueError:
        raise RetentionError("retention_path_escape", "retention path escapes the data root") from None
    return target


def _read_json(path: Path, *, maximum: int = 32 * 1024 * 1024) -> tuple[dict[str, Any], str]:
    try:
        snapshot = stable_read(path)
    except StableReadError as error:
        raise RetentionError("retention_unstable_evidence", str(error)) from None
    if len(snapshot.content) > maximum:
        raise RetentionError("retention_evidence_too_large", "retention evidence exceeds its bound")
    try:
        payload = json.loads(snapshot.content.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        raise RetentionError("retention_invalid_json", "retention evidence is not valid UTF-8 JSON") from None
    if not isinstance(payload, dict):
        raise RetentionError("retention_invalid_json", "retention evidence must be an object")
    return payload, snapshot.sha256


def _manifest(path: Path, expected_id: str) -> tuple[dict[str, Any], str]:
    if _GENERATION_ID.fullmatch(expected_id) is None:
        raise RetentionError("retention_invalid_generation_id", "generation ID is unsafe")
    payload, digest = _read_json(path / "manifest.json")
    if payload.get("generationId") != expected_id:
        raise RetentionError("retention_generation_identity_mismatch", "manifest identity differs from its directory")
    return payload, digest


def _tree_snapshot(path: Path, root: Path) -> dict[str, Any]:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        raise RetentionError("retention_path_escape", "target escapes the data root") from None
    entries: list[tuple[str, int, int, int, int, str]] = []
    total_bytes = 0
    file_count = 0

    def record(current: Path) -> None:
        nonlocal total_bytes, file_count
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise RetentionError("retention_target_changed", f"retention target is unavailable: {error}") from None
        if _link(metadata):
            raise RetentionError("retention_unsafe_link", "retention never follows filesystem links or junctions")
        child_relative = current.absolute().relative_to(path.absolute()).as_posix()
        if child_relative == ".":
            child_relative = ""
        if stat.S_ISDIR(metadata.st_mode):
            entries.append(
                (child_relative + "/", int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_mtime_ns), 0, "")
            )
            try:
                children = sorted(current.iterdir(), key=lambda item: item.name)
            except OSError as error:
                raise RetentionError("retention_target_changed", f"retention directory changed: {error}") from None
            for child in children:
                record(child)
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise RetentionError("retention_unsafe_type", "retention target contains a non-regular entry")
        if int(metadata.st_nlink) != 1:
            raise RetentionError("retention_unsafe_hardlink", "retention target contains a hard-linked file")
        try:
            content = stable_read(current)
        except StableReadError as error:
            raise RetentionError("retention_target_changed", str(error)) from None
        total_bytes += len(content.content)
        file_count += 1
        entries.append(
            (
                child_relative,
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_mtime_ns),
                len(content.content),
                content.sha256,
            )
        )

    record(path)
    identity_bytes = json.dumps(entries, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    content_entries = [(item[0], item[4], item[5]) for item in entries]
    content_bytes = json.dumps(content_entries, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return {
        "relativePath": relative.as_posix(),
        "fileCount": file_count,
        "bytes": total_bytes,
        "identityDigest": hashlib.sha256(identity_bytes).hexdigest(),
        "contentDigest": hashlib.sha256(content_bytes).hexdigest(),
    }


def _directory_bytes(path: Path) -> tuple[int, int]:
    snapshot = _tree_snapshot(path, path.parent)
    return int(snapshot["fileCount"]), int(snapshot["bytes"])


def _json_references(payload: object) -> Iterable[str]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "originalObservationPath" and isinstance(value, str):
                yield value
            yield from _json_references(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _json_references(item)


def _capture_references(root: Path) -> set[str]:
    references: set[str] = set()
    for path in sorted(root.rglob("*.json")):
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json":
            continue
        payload, _ = _read_json(path)
        for reference in _json_references(payload):
            if _CAPTURE_REFERENCE.fullmatch(reference) is None:
                raise RetentionError("retention_invalid_capture_reference", "retained artifact contains an unsafe capture reference")
            references.add(reference)
    return references


def _generation_artifact_type(manifest: Mapping[str, object]) -> str:
    operation = str(manifest.get("operation"))
    artifacts = manifest.get("artifacts")
    if (
        operation == "derive"
        and isinstance(artifacts, list)
        and "trending/explosion.json" in artifacts
    ):
        return "explosion"
    return operation


def _keep_marker(path: Path, boundary: Path) -> bool:
    current = path
    while True:
        for name in ("keep", ".keep", "protected", ".protected"):
            marker = current / name
            if os.path.lexists(marker):
                metadata = os.lstat(marker)
                if (
                    _link(metadata)
                    or not stat.S_ISREG(metadata.st_mode)
                    or int(metadata.st_nlink) != 1
                ):
                    raise RetentionError("retention_unsafe_keep_marker", "keep marker must be a regular file")
                return True
        if current == boundary:
            return False
        current = current.parent


def _iter_real_directories(root: Path) -> list[Path]:
    if not root.exists():
        return []
    _require_real_directory(root, code="retention_unsafe_store")
    result: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        metadata = os.lstat(child)
        if _link(metadata):
            raise RetentionError("retention_unsafe_link", "retention store contains a link or junction")
        if not stat.S_ISDIR(metadata.st_mode):
            raise RetentionError("retention_unsafe_type", "retention generation store contains a non-directory")
        result.append(child)
    return result


def _observer_is_active(data_dir: Path) -> bool:
    path = observer_lock_path(data_dir)
    if not os.path.lexists(path):
        return False
    metadata = os.lstat(path)
    if _link(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise RetentionError("retention_unsafe_observer_lock", "observer lock is unsafe")
    with path.open("rb") as handle:
        acquired = False
        try:
            try:
                _try_lock(handle)
                acquired = True
            except OSError:
                return True
            return False
        finally:
            if acquired:
                _unlock(handle)


def storage_snapshot(
    data_dir: Path,
    settings: RuntimeSettings,
    *,
    disk_usage: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage,
) -> StorageSnapshot:
    usage = disk_usage(data_dir)
    used_percent = int((usage.used * 100 + max(1, usage.total) - 1) // max(1, usage.total))
    if used_percent >= settings.storage_hard_percent or usage.free < settings.storage_minimum_free_bytes:
        state = "blocked"
    elif used_percent >= settings.storage_warning_percent:
        state = "warning"
    else:
        state = "healthy"
    return StorageSnapshot(
        used_percent=used_percent,
        free_bytes=int(usage.free),
        total_bytes=int(usage.total),
        warning_threshold=settings.storage_warning_percent,
        hard_threshold=settings.storage_hard_percent,
        minimum_free_bytes=settings.storage_minimum_free_bytes,
        guard_state=state,
    )


def require_discover_storage_capacity(
    data_dir: Path,
    settings: RuntimeSettings,
    *,
    disk_usage: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage,
) -> StorageSnapshot:
    snapshot = storage_snapshot(data_dir, settings, disk_usage=disk_usage)
    if snapshot.guard_state == "blocked":
        raise RetentionError(
            "discover_storage_guard",
            "storage guard blocks creation of a new Discover candidate",
        )
    return snapshot


def _plan_digest(plan: Mapping[str, object]) -> str:
    bound = {key: value for key, value in plan.items() if key != "planDigest"}
    content = json.dumps(bound, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _add_protected(
    protected: dict[str, set[str]],
    relative: str,
    reason: str,
) -> None:
    normalized = _safe_relative(relative).as_posix()
    protected.setdefault(normalized, set()).add(reason)


def _is_protected(relative: str, protected: Mapping[str, set[str]]) -> bool:
    candidate = _safe_relative(relative)
    for item in protected:
        boundary = _safe_relative(item)
        if candidate == boundary or boundary in candidate.parents or candidate in boundary.parents:
            return True
    return False


def _external_audit(paths: Sequence[Path]) -> dict[str, int]:
    files = 0
    bytes_count = 0
    links = 0
    directories = 0
    for root in paths:
        if not root.exists():
            continue
        _require_real_directory(root, code="retention_unsafe_external_root")
        for current, directory_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            retained_directories: list[str] = []
            for name in sorted(directory_names):
                path = current_path / name
                metadata = os.lstat(path)
                if _link(metadata):
                    links += 1
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    raise RetentionError(
                        "retention_unsafe_external_entry",
                        "external audit encountered an unexpected directory entry",
                    )
                directories += 1
                retained_directories.append(name)
            directory_names[:] = retained_directories
            for name in sorted(file_names):
                path = current_path / name
                metadata = os.lstat(path)
                if _link(metadata):
                    links += 1
                elif stat.S_ISREG(metadata.st_mode):
                    files += 1
                    bytes_count += int(metadata.st_size)
                else:
                    raise RetentionError(
                        "retention_unsafe_external_entry",
                        "external audit encountered an unsupported filesystem entry",
                    )
    return {
        "directories": directories,
        "files": files,
        "bytes": bytes_count,
        "linksNotFollowed": links,
    }


def _build_plan_locked(
    canonical: Path,
    settings: RuntimeSettings,
    *,
    now: datetime,
    release_roots: Sequence[Path],
    backup_roots: Sequence[Path],
    operator_artifact_roots: Sequence[Path],
) -> dict[str, Any]:
    if _pending_transaction_digests(canonical):
        raise RetentionError(
            "retention_transaction_pending",
            "an interrupted retention transaction must be recovered before planning",
        )
    now_utc = now.astimezone(timezone.utc)
    protected: dict[str, set[str]] = {}
    deletions: list[dict[str, Any]] = []
    referenced_captures: set[str] = set()
    scanned_paths = 0

    try:
        current = resolve_current_generation(canonical)
    except GenerationProtocolError as error:
        raise RetentionError("retention_current_invalid", str(error)) from None
    if current.legacy or current.generation_id is None:
        raise RetentionError("retention_current_invalid", "retention requires an audited generation pointer")
    verified_unified: dict[str, object] = {}

    def verify_unified(identifier: str):
        verified = verified_unified.get(identifier)
        if verified is None:
            verified = verify_retained_generation(canonical, identifier)
            verified_unified[identifier] = verified
        return verified

    _add_protected(protected, f"generations/{current.generation_id}", "current_generation")
    previous = current.pointer.get("previousGenerationId") if current.pointer else None
    if isinstance(previous, str):
        verify_unified(previous)
        _add_protected(protected, f"generations/{previous}", "previous_healthy_generation")
    current_pointer_snapshot = stable_read(canonical / "current.json")

    unified: list[tuple[datetime, str, str, Path]] = []
    generation_root = canonical / UNIFIED_GENERATION_ROOT
    for root in _iter_real_directories(generation_root):
        if root.name == ".candidates":
            continue
        scanned_paths += 1
        manifest, _ = _manifest(root, root.name)
        if manifest.get("state") != "ready":
            raise RetentionError("retention_retained_not_ready", "retained generation manifest is not ready")
        verify_unified(root.name)
        _tree_snapshot(root, canonical)
        created = _parse_time(manifest.get("createdAt"), field="generation.createdAt")
        artifact_type = _generation_artifact_type(manifest)
        unified.append((created, root.name, artifact_type, root))
        referenced_captures.update(_capture_references(root))
        if _keep_marker(root, generation_root):
            _add_protected(protected, root.relative_to(canonical).as_posix(), "operator_keep_marker")
    grouped: dict[str, list[tuple[datetime, str, str, Path]]] = {}
    for item in unified:
        grouped.setdefault(item[2], []).append(item)
    for operation, entries in grouped.items():
        for _, identifier, _, _ in sorted(entries, reverse=True)[:NEWEST_READY_PER_TYPE]:
            _add_protected(protected, f"generations/{identifier}", f"newest_ready_{operation}")
    generation_cutoff = now_utc - timedelta(days=settings.retention_generation_days)
    for created, identifier, operation, root in sorted(unified):
        relative = root.relative_to(canonical).as_posix()
        if created < generation_cutoff and not _is_protected(relative, protected):
            verified = verify_unified(identifier)
            snapshot = _tree_snapshot(root, canonical)
            deletions.append({
                **snapshot,
                "kind": f"{operation}_generation",
                "reason": "generation_retention_expired",
                "manifestSha256": verified.manifest_sha256,
                "numericIdentity": identifier,
            })

    discover_root = canonical / DISCOVER_RELATIVE_ROOT
    discover_pointer_digest: str | None = None
    discover_entries: list[tuple[datetime, str, Path]] = []
    verified_discover: dict[str, dict[str, Any]] = {}

    def verify_discover(root: Path) -> dict[str, Any]:
        report = verified_discover.get(root.name)
        if report is None:
            report = audit_discover_generation(root)
            verified_discover[root.name] = report
        return report

    discover_pointer = discover_root / "current.json"
    if os.path.lexists(discover_pointer):
        try:
            discover_current = resolve_current_discover(canonical)
        except TrendingDiscoverError as error:
            raise RetentionError("retention_discover_current_invalid", str(error)) from None
        discover_pointer_digest = stable_read(discover_pointer).sha256
        _add_protected(
            protected,
            (DISCOVER_GENERATION_ROOT / discover_current.generation_id).as_posix(),
            "current_discover_generation",
        )
        previous_discover = discover_current.pointer.get("previousGenerationId")
        if isinstance(previous_discover, str):
            previous_root = discover_root / "generations" / previous_discover
            verify_discover(previous_root)
            _add_protected(
                protected,
                (DISCOVER_GENERATION_ROOT / previous_discover).as_posix(),
                "previous_healthy_discover_generation",
            )
    discover_generation_root = canonical / DISCOVER_GENERATION_ROOT
    for root in _iter_real_directories(discover_generation_root):
        if root.name == ".candidates":
            continue
        scanned_paths += 1
        manifest, _ = _manifest(root, root.name)
        if manifest.get("state") != "ready":
            raise RetentionError("retention_retained_not_ready", "retained Discover generation is not ready")
        verify_discover(root)
        _tree_snapshot(root, canonical)
        created = _parse_time(manifest.get("createdAt"), field="discover.createdAt")
        discover_entries.append((created, root.name, root))
        referenced_captures.update(_capture_references(root))
        if _keep_marker(root, discover_generation_root):
            _add_protected(protected, root.relative_to(canonical).as_posix(), "operator_keep_marker")
    for _, identifier, root in sorted(discover_entries, reverse=True)[:NEWEST_READY_PER_TYPE]:
        _add_protected(
            protected,
            (DISCOVER_GENERATION_ROOT / identifier).as_posix(),
            "newest_ready_discover",
        )
    discover_cutoff = now_utc - timedelta(days=settings.retention_discover_generation_days)
    for created, identifier, root in sorted(discover_entries):
        relative = root.relative_to(canonical).as_posix()
        if created < discover_cutoff and not _is_protected(relative, protected):
            report = verify_discover(root)
            snapshot = _tree_snapshot(root, canonical)
            deletions.append({
                **snapshot,
                "kind": "discover_generation",
                "reason": "discover_retention_expired",
                "manifestSha256": report["manifestSha256"],
                "numericIdentity": identifier,
            })

    for reference in sorted(referenced_captures):
        capture_path = _contained(canonical, _safe_relative(reference))
        load_capture(capture_path)
        _add_protected(protected, reference, "retained_generation_reference")

    capture_root = canonical / CAPTURE_ROOT
    capture_cutoff = now_utc - timedelta(days=settings.retention_capture_days)
    if capture_root.exists():
        _require_real_directory(capture_root, code="retention_unsafe_capture_store")
        _tree_snapshot(capture_root, canonical)
        for path in sorted(capture_root.rglob("*.json")):
            scanned_paths += 1
            metadata = os.lstat(path)
            if _link(metadata) or not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
                raise RetentionError("retention_unsafe_capture", "capture store contains an unsafe file")
            payload = load_capture(path)
            scheduled = _parse_time(payload.get("scheduledAt"), field="capture.scheduledAt")
            retention = payload.get("retention")
            if not isinstance(retention, Mapping):
                raise RetentionError(
                    "retention_invalid_capture_metadata",
                    "capture retention metadata is missing",
                )
            retain_until = _parse_time(
                retention.get("retainUntil"),
                field="capture.retention.retainUntil",
            )
            relative = path.relative_to(canonical).as_posix()
            if _keep_marker(path.parent, capture_root):
                _add_protected(protected, relative, "operator_keep_marker")
            if (
                scheduled < capture_cutoff
                and retain_until <= now_utc
                and not _is_protected(relative, protected)
            ):
                snapshot = _tree_snapshot(path, canonical)
                deletions.append({
                    **snapshot,
                    "kind": "observation_capture",
                    "reason": "capture_retention_expired",
                    "manifestSha256": None,
                    "numericIdentity": payload.get("captureId"),
                })

    candidate_entries: list[tuple[datetime, str, str, Path, str]] = []
    for candidate_relative, label in (
        (UNIFIED_CANDIDATE_ROOT, "generation_candidate"),
        (DISCOVER_CANDIDATE_ROOT, "discover_candidate"),
    ):
        root = canonical / candidate_relative
        for path in _iter_real_directories(root):
            scanned_paths += 1
            if _TEMP_NAME.search(path.name):
                continue
            manifest, digest = _manifest(path, path.name)
            _tree_snapshot(path, canonical)
            created = _parse_time(manifest.get("createdAt"), field="candidate.createdAt")
            state = str(manifest.get("state"))
            candidate_entries.append((created, path.name, state, path, label))
            relative = path.relative_to(canonical).as_posix()
            if state == "building":
                _add_protected(protected, relative, "active_candidate")
            if _keep_marker(path, root):
                _add_protected(protected, relative, "operator_keep_marker")
    for candidate_state in ("failed", "ready"):
        matching = [item for item in candidate_entries if item[2] == candidate_state]
        for _, _, _, path, _ in sorted(matching, reverse=True)[
            : settings.retention_candidate_latest_count
        ]:
            _add_protected(
                protected,
                path.relative_to(canonical).as_posix(),
                f"newest_{candidate_state}_candidate",
            )
    for created, identifier, state, path, label in sorted(candidate_entries):
        relative = path.relative_to(canonical).as_posix()
        if state not in {"failed", "ready"}:
            _add_protected(protected, relative, "unknown_candidate_state")
            continue
        cutoff_days = (
            settings.retention_failed_candidate_days
            if state == "failed"
            else settings.retention_candidate_days
        )
        candidate_cutoff = now_utc - timedelta(days=cutoff_days)
        if created < candidate_cutoff and not _is_protected(relative, protected):
            manifest, digest = _manifest(path, identifier)
            snapshot = _tree_snapshot(path, canonical)
            deletions.append({
                **snapshot,
                "kind": label,
                "reason": f"{state}_candidate_retention_expired",
                "manifestSha256": digest,
                "numericIdentity": identifier,
            })

    # Temporary entries are eligible only in bounded data namespaces and while
    # this caller owns the canonical data lock.  Candidate directories and
    # immutable retained directories are otherwise handled by their manifests.
    temp_cutoff_ns = int((now_utc - timedelta(hours=settings.retention_temp_hours)).timestamp() * 1_000_000_000)
    known_roots = [
        canonical / "artifacts" / "trending",
        canonical / UNIFIED_CANDIDATE_ROOT,
        canonical / DISCOVER_CANDIDATE_ROOT,
    ]
    if not _observer_is_active(canonical):
        known_roots.append(capture_root)
    existing_deletions = {_safe_relative(item["relativePath"]) for item in deletions}
    for root in known_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not _TEMP_NAME.search(path.name):
                continue
            relative_path = path.relative_to(canonical)
            if any(parent == target or target in parent.parents for parent in relative_path.parents for target in existing_deletions):
                continue
            metadata = os.lstat(path)
            if _link(metadata):
                raise RetentionError("retention_unsafe_link", "temporary path is a link or junction")
            if int(metadata.st_mtime_ns) >= temp_cutoff_ns:
                continue
            relative = relative_path.as_posix()
            if _is_protected(relative, protected):
                continue
            snapshot = _tree_snapshot(path, canonical)
            deletions.append({
                **snapshot,
                "kind": "temporary",
                "reason": "temporary_retention_expired",
                "manifestSha256": None,
                "numericIdentity": hashlib.sha256(relative.encode("utf-8")).hexdigest(),
            })

    protected_entries: list[dict[str, Any]] = []
    protected_files = 0
    protected_bytes = 0
    for relative, reasons in sorted(protected.items()):
        path = _contained(canonical, _safe_relative(relative))
        if not os.path.lexists(path):
            raise RetentionError("retention_protected_missing", "protected retention entry is missing")
        snapshot = _tree_snapshot(path, canonical)
        protected_files += int(snapshot["fileCount"])
        protected_bytes += int(snapshot["bytes"])
        protected_entries.append({
            **snapshot,
            "reasons": sorted(reasons),
        })

    deletions.sort(key=lambda item: (item["relativePath"], item["kind"]))
    for index, item in enumerate(deletions):
        relative = _safe_relative(item["relativePath"])
        for other in deletions[index + 1 :]:
            other_relative = _safe_relative(other["relativePath"])
            if relative in other_relative.parents or other_relative in relative.parents:
                raise RetentionError("retention_overlapping_targets", "retention plan contains overlapping targets")
        if _is_protected(relative.as_posix(), protected):
            raise RetentionError("retention_protected_target", "retention plan targets protected evidence")

    guard_payload = {
        "currentPointerSha256": current_pointer_snapshot.sha256,
        "discoverPointerSha256": discover_pointer_digest,
        "protected": protected_entries,
    }
    guard_digest = hashlib.sha256(
        json.dumps(guard_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    external = {
        "releaseDirectories": _external_audit(release_roots),
        "deploymentBackups": _external_audit(backup_roots),
        "operatorArtifacts": _external_audit(operator_artifact_roots),
        "automaticDeletion": False,
    }
    plan: dict[str, Any] = {
        "schemaVersion": RETENTION_PLAN_SCHEMA_VERSION,
        "policyVersion": RETENTION_POLICY_VERSION,
        "createdAt": _iso(now_utc),
        "policy": {
            "captureDays": settings.retention_capture_days,
            "generationDays": settings.retention_generation_days,
            "discoverGenerationDays": settings.retention_discover_generation_days,
            "failedCandidateDays": settings.retention_failed_candidate_days,
            "readyCandidateDays": settings.retention_candidate_days,
            "candidateLatestCount": settings.retention_candidate_latest_count,
            "temporaryHours": settings.retention_temp_hours,
            "newestReadyPerType": NEWEST_READY_PER_TYPE,
        },
        "guardDigest": guard_digest,
        "protected": protected_entries,
        "deletions": deletions,
        "externalAuditOnly": external,
        "summary": {
            "scannedPaths": scanned_paths,
            "protectedFiles": protected_files,
            "protectedBytes": protected_bytes,
            "plannedDeletions": len(deletions),
            "plannedFiles": sum(int(item["fileCount"]) for item in deletions),
            "plannedBytes": sum(int(item["bytes"]) for item in deletions),
        },
    }
    plan["planDigest"] = _plan_digest(plan)
    return plan


def create_retention_plan(
    data_dir: Path,
    settings: RuntimeSettings,
    *,
    now: datetime | None = None,
    release_roots: Sequence[Path] = (),
    backup_roots: Sequence[Path] = (),
    operator_artifact_roots: Sequence[Path] = (),
) -> dict[str, Any]:
    canonical = _canonical_data_dir(data_dir)
    effective_now = now or datetime.now(timezone.utc)
    with data_dir_lock(canonical):
        return _build_plan_locked(
            canonical,
            settings,
            now=effective_now,
            release_roots=release_roots,
            backup_roots=backup_roots,
            operator_artifact_roots=operator_artifact_roots,
        )


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    """Make rename/delete ordering durable on the Linux Production filesystem."""

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_retention_plan(path: Path, plan: Mapping[str, object]) -> None:
    _validate_retention_plan(plan)
    if _plan_digest(plan) != plan.get("planDigest"):
        raise RetentionError("retention_plan_digest_mismatch", "retention plan digest is invalid")
    _atomic_json(path, plan)


def _is_nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_exact_keys(value: Mapping[str, object], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise RetentionError("retention_plan_invalid", f"{label} fields are invalid")


def _require_sha256(value: object, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RetentionError("retention_plan_invalid", "retention digest is invalid")


def _validate_inventory_entry(raw: object, *, protected: bool) -> None:
    if not isinstance(raw, dict):
        raise RetentionError("retention_plan_invalid", "retention inventory entry must be an object")
    common = {
        "relativePath",
        "fileCount",
        "bytes",
        "identityDigest",
        "contentDigest",
    }
    expected = common | ({"reasons"} if protected else {
        "kind",
        "reason",
        "manifestSha256",
        "numericIdentity",
    })
    _require_exact_keys(raw, expected, label="retention inventory")
    _safe_relative(raw.get("relativePath"))
    for field in ("fileCount", "bytes"):
        if not _is_nonnegative_integer(raw.get(field)):
            raise RetentionError("retention_plan_invalid", f"retention {field} is invalid")
    _require_sha256(raw.get("identityDigest"))
    _require_sha256(raw.get("contentDigest"))
    if protected:
        reasons = raw.get("reasons")
        if (
            not isinstance(reasons, list)
            or not reasons
            or any(not isinstance(item, str) or not item for item in reasons)
            or reasons != sorted(set(reasons))
        ):
            raise RetentionError("retention_plan_invalid", "protected reasons are invalid")
        return
    for field in ("kind", "reason", "numericIdentity"):
        if not isinstance(raw.get(field), str) or not raw[field]:
            raise RetentionError("retention_plan_invalid", f"retention {field} is invalid")
    _require_sha256(raw.get("manifestSha256"), nullable=True)


def _validate_retention_plan(plan: Mapping[str, object]) -> None:
    if not isinstance(plan, Mapping):
        raise RetentionError("retention_plan_invalid", "retention plan must be an object")
    _require_exact_keys(
        plan,
        {
            "schemaVersion",
            "policyVersion",
            "createdAt",
            "policy",
            "guardDigest",
            "protected",
            "deletions",
            "externalAuditOnly",
            "summary",
            "planDigest",
        },
        label="retention plan",
    )
    if (
        plan.get("schemaVersion") != RETENTION_PLAN_SCHEMA_VERSION
        or plan.get("policyVersion") != RETENTION_POLICY_VERSION
    ):
        raise RetentionError("retention_plan_version_unsupported", "retention plan version is unsupported")
    _parse_time(plan.get("createdAt"), field="plan.createdAt")
    _require_sha256(plan.get("guardDigest"))
    _require_sha256(plan.get("planDigest"))

    policy = plan.get("policy")
    if not isinstance(policy, dict):
        raise RetentionError("retention_plan_invalid", "retention policy is invalid")
    _require_exact_keys(
        policy,
        {
            "captureDays",
            "generationDays",
            "discoverGenerationDays",
            "failedCandidateDays",
            "readyCandidateDays",
            "candidateLatestCount",
            "temporaryHours",
            "newestReadyPerType",
        },
        label="retention policy",
    )
    if any(not _is_nonnegative_integer(value) or value == 0 for value in policy.values()):
        raise RetentionError("retention_plan_invalid", "retention policy values are invalid")

    protected_inventory = plan.get("protected")
    deletion_inventory = plan.get("deletions")
    if not isinstance(protected_inventory, list) or not isinstance(deletion_inventory, list):
        raise RetentionError("retention_plan_invalid", "retention plan inventory is invalid")
    for item in protected_inventory:
        _validate_inventory_entry(item, protected=True)
    for item in deletion_inventory:
        _validate_inventory_entry(item, protected=False)
    protected_paths = [item["relativePath"] for item in protected_inventory]
    deletion_paths = [item["relativePath"] for item in deletion_inventory]
    if protected_paths != sorted(set(protected_paths)) or deletion_paths != sorted(set(deletion_paths)):
        raise RetentionError("retention_plan_invalid", "retention inventory must be unique and sorted")

    external = plan.get("externalAuditOnly")
    if not isinstance(external, dict):
        raise RetentionError("retention_plan_invalid", "external audit inventory is invalid")
    _require_exact_keys(
        external,
        {
            "releaseDirectories",
            "deploymentBackups",
            "operatorArtifacts",
            "automaticDeletion",
        },
        label="external audit",
    )
    if external.get("automaticDeletion") is not False:
        raise RetentionError("retention_plan_invalid", "external artifacts cannot be automatically deleted")
    for label in ("releaseDirectories", "deploymentBackups", "operatorArtifacts"):
        inventory = external.get(label)
        if not isinstance(inventory, dict):
            raise RetentionError("retention_plan_invalid", "external audit summary is invalid")
        _require_exact_keys(
            inventory,
            {"directories", "files", "bytes", "linksNotFollowed"},
            label="external audit summary",
        )
        if any(not _is_nonnegative_integer(value) for value in inventory.values()):
            raise RetentionError("retention_plan_invalid", "external audit counts are invalid")

    summary = plan.get("summary")
    if not isinstance(summary, dict):
        raise RetentionError("retention_plan_invalid", "retention summary is invalid")
    _require_exact_keys(
        summary,
        {
            "scannedPaths",
            "protectedFiles",
            "protectedBytes",
            "plannedDeletions",
            "plannedFiles",
            "plannedBytes",
        },
        label="retention summary",
    )
    if any(not _is_nonnegative_integer(value) for value in summary.values()):
        raise RetentionError("retention_plan_invalid", "retention summary counts are invalid")
    if summary["plannedDeletions"] != len(deletion_inventory):
        raise RetentionError("retention_plan_invalid", "retention deletion count is inconsistent")
    if summary["plannedFiles"] != sum(int(item["fileCount"]) for item in deletion_inventory):
        raise RetentionError("retention_plan_invalid", "retention file count is inconsistent")
    if summary["plannedBytes"] != sum(int(item["bytes"]) for item in deletion_inventory):
        raise RetentionError("retention_plan_invalid", "retention byte count is inconsistent")


def load_retention_plan(path: Path) -> dict[str, Any]:
    payload, _ = _read_json(path)
    _validate_retention_plan(payload)
    digest = payload.get("planDigest")
    if not isinstance(digest, str) or _plan_digest(payload) != digest:
        raise RetentionError("retention_plan_digest_mismatch", "retention plan digest is invalid")
    return payload


def _receipt_path(runtime_dir: Path, digest: str) -> Path:
    return runtime_dir / "retention" / "receipts" / f"{digest}.json"


def _pending_transaction_digests(canonical: Path) -> list[str]:
    pending: list[str] = []
    for path in sorted(canonical.iterdir(), key=lambda item: item.name):
        if not path.name.startswith(".retention-transaction-"):
            continue
        matched = _TRANSACTION_NAME.fullmatch(path.name)
        if matched is None:
            raise RetentionError(
                "retention_transaction_invalid",
                "retention data root contains an invalid transaction name",
            )
        metadata = os.lstat(path)
        if _link(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise RetentionError(
                "retention_unsafe_transaction",
                "retention transaction must be a real directory",
            )
        pending.append(matched.group(1))
    return pending


def _recover_transaction(canonical: Path, runtime_dir: Path, digest: str) -> None:
    transaction = canonical / f".retention-transaction-{digest}"
    if not os.path.lexists(transaction):
        return
    _require_real_directory(transaction, code="retention_unsafe_transaction")
    _tree_snapshot(transaction, canonical)
    manifest, _ = _read_json(transaction / "transaction.json")
    if manifest.get("planDigest") != digest or not isinstance(manifest.get("targets"), list):
        raise RetentionError("retention_transaction_invalid", "retention transaction cannot be trusted")
    receipt = _receipt_path(runtime_dir, digest)
    if receipt.exists():
        try:
            load_retention_plan_receipt(receipt, digest)
        except RetentionError as receipt_error:
            # An invalid receipt is not evidence that deletion committed. Restore
            # every staged source before surfacing the corruption; never turn a
            # forged or truncated receipt into permanent data loss.
            for raw in reversed(manifest["targets"]):
                relative = _safe_relative(raw)
                staged = transaction / "staged" / relative
                target = _contained(canonical, relative)
                staged_present = os.path.lexists(staged)
                target_present = os.path.lexists(target)
                if staged_present and target_present:
                    raise RetentionError(
                        "retention_recovery_conflict",
                        "retention recovery found both staged and restored evidence",
                    ) from None
                if staged_present:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, target)
                    _fsync_directory(staged.parent)
                    _fsync_directory(target.parent)
                elif target_present:
                    continue
                else:
                    raise RetentionError(
                        "retention_transaction_incomplete",
                        "retention transaction lost a target",
                    ) from None
            shutil.rmtree(transaction)
            _fsync_directory(canonical)
            raise receipt_error
        else:
            shutil.rmtree(transaction)
            _fsync_directory(canonical)
            return
    for raw in reversed(manifest["targets"]):
        relative = _safe_relative(raw)
        staged = transaction / "staged" / relative
        target = _contained(canonical, relative)
        staged_present = os.path.lexists(staged)
        target_present = os.path.lexists(target)
        if staged_present and target_present:
            raise RetentionError(
                "retention_recovery_conflict",
                "retention recovery found both staged and restored evidence",
            )
        if staged_present:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
            _fsync_directory(staged.parent)
            _fsync_directory(target.parent)
        elif target_present:
            continue
        else:
            raise RetentionError("retention_transaction_incomplete", "retention transaction lost a target")
    shutil.rmtree(transaction)
    _fsync_directory(canonical)


def recover_pending_retention_transactions(
    data_dir: Path,
    *,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Settle hard-interrupted transactions before a new plan is allowed."""

    canonical = _canonical_data_dir(data_dir)
    effective_runtime = (runtime_dir or default_runtime_dir()).expanduser().resolve()
    with data_dir_lock(canonical):
        pending = _pending_transaction_digests(canonical)
        for digest in pending:
            _recover_transaction(canonical, effective_runtime, digest)
        return {
            "schemaVersion": 1,
            "state": "recovered" if pending else "no_op",
            "recoveredTransactions": len(pending),
        }


def apply_retention_plan(
    data_dir: Path,
    plan: Mapping[str, Any],
    expected_digest: str,
    settings: RuntimeSettings,
    *,
    runtime_dir: Path | None = None,
    mover: Callable[[Path, Path], None] = os.replace,
) -> dict[str, Any]:
    canonical = _canonical_data_dir(data_dir)
    effective_runtime = (runtime_dir or default_runtime_dir()).expanduser().resolve()
    _validate_retention_plan(plan)
    policy = plan["policy"]
    expected_policy = {
        "captureDays": settings.retention_capture_days,
        "generationDays": settings.retention_generation_days,
        "discoverGenerationDays": settings.retention_discover_generation_days,
        "failedCandidateDays": settings.retention_failed_candidate_days,
        "readyCandidateDays": settings.retention_candidate_days,
        "candidateLatestCount": settings.retention_candidate_latest_count,
        "temporaryHours": settings.retention_temp_hours,
        "newestReadyPerType": NEWEST_READY_PER_TYPE,
    }
    if policy != expected_policy:
        raise RetentionError(
            "retention_policy_mismatch",
            "apply settings do not match the exact retention plan policy",
        )
    digest = plan.get("planDigest")
    if not isinstance(digest, str) or digest != expected_digest or _plan_digest(plan) != digest:
        raise RetentionError("retention_plan_digest_mismatch", "apply requires the exact plan digest")
    receipt_path = _receipt_path(effective_runtime, digest)
    with data_dir_lock(canonical):
        _recover_transaction(canonical, effective_runtime, digest)
        if receipt_path.exists():
            receipt = load_retention_plan_receipt(receipt_path, digest)
            return {**receipt, "state": "already_applied", "noOp": True}
        created_at = _parse_time(plan.get("createdAt"), field="plan.createdAt")
        current = _build_plan_locked(
            canonical,
            settings,
            now=created_at,
            release_roots=(),
            backup_roots=(),
            operator_artifact_roots=(),
        )
        if current.get("guardDigest") != plan.get("guardDigest"):
            raise RetentionError("retention_protected_set_changed", "protected set changed after the plan was created")
        targets = plan.get("deletions")
        if not isinstance(targets, list):
            raise RetentionError("retention_plan_invalid", "retention plan targets are invalid")
        protected_paths = {
            str(item.get("relativePath"))
            for item in current.get("protected", [])
            if isinstance(item, dict)
        }
        checked: list[tuple[Path, Path, dict[str, Any]]] = []
        for raw in targets:
            if not isinstance(raw, dict):
                raise RetentionError("retention_plan_invalid", "retention target must be an object")
            relative = _safe_relative(raw.get("relativePath"))
            if _is_protected(relative.as_posix(), {item: set() for item in protected_paths}):
                raise RetentionError("retention_protected_target", "retention target became protected")
            target = _contained(canonical, relative)
            if not os.path.lexists(target):
                raise RetentionError("retention_target_changed", "retention target disappeared")
            snapshot = _tree_snapshot(target, canonical)
            for field in ("identityDigest", "contentDigest", "bytes", "fileCount"):
                if snapshot.get(field) != raw.get(field):
                    raise RetentionError("retention_target_changed", "retention target changed after planning")
            checked.append((relative, target, raw))
        if not checked:
            receipt = {
                "schemaVersion": 1,
                "policyVersion": RETENTION_POLICY_VERSION,
                "planDigest": digest,
                "appliedAt": _iso(datetime.now(timezone.utc)),
                "deletedTargets": 0,
                "deletedFiles": 0,
                "deletedBytes": 0,
                "noOp": True,
            }
            _atomic_json(receipt_path, receipt)
            return {**receipt, "state": "completed"}
        transaction = canonical / f".retention-transaction-{digest}"
        transaction.mkdir()
        _fsync_directory(canonical)
        transaction_manifest = {
            "schemaVersion": 1,
            "planDigest": digest,
            "targets": [item[0].as_posix() for item in checked],
        }
        _atomic_json(transaction / "transaction.json", transaction_manifest)
        moved: list[tuple[Path, Path]] = []
        try:
            for relative, target, _ in checked:
                staged = transaction / "staged" / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                mover(target, staged)
                _fsync_directory(target.parent)
                _fsync_directory(staged.parent)
                moved.append((staged, target))
            receipt = {
                "schemaVersion": 1,
                "policyVersion": RETENTION_POLICY_VERSION,
                "planDigest": digest,
                "appliedAt": _iso(datetime.now(timezone.utc)),
                "deletedTargets": len(checked),
                "deletedFiles": sum(int(item[2]["fileCount"]) for item in checked),
                "deletedBytes": sum(int(item[2]["bytes"]) for item in checked),
                "noOp": False,
            }
            _atomic_json(receipt_path, receipt)
        except Exception as error:
            rollback_errors: list[str] = []
            for staged, target in reversed(moved):
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, target)
                    _fsync_directory(staged.parent)
                    _fsync_directory(target.parent)
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if not rollback_errors:
                shutil.rmtree(transaction, ignore_errors=True)
            if rollback_errors:
                raise RetentionError("retention_rollback_failed", "retention staging failed and rollback was incomplete") from None
            raise RetentionError("retention_apply_failed", f"retention staging failed: {error}") from None
        shutil.rmtree(transaction)
        _fsync_directory(canonical)
        return {**receipt, "state": "completed"}


def load_retention_plan_receipt(path: Path, expected_digest: str) -> dict[str, Any]:
    payload, _ = _read_json(path)
    _require_exact_keys(
        payload,
        {
            "schemaVersion",
            "policyVersion",
            "planDigest",
            "appliedAt",
            "deletedTargets",
            "deletedFiles",
            "deletedBytes",
            "noOp",
        },
        label="retention receipt",
    )
    if (
        payload.get("schemaVersion") != 1
        or payload.get("planDigest") != expected_digest
        or payload.get("policyVersion") != RETENTION_POLICY_VERSION
    ):
        raise RetentionError("retention_receipt_invalid", "retention receipt does not bind the plan")
    _require_sha256(payload.get("planDigest"))
    _parse_time(payload.get("appliedAt"), field="receipt.appliedAt")
    if any(
        not _is_nonnegative_integer(payload.get(field))
        for field in ("deletedTargets", "deletedFiles", "deletedBytes")
    ) or not isinstance(payload.get("noOp"), bool):
        raise RetentionError("retention_receipt_invalid", "retention receipt counts are invalid")
    if payload["noOp"] != (payload["deletedTargets"] == 0):
        raise RetentionError("retention_receipt_invalid", "retention receipt no-op state is inconsistent")
    return payload


def audit_retention(
    data_dir: Path,
    settings: RuntimeSettings,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        plan = create_retention_plan(data_dir, settings, now=now)
        current = resolve_current_generation(data_dir)
        observation_audit = audit_observation_store(data_dir)
        if observation_audit.get("status") not in {"healthy", "degraded"}:
            raise RetentionError(
                "retention_observation_audit_failed",
                "Observation audit did not pass after retention",
            )
        discover_state = "absent"
        pointer = data_dir.expanduser().resolve() / DISCOVER_RELATIVE_ROOT / "current.json"
        if os.path.lexists(pointer):
            discover = resolve_current_discover(data_dir)
            report = audit_discover_generation(discover.root)
            if report.get("status") not in {"healthy", "degraded"}:
                raise RetentionError("retention_discover_audit_failed", "Discover audit did not pass")
            discover_state = str(report.get("status"))
        return {
            "schemaVersion": 1,
            "policyVersion": RETENTION_POLICY_VERSION,
            "status": "healthy",
            "currentGenerationId": current.generation_id,
            "observations": observation_audit.get("status"),
            "discover": discover_state,
            "planDigest": plan["planDigest"],
            "protectedFiles": plan["summary"]["protectedFiles"],
            "protectedBytes": plan["summary"]["protectedBytes"],
            "plannedDeletions": plan["summary"]["plannedDeletions"],
            "issueCount": 0,
        }
    except (RetentionError, GenerationProtocolError, TrendingDiscoverError, TrendingObservationError, OSError) as error:
        return {
            "schemaVersion": 1,
            "policyVersion": RETENTION_POLICY_VERSION,
            "status": "failed",
            "errorCode": str(getattr(error, "code", "retention_audit_failed"))[:100],
            "issueCount": 1,
        }


def _settings_from_plan(plan: Mapping[str, Any]) -> RuntimeSettings:
    policy = plan.get("policy")
    if not isinstance(policy, dict):
        raise RetentionError("retention_plan_invalid", "retention plan policy is invalid")
    environment = dict(os.environ)
    environment.update(
        {
            "RARDAR_RETENTION_CAPTURE_DAYS": str(policy.get("captureDays")),
            "RARDAR_RETENTION_GENERATION_DAYS": str(policy.get("generationDays")),
            "RARDAR_RETENTION_DISCOVER_GENERATION_DAYS": str(
                policy.get("discoverGenerationDays")
            ),
            "RARDAR_RETENTION_FAILED_CANDIDATE_DAYS": str(
                policy.get("failedCandidateDays")
            ),
            "RARDAR_RETENTION_CANDIDATE_DAYS": str(policy.get("readyCandidateDays")),
            "RARDAR_RETENTION_CANDIDATE_LATEST_COUNT": str(
                policy.get("candidateLatestCount")
            ),
            "RARDAR_RETENTION_TEMP_HOURS": str(policy.get("temporaryHours")),
        }
    )
    return load_runtime_settings(environment)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan, apply, and audit bounded Rardar retention")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("RARDAR_DATA_DIR", "data")))
    parser.add_argument("--runtime-dir", type=Path, default=default_runtime_dir())
    commands = parser.add_subparsers(dest="command", required=True)
    plan_command = commands.add_parser("plan")
    plan_command.add_argument("--out", type=Path)
    plan_command.add_argument("--release-root", type=Path, action="append", default=[])
    plan_command.add_argument("--backup-root", type=Path, action="append", default=[])
    plan_command.add_argument("--operator-artifact-root", type=Path, action="append", default=[])
    apply_command = commands.add_parser("apply")
    apply_command.add_argument("--plan", type=Path, required=True)
    apply_command.add_argument("--digest", required=True)
    commands.add_parser("audit")
    arguments = parser.parse_args(argv)
    logger = StructuredLogger("retention", stream=sys.stderr)
    run_id = new_run_id()
    try:
        settings = load_runtime_settings()
        if arguments.command == "plan":
            result = create_retention_plan(
                arguments.data_dir,
                settings,
                release_roots=tuple(arguments.release_root),
                backup_roots=tuple(arguments.backup_root),
                operator_artifact_roots=tuple(arguments.operator_artifact_root),
            )
            if arguments.out:
                write_retention_plan(arguments.out, result)
            logger.emit(
                "retention_plan_created",
                state="completed",
                run_id=run_id,
                operationId=result["planDigest"],
                retentionDeletedFiles=0,
                retentionDeletedBytes=0,
                candidateCount=result["summary"]["plannedDeletions"],
            )
        elif arguments.command == "apply":
            plan = load_retention_plan(arguments.plan)
            settings = _settings_from_plan(plan)
            logger.emit("retention_apply_started", state="running", run_id=run_id, operationId=arguments.digest)
            result = apply_retention_plan(
                arguments.data_dir,
                plan,
                arguments.digest,
                settings,
                runtime_dir=arguments.runtime_dir,
            )
            logger.emit(
                "retention_apply_completed",
                state=str(result["state"]),
                run_id=run_id,
                operationId=arguments.digest,
                retentionDeletedFiles=result["deletedFiles"],
                retentionDeletedBytes=result["deletedBytes"],
            )
        else:
            result = audit_retention(arguments.data_dir, settings)
            if result["status"] != "healthy":
                raise RetentionError(str(result.get("errorCode")), "retention audit failed")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RetentionError, RuntimeSettingsError, OSError) as error:
        code = str(
            getattr(
                error,
                "code",
                "retention_io_failed" if isinstance(error, OSError) else "retention_configuration_failed",
            )
        )[:100]
        logger.emit(
            "retention_apply_failed" if arguments.command == "apply" else "retention_plan_created",
            state="failed",
            level="error",
            run_id=run_id,
            errorCode=code,
        )
        print(json.dumps({"status": "failed", "errorCode": code}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RETENTION_POLICY_VERSION",
    "RetentionError",
    "StorageSnapshot",
    "apply_retention_plan",
    "audit_retention",
    "create_retention_plan",
    "load_retention_plan",
    "recover_pending_retention_transactions",
    "require_discover_storage_capacity",
    "storage_snapshot",
    "write_retention_plan",
]
