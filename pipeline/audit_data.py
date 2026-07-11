"""Read-only consistency audit for Rardar's committed local data artifacts."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pipeline.build_catalog import (
    MAX_HEAT_OBSERVATIONS,
    MIN_PERSISTENCE_OBSERVATIONS,
    heat_observation_counts,
    persistence_is_verified,
)
from pipeline.codex_queue import build_codex_queue
from pipeline.schema_validation import (
    ArtifactKind,
    ArtifactValidationError,
    strict_json_loads,
    validate_payload,
)


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


def _load(
    path: Path,
    issues: list[dict[str, str]],
    kind: ArtifactKind | None = None,
) -> dict[str, Any]:
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        issues.append({"severity": "error", "code": "missing_artifact", "detail": str(path)})
        return {}
    except (ValueError, OSError) as error:
        issues.append({"severity": "error", "code": "invalid_artifact", "detail": f"{path}: {error}"})
        return {}
    if not isinstance(payload, dict):
        issues.append({"severity": "error", "code": "invalid_artifact_shape", "detail": str(path)})
        return {}
    if kind is not None:
        result = validate_payload(kind, payload, source_path=path)
        for issue in result.issues[:50]:
            issues.append(
                {
                    "severity": "error",
                    "code": "schema_validation_failed",
                    "detail": f"{path} {issue.instance_path}: {issue.message}",
                }
            )
        if len(result.issues) > 50:
            issues.append(
                {
                    "severity": "error",
                    "code": "schema_validation_failed",
                    "detail": f"{path}: {len(result.issues) - 50} additional Schema errors",
                }
            )
        # Schema-invalid objects are untrusted input.  Do not pass their
        # containers or field types into the semantic audit below: that can
        # both hide the original contract failure and turn a read-only audit
        # into an exception instead of a deterministic failed report.
        if not result.valid:
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
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(hostname)


def _is_valid_signal_score(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= 1
        and math.isfinite(value)
    )


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
    snapshot = _load(
        data_dir / "snapshots" / "latest.json",
        issues,
        ArtifactKind.GITHUB_SNAPSHOT,
    )
    catalog = _load(data_dir / "catalog" / "latest.json", issues, ArtifactKind.CATALOG)
    signals = _load(
        data_dir / "signals" / "latest.json",
        issues,
        ArtifactKind.TECHNICAL_SIGNALS,
    )
    queue = _load(data_dir / "queues" / "codex.json", issues, ArtifactKind.CODEX_QUEUE)
    history_dir = data_dir / "snapshots" / "history"
    history_paths = sorted(history_dir.glob("*.json")) if history_dir.exists() else []
    history_snapshots = [
        _load(path, issues, ArtifactKind.GITHUB_SNAPSHOT) for path in history_paths
    ]
    analysis_dir = data_dir / "analysis"
    if analysis_dir.exists():
        for path in sorted(analysis_dir.glob("*.json")):
            _load(path, issues, ArtifactKind.STATIC_EVIDENCE)
    enrichment_dir = data_dir / "enrichment"
    if enrichment_dir.exists():
        for path in sorted(enrichment_dir.glob("*.json")):
            _load(path, issues, ArtifactKind.PROJECT_ENRICHMENT)
    signal_enrichment_path = data_dir / "signals" / "enrichment.json"
    if signal_enrichment_path.exists():
        _load(signal_enrichment_path, issues, ArtifactKind.SIGNAL_ENRICHMENT)

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
    analysis_failure_value = catalog.get("analysisFailures")
    analysis_failures = analysis_failure_value if isinstance(analysis_failure_value, list) else []
    if "analysisFailures" in catalog:
        _add_if(
            issues,
            not isinstance(analysis_failure_value, list),
            "invalid_analysis_failure_rows",
            "analysisFailures must be a list",
        )
        invalid_analysis_failures = sum(
            not isinstance(item, dict)
            or not isinstance(item.get("repository"), str)
            or not item.get("repository")
            or not isinstance(item.get("error"), str)
            or not item.get("error")
            for item in analysis_failures
        )
        _add_if(
            issues,
            invalid_analysis_failures > 0,
            "invalid_analysis_failure_row",
            f"{invalid_analysis_failures} analysis failure rows are invalid",
        )
        _add_if(
            issues,
            len(analysis_failures) > 0,
            "partial_static_analysis_failure",
            f"read-only static analysis failed for {len(analysis_failures)} repositories",
            severity="warning",
        )
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

    successful_query_count: int | None = None
    failed_query_count: int | None = None
    if "query_status" in snapshot:
        query_value = snapshot.get("queries")
        query_status_value = snapshot.get("query_status")
        queries = query_value if isinstance(query_value, list) else []
        query_status = query_status_value if isinstance(query_status_value, list) else []
        _add_if(issues, not isinstance(query_value, list), "invalid_query_rows", "snapshot queries must be a list")
        _add_if(issues, not isinstance(query_status_value, list), "invalid_query_status_rows", "snapshot query_status must be a list")
        _add_if(
            issues,
            not queries
            or any(not isinstance(item, str) or not item for item in queries)
            or len(queries) != len(set(item for item in queries if isinstance(item, str))),
            "invalid_query_definition",
            "GitHub candidate queries must be non-empty and unique",
        )
        _add_if(
            issues,
            any(not isinstance(item, dict) for item in query_status),
            "invalid_query_status_row",
            "GitHub query status rows must be objects",
        )
        status_rows = [item for item in query_status if isinstance(item, dict)]
        status_queries = [str(item.get("query") or "") for item in status_rows]
        _add_if(
            issues,
            len(status_queries) != len(queries)
            or len(status_queries) != len(set(status_queries))
            or set(status_queries) != set(item for item in queries if isinstance(item, str)),
            "query_status_coverage_mismatch",
            "query_status must contain exactly one row for each candidate query",
        )
        invalid_query_states = sum(item.get("state") not in {"healthy", "failed"} for item in status_rows)
        _add_if(
            issues,
            invalid_query_states > 0,
            "invalid_query_state",
            f"{invalid_query_states} GitHub query rows have an invalid state",
        )
        invalid_query_counts = sum(
            _integer(item.get("item_count")) is None or int(item["item_count"]) < 0
            for item in status_rows
        )
        _add_if(
            issues,
            invalid_query_counts > 0,
            "invalid_query_item_count",
            f"{invalid_query_counts} GitHub query rows have an invalid item_count",
        )
        invalid_query_errors = sum(
            (item.get("state") == "healthy" and item.get("error") is not None)
            or (
                item.get("state") == "failed"
                and (not isinstance(item.get("error"), str) or not str(item.get("error")).strip())
            )
            for item in status_rows
        )
        _add_if(
            issues,
            invalid_query_errors > 0,
            "invalid_query_error",
            f"{invalid_query_errors} GitHub query rows have inconsistent error evidence",
        )
        successful_query_count = sum(item.get("state") == "healthy" for item in status_rows)
        failed_query_count = sum(item.get("state") == "failed" for item in status_rows)
        _add_if(
            issues,
            successful_query_count == 0,
            "no_successful_query",
            "at least one GitHub candidate query must succeed",
        )
        _check_count(
            issues,
            snapshot.get("successful_query_count"),
            successful_query_count,
            "successful_query_count_mismatch",
            "successful_query_count differs from query_status",
        )
        _check_count(
            issues,
            snapshot.get("failed_query_count"),
            failed_query_count,
            "failed_query_count_mismatch",
            "failed_query_count differs from query_status",
        )
        _check_count(
            issues,
            catalog.get("queryFailureCount"),
            failed_query_count,
            "catalog_query_failure_count_mismatch",
            "catalog queryFailureCount differs from the GitHub snapshot",
        )
        _add_if(
            issues,
            failed_query_count > 0,
            "partial_query_failure",
            f"{failed_query_count} GitHub candidate queries failed; candidate coverage is incomplete",
            severity="warning",
        )
        query_set = {item for item in queries if isinstance(item, str)}
        unknown_candidate_queries = sum(
            1
            for repository in repositories
            if isinstance(repository, dict)
            and (
                not isinstance(repository.get("candidate_query"), str)
                or not repository.get("candidate_query")
                or not set(str(repository["candidate_query"]).split(" | ")).issubset(query_set)
            )
        )
        _add_if(
            issues,
            unknown_candidate_queries > 0,
            "unknown_candidate_query",
            f"{unknown_candidate_queries} repositories reference an undeclared candidate query",
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

    heat_history_present = "heatHistory" in catalog or any(
        isinstance(item, dict) and "heatObservationWindow" in item for item in projects
    )
    if heat_history_present:
        observation_window, observation_counts = heat_observation_counts(
            snapshot,
            history_snapshots,
        )
        declared_heat_history = catalog.get("heatHistory")
        verified_long_term_count = sum(
            isinstance(item, dict)
            and item.get("heatTrack") == "long_term"
            and persistence_is_verified(
                observation_counts.get(str(item.get("repo") or ""), 0),
                observation_window,
            )
            for item in projects
        )
        heat_history_matches = (
            isinstance(declared_heat_history, dict)
            and _integer(declared_heat_history.get("snapshotCount")) == observation_window
            and _integer(declared_heat_history.get("maximumSnapshotCount")) == MAX_HEAT_OBSERVATIONS
            and _integer(declared_heat_history.get("minimumPersistenceSnapshots"))
            == MIN_PERSISTENCE_OBSERVATIONS
            and _integer(declared_heat_history.get("verifiedLongTermCount"))
            == verified_long_term_count
        )
        _add_if(
            issues,
            not heat_history_matches,
            "heat_history_mismatch",
            "heatHistory differs from the retained snapshot evidence",
        )
        invalid_heat_observations = 0
        for item in projects:
            if not isinstance(item, dict):
                continue
            repository = str(item.get("repo") or "")
            observation_count = observation_counts.get(repository, 0)
            expected_kind = (
                "multi_snapshot"
                if item.get("heatTrack") == "long_term"
                and persistence_is_verified(observation_count, observation_window)
                else "structural_proxy"
                if item.get("heatTrack") == "long_term"
                else None
            )
            invalid_heat_observations += (
                _integer(item.get("heatObservationCount")) != observation_count
                or _integer(item.get("heatObservationWindow")) != observation_window
                or item.get("longTermEvidenceKind") != expected_kind
            )
        _add_if(
            issues,
            invalid_heat_observations > 0,
            "heat_observation_mismatch",
            f"{invalid_heat_observations} projects differ from the retained snapshot evidence",
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
    invalid_signal_scores = sum(
        not isinstance(item, dict) or not _is_valid_signal_score(item.get("score"))
        for item in signal_items
    )
    _add_if(
        issues,
        invalid_signal_scores > 0,
        "invalid_signal_score",
        f"{invalid_signal_scores} signal scores must be finite numbers between 0 and 1",
    )
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
    _add_if(
        issues,
        failed_sources > 0,
        "partial_signal_source_failure",
        f"{failed_sources} technical-signal sources failed",
        severity="warning",
    )

    queue_ids = [str(item.get("id") or "") for item in queue_items if isinstance(item, dict)]
    project_pending = sum(isinstance(item, dict) and item.get("kind") == "project" for item in queue_items)
    signal_pending = sum(isinstance(item, dict) and item.get("kind") == "signal" for item in queue_items)
    _check_count(issues, queue.get("pendingCount"), len(queue_items), "queue_count_mismatch", "pendingCount differs from queue rows")
    _check_count(issues, queue.get("projectPendingCount"), project_pending, "project_queue_count_mismatch", "projectPendingCount is inconsistent")
    _check_count(issues, queue.get("signalPendingCount"), signal_pending, "signal_queue_count_mismatch", "signalPendingCount is inconsistent")
    _add_if(issues, len(queue_ids) != len(set(queue_ids)) or "" in queue_ids, "duplicate_queue_id", "queue IDs must be non-empty and unique")
    _add_if(issues, project_pending + signal_pending != len(queue_items), "invalid_queue_kind", "queue kind must be project or signal")
    analysis_metadata_present = any(
        key in catalog for key in ("deepAnalysisCount", "pendingDeepAnalysis", "codexPendingCount")
    )
    if analysis_metadata_present:
        actual_deep_count = sum(
            isinstance(item, dict) and item.get("analysisState") == "深度分析"
            for item in projects
        )
        expected_pending_deep_analysis = [
            str(item.get("repo"))
            for item in projects[:5]
            if isinstance(item, dict) and item.get("analysisState") != "深度分析"
        ]
        _check_count(
            issues,
            catalog.get("deepAnalysisCount"),
            actual_deep_count,
            "deep_analysis_count_mismatch",
            "deepAnalysisCount differs from catalog project states",
        )
        _add_if(
            issues,
            catalog.get("pendingDeepAnalysis") != expected_pending_deep_analysis,
            "pending_deep_analysis_mismatch",
            "pendingDeepAnalysis differs from the current Daily Five",
        )
        _check_count(
            issues,
            catalog.get("codexPendingCount"),
            len(queue_items),
            "catalog_queue_count_mismatch",
            "catalog codexPendingCount differs from the Codex queue",
        )
    if isinstance(queue.get("scope"), dict) and queue_at:
        project_limit_value = _integer(queue["scope"].get("projectLimit"))
        signal_limit_value = _integer(queue["scope"].get("signalLimit"))
        project_limit = max(0, min(project_limit_value if project_limit_value is not None else 5, 30))
        signal_limit = max(0, min(signal_limit_value if signal_limit_value is not None else 10, 30))
        try:
            expected_queue = build_codex_queue(
                catalog,
                signals,
                data_dir / "enrichment",
                data_dir / "signals" / "enrichment.json",
                queue_at,
                project_limit,
                signal_limit,
            )
        except (ArtifactValidationError, ValueError) as error:
            issues.append(
                {
                    "severity": "error",
                    "code": "schema_validation_failed",
                    "detail": f"Codex queue inputs cannot be rebuilt: {error}",
                }
            )
            expected_queue = {
                "items": queue_items,
                "completedProjectCount": _integer(queue.get("completedProjectCount")) or 0,
                "completedSignalCount": _integer(queue.get("completedSignalCount")) or 0,
            }
        _add_if(
            issues,
            queue_items != expected_queue["items"],
            "stale_queue_items",
            "Codex queue items or evidence states differ from a read-only rebuild of current inputs",
        )
        _check_count(
            issues,
            queue.get("completedProjectCount"),
            int(expected_queue["completedProjectCount"]),
            "completed_project_count_mismatch",
            "completedProjectCount differs from current project enrichments",
        )
        _check_count(
            issues,
            queue.get("completedSignalCount"),
            int(expected_queue["completedSignalCount"]),
            "completed_signal_count_mismatch",
            "completedSignalCount differs from current signal enrichments",
        )
        expected_pending_repositories = {
            str(item.get("repository"))
            for item in expected_queue["items"]
            if item.get("kind") == "project" and item.get("repository")
        }
        daily_projects = [item for item in projects[:project_limit] if isinstance(item, dict)]
        deep_without_current_evidence = sum(
            item.get("analysisState") == "深度分析"
            and str(item.get("repo") or "") in expected_pending_repositories
            for item in daily_projects
        )
        current_evidence_not_applied = sum(
            item.get("analysisState") != "深度分析"
            and str(item.get("repo") or "") not in expected_pending_repositories
            for item in daily_projects
        )
        _add_if(
            issues,
            deep_without_current_evidence > 0,
            "deep_analysis_without_current_evidence",
            f"{deep_without_current_evidence} deep-analysis projects lack current static evidence or enrichment",
        )
        _add_if(
            issues,
            current_evidence_not_applied > 0,
            "current_enrichment_not_applied",
            f"{current_evidence_not_applied} projects have current evidence and enrichment but are not marked deep-analysis",
        )

    previous_at = _parse_time(catalog.get("previousCapturedAt"))
    history_matches = 0
    previous_snapshot: dict[str, Any] | None = None
    if previous_at:
        for payload in history_snapshots:
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
        "deepAnalysisCount": sum(
            isinstance(item, dict) and item.get("analysisState") == "深度分析"
            for item in projects
        ),
        "analysisFailureCount": len(analysis_failures),
        "growthMode": catalog.get("growthMode"),
        "observedProjectCount": observed_count,
        "positiveGrowthProjectCount": sum(value > 0 for value in observed_values),
        "flatGrowthProjectCount": sum(value == 0 for value in observed_values),
        "negativeGrowthProjectCount": sum(value < 0 for value in observed_values),
        "observedNetStarChange": sum(observed_values),
        "dailyTrackCounts": catalog.get("dailyTrackCounts"),
        "successfulQueryCount": successful_query_count,
        "failedQueryCount": failed_query_count,
        "signalCount": len(signal_items),
        "healthySourceCount": healthy_sources,
        "failedSourceCount": failed_sources,
        "queuePendingCount": len(queue_items),
        "staticAnalysisRequiredCount": sum(
            isinstance(item, dict)
            and item.get("kind") == "project"
            and item.get("evidenceState") == "static_analysis_required"
            for item in queue_items
        ),
        "historyCount": len(history_paths),
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
