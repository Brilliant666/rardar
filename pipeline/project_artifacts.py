"""Identity-aware project artifact loading and candidate adoption.

Legacy evidence remains readable, but new generations use identity v1 in both
the payload and filename. Read selection is version-aware; candidate cleanup
is stricter and removes a legacy v1 artifact only when its mechanical v2
conversion is exactly equal to the existing stable artifact.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.project_identity import (
    ProjectIdentityError,
    ensure_unique_project_identities,
    identity_for_repository,
    legacy_slug_for_repository,
)
from pipeline.schema_validation import (
    ArtifactKind,
    atomic_write_validated_json,
    load_validated_json,
    require_valid,
)


PROJECT_ARTIFACT_KINDS = {
    ArtifactKind.STATIC_EVIDENCE,
    ArtifactKind.PROJECT_ENRICHMENT,
}


class ProjectArtifactError(ValueError):
    """A project artifact cannot be assigned to exactly one identity."""

    def __init__(self, code: str, message: str | None = None) -> None:
        if message is None:
            message = code
            code = "invalid_project_artifact"
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ProjectArtifactRecord:
    path: Path
    payload: dict[str, Any]
    repository_key: str
    schema_version: int
    kind: ArtifactKind
    source_bytes: bytes


@dataclass(frozen=True)
class _AdoptionPlan:
    source: ProjectArtifactRecord
    target: Path
    expected_payload: dict[str, Any]
    existing_target: ProjectArtifactRecord | None


def _is_filesystem_link(path: Path) -> bool:
    """Return true for symbolic links and Windows reparse-point junctions."""

    try:
        metadata = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _repository_key(repository: object) -> str:
    if not isinstance(repository, str) or not repository:
        raise ProjectArtifactError("project artifact repository must be non-empty")
    return repository.casefold()


def _artifact_timestamp_field(kind: ArtifactKind) -> str:
    if kind is ArtifactKind.STATIC_EVIDENCE:
        return "analyzed_at"
    if kind is ArtifactKind.PROJECT_ENRICHMENT:
        return "analyzedAt"
    raise ValueError(f"unsupported project artifact kind: {kind.value}")


def _records(directory: Path, kind: ArtifactKind) -> list[ProjectArtifactRecord]:
    if kind not in PROJECT_ARTIFACT_KINDS:
        raise ValueError(f"unsupported project artifact kind: {kind.value}")
    if not os.path.lexists(directory):
        return []
    if _is_filesystem_link(directory):
        raise ProjectArtifactError(
            "unsafe_project_artifact_directory",
            f"project artifact directory cannot be a filesystem link: {directory}",
        )
    if not directory.is_dir():
        raise ProjectArtifactError(
            "invalid_project_artifact_directory",
            f"project artifact directory must be a directory: {directory}",
        )
    records: list[ProjectArtifactRecord] = []
    for path in sorted(directory.glob("*.json")):
        if _is_filesystem_link(path):
            raise ProjectArtifactError(
                "unsafe_project_artifact_entry",
                f"project artifact cannot be a filesystem link: {path}",
            )
        payload = load_validated_json(path, kind)
        version = payload.get("schemaVersion")
        if not isinstance(version, int) or isinstance(version, bool):
            raise ProjectArtifactError(f"artifact has no integer schemaVersion: {path}")
        records.append(
            ProjectArtifactRecord(
                path=path,
                payload=payload,
                repository_key=_repository_key(payload.get("repository")),
                schema_version=version,
                kind=kind,
                source_bytes=path.read_bytes(),
            )
        )
    return records


def load_project_artifacts(
    directory: Path,
    kind: ArtifactKind,
    *,
    expected_repositories: list[object] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load one authoritative artifact per normalized repository.

    A higher explicit Schema version supersedes a lower version. Two artifacts
    at the same version are always ambiguous and are rejected rather than
    selected by directory order or timestamp.
    """

    records = _records(directory, kind)
    selected: dict[str, ProjectArtifactRecord] = {}
    for record in records:
        existing = selected.get(record.repository_key)
        if existing is None or record.schema_version > existing.schema_version:
            selected[record.repository_key] = record
            continue
        if record.schema_version == existing.schema_version:
            timestamp_field = _artifact_timestamp_field(kind)
            raise ProjectArtifactError(
                "duplicate project artifacts have the same explicit Schema version "
                f"for {record.payload.get('repository')!r}: {existing.path.name} and "
                f"{record.path.name} ({timestamp_field} cannot resolve ownership)"
            )

    if expected_repositories is not None:
        try:
            identities = ensure_unique_project_identities(expected_repositories)
        except ProjectIdentityError as error:
            raise ProjectArtifactError(f"{error.code}: {error}") from None
        by_legacy_slug: dict[str, list[str]] = {}
        for canonical in identities:
            slug = legacy_slug_for_repository(canonical)
            by_legacy_slug.setdefault(slug, []).append(canonical)
        for record in selected.values():
            if record.schema_version >= 2:
                continue
            slug = legacy_slug_for_repository(record.payload["repository"])
            candidates = by_legacy_slug.get(slug, [])
            if len(candidates) > 1:
                raise ProjectArtifactError(
                    "unresolved legacy slug collision for "
                    f"{slug!r}: {', '.join(sorted(candidates))}"
                )

    return {
        record.repository_key: record.payload
        for record in selected.values()
    }


