"""Read-only consistency audit for Rardar's committed local data artifacts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


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


def _load(path: Path, issues: list[dict[str, str]]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        issues.append({"severity": "error", "code": "missing_artifact", "detail": str(path)})
        return {}
    except (json.JSONDecodeError, OSError) as error:
        issues.append({"severity": "error", "code": "invalid_artifact", "detail": f"{path}: {error}"})
        return {}
    if not isinstance(payload, dict):
        issues.append({"severity": "error", "code": "invalid_artifact_shape", "detail": str(path)})
        return {}
    return payload


def _add_if(issues: list[dict[str, str]], condition: bool, code: str, detail: str, severity: str = "error") -> None:
    if condition:
        issues.append({"severity": severity, "code": code, "detail": detail})


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _check_count(
    issues: list[dict[str, str]],
    declared: object,
    actual: int,
    code: str,
    detail: str,
) -> None:
    _add_if(issues, _integer(declared) != actual, code, detail)


def audit_data(data_dir: Path) -> dict[str, Any]:
    data_dir = data_dir.resolve()
    issues: list[dict[str, str]] = []
    snapshot = _load(data_dir / "snapshots" / "latest.json", issues)
    catalog = _load(data_dir / "catalog" / "latest.json", issues)
    signals = _load(data_dir / "signals" / "latest.json", issues)
    queue = _load(data_dir / "queues" / "codex.json", issues)

    repository_value = snapshot.get("repositories")
    project_value = catalog.get("projects")
    signal_value = signals.get("signals")
    source_value = signals.get("sourceStatus")
    queue_value = queue.get("items")
    repositories = repository_value if isinstance(repository_value, list) else []
    projects = project_value if isinstance(project_value, list) else []
    signal_items = signal_value if isinstance(signal_value, list) else []
    source_items = source_value if isinstance(source_value, list) else []
    queue_items = queue_value if isinstance(queue_value, list) else []
    _add_if(issues, not isinstance(repository_value, list), "invalid_snapshot_rows", "snapshot repositories must be a list")
    _add_if(issues, not isinstance(project_value, list), "invalid_catalog_rows", "catalog projects must be a list")
    _add_if(issues, not isinstance(signal_value, list), "invalid_signal_rows", "signals must be a list")
    _add_if(issues, not isinstance(source_value, list), "invalid_source_rows", "sourceStatus must be a list")
    _add_if(issues, not isinstance(queue_value, list), "invalid_queue_rows", "queue items must be a list")
    _add_if(issues, any(not isinstance(item, dict) for item in repositories), "invalid_snapshot_row", "snapshot repository rows must be objects")
    _add_if(issues, any(not isinstance(item, dict) for item in projects), "invalid_catalog_row", "catalog project rows must be objects")
    _add_if(issues, any(not isinstance(item, dict) for item in signal_items), "invalid_signal_row", "signal rows must be objects")
    _add_if(issues, any(not isinstance(item, dict) for item in source_items), "invalid_source_row", "source rows must be objects")
    _add_if(issues, any(not isinstance(item, dict) for item in queue_items), "invalid_queue_row", "queue rows must be objects")

    snapshot_at = _parse_time(snapshot.get("captured_at"))
    catalog_at = _parse_time(catalog.get("capturedAt"))
    signals_at = _parse_time(signals.get("capturedAt"))
    queue_at = _parse_time(queue.get("generatedAt"))
    _add_if(issues, snapshot_at is None, "invalid_snapshot_time", "snapshots/latest.json captured_at is invalid")
    _add_if(issues, catalog_at is None, "invalid_catalog_time", "catalog/latest.json capturedAt is invalid")
    _add_if(issues, signals_at is None, "invalid_signal_time", "signals/latest.json capturedAt is invalid")
    _add_if(issues, queue_at is None, "invalid_queue_time", "queues/codex.json generatedAt is invalid")
    _add_if(issues, bool(snapshot_at and catalog_at and snapshot_at != catalog_at), "catalog_snapshot_mismatch", "catalog capture time differs from the GitHub snapshot")
    _add_if(issues, bool(snapshot_at and signals_at and signals_at < snapshot_at), "signals_older_than_snapshot", "signal snapshot predates the GitHub snapshot")
    _add_if(issues, bool(catalog_at and queue_at and queue_at < catalog_at), "queue_older_than_catalog", "Codex queue predates its catalog input")
    _add_if(issues, bool(signals_at and queue_at and queue_at < signals_at), "queue_older_than_signals", "Codex queue predates its signal input")

    repository_names = [str(item.get("repo") or "") for item in repositories if isinstance(item, dict)]
    project_repositories = [str(item.get("repo") or "") for item in projects if isinstance(item, dict)]
    project_slugs = [str(item.get("slug") or "") for item in projects if isinstance(item, dict)]
    _check_count(issues, snapshot.get("count"), len(repositories), "snapshot_count_mismatch", "snapshot count differs from repository rows")
    _add_if(issues, len(repository_names) != len(set(repository_names)) or "" in repository_names, "duplicate_snapshot_repository", "snapshot repository names must be non-empty and unique")
    _check_count(issues, catalog.get("sourceCount"), len(repositories), "catalog_source_count_mismatch", "catalog sourceCount differs from the snapshot")
    _check_count(issues, catalog.get("projectCount"), len(projects), "catalog_project_count_mismatch", "catalog projectCount differs from project rows")
    _add_if(issues, len(project_repositories) != len(set(project_repositories)) or "" in project_repositories, "duplicate_catalog_repository", "catalog repositories must be non-empty and unique")
    _add_if(issues, len(project_slugs) != len(set(project_slugs)) or "" in project_slugs, "duplicate_catalog_slug", "catalog slugs must be non-empty and unique")
    _add_if(issues, not set(project_repositories).issubset(set(repository_names)), "catalog_repository_missing_from_snapshot", "catalog contains a repository absent from the snapshot")
    repository_by_name = {
        str(item.get("repo")): item
        for item in repositories
        if isinstance(item, dict) and item.get("repo")
    }
    project_star_mismatches = sum(
        1
        for project in projects
        if isinstance(project, dict)
        and (
            not (source := repository_by_name.get(str(project.get("repo") or "")))
            or _integer(project.get("stars")) is None
            or _integer(source.get("stars")) is None
            or _integer(project.get("stars")) != _integer(source.get("stars"))
        )
    )
    _add_if(
        issues,
        project_star_mismatches > 0,
        "catalog_star_mismatch",
        f"{project_star_mismatches} catalog star values differ from the current snapshot",
    )

    track_metadata_present = "dailyTrackCounts" in catalog or any(
        isinstance(item, dict) and "heatTrack" in item for item in projects
    )
    if track_metadata_present:
        invalid_track_rows = sum(
            1
            for item in projects
            if not isinstance(item, dict)
            or item.get("heatTrack") not in {"recent_momentum", "long_term"}
            or not isinstance(item.get("heatLabel"), str)
            or not item.get("heatLabel")
            or _integer(item.get("momentumScore")) not in range(101)
            or _integer(item.get("enduranceScore")) not in range(101)
        )
        _add_if(issues, invalid_track_rows > 0, "invalid_heat_track", f"{invalid_track_rows} projects have invalid heat-track metadata")
        daily = [item for item in projects[:5] if isinstance(item, dict)]
        actual_long_term = sum(item.get("heatTrack") == "long_term" for item in daily)
        actual_recent_momentum = sum(item.get("heatTrack") == "recent_momentum" for item in daily)
        declared_tracks = catalog.get("dailyTrackCounts")
        track_counts_match = (
            isinstance(declared_tracks, dict)
            and _integer(declared_tracks.get("longTerm")) == actual_long_term
            and _integer(declared_tracks.get("recentMomentum")) == actual_recent_momentum
            and actual_long_term + actual_recent_momentum == len(daily)
        )
        _add_if(issues, not track_counts_match, "daily_track_count_mismatch", "dailyTrackCounts differs from the Daily Five")
        eligible_long_term = sum(
            item.get("heatTrack") == "long_term"
            and _integer(item.get("globalScore")) is not None
            and int(item["globalScore"]) >= 60
            and item.get("recommendation") != "观望"
            for item in projects
            if isinstance(item, dict)
        )
        available_recent_momentum = sum(
            item.get("heatTrack") == "recent_momentum"
            for item in projects
            if isinstance(item, dict)
        )
        _add_if(
            issues,
            len(daily) == 5
            and eligible_long_term >= 2
            and available_recent_momentum >= 3
            and (actual_long_term != 2 or actual_recent_momentum != 3),
            "daily_track_balance_mismatch",
            "Daily Five must reserve two long-term and three recent-momentum slots when available",
        )

    invalid_evidence_urls = 0
    for project in projects:
        if not isinstance(project, dict):
            continue
        for evidence in project.get("evidence") or []:
            href = str(evidence.get("href") or "") if isinstance(evidence, dict) else ""
            if not _is_http_url(href):
                invalid_evidence_urls += 1
    _add_if(issues, invalid_evidence_urls > 0, "unsafe_evidence_url", f"{invalid_evidence_urls} evidence URLs are not HTTP(S)")

    signal_ids = [str(item.get("id") or "") for item in signal_items if isinstance(item, dict)]
    signal_urls = [str(item.get("url") or "") for item in signal_items if isinstance(item, dict)]
    _check_count(issues, signals.get("signalCount"), len(signal_items), "signal_count_mismatch", "signalCount differs from signal rows")
    _add_if(issues, len(signal_ids) != len(set(signal_ids)) or "" in signal_ids, "duplicate_signal_id", "signal IDs must be non-empty and unique")
    _add_if(issues, len(signal_urls) != len(set(signal_urls)) or "" in signal_urls, "duplicate_signal_url", "signal URLs must be non-empty and unique")
    _add_if(issues, any(not _is_http_url(value) for value in signal_urls), "unsafe_signal_url", "signal URLs must use HTTP(S) with a host")
    if signals_at:
        window_hours = _integer(signals.get("windowHours"))
        _add_if(issues, window_hours is None or window_hours <= 0, "invalid_signal_window", "windowHours must be a positive integer")
        window = timedelta(hours=window_hours if window_hours and window_hours > 0 else 48)
        outside_window = sum(
            1
            for item in signal_items
            if not isinstance(item, dict)
            or not (published := _parse_time(item.get("publishedAt")))
            or published < signals_at - window
            or published > signals_at + timedelta(hours=2)
        )
        _add_if(issues, outside_window > 0, "signal_outside_window", f"{outside_window} signals fall outside the declared window")

    source_ids = [str(item.get("id") or "") for item in source_items if isinstance(item, dict)]
    source_urls = [str(item.get("url") or "") for item in source_items if isinstance(item, dict)]
    healthy_sources = sum(isinstance(item, dict) and item.get("state") == "healthy" for item in source_items)
    failed_sources = sum(isinstance(item, dict) and item.get("state") == "failed" for item in source_items)
    _check_count(issues, signals.get("healthySourceCount"), healthy_sources, "healthy_source_count_mismatch", "healthySourceCount is inconsistent")
    _check_count(issues, signals.get("failedSourceCount"), failed_sources, "failed_source_count_mismatch", "failedSourceCount is inconsistent")
    _add_if(issues, len(source_ids) != len(set(source_ids)) or "" in source_ids, "duplicate_source_id", "source IDs must be non-empty and unique")
    _add_if(issues, any(not _is_http_url(value) for value in source_urls), "unsafe_source_url", "source URLs must use HTTP(S) with a host")
    _add_if(issues, healthy_sources + failed_sources != len(source_items), "invalid_source_state", "source state must be healthy or failed")

    queue_ids = [str(item.get("id") or "") for item in queue_items if isinstance(item, dict)]
    project_pending = sum(isinstance(item, dict) and item.get("kind") == "project" for item in queue_items)
    signal_pending = sum(isinstance(item, dict) and item.get("kind") == "signal" for item in queue_items)
    _check_count(issues, queue.get("pendingCount"), len(queue_items), "queue_count_mismatch", "pendingCount differs from queue rows")
    _check_count(issues, queue.get("projectPendingCount"), project_pending, "project_queue_count_mismatch", "projectPendingCount is inconsistent")
    _check_count(issues, queue.get("signalPendingCount"), signal_pending, "signal_queue_count_mismatch", "signalPendingCount is inconsistent")
    _add_if(issues, len(queue_ids) != len(set(queue_ids)) or "" in queue_ids, "duplicate_queue_id", "queue IDs must be non-empty and unique")
    _add_if(issues, project_pending + signal_pending != len(queue_items), "invalid_queue_kind", "queue kind must be project or signal")

    previous_at = _parse_time(catalog.get("previousCapturedAt"))
    history_matches = 0
    previous_snapshot: dict[str, Any] | None = None
    history_dir = data_dir / "snapshots" / "history"
    if previous_at and history_dir.exists():
        for path in history_dir.glob("*.json"):
            payload = _load(path, issues)
            if _parse_time(payload.get("captured_at")) == previous_at:
                history_matches += 1
                previous_snapshot = payload
    _add_if(issues, bool(previous_at and history_matches != 1), "missing_previous_snapshot", "catalog previousCapturedAt must match exactly one history snapshot")
    observed_count = sum(isinstance(item, dict) and item.get("growthKind") == "observed" for item in projects)
    observed_values = [
        int(item["growthValue"])
        for item in projects
        if isinstance(item, dict)
        and item.get("growthKind") == "observed"
        and _integer(item.get("growthValue")) is not None
    ]
    growth_kind_mismatches = 0
    growth_value_mismatches = 0
    if previous_snapshot:
        previous_by_name = {
            str(item.get("repo")): item
            for item in previous_snapshot.get("repositories", [])
            if isinstance(item, dict) and item.get("repo")
        }
        for project in projects:
            if not isinstance(project, dict):
                continue
            repository = str(project.get("repo") or "")
            current_source = repository_by_name.get(repository)
            previous_source = previous_by_name.get(repository)
            if not previous_source:
                growth_kind_mismatches += project.get("growthKind") == "observed"
                continue
            if project.get("growthKind") != "observed":
                growth_kind_mismatches += 1
                continue
            current_stars = _integer((current_source or {}).get("stars"))
            previous_stars = _integer(previous_source.get("stars"))
            if current_stars is None or previous_stars is None:
                growth_value_mismatches += 1
                continue
            growth_value_mismatches += _integer(project.get("growthValue")) != current_stars - previous_stars
    elif observed_count:
        growth_kind_mismatches = observed_count
    _add_if(
        issues,
        growth_kind_mismatches > 0,
        "growth_kind_mismatch",
        f"{growth_kind_mismatches} projects use a growth kind inconsistent with snapshot history",
    )
    _add_if(
        issues,
        growth_value_mismatches > 0,
        "observed_growth_mismatch",
        f"{growth_value_mismatches} observed growth values differ from the exact star delta",
    )
    _add_if(
        issues,
        bool(previous_at and observed_count == 0),
        "no_observed_growth_after_history",
        "a prior snapshot exists but no catalog project has observed growth",
        severity="warning",
    )

    error_count = sum(item["severity"] == "error" for item in issues)
    warning_count = sum(item["severity"] == "warning" for item in issues)
    return {
        "schemaVersion": 1,
        "status": "failed" if error_count else "degraded" if warning_count else "healthy",
        "dataDir": str(data_dir),
        "snapshotCapturedAt": snapshot.get("captured_at"),
        "catalogCapturedAt": catalog.get("capturedAt"),
        "previousSnapshotCapturedAt": catalog.get("previousCapturedAt"),
        "repositoryCount": len(repositories),
        "projectCount": len(projects),
        "growthMode": catalog.get("growthMode"),
        "observedProjectCount": observed_count,
        "positiveGrowthProjectCount": sum(value > 0 for value in observed_values),
        "flatGrowthProjectCount": sum(value == 0 for value in observed_values),
        "negativeGrowthProjectCount": sum(value < 0 for value in observed_values),
        "observedNetStarChange": sum(observed_values),
        "dailyTrackCounts": catalog.get("dailyTrackCounts"),
        "signalCount": len(signal_items),
        "healthySourceCount": healthy_sources,
        "failedSourceCount": failed_sources,
        "queuePendingCount": len(queue_items),
        "historyCount": len(list(history_dir.glob("*.json"))) if history_dir.exists() else 0,
        "errorCount": error_count,
        "warningCount": warning_count,
        "issues": issues,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit committed Rardar data without modifying it")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    arguments = parser.parse_args()
    result = audit_data(arguments.data_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if result["status"] == "failed" else 0)


if __name__ == "__main__":
    main()
