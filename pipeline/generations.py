"""Candidate, validation, and atomic publication protocol for Rardar data.

This module deliberately does not collect or derive data.  It gives those
writers an immutable generation boundary:

* clone the currently published data into a private candidate;
* validate every supported JSON artifact, then run the semantic audit;
* compare-and-swap the current pointer while holding the canonical data lock.

Only ``current.json`` is mutable publication state.  A ready generation is
content-addressed by its manifest and must never be edited in place.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Sequence

from pipeline.data_lock import data_dir_lock
from pipeline.schema_validation import (
    strict_json_dumps,
    strict_json_loads,
    validate_data_tree,
)


GenerationOperation = Literal["bootstrap", "refresh", "derive"]

GENERATION_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
OPERATIONS = {"bootstrap", "refresh", "derive"}
STATES = {"building", "ready", "failed"}

POINTER_FIELDS = {
    "schemaVersion",
    "generationId",
    "publishedAt",
    "previousGenerationId",
    "manifestSha256",
}
MANIFEST_FIELDS = {
    "schemaVersion",
    "generationId",
    "createdAt",
    "baseGenerationId",
    "operation",
    "state",
    "failureStage",
    "error",
    "artifacts",
    "hashes",
    "audit",
}
AUDIT_SUMMARY_FIELDS = {"status", "errorCount", "warningCount", "validatedCount"}
FAILURE_STAGES = {"build", "schema_validation", "audit", "manifest", "publish"}

REQUIRED_ARTIFACTS = (
    "snapshots/latest.json",
    "catalog/latest.json",
    "signals/latest.json",
    "signals/enrichment.json",
    "queues/codex.json",
)
OPTIONAL_GLOB_ARTIFACTS = (
    ("snapshots/history", "*.json"),
    ("analysis", "*.json"),
    ("enrichment", "*.json"),
)


class GenerationProtocolError(RuntimeError):
    """A stable, diagnosable generation protocol failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        generation_id: str | None = None,
        stage: str | None = None,
    ) -> None:
        self.code = code
        self.generation_id = generation_id
        self.stage = stage
        super().__init__(message)

    def as_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "generationId": self.generation_id,
            "stage": self.stage,
            "error": str(self),
        }


class CurrentGenerationError(GenerationProtocolError):
    """The current pointer, generation, or legacy fallback is not trustworthy."""


class CandidateGenerationError(GenerationProtocolError):
    """A candidate could not be built or did not pass its publication gates."""


class GenerationConflictError(GenerationProtocolError):
    """Another publisher won, or a stale generation attempted publication."""


@dataclass(frozen=True)
class ResolvedGeneration:
    data_dir: Path
    generation_id: str | None
    root: Path
    pointer: dict[str, Any] | None
    manifest: dict[str, Any] | None
    legacy: bool


@dataclass(frozen=True)
class CandidateGeneration:
    data_dir: Path
    generation_id: str
    path: Path
    base_generation_id: str | None
    operation: GenerationOperation


@dataclass(frozen=True)
class PublicationResult:
    current: ResolvedGeneration
    audit: dict[str, Any]
    rolled_back: bool = False


@dataclass(frozen=True)
class _RecoveryPointerMetadata:
    generation_id: str | None
    published_at: datetime | None


