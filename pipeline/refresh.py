"""Refresh Rardar facts, history, static evidence, and the web catalog."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.analyze_repository import analyze_remote
from pipeline.build_catalog import build_catalog
from pipeline.collect_github import GitHubClient, collect
from pipeline.collect_signals import HttpClient, collect_signals


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _safe_name(repository: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", repository.lower().replace("/", "--")).strip("-")


def _history_name(snapshot: dict[str, Any]) -> str:
    captured = str(snapshot.get("captured_at") or "unknown")
    normalized = re.sub(r"[^0-9]+", "", captured)[:14] or "unknown"
    return f"{normalized}.json"


def _load_analyses(directory: Path) -> dict[str, dict[str, Any]]:
    analyses: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return analyses
    for path in directory.glob("*.json"):
        payload = _read_json(path)
        if payload and payload.get("repository"):
            analyses[str(payload["repository"])] = payload
    return analyses


def _load_enrichments(directory: Path) -> dict[str, dict[str, Any]]:
    enrichments: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return enrichments
    for path in directory.glob("*.json"):
        payload = _read_json(path)
        if payload and payload.get("repository"):
            enrichments[str(payload["repository"])] = payload
    return enrichments


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
    snapshots_dir = data_dir / "snapshots"
    latest_snapshot_path = snapshots_dir / "latest.json"
    history_dir = snapshots_dir / "history"
    analysis_dir = data_dir / "analysis"
    enrichment_dir = data_dir / "enrichment"
    catalog_path = data_dir / "catalog" / "latest.json"

    previous = _read_json(latest_snapshot_path)
    current = collect(client or GitHubClient(os.environ.get("GITHUB_TOKEN")), now, since_days)

    if previous and previous.get("captured_at") != current.get("captured_at"):
        _write_json(history_dir / _history_name(previous), previous)
    _write_json(latest_snapshot_path, current)

    preliminary = build_catalog(current, previous, limit)
    failures: list[dict[str, str]] = []
    for project in preliminary["projects"][: max(0, min(analyze_top, 10))]:
        repository = project["repo"]
        output = analysis_dir / f"{_safe_name(repository)}.json"
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
    )
    catalog["previousCapturedAt"] = previous.get("captured_at") if previous else None
    catalog["analysisFailures"] = failures
    if collect_external_signals:
        signals = collect_signals(
            signal_client or HttpClient(os.environ.get("GITHUB_TOKEN")),
            now,
            window_hours=48,
            limit=30,
        )
        _write_json(data_dir / "signals" / "latest.json", signals)
        catalog["signalCount"] = signals["signalCount"]
        catalog["healthySignalSourceCount"] = signals["healthySourceCount"]
    _write_json(catalog_path, catalog)
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
