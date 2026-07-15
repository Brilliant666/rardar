"""Refresh Rardar facts, history, static evidence, and the web catalog."""

from __future__ import annotations

import argparse
import os
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.analyze_repository import analyze_remote
from pipeline.build_catalog import build_catalog
from pipeline.collect_github import GitHubClient, collect
from pipeline.collect_signals import HttpClient, collect_signals
from pipeline.codex_queue import build_codex_queue
from pipeline.generations import (
    CandidateGeneration,
    CandidateGenerationError,
    GenerationProtocolError,
    create_candidate_generation,
    fail_candidate_generation,
    publish_candidate_generation,
)
from pipeline.project_artifacts import (
    adopt_candidate_project_identities,
    load_project_artifacts,
)
from pipeline.project_identity import project_id_for_repository
from pipeline.schema_validation import (
    ArtifactKind,
    infer_artifact_kind,
    load_validated_json,
    require_valid_for_path,
    strict_json_dumps,
    strict_json_loads,
)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    kind = infer_artifact_kind(path)
    if kind is not None:
        return load_validated_json(path, kind)
    payload = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    require_valid_for_path(path, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(strict_json_dumps(payload) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_json_batch(entries: list[tuple[Path, dict[str, Any]]]) -> None:
    """Replace a related set of JSON files together, rolling back write failures."""
    transaction_id = uuid.uuid4().hex
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path | None] = {}
    changed: list[Path] = []
    serialized: dict[Path, str] = {}

    for path, payload in entries:
        if path in serialized:
            raise ValueError(f"duplicate JSON batch target: {path}")
        require_valid_for_path(path, payload)
        serialized[path] = strict_json_dumps(payload) + "\n"

    try:
        for path, _ in entries:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{transaction_id}.tmp")
            temporary.write_text(serialized[path], encoding="utf-8")
            staged[path] = temporary

        for path, _ in entries:
            backup: Path | None = None
            if path.exists():
                backup = path.with_name(f".{path.name}.{transaction_id}.bak")
                path.replace(backup)
            backups[path] = backup
            changed.append(path)
            staged[path].replace(path)
    except Exception:
        for path in reversed(changed):
            backup = backups.get(path)
            try:
                if path.exists():
                    path.unlink()
                if backup and backup.exists():
                    backup.replace(path)
            except OSError:
                # Keep a remaining backup for manual recovery instead of
                # deleting the only good copy while handling another error.
                pass
        raise
    else:
        for backup in backups.values():
            if backup and backup.exists():
                try:
                    backup.unlink()
                except OSError:
                    pass
    finally:
        for temporary in staged.values():
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass


def _safe_name(repository: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", repository.lower().replace("/", "--")).strip("-")


def _history_name(snapshot: dict[str, Any]) -> str:
    captured = str(snapshot.get("captured_at") or "unknown")
    normalized = re.sub(r"[^0-9]+", "", captured)[:14] or "unknown"
    return f"{normalized}.json"


def _load_analyses(directory: Path) -> dict[str, dict[str, Any]]:
    return load_project_artifacts(directory, ArtifactKind.STATIC_EVIDENCE)


def _load_enrichments(directory: Path) -> dict[str, dict[str, Any]]:
    return load_project_artifacts(directory, ArtifactKind.PROJECT_ENRICHMENT)


def _load_snapshot_history(
    directory: Path,
    previous: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    if directory.exists():
        for path in directory.glob("*.json"):
            payload = _read_json(path)
            if payload and payload.get("captured_at"):
                snapshots[str(payload["captured_at"])] = payload
    if previous and previous.get("captured_at"):
        snapshots[str(previous["captured_at"])] = previous
    return sorted(snapshots.values(), key=lambda item: str(item.get("captured_at") or ""))


def _ensure_signal_enrichment(generation_root: Path, generated_at: datetime) -> dict[str, Any]:
    """Ensure every candidate has the consumer-required enrichment artifact."""
    path = generation_root / "signals" / "enrichment.json"
    existing = _read_json(path)
    if existing is not None:
        return existing
    payload = {
        "schemaVersion": 1,
        "generatedAt": generated_at.astimezone(timezone.utc).isoformat(),
        "model": "none",
        "items": {},
    }
    _write_json(path, payload)
    return payload


def _refresh_candidate_tree(
    candidate: CandidateGeneration,
    now: datetime,
    since_days: int = 14,
    limit: int = 30,
    analyze_top: int = 5,
    client: GitHubClient | None = None,
    signal_client: HttpClient | None = None,
    collect_external_signals: bool = True,
) -> dict[str, Any]:
    generation_root = candidate.path
    snapshots_dir = generation_root / "snapshots"
    latest_snapshot_path = snapshots_dir / "latest.json"
    history_dir = snapshots_dir / "history"
    analysis_dir = generation_root / "analysis"
    enrichment_dir = generation_root / "enrichment"
    catalog_path = generation_root / "catalog" / "latest.json"
    _ensure_signal_enrichment(generation_root, now)
    adopt_candidate_project_identities(generation_root)

    previous = _read_json(latest_snapshot_path)
    history = _load_snapshot_history(history_dir, previous)
    current = collect(client or GitHubClient(os.environ.get("GITHUB_TOKEN")), now, since_days)
    # Validate collector output before it drives analysis or any filesystem
    # mutation.  The final batch validates again at the publication boundary.
    require_valid_for_path(latest_snapshot_path, current)

    preliminary = build_catalog(
        current,
        previous,
        limit,
        history=history,
        schema_version=3,
    )
    failures: list[dict[str, str]] = []
    for project in preliminary["projects"][: max(0, min(analyze_top, 10))]:
        repository = project["repo"]
        output = analysis_dir / f"{project_id_for_repository(repository)}.json"
        try:
            _write_json(output, asdict(analyze_remote(repository)))
        except (RuntimeError, OSError, ValueError) as error:
            failures.append({"repository": repository, "error": str(error)})

    catalog = build_catalog(
        current,
        previous,
        limit,
        _load_analyses(analysis_dir),
        _load_enrichments(enrichment_dir),
        history,
        schema_version=3,
    )
    catalog["previousCapturedAt"] = previous.get("captured_at") if previous else None
    catalog["analysisFailures"] = failures
    signals: dict[str, Any] = _read_json(generation_root / "signals" / "latest.json") or {"signals": []}
    if collect_external_signals:
        signals = collect_signals(
            signal_client or HttpClient(os.environ.get("GITHUB_TOKEN")),
            now,
            window_hours=48,
            limit=30,
        )
        require_valid_for_path(generation_root / "signals" / "latest.json", signals)
        catalog["signalCount"] = signals["signalCount"]
        catalog["healthySignalSourceCount"] = signals["healthySourceCount"]
    queue = build_codex_queue(
        catalog,
        signals,
        enrichment_dir,
        generation_root / "signals" / "enrichment.json",
        now,
        input_data_prefix=f"data/generations/{candidate.generation_id}",
    )
    catalog["codexPendingCount"] = queue["pendingCount"]

    # The snapshot becomes the next run's growth baseline. Publish it only
    # after every derived artifact has been prepared successfully so a failed
    # signal or catalog stage cannot silently advance that baseline.
    writes: list[tuple[Path, dict[str, Any]]] = []
    if previous and previous.get("captured_at") != current.get("captured_at"):
        writes.append((history_dir / _history_name(previous), previous))
    if collect_external_signals:
        writes.append((generation_root / "signals" / "latest.json", signals))
    writes.extend(
        [
            (generation_root / "queues" / "codex.json", queue),
            (catalog_path, catalog),
            (latest_snapshot_path, current),
        ]
    )
    _write_json_batch(writes)
    return catalog


def refresh(
    data_dir: Path,
    now: datetime,
    since_days: int = 14,
    limit: int = 30,
    analyze_top: int = 5,
    client: GitHubClient | None = None,
    signal_client: HttpClient | None = None,
    collect_external_signals: bool = True,
) -> dict[str, Any]:
    """Build and publish one audited refresh generation.

    Collection and static analysis happen in a private candidate. The only
    mutation visible to readers is the final atomic current-pointer switch.
    """
    canonical = data_dir.expanduser().resolve()
    candidate = create_candidate_generation(
        canonical,
        "refresh",
        created_at=now,
    )
    try:
        catalog = _refresh_candidate_tree(
            candidate,
            now,
            since_days,
            limit,
            analyze_top,
            client,
            signal_client,
            collect_external_signals,
        )
    except Exception as error:
        fail_candidate_generation(candidate, "build", str(error))
        if isinstance(error, GenerationProtocolError):
            raise
        raise CandidateGenerationError(
            "candidate_build_failed",
            f"refresh candidate build failed: {error}",
            generation_id=candidate.generation_id,
            stage="build",
        ) from error
    try:
        publish_candidate_generation(candidate, published_at=now)
    except Exception as error:
        fail_candidate_generation(candidate, "publish", str(error))
        raise
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the complete local Rardar data pipeline")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--since-days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--analyze-top", type=int, default=5)
    arguments = parser.parse_args()

    catalog = refresh(
        data_dir=arguments.data_dir,
        now=datetime.now(timezone.utc),
        since_days=max(1, min(arguments.since_days, 90)),
        limit=max(5, min(arguments.limit, 100)),
        analyze_top=max(0, min(arguments.analyze_top, 10)),
    )
    print(
        f"refreshed {catalog['sourceCount']} facts -> {catalog['projectCount']} projects; "
        f"static analysis failures: {len(catalog['analysisFailures'])}"
    )


if __name__ == "__main__":
    main()
