"use client";

import { useCallback, useEffect, useState } from "react";

type ServiceStatus = {
  state: string;
  pid: number | null;
  restartCount?: number;
  url?: string;
  schedule?: { time: string; timezone: string };
  nextRunAt?: string | null;
  lastRunCompletedAt?: string | null;
};

type RuntimeSnapshot = {
  state: "healthy" | "degraded" | "starting" | "stopped" | "stale";
  checkedAt: string;
  message: string;
  services: {
    website: ServiceStatus;
    scheduler: ServiceStatus;
  };
};

const heartbeatLimit = 35_000;
const runtimeStatusUrl = "http://127.0.0.1:3002/status";

function formatTime(value?: string | null) {
  if (!value) return "等待调度";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function normalizeSnapshot(snapshot: RuntimeSnapshot): RuntimeSnapshot {
  const checkedAt = new Date(snapshot.checkedAt).getTime();
  if (!Number.isFinite(checkedAt) || Date.now() - checkedAt > heartbeatLimit) {
    return { ...snapshot, state: "stale", message: "运行心跳已过期，请重新启动本地管理器" };
  }
  return snapshot;
}

export function RuntimeStatus() {
  const [snapshot, setSnapshot] = useState<RuntimeSnapshot | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${runtimeStatusUrl}?t=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error("runtime status unavailable");
      setSnapshot(normalizeSnapshot((await response.json()) as RuntimeSnapshot));
    } catch {
      setSnapshot({
        state: "stopped",
        checkedAt: new Date().toISOString(),
        message: "本地运行管理器未响应，请使用一键启动入口",
        services: {
          website: { state: "unknown", pid: null },
          scheduler: { state: "unknown", pid: null, schedule: { time: "08:00", timezone: "Asia/Shanghai" } },
        },
      });
    }
  }, []);

  useEffect(() => {
    const initialRefresh = window.setTimeout(() => void refresh(), 0);
    const interval = window.setInterval(() => void refresh(), 10_000);
    return () => {
      window.clearTimeout(initialRefresh);
      window.clearInterval(interval);
    };
  }, [refresh]);

  const healthy = snapshot?.state === "healthy";
  const label = healthy ? "运行中" : snapshot ? "需启动" : "检查中";
  const scheduler = snapshot?.services.scheduler;

  return (
    <div className="schedule-card runtime-card" data-state={snapshot?.state ?? "checking"}>
      <span>本地自动运行</span>
      <strong>{label}</strong>
      <p>
        网站 {healthy ? "在线" : "状态未知"} · 每日 {scheduler?.schedule?.time ?? "08:00"}
        <br />
        {healthy ? `下次刷新 ${formatTime(scheduler?.nextRunAt)}` : snapshot?.message ?? "正在读取运行状态"}
      </p>
    </div>
  );
}
