"use client";

import { useRef, useState } from "react";
import { resultForGeneration, stableProjectSelector } from "../client-project-identity.mjs";
import { feedbackEventName, generationStaleEventName, getDeviceId } from "./device-id";
import { useDecisionState } from "./DecisionStateProvider";

const options = ["有用", "无用", "复用", "待确定"] as const;
type FeedbackValue = (typeof options)[number];

export function FeedbackButtons({
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
  const selected = options.includes(state.feedback as FeedbackValue)
    ? state.feedback as FeedbackValue
    : null;
  const identityKey = `${generationId}:${projectIdVersion}:${projectId}`;
  const [messages, setMessages] = useState<Record<string, string>>({});
  const [savingIdentities, setSavingIdentities] = useState<Set<string>>(new Set());
  const saveInFlight = useRef<Map<string, object>>(new Map());
  const message = messages[identityKey] ?? "";
  const saving = savingIdentities.has(identityKey);

  async function save(value: FeedbackValue) {
    if (saveInFlight.current.has(identityKey)) return;
    const requestIdentity = identityKey;
    const attempt = {};
    const project = stableProjectSelector({ projectIdVersion, projectId });
    saveInFlight.current.set(requestIdentity, attempt);
    setSavingIdentities((current) => new Set([...current, requestIdentity]));
    setMessages((current) => ({ ...current, [requestIdentity]: "保存中…" }));

    try {
      const response = await fetch("/api/feedback", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          deviceId: getDeviceId(),
          ...project,
          value,
          generationId,
        }),
      });
      if (!response.ok) {
        if (response.status === 409) {
          const stale = await response.clone().json().catch(() => null);
          if (stale?.error === "stale_generation") {
            window.dispatchEvent(new CustomEvent(generationStaleEventName, {
              detail: {
                expectedGenerationId: generationId,
                currentGenerationId: stale.generationId,
              },
            }));
          }
        }
        throw new Error("save failed");
      }
      const payload = resultForGeneration(generationId, await response.json());
      if (!payload) throw new Error("published generation changed");
      window.dispatchEvent(new CustomEvent(feedbackEventName, {
        detail: { ...project, value, generationId: payload.generationId },
      }));
      setMessages((current) => ({
        ...current,
        [requestIdentity]: "已记录为推荐质量反馈",
      }));
    } catch {
      setMessages((current) => ({ ...current, [requestIdentity]: "保存失败，请稍后重试" }));
    } finally {
      if (saveInFlight.current.get(requestIdentity) === attempt) {
        saveInFlight.current.delete(requestIdentity);
      }
      setSavingIdentities((current) => {
        const next = new Set(current);
        next.delete(requestIdentity);
        return next;
      });
    }
  }

  return (
    <div className="feedback-wrap" aria-busy={loading || saving}>
      <div className="feedback-buttons" aria-label="推荐质量反馈">
        {options.map((option) => (
          <button
            className={selected === option ? "selected" : ""}
            key={option}
            onClick={() => save(option)}
            type="button"
            aria-pressed={selected === option}
            disabled={loading || saving || Boolean(error)}
          >
            {option}
          </button>
        ))}
      </div>
      <span className="feedback-message" aria-live="polite">
        {loading ? "正在读取反馈…" : message}
        {error ? <>
          <span>{error}</span>
          <button type="button" onClick={staleGeneration ? reload : retry}>
            {staleGeneration ? "刷新页面" : "重试"}
          </button>
        </> : null}
      </span>
    </div>
  );
}
