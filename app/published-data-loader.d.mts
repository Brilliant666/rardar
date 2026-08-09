export type RawPublishedBundle = {
  generationId: string;
  publishedAt: string;
  previousGenerationId: string | null;
  manifest: Record<string, unknown>;
  snapshotCapturedAt: string;
  catalog: Record<string, unknown>;
  signals: Record<string, unknown>;
  signalEnrichment: Record<string, unknown>;
  codexQueue: Record<string, unknown>;
};

export function loadPublishedBundle(dataDirectory?: string): RawPublishedBundle;

export const REQUIRED_ARTIFACTS: Readonly<{
  snapshot: "snapshots/latest.json";
  catalog: "catalog/latest.json";
  signals: "signals/latest.json";
  signalEnrichment: "signals/enrichment.json";
  codexQueue: "queues/codex.json";
}>;