_CANDIDATE_ARTIFACT_DIRECTORIES = frozenset({"analysis", "enrichment"})


def _has_candidate_scope(path: Path) -> bool:
    parts = [part.casefold() for part in path.parts]
    return (
        len(parts) >= 3
        and parts[-2] == ".candidates"
        and parts[-3] == "generations"
    )


def _assert_no_candidate_link_ancestors(path: Path) -> None:
    current = path
    while True:
        if os.path.lexists(current) and _is_filesystem_link(current):
            raise ProjectArtifactError(
                "unsafe_candidate_root",
                "candidate generation path cannot traverse a filesystem link: "
                f"{current}",
            )
        if current == current.parent:
            break
        current = current.parent


def _canonical_candidate_root(generation_root: Path) -> Path:
    lexical = Path(os.path.abspath(os.fspath(generation_root.expanduser())))
    if not _has_candidate_scope(lexical):
        raise ProjectArtifactError(
            "identity_adoption_scope_violation",
            "identity adoption is restricted to data/generations/.candidates/<id>",
        )
    _assert_no_candidate_link_ancestors(lexical)
    if not os.path.lexists(lexical) or not lexical.is_dir():
        raise ProjectArtifactError(
            "invalid_candidate_root",
            f"candidate generation root is unavailable: {lexical}",
        )
    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectArtifactError(
            "unsafe_candidate_root",
            f"candidate generation root cannot be resolved safely: {lexical}: {error}",
        ) from None
    if not _has_candidate_scope(resolved):
        raise ProjectArtifactError(
            "identity_adoption_scope_violation",
            "identity adoption is restricted to data/generations/.candidates/<id>",
        )
    return resolved


def _assert_candidate_root_unchanged(root: Path) -> None:
    _assert_no_candidate_link_ancestors(root)
    if (
        not os.path.lexists(root)
        or not root.is_dir()
    ):
        raise ProjectArtifactError(
            "unsafe_candidate_root",
            f"candidate generation root changed or became unsafe: {root}",
        )
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectArtifactError(
            "unsafe_candidate_root",
            f"candidate generation root cannot be revalidated: {root}: {error}",
        ) from None
    if not _same_path(root, resolved):
        raise ProjectArtifactError(
            "unsafe_candidate_root",
            f"candidate generation root resolved to an unexpected path: {root}",
        )


def _candidate_artifact_directory(root: Path, name: str) -> Path:
    _assert_candidate_root_unchanged(root)
    normalized_name = name.casefold()
    if normalized_name not in _CANDIDATE_ARTIFACT_DIRECTORIES:
        raise ProjectArtifactError(
            "candidate_path_escape",
            f"candidate project artifact directory is not allowed: {name!r}",
        )
    directory = root / normalized_name
    if not os.path.lexists(directory):
        return directory
    if _is_filesystem_link(directory) or not directory.is_dir():
        raise ProjectArtifactError(
            "unsafe_project_artifact_directory",
            f"candidate project artifact directory is unsafe: {directory}",
        )
    try:
        resolved = directory.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectArtifactError(
            "unsafe_project_artifact_directory",
            f"candidate project artifact directory cannot be resolved: {directory}: {error}",
        ) from None
    if not _same_path(resolved, directory) or not _same_path(resolved.parent, root):
        raise ProjectArtifactError(
            "candidate_path_escape",
            f"candidate project artifact directory escapes its root: {directory}",
        )
    return resolved


