"use client";

import { useCallback, useEffect, useState } from "react";
import {
  normalizeRuntimeStatusSnapshot,
  type RuntimeStatusSnapshot,
} from "../runtime-readiness.mjs";

const runtimeStatusUrl = "http://127.0.0.1:3002/status";

function formatTime(value?: string | null, timezone = "Asia/Shanghai") {
  if (!value) return "等待调度";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: timezone,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatSigned(value: number) {
  return value > 0 ? `+${value}` : String(value);
}

function formatAge(value?: number | null) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "未知";
  return `${(value / 3600).toFixed(1)} 小时`;
}

export function RuntimeStatus() {
  const [snapshot, setSnapshot] = useState<RuntimeStatusSnapshot | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${runtimeStatusUrl}?t=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error("runtime status unavailable");
      setSnapshot(normalizeRuntimeStatusSnapshot(await response.json()));
    } catch {
      setSnapshot({
        schemaVersion: 1,
        state: "stopped",
        checkedAt: new Date().toISOString(),
        message: "本地运行管理器未响应，请使用一键启动入口",
        services: {
          website: { state: "unknown", pid: null },
          scheduler: { state: "unknown", pid: null, schedule: { time: "08:00", timezone: "Asia/Shanghai" } },
        },
        data: {
          freshness: "invalid",
          currentGenerationId: null,
          snapshotCapturedAt: null,
          snapshotAgeSeconds: null,
          staleAfterSeconds: 36 * 60 * 60,
          lastSuccessfulRefreshAt: null,
          lastSuccessfulSnapshotAt: null,
        },
        schedule: {
          at: "08:00",
          timezone: "Asia/Shanghai",
          nextRunAt: null,
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

  const servicesHealthy = snapshot?.services.website.state === "healthy"
    && snapshot?.services.scheduler.state === "healthy";
  const healthy = snapshot?.state === "healthy";
  const scheduler = snapshot?.services.scheduler;
  const scheduleTimezone = snapshot?.schedule.timezone
    ?? scheduler?.schedule?.timezone
    ?? "Asia/Shanghai";
  const dataStale = servicesHealthy && snapshot?.data?.freshness === "stale";
  const refreshing = healthy && scheduler?.refreshState === "running";
  const waitingForRetry = healthy && scheduler?.refreshState === "failed" && scheduler.retryAttempt;
  const refreshFailed = healthy && scheduler?.refreshState === "failed";
  const auditDegraded = healthy && scheduler?.dataAuditStatus === "degraded";
  const auditSummary = scheduler?.dataAuditSummary;
  const queryCoverage =
    auditSummary?.successfulQueryCount != null && auditSummary?.failedQueryCount != null
      ? `查询 ${auditSummary.successfulQueryCount}/${auditSummary.successfulQueryCount + auditSummary.failedQueryCount}`
      : null;
  const sourceCoverage =
    auditSummary?.healthySourceCount != null && auditSummary?.failedSourceCount != null
      ? `信源 ${auditSummary.healthySourceCount}/${auditSummary.healthySourceCount + auditSummary.failedSourceCount}`
      : null;
  const staticAnalysisStatus = auditSummary?.analysisFailureCount
    ? `静态扫描失败 ${auditSummary.analysisFailureCount}`
    : auditSummary?.staticAnalysisRequiredCount
      ? `待静态扫描 ${auditSummary.staticAnalysisRequiredCount}`
      : null;
  const coverageDetail = [queryCoverage, sourceCoverage, staticAnalysisStatus].filter(Boolean).join(" · ");
  const label = dataStale
    ? "数据已陈旧"
    : refreshing
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
  const detail = dataStale
    ? `最近成功快照 ${formatTime(snapshot?.data?.snapshotCapturedAt, scheduleTimezone)} · 数据年龄 ${formatAge(snapshot?.data?.snapshotAgeSeconds)} · 下次刷新 ${formatTime(snapshot?.schedule?.nextRunAt ?? scheduler?.nextRunAt, scheduleTimezone)}`
    : refreshing
    ? `本轮开始 ${formatTime(scheduler?.lastRunStartedAt, scheduleTimezone)}`
    : waitingForRetry
      ? `第 ${waitingForRetry} 次尝试将在 ${formatTime(scheduler?.nextRunAt, scheduleTimezone)} 开始`
      : refreshFailed
        ? `本轮采集未完成 · 下次计划 ${formatTime(scheduler?.nextRunAt, scheduleTimezone)}`
        : auditDegraded
          ? `数据审计发现 ${scheduler?.dataAuditWarningCount ?? 0} 条警告${coverageDetail ? ` · ${coverageDetail}` : ""} · 下次刷新 ${formatTime(scheduler?.nextRunAt, scheduleTimezone)}`
          : auditSummary
            ? `本轮观测 ${auditSummary.observedProjectCount ?? 0} 项 · 净 Star ${formatSigned(auditSummary.observedNetStarChange ?? 0)} · 动量 ${auditSummary.dailyTrackCounts?.recentMomentum ?? 0} / 长期 ${auditSummary.dailyTrackCounts?.longTerm ?? 0}${coverageDetail ? ` · ${coverageDetail}` : ""} · 下次 ${formatTime(scheduler?.nextRunAt, scheduleTimezone)}`
            : `下次刷新 ${formatTime(scheduler?.nextRunAt, scheduleTimezone)}`;

  return (
    <div
      className="schedule-card runtime-card"
      data-state={dataStale || refreshFailed || auditDegraded ? "degraded" : snapshot?.state ?? "checking"}
    >
      <span>本地自动运行</span>
      <strong>{label}</strong>
      <p>
        网站 {servicesHealthy ? "在线" : "状态未知"} · 每日 {snapshot?.schedule?.at ?? scheduler?.schedule?.time ?? "08:00"} {scheduleTimezone}
        <br />
        {healthy || dataStale ? detail : snapshot?.message ?? "正在读取运行状态"}
      </p>
    </div>
  );
}
