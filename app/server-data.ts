import { env } from "cloudflare:workers";
import { loadPublishedBundleFromBridge } from "./published-data-client";
import type { CatalogSnapshot, StableProject } from "./data";
import {
  createProjectIdentityContext,
  type ProjectIdentityContext,
} from "./project-identity.mjs";
import { normalizeCatalogSnapshot } from "./score-semantics.mjs";
import {
  evaluateDataFreshness,
  parseRfc3339Instant,
  runtimeReadinessConfig,
  type DataFreshness,
  type RuntimeReadinessConfig,
} from "./runtime-readiness.mjs";
import {
  applySignalEnrichments,
  type AssociatedSignalSnapshot,
  type CodexQueueSnapshot,
  type SignalEnrichmentSnapshot,
  type SignalSnapshot,
} from "./signals";
import { associateSignalSnapshotWithCatalog } from "./signal-project-association.mjs";

export type PublishedData = {
  generationId: string;
  publishedAt: string;
  previousGenerationId: string | null;
  dataFreshness: DataFreshness;
  runtimeReadiness: RuntimeReadinessConfig;
  catalog: Omit<CatalogSnapshot, "projects"> & { projects: StableProject[] };
  identityContext: ProjectIdentityContext;
  projects: StableProject[];
  dailyProjects: StableProject[];
  candidateProjects: StableProject[];
  snapshotNotice: string;
  signalSnapshot: AssociatedSignalSnapshot;
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
  const normalizedCatalog = normalizeCatalogSnapshot(bundle.catalog) as unknown as CatalogSnapshot;
  if (
    parseRfc3339Instant(normalizedCatalog.capturedAt)
    !== parseRfc3339Instant(bundle.snapshotCapturedAt)
  ) {
    throw new Error("published generation is unavailable: snapshot and Catalog capturedAt do not match");
  }
  const runtimeReadiness = runtimeReadinessConfig(env as unknown as Record<string, unknown>);
  const dataFreshness = evaluateDataFreshness(
    bundle.snapshotCapturedAt,
    runtimeReadiness.staleAfterSeconds,
  );
  const identityContext = await createProjectIdentityContext(
    bundle.generationId,
    normalizedCatalog,
    bundle.publishedAt,
  );
  const projects = identityContext.stableProjects(normalizedCatalog.projects) as StableProject[];
  const catalog = { ...normalizedCatalog, projects };
  const enrichedSignalSnapshot = applySignalEnrichments(
    bundle.signals as unknown as SignalSnapshot,
    bundle.signalEnrichment as unknown as SignalEnrichmentSnapshot,
  );
  const signalSnapshot = await associateSignalSnapshotWithCatalog(
    enrichedSignalSnapshot,
    identityContext,
  );

  return {
    generationId: bundle.generationId,
    publishedAt: bundle.publishedAt,
    previousGenerationId: bundle.previousGenerationId,
    dataFreshness,
    runtimeReadiness,
    catalog,
    identityContext,
    projects,
    dailyProjects: projects.slice(0, 5),
    candidateProjects: projects.slice(5),
    snapshotNotice: catalog.notice,
    signalSnapshot,
    codexQueue: bundle.codexQueue as unknown as CodexQueueSnapshot,
  };
}
