"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  canonicalProjectPath,
  collectWatchStatusesByProjectId,
} from "../client-project-identity.mjs";
import type { StableProject } from "../data";
import { getDeviceId } from "./device-id";

export function WatchlistClient({ projects }: { projects: StableProject[] }) {
  const [statusByProjectId, setStatusByProjectId] = useState<Map<string, string[]>>(new Map());
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
        setStatusByProjectId(collectWatchStatusesByProjectId(
          feedbackPayload.feedback,
          actionPayload.actions,
        ));
      })
      .catch(() => setStatusByProjectId(new Map()))
      .finally(() => setLoaded(true));
  }, []);

  const watched = projects.filter((project) => statusByProjectId.has(project.projectId));

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
        <Link key={project.projectId} href={canonicalProjectPath(project)} className="watch-card">
          <span>{statusByProjectId.get(project.projectId)?.join(" · ")} · {project.category}</span>
          <strong>{project.repo}</strong>
          <p>{project.whyNow}</p>
        </Link>
      ))}
    </div>
  );
}
