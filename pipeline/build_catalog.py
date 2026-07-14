"""Turn GitHub fact snapshots into a bounded, explainable Rardar catalog.

The first observation cannot prove 24-hour star growth. In that case the
catalog uses a clearly-labelled stars-per-age-day proxy. Once a previous
snapshot exists, the catalog reports the observed delta and its exact window.
No repository code is executed by this module.
"""

from __future__ import annotations

import argparse
import math
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pipeline.schema_validation import (
    ArtifactKind,
    artifact_write_lock,
    atomic_write_validated_json,
    load_validated_json,
)


DISPLAY_ZONE = ZoneInfo("Asia/Shanghai")
MAX_HEAT_OBSERVATIONS = 30
MIN_PERSISTENCE_OBSERVATIONS = 7
MIN_PERSISTENCE_RATIO = 0.7


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


def _enrichment_is_current(
    enrichment: dict[str, Any] | None,
    repository: str,
    pushed_at: object,
    analysis: dict[str, Any] | None,
) -> bool:
    if (
        not enrichment
        or enrichment.get("schemaVersion") != 1
        or enrichment.get("repository") != repository
        or not analysis
        or analysis.get("schemaVersion") != 1
        or analysis.get("repository") != repository
    ):
        return False
    source_pushed_at = enrichment.get("sourcePushedAt")
    source_analysis_at = enrichment.get("sourceAnalysisAt")
    analysis_at = analysis.get("analyzed_at")
    source_pushed_time = _parse_time(source_pushed_at)
    source_analysis_time = _parse_time(source_analysis_at)
    enrichment_time = _parse_time(enrichment.get("analyzedAt"))
    return bool(
        isinstance(pushed_at, str)
        and isinstance(analysis_at, str)
        and source_pushed_at == pushed_at
        and source_analysis_at == analysis_at
        and source_pushed_time
        and source_analysis_time
        and enrichment_time
        and enrichment_time >= source_analysis_time
    )


def _analysis_is_current(analysis: dict[str, Any] | None, pushed_at: str | None) -> bool:
    if not analysis or analysis.get("schemaVersion") != 1:
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
            parsed = None
            try:
                parsed = urllib.parse.urlsplit(cleaned)
                hostname = parsed.hostname
                _ = parsed.port
            except ValueError:
                hostname = None
            if (
                parsed is not None
                and parsed.scheme.lower() in {"http", "https"}
                and hostname
                and hostname.strip(".")
                and parsed.username is None
                and parsed.password is None
                and "\\" not in cleaned
                and not any(character.isspace() for character in cleaned)
            ):
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


def heat_observation_counts(
    snapshot: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
) -> tuple[int, dict[str, int]]:
    """Count candidate presence across a bounded, de-duplicated snapshot window."""
    by_capture: dict[str, dict[str, Any]] = {}
    for item in [*(history or []), snapshot]:
        if not isinstance(item, dict) or not isinstance(item.get("repositories"), list):
            continue
        captured_at = _parse_time(str(item.get("captured_at") or ""))
        if not captured_at:
            continue
        by_capture[captured_at.isoformat()] = item
    ordered = sorted(
        by_capture.values(),
        key=lambda item: _parse_time(str(item.get("captured_at") or ""))
        or datetime.min.replace(tzinfo=timezone.utc),
    )[-MAX_HEAT_OBSERVATIONS:]
    counts: dict[str, int] = {}
    for item in ordered:
        repositories = {
            str(repository.get("repo"))
            for repository in item.get("repositories", [])
            if isinstance(repository, dict) and repository.get("repo")
        }
        for repository in repositories:
            counts[repository] = counts.get(repository, 0) + 1
    return len(ordered), counts


