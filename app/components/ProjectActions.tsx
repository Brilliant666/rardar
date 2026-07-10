"use client";

import { useEffect, useState } from "react";
import {
  getDeviceId,
  recordProjectAction,
  type ProjectActionValue,
} from "./device-id";

const actionOptions: Array<{ value: ProjectActionValue; label: string; detail: string }> = [
  { value: "saved", label: "已收藏", detail: "准备以后继续看" },
  { value: "tried", label: "已试用", detail: "实际运行或体验过" },
  { value: "cloned", label: "已浅克隆", detail: "已拉取代码做静态检查" },
  { value: "reused", label: "确认复用", detail: "已用于自己的任务或项目" },
];

export function TrackedRepositoryLink({ projectSlug, repository }: { projectSlug: string; repository: string }) {
  return (
    <a
      className="repo-name"
      href={`https://github.com/${repository}`}
      target="_blank"
      rel="noreferrer"
      onClick={() => void recordProjectAction(projectSlug, "opened").catch(() => undefined)}
    >
      {repository} ↗
    </a>
  );
}

export function ProjectActions({ projectSlug }: { projectSlug: string }) {
  const [selected, setSelected] = useState<Set<ProjectActionValue>>(new Set());
  const [message, setMessage] = useState("");

  useEffect(() => {
    const deviceId = getDeviceId();
    if (!deviceId) return;
    fetch(`/api/actions?deviceId=${encodeURIComponent(deviceId)}&projectSlug=${encodeURIComponent(projectSlug)}`)
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        const values = (payload?.actions ?? []).map((item: { action: ProjectActionValue }) => item.action);
        setSelected(new Set(values));
      })
      .catch(() => undefined);
  }, [projectSlug]);

  async function save(action: ProjectActionValue) {
    if (selected.has(action)) return;
    setMessage("记录中…");
    try {
      await recordProjectAction(projectSlug, action);
      setSelected((current) => new Set([...current, action]));
      setMessage("已记录为真实行动");
    } catch {
      setMessage("记录失败，请稍后重试");
    }
  }

  return (
    <div className="project-actions" aria-label="项目实际行动">
      <div className="project-actions-heading">
        <div><span>Outcome evidence</span><strong>你已经做到哪一步？</strong></div>
        <p>只有“试用 / 浅克隆 / 确认复用”计入北极星结果。</p>
      </div>
      <div className="project-action-options">
        {actionOptions.map((option) => (
          <button
            type="button"
            key={option.value}
            className={selected.has(option.value) ? "selected" : ""}
            aria-pressed={selected.has(option.value)}
            onClick={() => save(option.value)}
          >
            <strong>{option.label}</strong>
            <span>{option.detail}</span>
          </button>
        ))}
      </div>
      <small aria-live="polite">{message}</small>
    </div>
  );
}
