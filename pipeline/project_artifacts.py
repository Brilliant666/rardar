"""Identity-aware project artifact loading and candidate adoption.

Legacy evidence remains readable, but new generations use identity v1 in both
the payload and filename.  Selection is explicit: v2 wins over v1/v0 for the
same normalized repository; duplicate artifacts at the same version and
ambiguous legacy slugs fail closed.
"""

from __future__ import annotations

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
)


PROJECT_ARTIFACT_KINDS = {
    ArtifactKind.STATIC_EVIDENCE,
    ArtifactKind.PROJECT_ENRICHMENT,
}


class ProjectArtifactError(ValueError):
    """A project artifact cannot be assigned to exactly one identity."""


@dataclass(frozen=True)
class ProjectArtifactRecord:
    path: Path
    payload: dict[str, Any]
    repository_key: str
    schema_version: int


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
    if not directory.exists():
        return []
    if directory.is_symlink():
        raise ProjectArtifactError(f"project artifact directory cannot be a symlink: {directory}")
    records: list[ProjectArtifactRecord] = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink():
            raise ProjectArtifactError(f"project artifact cannot be a symlink: {path}")
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


def adopt_candidate_project_identities(generation_root: Path) -> dict[str, int]:
    """Convert v1 project artifacts inside one private candidate to v2.

    The full conversion is preflighted before any write. Existing equivalent
    targets are treated as completed retry work; conflicting targets abort.
    Retained generations and flat staging are never accepted by this helper.
    """

    root = generation_root.expanduser().resolve()
    parts = [part.casefold() for part in root.parts]
    if len(parts) < 3 or parts[-2] != ".candidates" or parts[-3] != "generations":
        raise ProjectArtifactError(
            "identity adoption is restricted to data/generations/.candidates/<id>"
        )

    plans: list[tuple[ProjectArtifactRecord, Path, dict[str, Any], bool]] = []
    all_repositories: dict[str, str] = {}
    legacy_owners: dict[str, set[str]] = {}
    for directory_name, kind in (
        ("analysis", ArtifactKind.STATIC_EVIDENCE),
        ("enrichment", ArtifactKind.PROJECT_ENRICHMENT),
    ):
        directory = root / directory_name
        for record in _records(directory, kind):
            repository = str(record.payload["repository"])
            identity = identity_for_repository(repository)
            all_repositories.setdefault(identity.canonical_repository, repository)
            if record.schema_version in {0, 1}:
                legacy_owners.setdefault(
                    legacy_slug_for_repository(repository), set()
                ).add(identity.canonical_repository)
            if record.schema_version == 0:
                continue
            if record.schema_version == 2:
                continue
            if record.schema_version != 1:
                raise ProjectArtifactError(
                    f"unsupported project artifact Schema version {record.schema_version}: {record.path}"
                )
            converted = {
                **record.payload,
                "schemaVersion": 2,
                "projectIdVersion": identity.project_id_version,
                "projectId": identity.project_id,
            }
            target = directory / f"{identity.project_id}.json"
            target_exists = target.exists()
            if target_exists:
                if target.is_symlink():
                    raise ProjectArtifactError(f"identity target cannot be a symlink: {target}")
                existing = load_validated_json(target, kind, expected_repository=repository)
                # Explicit Schema precedence is the selection rule inside a
                # candidate: a validated v2 artifact for the same repository
                # and recomputed ID is authoritative even when its content is
                # newer than the mechanical v1 conversion. Schema validation
                # above rejects ownership, filename, or forged-ID conflicts.
                if existing.get("schemaVersion") != 2:
                    raise ProjectArtifactError(
                        f"stable identity target is not project artifact v2: {target}"
                    )
            plans.append((record, target, converted, target_exists))

    for slug, repositories in legacy_owners.items():
        if len(repositories) > 1:
            raise ProjectArtifactError(
                "unresolved legacy slug collision for "
                f"{slug!r}: {', '.join(sorted(repositories))}"
            )

    try:
        ensure_unique_project_identities(all_repositories.values())
    except ProjectIdentityError as error:
        raise ProjectArtifactError(f"{error.code}: {error}") from None

    written = 0
    removed = 0
    for record, target, converted, target_exists in plans:
        if not target_exists:
            kind = (
                ArtifactKind.STATIC_EVIDENCE
                if target.parent.name.casefold() == "analysis"
                else ArtifactKind.PROJECT_ENRICHMENT
            )
            atomic_write_validated_json(
                target,
                kind,
                converted,
                expected_repository=str(converted["repository"]),
            )
            written += 1
        record.path.unlink()
        removed += 1
    return {"converted": written, "removedLegacy": removed}


__all__ = [
    "ProjectArtifactError",
    "adopt_candidate_project_identities",
    "load_project_artifacts",
]
