"""Collect a small, attributable technical-signal brief for Rardar.

The collector prefers public feeds and generated JSON over page scraping. It
stores source links and collection health, and treats third-party rankings as
candidate signals rather than ground truth.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


OFFICIAL_FEEDS = [
    ("openai_news", "OpenAI News", "https://openai.com/news/rss.xml"),
    ("github_changelog", "GitHub Changelog", "https://github.blog/changelog/feed/"),
    ("huggingface_blog", "Hugging Face Blog", "https://huggingface.co/blog/feed.xml"),
]
AI_RADAR_URL = "https://raw.githubusercontent.com/LearnPrompt/ai-news-radar/master/data/daily-brief.json"
DAILY_RANK_URL = "https://raw.githubusercontent.com/OpenGithubs/github-daily-rank/main/README.md"
HELLOGITHUB_RELEASE_URL = "https://api.github.com/repos/521xueweihan/HelloGitHub/releases/latest"

AI_TERMS = {
    "agent",
    "ai",
    "claude",
    "codex",
    "copilot",
    "developer",
    "github",
    "gpt",
    "hugging face",
    "llm",
    "model",
    "open source",
    "人工智能",
    "大模型",
    "开源",
    "智能体",
}

PRODUCT_CHANGE_TERMS = {
    "api",
    "available",
    "changelog",
    "copilot",
    "developer",
    "launch",
    "model",
    "now",
    "open source",
    "release",
    "update",
}

MARKETING_STORY_TERMS = {"case study", "customer", "how ", "rewiring", "with ai"}
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_AGGREGATED_SCORE = 0.96


class HttpClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token

    def get(self, url: str, accept: str = "*/*") -> bytes:
        headers = {
            "accept": accept,
            "user-agent": "rardar-signal-collector/0.1",
        }
        if self.token and urllib.parse.urlparse(url).hostname == "api.github.com":
            headers["authorization"] = f"Bearer {self.token}"
            headers["x-github-api-version"] = "2022-11-28"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError(f"source response exceeds {MAX_RESPONSE_BYTES} bytes")
        return payload


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(cleaned)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(node: ET.Element, names: set[str]) -> str | None:
    for child in node:
        if _local_name(child.tag) in names and child.text:
            return child.text.strip()
    return None


def _feed_link(node: ET.Element) -> str | None:
    for child in node:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        relation = child.attrib.get("rel", "alternate")
        if href and relation in {"alternate", ""}:
            return href.strip()
        if child.text and child.text.strip().startswith("http"):
            return child.text.strip()
    return None


def _clean_title(value: str | None) -> str:
    title = re.sub(r"\s+", " ", html.unescape(value or "")).strip()
    if " / " in title:
        parts = [item.strip() for item in title.split(" / ") if item.strip()]
        if parts and "�" in parts[0] and "�" not in parts[-1]:
            return parts[-1]
    return title.replace("�", "").strip()


def _canonical_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    filtered = [(key, item) for key, item in query if not key.lower().startswith("utm_")]
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urllib.parse.urlencode(filtered), ""))


def _safe_http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 2_048 or any(ord(character) < 32 for character in cleaned):
        return None
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    return cleaned


def _signal_id(kind: str, url: str) -> str:
    digest = hashlib.sha1(f"{kind}:{_canonical_url(url)}".encode("utf-8")).hexdigest()[:12]
    return f"signal_{digest}"


def _recency_score(published_at: datetime, now: datetime, window_hours: int) -> float:
    age_hours = max(0.0, (now - published_at).total_seconds() / 3_600)
    return max(0.0, 1 - age_hours / max(window_hours, 1))


def _relevance(title: str) -> float:
    normalized = title.lower()
    ai_matches = sum(1 for term in AI_TERMS if term in normalized)
    product_matches = sum(1 for term in PRODUCT_CHANGE_TERMS if term in normalized)
    marketing_matches = sum(1 for term in MARKETING_STORY_TERMS if term in normalized)
    return max(0.0, min(1.0, 0.24 + ai_matches * 0.14 + product_matches * 0.1 - marketing_matches * 0.09))


def _aggregated_score(item: dict[str, Any]) -> float | None:
    values = (item.get("importance_score"), item.get("importance"), item.get("score"))
    raw_value = next((value for value in values if value is not None and value != ""), 0.5)
    if isinstance(raw_value, bool):
        return None
    try:
        score = float(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(score):
        return None
    return round(max(0.0, min(score, MAX_AGGREGATED_SCORE)), 4)


def _official_signals(client: HttpClient, now: datetime, window_hours: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    signals: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    cutoff = now - timedelta(hours=window_hours)
    for source_id, source_name, url in OFFICIAL_FEEDS:
        try:
            root = ET.fromstring(client.get(url, "application/rss+xml, application/atom+xml, application/xml, text/xml"))
            entries = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]
            accepted = 0
            latest: datetime | None = None
            for entry in entries[:30]:
                title = _clean_title(_child_text(entry, {"title"}))
                link = _safe_http_url(_feed_link(entry))
                published = _parse_datetime(_child_text(entry, {"pubdate", "published", "updated", "date"}))
                if not title or not link or not published or published < cutoff or published > now + timedelta(hours=2):
                    continue
                accepted += 1
                latest = max(latest, published) if latest else published
                score = 0.58 + 0.14 * _recency_score(published, now, window_hours) + 0.28 * _relevance(title)
                signals.append(
                    {
                        "id": _signal_id("official", link),
                        "kind": "official",
                        "title": title,
                        "summaryZh": f"{source_name} 发布官方更新。该条目直接来自官方订阅源，建议先阅读原文再判断是否影响当前项目。",
                        "url": link,
                        "source": source_name,
                        "sourceUrl": url,
                        "publishedAt": published.isoformat(),
                        "score": round(min(score, 0.99), 4),
                        "evidence": ["official_feed", "timestamped_source"],
                        "sources": [source_name],
                    }
                )
            statuses.append(
                {
                    "id": source_id,
                    "name": source_name,
                    "url": url,
                    "state": "healthy",
                    "itemCount": accepted,
                    "latestItemAt": latest.isoformat() if latest else None,
                    "error": None,
                }
            )
        except Exception as error:  # Network/source failures must not stop other feeds.
            statuses.append(
                {"id": source_id, "name": source_name, "url": url, "state": "failed", "itemCount": 0, "latestItemAt": None, "error": str(error)}
            )
    return signals, statuses


def _radar_signals(client: HttpClient, now: datetime, window_hours: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        payload = json.loads(client.get(AI_RADAR_URL, "application/json").decode("utf-8"))
        signals: list[dict[str, Any]] = []
        for item in payload.get("items", [])[:30]:
            published = _parse_datetime(item.get("latest_at") or item.get("earliest_at"))
            link = _safe_http_url(item.get("primary_url") or item.get("url"))
            title = _clean_title(item.get("title"))
            importance = _aggregated_score(item)
            if (
                not published
                or not link
                or not title
                or importance is None
                or published < now - timedelta(hours=window_hours)
                or published > now + timedelta(hours=2)
            ):
                continue
            source_names = [str(value) for value in item.get("source_names") or [item.get("source")] if value]
            signals.append(
                {
                    "id": _signal_id("aggregated", link),
                    "kind": "aggregated",
                    "title": title,
                    "summaryZh": f"AI News Radar 将其归入“{item.get('category') or '综合'}”信号，合并 {int(item.get('source_count') or 1)} 个来源；Rardar 保留原文入口并独立展示来源。",
                    "url": link,
                    "source": "AI News Radar",
                    "sourceUrl": AI_RADAR_URL,
                    "publishedAt": published.isoformat(),
                    "score": importance,
                    "evidence": list(item.get("reasons") or ["curated_aggregator"]),
                    "sources": source_names or ["AI News Radar"],
                }
            )
        latest = max((_parse_datetime(item["publishedAt"]) for item in signals), default=None)
        return signals, {
            "id": "ai_news_radar",
            "name": "AI News Radar",
            "url": AI_RADAR_URL,
            "state": "healthy",
            "itemCount": len(signals),
            "latestItemAt": latest.isoformat() if latest else None,
            "error": None,
        }
    except Exception as error:
        return [], {"id": "ai_news_radar", "name": "AI News Radar", "url": AI_RADAR_URL, "state": "failed", "itemCount": 0, "latestItemAt": None, "error": str(error)}


def _daily_rank_signals(client: HttpClient, now: datetime, window_hours: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        content = client.get(DAILY_RANK_URL, "text/plain").decode("utf-8", errors="replace")
        # The repository mixes Markdown and generated HTML, but its first
        # dated heading consistently represents the current ranking day.
        date_match = re.search(r"##\s+(\d{4}\.\d{2}\.\d{2})", content)
        published = datetime.strptime(date_match.group(1), "%Y.%m.%d").replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc) if date_match else now
        row_pattern = re.compile(r"\|\s*(\d+)\s*\|\s*\[([^\]]+)\]\((https://github\.com/[^)]+)\)\|\s*([^|]+)\|\s*([^|]+)\|")
        signals: list[dict[str, Any]] = []
        rows = (
            row_pattern.findall(content)[:10]
            if now - timedelta(hours=window_hours) <= published <= now + timedelta(hours=2)
            else []
        )
        for rank_text, repository, link, total_stars, daily_growth in rows:
            rank = int(rank_text)
            growth_text = re.sub(r"[^0-9]", "", daily_growth)
            growth = int(growth_text) if growth_text else 0
            signals.append(
                {
                    "id": _signal_id("ranking", link),
                    "kind": "ranking",
                    "title": repository,
                    "summaryZh": f"OpenGithubs 日榜第 {rank} 名，来源报告日增 {growth:,} Star、总量 {total_stars.strip()}。该数值只作为外部候选信号，Rardar 会用自己的快照验证增长。",
                    "url": link,
                    "source": "OpenGithubs Daily Rank",
                    "sourceUrl": DAILY_RANK_URL,
                    "publishedAt": published.isoformat(),
                    "score": round(max(0.5, 0.82 - (rank - 1) * 0.025), 4),
                    "evidence": ["external_daily_rank", "requires_independent_verification"],
                    "sources": ["OpenGithubs Daily Rank"],
                    "repo": repository,
                    "reportedDailyGrowth": growth,
                }
            )
        return signals, {
            "id": "open_githubs_daily_rank",
            "name": "OpenGithubs Daily Rank",
            "url": DAILY_RANK_URL,
            "state": "healthy",
            "itemCount": len(signals),
            "latestItemAt": published.isoformat(),
            "error": None,
        }
    except Exception as error:
        return [], {"id": "open_githubs_daily_rank", "name": "OpenGithubs Daily Rank", "url": DAILY_RANK_URL, "state": "failed", "itemCount": 0, "latestItemAt": None, "error": str(error)}


def _hellogithub_signal(client: HttpClient, now: datetime, window_hours: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        release = json.loads(client.get(HELLOGITHUB_RELEASE_URL, "application/vnd.github+json").decode("utf-8"))
        published = _parse_datetime(release.get("published_at"))
        link = _safe_http_url(release.get("html_url"))
        if not published or not link:
            raise ValueError("latest release has no timestamp or URL")
        recent = now - timedelta(hours=window_hours) <= published <= now + timedelta(hours=2)
        signals = []
        if recent:
            tag = str(release.get("tag_name") or release.get("name") or "最新一期")
            signals.append(
                {
                    "id": _signal_id("curated", link),
                    "kind": "curated",
                    "title": f"HelloGitHub {tag} 发布",
                    "summaryZh": "HelloGitHub 发布新的人工精选月刊。Rardar 只记录发布事实和原始入口，不复制其受 CC BY-NC-ND 限制的项目介绍。",
                    "url": link,
                    "source": "HelloGitHub",
                    "sourceUrl": HELLOGITHUB_RELEASE_URL,
                    "publishedAt": published.isoformat(),
                    "score": 0.68,
                    "evidence": ["human_curated_release", "content_not_copied"],
                    "sources": ["HelloGitHub"],
                }
            )
        return signals, {
            "id": "hellogithub",
            "name": "HelloGitHub",
            "url": "https://github.com/521xueweihan/HelloGitHub/releases",
            "state": "healthy",
            "itemCount": len(signals),
            "latestItemAt": published.isoformat(),
            "error": None,
        }
    except Exception as error:
        return [], {"id": "hellogithub", "name": "HelloGitHub", "url": HELLOGITHUB_RELEASE_URL, "state": "failed", "itemCount": 0, "latestItemAt": None, "error": str(error)}


def _merge_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    kind_priority = {"official": 4, "curated": 3, "ranking": 2, "aggregated": 1}
    for signal in signals:
        key = _canonical_url(signal["url"])
        if key not in merged:
            merged[key] = signal
            continue
        current = merged[key]
        current["sources"] = sorted(set(current.get("sources", []) + signal.get("sources", []) + [signal["source"]]))
        current["evidence"] = sorted(set(current.get("evidence", []) + signal.get("evidence", [])))
        current["score"] = max(float(current["score"]), float(signal["score"]))
        if kind_priority.get(signal["kind"], 0) > kind_priority.get(current["kind"], 0):
            for field in ("kind", "title", "summaryZh", "source", "sourceUrl", "publishedAt", "id"):
                current[field] = signal[field]
    return sorted(
        merged.values(),
        key=lambda item: (
            kind_priority.get(str(item.get("kind")), 0),
            float(item["score"]),
            _parse_datetime(item["publishedAt"]) or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )


def collect_signals(client: HttpClient, now: datetime, window_hours: int = 48, limit: int = 30) -> dict[str, Any]:
    now = now.astimezone(timezone.utc)
    official, statuses = _official_signals(client, now, window_hours)
    radar, radar_status = _radar_signals(client, now, window_hours)
    ranking, ranking_status = _daily_rank_signals(client, now, window_hours)
    curated, curated_status = _hellogithub_signal(client, now, window_hours)
    statuses.extend([radar_status, ranking_status, curated_status])
    merged = _merge_signals([*official, *radar, *ranking, *curated])[: max(5, min(limit, 100))]
    return {
        "schemaVersion": 1,
        "capturedAt": now.isoformat(),
        "windowHours": window_hours,
        "signalCount": len(merged),
        "healthySourceCount": sum(1 for item in statuses if item["state"] == "healthy"),
        "failedSourceCount": sum(1 for item in statuses if item["state"] == "failed"),
        "sourceStatus": statuses,
        "topSignals": merged[:5],
        "signals": merged,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Rardar trusted technical signals")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--window-hours", type=int, default=48)
    parser.add_argument("--limit", type=int, default=30)
    arguments = parser.parse_args()
    payload = collect_signals(
        HttpClient(os.environ.get("GITHUB_TOKEN")),
        datetime.now(timezone.utc),
        max(24, min(arguments.window_hours, 168)),
        max(5, min(arguments.limit, 100)),
    )
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"saved {payload['signalCount']} signals from {payload['healthySourceCount']} healthy sources "
        f"({payload['failedSourceCount']} failed) to {arguments.out}"
    )


if __name__ == "__main__":
    main()
