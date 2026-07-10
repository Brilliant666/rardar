"""Rebuild catalog and Codex queue from committed local facts only.

This command never collects GitHub or signal data and never advances the
snapshot baseline. It is the safe way to apply newly written Codex enrichment
files after a scheduled refresh.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.build_catalog import build_catalog
from pipeline.codex_queue import build_codex_queue
from pipeline.refresh import (
    _load_analyses,
    _load_enrichments,
    _load_snapshot_history,
    _write_json_batch,
)


def _required_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"required local artifact is unavailable: {path}: {error}") from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"required local artifact must be a JSON object: {path}")
    return payload


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _previous_snapshot(
    current: dict[str, Any],
    history: list[dict[str, Any]],
    previous_captured_at: object,
) -> dict[str, Any] | None:
    declared_previous = _parse_time(previous_captured_at)
    if declared_previous:
        matches = [
            item
            for item in history
            if _parse_time(item.get("captured_at")) == declared_previous
        ]
        if len(matches) != 1:
            raise RuntimeError("catalog previousCapturedAt does not match exactly one history snapshot")
        return matches[0]

    current_at = _parse_time(current.get("captured_at"))
    eligible = [
        item
        for item in history
        if (captured_at := _parse_time(item.get("captured_at")))
        and (not current_at or captured_at < current_at)
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda item: _parse_time(item.get("captured_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
    )


def rebuild_derived(
    data_dir: Path,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    data_dir = data_dir.resolve()
    snapshot_path = data_dir / "snapshots" / "latest.json"
    catalog_path = data_dir / "catalog" / "latest.json"
    signals_path = data_dir / "signals" / "latest.json"
    queue_path = data_dir / "queues" / "codex.json"
    snapshot = _required_json(snapshot_path)
    existing_catalog = _required_json(catalog_path)
    signals = _required_json(signals_path)
    existing_queue = _required_json(queue_path)
    history = _load_snapshot_history(data_dir / "snapshots" / "history", None)
    previous = _previous_snapshot(
        snapshot,
        history,
        existing_catalog.get("previousCapturedAt"),
    )
    limit = max(5, min(int(existing_catalog.get("projectCount") or 30), 100))
    catalog = build_catalog(
        snapshot,
        previous,
        limit,
        _load_analyses(data_dir / "analysis"),
        _load_enrichments(data_dir / "enrichment"),
        history,
    )
    catalog["previousCapturedAt"] = previous.get("captured_at") if previous else None
    catalog["analysisFailures"] = existing_catalog.get("analysisFailures") or []
    catalog["signalCount"] = int(signals.get("signalCount") or len(signals.get("signals") or []))
    catalog["healthySignalSourceCount"] = int(signals.get("healthySourceCount") or 0)

    scope = existing_queue.get("scope") if isinstance(existing_queue.get("scope"), dict) else {}
    project_limit = max(1, min(int(scope.get("projectLimit") or 5), 30))
    signal_limit = max(1, min(int(scope.get("signalLimit") or 10), 30))
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    queue = build_codex_queue(
        catalog,
        signals,
        data_dir / "enrichment",
        data_dir / "signals" / "enrichment.json",
        generated_at,
        project_limit,
        signal_limit,
    )
    catalog["codexPendingCount"] = queue["pendingCount"]
    _write_json_batch([(catalog_path, catalog), (queue_path, queue)])
    return catalog, queue


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild derived Rardar catalog and queue without collecting new facts"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    arguments = parser.parse_args()
    catalog, queue = rebuild_derived(arguments.data_dir)
    print(
        json.dumps(
            {
                "capturedAt": catalog["capturedAt"],
                "growthMode": catalog["growthMode"],
                "projectCount": catalog["projectCount"],
                "pendingCount": queue["pendingCount"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
