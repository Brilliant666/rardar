"use client";

import { useCallback, useEffect, useState } from "react";

type ServiceStatus = {
  state: string;
  pid: number | null;
  restartCount?: number;
  url?: string;
  schedule?: { time: string; timezone: string };
  refreshState?: "scheduled" | "running" | "healthy" | "failed";
  nextRunAt?: string | null;
  lastRunStartedAt?: string | null;
  lastRunCompletedAt?: string | null;
  retryAttempt?: number | null;
  dataAuditStatus?: "healthy" | "degraded" | "failed" | null;
  dataAuditWarningCount?: number | null;
  dataAuditSummary?: {
    observedProjectCount?: number;
    observedNetStarChange?: number;
    dailyTrackCounts?: { recentMomentum?: number; longTerm?: number } | null;
    historyCount?: number;
  } | null;
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

function formatSigned(value: number) {
  return value > 0 ? `+${value}` : String(value);
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
  const scheduler = snapshot?.services.scheduler;
  const refreshing = healthy && scheduler?.refreshState === "running";
  const waitingForRetry = healthy && scheduler?.refreshState === "failed" && scheduler.retryAttempt;
  const refreshFailed = healthy && scheduler?.refreshState === "failed";
  const auditDegraded = healthy && scheduler?.dataAuditStatus === "degraded";
  const auditSummary = scheduler?.dataAuditSummary;
  const label = refreshing
    ? "刷新中"
    : waitingForRetry
      ? "等待重试"
      : refreshFailed
        ? "刷新失败"
        : auditDegraded
          ? "数据需复核"
          : healthy
            ? "运行中"
            : snapshot
              ? "需启动"
              : "检查中";
  const detail = refreshing
    ? `本轮开始 ${formatTime(scheduler?.lastRunStartedAt)}`
    : waitingForRetry
      ? `第 ${waitingForRetry} 次尝试将在 ${formatTime(scheduler?.nextRunAt)} 开始`
      : refreshFailed
        ? `本轮采集未完成 · 下次计划 ${formatTime(scheduler?.nextRunAt)}`
        : auditDegraded
          ? `数据审计发现 ${scheduler?.dataAuditWarningCount ?? 0} 条警告 · 下次刷新 ${formatTime(scheduler?.nextRunAt)}`
          : auditSummary
            ? `本轮观测 ${auditSummary.observedProjectCount ?? 0} 项 · 净 Star ${formatSigned(auditSummary.observedNetStarChange ?? 0)} · 动量 ${auditSummary.dailyTrackCounts?.recentMomentum ?? 0} / 长期 ${auditSummary.dailyTrackCounts?.longTerm ?? 0} · 下次 ${formatTime(scheduler?.nextRunAt)}`
            : `下次刷新 ${formatTime(scheduler?.nextRunAt)}`;

  return (
    <div
      className="schedule-card runtime-card"
      data-state={refreshFailed || auditDegraded ? "degraded" : snapshot?.state ?? "checking"}
    >
      <span>本地自动运行</span>
      <strong>{label}</strong>
      <p>
        网站 {healthy ? "在线" : "状态未知"} · 每日 {scheduler?.schedule?.time ?? "08:00"}
        <br />
        {healthy ? detail : snapshot?.message ?? "正在读取运行状态"}
      </p>
    </div>
  );
}
