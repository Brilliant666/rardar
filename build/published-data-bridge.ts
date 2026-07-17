import { execFile } from "node:child_process";
import type { IncomingMessage, ServerResponse } from "node:http";
import { isAbsolute, resolve } from "node:path";
import { promisify } from "node:util";
import type { Plugin } from "vite";
import { loadPublishedBundle } from "../app/published-data-loader.mjs";

export const PUBLISHED_DATA_BRIDGE_PATH = "/__rardar/published-generation";
export const HISTORICAL_IDENTITY_BRIDGE_PATH = "/__rardar/historical-project-identities";

const execFileAsync = promisify(execFile);
const HISTORICAL_IDENTITY_TIMEOUT_MS = 180_000;
const HISTORICAL_IDENTITY_MAX_BYTES = 16 * 1024 * 1024;

type PublishedDataBridgeOptions = {
  token: string;
  dataDirectory?: string;
};

function isLoopbackRequest(request: IncomingMessage): boolean {
  const address = request.socket.remoteAddress;
  return address === "127.0.0.1" || address === "::1" || address === "::ffff:127.0.0.1";
}

function shortError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/\s+/g, " ").trim().slice(0, 240) || "published generation is unavailable";
}

function sendJson(response: ServerResponse, status: number, payload: unknown): void {
  const body = Buffer.from(`${JSON.stringify(payload)}\n`, "utf8");
  response.statusCode = status;
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.setHeader("Content-Length", String(body.byteLength));
  response.setHeader("X-Content-Type-Options", "nosniff");
  response.end(body);
}

async function loadHistoricalIdentityBundle(
  repositoryRoot: string,
  dataDirectory: string,
): Promise<unknown> {
  const python = process.env.RARDAR_PYTHON || "python";
  const { stdout } = await execFileAsync(
    python,
    ["-m", "pipeline.historical_identity", "--data-dir", dataDirectory],
    {
      cwd: repositoryRoot,
      encoding: "utf8",
      env: {
        ...process.env,
        PYTHONIOENCODING: "utf-8",
        PYTHONUTF8: "1",
      },
      maxBuffer: HISTORICAL_IDENTITY_MAX_BYTES,
      timeout: HISTORICAL_IDENTITY_TIMEOUT_MS,
      windowsHide: true,
    },
  );
  const serialized = stdout.trim();
  if (!serialized) throw new Error("historical identity builder returned no output");
  return JSON.parse(serialized) as unknown;
}

export function publishedDataBridge(options: PublishedDataBridgeOptions): Plugin {
  let dataDirectory = options.dataDirectory;
  let repositoryRoot: string | undefined;

  return {
    name: "rardar-published-data-bridge",
    apply: "serve",
    enforce: "pre",
    configResolved(config) {
      repositoryRoot = resolve(config.root);
      const configured = dataDirectory || resolve(config.root, "data");
      dataDirectory = isAbsolute(configured) ? configured : resolve(config.root, configured);
    },
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const pathname = new URL(request.url || "/", "http://127.0.0.1").pathname;
        if (
          pathname !== PUBLISHED_DATA_BRIDGE_PATH
          && pathname !== HISTORICAL_IDENTITY_BRIDGE_PATH
        ) {
          next();
          return;
        }
        const suppliedToken = request.headers["x-rardar-data-token"];
        if (
          !isLoopbackRequest(request) ||
          typeof suppliedToken !== "string" ||
          suppliedToken !== options.token
        ) {
          sendJson(response, 404, { status: "not_found" });
          return;
        }
        if (request.method !== "GET") {
          response.setHeader("Allow", "GET");
          sendJson(response, 405, { status: "method_not_allowed" });
          return;
        }
        if (pathname === PUBLISHED_DATA_BRIDGE_PATH) {
          try {
            const bundle = loadPublishedBundle(dataDirectory);
            sendJson(response, 200, { schemaVersion: 1, status: "healthy", bundle });
          } catch (error) {
            sendJson(response, 503, { status: "degraded", error: shortError(error) });
          }
          return;
        }

        void loadHistoricalIdentityBundle(repositoryRoot!, dataDirectory!)
          .then((bundle) => {
            sendJson(response, 200, { schemaVersion: 1, status: "healthy", bundle });
          })
          .catch((error) => {
            sendJson(response, 503, { status: "degraded", error: shortError(error) });
          });
      });
    },
  };
}
