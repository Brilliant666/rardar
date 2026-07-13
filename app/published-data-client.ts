import { env } from "cloudflare:workers";
import type { RawPublishedBundle } from "./published-data-loader.mjs";

const BRIDGE_PATH = "/__rardar/published-generation";
const GENERATION_ID = /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$/;

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
