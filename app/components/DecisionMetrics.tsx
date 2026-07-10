"use client";

import { useCallback, useEffect, useState } from "react";
import { feedbackEventName, getDeviceId, projectActionEventName } from "./device-id";

type Metrics = {
  northStar: { label: string; value: number };
  week: {
    openedProjects: number;
    savedProjects: number;
    triedProjects: number;
    clonedProjects: number;
    reusedProjects: number;
    feedbackDecisions: number;
    feedbackReuseDecisions: number;
    feedbackChanges: number;
  };
  current: { useful: number; useless: number; reused: number; uncertain: number; total: number };
};

const emptyMetrics: Metrics = {
  northStar: { label: "近 7 天已行动项目", value: 0 },
  week: {
    openedProjects: 0,
    savedProjects: 0,
    triedProjects: 0,
    clonedProjects: 0,
    reusedProjects: 0,
    feedbackDecisions: 0,
    feedbackReuseDecisions: 0,
    feedbackChanges: 0,
  },
  current: { useful: 0, useless: 0, reused: 0, uncertain: 0, total: 0 },
};

export function DecisionMetrics() {
  const [metrics, setMetrics] = useState(emptyMetrics);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    const deviceId = getDeviceId();
    if (!deviceId) return;
    const response = await fetch(`/api/metrics?deviceId=${encodeURIComponent(deviceId)}`);
    if (!response.ok) throw new Error("metrics unavailable");
    setMetrics(await response.json());
    setLoaded(true);
  }, []);

  useEffect(() => {
    const initialLoad = window.setTimeout(() => {
      load().catch(() => setLoaded(true));
    }, 0);
    const refresh = () => load().catch(() => undefined);
    window.addEventListener(feedbackEventName, refresh);
    window.addEventListener(projectActionEventName, refresh);
    return () => {
      window.clearTimeout(initialLoad);
      window.removeEventListener(feedbackEventName, refresh);
      window.removeEventListener(projectActionEventName, refresh);
    };
  }, [load]);

  return (
    <section className="decision-metrics" aria-label="有效项目决策指标">
      <div className="decision-metrics-copy">
        <span className="section-label">North star</span>
        <h2>不是看了多少项目，<br />而是做出了多少有效决定。</h2>
        <p>反馈负责教会排序，“试用 / 浅克隆 / 确认复用”才计入近 7 天结果；同一项目无论完成几步，北极星只计一次。</p>
      </div>
      <div className="north-star-card" aria-live="polite">
        <span>{metrics.northStar.label}</span>
        <strong>{loaded ? metrics.northStar.value : "—"}</strong>
        <p>确认复用 {metrics.week.reusedProjects} · 浅克隆 {metrics.week.clonedProjects} · 试用 {metrics.week.triedProjects}</p>
      </div>
      <div className="decision-breakdown">
        <div><strong>{metrics.week.openedProjects}</strong><span>打开仓库</span></div>
        <div><strong>{metrics.week.savedProjects}</strong><span>收藏</span></div>
        <div><strong>{metrics.week.triedProjects}</strong><span>试用</span></div>
        <div><strong>{metrics.week.clonedProjects}</strong><span>浅克隆</span></div>
      </div>
    </section>
  );
}
