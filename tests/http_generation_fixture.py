"""Build isolated, fully validated generations for the Vinext HTTP test.

This helper deliberately uses the production generation API.  It never writes
to the repository's live data tree: a verified source generation is copied to
the caller-provided temporary data directory before test generations are
created, audited, and published there.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from pipeline.generations import (
    _atomic_write_json,
    _rebuild_candidate_queue_paths,
    create_candidate_generation,
    finalize_candidate_generation,
    publish_candidate_generation,
    resolve_current_generation,
    rollback_to_generation,
)
from pipeline.project_identity import project_id_for_repository
from pipeline.schema_validation import strict_json_loads


GENERATION_A = "http-generation-a"
GENERATION_B = "http-generation-b"
GENERATION_WITH_REMOVAL = "http-generation-with-removal"
CATALOG_MARKER_A = "HTTP_CATALOG_GENERATION_A"
CATALOG_MARKER_B = "HTTP_CATALOG_GENERATION_B"
SIGNAL_MARKER_A = "HTTP_SIGNAL_GENERATION_A"
SIGNAL_MARKER_B = "HTTP_SIGNAL_GENERATION_B"
FLAT_MARKER = "HTTP_FLAT_DATA_MUST_NOT_LOAD"


def _read_object(path: Path) -> dict[str, Any]:
    payload = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected a JSON object at {path}")
    return payload


def _seed_verified_generation(source_data: Path, target_data: Path) -> None:
    source = resolve_current_generation(source_data)
    if source.legacy or not source.generation_id:
        raise RuntimeError("the HTTP fixture requires a published source generation")

    target_generations = target_data / "generations"
    target_generations.mkdir(parents=True, exist_ok=True)
    copied_root = target_generations / source.generation_id
    shutil.copytree(source.root, copied_root)

    manifest = _read_object(copied_root / "manifest.json")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise RuntimeError("the source generation manifest has no artifacts")
    # Plant a complete, otherwise usable legacy flat tree.  The corruption
    # assertion below can then distinguish true fail-closed behavior from an
    # implementation that merely falls back and fails for unrelated missing
    # flat files.
    for artifact in artifacts:
        if not isinstance(artifact, str):
            raise RuntimeError("the source generation artifact list is invalid")
        relative = Path(artifact)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"unsafe source artifact path: {artifact}")
        source_path = copied_root / relative
        target_path = target_data / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)

    manifest_bytes = (copied_root / "manifest.json").read_bytes()
    _atomic_write_json(
        target_data / "current.json",
        {
            "schemaVersion": 1,
            "generationId": source.generation_id,
            # Keep the seed safely behind real publication time so the
            # production monotonicity guard remains active for A and B.
            "publishedAt": "2000-01-01T00:00:00+00:00",
            "previousGenerationId": None,
            "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
    )
    resolve_current_generation(target_data)


def _copy_retained_v1_generation(source_data: Path, target_data: Path) -> str:
    """Copy one real retained Catalog v1 generation for Web compatibility tests.

    The HTTP test later enters it through the production rollback API, which
    performs the complete manifest, hash, Schema, and semantic audit again.
    """
    source_generations = source_data.resolve() / "generations"
    target_generations = target_data.resolve() / "generations"
    for source_root in sorted(source_generations.iterdir(), reverse=True):
        if source_root.name.startswith(".") or not source_root.is_dir() or source_root.is_symlink():
            continue
        catalog_path = source_root / "catalog" / "latest.json"
        manifest_path = source_root / "manifest.json"
        if not catalog_path.is_file() or not manifest_path.is_file():
            continue
        catalog = _read_object(catalog_path)
        manifest = _read_object(manifest_path)
        if catalog.get("schemaVersion") != 1 or manifest.get("state") != "ready":
            continue
        generation_id = str(manifest.get("generationId") or "")
        if generation_id != source_root.name:
            raise RuntimeError("retained Catalog v1 manifest identity is inconsistent")
        destination = target_generations / generation_id
        if not destination.exists():
            shutil.copytree(source_root, destination)
        return generation_id
    raise RuntimeError("the HTTP fixture requires one retained Catalog v1 generation")


def _mark_candidate(root: Path, catalog_marker: str, signal_marker: str) -> None:
    catalog_path = root / "catalog" / "latest.json"
    catalog = _read_object(catalog_path)
    catalog["notice"] = catalog_marker
    _atomic_write_json(catalog_path, catalog)

    signals_path = root / "signals" / "latest.json"
    signals = _read_object(signals_path)
    top_signals = signals.get("topSignals")
    all_signals = signals.get("signals")
    if not isinstance(top_signals, list) or not top_signals:
        raise RuntimeError("the source generation needs at least one top signal")
    if not isinstance(all_signals, list):
        raise RuntimeError("the source generation signals list is invalid")
    first = top_signals[0]
    if not isinstance(first, dict) or not isinstance(first.get("id"), str):
        raise RuntimeError("the source generation top signal is invalid")
    signal_id = first["id"]
    matches = 0
    for rows in (top_signals, all_signals):
        for row in rows:
            if isinstance(row, dict) and row.get("id") == signal_id:
                row["source"] = signal_marker
                matches += 1
    if matches < 2:
        raise RuntimeError("the top signal is not present in the complete signal list")
    _atomic_write_json(signals_path, signals)


def _publish_marked_generation(
    data_dir: Path,
    generation_id: str,
    catalog_marker: str,
    signal_marker: str,
) -> None:
    candidate = create_candidate_generation(
        data_dir,
        "derive",
        generation_id=generation_id,
        overlay_flat_staging=False,
    )
    _mark_candidate(candidate.path, catalog_marker, signal_marker)
    # The queue binds evidence paths and signal details to the candidate's
    # eventual immutable path, so rebuild it after applying the test marker.
    _rebuild_candidate_queue_paths(candidate.path, generation_id)
    finalize_candidate_generation(candidate)
    published = publish_candidate_generation(candidate)
    if published.current.generation_id != generation_id:
        raise RuntimeError(f"failed to publish {generation_id}")


def _publish_generation_with_removed_project(data_dir: Path) -> dict[str, Any]:
    candidate = create_candidate_generation(
        data_dir,
        "derive",
        generation_id=GENERATION_WITH_REMOVAL,
        overlay_flat_staging=False,
    )
    catalog_path = candidate.path / "catalog" / "latest.json"
    catalog = _read_object(catalog_path)
    projects = catalog.get("projects")
    if not isinstance(projects, list) or len(projects) < 6:
        raise RuntimeError("the HTTP fixture needs a non-Daily-Five project to remove")
    removed = projects.pop()
    if (
        not isinstance(removed, dict)
        or not isinstance(removed.get("slug"), str)
        or not isinstance(removed.get("repo"), str)
    ):
        raise RuntimeError("the removable HTTP fixture project has no stable identity evidence")
    catalog["projectCount"] = len(projects)
    _atomic_write_json(catalog_path, catalog)
    # The removed project is outside the queue's Daily Five scope, so rebuilding
    # immutable paths is sufficient and the production audit verifies that the
    # resulting queue still exactly matches the pruned Catalog.
    _rebuild_candidate_queue_paths(candidate.path, GENERATION_WITH_REMOVAL)
    finalize_candidate_generation(candidate)
    published = publish_candidate_generation(candidate)
    if published.current.generation_id != GENERATION_WITH_REMOVAL:
        raise RuntimeError("failed to publish the project-removal generation")
    return removed


def prepare(source_data: Path, target_data: Path) -> dict[str, str]:
    target_data.mkdir(parents=True, exist_ok=False)
    _seed_verified_generation(source_data.resolve(), target_data.resolve())
    legacy_generation = _copy_retained_v1_generation(
        source_data.resolve(),
        target_data.resolve(),
    )
    _publish_marked_generation(
        target_data,
        GENERATION_A,
        CATALOG_MARKER_A,
        SIGNAL_MARKER_A,
    )
    _publish_marked_generation(
        target_data,
        GENERATION_B,
        CATALOG_MARKER_B,
        SIGNAL_MARKER_B,
    )
    removed_project = _publish_generation_with_removed_project(target_data)
    rolled_back = rollback_to_generation(target_data, GENERATION_A)
    if rolled_back.current.generation_id != GENERATION_A:
        raise RuntimeError("failed to establish generation A as the initial pointer")

    # Make the complete legacy flat tree visibly renderable if a regression
    # ever bypasses the damaged pointer and reads it.
    flat_catalog_path = target_data / "catalog" / "latest.json"
    flat_catalog = _read_object(flat_catalog_path)
    flat_projects = flat_catalog.get("projects")
    if not isinstance(flat_projects, list) or not flat_projects or not isinstance(flat_projects[0], dict):
        raise RuntimeError("the source generation needs a renderable flat catalog")
    flat_projects[0]["title"] = FLAT_MARKER
    _atomic_write_json(flat_catalog_path, flat_catalog)
    current = resolve_current_generation(target_data)
    current_catalog = _read_object(current.root / "catalog" / "latest.json")
    current_projects = current_catalog.get("projects")
    if (
        not isinstance(current_projects, list)
        or len(current_projects) < 2
        or not isinstance(current_projects[0], dict)
        or not isinstance(current_projects[0].get("slug"), str)
        or not isinstance(current_projects[0].get("repo"), str)
    ):
        raise RuntimeError("the source generation needs a project slug for action API testing")
    legacy_catalog = _read_object(
        target_data / "generations" / legacy_generation / "catalog" / "latest.json"
    )
    legacy_projects = legacy_catalog.get("projects")
    if (
        not isinstance(legacy_projects, list)
        or not legacy_projects
        or not isinstance(legacy_projects[0], dict)
        or not isinstance(legacy_projects[0].get("slug"), str)
        or not isinstance(legacy_projects[0].get("repo"), str)
    ):
        raise RuntimeError("the retained Catalog v1 generation needs a project identity fixture")
    current_project = current_projects[0]
    second_current_project = current_projects[1]
    if (
        not isinstance(second_current_project, dict)
        or not isinstance(second_current_project.get("slug"), str)
        or not isinstance(second_current_project.get("repo"), str)
    ):
        raise RuntimeError("the source generation needs a second project identity fixture")
    legacy_project = legacy_projects[0]
    return {
        "generationA": GENERATION_A,
        "generationB": GENERATION_B,
        "generationWithRemoval": GENERATION_WITH_REMOVAL,
        "legacyGeneration": legacy_generation,
        "catalogMarkerA": CATALOG_MARKER_A,
        "catalogMarkerB": CATALOG_MARKER_B,
        "signalMarkerA": SIGNAL_MARKER_A,
        "signalMarkerB": SIGNAL_MARKER_B,
        "flatMarker": FLAT_MARKER,
        "projectSlug": current_project["slug"],
        "projectRepository": current_project["repo"],
        "projectId": project_id_for_repository(current_project["repo"]),
        "secondProjectSlug": second_current_project["slug"],
        "secondProjectId": project_id_for_repository(second_current_project["repo"]),
        "removedProjectSlug": removed_project["slug"],
        "removedProjectId": project_id_for_repository(removed_project["repo"]),
        "legacyProjectSlug": legacy_project["slug"],
        "legacyProjectRepository": legacy_project["repo"],
        "legacyProjectId": project_id_for_repository(legacy_project["repo"]),
    }


def corrupt_pointer(data_dir: Path) -> dict[str, Any]:
    pointer_path = data_dir.resolve() / "current.json"
    pointer = _read_object(pointer_path)
    pointer["manifestSha256"] = "0" * 64
    _atomic_write_json(pointer_path, pointer)
    return pointer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare isolated Vinext HTTP generation fixtures")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--source-data", type=Path, required=True)
    prepare_command.add_argument("--target-data", type=Path, required=True)
    corrupt_command = commands.add_parser("corrupt-pointer")
    corrupt_command.add_argument("--data-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "prepare":
        payload = prepare(arguments.source_data, arguments.target_data)
    else:
        payload = corrupt_pointer(arguments.data_dir)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
