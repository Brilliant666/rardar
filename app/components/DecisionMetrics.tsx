"use client";

import { useCallback, useEffect, useState } from "react";
import { feedbackEventName, getDeviceId } from "./device-id";

type Metrics = {
  northStar: { label: string; value: number };
  week: { reuseDecisions: number; feedbackChanges: number };
  current: { useful: number; useless: number; reused: number; uncertain: number; total: number };
};

const emptyMetrics: Metrics = {
  northStar: { label: "近 7 天有效项目决策", value: 0 },
  week: { reuseDecisions: 0, feedbackChanges: 0 },
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
    return () => {
      window.clearTimeout(initialLoad);
      window.removeEventListener(feedbackEventName, refresh);
    };
  }, [load]);

  return (
    <section className="decision-metrics" aria-label="有效项目决策指标">
      <div className="decision-metrics-copy">
        <span className="section-label">North star</span>
        <h2>不是看了多少项目，<br />而是做出了多少有效决定。</h2>
        <p>“有用”和“复用”会计入近 7 天有效项目决策；改变同一项目的选择会保留为决策事件，但不会重复抬高项目数。</p>
      </div>
      <div className="north-star-card" aria-live="polite">
        <span>{metrics.northStar.label}</span>
        <strong>{loaded ? metrics.northStar.value : "—"}</strong>
        <p>其中明确复用 {metrics.week.reuseDecisions} 个 · 本周反馈变化 {metrics.week.feedbackChanges} 次</p>
      </div>
      <div className="decision-breakdown">
        <div><strong>{metrics.current.useful}</strong><span>有用</span></div>
        <div><strong>{metrics.current.reused}</strong><span>复用</span></div>
        <div><strong>{metrics.current.uncertain}</strong><span>待确定</span></div>
        <div><strong>{metrics.current.useless}</strong><span>无用</span></div>
      </div>
    </section>
  );
}
