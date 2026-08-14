"use client";

import { useRef, useState } from "react";
import { decisionStatusForProject } from "../decision-flow.mjs";
import { stableProjectSelector } from "../client-project-identity.mjs";
import {
  recordSharedProjectAction,
} from "./device-id";
import { useDecisionState } from "./DecisionStateProvider";

export function WatchButton({
  projectIdVersion,
  projectId,
}: {
  projectIdVersion: 1;
  projectId: string;
}) {
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
  const identityKey = `${generationId}:${projectIdVersion}:${projectId}`;
  const [pendingIdentity, setPendingIdentity] = useState<string | null>(null);
  const [messages, setMessages] = useState<Record<string, string>>({});
  const inFlight = useRef<Set<string>>(new Set());
  const pending = pendingIdentity === identityKey;
  const message = messages[identityKey] ?? "";

  async function watch() {
    if (status.watched || inFlight.current.has(identityKey)) return;
    const project = stableProjectSelector({ projectIdVersion, projectId });
    inFlight.current.add(identityKey);
    setPendingIdentity(identityKey);
    setMessages((current) => ({ ...current, [identityKey]: "关注中…" }));
    try {
      await recordSharedProjectAction(
        identityKey,
        project,
        "saved",
        generationId,
      );
      setMessages((current) => ({ ...current, [identityKey]: "已加入观察列表" }));
    } catch {
      setMessages((current) => ({ ...current, [identityKey]: "关注失败，请稍后重试" }));
    } finally {
      inFlight.current.delete(identityKey);
      setPendingIdentity((current) => current === identityKey ? null : current);
    }
  }

  return (
    <span className="watch-control">
      <button
        type="button"
        className={status.watched ? "watch-button selected" : "watch-button"}
        aria-pressed={status.watched}
        disabled={loading || pending || status.watched || Boolean(error)}
        onClick={watch}
        title={status.watched ? "当前版本只支持单向关注" : undefined}
      >
        {pending ? "关注中…" : status.watched ? "已关注" : "关注"}
      </button>
      {error ? (
        <small aria-live="polite">
          {staleGeneration ? "数据已更新" : "状态暂不可用"}
          <button type="button" onClick={staleGeneration ? reload : retry}>
            {staleGeneration ? "刷新页面" : "重试"}
          </button>
        </small>
      ) : message ? <small aria-live="polite">{message}</small>
        : status.watched ? <small>关注是单向记录</small> : null}
    </span>
  );
}
