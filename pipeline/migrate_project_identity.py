"""Safely migrate flat project artifacts between legacy slugs and stable IDs.

This command deliberately operates only on the flat ``analysis`` and
``enrichment`` staging directories.  Retained generations and the current
pointer are immutable inputs to this migration and are never inspected or
modified.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from pipeline.data_lock import data_dir_lock
from pipeline.project_identity import (
    PROJECT_ID_VERSION,
    ProjectIdentityError,
    canonicalize_repository,
    legacy_slug_for_repository,
    project_id_for_repository,
    validate_project_identity,
)
from pipeline.schema_validation import (
    ArtifactKind,
    ArtifactValidationError,
    atomic_write_validated_json,
    require_valid,
    strict_json_loads,
)


STAGING_KINDS = {
    "analysis": ArtifactKind.STATIC_EVIDENCE,
    "enrichment": ArtifactKind.PROJECT_ENRICHMENT,
}

TO_STABLE_V2 = "to-stable-v2"
TO_LEGACY_V1 = "to-legacy-v1"


class ProjectIdentityMigrationError(RuntimeError):
    """A fail-closed migration error with a stable diagnostic code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Artifact:
    path: Path
    relative_path: str
    kind: ArtifactKind
    repository: str
    canonical_repository: str
    project_id: str
    version: int
    payload: dict[str, Any]
    source_bytes: bytes


@dataclass(frozen=True)
class _MigrationAction:
    source: _Artifact
    target: Path
    target_relative_path: str
    expected_payload: dict[str, Any]
    target_already_equivalent: bool
    direction: str


@dataclass(frozen=True)
class _Preflight:
    artifacts: tuple[_Artifact, ...]
    actions: tuple[_MigrationAction, ...]
    direction: str


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


def _canonical_data_dir(data_dir: Path) -> Path:
    lexical = Path(os.path.abspath(os.fspath(data_dir.expanduser())))
    if not lexical.exists() or not lexical.is_dir():
        raise ProjectIdentityMigrationError(
            "data_dir_unavailable",
            f"flat staging data directory is unavailable: {lexical}",
        )
    current = lexical
    while True:
        if _is_filesystem_link(current):
            raise ProjectIdentityMigrationError(
                "unsafe_data_dir",
                f"flat staging data path cannot traverse a filesystem link: {current}",
            )
        if current == current.parent:
            break
        current = current.parent
    try:
        canonical = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectIdentityMigrationError(
            "unsafe_data_dir",
            f"flat staging data directory cannot be resolved safely: {error}",
        ) from None
    protected_names = {"generations", ".candidates"}
    if (
        any(
            ancestor.name.casefold() in protected_names
            for ancestor in (canonical, *canonical.parents)
        )
        or (canonical / "manifest.json").exists()
    ):
        raise ProjectIdentityMigrationError(
            "protected_generation",
            f"retained or candidate generations cannot be migrated in place: {canonical}",
        )
    return canonical


def _staging_directories(data_dir: Path) -> list[tuple[Path, ArtifactKind]]:
    directories: list[tuple[Path, ArtifactKind]] = []
    for name, kind in STAGING_KINDS.items():
        path = data_dir / name
        if not os.path.lexists(path):
            continue
        if _is_filesystem_link(path):
            raise ProjectIdentityMigrationError(
                "unsafe_staging_directory",
                f"flat staging directory cannot be a filesystem link: {path}",
            )
        if not path.is_dir():
            raise ProjectIdentityMigrationError(
                "invalid_staging_directory",
                f"flat staging location must be a directory: {path}",
            )
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ProjectIdentityMigrationError(
                "unsafe_staging_directory",
                f"flat staging directory cannot be resolved safely: {path}: {error}",
            ) from None
        if not _same_path(resolved.parent, data_dir) or resolved.name.casefold() != name:
            raise ProjectIdentityMigrationError(
                "staging_path_escape",
                f"flat staging directory escapes the data root: {path}",
            )
        directories.append((resolved, kind))
    return directories


