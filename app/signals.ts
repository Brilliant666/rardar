export type TechnicalSignal = {
  id: string;
  kind: "official" | "aggregated" | "ranking" | "curated";
  title: string;
  titleZh?: string;
  summaryZh: string;
  takeawayZh?: string;
  whyItMattersZh?: string;
  categoryZh?: string;
  url: string;
  source: string;
  sourceUrl: string;
  publishedAt: string;
  score: number;
  evidence: string[];
  sources: string[];
  repo?: string;
  reportedDailyGrowth?: number;
};

export type SourceStatus = {
  id: string;
  name: string;
  url: string;
  state: "healthy" | "failed";
  itemCount: number;
  latestItemAt: string | null;
  error: string | null;
};

export type SignalSnapshot = {
  schemaVersion: number;
  capturedAt: string;
  windowHours: number;
  signalCount: number;
  healthySourceCount: number;
  failedSourceCount: number;
  sourceStatus: SourceStatus[];
  topSignals: TechnicalSignal[];
  signals: TechnicalSignal[];
};

export type CodexQueueSnapshot = {
  schemaVersion: number;
  generatedAt: string;
  pendingCount: number;
  projectPendingCount: number;
  signalPendingCount: number;
  completedProjectCount: number;
  completedSignalCount: number;
};

export type SignalEnrichment = {
  titleZh: string;
  takeawayZh: string;
  whyItMattersZh: string;
  categoryZh: string;
  analyzedAt?: string;
  sourcePublishedAt?: string;
};

export type SignalEnrichmentSnapshot = {
  schemaVersion: number;
  generatedAt: string;
  items: Record<string, SignalEnrichment>;
};

function isCurrentEnrichment(
  signal: TechnicalSignal,
  enrichment: SignalEnrichment | undefined,
  legacyAnalyzedAt: string,
) {
  if (
    !enrichment?.titleZh ||
    !enrichment.takeawayZh ||
    !enrichment.whyItMattersZh ||
    !enrichment.categoryZh
  ) return false;
  const publishedAt = new Date(signal.publishedAt).getTime();
  const analyzedAt = new Date(enrichment.analyzedAt ?? legacyAnalyzedAt).getTime();
  if (!Number.isFinite(publishedAt) || !Number.isFinite(analyzedAt) || analyzedAt < publishedAt) return false;
  if (!enrichment.sourcePublishedAt) return true;
  return new Date(enrichment.sourcePublishedAt).getTime() === publishedAt;
}

export function applySignalEnrichments(
  rawSignals: SignalSnapshot,
  enrichmentSnapshot: SignalEnrichmentSnapshot,
): SignalSnapshot {
  const enrichments = enrichmentSnapshot.items ?? {};
  const legacyAnalyzedAt = enrichmentSnapshot.generatedAt;
  const signals = rawSignals.signals.map((signal) => ({
    ...signal,
    ...(isCurrentEnrichment(signal, enrichments[signal.url], legacyAnalyzedAt)
      ? enrichments[signal.url]
      : {}),
  }));
  const signalById = new Map(signals.map((signal) => [signal.id, signal]));
  return {
    ...rawSignals,
    signals,
    topSignals: rawSignals.topSignals.map((signal) => signalById.get(signal.id) ?? signal),
  };
}

export const signalKindLabels: Record<TechnicalSignal["kind"], string> = {
  official: "官方更新",
  aggregated: "聚合信号",
  ranking: "外部榜单",
  curated: "人工精选",
};

export function formatSignalTime(value: string) {
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
