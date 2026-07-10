"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { projects } from "../data";
import { getDeviceId } from "./device-id";

export function WatchlistClient() {
  const [slugs, setSlugs] = useState<string[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const id = getDeviceId(false);
    const request = id
      ? fetch(`/api/feedback?deviceId=${encodeURIComponent(id)}`).then((response) => response.json())
      : Promise.resolve({ feedback: [] });

    request
      .then((payload) => {
        setSlugs((payload.feedback ?? []).filter((item: { value: string }) => item.value === "待确定").map((item: { projectSlug: string }) => item.projectSlug));
      })
      .finally(() => setLoaded(true));
  }, []);

  const watched = projects.filter((project) => slugs.includes(project.slug));

  if (!loaded) return <div className="empty-state">正在读取观察列表…</div>;
  if (!watched.length) {
    return (
      <div className="empty-state">
        <span>0</span>
        <h2>还没有待确定的项目</h2>
        <p>在任意项目卡片选择“待确定”，它会出现在这里，方便以后继续跟踪。</p>
        <Link className="primary-link" href="/discover">去发现项目</Link>
      </div>
    );
  }

  return (
    <div className="watch-grid">
      {watched.map((project) => (
        <Link key={project.slug} href={`/projects/${project.slug}`} className="watch-card">
          <span>{project.category}</span>
          <strong>{project.repo}</strong>
          <p>{project.whyNow}</p>
        </Link>
      ))}
    </div>
  );
}