def _assert_safe_regular_file(path: Path, directory: Path) -> None:
    if _is_filesystem_link(path):
        raise ProjectIdentityMigrationError(
            "unsafe_staging_entry",
            f"flat staging entries cannot be filesystem links: {path}",
        )
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectIdentityMigrationError(
            "unsafe_staging_entry",
            f"flat staging entry cannot be inspected safely: {path}: {error}",
        ) from None
    if not stat.S_ISREG(metadata.st_mode):
        raise ProjectIdentityMigrationError(
            "invalid_staging_entry",
            f"flat staging JSON entry must be a regular file: {path}",
        )
    if not _same_path(resolved.parent, directory):
        raise ProjectIdentityMigrationError(
            "staging_path_escape",
            f"flat staging entry escapes its directory: {path}",
        )


def _safe_json_target_path(
    directory: Path,
    stem: str,
    *,
    error_code: str,
    label: str,
) -> Path:
    if (
        not stem
        or stem in {".", ".."}
        or "/" in stem
        or "\\" in stem
        or any(ord(character) < 32 for character in stem)
        or Path(stem).name != stem
    ):
        raise ProjectIdentityMigrationError(
            error_code,
            f"{label} is not a safe filename segment: {stem!r}",
        )
    target = directory / f"{stem}.json"
    try:
        resolved_parent = target.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ProjectIdentityMigrationError(
            "staging_path_escape",
            f"project artifact target cannot be resolved safely: {target}: {error}",
        ) from None
    if not _same_path(resolved_parent, directory):
        raise ProjectIdentityMigrationError(
            "staging_path_escape",
            f"project artifact target escapes its staging directory: {target}",
        )
    return target


def _safe_stable_target_path(directory: Path, project_id: str) -> Path:
    return _safe_json_target_path(
        directory,
        project_id,
        error_code="unsafe_project_id_path",
        label="stable project ID",
    )


def _safe_legacy_target_path(directory: Path, repository: str) -> Path:
    return _safe_json_target_path(
        directory,
        legacy_slug_for_repository(repository),
        error_code="unsafe_legacy_slug_path",
        label="legacy project slug",
    )


