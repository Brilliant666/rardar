"""Turn GitHub fact snapshots into a bounded, explainable Rardar catalog.

The first observation cannot prove 24-hour star growth. In that case the
catalog uses a clearly-labelled stars-per-age-day proxy. Once a previous
snapshot exists, the catalog reports the observed delta and its exact window.
No repository code is executed by this module.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


DISPLAY_ZONE = ZoneInfo("Asia/Shanghai")


PRODUCTIVITY_TERMS = {
    "agent",
    "automation",
    "cli",
    "code",
    "developer",
    "github",
    "llm",
    "mcp",
    "productivity",
    "research",
    "sdk",
    "self-hosted",
    "tool",
    "video",
    "workflow",
    "人工智能",
    "公众号",
    "工作流",
    "开发",
    "生产力",
    "自动化",
    "视频",
}

LOW_ACTIONABILITY_TERMS = {
    "awesome",
    "books",
    "cheatsheet",
    "curriculum",
    "fundamentals",
    "interview",
    "roadmap",
}

RISK_TERMS = {
    "bypass safety",
    "disable safety",
    "disable all content filters",
    "exploit",
    "fuckclaude",
    "jailbreak",
    "offensive-security",
    "redeem only",
    "spoof",
    "unrestricted mode",
    "关闭所有内容过滤器",
    "关闭内容过滤",
    "无限制模式",
}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _enrichment_is_current(enrichment: dict[str, Any] | None, pushed_at: str | None) -> bool:
    if not enrichment:
        return False
    analyzed_at = _parse_time(str(enrichment.get("analyzedAt") or ""))
    source_pushed_at = _parse_time(pushed_at)
    return bool(analyzed_at and (not source_pushed_at or analyzed_at >= source_pushed_at))


def _analysis_is_current(analysis: dict[str, Any] | None, pushed_at: str | None) -> bool:
    if not analysis:
        return False
    analyzed_at = _parse_time(str(analysis.get("analyzed_at") or ""))
    source_pushed_at = _parse_time(pushed_at)
    return bool(analyzed_at and (not source_pushed_at or analyzed_at >= source_pushed_at))


def _clamp(value: float, minimum: int = 0, maximum: int = 100) -> int:
    return round(max(minimum, min(maximum, value)))


def _safe_http_url(value: object, fallback: str) -> str:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned and len(cleaned) <= 2_048 and not any(ord(character) < 32 for character in cleaned):
            parsed = urllib.parse.urlsplit(cleaned)
            if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
                return cleaned
    return fallback


def _text(repository: dict[str, Any]) -> str:
    return " ".join(
        [
            str(repository.get("repo") or ""),
            str(repository.get("description") or ""),
            " ".join(str(item) for item in repository.get("topics") or []),
        ]
    ).lower()


def _matched_terms(repository: dict[str, Any]) -> list[str]:
    content = _text(repository)
    return sorted(term for term in PRODUCTIVITY_TERMS if term in content)


def _category(repository: dict[str, Any]) -> str:
    content = _text(repository)
    if any(term in content for term in ("video", "视频", "douyin", "youtube", "公众号")):
        return "视频与内容"
    if any(term in content for term in ("agent", "mcp", "llm", "artificial-intelligence", "人工智能")):
        return "AI 与 Agent"
    if any(term in content for term in ("research", "science", "evaluation", "研究")):
        return "研发工具"
    if any(term in content for term in ("automation", "workflow", "productivity", "自动化", "工作流", "生产力")):
        return "生产力"
    if any(term in content for term in ("developer", "sdk", "cli", "api", "code", "开发")):
        return "开发工具"
    if "self-hosted" in content or "selfhosted" in content:
        return "自托管"
    return "开源工具"


def _capabilities(repository: dict[str, Any], category: str) -> list[str]:
    topics = [str(item).replace("-", " ") for item in repository.get("topics") or []]
    values = [category, *topics[:4]]
    if repository.get("language"):
        values.append(f"{repository['language']} 项目")
    return list(dict.fromkeys(values))[:5]


def _task_terms(repository: dict[str, Any], category: str) -> list[str]:
    topics = [str(item).replace("-", " ") for item in repository.get("topics") or []]
    return list(dict.fromkeys([category, *_matched_terms(repository), *topics]))[:16]


def _previous_index(previous: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not previous:
        return {}
    return {
        item["repo"]: item
        for item in previous.get("repositories", [])
        if item.get("repo")
    }


def _growth(
    repository: dict[str, Any],
    captured_at: datetime,
    previous_repository: dict[str, Any] | None,
    previous_captured_at: datetime | None,
) -> dict[str, Any]:
    stars = int(repository.get("stars") or 0)
    created_at = _parse_time(repository.get("created_at")) or captured_at
    age_days = max((captured_at - created_at).total_seconds() / 86_400, 1.0)
    velocity = stars / age_days

    if previous_repository and previous_captured_at and captured_at > previous_captured_at:
        window_hours = (captured_at - previous_captured_at).total_seconds() / 3_600
        delta = stars - int(previous_repository.get("stars") or 0)
        normalized_delta = max(0, delta) * 24 / max(window_hours, 1)
        return {
            "kind": "observed",
            "value": delta,
            "label": f"观测 {delta:+d} / {window_hours:.1f} 小时",
            "trend": f"{delta:+d} / {window_hours:.1f}h",
            "ranking_value": normalized_delta,
            "age_days": age_days,
        }

    proxy = round(velocity)
    return {
        "kind": "velocity_proxy",
        "value": proxy,
        "label": f"创建以来约 {proxy} Star/日（首次观察代理）",
        "trend": f"约 {proxy}/日 · 代理值",
        "ranking_value": velocity,
        "age_days": age_days,
    }


def _score(
    repository: dict[str, Any],
    growth: dict[str, Any],
    captured_at: datetime,
    analysis: dict[str, Any] | None,
) -> tuple[int, int]:
    content = _text(repository)
    stars = int(repository.get("stars") or 0)
    age_days = float(growth["age_days"])
    pushed_at = _parse_time(repository.get("pushed_at"))
    push_age_days = (
        max(0.0, (captured_at - pushed_at).total_seconds() / 86_400)
        if pushed_at
        else 30.0
    )
    matched = _matched_terms(repository)
    query_count = len(str(repository.get("candidate_query") or "").split(" | "))

    growth_weight = 7 if growth["kind"] == "observed" else 4.5
    growth_score = min(44, math.log2(float(growth["ranking_value"]) + 1) * growth_weight)
    newness_score = max(0.0, 22 * (1 - age_days / 45))
    freshness_score = max(0.0, 12 * (1 - push_age_days / 14))
    relevance_score = min(18, len(matched) * 3)
    discovery_score = min(6, query_count * 1.5)
    established_score = min(8, math.log10(max(stars, 1)) * 1.8)
    low_actionability_penalty = 12 if any(term in content for term in LOW_ACTIONABILITY_TERMS) else 0
    risk_penalty = 26 if any(term in content for term in RISK_TERMS) else 0
    global_score = _clamp(
        16
        + growth_score
        + newness_score
        + freshness_score
        + relevance_score
        + discovery_score
        + established_score
        - low_actionability_penalty
        - risk_penalty
    )

    if analysis:
        indicators = analysis.get("indicators") or {}
        counts = analysis.get("counts") or {}
        completeness = 30
        completeness += 10 if indicators.get("readme") else 0
        completeness += 12 if indicators.get("license") else 0
        completeness += 14 if indicators.get("tests") else 0
        completeness += 9 if indicators.get("ci") else 0
        completeness += 7 if indicators.get("docs") else 0
        completeness += 6 if indicators.get("examples") else 0
        completeness += 5 if indicators.get("package_manifest") else 0
        completeness += 4 if indicators.get("dependency_lock") else 0
        completeness += min(4, math.log10(max(int(counts.get("test_files") or 0), 1)) * 2)
        completeness -= risk_penalty
        reuse_score = _clamp(completeness, maximum=96)
    else:
        # Metadata alone cannot prove implementation completeness. Keep its
        # reuse ceiling below a statically inspected repository.
        completeness = 24
        completeness += 10 if repository.get("description") else 0
        completeness += 10 if repository.get("license") not in (None, "NOASSERTION") else 0
        completeness += 8 if repository.get("language") else 0
        completeness += min(8, len(repository.get("topics") or []) * 2)
        completeness += 6 if push_age_days <= 7 else 2 if push_age_days <= 30 else 0
        completeness += min(4, math.log10(max(int(repository.get("forks") or 0), 1)) * 1.5)
        completeness -= risk_penalty
        reuse_score = _clamp(completeness, maximum=72)
    if risk_penalty:
        # Keep risky repositories visible for awareness, but never allow viral
        # growth or polished documentation to promote them into the Daily Five.
        global_score = min(global_score, 49)
        reuse_score = min(reuse_score, 35)
    if repository.get("license") in (None, "NOASSERTION"):
        detected_license = str((analysis or {}).get("license_hint") or "")
        recognized_license = detected_license in {"Apache-2.0", "MIT", "GPL", "BSD"}
        # A static text signature is useful evidence but not equivalent to the
        # repository API declaring a machine-readable license.
        reuse_score = min(reuse_score, 78 if recognized_license else 59)
    return global_score, reuse_score


def _recommendation(global_score: int, reuse_score: int, risk_detected: bool) -> str:
    if risk_detected:
        return "观望"
    if reuse_score >= 88 and global_score >= 78:
        return "复用"
    if reuse_score >= 78 and global_score >= 75:
        return "试用"
    if reuse_score >= 70:
        return "收藏"
    if global_score >= 78:
        return "了解"
    return "观望"


def _project(
    repository: dict[str, Any],
    captured_at: datetime,
    previous_repository: dict[str, Any] | None,
    previous_captured_at: datetime | None,
    analysis: dict[str, Any] | None,
    enrichment: dict[str, Any] | None,
) -> dict[str, Any]:
    repo = str(repository["repo"])
    growth = _growth(repository, captured_at, previous_repository, previous_captured_at)
    analysis_payload = analysis
    analysis_current = _analysis_is_current(analysis_payload, repository.get("pushed_at"))
    analysis = analysis_payload if analysis_current else None
    global_score, reuse_score = _score(repository, growth, captured_at, analysis)
    content = _text(repository)
    risk_detected = any(term in content for term in RISK_TERMS)
    category = _category(repository)
    captured_label = captured_at.astimezone(DISPLAY_ZONE).strftime("%Y-%m-%d %H:%M %Z")
    created_label = (_parse_time(repository.get("created_at")) or captured_at).date().isoformat()
    pushed_label = (_parse_time(repository.get("pushed_at")) or captured_at).date().isoformat()
    query_count = len(str(repository.get("candidate_query") or "").split(" | "))
    repository_url = _safe_http_url(repository.get("url"), f"https://github.com/{repo}")
    enrichment_current = _enrichment_is_current(enrichment, repository.get("pushed_at"))
    api_license = repository.get("license")
    detected_license = str((analysis or {}).get("license_hint") or "").strip()

    if growth["kind"] == "observed":
        why_now = f"Rardar 两次事实快照之间观测到 {growth['label']}，且仓库最近推送于 {pushed_label}。"
    else:
        why_now = (
            f"仓库创建于 {created_label}，当前有 {int(repository.get('stars') or 0):,} Star；"
            f"因尚无第二次快照，当前只把“{growth['label']}”作为起飞线索，不冒充 24 小时增量。"
        )

    analysis_state = "事实初筛"
    risk = (
        "已有静态报告缺少可核验时间，或早于仓库最近推送；本轮不将它作为当前实现证据，复用评分已受限制。"
        if analysis_payload and not analysis_current
        else "目前只完成 GitHub 元数据初筛，尚未浅克隆检查代码、测试和文档，复用评分上限已受限制。"
    )
    if analysis:
        analysis_state = "静态分析"
        indicators = analysis.get("indicators") or {}
        counts = analysis.get("counts") or {}
        missing = [
            label
            for key, label in (("license", "许可证"), ("tests", "测试"), ("ci", "CI"), ("docs", "文档"))
            if not indicators.get(key)
        ]
        risk = (
            f"已只读扫描 {int(analysis.get('scanned_files') or 0):,} 个文件，发现 "
            f"{int(counts.get('test_files') or 0)} 个测试文件。"
            + (f"仍未检测到：{'、'.join(missing)}。" if missing else "README、许可证、测试、CI 和文档证据较完整。")
            + "静态检查不能代替实际运行验证。"
        )
    if enrichment:
        analysis_state = "深度分析" if enrichment_current else "画像待复核"
    if risk_detected:
        risk = "仓库描述触发安全或滥用风险关键词，应先人工审查；当前不建议下载、运行或复用。"
    elif api_license in (None, "NOASSERTION"):
        if analysis_payload and not analysis_current:
            risk = "GitHub API 未返回标准许可证，现有静态报告也已过期；复用前必须重新检查授权范围。"
        elif detected_license:
            risk = (
                f"GitHub API 未返回标准许可证；只读静态扫描识别到“{detected_license}”文本线索，"
                "但这不能代替法律与文件范围核验。"
            )
        elif analysis:
            risk = "GitHub API 与只读静态扫描均未确认明确许可证；复用前必须核验授权范围。"
        else:
            risk = "GitHub API 未返回明确许可证，且尚未进行代码静态检查；复用前必须核验许可证和实现完整度。"

    title = repo.split("/", 1)[-1]
    description = repository.get("description") or "仓库尚未提供公开描述，需进入静态分析阶段后再判断具体能力。"
    capabilities = _capabilities(repository, category)
    task_terms = _task_terms(repository, category)
    fit = f"当前被归入“{category}”，命中 {len(_matched_terms(repository))} 个生产力相关信号；下一步需结合具体任务做能力匹配。"
    reuse_plan = "先阅读 README 和静态证据，再决定是否进入隔离环境试用。"
    if enrichment:
        title = str(enrichment.get("titleZh") or title)
        description = str(enrichment.get("summaryZh") or description)
        category = str(enrichment.get("category") or category)
        capabilities = list(enrichment.get("capabilities") or capabilities)[:6]
        task_terms = list(dict.fromkeys([*task_terms, *(enrichment.get("taskTerms") or [])]))[:32]
        fit = str(enrichment.get("bestFor") or fit)
        reuse_plan = str(enrichment.get("reusePlan") or reuse_plan)
        limitation = str(enrichment.get("limitation") or "").strip()
        if limitation:
            risk = f"{limitation} {risk}"
        if not enrichment_current:
            risk = "仓库最近推送晚于当前中文画像，能力与复用判断需要重新核对。 " + risk

    return {
        "slug": re.sub(r"[^a-z0-9-]+", "-", repo.lower().replace("/", "--")).strip("-"),
        "repo": repo,
        "title": title,
        "description": description,
        "category": category,
        "language": repository.get("language") or "未标注",
        "license": (
            str(api_license)
            if api_license not in (None, "NOASSERTION")
            else f"{detected_license}（静态线索）"
            if detected_license
            else "待核验"
        ),
        "stars": int(repository.get("stars") or 0),
        "growthValue": int(growth["value"]),
        "growthLabel": growth["label"],
        "growthKind": growth["kind"],
        "globalScore": global_score,
        "reuseScore": reuse_score,
        "trend": growth["trend"],
        "analysisState": analysis_state,
        "sourcePushedAt": repository.get("pushed_at"),
        "analysisAnalyzedAt": analysis_payload.get("analyzed_at") if analysis_payload else None,
        "enrichmentAnalyzedAt": enrichment.get("analyzedAt") if enrichment else None,
        "whyNow": why_now,
        "recommendation": _recommendation(global_score, reuse_score, risk_detected),
        "fit": fit,
        "reusePlan": reuse_plan,
        "risk": risk,
        "capabilities": capabilities,
        "taskTerms": task_terms,
        "evidence": [
            {
                "label": "GitHub API 事实快照",
                "detail": f"采集到 {int(repository.get('stars') or 0):,} Star、{int(repository.get('forks') or 0):,} Fork，最近推送 {pushed_label}。",
                "href": f"https://api.github.com/repos/{repo}",
            },
            {
                "label": "候选召回依据",
                "detail": f"仓库由 {query_count} 条 GitHub 搜索规则召回；当前增长字段类型为 {growth['kind']}。",
                "href": repository_url,
            },
            *(
                [
                    {
                        "label": "只读仓库静态检查",
                        "detail": (
                            f"扫描 {int(analysis.get('scanned_files') or 0):,} 个文件；"
                            f"测试文件 {int((analysis.get('counts') or {}).get('test_files') or 0)} 个；"
                            f"置信度 {int(analysis.get('confidence') or 0)}%。未执行仓库代码。"
                        ),
                        "href": repository_url,
                    }
                ]
                if analysis
                else []
            ),
            *(
                [
                    {
                        "label": "Codex 中文能力画像",
                        "detail": str(enrichment.get("evidenceSummary") or "依据仓库 README 与静态证据生成中文能力、适用任务和复用边界。"),
                        "href": _safe_http_url(enrichment.get("sourceUrl"), repository_url),
                    }
                ]
                if enrichment
                else []
            ),
        ],
        "capturedAt": captured_label,
    }


def build_catalog(
    snapshot: dict[str, Any],
    previous: dict[str, Any] | None = None,
    limit: int = 30,
    analyses: dict[str, dict[str, Any]] | None = None,
    enrichments: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    captured_at = _parse_time(snapshot.get("captured_at")) or datetime.now(timezone.utc)
    previous_captured_at = _parse_time(previous.get("captured_at")) if previous else None
    previous_repositories = _previous_index(previous)
    projects = [
        _project(
            repository,
            captured_at,
            previous_repositories.get(repository.get("repo")),
            previous_captured_at,
            (analyses or {}).get(repository.get("repo")),
            (enrichments or {}).get(repository.get("repo")),
        )
        for repository in snapshot.get("repositories", [])
        if repository.get("repo")
    ]
    projects.sort(key=lambda item: (item["globalScore"], item["reuseScore"], item["stars"]), reverse=True)
    bounded = projects[: max(5, min(limit, 100))]
    observed_count = sum(1 for item in bounded if item["growthKind"] == "observed")
    deep_analysis_count = sum(1 for item in bounded if item["analysisState"] == "深度分析")
    query_failure_count = int(snapshot.get("failed_query_count") or 0)
    pending_deep_analysis = [
        item["repo"] for item in bounded[:5] if item["analysisState"] != "深度分析"
    ]
    return {
        "schemaVersion": 1,
        "capturedAt": captured_at.isoformat(),
        "sourceCount": int(snapshot.get("count") or len(snapshot.get("repositories", []))),
        "queryFailureCount": query_failure_count,
        "projectCount": len(bounded),
        "deepAnalysisCount": deep_analysis_count,
        "pendingDeepAnalysis": pending_deep_analysis,
        "growthMode": (
            "observed"
            if observed_count == len(bounded)
            else "mixed_observation"
            if observed_count > 0
            else "first_observation_proxy"
        ),
        "notice": (
            f"本页来自 {captured_at.astimezone(DISPLAY_ZONE).strftime('%Y-%m-%d %H:%M %Z')} 的真实 GitHub API 快照，"
            f"从 {int(snapshot.get('count') or 0)} 个候选中选出 {len(bounded)} 个。"
            + (
                "增长来自两次快照的实际观测区间。"
                if observed_count == len(bounded)
                else (
                    f"其中 {observed_count} 个项目具有两次快照的实际观测增长；"
                    "本轮新进入的项目继续使用明确标注的创建以来速度代理。"
                    if observed_count > 0
                    else "这是首次观察：页面明确使用创建以来速度代理，不将其表述为 24 小时新增。"
                )
            )
            + (
                f"注意：本轮有 {query_failure_count} 条 GitHub 搜索规则失败，候选覆盖不完整。"
                if query_failure_count
                else ""
            )
        ),
        "projects": bounded,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an explainable Rardar project catalog")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--analysis-dir", type=Path)
    parser.add_argument("--enrichment-dir", type=Path)
    arguments = parser.parse_args()

    snapshot = json.loads(arguments.snapshot.read_text(encoding="utf-8"))
    previous = (
        json.loads(arguments.previous.read_text(encoding="utf-8"))
        if arguments.previous and arguments.previous.exists()
        else None
    )
    analyses: dict[str, dict[str, Any]] = {}
    if arguments.analysis_dir and arguments.analysis_dir.exists():
        for path in arguments.analysis_dir.glob("*.json"):
            analysis = json.loads(path.read_text(encoding="utf-8"))
            if analysis.get("repository"):
                analyses[analysis["repository"]] = analysis
    enrichments: dict[str, dict[str, Any]] = {}
    if arguments.enrichment_dir and arguments.enrichment_dir.exists():
        for path in arguments.enrichment_dir.glob("*.json"):
            enrichment = json.loads(path.read_text(encoding="utf-8"))
            if enrichment.get("repository"):
                enrichments[enrichment["repository"]] = enrichment
    catalog = build_catalog(snapshot, previous, arguments.limit, analyses, enrichments)
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"saved {catalog['projectCount']} ranked projects to {arguments.out}")


if __name__ == "__main__":
    main()