def _assert_safe_candidate_entry(
    path: Path,
    root: Path,
    *,
    must_exist: bool,
) -> None:
    directory = _candidate_artifact_directory(root, path.parent.name)
    if not _same_path(path.parent, directory):
        raise ProjectArtifactError(
            "candidate_path_escape",
            f"candidate project artifact escapes its direct directory: {path}",
        )
    if not os.path.lexists(path):
        if must_exist:
            raise ProjectArtifactError(
                "project_artifact_changed_during_adoption",
                f"candidate project artifact became unavailable: {path}",
            )
        return
    if _is_filesystem_link(path) or not path.is_file():
        raise ProjectArtifactError(
            "unsafe_project_artifact_entry",
            f"candidate project artifact entry is unsafe: {path}",
        )
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectArtifactError(
            "unsafe_project_artifact_entry",
            f"candidate project artifact entry cannot be resolved: {path}: {error}",
        ) from None
    if not _same_path(resolved, path) or not _same_path(resolved.parent, directory):
        raise ProjectArtifactError(
            "candidate_path_escape",
            f"candidate project artifact entry escapes its directory: {path}",
        )


def _verify_record_unchanged(
    record: ProjectArtifactRecord,
    candidate_root: Path,
) -> None:
    _assert_safe_candidate_entry(record.path, candidate_root, must_exist=True)
    if not record.path.is_file():
        raise ProjectArtifactError(
            "project_artifact_changed_during_adoption",
            f"project artifact became unavailable or unsafe: {record.path}",
        )
    try:
        current_bytes = record.path.read_bytes()
        current_payload = load_validated_json(record.path, record.kind)
    except (OSError, TypeError, ValueError) as error:
        raise ProjectArtifactError(
            "project_artifact_changed_during_adoption",
            f"project artifact changed after preflight: {record.path}: {error}",
        ) from None
    if current_bytes != record.source_bytes or current_payload != record.payload:
        raise ProjectArtifactError(
            "project_artifact_changed_during_adoption",
            f"project artifact changed after preflight: {record.path}",
        )


def _verify_adoption_target(plan: _AdoptionPlan, candidate_root: Path) -> None:
    _assert_safe_candidate_entry(plan.target, candidate_root, must_exist=True)
    if not plan.target.is_file():
        raise ProjectArtifactError(
            "project_artifact_target_changed",
            f"stable identity target became unavailable or unsafe: {plan.target}",
        )
    try:
        payload = load_validated_json(
            plan.target,
            plan.source.kind,
            expected_repository=str(plan.expected_payload["repository"]),
        )
    except (OSError, TypeError, ValueError) as error:
        raise ProjectArtifactError(
            "project_artifact_target_changed",
            f"stable identity target failed verification: {plan.target}: {error}",
        ) from None
    if payload != plan.expected_payload:
        raise ProjectArtifactError(
            "project_artifact_target_changed",
            f"stable identity target differs from the preflight payload: {plan.target}",
        )


def _remove_legacy_source(plan: _AdoptionPlan, candidate_root: Path) -> None:
    _verify_record_unchanged(plan.source, candidate_root)
    plan.source.path.unlink()