def _load_artifact(
    path: Path,
    directory: Path,
    data_dir: Path,
    kind: ArtifactKind,
) -> _Artifact:
    _assert_safe_regular_file(path, directory)
    try:
        source_bytes = path.read_bytes()
        payload = strict_json_loads(source_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ProjectIdentityMigrationError(
            "invalid_source_artifact",
            f"flat staging artifact is not strict UTF-8 JSON: {path}: {error}",
        ) from None
    if not isinstance(payload, dict):
        raise ProjectIdentityMigrationError(
            "invalid_source_artifact",
            f"flat staging artifact must be a JSON object: {path}",
        )
    try:
        validated = require_valid(kind, payload, source_path=path)
    except (ArtifactValidationError, TypeError, ValueError) as error:
        raise ProjectIdentityMigrationError(
            "invalid_source_artifact",
            f"flat staging artifact failed Schema validation: {path}: {error}",
        ) from None

    repository = validated.get("repository")
    version = validated.get("schemaVersion")
    if not isinstance(repository, str) or not isinstance(version, int) or isinstance(version, bool):
        raise ProjectIdentityMigrationError(
            "invalid_source_identity",
            f"flat staging artifact lacks a versioned repository identity: {path}",
        )
    try:
        canonical_repository = canonicalize_repository(repository)
        project_id = project_id_for_repository(repository)
    except ProjectIdentityError as error:
        raise ProjectIdentityMigrationError(
            error.code,
            f"flat staging repository identity is invalid: {path}: {error}",
        ) from None

    if version in {0, 1}:
        expected_legacy_name = f"{legacy_slug_for_repository(repository)}.json"
        if path.name != expected_legacy_name:
            raise ProjectIdentityMigrationError(
                "legacy_filename_mismatch",
                f"legacy artifact {path.name!r} does not belong to {repository!r}",
            )
    elif version == 2:
        try:
            validate_project_identity(
                repository,
                validated.get("projectId"),
                validated.get("projectIdVersion"),
            )
        except ProjectIdentityError as error:
            raise ProjectIdentityMigrationError(
                error.code,
                f"stable artifact identity is invalid: {path}: {error}",
            ) from None
        if path.name != f"{project_id}.json":
            raise ProjectIdentityMigrationError(
                "stable_filename_mismatch",
                f"stable artifact filename does not match its project ID: {path}",
            )
    else:
        raise ProjectIdentityMigrationError(
            "unsupported_artifact_version",
            f"flat staging artifact uses an unsupported version: {path}: {version!r}",
        )

    return _Artifact(
        path=path,
        relative_path=path.relative_to(data_dir).as_posix(),
        kind=kind,
        repository=repository,
        canonical_repository=canonical_repository,
        project_id=project_id,
        version=version,
        payload=validated,
        source_bytes=source_bytes,
    )


def _expected_payload(
    artifact: _Artifact,
    target: Path,
    direction: str,
) -> dict[str, Any]:
    if direction == TO_STABLE_V2:
        expected_payload = {
            **artifact.payload,
            "schemaVersion": 2,
            "projectIdVersion": PROJECT_ID_VERSION,
            "projectId": artifact.project_id,
        }
    elif direction == TO_LEGACY_V1:
        expected_payload = dict(artifact.payload)
        expected_payload["schemaVersion"] = 1
        expected_payload.pop("projectIdVersion", None)
        expected_payload.pop("projectId", None)
    else:  # pragma: no cover - callers only use the two public directions.
        raise ValueError(f"unsupported project identity migration direction: {direction}")

    try:
        return require_valid(
            artifact.kind,
            expected_payload,
            source_path=target,
            expected_repository=artifact.repository,
        )
    except (ArtifactValidationError, TypeError, ValueError) as error:
        raise ProjectIdentityMigrationError(
            "invalid_migrated_artifact",
            f"mechanically migrated artifact fails its target contract: {target}: {error}",
        ) from None


def _preflight(data_dir: Path, direction: str = TO_STABLE_V2) -> _Preflight:
    if direction not in {TO_STABLE_V2, TO_LEGACY_V1}:
        raise ValueError(f"unsupported project identity migration direction: {direction}")

    artifacts: list[_Artifact] = []
    for directory, kind in _staging_directories(data_dir):
        for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            if _is_filesystem_link(path):
                raise ProjectIdentityMigrationError(
                    "unsafe_staging_entry",
                    f"flat staging entries cannot be filesystem links: {path}",
                )
            if path.suffix.casefold() != ".json":
                continue
            artifacts.append(_load_artifact(path, directory, data_dir, kind))

    repository_by_project_id: dict[str, str] = {}
    for artifact in artifacts:
        existing_repository = repository_by_project_id.get(artifact.project_id)
        if (
            existing_repository is not None
            and existing_repository != artifact.canonical_repository
        ):
            raise ProjectIdentityMigrationError(
                "project_id_collision",
                f"projectId {artifact.project_id!r} belongs to both "
                f"{existing_repository!r} and {artifact.canonical_repository!r}",
            )
        repository_by_project_id[artifact.project_id] = artifact.canonical_repository

    legacy_owners: dict[str, dict[str, list[str]]] = {}
    collision_versions = {0, 1} if direction == TO_STABLE_V2 else {0, 1, 2}
    for artifact in artifacts:
        if artifact.version not in collision_versions:
            continue
        legacy_slug = legacy_slug_for_repository(artifact.repository)
        owners = legacy_owners.setdefault(legacy_slug, {})
        owners.setdefault(artifact.canonical_repository, []).append(artifact.relative_path)
    for legacy_name, owners in legacy_owners.items():
        if len(owners) > 1:
            repositories = ", ".join(sorted(owners))
            raise ProjectIdentityMigrationError(
                "unresolved_legacy_collision",
                f"legacy filename {legacy_name!r} maps to multiple repositories: {repositories}",
            )

    artifacts_by_path = {artifact.path: artifact for artifact in artifacts}
    actions: list[_MigrationAction] = []
    claimed_targets: dict[Path, _Artifact] = {}
    source_version = 1 if direction == TO_STABLE_V2 else 2
    for artifact in artifacts:
        if artifact.version != source_version:
            continue
        target = (
            _safe_stable_target_path(artifact.path.parent, artifact.project_id)
            if direction == TO_STABLE_V2
            else _safe_legacy_target_path(artifact.path.parent, artifact.repository)
        )
        previous = claimed_targets.get(target)
        if previous is not None and previous.path != artifact.path:
            raise ProjectIdentityMigrationError(
                "duplicate_project_artifact",
                f"multiple project artifacts claim migration target {target}",
            )
        claimed_targets[target] = artifact
        if _same_path(target, artifact.path):
            raise ProjectIdentityMigrationError(
                "invalid_migration_target",
                f"source artifact already occupies its migration target path: {target}",
            )

        validated_expected = _expected_payload(artifact, target, direction)

        target_artifact = artifacts_by_path.get(target)
        target_already_equivalent = target_artifact is not None
        if target_artifact is not None and target_artifact.payload != validated_expected:
            raise ProjectIdentityMigrationError(
                "target_conflict",
                f"migration target already exists with different content or ownership: {target}",
            )
        if target_artifact is None and os.path.lexists(target):
            # Every safe JSON target in a staging directory must have been
            # included in the scan.  Reaching this branch indicates a race or
            # a non-regular filesystem object.
            raise ProjectIdentityMigrationError(
                "unsafe_target",
                f"migration target exists but was not a safe preflight artifact: {target}",
            )
        actions.append(
            _MigrationAction(
                source=artifact,
                target=target,
                target_relative_path=target.relative_to(data_dir).as_posix(),
                expected_payload=validated_expected,
                target_already_equivalent=target_already_equivalent,
                direction=direction,
            )
        )
    return _Preflight(tuple(artifacts), tuple(actions), direction)


def _verify_source_unchanged(action: _MigrationAction) -> None:
    source_label = "legacy" if action.direction == TO_STABLE_V2 else "stable"
    _assert_safe_regular_file(action.source.path, action.source.path.parent)
    try:
        current = action.source.path.read_bytes()
    except OSError as error:
        raise ProjectIdentityMigrationError(
            "source_changed_during_apply",
            f"{source_label} source became unavailable during apply: "
            f"{action.source.path}: {error}",
        ) from None
    if current != action.source.source_bytes:
        raise ProjectIdentityMigrationError(
            "source_changed_during_apply",
            f"{source_label} source changed after preflight: {action.source.path}",
        )


def _verify_target(action: _MigrationAction) -> None:
    target_label = "stable" if action.direction == TO_STABLE_V2 else "legacy"
    _assert_safe_regular_file(action.target, action.target.parent)
    try:
        payload = strict_json_loads(action.target.read_text(encoding="utf-8"))
        validated = require_valid(
            action.source.kind,
            payload,
            source_path=action.target,
            expected_repository=action.source.repository,
        )
    except (OSError, ArtifactValidationError, TypeError, ValueError) as error:
        raise ProjectIdentityMigrationError(
            "target_changed_during_apply",
            f"{target_label} target failed post-write verification: "
            f"{action.target}: {error}",
        ) from None
    if validated != action.expected_payload:
        raise ProjectIdentityMigrationError(
            "target_changed_during_apply",
            f"{target_label} target differs from the preflight payload: {action.target}",
        )


def _remove_source(action: _MigrationAction) -> None:
    action.source.path.unlink()


def _verify_apply_preconditions(preflight: _Preflight) -> None:
    """Recheck every preflight assumption before the first target write."""

    for action in preflight.actions:
        _verify_source_unchanged(action)
        if action.target_already_equivalent:
            _verify_target(action)
        elif os.path.lexists(action.target):
            target_label = "stable" if action.direction == TO_STABLE_V2 else "legacy"
            raise ProjectIdentityMigrationError(
                "target_changed_during_apply",
                f"{target_label} target appeared after preflight: {action.target}",
            )


def _report(preflight: _Preflight, data_dir: Path, *, apply: bool) -> dict[str, Any]:
    action_by_source = {action.source.path: action for action in preflight.actions}
    items: list[dict[str, Any]] = []
    for artifact in preflight.artifacts:
        action = action_by_source.get(artifact.path)
        if artifact.version == 0:
            status = "legacy_v0_unmigrated"
            target_path = None
        elif preflight.direction == TO_STABLE_V2:
            if artifact.version == 2:
                status = "already_current"
                target_path = artifact.relative_path
            elif action is not None and action.target_already_equivalent:
                status = (
                    "equivalent_target_cleaned"
                    if apply
                    else "would_cleanup_equivalent_target"
                )
                target_path = action.target_relative_path
            elif action is not None:
                status = "migrated" if apply else "would_migrate"
                target_path = action.target_relative_path
            else:  # pragma: no cover - versions are exhaustively handled in preflight.
                continue
        elif artifact.version == 1:
            status = "already_legacy_v1"
            target_path = artifact.relative_path
        elif action is not None and action.target_already_equivalent:
            status = (
                "equivalent_target_cleaned"
                if apply
                else "would_cleanup_equivalent_target"
            )
            target_path = action.target_relative_path
        elif action is not None:
            status = "downgraded" if apply else "would_downgrade"
            target_path = action.target_relative_path
        else:  # pragma: no cover - versions are exhaustively handled in preflight.
            continue
        items.append(
            {
                "path": artifact.relative_path,
                "kind": artifact.kind.value,
                "repository": artifact.repository,
                "projectId": artifact.project_id if artifact.version != 0 else None,
                "targetPath": target_path,
                "status": status,
            }
        )

    return {
        "status": "applied" if apply else "dry-run",
        "direction": preflight.direction,
        "dataDir": str(data_dir),
        "migrationCount": sum(
            not action.target_already_equivalent for action in preflight.actions
        ),
        "equivalentTargetCount": sum(
            action.target_already_equivalent for action in preflight.actions
        ),
        "legacyUnmigratedCount": sum(
            artifact.version == 0 for artifact in preflight.artifacts
        ),
        "alreadyCurrentCount": sum(
            artifact.version == (2 if preflight.direction == TO_STABLE_V2 else 1)
            for artifact in preflight.artifacts
        ),
        "items": items,
    }


def migrate_project_identity(
    data_dir: Path,
    *,
    apply: bool = False,
    to_legacy_v1: bool = False,
) -> dict[str, Any]:
    """Preflight and optionally apply one retry-safe flat staging migration."""

    canonical = _canonical_data_dir(data_dir)
    direction = TO_LEGACY_V1 if to_legacy_v1 else TO_STABLE_V2
    with data_dir_lock(canonical):
        preflight = _preflight(canonical, direction)
        if not apply:
            return _report(preflight, canonical, apply=False)

        # Complete every atomic target write before deleting any source.
        # If this phase is interrupted, all sources remain and the next apply
        # recognizes already-equivalent targets.
        _verify_apply_preconditions(preflight)
        for action in preflight.actions:
            if action.target_already_equivalent:
                continue
            try:
                atomic_write_validated_json(
                    action.target,
                    action.source.kind,
                    action.expected_payload,
                    expected_repository=action.source.repository,
                )
            except (OSError, TypeError, ValueError) as error:
                target_label = (
                    "stable" if action.direction == TO_STABLE_V2 else "legacy"
                )
                raise ProjectIdentityMigrationError(
                    "target_write_failed",
                    f"{target_label} target could not be written atomically: "
                    f"{action.target}: {error}",
                ) from None

        for action in preflight.actions:
            _verify_target(action)
        for action in preflight.actions:
            _verify_source_unchanged(action)
            try:
                _remove_source(action)
            except OSError as error:
                source_label = (
                    "legacy" if action.direction == TO_STABLE_V2 else "stable"
                )
                raise ProjectIdentityMigrationError(
                    "source_cleanup_failed",
                    f"migration targets are durable but {source_label} source cleanup "
                    f"was interrupted: "
                    f"{action.source.path}: {error}",
                ) from None

        return _report(preflight, canonical, apply=True)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate flat Rardar project artifacts between legacy and stable IDs"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the fully preflighted migration (default: dry-run)",
    )
    parser.add_argument(
        "--to-legacy-v1",
        action="store_true",
        help=(
            "mechanically downgrade flat v2 project artifacts to legacy v1 "
            "filenames and payloads (default: migrate v1 to stable v2)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    try:
        report = migrate_project_identity(
            arguments.data_dir,
            apply=arguments.apply,
            to_legacy_v1=arguments.to_legacy_v1,
        )
    except ProjectIdentityMigrationError as error:
        print(
            json.dumps(
                {"status": "failed", "errorCode": error.code, "error": str(error)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
