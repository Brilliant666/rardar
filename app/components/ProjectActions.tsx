"use client";

import { useEffect, useRef, useState } from "react";
import {
  createProjectActionIdempotencyKey,
  getDeviceId,
  isRetryableProjectActionError,
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
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState("");
  const successfulMutationVersion = useRef(0);
  const inFlightActions = useRef<Set<string>>(new Set());
  const retryKeys = useRef<Map<string, string>>(new Map());

  useEffect(() => {
    const deviceId = getDeviceId();
    if (!deviceId) return;
    const controller = new AbortController();
    const mutationVersionAtStart = successfulMutationVersion.current;

    fetch(`/api/actions?deviceId=${encodeURIComponent(deviceId)}&projectSlug=${encodeURIComponent(projectSlug)}`, {
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        if (controller.signal.aborted || successfulMutationVersion.current !== mutationVersionAtStart) return;
        const values = (payload?.actions ?? []).map((item: { action: ProjectActionValue }) => item.action);
        setSelected(new Set(values));
      })
      .catch(() => undefined);

    return () => controller.abort();
  }, [projectSlug]);

  async function save(action: ProjectActionValue) {
    const requestProjectSlug = projectSlug;
    const attemptKey = `${requestProjectSlug}:${action}`;
    if (inFlightActions.current.has(attemptKey)) return;
    const wasSelected = selected.has(action);
    const idempotencyKey = retryKeys.current.get(attemptKey) ?? createProjectActionIdempotencyKey();
    retryKeys.current.set(attemptKey, idempotencyKey);
    inFlightActions.current.add(attemptKey);
    setPending((current) => new Set([...current, attemptKey]));
    setMessage("记录中…");
    try {
      const result = await recordProjectAction(requestProjectSlug, action, idempotencyKey);
      retryKeys.current.delete(attemptKey);
      successfulMutationVersion.current += 1;
      setSelected((current) => new Set([...current, action]));
      setMessage(
        result.recorded
          ? (wasSelected ? "已再次记录为本次真实行动" : "已记录为真实行动")
          : "重复请求已安全确认，没有重复计数",
      );
    } catch (error) {
      if (!isRetryableProjectActionError(error)) retryKeys.current.delete(attemptKey);
      setMessage("记录失败，请稍后重试");
    } finally {
      inFlightActions.current.delete(attemptKey);
      setPending((current) => {
        const next = new Set(current);
        next.delete(attemptKey);
        return next;
      });
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
            disabled={pending.has(`${projectSlug}:${option.value}`)}
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