def persistence_is_verified(observation_count: int, observation_window: int) -> bool:
    return (
        observation_window >= MIN_PERSISTENCE_OBSERVATIONS
        and observation_count >= 5
        and observation_count / max(observation_window, 1) >= MIN_PERSISTENCE_RATIO
    )


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
    observation_count: int,
    observation_window: int,
) -> tuple[int, int, int, int | None, bool, bool]:
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
    low_actionability_penalty = 12 if any(term in content for term in LOW_ACTIONABILITY_TERMS) else 0
    risk_penalty = 26 if any(term in content for term in RISK_TERMS) else 0
    momentum_score = _clamp(
        16
        + growth_score
        + newness_score
        + freshness_score
        + relevance_score
        + discovery_score
        - low_actionability_penalty
        - risk_penalty
    )
    star_depth_score = min(45, max(0.0, (math.log10(max(stars, 1)) - 3) * 22.5))
    longevity_score = min(20, max(0.0, math.log2(max(age_days / 30, 1)) * 5.2))
    maintenance_score = max(0.0, 15 * (1 - push_age_days / 120))
    ecosystem_score = min(10, math.log10(max(int(repository.get("forks") or 0), 1)) * 2.5)
    sustained_relevance_score = min(10, len(matched) * 2)
    long_term_eligible = age_days >= 180 and stars >= 5_000 and push_age_days <= 120
    persistence_verified = long_term_eligible and persistence_is_verified(
        observation_count,
        observation_window,
    )
    persistence_score = (
        min(10, 5 + observation_count / max(observation_window, 1) * 5)
        if persistence_verified
        else 0
    )
    endurance_score = _clamp(
        star_depth_score
        + longevity_score
        + maintenance_score
        + ecosystem_score
        + sustained_relevance_score
        + persistence_score
        - low_actionability_penalty
        - risk_penalty
    )
    attention_score = max(momentum_score, round(endurance_score * 0.92))

    if analysis:
        indicators = analysis.get("indicators") or {}
        counts = analysis.get("counts") or {}
        readiness = 0
        readiness += 15 if indicators.get("readme") else 0
        readiness += 15 if indicators.get("license") else 0
        readiness += 20 if indicators.get("tests") else 0
        readiness += 15 if indicators.get("ci") else 0
        readiness += 10 if indicators.get("docs") else 0
        readiness += 8 if indicators.get("examples") else 0
        readiness += 7 if indicators.get("package_manifest") else 0
        readiness += 5 if indicators.get("dependency_lock") else 0
        readiness += min(5, math.log10(max(int(counts.get("test_files") or 0), 1)) * 2.5)
        engineering_readiness: int | None = _clamp(readiness - risk_penalty)
    else:
        # Repository metadata cannot establish engineering readiness. Keep the
        # value unknown until a current read-only static inspection exists.
        engineering_readiness = None
    if risk_penalty:
        # Keep risky repositories visible for awareness, but never allow viral
        # growth or polished documentation to promote them into the Daily Five.
        attention_score = min(attention_score, 49)
        momentum_score = min(momentum_score, 49)
        endurance_score = min(endurance_score, 35)
        if engineering_readiness is not None:
            engineering_readiness = min(engineering_readiness, 35)
    return (
        attention_score,
        momentum_score,
        endurance_score,
        engineering_readiness,
        long_term_eligible,
        persistence_verified,
    )


def _recommendation(
    attention_score: int,
    engineering_readiness: int | None,
    api_license_confirmed: bool,
    risk_detected: bool,
) -> str:
    if risk_detected:
        return "观望"
    if (
        engineering_readiness is not None
        and engineering_readiness >= 75
        and attention_score >= 55
        and api_license_confirmed
    ):
        return "隔离试用"
    if engineering_readiness is not None and engineering_readiness >= 60:
        return "收藏"
    if attention_score >= 60 or engineering_readiness is not None:
        return "了解"
    return "观望"


def _score_explanation(
    score: int | None,
    summary: str,
    *,
    facts: list[str] | None = None,
    proxies: list[str] | None = None,
    limitations: list[str] | None = None,
    upgrade_conditions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "score": score,
        "summary": summary,
        "facts": list(dict.fromkeys(facts or [])),
        "proxies": list(dict.fromkeys(proxies or [])),
        "limitations": list(dict.fromkeys(limitations or [])),
        "upgradeConditions": list(dict.fromkeys(upgrade_conditions or [])),
    }