def adopt_candidate_project_identities(generation_root: Path) -> dict[str, int]:
    """Convert v1 project artifacts inside one private candidate to v2.

    The full conversion is preflighted before any write. Existing equivalent
    targets are treated as completed retry work; conflicting targets abort.
    All missing targets are durable and verified before any source is removed.
    Retained generations and flat staging are never accepted by this helper.
    """

    root = _canonical_candidate_root(generation_root)

    records: list[ProjectArtifactRecord] = []
    for directory_name, kind in (
        ("analysis", ArtifactKind.STATIC_EVIDENCE),
        ("enrichment", ArtifactKind.PROJECT_ENRICHMENT),
    ):
        directory = _candidate_artifact_directory(root, directory_name)
        records.extend(_records(directory, kind))

    records_by_path = {record.path: record for record in records}
    plans: list[_AdoptionPlan] = []
    all_repositories: dict[str, str] = {}
    legacy_owners: dict[str, set[str]] = {}
    for record in records:
        repository = str(record.payload["repository"])
        identity = identity_for_repository(repository)
        all_repositories.setdefault(identity.canonical_repository, repository)
        if record.schema_version in {0, 1}:
            legacy_owners.setdefault(
                legacy_slug_for_repository(repository), set()
            ).add(identity.canonical_repository)
        if record.schema_version in {0, 2}:
            continue
        if record.schema_version != 1:
            raise ProjectArtifactError(
                "unsupported_project_artifact_version",
                f"unsupported project artifact Schema version {record.schema_version}: {record.path}",
            )
        converted = {
            **record.payload,
            "schemaVersion": 2,
            "projectIdVersion": identity.project_id_version,
            "projectId": identity.project_id,
        }
        target = record.path.parent / f"{identity.project_id}.json"
        try:
            converted = require_valid(
                record.kind,
                converted,
                source_path=target,
                expected_repository=repository,
            )
        except (TypeError, ValueError) as error:
            raise ProjectArtifactError(
                "invalid_mechanical_project_artifact_conversion",
                f"mechanical v2 conversion failed validation for {record.path}: {error}",
            ) from None
        existing_target = records_by_path.get(target)
        if existing_target is not None:
            if (
                existing_target.schema_version != 2
                or existing_target.payload != converted
            ):
                raise ProjectArtifactError(
                    "conflicting_project_artifact_versions",
                    "legacy v1 and stable v2 project artifacts are not exactly "
                    f"equivalent for {repository!r}: {record.path.name} and {target.name}",
                )
        elif os.path.lexists(target):
            raise ProjectArtifactError(
                "unsafe_project_artifact_target",
                f"stable identity target exists outside the validated preflight set: {target}",
            )
        plans.append(
            _AdoptionPlan(
                source=record,
                target=target,
                expected_payload=converted,
                existing_target=existing_target,
            )
        )

    for slug, repositories in legacy_owners.items():
        if len(repositories) > 1:
            raise ProjectArtifactError(
                "unresolved_legacy_collision",
                "unresolved legacy slug collision for "
                f"{slug!r}: {', '.join(sorted(repositories))}",
            )

    try:
        ensure_unique_project_identities(all_repositories.values())
    except ProjectIdentityError as error:
        raise ProjectArtifactError(error.code, str(error)) from None

    # Recheck every assumption before the first target write. No legacy source
    # is removed until every missing target is durable and verified.
    for plan in plans:
        _verify_record_unchanged(plan.source, root)
        if plan.existing_target is not None:
            _verify_record_unchanged(plan.existing_target, root)
            _verify_adoption_target(plan, root)
        elif os.path.lexists(plan.target):
            raise ProjectArtifactError(
                "project_artifact_target_changed",
                f"stable identity target appeared after preflight: {plan.target}",
            )

    written = 0
    for plan in plans:
        if plan.existing_target is not None:
            continue
        _assert_safe_candidate_entry(plan.target, root, must_exist=False)
        try:
            atomic_write_validated_json(
                plan.target,
                plan.source.kind,
                plan.expected_payload,
                expected_repository=str(plan.expected_payload["repository"]),
            )
        except (OSError, TypeError, ValueError) as error:
            raise ProjectArtifactError(
                "project_artifact_target_write_failed",
                f"stable identity target could not be written atomically: {plan.target}: {error}",
            ) from None
        written += 1

    for plan in plans:
        _verify_adoption_target(plan, root)
    for plan in plans:
        _verify_record_unchanged(plan.source, root)

    removed = 0
    for plan in plans:
        try:
            _remove_legacy_source(plan, root)
        except OSError as error:
            raise ProjectArtifactError(
                "project_artifact_source_cleanup_failed",
                "stable identity targets are durable but legacy cleanup was "
                f"interrupted at {plan.source.path}: {error}",
            ) from None
        removed += 1
    return {"converted": written, "removedLegacy": removed}


__all__ = [
    "ProjectArtifactError",
    "adopt_candidate_project_identities",
    "load_project_artifacts",
]
