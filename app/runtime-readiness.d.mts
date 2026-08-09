export type RuntimeReadinessConfig = Readonly<{
  scheduleAt: string;
  scheduleTimezone: string;
  staleAfterHours: number;
  staleAfterSeconds: number;
}>;

export type DataFreshness = Readonly<{
  freshness: "fresh" | "stale";
  snapshotCapturedAt: string;
  ageSeconds: number;
  staleAfterSeconds: number;
}>;

export type RuntimeServiceStatus = {
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
    successfulQueryCount?: number | null;
    failedQueryCount?: number | null;
    healthySourceCount?: number | null;
    failedSourceCount?: number | null;
    analysisFailureCount?: number;
    staticAnalysisRequiredCount?: number;
  } | null;
};

export type RuntimeStatusSnapshot = {
  schemaVersion: 1;
  state: "healthy" | "degraded" | "starting" | "stopped" | "stale";
  checkedAt: string;
  message: string;
  services: {
    website: RuntimeServiceStatus;
    scheduler: RuntimeServiceStatus;
  };
  data: {
    freshness: "fresh" | "stale" | "invalid";
    currentGenerationId: string | null;
    snapshotCapturedAt: string | null;
    snapshotAgeSeconds: number | null;
    staleAfterSeconds: number;
    lastSuccessfulRefreshAt: string | null;
    lastSuccessfulSnapshotAt: string | null;
  };
  schedule: {
    at: string;
    timezone: string;
    nextRunAt: string | null;
  };
};

export function runtimeReadinessConfig(
  environment?: Record<string, unknown>,
): RuntimeReadinessConfig;
export function evaluateDataFreshness(
  snapshotCapturedAt: unknown,
  staleAfterSeconds: number,
  nowMilliseconds?: number,
): DataFreshness;
export function parseRfc3339Instant(value: unknown): number;
export function formatSnapshotAge(ageSeconds: number): string;
export function normalizeRuntimeStatusSnapshot(
  value: unknown,
  nowMilliseconds?: number,
  heartbeatLimitMilliseconds?: number,
): RuntimeStatusSnapshot;
export const DEFAULT_SCHEDULE_AT: "08:00";
export const DEFAULT_SCHEDULE_TIMEZONE: "Asia/Shanghai";
export const DEFAULT_STALE_AFTER_HOURS: 36;
export const MAX_FUTURE_SKEW_SECONDS: 300;
