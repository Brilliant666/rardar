import { randomBytes } from "node:crypto";
import { resolve } from "node:path";
import vinext from "vinext";
import { defineConfig } from "vite";
import hostingConfig from "./.openai/hosting.json";
import { publishedDataBridge } from "./build/published-data-bridge";
import { sites } from "./build/sites-vite-plugin";

const SITE_CREATOR_PLACEHOLDER_DATABASE_ID =
  "00000000-0000-4000-8000-000000000000";

const { d1, r2 } = hostingConfig;
const DEFAULT_LOCAL_PORT = 3000;

// macOS Seatbelt blocks FSEvents, so Codex previews need polling for HMR.
const isCodexSeatbeltSandbox = process.env.CODEX_SANDBOX === "seatbelt";

function ensureLoopbackBypassesProxy() {
  const values = [process.env.NO_PROXY, process.env.no_proxy]
    .flatMap((value) => (value || "").split(","))
    .map((value) => value.trim())
    .filter(Boolean);
  for (const host of ["127.0.0.1", "localhost"]) {
    if (!values.includes(host)) values.push(host);
  }
  const combined = [...new Set(values)].join(",");
  process.env.NO_PROXY = combined;
  process.env.no_proxy = combined;
}

function localPortFromEnvironment(): number {
  const raw = process.env.RARDAR_VINEXT_PORT;
  if (raw === undefined) return DEFAULT_LOCAL_PORT;
  if (!/^[1-9]\d{0,4}$/.test(raw)) {
    throw new Error("RARDAR_VINEXT_PORT must be an integer from 1 to 65535");
  }
  const port = Number(raw);
  if (port > 65535) {
    throw new Error("RARDAR_VINEXT_PORT must be an integer from 1 to 65535");
  }
  return port;
}

export default defineConfig(async () => {
  // Keep Wrangler and Miniflare state project-local. These are non-secret tool
  // settings; application environment belongs in ignored `.env*` files.
  process.env.WRANGLER_WRITE_LOGS ??= "false";
  process.env.WRANGLER_LOG_PATH ??= ".wrangler/logs";
  process.env.MINIFLARE_REGISTRY_PATH ??= ".wrangler/registry";
  ensureLoopbackBypassesProxy();

  const localPort = localPortFromEnvironment();
  const bridgeOrigin = `http://127.0.0.1:${localPort}`;
  const bridgeToken = randomBytes(32).toString("hex");
  const localBindingConfig = {
    main: "./worker/index.ts",
    compatibility_flags: ["nodejs_compat"],
    vars: {
      RARDAR_DATA_BRIDGE_ORIGIN: bridgeOrigin,
      RARDAR_DATA_BRIDGE_TOKEN: bridgeToken,
    },
    d1_databases: d1
      ? [
          {
            binding: d1,
            database_name: "site-creator-d1",
            database_id: SITE_CREATOR_PLACEHOLDER_DATABASE_ID,
          },
        ]
      : [],
    r2_buckets: r2
      ? [
          {
            binding: r2,
            bucket_name: "site-creator-r2",
          },
        ]
      : [],
  };
  const persistedState = process.env.RARDAR_VINEXT_STATE_DIR;

  // Wrangler snapshots its log path while the Cloudflare plugin is imported.
  const { cloudflare } = await import("@cloudflare/vite-plugin");

  return {
    server: {
      host: "127.0.0.1",
      port: localPort,
      strictPort: true,
      watch: {
        // A Windows dev watcher can keep candidate directory handles open and
        // block the protocol's same-volume atomic rename. Published data is
        // loaded per request via current.json, so generation internals do not
        // need HMR watching.
        ignored: ["**/data/generations/**"],
        ...(isCodexSeatbeltSandbox
          ? { useFsEvents: false, usePolling: true }
          : {}),
      },
    },
    plugins: [
      publishedDataBridge({
        token: bridgeToken,
        dataDirectory: process.env.RARDAR_DATA_DIR,
      }),
      vinext(),
      sites(),
      cloudflare({
        viteEnvironment: { name: "rsc", childEnvironments: ["ssr"] },
        config: localBindingConfig,
        ...(persistedState
          ? { persistState: { path: resolve(persistedState) } }
          : {}),
      }),
    ],
  };
});
