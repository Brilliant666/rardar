import { env } from "cloudflare:workers";

const noStoreHeaders = {
  "cache-control": "no-store",
  "content-type": "application/json; charset=utf-8",
};
const defaultOrigin = "http://127.0.0.1:3002";
const maximumStatusBytes = 64 * 1024;

function runtimeStatusOrigin(): string {
  const bindings = env as unknown as Record<string, unknown>;
  const candidate = bindings.RARDAR_RUNTIME_STATUS_ORIGIN ?? defaultOrigin;
  if (typeof candidate !== "string") {
    throw new Error("runtime status origin is unavailable");
  }
  const parsed = new URL(candidate);
  if (
    parsed.protocol !== "http:"
    || parsed.hostname !== "127.0.0.1"
    || !/^[1-9]\d{0,4}$/.test(parsed.port)
    || Number(parsed.port) > 65535
    || parsed.username
    || parsed.password
    || parsed.pathname !== "/"
    || parsed.search
    || parsed.hash
  ) {
    throw new Error("runtime status origin must be a loopback HTTP origin");
  }
  return parsed.origin;
}

function boundedError(error: unknown): string {
  const detail = error instanceof Error ? error.message : String(error);
  return detail.replace(/\s+/g, " ").trim().slice(0, 160) || "runtime status unavailable";
}

export async function GET() {
  try {
    const response = await fetch(`${runtimeStatusOrigin()}/status`, {
      cache: "no-store",
      headers: { accept: "application/json", "cache-control": "no-store" },
      signal: AbortSignal.timeout(2_000),
    });
    if (!response.ok) {
      throw new Error(`runtime status returned HTTP ${response.status}`);
    }
    const body = await response.text();
    if (new TextEncoder().encode(body).byteLength > maximumStatusBytes) {
      throw new Error("runtime status response exceeded the size limit");
    }
    const payload: unknown = JSON.parse(body);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("runtime status response was not a JSON object");
    }
    return Response.json(payload, { status: 200, headers: noStoreHeaders });
  } catch (error) {
    return Response.json(
      {
        schemaVersion: 1,
        state: "unavailable",
        message: boundedError(error),
      },
      { status: 503, headers: noStoreHeaders },
    );
  }
}