def _evidence_completeness(
    growth_kind: str,
    analysis: dict[str, Any] | None,
    enrichment: dict[str, Any] | None,
    persistence_verified: bool,
) -> int:
    # This is evidence coverage, not a quality or reuse verdict.
    return _clamp(
        25
        + (15 if growth_kind == "observed" else 5)
        + (30 if analysis else 0)
        + (20 if enrichment else 0)
        + (10 if persistence_verified else 0)
    )


def _project(
    repository: dict[str, Any],
    captured_at: datetime,
    previous_repository: dict[str, Any] | None,
    previous_captured_at: datetime | None,
    analysis: dict[str, Any] | None,
    enrichment: dict[str, Any] | None,
    observation_count: int,
    observation_window: int,
) -> dict[str, Any]:
    repo = str(repository["repo"])
    growth = _growth(repository, captured_at, previous_repository, previous_captured_at)
    analysis_payload = analysis
    analysis_current = _analysis_is_current(analysis_payload, repository.get("pushed_at"))
    analysis = analysis_payload if analysis_current else None
    (
        attention_score,
        momentum_score,
        endurance_score,
        engineering_readiness,
        long_term_eligible,
        persistence_verified,
    ) = _score(
        repository,
        growth,
        captured_at,
        analysis,
        observation_count,
        observation_window,
    )
    content = _text(repository)
    risk_detected = any(term in content for term in RISK_TERMS)
    category = _category(repository)
    captured_label = captured_at.astimezone(DISPLAY_ZONE).strftime("%Y-%m-%d %H:%M %Z")
    created_label = (_parse_time(repository.get("created_at")) or captured_at).date().isoformat()
    pushed_label = (_parse_time(repository.get("pushed_at")) or captured_at).date().isoformat()
    query_count = len(str(repository.get("candidate_query") or "").split(" | "))
    repository_url = _safe_http_url(repository.get("url"), f"https://github.com/{repo}")
    enrichment_payload = enrichment
    enrichment_current = _enrichment_is_current(
        enrichment_payload,
        repo,
        repository.get("pushed_at"),
        analysis,
    )
    enrichment = enrichment_payload if enrichment_current else None
    api_license = repository.get("license")
    detected_license = str((analysis or {}).get("license_hint") or "").strip()

    if growth["kind"] == "observed":
        why_now = f"Rardar 两次事实快照之间观测到 {growth['label']}，且仓库最近推送于 {pushed_label}。"
    else:
        why_now = (
            f"仓库创建于 {created_label}，当前有 {int(repository.get('stars') or 0):,} Star；"
            f"因尚无第二次快照，当前只把“{growth['label']}”作为起飞线索，不冒充 24 小时增量。"
        )
    heat_track = "long_term" if long_term_eligible else "recent_momentum"
    heat_label = (
        "长期高热 · 多周期验证"
        if long_term_eligible and persistence_verified
        else "长期高热 · 结构代理"
        if long_term_eligible
        else "近期动量 · 区间上升"
        if growth["kind"] == "observed" and growth["value"] > 0
        else "近期动量 · 区间持平"
        if growth["kind"] == "observed" and growth["value"] == 0
        else "近期动量 · 区间回落"
        if growth["kind"] == "observed"
        else "近期动量 · 首次代理"
    )
    if long_term_eligible:
        if persistence_verified:
            why_now += (
                f" 长期热度评分为 {endurance_score}/100；该仓库在最近 {observation_window} 次 Rardar "
                f"候选快照中出现 {observation_count} 次，已达到多周期持续性阈值。"
            )
        else:
            why_now += (
                f" 长期热度评分为 {endurance_score}/100，当前依据总 Star、仓库年龄、近期维护和 Fork 生态计算；"
                f"Rardar 已积累 {observation_window} 次快照，达到 {MIN_PERSISTENCE_OBSERVATIONS} 次后再判断多周期持续性。"
            )

    analysis_state = "事实初筛"
    risk = (
        "已有静态报告缺少可核验时间，或早于仓库最近推送；本轮不将它作为当前实现证据，工程就绪度保持未知。"
        if analysis_payload and not analysis_current
        else "目前只完成 GitHub 元数据初筛，尚未浅克隆检查代码、测试和文档，工程就绪度保持未知。"
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
        analysis_state = "深度分析"
    elif enrichment_payload:
        analysis_state = "画像待复核"
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
    fit_hypothesis = f"当前被归入“{category}”，命中 {len(_matched_terms(repository))} 个生产力相关信号；下一步需结合具体任务做能力匹配。"
    reuse_plan = "先阅读 README 和静态证据，再决定是否进入隔离环境试用。"
    if enrichment:
        title = str(enrichment.get("titleZh") or title)
        description = str(enrichment.get("summaryZh") or description)
        category = str(enrichment.get("category") or category)
        capabilities = list(enrichment.get("capabilities") or capabilities)[:6]
        task_terms = list(dict.fromkeys([*task_terms, *(enrichment.get("taskTerms") or [])]))[:32]
        fit_hypothesis = str(enrichment.get("bestFor") or fit_hypothesis)
        reuse_plan = str(enrichment.get("reusePlan") or reuse_plan)
        limitation = str(enrichment.get("limitation") or "").strip()
        if limitation:
            risk = f"{limitation} {risk}"
    if enrichment_payload and not enrichment:
        risk = (
            "现有中文画像缺少与仓库最新推送对应的只读静态证据，本轮不采用其能力与复用结论。 "
            if not analysis_current
            else "现有中文画像绑定的仓库推送或静态分析版本与当前证据不一致，能力与复用判断需要重新核对。 "
        ) + risk

    evidence_completeness = _evidence_completeness(
        str(growth["kind"]),
        analysis,
        enrichment,
        persistence_verified,
    )
    repository_facts = [
        f"GitHub API 快照记录 {int(repository.get('stars') or 0):,} Star、{int(repository.get('forks') or 0):,} Fork。",
        f"仓库最近推送日期为 {pushed_label}。",
    ]
    attention_facts = [*repository_facts]
    attention_proxies = [
        f"近期动量分为 {momentum_score}/100；持久热度按 92% 折算后与其取较高值。",
        f"命中 {len(_matched_terms(repository))} 个生产力相关词和 {query_count} 条候选查询。",
    ]
    attention_limitations = ["关注优先级只回答是否值得先看，不代表工程质量或任务适配。"]
    attention_upgrade_conditions: list[str] = []
    if growth["kind"] == "observed":
        attention_facts.append(f"两次事实快照之间观测到 {growth['label']}。")
    else:
        attention_proxies.append(str(growth["label"]))
        attention_limitations.append("尚无第二次快照，增长使用创建以来速度代理。")
        attention_upgrade_conditions.append("获得下一次事实快照后改用精确区间 Star 增量。")
    if risk_detected:
        attention_limitations.append("仓库文本触发安全或滥用风险关键词，关注分上限为 49。")
        attention_upgrade_conditions.append("先完成人工安全审查；默认雷达仍不会执行第三方代码。")

    endurance_facts = [*repository_facts, f"仓库创建日期为 {created_label}。"]
    endurance_proxies = ["总 Star、仓库年龄、近期维护和 Fork 生态用于估计持久热度。"]
    endurance_limitations = ["持久热度不等于代码质量或当前任务适配。"]
    endurance_upgrade_conditions: list[str] = []
    if persistence_verified:
        endurance_facts.append(
            f"最近 {observation_window} 次候选快照中出现 {observation_count} 次。"
        )
    else:
        endurance_limitations.append("尚未达到多周期持续性验证阈值，当前含结构代理。")
        endurance_upgrade_conditions.append(
            f"积累至少 {MIN_PERSISTENCE_OBSERVATIONS} 次快照并达到持续出现阈值。"
        )
    if risk_detected:
        endurance_limitations.append("仓库文本触发安全或滥用风险关键词，持久热度上限为 35。")

    if analysis:
        indicators = analysis.get("indicators") or {}
        counts = analysis.get("counts") or {}
        present = [
            label
            for key, label in (
                ("readme", "README"),
                ("license", "许可证文件"),
                ("tests", "测试"),
                ("ci", "CI"),
                ("docs", "文档"),
                ("examples", "示例"),
                ("package_manifest", "包清单"),
                ("dependency_lock", "依赖锁"),
            )
            if indicators.get(key)
        ]
        engineering_limitations = ["未安装依赖、未启动服务、未执行测试；本分数不是运行可靠性。"]
        if risk_detected:
            engineering_limitations.append("仓库文本触发安全或滥用风险关键词，工程就绪度上限为 35。")
        engineering_explanation = _score_explanation(
            engineering_readiness,
            f"{engineering_readiness}/100；仅依据当前只读静态检查衡量工程材料就绪度。",
            facts=[
                f"只读扫描 {int(analysis.get('scanned_files') or 0):,} 个文件，测试文件 {int(counts.get('test_files') or 0)} 个。",
                f"检测到：{'、'.join(present) if present else '未检测到主要工程材料'}。",
            ],
            proxies=["文件与目录存在性是工程就绪的静态代理。"],
            limitations=engineering_limitations,
            upgrade_conditions=["在隔离环境完成安装、测试与关键路径验收后记录独立运行证据。"],
        )
    else:
        engineering_explanation = _score_explanation(
            None,
            "未评分：没有与仓库最新推送匹配的只读静态检查。",
            limitations=["GitHub 元数据不能证明代码、测试、文档或依赖是否完整。"],
            upgrade_conditions=["对当前推送执行只读浅克隆静态检查。"],
        )

    reuse_fit_explanation = _score_explanation(
        None,
        "未评分：通用目录没有你的具体任务、约束和验收标准。",
        facts=(
            ["当前中文能力画像已绑定最新仓库推送与静态证据。"]
            if enrichment
            else []
        ),
        proxies=["页面中的适用场景仅为检索假设，不换算成复用匹配分。"],
        limitations=["没有任务上下文时，不能推断该项目适合直接复用。"],
        upgrade_conditions=["提供目标任务、必需能力、技术约束与验收标准，再做能力映射和隔离验证。"],
    )

    evidence_sources = ["GitHub API 事实快照"]
    evidence_limitations: list[str] = []
    evidence_upgrade_conditions: list[str] = []
    if growth["kind"] == "observed":
        evidence_sources.append("精确区间增长")
    else:
        evidence_limitations.append("缺少第二次快照，增长仍为速度代理。")
        evidence_upgrade_conditions.append("采集下一次事实快照。")
    if analysis:
        evidence_sources.append("当前只读静态检查")
    else:
        evidence_limitations.append("缺少当前只读静态检查。")
        evidence_upgrade_conditions.append("完成只读浅克隆静态检查。")
    if enrichment:
        evidence_sources.append("版本绑定的中文能力画像")
    else:
        evidence_limitations.append("缺少与当前静态证据绑定的中文能力画像。")
        evidence_upgrade_conditions.append("基于当前静态证据生成并发布中文能力画像。")
    if persistence_verified:
        evidence_sources.append("多周期持续性证据")
    else:
        evidence_limitations.append("缺少达到阈值的多周期持续性证据。")
        evidence_upgrade_conditions.append("积累满足持续性阈值的候选快照。")

    score_explanations = {
        "attention": _score_explanation(
            attention_score,
            f"{attention_score}/100；综合近期动量与长期关注价值，决定是否值得先看。",
            facts=attention_facts,
            proxies=attention_proxies,
            limitations=attention_limitations,
            upgrade_conditions=attention_upgrade_conditions,
        ),
        "endurance": _score_explanation(
            endurance_score,
            f"{endurance_score}/100；衡量长期高热与持续维护线索。",
            facts=endurance_facts,
            proxies=endurance_proxies,
            limitations=endurance_limitations,
            upgrade_conditions=endurance_upgrade_conditions,
        ),
        "engineeringReadiness": engineering_explanation,
        "reuseFit": reuse_fit_explanation,
        "evidenceCompleteness": _score_explanation(
            evidence_completeness,
            f"{evidence_completeness}/100；只表示证据覆盖范围，不表示项目质量。",
            facts=[f"当前覆盖：{'、'.join(evidence_sources)}。"],
            limitations=evidence_limitations,
            upgrade_conditions=evidence_upgrade_conditions,
        ),
    }

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
        "attentionScore": attention_score,
        "enduranceScore": endurance_score,
        "engineeringReadiness": engineering_readiness,
        "reuseFitScore": None,
        "evidenceCompleteness": evidence_completeness,
        "scoreExplanations": score_explanations,
        "heatTrack": heat_track,
        "heatLabel": heat_label,
        "longTermEvidenceKind": (
            "multi_snapshot"
            if persistence_verified
            else "structural_proxy"
            if long_term_eligible
            else None
        ),
        "heatObservationCount": observation_count,
        "heatObservationWindow": observation_window,
        "trend": growth["trend"],
        "analysisState": analysis_state,
        "sourcePushedAt": repository.get("pushed_at"),
        "analysisAnalyzedAt": analysis_payload.get("analyzed_at") if analysis_payload else None,
        "enrichmentAnalyzedAt": enrichment_payload.get("analyzedAt") if enrichment_payload else None,
        "whyNow": why_now,
        "recommendation": _recommendation(
            attention_score,
            engineering_readiness,
            api_license not in (None, "NOASSERTION", ""),
            risk_detected,
        ),
        "fitHypothesis": fit_hypothesis,
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
                        "label": "Rardar 多周期热度证据",
                        "detail": (
                            f"最近 {observation_window} 次候选快照中出现 {observation_count} 次；"
                            f"覆盖率 {observation_count / max(observation_window, 1):.0%}。"
                        ),
                        "href": repository_url,
                    }
                ]
                if long_term_eligible and persistence_verified
                else []
            ),
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


