"""Collect a bounded GitHub candidate snapshot for Rardar.

This is a candidate generator, not the final ranking algorithm. It deliberately
uses several narrow searches so new repositories, maintained repositories and
productivity-related repositories can all enter the pool.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class GitHubClient:
    def __init__(self, token: str | None = None) -> None:
        self.token = token

    def search(self, query: str, per_page: int = 30) -> list[dict[str, Any]]:
        parameters = urllib.parse.urlencode(
            {"q": query, "sort": "stars", "order": "desc", "per_page": min(per_page, 100)}
        )
        request = urllib.request.Request(
            f"https://api.github.com/search/repositories?{parameters}",
            headers={
                "accept": "application/vnd.github+json",
                "user-agent": "rardar-candidate-collector/0.1",
                "x-github-api-version": "2022-11-28",
                **({"authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        return payload.get("items", [])


def candidate_queries(now: datetime, since_days: int = 14) -> list[str]:
    since = (now - timedelta(days=since_days)).date().isoformat()
    recent = (now - timedelta(days=7)).date().isoformat()
    return [
        f"created:>={since} stars:>=25 archived:false fork:false",
        f"pushed:>={recent} stars:>=500 archived:false fork:false",
        f"topic:productivity pushed:>={recent} stars:>=50 archived:false fork:false",
        f"topic:artificial-intelligence pushed:>={recent} stars:>=100 archived:false fork:false",
        f"topic:developer-tools pushed:>={recent} stars:>=100 archived:false fork:false",
        f"topic:self-hosted pushed:>={recent} stars:>=100 archived:false fork:false",
    ]


def normalize(item: dict[str, Any], captured_at: str, query: str) -> dict[str, Any]:
    license_info = item.get("license") or {}
    owner = item.get("owner") or {}
    return {
        "repo": item.get("full_name"),
        "url": item.get("html_url"),
        "description": item.get("description"),
        "owner": owner.get("login"),
        "language": item.get("language"),
        "license": license_info.get("spdx_id"),
        "topics": item.get("topics", []),
        "stars": item.get("stargazers_count", 0),
        "forks": item.get("forks_count", 0),
        "open_issues": item.get("open_issues_count", 0),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "pushed_at": item.get("pushed_at"),
        "default_branch": item.get("default_branch"),
        "captured_at": captured_at,
        "candidate_query": query,
        "analysis_state": "pending",
    }


def collect(client: GitHubClient, now: datetime, since_days: int = 14) -> dict[str, Any]:
    captured_at = now.astimezone(timezone.utc).isoformat()
    repositories: dict[str, dict[str, Any]] = {}
    queries = candidate_queries(now, since_days)

    for query in queries:
        for item in client.search(query):
            repo = item.get("full_name")
            if not repo:
                continue
            normalized = normalize(item, captured_at, query)
            if repo in repositories:
                previous = repositories[repo]
                previous["candidate_query"] = sorted(
                    set(str(previous["candidate_query"]).split(" | ") + [query])
                )
                previous["candidate_query"] = " | ".join(previous["candidate_query"])
            else:
                repositories[repo] = normalized

    ranked = sorted(
        repositories.values(),
        key=lambda item: (int(item["stars"]), item.get("pushed_at") or ""),
        reverse=True,
    )
    return {
        "schema_version": 1,
        "captured_at": captured_at,
        "queries": queries,
        "count": len(ranked),
        "repositories": ranked,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect a GitHub candidate snapshot")
    parser.add_argument("--since-days", type=int, default=14)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()

    snapshot = collect(
        GitHubClient(os.environ.get("GITHUB_TOKEN")),
        datetime.now(timezone.utc),
        since_days=max(1, min(arguments.since_days, 90)),
    )
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"saved {snapshot['count']} candidates to {arguments.out}")


if __name__ == "__main__":
    main()
