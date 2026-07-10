import signalJson from "../data/signals/latest.json";
import enrichmentJson from "../data/signals/enrichment.json";

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

type SignalEnrichment = {
  titleZh: string;
  takeawayZh: string;
  whyItMattersZh: string;
  categoryZh: string;
};

const rawSignals = signalJson as SignalSnapshot;
const enrichments = enrichmentJson.items as Record<string, SignalEnrichment>;

export const signals = rawSignals.signals.map((signal) => ({
  ...signal,
  ...(enrichments[signal.url] ?? {}),
}));
const signalById = new Map(signals.map((signal) => [signal.id, signal]));

export const signalSnapshot = {
  ...rawSignals,
  signals,
  topSignals: rawSignals.topSignals.map((signal) => signalById.get(signal.id) ?? signal),
};

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
