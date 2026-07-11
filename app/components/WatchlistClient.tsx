"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { Project } from "../data";
import { getDeviceId } from "./device-id";

export function WatchlistClient({ projects }: { projects: Project[] }) {
  const [statusBySlug, setStatusBySlug] = useState<Record<string, string[]>>({});
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const id = getDeviceId(false);
    const request = id
      ? Promise.all([
          fetch(`/api/feedback?deviceId=${encodeURIComponent(id)}`).then((response) =>
            response.ok ? response.json() : { feedback: [] },
          ),
          fetch(`/api/actions?deviceId=${encodeURIComponent(id)}`).then((response) =>
            response.ok ? response.json() : { actions: [] },
          ),
        ])
      : Promise.resolve([{ feedback: [] }, { actions: [] }]);

    request
      .then(([feedbackPayload, actionPayload]) => {
        const next: Record<string, string[]> = {};
        for (const item of feedbackPayload.feedback ?? []) {
          if (item.value === "待确定") next[item.projectSlug] = ["待确定"];
        }
        for (const item of actionPayload.actions ?? []) {
          if (item.action !== "saved") continue;
          next[item.projectSlug] = [...(next[item.projectSlug] ?? []), "已收藏"];
        }
        setStatusBySlug(next);
      })
      .catch(() => setStatusBySlug({}))
      .finally(() => setLoaded(true));
  }, []);

  const watched = projects.filter((project) => statusBySlug[project.slug]);

  if (!loaded) return <div className="empty-state">正在读取观察列表…</div>;
  if (!watched.length) {
    return (
      <div className="empty-state">
        <span>0</span>
        <h2>还没有收藏或待确定的项目</h2>
        <p>把项目标记为“待确定”或在详情页选择“已收藏”，它就会出现在这里，方便以后继续跟踪。</p>
        <Link className="primary-link" href="/discover">去发现项目</Link>
      </div>
    );
  }

  return (
    <div className="watch-grid">
      {watched.map((project) => (
        <Link key={project.slug} href={`/projects/${project.slug}`} className="watch-card">
          <span>{statusBySlug[project.slug].join(" · ")} · {project.category}</span>
          <strong>{project.repo}</strong>
          <p>{project.whyNow}</p>
        </Link>
      ))}
    </div>
  );
}
