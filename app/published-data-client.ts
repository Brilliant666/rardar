import { env } from "cloudflare:workers";
import type { RawPublishedBundle } from "./published-data-loader.mjs";

const BRIDGE_PATH = "/__rardar/published-generation";
const HISTORICAL_IDENTITY_BRIDGE_PATH = "/__rardar/historical-project-identities";
const GENERATION_ID = /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$/;
const SHA256 = /^[a-f0-9]{64}$/;
const RFC3339_WITH_TIMEZONE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

export type HistoricalIdentityGeneration = {
  generationId: string;
  generationCreatedAt: string;
  publishedAt: string | null;
  manifestSha256: string;
  catalogSchemaVersion: 1 | 2 | 3;
  active: boolean;
};

export type HistoricalIdentityMapping = {
  generationId: string;
  generationCreatedAt: string;
  publishedAt: string | null;
  manifestSha256: string;
  catalogSchemaVersion: 1 | 2 | 3;
  projectIdVersion: 1;
  projectId: string;
  canonicalRepository: string;
  projectSlug: string;
  active: boolean;
};

export type HistoricalIdentityBundle = {
  schemaVersion: 1;
  activeGenerationId: string;
  activePublishedAt: string;
  generationCount: number;
  mappingCount: number;
  generations: HistoricalIdentityGeneration[];
  mappings: HistoricalIdentityMapping[];
};

type BridgeEnvironment = {
  RARDAR_DATA_BRIDGE_ORIGIN?: unknown;
  RARDAR_DATA_BRIDGE_TOKEN?: unknown;
};

