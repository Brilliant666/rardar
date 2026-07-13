import type { IncomingMessage, ServerResponse } from "node:http";
import { isAbsolute, resolve } from "node:path";
import type { Plugin } from "vite";
import { loadPublishedBundle } from "../app/published-data-loader.mjs";

export const PUBLISHED_DATA_BRIDGE_PATH = "/__rardar/published-generation";

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

export function publishedDataBridge(options: PublishedDataBridgeOptions): Plugin {
  let dataDirectory = options.dataDirectory;

  return {
    name: "rardar-published-data-bridge",
    apply: "serve",
    enforce: "pre",
    configResolved(config) {
      const configured = dataDirectory || resolve(config.root, "data");
      dataDirectory = isAbsolute(configured) ? configured : resolve(config.root, configured);
    },
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const pathname = new URL(request.url || "/", "http://127.0.0.1").pathname;
        if (pathname !== PUBLISHED_DATA_BRIDGE_PATH) {
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
        try {
          const bundle = loadPublishedBundle(dataDirectory);
          sendJson(response, 200, { schemaVersion: 1, status: "healthy", bundle });
        } catch (error) {
          sendJson(response, 503, { status: "degraded", error: shortError(error) });
        }
      });
    },
  };
}
