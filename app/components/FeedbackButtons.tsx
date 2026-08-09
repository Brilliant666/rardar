"use client";

import { useEffect, useRef, useState } from "react";
import { isFreshClientRead, stableProjectSelector } from "../client-project-identity.mjs";
import { feedbackEventName, getDeviceId } from "./device-id";

const options = ["有用", "无用", "复用", "待确定"] as const;
type FeedbackValue = (typeof options)[number];

export function FeedbackButtons({
  projectIdVersion,
  projectId,
}: {
  projectIdVersion: 1;
  projectId: string;
}) {
  const [selected, setSelected] = useState<FeedbackValue | null>(null);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const requestVersion = useRef(0);
  const successfulMutationVersion = useRef(0);
  const saveInFlight = useRef(false);

  useEffect(() => {
    const currentRequestVersion = ++requestVersion.current;
    const mutationVersionAtStart = successfulMutationVersion.current;
    const deviceId = getDeviceId();
    if (!deviceId) return;
    const project = stableProjectSelector({ projectIdVersion, projectId });
    const controller = new AbortController();
    const query = new URLSearchParams({
      deviceId,
      projectIdVersion: String(project.projectIdVersion),
      projectId: project.projectId,
    });
    fetch(`/api/feedback?${query}`, { signal: controller.signal })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        if (!isFreshClientRead(
          controller.signal.aborted,
          currentRequestVersion,
          requestVersion.current,
          mutationVersionAtStart,
          successfulMutationVersion.current,
        )) return;
        if (payload?.feedback?.value) setSelected(payload.feedback.value);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [projectId, projectIdVersion]);

  async function save(value: FeedbackValue) {
    if (saveInFlight.current) return;
    saveInFlight.current = true;
    setSaving(true);
    const currentRequestVersion = requestVersion.current;
    const project = stableProjectSelector({ projectIdVersion, projectId });
    const previous = selected;
    setSelected(value);
    setMessage("保存中…");

    try {
      const response = await fetch("/api/feedback", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          deviceId: getDeviceId(),
          ...project,
          value,
        }),
      });

      if (!response.ok) throw new Error("save failed");
      if (currentRequestVersion !== requestVersion.current) return;
      successfulMutationVersion.current += 1;
      setSelected(value);
      setMessage("已记录");
      window.dispatchEvent(new CustomEvent(feedbackEventName, { detail: { ...project, value } }));
    } catch {
      if (currentRequestVersion !== requestVersion.current) return;
      setSelected(previous);
      setMessage("保存失败，请稍后重试");
    } finally {
      saveInFlight.current = false;
      if (currentRequestVersion === requestVersion.current) setSaving(false);
    }
  }

  return (
    <div className="feedback-wrap">
      <div className="feedback-buttons" aria-label="项目反馈">
        {options.map((option) => (
          <button
            className={selected === option ? "selected" : ""}
            key={option}
            onClick={() => save(option)}
            type="button"
            aria-pressed={selected === option}
            disabled={saving}
          >
            {option}
          </button>
        ))}
      </div>
      <span className="feedback-message" aria-live="polite">
        {message}
      </span>
    </div>
  );
}
