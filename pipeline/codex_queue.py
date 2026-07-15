"""Build the evidence-backed queue for local Codex enrichment work."""

from __future__ import annotations

import argparse
import json
import re
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.data_lock import data_dir_lock
from pipeline.project_identity import (
    PROJECT_ID_VERSION,
    ProjectIdentityError,
    canonicalize_repository,
    validate_project_identity,
)
from pipeline.schema_validation import (
    ArtifactKind,
    atomic_write_validated_json,
    infer_artifact_kind,
    load_validated_json,
    strict_json_loads,
)


PROJECT_REQUIRED_FIELDS = {
    "schemaVersion",
    "repository",
    "sourcePushedAt",
    "sourceAnalysisAt",
    "analyzedAt",
    "titleZh",
    "summaryZh",
    "category",
    "capabilities",
    "taskTerms",
    "bestFor",
    "reusePlan",
    "limitation",
    "evidenceSummary",
    "sourceUrl",
}
PROJECT_V2_REQUIRED_FIELDS = {
    *PROJECT_REQUIRED_FIELDS,
    "projectIdVersion",
    "projectId",
}
SIGNAL_CONTENT_FIELDS = {"titleZh", "takeawayZh", "whyItMattersZh", "categoryZh"}
SIGNAL_REQUIRED_FIELDS = {*SIGNAL_CONTENT_FIELDS, "analyzedAt", "sourcePublishedAt"}
DATA_PREFIX_PATTERN = re.compile(
    r"data(?:/generations/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?)?"
)