def _balanced_project_order(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(
        projects,
        key=lambda item: (
            item["attentionScore"],
            item["engineeringReadiness"]
            if item["engineeringReadiness"] is not None
            else -1,
            item["stars"],
        ),
        reverse=True,
    )
    long_term = sorted(
        (
            item
            for item in ranked
            if item["heatTrack"] == "long_term"
            and item["attentionScore"] >= 60
            and item["recommendation"] != "观望"
        ),
        key=lambda item: (
            item["enduranceScore"],
            item["attentionScore"],
            item["engineeringReadiness"]
            if item["engineeringReadiness"] is not None
            else -1,
            item["stars"],
        ),
        reverse=True,
    )[:2]
    recent_momentum = [item for item in ranked if item["heatTrack"] == "recent_momentum"][:3]
    selected_repositories = {item["repo"] for item in [*long_term, *recent_momentum]}
    if len(selected_repositories) < 5:
        for item in ranked:
            selected_repositories.add(item["repo"])
            if len(selected_repositories) == 5:
                break
    daily = [item for item in ranked if item["repo"] in selected_repositories]
    remaining = [item for item in ranked if item["repo"] not in selected_repositories]
    return [*daily, *remaining]


def build_catalog(
    snapshot: dict[str, Any],
    previous: dict[str, Any] | None = None,
    limit: int = 30,
    analyses: dict[str, dict[str, Any]] | None = None,
    enrichments: dict[str, dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    captured_at = _parse_time(snapshot.get("captured_at")) or datetime.now(timezone.utc)
    previous_captured_at = _parse_time(previous.get("captured_at")) if previous else None
    previous_repositories = _previous_index(previous)
    observation_window, observation_counts = heat_observation_counts(snapshot, history)
    projects = [
        _project(
            repository,
            captured_at,
            previous_repositories.get(repository.get("repo")),
            previous_captured_at,
            (analyses or {}).get(repository.get("repo")),
            (enrichments or {}).get(repository.get("repo")),
            observation_counts.get(str(repository.get("repo")), 0),
            observation_window,
        )
        for repository in snapshot.get("repositories", [])
        if repository.get("repo")
    ]
    bounded = _balanced_project_order(projects)[: max(5, min(limit, 100))]
    observed_count = sum(1 for item in bounded if item["growthKind"] == "observed")
    daily_count = min(5, len(bounded))
    daily_long_term_count = sum(1 for item in bounded[:5] if item["heatTrack"] == "long_term")
    verified_long_term_count = sum(
        1 for item in bounded if item.get("longTermEvidenceKind") == "multi_snapshot"
    )
    deep_analysis_count = sum(1 for item in bounded if item["analysisState"] == "深度分析")
    query_failure_count = int(snapshot.get("failed_query_count") or 0)
    pending_deep_analysis = [
        item["repo"] for item in bounded[:5] if item["analysisState"] != "深度分析"
    ]
    return {
        "schemaVersion": 2,
        "scoreModelVersion": "evidence-v2",
        "capturedAt": captured_at.isoformat(),
        "sourceCount": int(snapshot.get("count") or len(snapshot.get("repositories", []))),
        "queryFailureCount": query_failure_count,
        "projectCount": len(bounded),
        "deepAnalysisCount": deep_analysis_count,
        "pendingDeepAnalysis": pending_deep_analysis,
        "dailyTrackCounts": {
            "recentMomentum": daily_count - daily_long_term_count,
            "longTerm": daily_long_term_count,
        },
        "heatHistory": {
            "snapshotCount": observation_window,
            "maximumSnapshotCount": MAX_HEAT_OBSERVATIONS,
            "minimumPersistenceSnapshots": MIN_PERSISTENCE_OBSERVATIONS,
            "verifiedLongTermCount": verified_long_term_count,
        },
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
            + f"每日五项包含 {daily_long_term_count} 个长期高热项目，其余席位用于近期动量项目；"
            + (
                f"已有 {verified_long_term_count} 个长期项目通过至少 {MIN_PERSISTENCE_OBSERVATIONS} 次快照的多周期持续性验证。"
                if verified_long_term_count
                else f"长期高热当前先使用结构代理；已积累 {observation_window} 次快照，达到 {MIN_PERSISTENCE_OBSERVATIONS} 次后自动升级多周期验证，不冒充历史每日排名。"
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

    with artifact_write_lock(arguments.out):
        snapshot = load_validated_json(arguments.snapshot, ArtifactKind.GITHUB_SNAPSHOT)
        previous = (
            load_validated_json(arguments.previous, ArtifactKind.GITHUB_SNAPSHOT)
            if arguments.previous and arguments.previous.exists()
            else None
        )
        analyses: dict[str, dict[str, Any]] = {}
        if arguments.analysis_dir and arguments.analysis_dir.exists():
            for path in arguments.analysis_dir.glob("*.json"):
                analysis = load_validated_json(path, ArtifactKind.STATIC_EVIDENCE)
                if analysis.get("repository"):
                    analyses[analysis["repository"]] = analysis
        enrichments: dict[str, dict[str, Any]] = {}
        if arguments.enrichment_dir and arguments.enrichment_dir.exists():
            for path in arguments.enrichment_dir.glob("*.json"):
                enrichment = load_validated_json(path, ArtifactKind.PROJECT_ENRICHMENT)
                if enrichment.get("repository"):
                    enrichments[enrichment["repository"]] = enrichment
        catalog = build_catalog(snapshot, previous, arguments.limit, analyses, enrichments)
        atomic_write_validated_json(arguments.out, ArtifactKind.CATALOG, catalog)
    print(f"saved {catalog['projectCount']} ranked projects to {arguments.out}")


if __name__ == "__main__":
    main()
