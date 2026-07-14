import { loadPublishedBundleFromBridge } from "./published-data-client";
import type { CatalogSnapshot } from "./data";
import { normalizeCatalogSnapshot } from "./score-semantics.mjs";
import {
  applySignalEnrichments,
  type CodexQueueSnapshot,
  type SignalEnrichmentSnapshot,
  type SignalSnapshot,
} from "./signals";

export type PublishedData = {
  generationId: string;
  publishedAt: string;
  previousGenerationId: string | null;
  catalog: CatalogSnapshot;
  projects: CatalogSnapshot["projects"];
  dailyProjects: CatalogSnapshot["projects"];
  candidateProjects: CatalogSnapshot["projects"];
  snapshotNotice: string;
  signalSnapshot: SignalSnapshot;
  codexQueue: CodexQueueSnapshot;
};

/**
 * Load one request's complete public data view. The underlying loader resolves
 * current.json once, verifies every manifest artifact, and parses all values
 * from that same immutable generation directory.
 */
export async function loadPublishedData(): Promise<PublishedData> {
  if (typeof window !== "undefined") {
    throw new Error("published Rardar data can only be loaded on the server");
  }
  const bundle = await loadPublishedBundleFromBridge();
  const catalog = normalizeCatalogSnapshot(bundle.catalog) as unknown as CatalogSnapshot;
  const projects = catalog.projects;
  const signalSnapshot = applySignalEnrichments(
    bundle.signals as unknown as SignalSnapshot,
    bundle.signalEnrichment as unknown as SignalEnrichmentSnapshot,
  );

  return {
    generationId: bundle.generationId,
    publishedAt: bundle.publishedAt,
    previousGenerationId: bundle.previousGenerationId,
    catalog,
    projects,
    dailyProjects: projects.slice(0, 5),
    candidateProjects: projects.slice(5),
    snapshotNotice: catalog.notice,
    signalSnapshot,
    codexQueue: bundle.codexQueue as unknown as CodexQueueSnapshot,
  };
}
