"""Resolve one explicitly reviewed flat project-artifact conflict safely.

The normal candidate adoption path intentionally fails closed when a legacy
v1 artifact is not mechanically equivalent to the stable v2 artifact copied
from the current generation.  This module is the narrow recovery tool for that
case.  It never chooses an authority automatically: the caller supplies one
repository, one artifact kind, one decision, and both expected byte hashes.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from pipeline.data_lock import data_dir_lock
from pipeline.generations import (
    CandidateGenerationError,
    GenerationProtocolError,
    resolve_current_generation,
    verify_retained_generation,
)
from pipeline.migrate_project_identity import (
    ProjectIdentityMigrationError,
    _canonical_data_dir,
    _is_filesystem_link,
    _load_artifact,
    _safe_legacy_target_path,
    _safe_stable_target_path,
    _staging_directories,
)
from pipeline.project_identity import (
    PROJECT_ID_VERSION,
    ProjectIdentityError,
    canonicalize_repository,
    project_id_for_repository,
    validate_project_identity,
)
from pipeline.schema_validation import (
    ArtifactKind,
    ArtifactValidationError,
    _is_rfc3339,
    load_validated_json,
    require_valid,
    strict_json_dumps,
    strict_json_loads,
)


TOOL_VERSION = "1"
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

KEEP_STABLE = "keep-stable"
PROMOTE_LEGACY = "promote-legacy"
BLOCKED = "blocked"

DECISION_LABELS = {
    KEEP_STABLE: "KEEP_STABLE_ARCHIVE_LEGACY",
    PROMOTE_LEGACY: "PROMOTE_LEGACY_TO_STABLE",
    BLOCKED: "BLOCKED_UNPROVABLE",
}

REASON_CODES = {
    KEEP_STABLE: "explicit_review_stable_authoritative_time_guard_passed",
    PROMOTE_LEGACY: "explicit_review_legacy_authoritative_time_guard_passed",
    BLOCKED: "explicit_review_authority_cannot_be_proven",
}

STAGING_KINDS = {
    "analysis": ArtifactKind.STATIC_EVIDENCE,
    "enrichment": ArtifactKind.PROJECT_ENRICHMENT,
}

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FileIdentity = tuple[int, int, int, int, int, int]
_EVIDENCE_REFERENCE_PATTERN = re.compile(
    r"^docs/iterations/[A-Za-z0-9._-]+\.md(?:#[A-Za-z0-9._-]+)?$"
)
_AUDIT_RECORD_FIELDS = frozenset(
    {
        "schemaVersion",
        "toolVersion",
        "repository",
        "artifactKind",
        "decision",
        "legacyPath",
        "archivedArtifact",
        "detachedArtifact",
        "stableReferenceGeneration",
        "legacySha256",
        "stableSha256",
        "sourceTimes",
        "sourceUrls",
        "sourceVersions",
        "evidenceReference",
        "evidenceSha256",
        "reasonCode",
        "state",
        "preparedAt",
        "resolvedAt",
    }
)


class ArtifactConflictResolutionError(RuntimeError):
    """A fail-closed resolver error with a stable diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _LegacyArtifact:
    path: Path
    relative_path: str
    kind: ArtifactKind
    repository: str
    project_id: str
    payload: dict[str, Any]
    source_bytes: bytes


@dataclass(frozen=True)
class _Preflight:
    data_dir: Path
    repository: str
    canonical_repository: str
    project_id: str
    kind_name: str
    kind: ArtifactKind
    decision: str
    expected_legacy_sha256: str
    expected_stable_sha256: str
    evidence_reference: str
    evidence_sha256: str
    source_versions: dict[str, dict[str, str]] | None
    legacy_path: Path
    legacy_quarantine_path: Path
    stable_flat_path: Path
    stable_reference_path: Path
    stable_reference_relative_path: str
    stable_reference_generation: str
    stable_payload: dict[str, Any]
    stable_bytes: bytes
    legacy: _LegacyArtifact
    converted_payload: dict[str, Any]
    flat_stable_payload: dict[str, Any] | None
    archive_root: Path
    archive_directory: Path
    archived_artifact_path: Path
    detached_artifact_path: Path
    audit_record_path: Path
    audit_record: dict[str, Any] | None


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalized_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(canonical)


def _validate_expected_sha256(value: str, label: str) -> str:
    if not _SHA256_PATTERN.fullmatch(value):
        raise ArtifactConflictResolutionError(
            "invalid_expected_sha256",
            f"{label} must be exactly 64 lowercase hexadecimal characters",
        )
    return value


def _validate_evidence_reference(value: str) -> str:
    if not _EVIDENCE_REFERENCE_PATTERN.fullmatch(value):
        raise ArtifactConflictResolutionError(
            "invalid_evidence_reference",
            "evidence reference must be a non-secret docs/iterations/*.md path "
            "with an optional safe anchor",
        )
    return value


def _same_path(left: Path, right: Path) -> bool:
    try:
        if os.path.lexists(left) and os.path.lexists(right):
            return os.path.samefile(left, right)
    except OSError:
        pass
    try:
        left = left.resolve(strict=False)
        right = right.resolve(strict=False)
    except (OSError, RuntimeError):
        pass
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path = path.resolve(strict=False)
        parent = parent.resolve(strict=False)
    except (OSError, RuntimeError):
        pass
    try:
        return os.path.commonpath(
            (os.path.normcase(str(path)), os.path.normcase(str(parent)))
        ) == os.path.normcase(str(parent))
    except ValueError:
        return False


def _assert_no_link_ancestors(path: Path, *, code: str, label: str) -> None:
    current = path
    while True:
        if os.path.lexists(current) and _is_filesystem_link(current):
            raise ArtifactConflictResolutionError(
                code,
                f"{label} cannot traverse a symlink, junction, or reparse point: {current}",
            )
        if current == current.parent:
            break
        current = current.parent


def _markdown_anchor_exists(document: str, anchor: str) -> bool:
    for line in document.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match is None:
            continue
        title = match.group(1).strip().casefold()
        slug = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE)
        slug = re.sub(r"[\s-]+", "-", slug).strip("-")
        if slug == anchor.casefold():
            return True
    return False


def _evidence_sha256(reference: str) -> str:
    relative, separator, anchor = reference.partition("#")
    lexical = Path(os.path.abspath(os.fspath(REPOSITORY_ROOT / relative)))
    _assert_no_link_ancestors(
        lexical,
        code="unsafe_evidence_reference",
        label="artifact conflict evidence document",
    )
    if not _is_within(lexical, REPOSITORY_ROOT) or not os.path.lexists(lexical):
        raise ArtifactConflictResolutionError(
            "evidence_reference_unavailable",
            f"artifact conflict evidence document is unavailable: {relative}",
        )
    if _is_filesystem_link(lexical):
        raise ArtifactConflictResolutionError(
            "unsafe_evidence_reference",
            f"artifact conflict evidence document cannot be a filesystem link: {relative}",
        )
    try:
        source_bytes = _read_safe_regular_bytes(
            lexical,
            lexical.parent,
            code="unsafe_evidence_reference",
            label="artifact conflict evidence document",
        )
        document = source_bytes.decode("utf-8")
    except ArtifactConflictResolutionError:
        raise
    except UnicodeDecodeError as error:
        raise ArtifactConflictResolutionError(
            "evidence_reference_unavailable",
            f"artifact conflict evidence document cannot be read as UTF-8: {relative}: {error}",
        ) from None
    if separator and not _markdown_anchor_exists(document, anchor):
        raise ArtifactConflictResolutionError(
            "evidence_anchor_unavailable",
            f"artifact conflict evidence anchor is unavailable: {reference}",
        )
    return _sha256_bytes(source_bytes)


@contextmanager
def _resolver_data_lock(data_dir: Path) -> Iterator[None]:
    try:
        with data_dir_lock(data_dir):
            yield
    except ArtifactConflictResolutionError:
        raise
    except (OSError, TimeoutError) as error:
        raise ArtifactConflictResolutionError(
            "resolution_io_failed",
            f"artifact conflict resolution could not complete an atomic filesystem operation: {error}",
        ) from None


def _git_worktree_root(data_dir: Path) -> Path | None:
    for candidate in (data_dir, *data_dir.parents):
        if os.path.lexists(candidate / ".git"):
            return candidate
    return None


def _default_archive_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "state"
    return base / "Rardar" / "artifact-conflict-resolutions"


def _safe_archive_root(
    archive_dir: Path | None,
    data_dir: Path,
    *,
    create: bool,
) -> Path:
    requested = archive_dir or _default_archive_root()
    lexical = Path(os.path.abspath(os.fspath(requested.expanduser())))
    _assert_no_link_ancestors(
        lexical,
        code="unsafe_archive_path",
        label="artifact conflict archive path",
    )

    worktree_root = _git_worktree_root(data_dir)
    if _is_within(lexical, data_dir) or (
        worktree_root is not None and _is_within(lexical, worktree_root)
    ):
        raise ArtifactConflictResolutionError(
            "archive_inside_protected_tree",
            "artifact conflict archives must be outside the data directory and Git worktree",
        )

    if create:
        try:
            lexical.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ArtifactConflictResolutionError(
                "archive_directory_unavailable",
                f"artifact conflict archive directory could not be created: {lexical}: {error}",
            ) from None
        _assert_no_link_ancestors(
            lexical,
            code="unsafe_archive_path",
            label="artifact conflict archive path",
        )

    if os.path.lexists(lexical):
        if _is_filesystem_link(lexical) or not lexical.is_dir():
            raise ArtifactConflictResolutionError(
                "unsafe_archive_path",
                f"artifact conflict archive root is not a safe directory: {lexical}",
            )
        try:
            resolved = lexical.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ArtifactConflictResolutionError(
                "unsafe_archive_path",
                f"artifact conflict archive root cannot be resolved safely: {error}",
            ) from None
        if not _same_path(resolved, lexical):
            raise ArtifactConflictResolutionError(
                "unsafe_archive_path",
                f"artifact conflict archive root resolves unexpectedly: {lexical}",
            )
    return lexical