def _utc_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise GenerationProtocolError(
            "invalid_timestamp",
            "generation timestamps must include a timezone",
            stage="contract",
        )
    return current.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not RFC3339_PATTERN.fullmatch(value):
        raise GenerationProtocolError(
            "invalid_timestamp",
            f"{field} must be a timezone-aware RFC3339 timestamp",
            stage="contract",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GenerationProtocolError(
            "invalid_timestamp",
            f"{field} must be a timezone-aware RFC3339 timestamp",
            stage="contract",
        )
    return parsed.astimezone(timezone.utc)


def _validate_generation_id(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not GENERATION_ID_PATTERN.fullmatch(value):
        raise GenerationProtocolError(
            "invalid_generation_id",
            f"unsafe generation id: {value!r}",
            stage="contract",
        )
    if value in {".", "..", ".candidates"}:
        raise GenerationProtocolError(
            "invalid_generation_id",
            f"reserved generation id: {value!r}",
            stage="contract",
        )
    return value


def _canonical_data_dir(data_dir: Path) -> Path:
    raw = Path(data_dir).expanduser().absolute()
    if _is_filesystem_link(raw):
        raise GenerationProtocolError(
            "unsafe_path",
            f"data directory cannot be a symbolic link: {raw}",
            stage="path",
        )
    return raw.resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_filesystem_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _require_safe_existing_path(path: Path, root: Path, *, directory: bool = False) -> None:
    root = root.resolve()
    if _is_filesystem_link(path):
        raise GenerationProtocolError(
            "unsafe_symlink",
            f"generation data cannot contain symbolic links: {path}",
            stage="path",
        )
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise GenerationProtocolError(
            "missing_path",
            f"required generation path is unavailable: {path}: {error}",
            stage="path",
        ) from None
    if not _is_within(resolved, root):
        raise GenerationProtocolError(
            "unsafe_path",
            f"generation path escapes its root: {path}",
            stage="path",
        )
    if directory and not resolved.is_dir():
        raise GenerationProtocolError(
            "invalid_path_type",
            f"expected a generation directory: {path}",
            stage="path",
        )
    if not directory and not resolved.is_file():
        raise GenerationProtocolError(
            "invalid_path_type",
            f"expected a generation file: {path}",
            stage="path",
        )


def _safe_generation_path(data_dir: Path, generation_id: str, *, candidate: bool) -> Path:
    normalized = _validate_generation_id(generation_id)
    assert normalized is not None
    generations_root = data_dir / "generations"
    base = generations_root / ".candidates" if candidate else generations_root
    for container in (generations_root, base):
        if _is_filesystem_link(container):
            raise GenerationProtocolError(
                "unsafe_symlink",
                f"generation container cannot be a symbolic link or junction: {container}",
                generation_id=normalized,
                stage="path",
            )
        if not container.exists():
            continue
        if not container.is_dir() or not _is_within(container.resolve(), data_dir.resolve()):
            raise GenerationProtocolError(
                "unsafe_path",
                f"generation container escapes canonical data: {container}",
                generation_id=normalized,
                stage="path",
            )
    path = base / normalized
    if not _is_within(path.resolve(), base.resolve()):
        raise GenerationProtocolError(
            "unsafe_path",
            f"generation path escapes its container: {path}",
            generation_id=normalized,
            stage="path",
        )
    if os.path.lexists(path) and _is_filesystem_link(path):
        raise GenerationProtocolError(
            "unsafe_symlink",
            f"generation directory cannot be a symbolic link or junction: {path}",
            generation_id=normalized,
            stage="path",
        )
    return path


def _safe_relative_artifact(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise GenerationProtocolError(
            "unsafe_artifact_path",
            f"invalid artifact path: {value!r}",
            stage="manifest",
        )
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise GenerationProtocolError(
            "unsafe_artifact_path",
            f"artifact path must remain inside its generation: {value!r}",
            stage="manifest",
        )
    normalized = relative.as_posix()
    if normalized == "manifest.json" or not normalized.endswith(".json"):
        raise GenerationProtocolError(
            "unsafe_artifact_path",
            f"manifest cannot publish artifact path: {value!r}",
            stage="manifest",
        )
    return normalized


def _read_object(path: Path, *, code: str, stage: str) -> dict[str, Any]:
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GenerationProtocolError(
            code,
            f"cannot read trusted JSON object {path}: {error}",
            stage=stage,
        ) from None
    if not isinstance(payload, dict):
        raise GenerationProtocolError(
            code,
            f"trusted JSON must be an object: {path}",
            stage=stage,
        )
    return payload


def _validate_pointer(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != POINTER_FIELDS:
        raise CurrentGenerationError(
            "current_pointer_invalid",
            "current.json fields do not match the version 1 pointer contract",
            stage="pointer",
        )
    if payload.get("schemaVersion") != 1:
        raise CurrentGenerationError(
            "current_pointer_invalid",
            "current.json schemaVersion must be 1",
            stage="pointer",
        )
    _validate_generation_id(payload.get("generationId"))
    _validate_generation_id(payload.get("previousGenerationId"), nullable=True)
    _parse_timestamp(payload.get("publishedAt"), field="publishedAt")
    if not isinstance(payload.get("manifestSha256"), str) or not SHA256_PATTERN.fullmatch(
        str(payload.get("manifestSha256"))
    ):
        raise CurrentGenerationError(
            "current_pointer_invalid",
            "current.json manifestSha256 must be a lowercase SHA-256 digest",
            stage="pointer",
        )
    return payload


def _read_recovery_pointer_metadata(
    pointer_path: Path,
    canonical_data_dir: Path,
) -> _RecoveryPointerMetadata:
    """Read only safe, non-authoritative metadata from a broken pointer.

    This helper is deliberately narrower than pointer validation.  It never
    follows a symbolic link or junction and never trusts the old manifest
    digest, audit state, or previous-generation field.  Each allowed field is
    independently accepted only when strict JSON parsing and its own contract
    validation succeed.
    """

    empty = _RecoveryPointerMetadata(None, None)
    if not os.path.lexists(pointer_path):
        return empty
    try:
        if _is_filesystem_link(pointer_path):
            return empty
        before = pointer_path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            return empty
        _require_safe_existing_path(pointer_path, canonical_data_dir)
        payload = _read_object(
            pointer_path,
            code="current_pointer_invalid",
            stage="recovery",
        )
        _require_safe_existing_path(pointer_path, canonical_data_dir)
        after = pointer_path.stat(follow_symlinks=False)
    except (GenerationProtocolError, OSError):
        return empty
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        return empty

    generation_id: str | None = None
    try:
        generation_id = _validate_generation_id(payload.get("generationId"))
    except GenerationProtocolError:
        pass

    published_at: datetime | None = None
    try:
        published_at = _parse_timestamp(
            payload.get("publishedAt"),
            field="publishedAt",
        )
    except GenerationProtocolError:
        pass

    return _RecoveryPointerMetadata(generation_id, published_at)


def _validate_audit_summary(value: object, *, nullable: bool) -> dict[str, Any] | None:
    if value is None and nullable:
        return None
    if not isinstance(value, dict) or set(value) != AUDIT_SUMMARY_FIELDS:
        raise GenerationProtocolError(
            "manifest_invalid",
            "manifest audit must be null or a version 1 audit summary",
            stage="manifest",
        )
    if value.get("status") not in {"healthy", "degraded"}:
        raise GenerationProtocolError(
            "manifest_invalid",
            "manifest audit status is invalid",
            stage="manifest",
        )
    for field in ("errorCount", "warningCount", "validatedCount"):
        count = value.get(field)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise GenerationProtocolError(
                "manifest_invalid",
                f"manifest audit {field} must be a non-negative integer",
                stage="manifest",
            )
    if value.get("errorCount") != 0 or int(value.get("validatedCount") or 0) < 1:
        raise GenerationProtocolError(
            "manifest_invalid",
            "a manifest audit summary must represent a passing validation",
            stage="manifest",
        )
    return value


def _validate_manifest(payload: dict[str, Any], *, expected_id: str | None = None) -> dict[str, Any]:
    if set(payload) != MANIFEST_FIELDS:
        raise GenerationProtocolError(
            "manifest_invalid",
            "manifest fields do not match the version 1 generation contract",
            generation_id=expected_id,
            stage="manifest",
        )
    if payload.get("schemaVersion") != 1:
        raise GenerationProtocolError(
            "manifest_invalid",
            "manifest schemaVersion must be 1",
            generation_id=expected_id,
            stage="manifest",
        )
    generation_id = _validate_generation_id(payload.get("generationId"))
    if expected_id is not None and generation_id != expected_id:
        raise GenerationProtocolError(
            "manifest_identity_mismatch",
            f"manifest generationId {generation_id!r} does not match directory {expected_id!r}",
            generation_id=expected_id,
            stage="manifest",
        )
    _validate_generation_id(payload.get("baseGenerationId"), nullable=True)
    _parse_timestamp(payload.get("createdAt"), field="createdAt")
    if payload.get("operation") not in OPERATIONS:
        raise GenerationProtocolError(
            "manifest_invalid",
            f"unsupported generation operation: {payload.get('operation')!r}",
            generation_id=generation_id,
            stage="manifest",
        )
    state = payload.get("state")
    if state not in STATES:
        raise GenerationProtocolError(
            "manifest_invalid",
            f"unsupported generation state: {state!r}",
            generation_id=generation_id,
            stage="manifest",
        )

    artifacts = payload.get("artifacts")
    hashes = payload.get("hashes")
    if not isinstance(artifacts, list) or any(not isinstance(item, str) for item in artifacts):
        raise GenerationProtocolError(
            "manifest_invalid",
            "manifest artifacts must be an array of relative JSON paths",
            generation_id=generation_id,
            stage="manifest",
        )
    normalized = [_safe_relative_artifact(item) for item in artifacts]
    if normalized != sorted(set(normalized)) or (state == "ready" and not normalized):
        raise GenerationProtocolError(
            "manifest_invalid",
            "manifest artifacts must be sorted and unique, and ready manifests cannot be empty",
            generation_id=generation_id,
            stage="manifest",
        )
    if not isinstance(hashes, dict) or set(hashes) != set(normalized):
        raise GenerationProtocolError(
            "manifest_invalid",
            "manifest hashes must contain exactly one digest per artifact",
            generation_id=generation_id,
            stage="manifest",
        )
    if any(not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value) for value in hashes.values()):
        raise GenerationProtocolError(
            "manifest_invalid",
            "manifest artifact hashes must be lowercase SHA-256 digests",
            generation_id=generation_id,
            stage="manifest",
        )

    failure_stage = payload.get("failureStage")
    error = payload.get("error")
    audit = _validate_audit_summary(payload.get("audit"), nullable=True)
    if state == "ready":
        if failure_stage is not None or error is not None or audit is None or audit["status"] == "failed":
            raise GenerationProtocolError(
                "manifest_invalid",
                "a ready manifest requires a passing audit and no failure fields",
                generation_id=generation_id,
                stage="manifest",
            )
    elif state == "building":
        if failure_stage is not None or error is not None or audit is not None:
            raise GenerationProtocolError(
                "manifest_invalid",
                "a building manifest cannot claim validation or failure results",
                generation_id=generation_id,
                stage="manifest",
            )
    else:
        if failure_stage not in FAILURE_STAGES or not isinstance(error, str) or not error:
            raise GenerationProtocolError(
                "manifest_invalid",
                "a failed manifest requires failureStage and error",
                generation_id=generation_id,
                stage="manifest",
            )
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise GenerationProtocolError(
            "artifact_read_failed",
            f"generation artifact could not be hashed: {path}: {error}",
            stage="integrity",
        ) from None
    return digest.hexdigest()


def _manifest_sha256(root: Path) -> str:
    return _sha256(root / "manifest.json")


def _supported_artifact_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for relative in REQUIRED_ARTIFACTS:
        path = root / Path(relative)
        if path.exists():
            paths.append(path)
    for directory, pattern in OPTIONAL_GLOB_ARTIFACTS:
        parent = root / Path(directory)
        if parent.exists():
            paths.extend(sorted(parent.glob(pattern)))
    return sorted(set(paths), key=lambda path: path.relative_to(root).as_posix())


def _artifact_inventory(
    root: Path,
    *,
    require_complete: bool = False,
) -> tuple[list[str], dict[str, str]]:
    try:
        return _artifact_inventory_unchecked(root, require_complete=require_complete)
    except GenerationProtocolError:
        raise
    except OSError as error:
        raise GenerationProtocolError(
            "artifact_inventory_failed",
            f"generation artifact inventory could not be read: {root}: {error}",
            stage="integrity",
        ) from None


def _artifact_inventory_unchecked(
    root: Path,
    *,
    require_complete: bool,
) -> tuple[list[str], dict[str, str]]:
    root = root.resolve()
    _require_safe_existing_path(root, root, directory=True)
    missing = [relative for relative in REQUIRED_ARTIFACTS if not (root / relative).is_file()]
    if require_complete and missing:
        raise CandidateGenerationError(
            "missing_required_artifact",
            "generation is missing required artifacts: " + ", ".join(missing),
            stage="manifest",
        )
    artifact_paths = _supported_artifact_paths(root)
    relative_paths: list[str] = []
    hashes: dict[str, str] = {}
    for path in artifact_paths:
        _require_safe_existing_path(path, root)
        relative = path.relative_to(root).as_posix()
        normalized = _safe_relative_artifact(relative)
        relative_paths.append(normalized)
        hashes[normalized] = _sha256(path)

    known = set(relative_paths)
    unexpected: list[str] = []
    for path in root.rglob("*.json"):
        if path == root / "manifest.json":
            continue
        _require_safe_existing_path(path, root)
        relative = path.relative_to(root).as_posix()
        if relative not in known:
            unexpected.append(relative)
    if unexpected:
        raise GenerationProtocolError(
            "unsupported_artifact_path",
            f"generation contains unsupported JSON artifacts: {', '.join(sorted(unexpected)[:5])}",
            stage="manifest",
        )
    return relative_paths, hashes


def _replace_file(source: Path, target: Path) -> None:
    os.replace(source, target)


def _replace_directory(source: Path, target: Path) -> None:
    os.replace(source, target)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    serialized = strict_json_dumps(payload) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_file(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _load_manifest(root: Path, *, expected_id: str | None = None) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    _require_safe_existing_path(manifest_path, root.resolve())
    payload = _read_object(manifest_path, code="manifest_invalid", stage="manifest")
    return _validate_manifest(payload, expected_id=expected_id)


def _write_manifest(root: Path, payload: dict[str, Any]) -> None:
    expected_id = str(payload.get("generationId") or "")
    _validate_manifest(payload, expected_id=expected_id)
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        existing = _load_manifest(root, expected_id=expected_id)
        if existing.get("state") == "ready":
            raise CandidateGenerationError(
                "generation_immutable",
                f"ready generation cannot be modified: {expected_id}",
                generation_id=expected_id,
                stage="manifest",
            )
    _atomic_write_json(manifest_path, payload)


def _audit_summary(audit: dict[str, Any], validated_count: int) -> dict[str, Any]:
    return {
        "status": audit.get("status"),
        "errorCount": int(audit.get("errorCount") or 0),
        "warningCount": int(audit.get("warningCount") or 0),
        "validatedCount": validated_count,
    }


def _schema_report(root: Path) -> tuple[list[str], int]:
    issues: list[str] = []
    results = validate_data_tree(root)
    for result in results:
        for issue in result.issues:
            source = issue.source_path or result.kind.value
            issues.append(f"{source} {issue.instance_path}: {issue.message}")
    return issues, len(results)


def _audit_generation(root: Path) -> dict[str, Any]:
    # Lazy import avoids a cycle once audit_data learns how to resolve current.
    from pipeline.audit_data import audit_data

    return audit_data(root)


def _run_publication_checks(root: Path, generation_id: str) -> tuple[dict[str, Any], int]:
    schema_issues, validated_count = _schema_report(root)
    if schema_issues:
        raise CandidateGenerationError(
            "schema_validation_failed",
            "candidate Schema validation failed: " + "; ".join(schema_issues[:5]),
            generation_id=generation_id,
            stage="schema",
        )
    try:
        audit = _audit_generation(root)
    except Exception as error:
        raise CandidateGenerationError(
            "audit_failed",
            f"candidate audit could not complete: {error}",
            generation_id=generation_id,
            stage="audit",
        ) from None
    if audit.get("status") == "failed":
        codes = [
            str(item.get("code"))
            for item in audit.get("issues", [])[:5]
            if isinstance(item, dict)
        ]
        raise CandidateGenerationError(
            "audit_failed",
            "candidate semantic audit failed" + (f": {', '.join(codes)}" if codes else ""),
            generation_id=generation_id,
            stage="audit",
        )
    return audit, validated_count


def _verify_manifest_integrity(
    root: Path,
    generation_id: str,
    *,
    verify_audit: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    _require_safe_existing_path(root, root.resolve(), directory=True)
    manifest = _load_manifest(root, expected_id=generation_id)
    if manifest.get("state") != "ready":
        raise CandidateGenerationError(
            "candidate_not_ready",
            f"generation is not ready for publication: {generation_id}",
            generation_id=generation_id,
            stage="manifest",
        )
    try:
        artifacts, hashes = _artifact_inventory(root, require_complete=True)
    except GenerationProtocolError as error:
        if isinstance(error, CandidateGenerationError) and error.generation_id == generation_id:
            raise
        raise CandidateGenerationError(
            error.code,
            str(error),
            generation_id=generation_id,
            stage=error.stage or "integrity",
        ) from None
    except OSError as error:
        raise CandidateGenerationError(
            "artifact_inventory_failed",
            f"generation artifact inventory could not be read: {root}: {error}",
            generation_id=generation_id,
            stage="integrity",
        ) from None
    if artifacts != manifest.get("artifacts") or hashes != manifest.get("hashes"):
        raise CandidateGenerationError(
            "integrity_mismatch",
            f"generation artifacts no longer match ready manifest: {generation_id}",
            generation_id=generation_id,
            stage="integrity",
        )
    schema_issues, validated_count = _schema_report(root)
    if schema_issues:
        raise CandidateGenerationError(
            "schema_validation_failed",
            "generation Schema validation failed: " + "; ".join(schema_issues[:5]),
            generation_id=generation_id,
            stage="schema_validation",
        )
    manifest_audit = manifest.get("audit")
    if not isinstance(manifest_audit, dict) or manifest_audit.get("validatedCount") != validated_count:
        raise CandidateGenerationError(
            "schema_summary_mismatch",
            f"generation Schema count no longer matches ready manifest: {generation_id}",
            generation_id=generation_id,
            stage="schema_validation",
        )

    audit: dict[str, Any] | None = None
    if verify_audit:
        try:
            audit = _audit_generation(root)
        except Exception as error:
            raise CandidateGenerationError(
                "audit_failed",
                f"generation audit could not complete: {error}",
                generation_id=generation_id,
                stage="audit",
            ) from None
        if audit.get("status") == "failed":
            raise CandidateGenerationError(
                "audit_failed",
                f"generation semantic audit failed: {generation_id}",
                generation_id=generation_id,
                stage="audit",
            )
        if _audit_summary(audit, validated_count) != manifest.get("audit"):
            raise CandidateGenerationError(
                "audit_summary_mismatch",
                f"generation audit no longer matches ready manifest: {generation_id}",
                generation_id=generation_id,
                stage="audit",
            )
    return manifest, audit


def _legacy_generation(data_dir: Path, *, verify_audit: bool) -> ResolvedGeneration:
    missing = [relative for relative in REQUIRED_ARTIFACTS if not (data_dir / relative).is_file()]
    if missing:
        raise CurrentGenerationError(
            "legacy_data_unavailable",
            "current.json is absent and the legacy data tree is incomplete: " + ", ".join(missing),
            stage="resolve",
        )
    for path in _supported_artifact_paths(data_dir):
        _require_safe_existing_path(path, data_dir)
    schema_issues, _ = _schema_report(data_dir)
    if schema_issues:
        raise CurrentGenerationError(
            "legacy_schema_invalid",
            "legacy fallback failed Schema validation: " + "; ".join(schema_issues[:5]),
            stage="resolve",
        )
    if verify_audit:
        audit = _audit_generation(data_dir)
        if audit.get("status") == "failed":
            raise CurrentGenerationError(
                "legacy_audit_failed",
                "legacy fallback failed the semantic data audit",
                stage="resolve",
            )
    return ResolvedGeneration(data_dir, None, data_dir, None, None, True)


def resolve_current_generation(
    data_dir: Path,
    *,
    verify_audit: bool = True,
) -> ResolvedGeneration:
    """Resolve one authoritative data root, never falling back past a pointer.

    The pre-generation flat layout is accepted only when ``current.json`` is
    genuinely absent.  A present but malformed pointer, missing target,
    digest mismatch, Schema failure, or audit failure is fatal.
    """

    canonical = _canonical_data_dir(data_dir)
    pointer_path = canonical / "current.json"
    if not os.path.lexists(pointer_path):
        return _legacy_generation(canonical, verify_audit=verify_audit)
    try:
        _require_safe_existing_path(pointer_path, canonical)
        pointer = _validate_pointer(
            _read_object(pointer_path, code="current_pointer_invalid", stage="pointer")
        )
    except GenerationProtocolError as error:
        if isinstance(error, CurrentGenerationError):
            raise
        raise CurrentGenerationError(
            error.code,
            str(error),
            stage=error.stage,
        ) from None
    generation_id = str(pointer["generationId"])
    root = _safe_generation_path(canonical, generation_id, candidate=False)
    if not root.exists():
        raise CurrentGenerationError(
            "current_generation_missing",
            f"current generation directory is missing: {root}",
            generation_id=generation_id,
            stage="resolve",
        )
    try:
        _require_safe_existing_path(root, root.resolve(), directory=True)
        _require_safe_existing_path(root / "manifest.json", root.resolve())
        actual_manifest_sha = _manifest_sha256(root)
    except GenerationProtocolError as error:
        raise CurrentGenerationError(
            "manifest_invalid",
            f"current generation manifest is unsafe or unavailable: {error}",
            generation_id=generation_id,
            stage="manifest",
        ) from None
    except OSError as error:
        raise CurrentGenerationError(
            "manifest_invalid",
            f"current generation manifest is unavailable: {error}",
            generation_id=generation_id,
            stage="manifest",
        ) from None
    if pointer["manifestSha256"] != actual_manifest_sha:
        raise CurrentGenerationError(
            "manifest_digest_mismatch",
            f"current pointer manifest digest does not match generation {generation_id}",
            generation_id=generation_id,
            stage="integrity",
        )
    try:
        manifest, _ = _verify_manifest_integrity(
            root,
            generation_id,
            verify_audit=verify_audit,
        )
    except GenerationProtocolError as error:
        raise CurrentGenerationError(
            error.code,
            str(error),
            generation_id=generation_id,
            stage=error.stage,
        ) from None
    return ResolvedGeneration(canonical, generation_id, root, pointer, manifest, False)


def resolve_current_artifacts(
    data_dir: Path,
    relative_paths: Sequence[str],
) -> tuple[ResolvedGeneration, dict[str, Path]]:
    """Resolve multiple page/baseline inputs against one pointer read."""

    current = resolve_current_generation(data_dir)
    available = set(_artifact_inventory(current.root, require_complete=True)[0])
    resolved: dict[str, Path] = {}
    for value in relative_paths:
        relative = _safe_relative_artifact(value)
        if relative not in available:
            raise CurrentGenerationError(
                "artifact_not_in_generation",
                f"current generation does not publish {relative}",
                generation_id=current.generation_id,
                stage="resolve",
            )
        resolved[relative] = current.root / Path(relative)
    return current, resolved


def _copy_file(source: Path, destination: Path, source_root: Path, destination_root: Path) -> None:
    _require_safe_existing_path(source, source_root.resolve())
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.is_symlink():
        raise GenerationProtocolError(
            "unsafe_symlink",
            f"candidate destination cannot be a symbolic link: {destination}",
            stage="write",
        )
    if not _is_within(destination.resolve(), destination_root.resolve()):
        raise GenerationProtocolError(
            "unsafe_path",
            f"candidate destination escapes its root: {destination}",
            stage="write",
        )
    shutil.copy2(source, destination)


def _copy_supported_artifacts(source_root: Path, destination_root: Path) -> None:
    for source in _supported_artifact_paths(source_root):
        relative = source.relative_to(source_root)
        _copy_file(source, destination_root / relative, source_root, destination_root)


def _overlay_flat_staging(data_dir: Path, destination_root: Path) -> None:
    """Overlay only missing or provably newer flat staging evidence.

    A v0 or unversioned source may populate a missing artifact for historical
    compatibility, but it can never replace an artifact already retained in
    the current generation.  Existing files require two valid source-version
    timestamps and a strict ``source > target`` comparison.
    """

    candidates: list[Path] = []
    for directory in (data_dir / "analysis", data_dir / "enrichment"):
        if directory.exists():
            if directory.is_symlink():
                raise GenerationProtocolError(
                    "unsafe_symlink",
                    f"flat staging directory cannot be a symbolic link: {directory}",
                    stage="write",
                )
            candidates.extend(sorted(directory.glob("*.json")))
    signal_enrichment = data_dir / "signals" / "enrichment.json"
    if signal_enrichment.exists():
        candidates.append(signal_enrichment)
    for source in candidates:
        relative = source.relative_to(data_dir)
        target = destination_root / relative
        if not target.exists():
            _copy_file(source, target, data_dir, destination_root)
            continue

        field = (
            "analyzed_at"
            if relative.parts[0] == "analysis"
            else "analyzedAt"
            if relative.parts[0] == "enrichment"
            else "generatedAt"
        )
        try:
            source_payload = _read_object(
                source,
                code="flat_staging_invalid",
                stage="build",
            )
            target_payload = _read_object(
                target,
                code="candidate_write_failed",
                stage="build",
            )
            source_version = source_payload.get(
                "schemaVersion",
                source_payload.get("schema_version"),
            )
            if not isinstance(source_version, int) or isinstance(source_version, bool) or source_version < 1:
                continue
            source_time = _parse_timestamp(source_payload.get(field), field=f"staging.{field}")
            target_time = _parse_timestamp(target_payload.get(field), field=f"current.{field}")
        except GenerationProtocolError:
            continue
        if source_time > target_time:
            _copy_file(source, target, data_dir, destination_root)


def _rebuild_candidate_queue_paths(destination_root: Path, generation_id: str) -> None:
    """Bind queue evidence inputs to the candidate's eventual immutable path."""

    from pipeline.codex_queue import build_codex_queue

    catalog = _read_object(
        destination_root / "catalog" / "latest.json",
        code="candidate_write_failed",
        stage="build",
    )
    signals = _read_object(
        destination_root / "signals" / "latest.json",
        code="candidate_write_failed",
        stage="build",
    )
    existing = _read_object(
        destination_root / "queues" / "codex.json",
        code="candidate_write_failed",
        stage="build",
    )
    generated_at = _parse_timestamp(existing.get("generatedAt"), field="queue.generatedAt")
    scope = existing.get("scope") if isinstance(existing.get("scope"), dict) else {}
    project_limit = scope.get("projectLimit", 5)
    signal_limit = scope.get("signalLimit", 10)
    if not isinstance(project_limit, int) or isinstance(project_limit, bool):
        project_limit = 5
    if not isinstance(signal_limit, int) or isinstance(signal_limit, bool):
        signal_limit = 10
    queue = build_codex_queue(
        catalog,
        signals,
        destination_root / "enrichment",
        destination_root / "signals" / "enrichment.json",
        generated_at,
        max(0, min(project_limit, 30)),
        max(0, min(signal_limit, 30)),
        input_data_prefix=f"data/generations/{generation_id}",
    )
    _atomic_write_json(destination_root / "queues" / "codex.json", queue)


def _building_manifest(
    generation_id: str,
    created_at: str,
    base_generation_id: str | None,
    operation: GenerationOperation,
    artifacts: list[str],
    hashes: dict[str, str],
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "generationId": generation_id,
        "createdAt": created_at,
        "baseGenerationId": base_generation_id,
        "operation": operation,
        "state": "building",
        "failureStage": None,
        "error": None,
        "artifacts": artifacts,
        "hashes": hashes,
        "audit": None,
    }


def _candidate_from_manifest(data_dir: Path, path: Path, manifest: dict[str, Any]) -> CandidateGeneration:
    return CandidateGeneration(
        data_dir=data_dir,
        generation_id=str(manifest["generationId"]),
        path=path,
        base_generation_id=manifest.get("baseGenerationId"),
        operation=manifest["operation"],
    )


def create_candidate_generation(
    data_dir: Path,
    operation: GenerationOperation,
    *,
    generation_id: str | None = None,
    created_at: datetime | None = None,
    overlay_flat_staging: bool = True,
) -> CandidateGeneration:
    """Clone current data into a unique, unpublished candidate directory."""

    if operation not in OPERATIONS:
        raise CandidateGenerationError(
            "invalid_operation",
            f"unsupported generation operation: {operation!r}",
            stage="contract",
        )
    canonical = _canonical_data_dir(data_dir)
    current = resolve_current_generation(canonical)
    if current.legacy != (operation == "bootstrap"):
        expected = "bootstrap" if current.legacy else "refresh or derive"
        raise CandidateGenerationError(
            "invalid_operation",
            f"current data state requires a {expected} generation",
            stage="contract",
        )
    identifier = generation_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:12]
    )
    normalized = _validate_generation_id(identifier)
    assert normalized is not None
    candidate_path = _safe_generation_path(canonical, normalized, candidate=True)
    final_path = _safe_generation_path(canonical, normalized, candidate=False)
    if candidate_path.exists() or final_path.exists():
        raise CandidateGenerationError(
            "generation_exists",
            f"generation id is already present: {normalized}",
            generation_id=normalized,
            stage="create",
        )
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        candidate_path.mkdir()
    except FileExistsError:
        raise CandidateGenerationError(
            "generation_exists",
            f"generation id is already present: {normalized}",
            generation_id=normalized,
            stage="create",
        ) from None
    created_at_value = _utc_timestamp(created_at)
    manifest = _building_manifest(
        normalized,
        created_at_value,
        current.generation_id,
        operation,
        [],
        {},
    )
    _write_manifest(candidate_path, manifest)
    candidate = _candidate_from_manifest(canonical, candidate_path, manifest)
    try:
        _copy_supported_artifacts(current.root, candidate_path)
        if overlay_flat_staging and not current.legacy:
            _overlay_flat_staging(canonical, candidate_path)
        _rebuild_candidate_queue_paths(candidate_path, normalized)
        artifacts, hashes = _artifact_inventory(candidate_path, require_complete=True)
        manifest = {**manifest, "artifacts": artifacts, "hashes": hashes}
        _write_manifest(candidate_path, manifest)
    except Exception as error:
        fail_candidate_generation(candidate, "build", str(error))
        if isinstance(error, GenerationProtocolError):
            raise
        raise CandidateGenerationError(
            "candidate_write_failed",
            f"candidate clone failed: {error}",
            generation_id=normalized,
            stage="write",
        ) from None
    return _candidate_from_manifest(canonical, candidate_path, manifest)


def fail_candidate_generation(
    candidate: CandidateGeneration,
    failure_stage: str,
    error: str,
) -> dict[str, Any]:
    """Retain a diagnosable failed candidate without mutating ready/final data.

    Repeated calls are idempotent.  Once a manifest is ready, or once its
    directory has moved into the retained final namespace, this helper only
    returns the existing manifest and never rewrites it.
    """

    if failure_stage not in FAILURE_STAGES:
        raise CandidateGenerationError(
            "invalid_failure_stage",
            f"unsupported generation failure stage: {failure_stage!r}",
            generation_id=candidate.generation_id,
            stage="manifest",
        )
    if not isinstance(error, str) or not error.strip():
        raise CandidateGenerationError(
            "invalid_failure_error",
            "candidate failure diagnostics require a non-empty error",
            generation_id=candidate.generation_id,
            stage="manifest",
        )

    canonical = _canonical_data_dir(candidate.data_dir)
    candidate_path = _safe_generation_path(canonical, candidate.generation_id, candidate=True)
    final_path = _safe_generation_path(canonical, candidate.generation_id, candidate=False)
    if final_path.exists():
        return _load_manifest(final_path, expected_id=candidate.generation_id)
    if not candidate_path.exists():
        raise CandidateGenerationError(
            "candidate_not_found",
            f"candidate directory is unavailable: {candidate.generation_id}",
            generation_id=candidate.generation_id,
            stage="manifest",
        )
    manifest = _load_manifest(candidate_path, expected_id=candidate.generation_id)
    if manifest.get("state") in {"ready", "failed"}:
        return manifest

    try:
        artifacts, hashes = _artifact_inventory(candidate_path)
    except Exception:
        artifacts = list(manifest.get("artifacts") or [])
        hashes = dict(manifest.get("hashes") or {})
    failed = {
        **manifest,
        "state": "failed",
        "failureStage": failure_stage,
        "error": error.strip()[:2000],
        "artifacts": artifacts,
        "hashes": hashes,
        # The v1 audit summary represents only a passing gate.  Failed audit
        # codes remain in ``error`` and scheduler diagnostics.
        "audit": None,
    }
    _write_manifest(candidate_path, failed)
    return failed


def finalize_candidate_generation(candidate: CandidateGeneration) -> dict[str, Any]:
    """Run Schema then semantic audit and seal a candidate as immutable ready."""

    canonical = _canonical_data_dir(candidate.data_dir)
    expected_path = _safe_generation_path(canonical, candidate.generation_id, candidate=True)
    if candidate.path.resolve() != expected_path.resolve() or not expected_path.exists():
        raise CandidateGenerationError(
            "candidate_not_found",
            f"candidate directory is unavailable: {candidate.generation_id}",
            generation_id=candidate.generation_id,
            stage="finalize",
        )
    manifest = _load_manifest(expected_path, expected_id=candidate.generation_id)
    if manifest.get("state") == "ready":
        _verify_manifest_integrity(expected_path, candidate.generation_id, verify_audit=True)
        return manifest
    if manifest.get("state") == "failed":
        raise CandidateGenerationError(
            "candidate_failed",
            f"candidate is retained in failed state: {candidate.generation_id}",
            generation_id=candidate.generation_id,
            stage=str(manifest.get("failureStage") or "finalize"),
        )
    try:
        artifacts, hashes = _artifact_inventory(expected_path, require_complete=True)
        schema_issues, validated_count = _schema_report(expected_path)
        if schema_issues:
            error = "candidate Schema validation failed: " + "; ".join(schema_issues[:5])
            fail_candidate_generation(candidate, "schema_validation", error)
            raise CandidateGenerationError(
                "schema_validation_failed",
                error,
                generation_id=candidate.generation_id,
                stage="schema",
            )
        audit = _audit_generation(expected_path)
        if audit.get("status") == "failed":
            codes = [
                str(item.get("code"))
                for item in audit.get("issues", [])[:5]
                if isinstance(item, dict)
            ]
            error = "candidate semantic audit failed" + (f": {', '.join(codes)}" if codes else "")
            fail_candidate_generation(candidate, "audit", error)
            raise CandidateGenerationError(
                "audit_failed",
                error,
                generation_id=candidate.generation_id,
                stage="audit",
            )
        ready = {
            **manifest,
            "state": "ready",
            "failureStage": None,
            "error": None,
            "artifacts": artifacts,
            "hashes": hashes,
            "audit": _audit_summary(audit, validated_count),
        }
        _write_manifest(expected_path, ready)
        return ready
    except CandidateGenerationError as error:
        fail_candidate_generation(
            candidate,
            error.stage if error.stage in FAILURE_STAGES else "manifest",
            str(error),
        )
        raise
    except Exception as error:
        fail_candidate_generation(candidate, "manifest", str(error))
        raise CandidateGenerationError(
            "candidate_validation_failed",
            f"candidate validation could not complete: {error}",
            generation_id=candidate.generation_id,
            stage="validation",
        ) from None


def _locate_ready_generation(data_dir: Path, generation_id: str) -> Path:
    candidate = _safe_generation_path(data_dir, generation_id, candidate=True)
    final = _safe_generation_path(data_dir, generation_id, candidate=False)
    if candidate.exists() and final.exists():
        raise CandidateGenerationError(
            "generation_collision",
            f"candidate and retained generation both exist: {generation_id}",
            generation_id=generation_id,
            stage="publish",
        )
    if candidate.exists():
        return candidate
    if final.exists():
        return final
    raise CandidateGenerationError(
        "candidate_not_found",
        f"generation is unavailable: {generation_id}",
        generation_id=generation_id,
        stage="publish",
    )


def _snapshot_time(root: Path) -> datetime:
    payload = _read_object(
        root / "snapshots" / "latest.json",
        code="snapshot_unavailable",
        stage="conflict",
    )
    return _parse_timestamp(payload.get("captured_at"), field="snapshot.captured_at")


def _enforce_operation_baseline(
    manifest: dict[str, Any],
    candidate_root: Path,
    current: ResolvedGeneration,
) -> None:
    operation = manifest["operation"]
    if operation == "refresh":
        candidate_snapshot_time = _snapshot_time(candidate_root)
        current_snapshot_time = _snapshot_time(current.root)
        if candidate_snapshot_time <= current_snapshot_time:
            raise GenerationConflictError(
                "stale_generation",
                "refresh candidate snapshot must be newer than the current growth baseline",
                generation_id=str(manifest["generationId"]),
                stage="conflict",
            )
        current_snapshot_path = current.root / "snapshots" / "latest.json"
        current_snapshot = _read_object(
            current_snapshot_path,
            code="snapshot_unavailable",
            stage="conflict",
        )
        base_captured_at = current_snapshot.get("captured_at")
        catalog = _read_object(
            candidate_root / "catalog" / "latest.json",
            code="catalog_unavailable",
            stage="conflict",
        )
        if catalog.get("previousCapturedAt") != base_captured_at:
            raise GenerationConflictError(
                "refresh_previous_capture_mismatch",
                "refresh catalog previousCapturedAt must exactly identify the current snapshot baseline",
                generation_id=str(manifest["generationId"]),
                stage="conflict",
            )

        current_history = {
            path.relative_to(current.root).as_posix(): _sha256(path)
            for path in sorted((current.root / "snapshots" / "history").glob("*.json"))
        }
        for relative, expected_hash in current_history.items():
            candidate_path = candidate_root / Path(relative)
            if not candidate_path.is_file() or _sha256(candidate_path) != expected_hash:
                raise GenerationConflictError(
                    "refresh_history_changed",
                    "refresh candidate must retain every existing history path and byte-exact hash",
                    generation_id=str(manifest["generationId"]),
                    stage="conflict",
                )

        archived_base: list[Path] = []
        for path in sorted((candidate_root / "snapshots" / "history").glob("*.json")):
            payload = _read_object(
                path,
                code="history_snapshot_unavailable",
                stage="conflict",
            )
            if payload.get("captured_at") == base_captured_at:
                archived_base.append(path)
        if (
            len(archived_base) != 1
            or _sha256(archived_base[0]) != _sha256(current_snapshot_path)
        ):
            raise GenerationConflictError(
                "refresh_base_snapshot_not_archived",
                "refresh history must contain exactly one byte-exact archive of the current snapshot",
                generation_id=str(manifest["generationId"]),
                stage="conflict",
            )
    elif operation == "derive":
        candidate_snapshot = _sha256(candidate_root / "snapshots" / "latest.json")
        current_snapshot = _sha256(current.root / "snapshots" / "latest.json")
        candidate_history = {
            path.relative_to(candidate_root).as_posix(): _sha256(path)
            for path in sorted((candidate_root / "snapshots" / "history").glob("*.json"))
        }
        current_history = {
            path.relative_to(current.root).as_posix(): _sha256(path)
            for path in sorted((current.root / "snapshots" / "history").glob("*.json"))
        }
        if candidate_snapshot != current_snapshot or candidate_history != current_history:
            raise GenerationConflictError(
                "growth_baseline_changed",
                "derive candidate cannot change the published snapshot or history baseline",
                generation_id=str(manifest["generationId"]),
                stage="conflict",
            )


def _pointer_payload(
    generation_id: str,
    previous_generation_id: str | None,
    manifest_sha256: str,
    published_at: datetime | None,
) -> dict[str, Any]:
    payload = {
        "schemaVersion": 1,
        "generationId": generation_id,
        "publishedAt": _utc_timestamp(published_at),
        "previousGenerationId": previous_generation_id,
        "manifestSha256": manifest_sha256,
    }
    return _validate_pointer(payload)


def _require_newer_publication_time(
    current: ResolvedGeneration,
    published_at: datetime | None,
    generation_id: str,
    *,
    stage: str,
) -> None:
    if current.pointer is None:
        return
    requested_time = _parse_timestamp(
        _utc_timestamp(published_at),
        field="publishedAt",
    )
    current_time = _parse_timestamp(current.pointer.get("publishedAt"), field="publishedAt")
    if requested_time <= current_time:
        raise GenerationConflictError(
            "stale_publication_time",
            "publication time must be later than the current pointer",
            generation_id=generation_id,
            stage=stage,
        )


def publish_candidate_generation(
    candidate: CandidateGeneration,
    *,
    published_at: datetime | None = None,
) -> PublicationResult:
    """Publish a candidate with an exact base-generation compare-and-swap."""

    canonical = _canonical_data_dir(candidate.data_dir)
    candidate_path = _safe_generation_path(canonical, candidate.generation_id, candidate=True)
    if candidate_path.exists():
        manifest = _load_manifest(candidate_path, expected_id=candidate.generation_id)
        if manifest.get("state") == "building":
            finalize_candidate_generation(candidate)

    with data_dir_lock(canonical):
        current = resolve_current_generation(canonical)
        if current.generation_id == candidate.generation_id:
            assert current.manifest is not None
            return PublicationResult(current, dict(current.manifest["audit"]))

        root = _locate_ready_generation(canonical, candidate.generation_id)
        manifest, _ = _verify_manifest_integrity(
            root,
            candidate.generation_id,
            verify_audit=False,
        )
        if manifest.get("baseGenerationId") != current.generation_id:
            raise GenerationConflictError(
                "stale_base_generation",
                "candidate base generation no longer matches current; another publisher won",
                generation_id=candidate.generation_id,
                stage="conflict",
            )
        try:
            _enforce_operation_baseline(manifest, root, current)
        except GenerationConflictError:
            raise
        except GenerationProtocolError as error:
            raise CandidateGenerationError(
                error.code,
                str(error),
                generation_id=candidate.generation_id,
                stage=error.stage or "integrity",
            ) from None
        manifest, audit = _verify_manifest_integrity(
            root,
            candidate.generation_id,
            verify_audit=True,
        )
        assert audit is not None
        try:
            manifest_digest = _manifest_sha256(root)
        except GenerationProtocolError as error:
            raise CandidateGenerationError(
                error.code,
                str(error),
                generation_id=candidate.generation_id,
                stage=error.stage or "integrity",
            ) from None

        _require_newer_publication_time(
            current,
            published_at,
            candidate.generation_id,
            stage="conflict",
        )

        final_path = _safe_generation_path(canonical, candidate.generation_id, candidate=False)
        if root == candidate_path:
            if final_path.exists():
                raise CandidateGenerationError(
                    "generation_exists",
                    f"retained generation already exists: {candidate.generation_id}",
                    generation_id=candidate.generation_id,
                    stage="publish",
                )
            final_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                _replace_directory(candidate_path, final_path)
                _fsync_directory(final_path.parent)
            except OSError as error:
                raise CandidateGenerationError(
                    "generation_rename_failed",
                    f"candidate could not be retained on the data filesystem: {error}",
                    generation_id=candidate.generation_id,
                    stage="publish",
                ) from None
        else:
            final_path = root

        pointer = _pointer_payload(
            candidate.generation_id,
            current.generation_id,
            manifest_digest,
            published_at,
        )
        try:
            _atomic_write_json(canonical / "current.json", pointer)
        except OSError as error:
            raise CandidateGenerationError(
                "pointer_write_failed",
                f"current generation pointer was not replaced: {error}",
                generation_id=candidate.generation_id,
                stage="pointer",
            ) from None

        published = ResolvedGeneration(
            data_dir=canonical,
            generation_id=candidate.generation_id,
            root=final_path,
            pointer=pointer,
            manifest=manifest,
            legacy=False,
        )
        return PublicationResult(published, audit)


def _verify_rollback_target(
    canonical: Path,
    generation_id: str,
) -> tuple[Path, dict[str, Any], dict[str, Any], str]:
    """Fully validate a retained rollback target before inspecting current."""

    target = _safe_generation_path(canonical, generation_id, candidate=False)
    if not target.exists():
        raise CandidateGenerationError(
            "rollback_target_missing",
            f"retained rollback generation is unavailable: {generation_id}",
            generation_id=generation_id,
            stage="rollback",
        )

    try:
        # A retained generation has no separate historical digest registry.
        # Fully validate it before hashing, then bind those exact manifest
        # bytes to the replacement pointer and reject a concurrent mutation.
        manifest, audit = _verify_manifest_integrity(
            target,
            generation_id,
            verify_audit=True,
        )
        manifest_digest = _manifest_sha256(target)
        confirmed_manifest = _load_manifest(target, expected_id=generation_id)
        verified_digest = _manifest_sha256(target)
    except CandidateGenerationError:
        raise
    except GenerationProtocolError as error:
        raise CandidateGenerationError(
            error.code,
            str(error),
            generation_id=generation_id,
            stage=error.stage or "integrity",
        ) from None

    if manifest != confirmed_manifest or manifest_digest != verified_digest:
        raise CandidateGenerationError(
            "rollback_target_changed",
            f"rollback target manifest changed during validation: {generation_id}",
            generation_id=generation_id,
            stage="integrity",
        )
    assert audit is not None
    return target, manifest, audit, verified_digest


def _recovery_publication_time(
    previous_published_at: datetime | None,
    published_at: datetime | None,
    generation_id: str,
) -> datetime:
    """Choose a legal recovery time without letting a broken field block repair."""

    requested = _parse_timestamp(
        _utc_timestamp(published_at),
        field="publishedAt",
    )
    if previous_published_at is None or requested > previous_published_at:
        return requested
    if published_at is not None:
        raise GenerationConflictError(
            "stale_publication_time",
            "publication time must be later than the recoverable current pointer time",
            generation_id=generation_id,
            stage="rollback",
        )
    try:
        return previous_published_at + timedelta(microseconds=1)
    except OverflowError:
        raise GenerationConflictError(
            "stale_publication_time",
            "recoverable current pointer time cannot be advanced",
            generation_id=generation_id,
            stage="rollback",
        ) from None


def _confirm_rollback_target_integrity(
    target: Path,
    generation_id: str,
    expected_manifest: dict[str, Any],
    expected_manifest_digest: str,
) -> None:
    """Recheck immutable bytes immediately before publishing the pointer."""

    try:
        confirmed_manifest, _ = _verify_manifest_integrity(
            target,
            generation_id,
            verify_audit=False,
        )
        confirmed_digest = _manifest_sha256(target)
    except CandidateGenerationError:
        raise
    except GenerationProtocolError as error:
        raise CandidateGenerationError(
            error.code,
            str(error),
            generation_id=generation_id,
            stage=error.stage or "integrity",
        ) from None
    if (
        confirmed_manifest != expected_manifest
        or confirmed_digest != expected_manifest_digest
    ):
        raise CandidateGenerationError(
            "rollback_target_changed",
            f"rollback target changed after its full validation: {generation_id}",
            generation_id=generation_id,
            stage="integrity",
        )


def rollback_to_generation(
    data_dir: Path,
    generation_id: str,
    *,
    published_at: datetime | None = None,
) -> PublicationResult:
    """Explicitly repoint current to a retained, revalidated ready generation."""

    canonical = _canonical_data_dir(data_dir)
    normalized = _validate_generation_id(generation_id)
    assert normalized is not None
    with data_dir_lock(canonical):
        target, manifest, audit, manifest_digest = _verify_rollback_target(
            canonical,
            normalized,
        )

        pointer_path = canonical / "current.json"
        current: ResolvedGeneration | None = None
        if os.path.lexists(pointer_path):
            try:
                current = resolve_current_generation(canonical)
            except GenerationProtocolError:
                # Explicit rollback is the disaster-recovery entry point.  It
                # may replace a broken pointer only after the requested target
                # has independently passed every publication gate.
                current = None

        if current is not None and not current.legacy:
            if current.generation_id == normalized:
                assert current.manifest is not None
                return PublicationResult(
                    current,
                    dict(current.manifest["audit"]),
                    rolled_back=True,
                )
            _require_newer_publication_time(
                current,
                published_at,
                normalized,
                stage="rollback",
            )
            previous_generation_id = current.generation_id
            effective_published_at = published_at
        else:
            recovery = _read_recovery_pointer_metadata(pointer_path, canonical)
            previous_generation_id = recovery.generation_id
            effective_published_at = _recovery_publication_time(
                recovery.published_at,
                published_at,
                normalized,
            )

        _confirm_rollback_target_integrity(
            target,
            normalized,
            manifest,
            manifest_digest,
        )
        pointer = _pointer_payload(
            normalized,
            previous_generation_id,
            manifest_digest,
            effective_published_at,
        )
        try:
            _atomic_write_json(pointer_path, pointer)
        except OSError as error:
            raise CandidateGenerationError(
                "pointer_write_failed",
                f"rollback pointer was not replaced: {error}",
                generation_id=normalized,
                stage="rollback",
            ) from None
        resolved = ResolvedGeneration(
            data_dir=canonical,
            generation_id=normalized,
            root=target,
            pointer=pointer,
            manifest=manifest,
            legacy=False,
        )
        return PublicationResult(resolved, audit, rolled_back=True)


def _status_payload(current: ResolvedGeneration) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "state": "legacy" if current.legacy else "published",
        "generationId": current.generation_id,
        "root": str(current.root),
        "publishedAt": (
            current.pointer.get("publishedAt")
            if isinstance(current.pointer, dict)
            else None
        ),
        "previousGenerationId": (
            current.pointer.get("previousGenerationId")
            if isinstance(current.pointer, dict)
            else None
        ),
        "audit": (
            current.manifest.get("audit")
            if isinstance(current.manifest, dict)
            else None
        ),
    }


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and atomically publish Rardar data generations"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="resolve and verify the current generation")
    bootstrap = commands.add_parser(
        "bootstrap",
        help="create and publish the first generation from a valid flat legacy tree",
    )
    bootstrap.add_argument("--generation-id")
    publish = commands.add_parser(
        "publish",
        help="retry publication of one retained candidate or orphan generation",
    )
    publish.add_argument("generation_id")
    rollback = commands.add_parser(
        "rollback",
        help=(
            "fully revalidate and atomically repoint to one retained ready generation, "
            "including recovery from a broken current pointer"
        ),
    )
    rollback.add_argument("generation_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    try:
        if arguments.command == "status":
            payload = _status_payload(resolve_current_generation(arguments.data_dir))
        elif arguments.command == "bootstrap":
            candidate = create_candidate_generation(
                arguments.data_dir,
                "bootstrap",
                generation_id=arguments.generation_id,
            )
            result = publish_candidate_generation(candidate)
            payload = _status_payload(result.current)
        elif arguments.command == "publish":
            canonical = _canonical_data_dir(arguments.data_dir)
            generation_id = _validate_generation_id(arguments.generation_id)
            assert generation_id is not None
            root = _locate_ready_generation(canonical, generation_id)
            manifest = _load_manifest(root, expected_id=generation_id)
            candidate = _candidate_from_manifest(canonical, root, manifest)
            result = publish_candidate_generation(candidate)
            payload = {**_status_payload(result.current), "publicationRetried": True}
        else:
            result = rollback_to_generation(
                arguments.data_dir,
                arguments.generation_id,
            )
            payload = {**_status_payload(result.current), "rolledBack": True}
    except GenerationProtocolError as error:
        print(strict_json_dumps(error.as_dict()), file=sys.stderr)
        return 1
    print(strict_json_dumps(payload))
    return 0


__all__ = [
    "CandidateGeneration",
    "CandidateGenerationError",
    "CurrentGenerationError",
    "GenerationConflictError",
    "GenerationProtocolError",
    "PublicationResult",
    "ResolvedGeneration",
    "create_candidate_generation",
    "fail_candidate_generation",
    "finalize_candidate_generation",
    "main",
    "publish_candidate_generation",
    "resolve_current_artifacts",
    "resolve_current_generation",
    "rollback_to_generation",
]


if __name__ == "__main__":
    raise SystemExit(main())