function fail(message: string): never {
  throw new Error(`published generation bridge is unavailable: ${message}`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function requireBridgeOrigin(value: unknown): string {
  if (typeof value !== "string") fail("bridge origin is unavailable");
  let origin: URL;
  try {
    origin = new URL(value);
  } catch {
    fail("bridge origin is invalid");
  }
  if (
    origin.protocol !== "http:" ||
    origin.hostname !== "127.0.0.1" ||
    !origin.port ||
    origin.username ||
    origin.password ||
    origin.pathname !== "/" ||
    origin.search ||
    origin.hash
  ) {
    fail("bridge origin is not a fixed loopback origin");
  }
  return origin.origin;
}

function requireBundle(value: unknown): RawPublishedBundle {
  if (!isRecord(value)) fail("bridge bundle is not an object");
  const generationId = value.generationId;
  if (typeof generationId !== "string" || !GENERATION_ID.test(generationId)) {
    fail("bridge generationId is invalid");
  }
  if (
    !isRecord(value.manifest) ||
    value.manifest.generationId !== generationId ||
    !isRecord(value.catalog) ||
    !isRecord(value.signals) ||
    !isRecord(value.signalEnrichment) ||
    !isRecord(value.codexQueue)
  ) {
    fail("bridge bundle is incomplete or internally inconsistent");
  }
  return value as RawPublishedBundle;
}

function isTimestamp(value: unknown): value is string {
  return typeof value === "string"
    && RFC3339_WITH_TIMEZONE.test(value)
    && Number.isFinite(Date.parse(value));
}

function isGenerationProvenance(value: unknown): value is HistoricalIdentityGeneration {
  return isRecord(value)
    && typeof value.generationId === "string"
    && GENERATION_ID.test(value.generationId)
    && isTimestamp(value.generationCreatedAt)
    && (value.publishedAt === null || isTimestamp(value.publishedAt))
    && typeof value.manifestSha256 === "string"
    && SHA256.test(value.manifestSha256)
    && [1, 2, 3].includes(value.catalogSchemaVersion as number)
    && typeof value.active === "boolean";
}

function requireHistoricalIdentityBundle(value: unknown): HistoricalIdentityBundle {
  if (
    !isRecord(value)
    || value.schemaVersion !== 1
    || typeof value.activeGenerationId !== "string"
    || !GENERATION_ID.test(value.activeGenerationId)
    || !isTimestamp(value.activePublishedAt)
    || !Number.isSafeInteger(value.generationCount)
    || (value.generationCount as number) < 1
    || !Number.isSafeInteger(value.mappingCount)
    || (value.mappingCount as number) < 0
    || !Array.isArray(value.generations)
    || value.generations.length !== value.generationCount
    || !Array.isArray(value.mappings)
    || value.mappings.length !== value.mappingCount
  ) {
    fail("historical identity bundle contract is invalid");
  }
  const generationFacts = new Map<string, HistoricalIdentityGeneration>();
  let activeCount = 0;
  for (const generation of value.generations) {
    if (!isGenerationProvenance(generation) || generationFacts.has(generation.generationId)) {
      fail("historical identity bundle generation provenance is invalid");
    }
    const expectedActive = generation.generationId === value.activeGenerationId;
    if (generation.active !== expectedActive
      || (expectedActive && generation.publishedAt !== value.activePublishedAt)
      || (!expectedActive && generation.publishedAt !== null)) {
      fail("historical identity bundle active generation provenance is inconsistent");
    }
    if (generation.active) activeCount += 1;
    generationFacts.set(generation.generationId, generation);
  }
  if (activeCount !== 1 || !generationFacts.has(value.activeGenerationId)) {
    fail("historical identity bundle active generation is invalid");
  }
  for (const mapping of value.mappings) {
    if (!isRecord(mapping)) fail("historical identity bundle mapping is invalid");
    const generation = typeof mapping.generationId === "string"
      ? generationFacts.get(mapping.generationId)
      : undefined;
    if (!generation
      || mapping.generationCreatedAt !== generation.generationCreatedAt
      || mapping.publishedAt !== generation.publishedAt
      || mapping.manifestSha256 !== generation.manifestSha256
      || mapping.catalogSchemaVersion !== generation.catalogSchemaVersion
      || mapping.active !== generation.active
      || mapping.projectIdVersion !== 1
      || typeof mapping.projectId !== "string"
      || typeof mapping.canonicalRepository !== "string"
      || typeof mapping.projectSlug !== "string") {
      fail("historical identity bundle mapping provenance is inconsistent");
    }
  }
  return value as HistoricalIdentityBundle;
}

function bridgeError(payload: unknown, status: number): string {
  if (isRecord(payload) && typeof payload.error === "string") {
    return payload.error.replace(/\s+/g, " ").trim().slice(0, 240);
  }
  return `bridge returned HTTP ${status}`;
}

export async function loadPublishedBundleFromBridge(): Promise<RawPublishedBundle> {
  const bridgeEnvironment = env as unknown as BridgeEnvironment;
  const origin = requireBridgeOrigin(bridgeEnvironment.RARDAR_DATA_BRIDGE_ORIGIN);
  const token = bridgeEnvironment.RARDAR_DATA_BRIDGE_TOKEN;
  if (typeof token !== "string" || token.length < 32) fail("bridge token is unavailable");

  const response = await fetch(`${origin}${BRIDGE_PATH}`, {
    method: "GET",
    cache: "no-store",
    redirect: "manual",
    headers: {
      Accept: "application/json",
      "X-Rardar-Data-Token": token,
    },
  });
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    fail(`bridge returned non-JSON HTTP ${response.status}`);
  }
  if (!response.ok) fail(bridgeError(payload, response.status));
  if (!isRecord(payload) || payload.schemaVersion !== 1 || payload.status !== "healthy") {
    fail("bridge response contract is invalid");
  }
  return requireBundle(payload.bundle);
}

export async function loadHistoricalIdentityBundleFromBridge(): Promise<HistoricalIdentityBundle> {
  const bridgeEnvironment = env as unknown as BridgeEnvironment;
  const origin = requireBridgeOrigin(bridgeEnvironment.RARDAR_DATA_BRIDGE_ORIGIN);
  const token = bridgeEnvironment.RARDAR_DATA_BRIDGE_TOKEN;
  if (typeof token !== "string" || token.length < 32) fail("bridge token is unavailable");

  const response = await fetch(`${origin}${HISTORICAL_IDENTITY_BRIDGE_PATH}`, {
    method: "GET",
    cache: "no-store",
    redirect: "manual",
    headers: {
      Accept: "application/json",
      "X-Rardar-Data-Token": token,
    },
  });
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    fail(`historical identity bridge returned non-JSON HTTP ${response.status}`);
  }
  if (!response.ok) fail(bridgeError(payload, response.status));
  if (!isRecord(payload) || payload.schemaVersion !== 1 || payload.status !== "healthy") {
    fail("historical identity bridge response contract is invalid");
  }
  return requireHistoricalIdentityBundle(payload.bundle);
}