def _archive_paths(
    archive_root: Path,
    project_id: str,
    kind_name: str,
    decision: str,
    legacy_sha256: str,
    stable_sha256: str,
    evidence_reference: str,
) -> tuple[Path, Path, Path, Path]:
    material = "\0".join(
        (
            TOOL_VERSION,
            project_id,
            kind_name,
            decision,
            legacy_sha256,
            stable_sha256,
            evidence_reference,
        )
    ).encode("utf-8")
    digest = _sha256_bytes(material)[:24]
    entry_name = f"{project_id}--{kind_name}--{decision}--{digest}"
    entry = archive_root / entry_name
    if not _is_within(entry, archive_root):  # pragma: no cover - fixed safe segments
        raise ArtifactConflictResolutionError(
            "archive_path_escape",
            f"artifact conflict archive entry escapes its root: {entry}",
        )
    return (
        entry,
        entry / "legacy.json",
        entry / "detached-legacy.json",
        entry / "resolution.json",
    )


def _ensure_safe_archive_entry(entry: Path, archive_root: Path, *, create: bool) -> None:
    _assert_no_link_ancestors(
        entry,
        code="unsafe_archive_path",
        label="artifact conflict archive entry",
    )
    if not _is_within(entry, archive_root) or not _same_path(entry.parent, archive_root):
        raise ArtifactConflictResolutionError(
            "archive_path_escape",
            f"artifact conflict archive entry escapes its direct root: {entry}",
        )
    if create:
        try:
            entry.mkdir(parents=False, exist_ok=True)
        except OSError as error:
            raise ArtifactConflictResolutionError(
                "archive_directory_unavailable",
                f"artifact conflict archive entry could not be created: {entry}: {error}",
            ) from None
        _assert_no_link_ancestors(
            entry,
            code="unsafe_archive_path",
            label="artifact conflict archive entry",
        )
    if os.path.lexists(entry) and (_is_filesystem_link(entry) or not entry.is_dir()):
        raise ArtifactConflictResolutionError(
            "unsafe_archive_path",
            f"artifact conflict archive entry is unsafe: {entry}",
        )


def _assert_safe_archive_leaf(path: Path, entry: Path, *, allow_missing: bool) -> None:
    _ensure_safe_archive_entry(entry, entry.parent, create=False)
    if not _same_path(path.parent, entry) or not _is_within(path, entry):
        raise ArtifactConflictResolutionError(
            "archive_path_escape",
            f"artifact conflict archive file escapes its entry: {path}",
        )
    if not os.path.lexists(path):
        if allow_missing:
            return
        raise ArtifactConflictResolutionError(
            "archive_file_missing",
            f"artifact conflict archive file is missing: {path}",
        )
    if _is_filesystem_link(path):
        raise ArtifactConflictResolutionError(
            "unsafe_archive_path",
            f"artifact conflict archive file cannot be a filesystem link: {path}",
        )
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ArtifactConflictResolutionError(
            "unsafe_archive_path",
            f"artifact conflict archive file cannot be inspected safely: {path}: {error}",
        ) from None
    if not stat.S_ISREG(metadata.st_mode) or not _same_path(resolved.parent, entry):
        raise ArtifactConflictResolutionError(
            "unsafe_archive_path",
            f"artifact conflict archive file is not a direct regular file: {path}",
        )


