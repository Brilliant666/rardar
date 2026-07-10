"""Build the evidence-backed queue for local Codex enrichment work."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_REQUIRED_FIELDS = {
    "repository",
    "analyzedAt",
    "titleZh",
    "summaryZh",
    "capabilities",
    "taskTerms",
    "bestFor",
    "reusePlan",
    "limitation",
    "evidenceSummary",
    "sourceUrl",
}
SIGNAL_REQUIRED_FIELDS = {"titleZh", "takeawayZh", "whyItMattersZh", "categoryZh"}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


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
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _project_enrichment_is_current(payload: dict[str, Any] | None, project: dict[str, Any]) -> bool:
    if not _is_complete(payload, PROJECT_REQUIRED_FIELDS):
        return False
    pushed_at = _parse_time(project.get("sourcePushedAt"))
    analyzed_at = _parse_time(payload.get("analyzedAt") if payload else None)
    return bool(analyzed_at and (not pushed_at or analyzed_at >= pushed_at))


def build_codex_queue(
    catalog: dict[str, Any],
    signals: dict[str, Any],
    project_enrichment_dir: Path,
    signal_enrichment_path: Path,
    generated_at: datetime,
    project_limit: int = 5,
    signal_limit: int = 10,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    completed_projects = 0
    completed_signals = 0

    for index, project in enumerate(catalog.get("projects", [])[: max(0, project_limit)]):
        repository = str(project.get("repo") or "").strip()
        if not repository:
            continue
        safe_name = _safe_name(repository)
        enrichment_path = project_enrichment_dir / f"{safe_name}.json"
        enrichment = _read_json(enrichment_path)
        if _project_enrichment_is_current(enrichment, project):
            completed_projects += 1
            continue
        complete_but_stale = _is_complete(enrichment, PROJECT_REQUIRED_FIELDS)
        items.append(
            {
                "id": f"project:{safe_name}",
                "kind": "project",
                "priority": 100 - index * 4,
                "repository": repository,
                "title": project.get("title") or repository,
                "reason": (
                    "仓库在上次中文画像之后有新推送，需要基于最新 README 与静态证据复核"
                    if complete_but_stale
                    else "进入高优先级项目区，但缺少完整中文能力画像"
                ),
                "sourcePushedAt": project.get("sourcePushedAt"),
                "previousAnalyzedAt": enrichment.get("analyzedAt") if enrichment else None,
                "inputPaths": [
                    f"data/analysis/{safe_name}.json",
                    "data/catalog/latest.json",
                ],
                "outputPath": f"data/enrichment/{safe_name}.json",
                "requiredFields": sorted(PROJECT_REQUIRED_FIELDS),
                "safety": "只阅读 README 与静态分析证据，不执行仓库代码",
            }
        )

    signal_enrichment = _read_json(signal_enrichment_path) or {}
    enriched_signals = signal_enrichment.get("items") or {}
    ranked_signals = signals.get("signals") or signals.get("topSignals") or []
    for index, signal_item in enumerate(ranked_signals[: max(0, signal_limit)]):
        url = str(signal_item.get("url") or "").strip()
        if not url:
            continue
        enrichment = enriched_signals.get(url) if isinstance(enriched_signals, dict) else None
        if _is_complete(enrichment, SIGNAL_REQUIRED_FIELDS):
            completed_signals += 1
            continue
        items.append(
            {
                "id": f"signal:{signal_item.get('id') or index}",
                "kind": "signal",
                "priority": 98 - index * 3,
                "title": signal_item.get("title") or url,
                "url": url,
                "source": signal_item.get("source"),
                "reason": "进入高优先级技术动态区，但缺少中文事实摘要与影响判断",
                "inputPaths": ["data/signals/latest.json"],
                "outputPath": "data/signals/enrichment.json",
                "requiredFields": sorted(SIGNAL_REQUIRED_FIELDS),
                "safety": "保留原始链接与发布时间，明确区分来源事实和 Codex 判断",
            }
        )

    items.sort(key=lambda item: (-int(item["priority"]), str(item["id"])))
    project_pending = sum(item["kind"] == "project" for item in items)
    signal_pending = sum(item["kind"] == "signal" for item in items)
    return {
        "schemaVersion": 1,
        "generatedAt": generated_at.astimezone(timezone.utc).isoformat(),
        "scope": {"projectLimit": project_limit, "signalLimit": signal_limit},
        "pendingCount": len(items),
        "projectPendingCount": project_pending,
        "signalPendingCount": signal_pending,
        "completedProjectCount": completed_projects,
        "completedSignalCount": completed_signals,
        "items": items,
    }


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

    catalog = _read_json(arguments.catalog)
    signals = _read_json(arguments.signals)
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
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.out.with_suffix(arguments.out.suffix + ".tmp")
    temporary.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(arguments.out)
    print(json.dumps({key: queue[key] for key in ("pendingCount", "projectPendingCount", "signalPendingCount")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