def _read_json(
    path: Path,
    kind: ArtifactKind | None = None,
    *,
    expected_repository: str | None = None,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if kind is not None:
        return load_validated_json(
            path,
            kind,
            expected_repository=expected_repository,
        )
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    if not isinstance(payload, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return payload


def _safe_name(repository: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", repository.lower().replace("/", "--")).strip("-")


def _is_complete(payload: dict[str, Any] | None, required: set[str]) -> bool:
    if not payload or not required.issubset(payload):
        return False
    return all(payload.get(field) not in (None, "", []) for field in required)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _repository_matches(
    artifact_repository: object,
    expected_repository: object,
    artifact_schema_version: int,
) -> bool:
    if artifact_schema_version != 2:
        return artifact_repository == expected_repository
    try:
        return canonicalize_repository(artifact_repository) == canonicalize_repository(
            expected_repository
        )
    except ProjectIdentityError:
        return False


def _project_enrichment_is_current(
    payload: dict[str, Any] | None,
    project: dict[str, Any],
    analysis: dict[str, Any] | None,
    artifact_schema_version: int = 1,
) -> bool:
    if (
        not _is_complete(
            payload,
            PROJECT_V2_REQUIRED_FIELDS
            if artifact_schema_version == 2
            else PROJECT_REQUIRED_FIELDS,
        )
        or payload.get("schemaVersion") != artifact_schema_version
        or not _repository_matches(
            payload.get("repository"),
            project.get("repo"),
            artifact_schema_version,
        )
        or not analysis
        or analysis.get("schemaVersion") != artifact_schema_version
        or not _repository_matches(
            analysis.get("repository"),
            project.get("repo"),
            artifact_schema_version,
        )
    ):
        return False
    pushed_at = project.get("sourcePushedAt")
    analysis_at = analysis.get("analyzed_at")
    source_pushed_time = _parse_time(payload.get("sourcePushedAt"))
    source_analysis_time = _parse_time(payload.get("sourceAnalysisAt"))
    enrichment_time = _parse_time(payload.get("analyzedAt"))
    return bool(
        isinstance(pushed_at, str)
        and isinstance(analysis_at, str)
        and payload.get("sourcePushedAt") == pushed_at
        and payload.get("sourceAnalysisAt") == analysis_at
        and source_pushed_time
        and source_analysis_time
        and enrichment_time
        and enrichment_time >= source_analysis_time
    )


def _project_analysis_is_current(
    payload: dict[str, Any] | None,
    project: dict[str, Any],
    artifact_schema_version: int = 1,
) -> bool:
    if (
        not payload
        or payload.get("schemaVersion") != artifact_schema_version
        or not _repository_matches(
            payload.get("repository"),
            project.get("repo"),
            artifact_schema_version,
        )
    ):
        return False
    analyzed_at = _parse_time(payload.get("analyzed_at"))
    pushed_at = _parse_time(project.get("sourcePushedAt"))
    return bool(analyzed_at and pushed_at and analyzed_at >= pushed_at)


def _signal_enrichment_is_current(
    payload: dict[str, Any] | None,
    signal: dict[str, Any],
    fallback_analyzed_at: object,
) -> bool:
    if not _is_complete(payload, SIGNAL_CONTENT_FIELDS):
        return False
    published_at = _parse_time(signal.get("publishedAt"))
    analyzed_at = _parse_time((payload or {}).get("analyzedAt") or fallback_analyzed_at)
    source_published_at = _parse_time((payload or {}).get("sourcePublishedAt"))
    if not published_at or not analyzed_at or analyzed_at < published_at:
        return False
    return not source_published_at or source_published_at == published_at


def build_codex_queue(
    catalog: dict[str, Any],
    signals: dict[str, Any],
    project_enrichment_dir: Path,
    signal_enrichment_path: Path,
    generated_at: datetime,
    project_limit: int = 5,
    signal_limit: int = 10,
    input_data_prefix: str = "data",
) -> dict[str, Any]:
    if not DATA_PREFIX_PATTERN.fullmatch(input_data_prefix):
        raise ValueError("Codex queue input data prefix must identify data or one immutable generation")
    items: list[dict[str, Any]] = []
    completed_projects = 0
    completed_signals = 0
    catalog_version = catalog.get("schemaVersion")
    if catalog_version in {None, 1, 2}:
        queue_version = 1
        artifact_schema_version = 1
    elif catalog_version == 3 and catalog.get("projectIdVersion") == PROJECT_ID_VERSION:
        queue_version = 2
        artifact_schema_version = 2
    else:
        raise ValueError(
            f"unsupported Catalog identity version for Codex queue: {catalog_version!r}"
        )

    for index, project in enumerate(catalog.get("projects", [])[: max(0, project_limit)]):
        repository = str(project.get("repo") or "").strip()
        if not repository:
            continue
        if queue_version == 2:
            identity = validate_project_identity(
                repository,
                project.get("projectId"),
                project.get("projectIdVersion"),
            )
            safe_name = identity.project_id
            required_fields = PROJECT_V2_REQUIRED_FIELDS
        else:
            safe_name = _safe_name(repository)
            required_fields = PROJECT_REQUIRED_FIELDS
        enrichment_path = project_enrichment_dir / f"{safe_name}.json"
        analysis_path = project_enrichment_dir.parent / "analysis" / f"{safe_name}.json"
        enrichment = _read_json(
            enrichment_path,
            ArtifactKind.PROJECT_ENRICHMENT,
            expected_repository=repository,
        )
        analysis = _read_json(
            analysis_path,
            ArtifactKind.STATIC_EVIDENCE,
            expected_repository=repository,
        )
        analysis_ready = _project_analysis_is_current(
            analysis,
            project,
            artifact_schema_version,
        )
        if analysis_ready and _project_enrichment_is_current(
            enrichment,
            project,
            analysis,
            artifact_schema_version,
        ):
            completed_projects += 1
            continue
        complete_but_stale = _is_complete(enrichment, required_fields)
        items.append(
            {
                "id": f"project:{safe_name}",
                "kind": "project",
                "priority": 100 - index * 4,
                **(
                    {
                        "projectIdVersion": PROJECT_ID_VERSION,
                        "projectId": safe_name,
                    }
                    if queue_version == 2
                    else {}
                ),
                "repository": repository,
                "title": project.get("title") or repository,
                "reason": (
                    "缺少与仓库最新推送对应的只读静态分析证据，必须先完成浅克隆扫描"
                    if not analysis_ready
                    else
                    "仓库在上次中文画像之后有新推送，需要基于最新 README 与静态证据复核"
                    if complete_but_stale
                    else "进入高优先级项目区，但缺少完整中文能力画像"
                ),
                "evidenceState": "ready" if analysis_ready else "static_analysis_required",
                "sourcePushedAt": project.get("sourcePushedAt"),
                "sourceAnalysisAt": analysis.get("analyzed_at") if analysis else None,
                "previousAnalyzedAt": enrichment.get("analyzedAt") if enrichment else None,
                "inputPaths": [
                    *(
                        [f"{input_data_prefix}/analysis/{safe_name}.json"]
                        if analysis_ready
                        else []
                    ),
                    f"{input_data_prefix}/catalog/latest.json",
                ],
                "outputPath": f"data/enrichment/{safe_name}.json",
                "requiredFields": sorted(required_fields),
                "safety": (
                    "只阅读 README 与静态分析证据，不执行仓库代码；必须将本项队列中的 "
                    "sourcePushedAt 与 sourceAnalysisAt 原样复制到草稿，不能自行生成、推算或改写；"
                    if analysis_ready
                    else "先执行只读浅克隆静态扫描；扫描失败则保持待分析，不得仅凭仓库元数据生成画像；"
                    "重新扫描并重建队列、证据状态达到 ready 后，必须将新队列中的 sourcePushedAt 与 "
                    "sourceAnalysisAt 原样复制到草稿，不能自行生成、推算或改写；"
                )
                + "先写 data/ 外草稿，再经 pipeline.ingest_enrichment 发布；"
                + "outputPath 只是最终归属，不能直接覆盖。",
            }
        )
        if queue_version == 2:
            items[-1]["safety"] += (
                " projectIdVersion and projectId must also be copied verbatim "
                "from this queue item; never generate or rewrite them."
            )

    signal_enrichment = (
        _read_json(signal_enrichment_path, ArtifactKind.SIGNAL_ENRICHMENT) or {}
    )
    enriched_signals = signal_enrichment.get("items") or {}
    legacy_analyzed_at = signal_enrichment.get("generatedAt")
    ranked_signals = signals.get("signals") or signals.get("topSignals") or []
    for index, signal_item in enumerate(ranked_signals[: max(0, signal_limit)]):
        url = str(signal_item.get("url") or "").strip()
        if not url:
            continue
        enrichment = enriched_signals.get(url) if isinstance(enriched_signals, dict) else None
        if _signal_enrichment_is_current(enrichment, signal_item, legacy_analyzed_at):
            completed_signals += 1
            continue
        complete_but_stale = _is_complete(enrichment, SIGNAL_CONTENT_FIELDS)
        items.append(
            {
                "id": f"signal:{signal_item.get('id') or index}",
                "kind": "signal",
                "priority": 98 - index * 3,
                "title": signal_item.get("title") or url,
                "url": url,
                "source": signal_item.get("source"),
                "reason": (
                    "同一链接出现了更新的发布时间或事件版本，需要重新核对中文结论"
                    if complete_but_stale
                    else "进入高优先级技术动态区，但缺少中文事实摘要与影响判断"
                ),
                "sourcePublishedAt": signal_item.get("publishedAt"),
                "previousAnalyzedAt": (enrichment or {}).get("analyzedAt") or legacy_analyzed_at,
                "inputPaths": [f"{input_data_prefix}/signals/latest.json"],
                "outputPath": "data/signals/enrichment.json",
                "requiredFields": sorted(SIGNAL_REQUIRED_FIELDS),
                "safety": (
                    "保留原始链接与发布时间，明确区分来源事实和 Codex 判断；"
                    "先写 data/ 外草稿，再经 pipeline.ingest_enrichment 发布；"
                    "outputPath 只是最终归属，不能直接覆盖。"
                ),
            }
        )

    items.sort(key=lambda item: (-int(item["priority"]), str(item["id"])))
    project_pending = sum(item["kind"] == "project" for item in items)
    signal_pending = sum(item["kind"] == "signal" for item in items)
    queue = {
        "schemaVersion": queue_version,
        "generatedAt": generated_at.astimezone(timezone.utc).isoformat(),
        "scope": {"projectLimit": project_limit, "signalLimit": signal_limit},
        "pendingCount": len(items),
        "projectPendingCount": project_pending,
        "signalPendingCount": signal_pending,
        "completedProjectCount": completed_projects,
        "completedSignalCount": completed_signals,
        "items": items,
    }
    if queue_version == 2:
        queue = {"projectIdVersion": PROJECT_ID_VERSION, **queue}
    return queue


def _artifact_data_root(path: Path) -> Path | None:
    resolved = path.expanduser().resolve()
    kind = infer_artifact_kind(resolved)
    if kind is None:
        return None
    if (
        kind is ArtifactKind.GITHUB_SNAPSHOT
        and resolved.parent.name.lower() == "history"
    ):
        return resolved.parent.parent.parent
    return resolved.parent.parent


def _queue_lock_roots(
    catalog_path: Path,
    signals_path: Path,
    project_enrichment_dir: Path,
    signal_enrichment_path: Path,
    output_path: Path,
) -> list[Path]:
    roots = {
        root
        for path in (catalog_path, signals_path, signal_enrichment_path, output_path)
        if (root := _artifact_data_root(path)) is not None
    }
    enrichment_dir = project_enrichment_dir.expanduser().resolve()
    if enrichment_dir.name.lower() == "enrichment":
        roots.add(enrichment_dir.parent)
    if not roots:
        roots.add(output_path.expanduser().resolve().parent)
    return sorted(roots, key=lambda path: str(path).casefold())


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local Codex analysis queue")
    parser.add_argument("--catalog", type=Path, default=Path("data/catalog/latest.json"))
    parser.add_argument("--signals", type=Path, default=Path("data/signals/latest.json"))
    parser.add_argument("--project-enrichment-dir", type=Path, default=Path("data/enrichment"))
    parser.add_argument("--signal-enrichment", type=Path, default=Path("data/signals/enrichment.json"))
    parser.add_argument("--out", type=Path, default=Path("data/queues/codex.json"))
    parser.add_argument("--project-limit", type=int, default=5)
    parser.add_argument("--signal-limit", type=int, default=10)
    arguments = parser.parse_args()

    with ExitStack() as lock_stack:
        for data_root in _queue_lock_roots(
            arguments.catalog,
            arguments.signals,
            arguments.project_enrichment_dir,
            arguments.signal_enrichment,
            arguments.out,
        ):
            lock_stack.enter_context(data_dir_lock(data_root))

        catalog = _read_json(arguments.catalog, ArtifactKind.CATALOG)
        signals = _read_json(arguments.signals, ArtifactKind.TECHNICAL_SIGNALS)
        if not catalog or not signals:
            raise SystemExit("catalog and signals snapshots are required")
        queue = build_codex_queue(
            catalog,
            signals,
            arguments.project_enrichment_dir,
            arguments.signal_enrichment,
            datetime.now(timezone.utc),
            max(1, min(arguments.project_limit, 30)),
            max(1, min(arguments.signal_limit, 30)),
        )
        atomic_write_validated_json(arguments.out, ArtifactKind.CODEX_QUEUE, queue)
    print(json.dumps({key: queue[key] for key in ("pendingCount", "projectPendingCount", "signalPendingCount")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