def _write_new_file_atomic(path: Path, payload: bytes) -> None:
    """Create a durable file without replacing a concurrently created target."""

    _assert_safe_archive_leaf(path, path.parent, allow_missing=True)
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
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError as error:
            raise ArtifactConflictResolutionError(
                "archive_write_failed",
                f"artifact conflict archive file could not be created atomically: {path}: {error}",
            ) from None
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _replace_file_atomic(path: Path, payload: bytes) -> None:
    _assert_safe_archive_leaf(path, path.parent, allow_missing=False)
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
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_safe_archive_leaf(path, path.parent, allow_missing=False)
        os.replace(temporary, path)
        temporary = None
    except ArtifactConflictResolutionError:
        raise
    except OSError as error:
        raise ArtifactConflictResolutionError(
            "audit_record_write_failed",
            f"artifact conflict audit record could not be replaced atomically: {path}: {error}",
        ) from None
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _write_new_validated_flat_artifact(
    path: Path,
    kind: ArtifactKind,
    payload: dict[str, Any],
    repository: str,
) -> None:
    """Atomically create, but never replace, one validated flat artifact."""

    if os.path.lexists(path):
        raise ArtifactConflictResolutionError(
            "flat_stable_changed",
            f"flat stable target appeared before its atomic creation: {path}",
        )
    _assert_no_link_ancestors(
        path.parent,
        code="unsafe_flat_stable_path",
        label="flat stable artifact directory",
    )
    try:
        validated = require_valid(
            kind,
            payload,
            source_path=path,
            expected_repository=repository,
        )
        serialized = (strict_json_dumps(validated) + "\n").encode("utf-8")
    except (ArtifactValidationError, TypeError, ValueError) as error:
        raise ArtifactConflictResolutionError(
            "invalid_mechanical_promotion",
            f"flat stable artifact failed validation before write: {error}",
        ) from None

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
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_no_link_ancestors(
            path.parent,
            code="unsafe_flat_stable_path",
            label="flat stable artifact directory",
        )
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise ArtifactConflictResolutionError(
                "flat_stable_changed",
                f"flat stable target appeared during atomic creation: {path}",
            ) from None
        except OSError as error:
            raise ArtifactConflictResolutionError(
                "stable_target_write_failed",
                f"flat stable target could not be created atomically: {path}: {error}",
            ) from None
        _assert_no_link_ancestors(
            path,
            code="unsafe_flat_stable_path",
            label="flat stable artifact",
        )
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not _is_rfc3339(value):
        raise ArtifactConflictResolutionError(
            "untrusted_source_time",
            f"artifact source time {field} is not timezone-qualified RFC3339",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ArtifactConflictResolutionError(
            "untrusted_source_time",
            f"artifact source time {field} is not RFC3339: {value!r}",
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArtifactConflictResolutionError(
            "untrusted_source_time",
            f"artifact source time {field} has no timezone: {value!r}",
        )
    return parsed.astimezone(timezone.utc)


def _source_times(kind_name: str, payload: dict[str, Any]) -> dict[str, str]:
    fields = (
        ("analyzed_at",)
        if kind_name == "analysis"
        else ("sourcePushedAt", "sourceAnalysisAt", "analyzedAt")
    )
    result: dict[str, str] = {}
    for field in fields:
        value = payload.get(field)
        _timestamp(value, field)
        assert isinstance(value, str)
        result[field] = value
    return result


def _source_url(
    repository: str,
    payload: dict[str, Any],
    kind_name: str,
) -> str:
    base = f"https://github.com/{repository}"
    field = "source" if kind_name == "analysis" else "sourceUrl"
    allowed = {base} if kind_name == "analysis" else {base, f"{base}#readme"}
    value = payload.get(field)
    if value not in allowed:
        raise ArtifactConflictResolutionError(
            "source_repository_mismatch",
            "project artifact source URL does not exactly bind the explicit "
            f"repository: expected one of {sorted(allowed)!r}, got {value!r}",
        )
    assert isinstance(value, str)
    return value


def _snapshot_source_version(
    snapshot_path: Path,
    repository: str,
    expected_pushed_at: str,
    analyzed_at: str,
) -> dict[str, str]:
    _timestamp(expected_pushed_at, "sourcePushedAt")
    if not os.path.lexists(snapshot_path):
        raise ArtifactConflictResolutionError(
            "untrusted_source_version",
            f"source snapshot is unavailable: {snapshot_path}",
        )
    try:
        source_bytes = _read_safe_regular_bytes(
            snapshot_path,
            snapshot_path.parent,
            code="unsafe_source_snapshot",
            label="source snapshot",
        )
        snapshot = require_valid(
            ArtifactKind.GITHUB_SNAPSHOT,
            strict_json_loads(source_bytes.decode("utf-8")),
            source_path=snapshot_path,
        )
    except ArtifactConflictResolutionError:
        raise
    except (ArtifactValidationError, TypeError, UnicodeDecodeError, ValueError) as error:
        raise ArtifactConflictResolutionError(
            "untrusted_source_version",
            f"source snapshot cannot be validated: {snapshot_path}: {error}",
        ) from None
    captured_at = snapshot.get("captured_at")
    _timestamp(captured_at, "snapshotCapturedAt")
    repositories = snapshot.get("repositories")
    matches = [
        item
        for item in repositories
        if isinstance(item, dict) and item.get("repo") == repository
    ] if isinstance(repositories, list) else []
    if len(matches) != 1:
        raise ArtifactConflictResolutionError(
            "untrusted_source_version",
            f"source snapshot does not contain exactly one entry for {repository!r}",
        )
    matched = matches[0]
    item_captured_at = matched.get("captured_at")
    _timestamp(item_captured_at, "repositoryCapturedAt")
    if (
        matched.get("pushed_at") != expected_pushed_at
        or matched.get("url") != f"https://github.com/{repository}"
    ):
        raise ArtifactConflictResolutionError(
            "untrusted_source_version",
            "sourcePushedAt and repository URL are not exactly bound by the selected snapshot for "
            f"{repository!r}",
        )
    if item_captured_at != captured_at:
        raise ArtifactConflictResolutionError(
            "untrusted_source_version",
            "repository captured_at does not exactly match its snapshot captured_at",
        )
    if _timestamp(captured_at, "snapshotCapturedAt") > _timestamp(
        analyzed_at, "analyzed_at"
    ):
        raise ArtifactConflictResolutionError(
            "untrusted_source_version",
            "source snapshot was captured after the artifact analysis time",
        )
    assert isinstance(captured_at, str)
    return {
        "sourcePushedAt": expected_pushed_at,
        "snapshotCapturedAt": captured_at,
        "analyzedAt": analyzed_at,
    }


def _source_versions(
    data_dir: Path,
    stable_reference_path: Path,
    repository: str,
    kind_name: str,
    legacy_payload: dict[str, Any],
    stable_payload: dict[str, Any],
    legacy_source_pushed_at: str | None,
    stable_source_pushed_at: str | None,
    *,
    allow_unproven: bool = False,
) -> dict[str, dict[str, str]] | None:
    if kind_name == "analysis":
        if legacy_source_pushed_at is None or stable_source_pushed_at is None:
            if allow_unproven:
                return None
            raise ArtifactConflictResolutionError(
                "source_version_required",
                "analysis conflict resolution requires explicit legacy and stable sourcePushedAt values",
            )
        legacy_analyzed = str(legacy_payload.get("analyzed_at"))
        stable_analyzed = str(stable_payload.get("analyzed_at"))
        stable_root = stable_reference_path.parent.parent
        try:
            return {
                "legacy": _snapshot_source_version(
                    data_dir / "snapshots" / "latest.json",
                    repository,
                    legacy_source_pushed_at,
                    legacy_analyzed,
                ),
                "stable": _snapshot_source_version(
                    stable_root / "snapshots" / "latest.json",
                    repository,
                    stable_source_pushed_at,
                    stable_analyzed,
                ),
            }
        except ArtifactConflictResolutionError as error:
            if allow_unproven and error.code in {
                "source_version_required",
                "untrusted_source_time",
                "untrusted_source_version",
            }:
                return None
            raise

    legacy_pushed = legacy_payload.get("sourcePushedAt")
    stable_pushed = stable_payload.get("sourcePushedAt")
    if legacy_source_pushed_at is not None and legacy_source_pushed_at != legacy_pushed:
        raise ArtifactConflictResolutionError(
            "untrusted_source_version",
            "explicit legacy sourcePushedAt disagrees with project enrichment",
        )
    if stable_source_pushed_at is not None and stable_source_pushed_at != stable_pushed:
        raise ArtifactConflictResolutionError(
            "untrusted_source_version",
            "explicit stable sourcePushedAt disagrees with project enrichment",
        )
    assert isinstance(legacy_pushed, str) and isinstance(stable_pushed, str)
    return {
        "legacy": {
            "sourcePushedAt": legacy_pushed,
            "sourceAnalysisAt": str(legacy_payload.get("sourceAnalysisAt")),
            "analyzedAt": str(legacy_payload.get("analyzedAt")),
        },
        "stable": {
            "sourcePushedAt": stable_pushed,
            "sourceAnalysisAt": str(stable_payload.get("sourceAnalysisAt")),
            "analyzedAt": str(stable_payload.get("analyzedAt")),
        },
    }


def _source_versions_from_record(
    record: dict[str, Any],
    data_dir: Path,
    stable_reference_path: Path,
    repository: str,
    kind_name: str,
    legacy_payload: dict[str, Any],
    stable_payload: dict[str, Any],
    legacy_source_pushed_at: str | None,
    stable_source_pushed_at: str | None,
) -> dict[str, dict[str, str]]:
    """Revalidate a prepared decision without rebinding its legacy snapshot.

    A prepared record freezes the reviewed legacy source version.  The flat
    snapshot is mutable staging and may advance after an interrupted apply, so
    retries trust the exact audited legacy binding while independently
    revalidating the retained stable generation and its snapshot.
    """

    recorded = record.get("sourceVersions")
    if not isinstance(recorded, dict):  # validated by _read_audit_record
        raise ArtifactConflictResolutionError(
            "invalid_audit_record",
            "artifact conflict audit record has no sourceVersions binding",
        )
    legacy = recorded.get("legacy")
    stable = recorded.get("stable")
    if not isinstance(legacy, dict) or not isinstance(stable, dict):
        raise ArtifactConflictResolutionError(
            "invalid_audit_record",
            "artifact conflict audit record has malformed sourceVersions",
        )

    if kind_name == "analysis":
        if legacy_source_pushed_at is None or stable_source_pushed_at is None:
            raise ArtifactConflictResolutionError(
                "source_version_required",
                "analysis conflict retry requires the exact sourcePushedAt values bound by its audit",
            )
        legacy_analyzed = str(legacy_payload.get("analyzed_at"))
        stable_analyzed = str(stable_payload.get("analyzed_at"))
        if legacy.get("analyzedAt") != legacy_analyzed:
            raise ArtifactConflictResolutionError(
                "audit_record_conflict",
                "prepared audit legacy source version no longer matches the archived artifact",
            )
        if legacy_source_pushed_at != legacy.get("sourcePushedAt"):
            raise ArtifactConflictResolutionError(
                "audit_record_conflict",
                "explicit legacy sourcePushedAt disagrees with the prepared audit",
            )
        if stable_source_pushed_at != stable.get("sourcePushedAt"):
            raise ArtifactConflictResolutionError(
                "audit_record_conflict",
                "explicit stable sourcePushedAt disagrees with the prepared audit",
            )
        recorded_stable_pushed = stable.get("sourcePushedAt")
        if not isinstance(recorded_stable_pushed, str):
            raise ArtifactConflictResolutionError(
                "invalid_audit_record",
                "prepared audit stable sourcePushedAt is invalid",
            )
        if _timestamp(legacy.get("snapshotCapturedAt"), "snapshotCapturedAt") > _timestamp(
            legacy_analyzed,
            "analyzed_at",
        ):
            raise ArtifactConflictResolutionError(
                "audit_record_conflict",
                "prepared audit legacy snapshot time exceeds its archived analysis time",
            )
        verified_stable = _snapshot_source_version(
            stable_reference_path.parent.parent / "snapshots" / "latest.json",
            repository,
            recorded_stable_pushed,
            stable_analyzed,
        )
        if verified_stable != stable:
            raise ArtifactConflictResolutionError(
                "audit_record_conflict",
                "retained stable source version disagrees with the prepared audit",
            )
        return {
            "legacy": {str(key): str(value) for key, value in legacy.items()},
            "stable": verified_stable,
        }

    computed = _source_versions(
        data_dir,
        stable_reference_path,
        repository,
        kind_name,
        legacy_payload,
        stable_payload,
        legacy_source_pushed_at,
        stable_source_pushed_at,
    )
    if computed != recorded or computed is None:
        raise ArtifactConflictResolutionError(
            "audit_record_conflict",
            "project enrichment source versions disagree with the prepared audit",
        )
    return computed


def _assert_decision_time_order(
    kind_name: str,
    decision: str,
    legacy_payload: dict[str, Any],
    stable_payload: dict[str, Any],
    source_versions: dict[str, dict[str, str]] | None,
) -> None:
    if decision == BLOCKED:
        return
    if source_versions is None:
        raise ArtifactConflictResolutionError(
            "source_version_required",
            "a writable conflict decision requires fully proven source versions",
        )
    legacy_times = _source_times(kind_name, legacy_payload)
    stable_times = _source_times(kind_name, stable_payload)
    primary = "analyzed_at" if kind_name == "analysis" else "analyzedAt"

    if decision == KEEP_STABLE:
        if _timestamp(stable_times[primary], primary) <= _timestamp(
            legacy_times[primary], primary
        ):
            raise ArtifactConflictResolutionError(
                "blocked_unprovable_time_order",
                "keep-stable requires the ready stable reference analysis time to be strictly newer",
            )
        if _timestamp(
            source_versions["stable"]["sourcePushedAt"], "sourcePushedAt"
        ) < _timestamp(
            source_versions["legacy"]["sourcePushedAt"], "sourcePushedAt"
        ):
            raise ArtifactConflictResolutionError(
                "blocked_unprovable_time_order",
                "keep-stable would regress the explicitly bound sourcePushedAt",
            )
        if kind_name == "enrichment":
            for field in ("sourcePushedAt", "sourceAnalysisAt"):
                if _timestamp(stable_times[field], field) < _timestamp(
                    legacy_times[field], field
                ):
                    raise ArtifactConflictResolutionError(
                        "blocked_unprovable_time_order",
                        f"keep-stable would regress the trusted enrichment source time {field}",
                    )
        return

    if _timestamp(legacy_times[primary], primary) <= _timestamp(
        stable_times[primary], primary
    ):
        raise ArtifactConflictResolutionError(
            "blocked_unprovable_time_order",
            "promote-legacy requires the legacy analysis time to be strictly newer",
        )
    if _timestamp(
        source_versions["legacy"]["sourcePushedAt"], "sourcePushedAt"
    ) < _timestamp(
        source_versions["stable"]["sourcePushedAt"], "sourcePushedAt"
    ):
        raise ArtifactConflictResolutionError(
            "blocked_unprovable_time_order",
            "promote-legacy would regress the explicitly bound sourcePushedAt",
        )
    if kind_name == "enrichment":
        for field in ("sourcePushedAt", "sourceAnalysisAt"):
            if _timestamp(legacy_times[field], field) < _timestamp(
                stable_times[field], field
            ):
                raise ArtifactConflictResolutionError(
                    "blocked_unprovable_time_order",
                    f"promote-legacy would regress the trusted enrichment source time {field}",
                )


def _artifact_directory(data_dir: Path, kind: ArtifactKind) -> Path:
    matches = [
        directory
        for directory, candidate_kind in _staging_directories(data_dir)
        if candidate_kind is kind
    ]
    if len(matches) != 1:
        raise ArtifactConflictResolutionError(
            "staging_directory_unavailable",
            f"flat staging directory for {kind.value} is unavailable",
        )
    return matches[0]


def _legacy_from_active(
    path: Path,
    directory: Path,
    data_dir: Path,
    kind: ArtifactKind,
    repository: str,
) -> _LegacyArtifact:
    try:
        artifact = _load_artifact(path, directory, data_dir, kind)
    except ProjectIdentityMigrationError as error:
        raise ArtifactConflictResolutionError(error.code, str(error)) from None
    if artifact.version != 1:
        raise ArtifactConflictResolutionError(
            "legacy_version_mismatch",
            f"explicit conflict resolution requires a legacy v1 source: {path}",
        )
    if artifact.repository != repository:
        raise ArtifactConflictResolutionError(
            "repository_mismatch",
            f"legacy artifact repository does not exactly match {repository!r}: {artifact.repository!r}",
        )
    return _LegacyArtifact(
        path=artifact.path,
        relative_path=artifact.relative_path,
        kind=artifact.kind,
        repository=artifact.repository,
        project_id=artifact.project_id,
        payload=artifact.payload,
        source_bytes=artifact.source_bytes,
    )


def _legacy_from_archive(
    archived_path: Path,
    legacy_path: Path,
    data_dir: Path,
    kind: ArtifactKind,
    repository: str,
    project_id: str,
    expected_sha256: str,
) -> _LegacyArtifact:
    _assert_safe_archive_leaf(archived_path, archived_path.parent, allow_missing=False)
    try:
        source_bytes = _read_safe_regular_bytes(
            archived_path,
            archived_path.parent,
            code="unsafe_archive_path",
            label="archived legacy artifact",
        )
        payload = strict_json_loads(source_bytes.decode("utf-8"))
        validated = require_valid(kind, payload, source_path=legacy_path)
    except ArtifactConflictResolutionError:
        raise
    except (OSError, UnicodeDecodeError, ArtifactValidationError, TypeError, ValueError) as error:
        raise ArtifactConflictResolutionError(
            "invalid_archived_artifact",
            f"archived legacy artifact is not valid trusted v1 JSON: {archived_path}: {error}",
        ) from None
    if _sha256_bytes(source_bytes) != expected_sha256:
        raise ArtifactConflictResolutionError(
            "archive_sha256_mismatch",
            f"archived legacy artifact SHA-256 does not match the explicit expectation: {archived_path}",
        )
    if not isinstance(validated, dict) or validated.get("schemaVersion") != 1:
        raise ArtifactConflictResolutionError(
            "legacy_version_mismatch",
            f"archived conflict source is not Schema v1: {archived_path}",
        )
    if validated.get("repository") != repository:
        raise ArtifactConflictResolutionError(
            "repository_mismatch",
            f"archived conflict source does not belong to {repository!r}",
        )
    try:
        actual_project_id = project_id_for_repository(repository)
    except ProjectIdentityError as error:  # pragma: no cover - repository checked earlier
        raise ArtifactConflictResolutionError(error.code, str(error)) from None
    if actual_project_id != project_id:
        raise ArtifactConflictResolutionError(
            "project_id_mismatch",
            "archived conflict source does not map to the expected stable project ID",
        )
    return _LegacyArtifact(
        path=legacy_path,
        relative_path=legacy_path.relative_to(data_dir).as_posix(),
        kind=kind,
        repository=repository,
        project_id=project_id,
        payload=validated,
        source_bytes=source_bytes,
    )


def _converted_payload(legacy: _LegacyArtifact, target: Path) -> dict[str, Any]:
    payload = {
        **legacy.payload,
        "schemaVersion": 2,
        "projectIdVersion": PROJECT_ID_VERSION,
        "projectId": legacy.project_id,
    }
    try:
        return require_valid(
            legacy.kind,
            payload,
            source_path=target,
            expected_repository=legacy.repository,
        )
    except (ArtifactValidationError, TypeError, ValueError) as error:
        raise ArtifactConflictResolutionError(
            "invalid_mechanical_promotion",
            f"legacy artifact cannot be mechanically converted to stable v2: {error}",
        ) from None


def _read_audit_record(path: Path) -> dict[str, Any] | None:
    if not os.path.lexists(path):
        return None
    _assert_safe_archive_leaf(path, path.parent, allow_missing=False)
    try:
        source_bytes = _read_safe_regular_bytes(
            path,
            path.parent,
            code="unsafe_archive_path",
            label="artifact conflict audit record",
        )
        payload = strict_json_loads(source_bytes.decode("utf-8"))
    except ArtifactConflictResolutionError:
        raise
    except (UnicodeDecodeError, ValueError) as error:
        raise ArtifactConflictResolutionError(
            "invalid_audit_record",
            f"artifact conflict audit record is invalid JSON: {path}: {error}",
        ) from None
    if not isinstance(payload, dict) or set(payload) != _AUDIT_RECORD_FIELDS:
        raise ArtifactConflictResolutionError(
            "invalid_audit_record",
            f"artifact conflict audit record has an unsupported field set: {path}",
        )
    if type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 1:
        raise ArtifactConflictResolutionError(
            "invalid_audit_record",
            f"artifact conflict audit record has an invalid schemaVersion: {path}",
        )
    if payload.get("toolVersion") != TOOL_VERSION:
        raise ArtifactConflictResolutionError(
            "invalid_audit_record",
            f"artifact conflict audit record has an unsupported toolVersion: {path}",
        )
    string_fields = (
        "repository",
        "artifactKind",
        "decision",
        "legacyPath",
        "archivedArtifact",
        "detachedArtifact",
        "stableReferenceGeneration",
        "legacySha256",
        "stableSha256",
        "evidenceReference",
        "evidenceSha256",
        "reasonCode",
        "state",
        "preparedAt",
    )
    if any(
        not isinstance(payload.get(field), str) or not payload[field]
        for field in string_fields
    ):
        raise ArtifactConflictResolutionError(
            "invalid_audit_record",
            f"artifact conflict audit record has an invalid string field: {path}",
        )
    if (
        payload["artifactKind"] not in STAGING_KINDS
        or payload["decision"] not in DECISION_LABELS.values()
        or payload["archivedArtifact"] != "legacy.json"
        or payload["detachedArtifact"] != "detached-legacy.json"
        or payload["state"] not in {"prepared", "resolved"}
        or not _SHA256_PATTERN.fullmatch(payload["legacySha256"])
        or not _SHA256_PATTERN.fullmatch(payload["stableSha256"])
        or not _SHA256_PATTERN.fullmatch(payload["evidenceSha256"])
        or not _EVIDENCE_REFERENCE_PATTERN.fullmatch(payload["evidenceReference"])
        or not isinstance(payload.get("sourceTimes"), dict)
        or not isinstance(payload.get("sourceUrls"), dict)
        or not isinstance(payload.get("sourceVersions"), dict)
    ):
        raise ArtifactConflictResolutionError(
            "invalid_audit_record",
            f"artifact conflict audit record has invalid typed content: {path}",
        )
    source_times = payload["sourceTimes"]
    source_urls = payload["sourceUrls"]
    source_versions = payload["sourceVersions"]
    expected_time_fields = (
        {"analyzed_at"}
        if payload["artifactKind"] == "analysis"
        else {"sourcePushedAt", "sourceAnalysisAt", "analyzedAt"}
    )
    if set(source_times) != {"legacy", "stable"} or set(source_urls) != {
        "legacy",
        "stable",
    } or set(source_versions) != {"legacy", "stable"}:
        raise ArtifactConflictResolutionError(
            "invalid_audit_record",
            f"artifact conflict audit record has invalid source bindings: {path}",
        )
    for side in ("legacy", "stable"):
        times = source_times.get(side)
        if not isinstance(times, dict) or set(times) != expected_time_fields:
            raise ArtifactConflictResolutionError(
                "invalid_audit_record",
                f"artifact conflict audit record has invalid {side} source times: {path}",
            )
        for field, value in times.items():
            _timestamp(value, str(field))
        if not isinstance(source_urls.get(side), str) or not source_urls[side]:
            raise ArtifactConflictResolutionError(
                "invalid_audit_record",
                f"artifact conflict audit record has an invalid {side} source URL: {path}",
            )
        versions = source_versions.get(side)
        expected_version_fields = (
            {"sourcePushedAt", "snapshotCapturedAt", "analyzedAt"}
            if payload["artifactKind"] == "analysis"
            else {"sourcePushedAt", "sourceAnalysisAt", "analyzedAt"}
        )
        if not isinstance(versions, dict) or set(versions) != expected_version_fields:
            raise ArtifactConflictResolutionError(
                "invalid_audit_record",
                f"artifact conflict audit record has invalid {side} source versions: {path}",
            )
        for field, value in versions.items():
            _timestamp(value, str(field))
    prepared_at = _timestamp(payload["preparedAt"], "preparedAt")
    resolved_at = payload.get("resolvedAt")
    if payload["state"] == "prepared":
        if resolved_at is not None:
            raise ArtifactConflictResolutionError(
                "invalid_audit_record",
                f"prepared artifact conflict audit record has resolvedAt: {path}",
            )
    else:
        resolved = _timestamp(resolved_at, "resolvedAt")
        if resolved < prepared_at:
            raise ArtifactConflictResolutionError(
                "invalid_audit_record",
                f"artifact conflict audit resolvedAt predates preparedAt: {path}",
            )
    return payload


def _record_identity(preflight: _Preflight) -> dict[str, Any]:
    if preflight.source_versions is None:
        raise ArtifactConflictResolutionError(
            "source_version_required",
            "a writable conflict decision requires fully proven source versions",
        )
    return {
        "schemaVersion": 1,
        "toolVersion": TOOL_VERSION,
        "repository": preflight.repository,
        "artifactKind": preflight.kind_name,
        "decision": DECISION_LABELS[preflight.decision],
        "legacyPath": preflight.legacy.relative_path,
        "archivedArtifact": "legacy.json",
        "detachedArtifact": "detached-legacy.json",
        "stableReferenceGeneration": preflight.stable_reference_generation,
        "legacySha256": preflight.expected_legacy_sha256,
        "stableSha256": preflight.expected_stable_sha256,
        "sourceTimes": {
            "legacy": _source_times(preflight.kind_name, preflight.legacy.payload),
            "stable": _source_times(preflight.kind_name, preflight.stable_payload),
        },
        "sourceUrls": {
            "legacy": _source_url(
                preflight.repository, preflight.legacy.payload, preflight.kind_name
            ),
            "stable": _source_url(
                preflight.repository, preflight.stable_payload, preflight.kind_name
            ),
        },
        "sourceVersions": preflight.source_versions,
        "evidenceReference": preflight.evidence_reference,
        "evidenceSha256": preflight.evidence_sha256,
        "reasonCode": REASON_CODES[preflight.decision],
    }


def _verify_record_matches(preflight: _Preflight, record: dict[str, Any]) -> None:
    expected = _record_identity(preflight)
    for field, value in expected.items():
        if record.get(field) != value:
            raise ArtifactConflictResolutionError(
                "audit_record_conflict",
                f"existing artifact conflict audit record disagrees on {field}",
            )
    prepared_at = record.get("preparedAt")
    _timestamp(prepared_at, "preparedAt")
    if record.get("state") == "prepared":
        if record.get("resolvedAt") is not None:
            raise ArtifactConflictResolutionError(
                "invalid_audit_record",
                "prepared artifact conflict audit record cannot have resolvedAt",
            )
    else:
        _timestamp(record.get("resolvedAt"), "resolvedAt")


def _audit_bytes(payload: dict[str, Any]) -> bytes:
    return (strict_json_dumps(payload) + "\n").encode("utf-8")


def _resolve_healthy_current(data_dir: Path) -> Any:
    try:
        current = resolve_current_generation(data_dir)
    except GenerationProtocolError as error:
        raise ArtifactConflictResolutionError(
            "invalid_current_generation",
            f"published generation failed strict validation: {error}",
        ) from None
    if current.legacy or current.generation_id is None or current.manifest is None:
        raise ArtifactConflictResolutionError(
            "current_generation_required",
            "explicit conflict resolution requires a fully validated published generation",
        )
    return current


def _load_stable_reference(
    data_dir: Path,
    repository: str,
    project_id: str,
    kind_name: str,
    kind: ArtifactKind,
    expected_sha256: str,
    *,
    retained_generation: str | None = None,
    current_generation: Any | None = None,
) -> tuple[str, Path, str, dict[str, Any], bytes]:
    if retained_generation is None:
        current = current_generation or _resolve_healthy_current(data_dir)
        generation_id = current.generation_id
        root = current.root
        manifest = current.manifest
    else:
        try:
            verified = verify_retained_generation(
                data_dir,
                retained_generation,
            )
        except (CandidateGenerationError, GenerationProtocolError) as error:
            raise ArtifactConflictResolutionError(
                "invalid_archived_stable_reference",
                "the retained stable generation bound to the audit record failed "
                f"strict validation: {error}",
            ) from None
        generation_id = verified.generation_id
        root = verified.root
        manifest = verified.manifest

    relative = f"{kind_name}/{project_id}.json"
    artifacts = manifest.get("artifacts")
    hashes = manifest.get("hashes")
    if (
        not isinstance(artifacts, list)
        or relative not in artifacts
        or not isinstance(hashes, dict)
        or hashes.get(relative) != expected_sha256
    ):
        raise ArtifactConflictResolutionError(
            "stable_reference_not_manifest_bound",
            "current ready manifest does not bind the expected stable artifact SHA-256",
        )
    path = root / Path(relative)
    try:
        payload = load_validated_json(path, kind, expected_repository=repository)
        source_bytes = path.read_bytes()
    except (OSError, ArtifactValidationError, TypeError, ValueError) as error:
        raise ArtifactConflictResolutionError(
            "invalid_stable_reference",
            f"current ready stable reference cannot be validated: {path}: {error}",
        ) from None
    actual_sha = _sha256_bytes(source_bytes)
    if actual_sha != expected_sha256:
        raise ArtifactConflictResolutionError(
            "stable_sha256_mismatch",
            "current ready stable reference changed or does not match the explicit SHA-256",
        )
    if payload.get("schemaVersion") != 2:
        raise ArtifactConflictResolutionError(
            "stable_reference_version_mismatch",
            "current ready stable reference is not Schema v2",
        )
    try:
        validate_project_identity(
            repository,
            payload.get("projectId"),
            payload.get("projectIdVersion"),
        )
    except ProjectIdentityError as error:
        raise ArtifactConflictResolutionError(
            error.code,
            f"current ready stable reference identity is invalid: {error}",
        ) from None
    if payload.get("repository") != repository or payload.get("projectId") != project_id:
        raise ArtifactConflictResolutionError(
            "stable_reference_identity_mismatch",
            "current ready stable reference does not belong to the explicit repository/projectId",
        )
    return generation_id, path, relative, payload, source_bytes


def _preflight(
    data_dir: Path,
    repository: str,
    kind_name: str,
    decision: str,
    expected_legacy_sha256: str,
    expected_stable_sha256: str,
    evidence_reference: str,
    legacy_source_pushed_at: str | None,
    stable_source_pushed_at: str | None,
    archive_dir: Path | None,
    *,
    create_archive: bool,
) -> _Preflight:
    if kind_name not in STAGING_KINDS:
        raise ArtifactConflictResolutionError(
            "invalid_artifact_kind",
            f"unsupported project artifact kind: {kind_name!r}",
        )
    if decision not in DECISION_LABELS:
        raise ArtifactConflictResolutionError(
            "invalid_decision",
            f"unsupported explicit conflict decision: {decision!r}",
        )
    legacy_sha = _validate_expected_sha256(expected_legacy_sha256, "legacy SHA-256")
    stable_sha = _validate_expected_sha256(expected_stable_sha256, "stable SHA-256")
    evidence_reference = _validate_evidence_reference(evidence_reference)
    evidence_sha = _evidence_sha256(evidence_reference)
    try:
        canonical_repository = canonicalize_repository(repository)
        project_id = project_id_for_repository(repository)
    except ProjectIdentityError as error:
        raise ArtifactConflictResolutionError(error.code, str(error)) from None

    kind = STAGING_KINDS[kind_name]
    directory = _artifact_directory(data_dir, kind)
    try:
        legacy_path = _safe_legacy_target_path(directory, repository)
        stable_flat_path = _safe_stable_target_path(directory, project_id)
    except ProjectIdentityMigrationError as error:
        raise ArtifactConflictResolutionError(error.code, str(error)) from None
    legacy_quarantine_path = legacy_path.with_name(
        f".{legacy_path.name}.{legacy_sha[:16]}.rardar-quarantine"
    )

    archive_root = _safe_archive_root(archive_dir, data_dir, create=create_archive)
    archive_directory, archived_path, detached_path, audit_path = _archive_paths(
        archive_root,
        project_id,
        kind_name,
        decision,
        legacy_sha,
        stable_sha,
        evidence_reference,
    )
    if os.path.lexists(archive_directory):
        _ensure_safe_archive_entry(archive_directory, archive_root, create=False)
    audit_record = _read_audit_record(audit_path) if os.path.lexists(audit_path) else None
    retained_generation: str | None = None
    if audit_record is not None:
        recorded_generation = audit_record.get("stableReferenceGeneration")
        if not isinstance(recorded_generation, str) or not recorded_generation:
            raise ArtifactConflictResolutionError(
                "invalid_audit_record",
                "artifact conflict audit record has no stable reference generation",
            )
        retained_generation = recorded_generation

    # Every invocation validates the currently published generation. Existing
    # prepared/resolved work is additionally bound to its immutable retained
    # reference so a healthy current switch does not break retry/no-op.
    healthy_current = _resolve_healthy_current(data_dir)

    (
        stable_generation,
        stable_reference_path,
        stable_reference_relative,
        stable_payload,
        stable_bytes,
    ) = _load_stable_reference(
        data_dir,
        repository,
        project_id,
        kind_name,
        kind,
        stable_sha,
        retained_generation=retained_generation,
        current_generation=healthy_current,
    )

    if os.path.lexists(legacy_path):
        legacy = _legacy_from_active(
            legacy_path, directory, data_dir, kind, repository
        )
    else:
        if audit_record is None or not os.path.lexists(archived_path):
            raise ArtifactConflictResolutionError(
                "legacy_source_missing",
                f"active legacy artifact is unavailable and no matching audit archive exists: {legacy_path}",
            )
        legacy = _legacy_from_archive(
            archived_path,
            legacy_path,
            data_dir,
            kind,
            repository,
            project_id,
            legacy_sha,
        )

    if _sha256_bytes(legacy.source_bytes) != legacy_sha:
        raise ArtifactConflictResolutionError(
            "legacy_sha256_mismatch",
            "legacy artifact changed or does not match the explicit SHA-256",
        )
    if legacy.project_id != project_id:
        raise ArtifactConflictResolutionError(
            "project_id_mismatch",
            "legacy artifact repository does not map to the stable reference projectId",
        )
    _source_url(repository, legacy.payload, kind_name)
    _source_url(repository, stable_payload, kind_name)
    converted = _converted_payload(legacy, stable_flat_path)
    if audit_record is not None:
        source_versions = _source_versions_from_record(
            audit_record,
            data_dir,
            stable_reference_path,
            repository,
            kind_name,
            legacy.payload,
            stable_payload,
            legacy_source_pushed_at,
            stable_source_pushed_at,
        )
    else:
        source_versions = _source_versions(
            data_dir,
            stable_reference_path,
            repository,
            kind_name,
            legacy.payload,
            stable_payload,
            legacy_source_pushed_at,
            stable_source_pushed_at,
            allow_unproven=decision == BLOCKED,
        )
    _assert_decision_time_order(
        kind_name,
        decision,
        legacy.payload,
        stable_payload,
        source_versions,
    )

    flat_stable: dict[str, Any] | None = None
    if os.path.lexists(stable_flat_path):
        try:
            flat_artifact = _load_artifact(
                stable_flat_path, directory, data_dir, kind
            )
        except ProjectIdentityMigrationError as error:
            raise ArtifactConflictResolutionError(error.code, str(error)) from None
        if (
            flat_artifact.version != 2
            or flat_artifact.repository != repository
            or flat_artifact.project_id != project_id
        ):
            raise ArtifactConflictResolutionError(
                "flat_stable_identity_mismatch",
                "existing flat stable artifact does not belong to the explicit repository/projectId",
            )
        flat_stable = flat_artifact.payload
        if decision == KEEP_STABLE and (
            flat_artifact.source_bytes != stable_bytes
            or flat_artifact.payload != stable_payload
        ):
            raise ArtifactConflictResolutionError(
                "flat_stable_conflict",
                "keep-stable refuses a flat stable target that differs from the immutable current reference",
            )
        if decision == PROMOTE_LEGACY and flat_artifact.payload != converted:
            raise ArtifactConflictResolutionError(
                "flat_stable_conflict",
                "promote-legacy refuses to overwrite a non-equivalent flat stable target",
            )

    preflight = _Preflight(
        data_dir=data_dir,
        repository=repository,
        canonical_repository=canonical_repository,
        project_id=project_id,
        kind_name=kind_name,
        kind=kind,
        decision=decision,
        expected_legacy_sha256=legacy_sha,
        expected_stable_sha256=stable_sha,
        evidence_reference=evidence_reference,
        evidence_sha256=evidence_sha,
        source_versions=source_versions,
        legacy_path=legacy_path,
        legacy_quarantine_path=legacy_quarantine_path,
        stable_flat_path=stable_flat_path,
        stable_reference_path=stable_reference_path,
        stable_reference_relative_path=stable_reference_relative,
        stable_reference_generation=stable_generation,
        stable_payload=stable_payload,
        stable_bytes=stable_bytes,
        legacy=legacy,
        converted_payload=converted,
        flat_stable_payload=flat_stable,
        archive_root=archive_root,
        archive_directory=archive_directory,
        archived_artifact_path=archived_path,
        detached_artifact_path=detached_path,
        audit_record_path=audit_path,
        audit_record=audit_record,
    )
    if audit_record is not None:
        _verify_record_matches(preflight, audit_record)
        if not os.path.lexists(archived_path):
            raise ArtifactConflictResolutionError(
                "archive_file_missing",
                "existing artifact conflict audit record has no archived legacy bytes",
            )
        _assert_safe_archive_leaf(archived_path, archive_directory, allow_missing=False)
        archived_bytes = _read_safe_regular_bytes(
            archived_path,
            archive_directory,
            code="unsafe_archive_path",
            label="archived legacy artifact",
        )
        if _sha256_bytes(archived_bytes) != legacy_sha:
            raise ArtifactConflictResolutionError(
                "archive_sha256_mismatch",
                "archived legacy bytes do not match the audit record",
            )
    detached_exists = os.path.lexists(detached_path)
    if detached_exists:
        if audit_record is None:
            raise ArtifactConflictResolutionError(
                "detached_archive_conflict",
                "a detached legacy archive exists without a matching audit record",
            )
        _assert_safe_archive_leaf(detached_path, archive_directory, allow_missing=False)
        detached_bytes = _read_safe_regular_bytes(
            detached_path,
            archive_directory,
            code="unsafe_archive_path",
            label="detached legacy artifact",
        )
        if detached_bytes != legacy.source_bytes:
            raise ArtifactConflictResolutionError(
                "detached_archive_conflict",
                "detached legacy artifact differs from the reviewed source bytes",
            )
        if os.path.lexists(legacy_path) or os.path.lexists(legacy_quarantine_path):
            raise ArtifactConflictResolutionError(
                "detached_archive_conflict",
                "detached legacy archive cannot coexist with an active or quarantined source",
            )
    if audit_record is not None:
        if audit_record.get("state") == "resolved" and not detached_exists:
            raise ArtifactConflictResolutionError(
                "detached_archive_missing",
                "resolved artifact conflict audit has no retained detached legacy artifact",
            )
        if (
            audit_record.get("state") == "prepared"
            and not os.path.lexists(legacy_path)
            and not os.path.lexists(legacy_quarantine_path)
            and not detached_exists
        ):
            raise ArtifactConflictResolutionError(
                "legacy_cleanup_state_missing",
                "prepared artifact conflict has no active, quarantined, or detached legacy state",
            )
    if os.path.lexists(legacy_quarantine_path):
        if (
            audit_record is None
            or audit_record.get("state") != "prepared"
            or os.path.lexists(legacy_path)
        ):
            raise ArtifactConflictResolutionError(
                "legacy_quarantine_conflict",
                "a legacy quarantine entry exists outside a single interrupted prepared apply",
            )
        quarantined = _read_safe_regular_bytes(
            legacy_quarantine_path,
            directory,
            code="unsafe_legacy_quarantine",
        )
        if quarantined != legacy.source_bytes:
            raise ArtifactConflictResolutionError(
                "legacy_quarantine_conflict",
                "interrupted legacy quarantine bytes differ from the reviewed source",
            )
    return preflight


def _file_identity(metadata: os.stat_result) -> _FileIdentity:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _read_safe_regular_snapshot(
    path: Path,
    directory: Path,
    *,
    code: str,
    label: str = "protected regular file",
) -> tuple[bytes, _FileIdentity]:
    _assert_no_link_ancestors(
        directory,
        code=code,
        label=f"{label} directory",
    )
    if not os.path.lexists(path) or _is_filesystem_link(path):
        raise ArtifactConflictResolutionError(
            code,
            f"{label} is unavailable or is a filesystem link: {path}",
        )
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
        if not stat.S_ISREG(before.st_mode) or not _same_path(resolved.parent, directory):
            raise ArtifactConflictResolutionError(
                code,
                f"{label} is not a direct regular file: {path}",
            )
        with path.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            if _file_identity(opened_before) != _file_identity(before):
                raise ArtifactConflictResolutionError(
                    code,
                    f"{label} changed while it was opened: {path}",
                )
            source_bytes = handle.read()
            opened_after = os.fstat(handle.fileno())
            if _file_identity(opened_after) != _file_identity(opened_before):
                raise ArtifactConflictResolutionError(
                    code,
                    f"{label} changed through its open handle while it was read: {path}",
                )
        after = path.lstat()
    except ArtifactConflictResolutionError:
        raise
    except OSError as error:
        raise ArtifactConflictResolutionError(
            code,
            f"{label} could not be read safely: {path}: {error}",
        ) from None
    if _file_identity(after) != _file_identity(before):
        raise ArtifactConflictResolutionError(
            code,
            f"{label} changed while it was read: {path}",
        )
    return source_bytes, _file_identity(after)


def _read_safe_regular_bytes(
    path: Path,
    directory: Path,
    *,
    code: str,
    label: str = "protected regular file",
) -> bytes:
    source_bytes, _identity = _read_safe_regular_snapshot(
        path,
        directory,
        code=code,
        label=label,
    )
    return source_bytes


def _verify_active_legacy(preflight: _Preflight) -> None:
    if not os.path.lexists(preflight.legacy_path):
        raise ArtifactConflictResolutionError(
            "legacy_source_changed",
            f"active legacy artifact disappeared during apply: {preflight.legacy_path}",
        )
    directory = preflight.legacy_path.parent
    current = _legacy_from_active(
        preflight.legacy_path,
        directory,
        preflight.data_dir,
        preflight.kind,
        preflight.repository,
    )
    if current.source_bytes != preflight.legacy.source_bytes:
        raise ArtifactConflictResolutionError(
            "legacy_source_changed",
            "active legacy artifact changed after preflight",
        )


def _verify_flat_stable(preflight: _Preflight, *, required: bool) -> None:
    if not os.path.lexists(preflight.stable_flat_path):
        if required:
            raise ArtifactConflictResolutionError(
                "flat_stable_changed",
                f"required flat stable artifact is unavailable: {preflight.stable_flat_path}",
            )
        return
    try:
        artifact = _load_artifact(
            preflight.stable_flat_path,
            preflight.stable_flat_path.parent,
            preflight.data_dir,
            preflight.kind,
        )
    except ProjectIdentityMigrationError as error:
        raise ArtifactConflictResolutionError(
            "flat_stable_changed",
            f"flat stable artifact became unsafe or invalid: {error}",
        ) from None
    if (
        artifact.version != 2
        or artifact.repository != preflight.repository
        or artifact.project_id != preflight.project_id
    ):
        raise ArtifactConflictResolutionError(
            "flat_stable_changed",
            "flat stable artifact identity changed after preflight",
        )
    _source_url(preflight.repository, artifact.payload, preflight.kind_name)
    if preflight.decision == KEEP_STABLE:
        matches = (
            artifact.source_bytes == preflight.stable_bytes
            and artifact.payload == preflight.stable_payload
        )
    else:
        matches = artifact.payload == preflight.converted_payload
    if not matches:
        raise ArtifactConflictResolutionError(
            "flat_stable_changed",
            "flat stable artifact differs from the explicitly reviewed authority",
        )


def _verify_stable_reference(preflight: _Preflight) -> None:
    (
        generation,
        path,
        relative,
        payload,
        source_bytes,
    ) = _load_stable_reference(
        preflight.data_dir,
        preflight.repository,
        preflight.project_id,
        preflight.kind_name,
        preflight.kind,
        preflight.expected_stable_sha256,
        retained_generation=preflight.stable_reference_generation,
    )
    if (
        generation != preflight.stable_reference_generation
        or path != preflight.stable_reference_path
        or relative != preflight.stable_reference_relative_path
        or payload != preflight.stable_payload
        or source_bytes != preflight.stable_bytes
    ):
        raise ArtifactConflictResolutionError(
            "stable_reference_changed",
            "current ready stable reference changed after preflight",
        )


def _revalidate_source_versions(
    preflight: _Preflight,
) -> dict[str, dict[str, str]]:
    if preflight.source_versions is None:
        raise ArtifactConflictResolutionError(
            "source_version_required",
            "a writable conflict decision has no proven source version binding",
        )
    if preflight.audit_record is not None:
        current = _source_versions_from_record(
            preflight.audit_record,
            preflight.data_dir,
            preflight.stable_reference_path,
            preflight.repository,
            preflight.kind_name,
            preflight.legacy.payload,
            preflight.stable_payload,
            preflight.source_versions["legacy"]["sourcePushedAt"],
            preflight.source_versions["stable"]["sourcePushedAt"],
        )
    else:
        current = _source_versions(
            preflight.data_dir,
            preflight.stable_reference_path,
            preflight.repository,
            preflight.kind_name,
            preflight.legacy.payload,
            preflight.stable_payload,
            preflight.source_versions["legacy"]["sourcePushedAt"],
            preflight.source_versions["stable"]["sourcePushedAt"],
        )
    if current is None or current != preflight.source_versions:
        raise ArtifactConflictResolutionError(
            "source_version_changed",
            "artifact source authority changed after preflight",
        )
    return current


def _verify_post_quarantine_authority(preflight: _Preflight) -> None:
    """Recheck every mutable authority immediately before irreversible cleanup."""

    _resolve_healthy_current(preflight.data_dir)
    _verify_stable_reference(preflight)
    _verify_flat_stable(
        preflight,
        required=preflight.decision == PROMOTE_LEGACY,
    )
    if _evidence_sha256(preflight.evidence_reference) != preflight.evidence_sha256:
        raise ArtifactConflictResolutionError(
            "evidence_reference_changed",
            "artifact conflict evidence changed after preflight",
        )
    _revalidate_source_versions(preflight)


def _ensure_archived_bytes(preflight: _Preflight) -> None:
    _ensure_safe_archive_entry(
        preflight.archive_directory,
        preflight.archive_root,
        create=True,
    )
    if not os.path.lexists(preflight.archived_artifact_path):
        _write_new_file_atomic(
            preflight.archived_artifact_path,
            preflight.legacy.source_bytes,
        )
    _assert_safe_archive_leaf(
        preflight.archived_artifact_path,
        preflight.archive_directory,
        allow_missing=False,
    )
    archived_bytes = _read_safe_regular_bytes(
        preflight.archived_artifact_path,
        preflight.archive_directory,
        code="archive_read_failed",
        label="archived legacy artifact",
    )
    if archived_bytes != preflight.legacy.source_bytes:
        raise ArtifactConflictResolutionError(
            "archive_conflict",
            "existing archive file differs from the expected legacy source bytes",
        )


def _prepared_record(preflight: _Preflight) -> dict[str, Any]:
    return {
        **_record_identity(preflight),
        "state": "prepared",
        "preparedAt": datetime.now(timezone.utc).isoformat(),
        "resolvedAt": None,
    }


def _ensure_prepared_record(preflight: _Preflight) -> dict[str, Any]:
    existing = _read_audit_record(preflight.audit_record_path)
    if existing is not None:
        _verify_record_matches(preflight, existing)
        return existing
    prepared = _prepared_record(preflight)
    _write_new_file_atomic(preflight.audit_record_path, _audit_bytes(prepared))
    existing = _read_audit_record(preflight.audit_record_path)
    if existing is None:  # pragma: no cover - write helper either creates or raises
        raise ArtifactConflictResolutionError(
            "audit_record_write_failed",
            "prepared artifact conflict audit record was not created",
        )
    _verify_record_matches(preflight, existing)
    return existing


def _mark_resolved(preflight: _Preflight, prepared: dict[str, Any]) -> dict[str, Any]:
    resolved = {
        **prepared,
        "state": "resolved",
        "resolvedAt": datetime.now(timezone.utc).isoformat(),
    }
    _replace_file_atomic(preflight.audit_record_path, _audit_bytes(resolved))
    persisted = _read_audit_record(preflight.audit_record_path)
    if persisted is None:  # pragma: no cover - replace helper either succeeds or raises
        raise ArtifactConflictResolutionError(
            "audit_record_write_failed",
            "resolved artifact conflict audit record disappeared",
        )
    _verify_record_matches(preflight, persisted)
    if persisted.get("state") != "resolved":
        raise ArtifactConflictResolutionError(
            "audit_record_write_failed",
            "artifact conflict audit record did not advance to resolved",
        )
    return persisted


def _move_no_replace(
    source: Path,
    destination: Path,
    *,
    conflict_code: str,
    failure_code: str,
    label: str,
) -> None:
    """Atomically move one same-filesystem path without replacing a peer."""

    try:
        if os.name == "nt":
            # MoveFile on Windows fails when the destination already exists.
            os.rename(source, destination)
        elif sys.platform.startswith("linux"):
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            if renameat2 is None:
                raise OSError(
                    errno.ENOTSUP,
                    "renameat2(RENAME_NOREPLACE) is unavailable",
                )
            renameat2.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            renameat2.restype = ctypes.c_int
            ctypes.set_errno(0)
            result = renameat2(
                -100,  # AT_FDCWD
                os.fsencode(source),
                -100,
                os.fsencode(destination),
                1,  # RENAME_NOREPLACE
            )
            if result != 0:
                error_number = ctypes.get_errno() or errno.EIO
                raise OSError(error_number, os.strerror(error_number))
        elif sys.platform == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            renamex_np = getattr(libc, "renamex_np", None)
            if renamex_np is None:
                raise OSError(errno.ENOTSUP, "renamex_np(RENAME_EXCL) is unavailable")
            renamex_np.argtypes = (
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            renamex_np.restype = ctypes.c_int
            ctypes.set_errno(0)
            result = renamex_np(
                os.fsencode(source),
                os.fsencode(destination),
                0x00000004,  # RENAME_EXCL
            )
            if result != 0:
                error_number = ctypes.get_errno() or errno.EIO
                raise OSError(error_number, os.strerror(error_number))
        else:
            raise OSError(
                errno.ENOTSUP,
                "this platform has no supported atomic no-replace move",
            )
    except FileExistsError as error:
        raise ArtifactConflictResolutionError(
            conflict_code,
            f"{label} destination appeared concurrently and was not replaced: {destination}: {error}",
        ) from None
    except OSError as error:
        if error.errno == errno.EEXIST:
            raise ArtifactConflictResolutionError(
                conflict_code,
                f"{label} destination appeared concurrently and was not replaced: {destination}",
            ) from None
        raise ArtifactConflictResolutionError(
            failure_code,
            f"{label} could not be moved atomically without replacement: {source}: {error}",
        ) from None


def _move_to_quarantine_no_replace(source: Path, quarantine: Path) -> None:
    _move_no_replace(
        source,
        quarantine,
        conflict_code="legacy_quarantine_conflict",
        failure_code="legacy_quarantine_failed",
        label="active legacy artifact",
    )


def _restore_quarantined_legacy(preflight: _Preflight) -> None:
    quarantine = preflight.legacy_quarantine_path
    if os.path.lexists(preflight.legacy_path):
        raise ArtifactConflictResolutionError(
            "legacy_restore_conflict",
            "reviewed legacy bytes were quarantined but a new active path appeared; "
            f"the quarantined file was retained at {quarantine}",
        )
    before_bytes, before_identity = _read_safe_regular_snapshot(
        quarantine,
        quarantine.parent,
        code="legacy_restore_failed",
        label="quarantined legacy artifact",
    )
    _move_no_replace(
        quarantine,
        preflight.legacy_path,
        conflict_code="legacy_restore_conflict",
        failure_code="legacy_restore_failed",
        label="quarantined legacy artifact",
    )
    restored_bytes, restored_identity = _read_safe_regular_snapshot(
        preflight.legacy_path,
        preflight.legacy_path.parent,
        code="legacy_restore_changed",
        label="restored legacy artifact",
    )
    if (
        restored_bytes != before_bytes
        or restored_identity[:4] != before_identity[:4]
    ):
        raise ArtifactConflictResolutionError(
            "legacy_restore_changed",
            "restored legacy path is not the same reviewed file object; it was retained",
        )


def _detach_quarantined_legacy(
    preflight: _Preflight,
    expected_bytes: bytes,
    expected_identity: _FileIdentity,
) -> None:
    quarantine = preflight.legacy_quarantine_path
    detached = preflight.detached_artifact_path
    if os.path.lexists(detached):
        raise ArtifactConflictResolutionError(
            "detached_archive_conflict",
            "detached legacy archive appeared concurrently and was not replaced",
        )
    _move_no_replace(
        quarantine,
        detached,
        conflict_code="detached_archive_conflict",
        failure_code="legacy_detach_failed",
        label="quarantined legacy artifact",
    )
    detached_bytes, detached_identity = _read_safe_regular_snapshot(
        detached,
        preflight.archive_directory,
        code="legacy_quarantine_changed",
        label="detached legacy artifact",
    )
    if (
        detached_bytes != expected_bytes
        or detached_identity[:4] != expected_identity[:4]
    ):
        if not os.path.lexists(quarantine):
            _move_no_replace(
                detached,
                quarantine,
                conflict_code="legacy_quarantine_conflict",
                failure_code="legacy_restore_failed",
                label="unexpected detached legacy artifact",
            )
        raise ArtifactConflictResolutionError(
            "legacy_quarantine_changed",
            "quarantined legacy artifact changed before archival detachment and was retained",
        )


def _remove_active_legacy(preflight: _Preflight) -> None:
    """Atomically detach the exact reviewed source into retained audit custody.

    A deterministic same-directory quarantine makes a crash after rename
    retryable and ensures a file swapped in after preflight is restored rather
    than silently deleted.
    """

    source = preflight.legacy_path
    quarantine = preflight.legacy_quarantine_path
    directory = source.parent
    try:
        source_device = directory.stat().st_dev
        archive_device = preflight.archive_directory.stat().st_dev
    except OSError as error:
        raise ArtifactConflictResolutionError(
            "legacy_detach_failed",
            f"staging and archive devices could not be compared safely: {error}",
        ) from None
    if source_device != archive_device:
        raise ArtifactConflictResolutionError(
            "archive_cross_device",
            "the audit archive must share a filesystem with staging for atomic legacy detachment",
        )
    if os.path.lexists(source) and os.path.lexists(quarantine):
        raise ArtifactConflictResolutionError(
            "legacy_quarantine_conflict",
            "active legacy and its deterministic quarantine both exist",
        )
    if os.path.lexists(source):
        _verify_active_legacy(preflight)
        _move_to_quarantine_no_replace(source, quarantine)
    if not os.path.lexists(quarantine):
        return

    quarantined, quarantine_identity = _read_safe_regular_snapshot(
        quarantine,
        directory,
        code="unsafe_legacy_quarantine",
        label="quarantined legacy artifact",
    )
    if quarantined != preflight.legacy.source_bytes:
        _restore_quarantined_legacy(preflight)
        raise ArtifactConflictResolutionError(
            "legacy_source_changed",
            "active legacy changed at the final atomic cleanup boundary and was restored",
        )
    try:
        _verify_post_quarantine_authority(preflight)
    except ArtifactConflictResolutionError:
        _restore_quarantined_legacy(preflight)
        raise
    _detach_quarantined_legacy(
        preflight,
        preflight.legacy.source_bytes,
        quarantine_identity,
    )


def _report(preflight: _Preflight, status: str, *, apply: bool) -> dict[str, Any]:
    active_exists = os.path.lexists(preflight.legacy_path)
    record = _read_audit_record(preflight.audit_record_path)
    return {
        "schemaVersion": 1,
        "toolVersion": TOOL_VERSION,
        "status": status,
        "apply": apply,
        "repository": preflight.repository,
        "projectId": preflight.project_id,
        "artifactKind": preflight.kind_name,
        "decision": DECISION_LABELS[preflight.decision],
        "reasonCode": REASON_CODES[preflight.decision],
        "evidenceReference": preflight.evidence_reference,
        "evidenceSha256": preflight.evidence_sha256,
        "legacyPath": preflight.legacy.relative_path,
        "stableReferencePath": (
            f"generations/{preflight.stable_reference_generation}/"
            f"{preflight.stable_reference_relative_path}"
        ),
        "stableReferenceGeneration": preflight.stable_reference_generation,
        "legacySha256": preflight.expected_legacy_sha256,
        "stableSha256": preflight.expected_stable_sha256,
        "legacyNormalizedSha256": _normalized_sha256(preflight.legacy.payload),
        "legacyMechanicalV2NormalizedSha256": _normalized_sha256(
            preflight.converted_payload
        ),
        "stableNormalizedSha256": _normalized_sha256(preflight.stable_payload),
        "sourceTimes": {
            "legacy": _source_times(preflight.kind_name, preflight.legacy.payload),
            "stable": _source_times(preflight.kind_name, preflight.stable_payload),
        },
        "sourceVersions": preflight.source_versions,
        "archiveDirectory": str(preflight.archive_directory),
        "archivedArtifact": str(preflight.archived_artifact_path),
        "detachedArtifact": str(preflight.detached_artifact_path),
        "auditRecord": str(preflight.audit_record_path),
        "auditState": record.get("state") if record else None,
        "activeLegacyExists": active_exists,
        "legacyQuarantineExists": os.path.lexists(preflight.legacy_quarantine_path),
        "flatStableExists": os.path.lexists(preflight.stable_flat_path),
        "idempotentNoop": status == "no-op",
    }


def resolve_project_artifact_conflict(
    data_dir: Path,
    *,
    repository: str,
    kind: str,
    decision: str,
    expected_legacy_sha256: str,
    expected_stable_sha256: str,
    evidence_reference: str,
    legacy_source_pushed_at: str | None = None,
    stable_source_pushed_at: str | None = None,
    apply: bool = False,
    archive_dir: Path | None = None,
) -> dict[str, Any]:
    """Preflight and optionally resolve one reviewed flat artifact conflict."""

    try:
        canonical = _canonical_data_dir(data_dir)
    except ProjectIdentityMigrationError as error:
        raise ArtifactConflictResolutionError(error.code, str(error)) from None

    with _resolver_data_lock(canonical):
        # Dry-run must not create the archive root. Apply creates it only after
        # every data/reference/source precondition has passed.
        preflight = _preflight(
            canonical,
            repository,
            kind,
            decision,
            expected_legacy_sha256,
            expected_stable_sha256,
            evidence_reference,
            legacy_source_pushed_at,
            stable_source_pushed_at,
            archive_dir,
            create_archive=False,
        )

        existing_record = preflight.audit_record
        source_exists = os.path.lexists(preflight.legacy_path)
        if existing_record is not None and existing_record.get("state") == "resolved":
            if source_exists:
                raise ArtifactConflictResolutionError(
                    "resolved_source_reappeared",
                    "a resolved legacy source reappeared; a new explicit review is required",
                )
            _verify_flat_stable(
                preflight,
                required=preflight.decision == PROMOTE_LEGACY,
            )
            return _report(preflight, "no-op", apply=apply)

        if decision == BLOCKED:
            return _report(preflight, "blocked", apply=False)

        if not apply:
            status = (
                "would-finalize-interrupted-apply"
                if existing_record is not None
                and existing_record.get("state") == "prepared"
                and not source_exists
                else "dry-run"
            )
            return _report(preflight, status, apply=False)

        # Creating archive directories is the first side effect and only
        # occurs after the complete read-only preflight above.
        archive_root = _safe_archive_root(archive_dir, canonical, create=True)
        if not _same_path(archive_root, preflight.archive_root):
            raise ArtifactConflictResolutionError(
                "archive_path_changed",
                "artifact conflict archive root changed after preflight",
            )
        _ensure_safe_archive_entry(
            preflight.archive_directory,
            preflight.archive_root,
            create=True,
        )
        _verify_stable_reference(preflight)
        _ensure_archived_bytes(preflight)
        prepared = _ensure_prepared_record(preflight)

        if decision == PROMOTE_LEGACY:
            if os.path.lexists(preflight.stable_flat_path):
                _verify_flat_stable(preflight, required=True)
            else:
                if source_exists:
                    _verify_active_legacy(preflight)
                try:
                    _write_new_validated_flat_artifact(
                        preflight.stable_flat_path,
                        preflight.kind,
                        preflight.converted_payload,
                        preflight.repository,
                    )
                except ArtifactConflictResolutionError:
                    raise
                except (OSError, TypeError, ValueError) as error:
                    raise ArtifactConflictResolutionError(
                        "stable_target_write_failed",
                        f"promoted stable target could not be written atomically: {error}",
                    ) from None
            _verify_flat_stable(preflight, required=True)

        if os.path.lexists(preflight.legacy_path) or os.path.lexists(
            preflight.legacy_quarantine_path
        ):
            _verify_stable_reference(preflight)
            _verify_flat_stable(
                preflight,
                required=preflight.decision == PROMOTE_LEGACY,
            )
            try:
                _remove_active_legacy(preflight)
            except OSError as error:
                raise ArtifactConflictResolutionError(
                    "legacy_cleanup_failed",
                    "archive and prepared audit are durable but active legacy cleanup "
                    f"was interrupted: {preflight.legacy_path}: {error}",
                ) from None

        if os.path.lexists(preflight.legacy_path) or os.path.lexists(
            preflight.legacy_quarantine_path
        ):
            raise ArtifactConflictResolutionError(
                "legacy_cleanup_failed",
                "active legacy artifact or quarantine still exists after cleanup: "
                f"{preflight.legacy_path}",
            )
        _mark_resolved(preflight, prepared)
        return _report(preflight, "applied", apply=True)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve one explicitly reviewed Rardar project artifact conflict"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--kind", required=True, choices=sorted(STAGING_KINDS))
    parser.add_argument("--decision", required=True, choices=sorted(DECISION_LABELS))
    parser.add_argument("--expected-legacy-sha256", required=True)
    parser.add_argument("--expected-stable-sha256", required=True)
    parser.add_argument(
        "--legacy-source-pushed-at",
        default=None,
        help="exact RFC3339 source pushed_at paired with an analysis legacy artifact",
    )
    parser.add_argument(
        "--stable-source-pushed-at",
        default=None,
        help="exact RFC3339 source pushed_at bound by the stable ready generation",
    )
    parser.add_argument(
        "--evidence-reference",
        required=True,
        help="non-secret docs/iterations/*.md evidence path with optional anchor",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=None,
        help="repository-external audit archive root (defaults to user-local Rardar state)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the fully preflighted single-artifact decision (default: dry-run)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    try:
        report = resolve_project_artifact_conflict(
            arguments.data_dir,
            repository=arguments.repository,
            kind=arguments.kind,
            decision=arguments.decision,
            expected_legacy_sha256=arguments.expected_legacy_sha256,
            expected_stable_sha256=arguments.expected_stable_sha256,
            evidence_reference=arguments.evidence_reference,
            legacy_source_pushed_at=arguments.legacy_source_pushed_at,
            stable_source_pushed_at=arguments.stable_source_pushed_at,
            apply=arguments.apply,
            archive_dir=arguments.archive_dir,
        )
    except ArtifactConflictResolutionError as error:
        print(
            json.dumps(
                {"status": "failed", "errorCode": error.code, "error": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception as error:  # last-resort CLI boundary: never emit a traceback
        print(
            json.dumps(
                {
                    "status": "failed",
                    "errorCode": "unexpected_resolution_failure",
                    "error": f"unexpected resolver failure: {type(error).__name__}",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
