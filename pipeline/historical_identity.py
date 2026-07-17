"""Build one strictly verified cross-generation project identity bundle.

The bundle is intentionally derived only from the current published
generation and visible retained final generations.  Flat staging and hidden
directories (including ``generations/.candidates``) are outside this trust
boundary.  A visible final generation must pass the same immutable checks as
an explicit rollback target or the complete bundle fails closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from jsonschema import Draft202012Validator, FormatChecker

from pipeline.data_lock import data_dir_lock
from pipeline.generations import (
    CandidateGenerationError,
    GenerationProtocolError,
    ResolvedGeneration,
    VerifiedRetainedGeneration,
    _canonical_data_dir,
    _require_safe_existing_path,
    resolve_current_generation,
    verify_retained_generation,
)
from pipeline.project_identity import (
    PROJECT_ID_VERSION,
    ProjectIdentityError,
    identity_for_repository,
    validate_project_identity,
)
from pipeline.schema_validation import strict_json_dumps, strict_json_loads


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_SCHEMA_PATH = REPOSITORY_ROOT / "contracts" / "historical-identity-bundle.schema.json"


class HistoricalIdentityBundleError(GenerationProtocolError):
    """A verified generation set cannot produce one unambiguous identity map."""


def _fail(
    code: str,
    message: str,
    *,
    generation_id: str | None = None,
    stage: str = "historical_identity",
) -> None:
    raise HistoricalIdentityBundleError(
        code,
        message,
        generation_id=generation_id,
        stage=stage,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_catalog(
    root: Path,
    manifest: dict[str, Any],
    generation_id: str,
) -> dict[str, Any]:
    relative = "catalog/latest.json"
    expected = manifest.get("hashes", {}).get(relative)
    path = root / "catalog" / "latest.json"
    _require_safe_existing_path(path, root.resolve())
    try:
        raw = path.read_bytes()
    except OSError as error:
        _fail(
            "historical_catalog_unavailable",
            f"verified Catalog cannot be read for generation {generation_id}: {error}",
            generation_id=generation_id,
            stage="integrity",
        )
    if not isinstance(expected, str) or _sha256_bytes(raw) != expected:
        _fail(
            "historical_catalog_hash_mismatch",
            f"Catalog changed after generation verification: {generation_id}",
            generation_id=generation_id,
            stage="integrity",
        )
    try:
        payload = strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        _fail(
            "historical_catalog_invalid",
            f"verified Catalog cannot be parsed for generation {generation_id}: {error}",
            generation_id=generation_id,
            stage="catalog",
        )
    if not isinstance(payload, dict):
        _fail(
            "historical_catalog_invalid",
            f"verified Catalog is not an object for generation {generation_id}",
            generation_id=generation_id,
            stage="catalog",
        )
    return payload


def _mapping_for_project(
    project: object,
    *,
    catalog_schema_version: int,
    generation_id: str,
    generation_created_at: str,
    manifest_sha256: str,
    active: bool,
    active_published_at: str,
) -> dict[str, Any]:
    if not isinstance(project, dict):
        _fail(
            "historical_catalog_project_invalid",
            f"Catalog project is not an object in generation {generation_id}",
            generation_id=generation_id,
            stage="catalog",
        )
    repository = project.get("repo")
    project_slug = project.get("slug")
    if not isinstance(project_slug, str) or not project_slug:
        _fail(
            "historical_catalog_project_invalid",
            f"Catalog project has no exact legacy slug in generation {generation_id}",
            generation_id=generation_id,
            stage="catalog",
        )
    try:
        if catalog_schema_version == 3:
            identity = validate_project_identity(
                repository,
                project.get("projectId"),
                project.get("projectIdVersion"),
            )
        else:
            identity = identity_for_repository(repository)
    except ProjectIdentityError as error:
        _fail(
            error.code,
            f"Catalog identity is invalid in generation {generation_id}: {error}",
            generation_id=generation_id,
            stage="identity",
        )
    return {
        "generationId": generation_id,
        "generationCreatedAt": generation_created_at,
        # Only current.json records an activation time.  A retained generation
        # may have been activated more than once through rollback, so its
        # publishedAt is deliberately null rather than inferred from createdAt.
        "publishedAt": active_published_at if active else None,
        "manifestSha256": manifest_sha256,
        "catalogSchemaVersion": catalog_schema_version,
        "projectIdVersion": PROJECT_ID_VERSION,
        "projectId": identity.project_id,
        "canonicalRepository": identity.canonical_repository,
        "projectSlug": project_slug,
        "active": active,
    }


def _generation_payload(
    *,
    generation_id: str,
    root: Path,
    manifest: dict[str, Any],
    manifest_sha256: str,
    active: bool,
    active_published_at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    catalog = _read_catalog(root, manifest, generation_id)
    schema_version = catalog.get("schemaVersion")
    projects = catalog.get("projects")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in {1, 2, 3}
        or not isinstance(projects, list)
    ):
        _fail(
            "historical_catalog_invalid",
            f"Catalog version or projects are invalid in generation {generation_id}",
            generation_id=generation_id,
            stage="catalog",
        )
    created_at = manifest.get("createdAt")
    if not isinstance(created_at, str):
        _fail(
            "historical_manifest_invalid",
            f"generation {generation_id} has no verified createdAt",
            generation_id=generation_id,
            stage="manifest",
        )
    generation = {
        "generationId": generation_id,
        "generationCreatedAt": created_at,
        # Only current.json records an activation time.  A retained generation
        # may have been activated more than once through rollback, so its
        # publishedAt is deliberately null rather than inferred from createdAt.
        "publishedAt": active_published_at if active else None,
        "manifestSha256": manifest_sha256,
        "catalogSchemaVersion": schema_version,
        "active": active,
    }
    mappings = [
        _mapping_for_project(
            project,
            catalog_schema_version=schema_version,
            generation_id=generation_id,
            generation_created_at=created_at,
            manifest_sha256=manifest_sha256,
            active=active,
            active_published_at=active_published_at,
        )
        for project in projects
    ]
    return generation, mappings


def _validate_cross_generation_mappings(mappings: list[dict[str, Any]]) -> None:
    by_project_id: dict[str, dict[str, Any]] = {}
    by_repository: dict[str, dict[str, Any]] = {}
    by_slug: dict[str, dict[str, Any]] = {}
    for mapping in mappings:
        project_id = str(mapping["projectId"])
        repository = str(mapping["canonicalRepository"])
        slug = str(mapping["projectSlug"])
        same_id = by_project_id.get(project_id)
        if same_id is not None and same_id["canonicalRepository"] != repository:
            _fail(
                "historical_project_id_collision",
                f"projectId {project_id!r} belongs to multiple canonical repositories",
                generation_id=str(mapping["generationId"]),
                stage="identity",
            )
        same_repository = by_repository.get(repository)
        if same_repository is not None and same_repository["projectId"] != project_id:
            _fail(
                "historical_repository_collision",
                f"canonical repository {repository!r} has multiple Stable Project IDs",
                generation_id=str(mapping["generationId"]),
                stage="identity",
            )
        same_slug = by_slug.get(slug)
        if same_slug is not None and same_slug["projectId"] != project_id:
            _fail(
                "historical_project_slug_rebind",
                f"legacy slug {slug!r} is rebound to different Stable Project IDs",
                generation_id=str(mapping["generationId"]),
                stage="identity",
            )
        by_project_id[project_id] = mapping
        by_repository[repository] = mapping
        by_slug[slug] = mapping


def _validate_bundle_schema(bundle: dict[str, Any]) -> None:
    try:
        schema = strict_json_loads(BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        _fail(
            "historical_identity_schema_unavailable",
            f"Historical Identity Bundle Schema cannot be loaded: {error}",
            stage="contract",
        )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(bundle), key=lambda item: list(item.absolute_path))
    if errors:
        first = errors[0]
        location = "/" + "/".join(str(part) for part in first.absolute_path)
        _fail(
            "historical_identity_bundle_invalid",
            f"Historical Identity Bundle violates its Schema at {location}: {first.message}",
            stage="contract",
        )


def _visible_generation_names(generations_root: Path) -> list[str]:
    try:
        entries = sorted(generations_root.iterdir(), key=lambda path: path.name)
    except OSError as error:
        _fail(
            "historical_generations_unavailable",
            f"retained generations cannot be enumerated: {error}",
            stage="path",
        )
    # Hidden directories and files are outside the historical trust source.
    # Every visible entry is treated as an asserted retained final generation;
    # the strict verifier below rejects invalid names, files, links, failed
    # manifests, and damaged generations instead of silently dropping them.
    return [entry.name for entry in entries if not entry.name.startswith(".")]


def _current_verified_record(current: ResolvedGeneration) -> VerifiedRetainedGeneration:
    if (
        current.legacy
        or current.generation_id is None
        or current.manifest is None
        or current.pointer is None
    ):
        _fail(
            "historical_identity_requires_published_generation",
            "Historical identity bootstrap requires a valid current generation pointer",
            stage="pointer",
        )
    digest = current.pointer.get("manifestSha256")
    if not isinstance(digest, str):
        _fail(
            "historical_manifest_digest_missing",
            "current generation has no verified manifest digest",
            generation_id=current.generation_id,
            stage="integrity",
        )
    audit = current.manifest.get("audit")
    if not isinstance(audit, dict):
        _fail(
            "historical_manifest_invalid",
            "current generation has no verified audit summary",
            generation_id=current.generation_id,
            stage="manifest",
        )
    return VerifiedRetainedGeneration(
        data_dir=current.data_dir,
        generation_id=current.generation_id,
        root=current.root,
        manifest=current.manifest,
        audit=audit,
        manifest_sha256=digest,
    )


def build_historical_identity_bundle(data_dir: Path) -> dict[str, Any]:
    """Return one deterministic, fail-closed Historical Identity Bundle v1."""

    canonical = _canonical_data_dir(data_dir)
    with data_dir_lock(canonical):
        current = resolve_current_generation(canonical, verify_audit=True)
        current_record = _current_verified_record(current)
        assert current.pointer is not None
        active_published_at = str(current.pointer["publishedAt"])

        generations_root = canonical / "generations"
        _require_safe_existing_path(generations_root, canonical, directory=True)
        generation_names = _visible_generation_names(generations_root)
        if current_record.generation_id not in generation_names:
            _fail(
                "historical_current_generation_missing",
                "current generation is absent from the visible retained generation set",
                generation_id=current_record.generation_id,
                stage="resolve",
            )

        verified: list[VerifiedRetainedGeneration] = []
        for generation_id in generation_names:
            if generation_id == current_record.generation_id:
                verified.append(current_record)
            else:
                verified.append(verify_retained_generation(canonical, generation_id))

        verified.sort(
            key=lambda item: (str(item.manifest["createdAt"]), item.generation_id)
        )
        generations: list[dict[str, Any]] = []
        mappings: list[dict[str, Any]] = []
        for generation in verified:
            generation_payload, generation_mappings = _generation_payload(
                generation_id=generation.generation_id,
                root=generation.root,
                manifest=generation.manifest,
                manifest_sha256=generation.manifest_sha256,
                active=generation.generation_id == current_record.generation_id,
                active_published_at=active_published_at,
            )
            generations.append(generation_payload)
            mappings.extend(generation_mappings)
        generations.sort(
            key=lambda item: (
                str(item["generationCreatedAt"]),
                str(item["generationId"]),
            )
        )
        mappings.sort(
            key=lambda item: (
                str(item["generationCreatedAt"]),
                str(item["generationId"]),
                str(item["projectId"]),
                str(item["projectSlug"]),
            )
        )
        _validate_cross_generation_mappings(mappings)
        bundle = {
            "schemaVersion": 1,
            "activeGenerationId": current_record.generation_id,
            "activePublishedAt": active_published_at,
            "generationCount": len(verified),
            "mappingCount": len(mappings),
            "generations": generations,
            "mappings": mappings,
        }
        _validate_bundle_schema(bundle)
        return bundle


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a strictly verified Historical Identity Bundle",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    arguments = parser.parse_args(argv)
    try:
        bundle = build_historical_identity_bundle(arguments.data_dir)
    except (GenerationProtocolError, ProjectIdentityError) as error:
        code = getattr(error, "code", "historical_identity_failed")
        print(
            strict_json_dumps(
                {
                    "schemaVersion": 1,
                    "status": "error",
                    "errorCode": str(code),
                    "error": str(error),
                }
            ),
            file=sys.stderr,
        )
        return 1
    print(strict_json_dumps(bundle))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
