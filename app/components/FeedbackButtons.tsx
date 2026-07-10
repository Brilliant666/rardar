"use client";

import { useEffect, useState } from "react";
import { feedbackEventName, getDeviceId } from "./device-id";

const options = ["有用", "无用", "复用", "待确定"] as const;
type FeedbackValue = (typeof options)[number];

export function FeedbackButtons({ projectSlug }: { projectSlug: string }) {
  const [selected, setSelected] = useState<FeedbackValue | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    const deviceId = getDeviceId();
    if (!deviceId) return;
    fetch(`/api/feedback?deviceId=${encodeURIComponent(deviceId)}&projectSlug=${encodeURIComponent(projectSlug)}`)
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        if (payload?.feedback?.value) setSelected(payload.feedback.value);
      })
      .catch(() => undefined);
  }, [projectSlug]);

  async function save(value: FeedbackValue) {
    const previous = selected;
    setSelected(value);
    setMessage("保存中…");

    try {
      const response = await fetch("/api/feedback", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          deviceId: getDeviceId(),
          projectSlug,
          value,
        }),
      });

      if (!response.ok) throw new Error("save failed");
      setMessage("已记录");
      window.dispatchEvent(new CustomEvent(feedbackEventName, { detail: { projectSlug, value } }));
    } catch {
      setSelected(previous);
      setMessage("保存失败，请稍后重试");
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
