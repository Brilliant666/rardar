"use client";

import { useRef, useState } from "react";
import { decisionStatusForProject } from "../decision-flow.mjs";
import { stableProjectSelector } from "../client-project-identity.mjs";
import {
  createProjectActionIdempotencyKey,
  isRetryableProjectActionError,
  recordProjectAction,
  type ProjectActionValue,
} from "./device-id";
import { useDecisionState } from "./DecisionStateProvider";

const actionOptions: Array<{ value: ProjectActionValue; label: string; detail: string }> = [
  { value: "tried", label: "已尝试", detail: "实际运行或体验过" },
  { value: "cloned", label: "已克隆", detail: "已拉取代码做静态检查" },
  { value: "reused", label: "已复用", detail: "已用于自己的任务或项目" },
];

type ProjectIdentityProps = { projectIdVersion: 1; projectId: string };

export function TrackedRepositoryLink({
  projectIdVersion,
  projectId,
  repository,
}: ProjectIdentityProps & { repository: string }) {
  const { generationId } = useDecisionState(projectId);
  const project = stableProjectSelector({ projectIdVersion, projectId });
  return (
    <a
      className="repo-name"
      href={`https://github.com/${repository}`}
      target="_blank"
      rel="noreferrer"
      onClick={() => void recordProjectAction(
        project,
        "opened",
        createProjectActionIdempotencyKey(),
        generationId,
      ).catch(() => undefined)}
    >
      打开 GitHub 仓库 ↗
    </a>
  );
}

export function ProjectActions({ projectIdVersion, projectId }: ProjectIdentityProps) {
  const {
    generationId,
    state,
    loading,
    error,
    staleGeneration,
    retry,
    reload,
  } = useDecisionState(projectId);
  const status = decisionStatusForProject(state);
  const selected = new Set(state.actions);
  const identityKey = `${generationId}:${projectIdVersion}:${projectId}`;
  const [pending, setPending] = useState<Set<string>>(new Set());
  const [messages, setMessages] = useState<Record<string, string>>({});
  const inFlightActions = useRef<Set<string>>(new Set());
  const retryKeys = useRef<Map<string, string>>(new Map());
  const message = messages[identityKey] ?? "";

  async function save(action: ProjectActionValue) {
    const requestProject = stableProjectSelector({ projectIdVersion, projectId });
    const attemptKey = `${generationId}:${requestProject.projectId}:${action}`;
    if (inFlightActions.current.has(attemptKey)) return;
    const wasSelected = selected.has(action);
    const idempotencyKey = retryKeys.current.get(attemptKey) ?? createProjectActionIdempotencyKey();
    retryKeys.current.set(attemptKey, idempotencyKey);
    inFlightActions.current.add(attemptKey);
    setPending((current) => new Set([...current, attemptKey]));
    setMessages((current) => ({ ...current, [identityKey]: "记录中…" }));
    try {
      const result = await recordProjectAction(
        requestProject,
        action,
        idempotencyKey,
        generationId,
      );
      retryKeys.current.delete(attemptKey);
      setMessages((current) => ({ ...current, [identityKey]: result.recorded
          ? (wasSelected ? "已再次记录为本次真实行动" : "已记录为真实行动")
          : "重复请求已安全确认，没有重复计数",
      }));
    } catch (caught) {
      if (!isRetryableProjectActionError(caught)) retryKeys.current.delete(attemptKey);
      setMessages((current) => ({ ...current, [identityKey]: "记录失败，请稍后重试" }));
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
    <div className="project-actions" aria-label="项目实际行动" aria-busy={loading}>
      <div className="project-actions-heading">
        <div><span>Next action</span><strong>选择真实发生的下一步</strong></div>
        <p>打开、尝试、克隆、复用彼此独立；只有后三项计入北极星结果。</p>
      </div>
      <ol className="project-action-progress" aria-label="当前行动进度">
        <li className={selected.has("opened") ? "complete" : ""}>打开</li>
        <li className={selected.has("tried") ? "complete" : ""}>尝试</li>
        <li className={selected.has("cloned") ? "complete" : ""}>克隆</li>
        <li className={selected.has("reused") ? "complete" : ""}>复用</li>
      </ol>
      <div className="project-action-options">
        {actionOptions.map((option) => (
          <button
            type="button"
            key={option.value}
            className={selected.has(option.value) ? "selected" : ""}
            aria-pressed={selected.has(option.value)}
            disabled={loading || Boolean(error) || pending.has(`${generationId}:${projectId}:${option.value}`)}
            onClick={() => save(option.value)}
          >
            <strong>{option.label}</strong>
            <span>{option.detail}</span>
          </button>
        ))}
      </div>
      <small aria-live="polite">
        {loading ? "正在读取行动状态…" : message || `当前：${status.stageLabel}`}
        {error ? <>
          <span>{error}</span>
          <button type="button" onClick={staleGeneration ? reload : retry}>
            {staleGeneration ? "刷新页面" : "重试"}
          </button>
        </> : null}
      </small>
    </div>
  );
}
