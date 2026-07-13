import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer, request as httpRequest } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const repositoryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const fixtureHelper = join(repositoryRoot, "tests", "http_generation_fixture.py");
const vinextCli = join(repositoryRoot, "node_modules", "vinext", "dist", "cli.js");
const python = process.env.RARDAR_PYTHON || "python";
const ANSI_ESCAPE = /\u001b\[[0-?]*[ -/]*[@-~]/g;
const MAX_DIAGNOSTIC_LOG = 64 * 1024;

function runPython(arguments_, timeout = 180_000) {
  const result = spawnSync(python, arguments_, {
    cwd: repositoryRoot,
    encoding: "utf8",
    maxBuffer: 10 * 1024 * 1024,
    timeout,
    windowsHide: true,
  });
  if (result.error || result.status !== 0) {
    throw new Error(
      [
        `Python command failed: ${python} ${arguments_.join(" ")}`,
        result.error?.message,
        result.stdout,
        result.stderr,
      ]
        .filter(Boolean)
        .join("\n"),
    );
  }
  return result.stdout.trim();
}

function parseLastJsonLine(output) {
  const line = output.split(/\r?\n/).filter(Boolean).at(-1);
  if (!line) throw new Error("fixture helper returned no JSON output");
  return JSON.parse(line);
}

function loopbackEnvironment(temporaryRoot, dataDirectory, port) {
  const bypass = [process.env.NO_PROXY, process.env.no_proxy, "127.0.0.1", "localhost"]
    .filter(Boolean)
    .join(",");
  return {
    ...process.env,
    RARDAR_DATA_DIR: dataDirectory,
    RARDAR_VINEXT_PORT: String(port),
    RARDAR_VINEXT_STATE_DIR: join(temporaryRoot, "cloudflare-state"),
    WRANGLER_REGISTRY_PATH: join(temporaryRoot, "wrangler-registry"),
    WRANGLER_LOG_PATH: join(temporaryRoot, "wrangler-logs"),
    MINIFLARE_REGISTRY_PATH: join(temporaryRoot, "miniflare-registry"),
    WRANGLER_WRITE_LOGS: "false",
    CLOUDFLARE_VITE_FORCE_LOCAL: "true",
    NO_PROXY: bypass,
    no_proxy: bypass,
  };
}

function startVinext(temporaryRoot, dataDirectory, port) {
  const child = spawn(
    process.execPath,
    [vinextCli, "dev", "--hostname", "127.0.0.1", "--port", String(port)],
    {
      cwd: repositoryRoot,
      env: loopbackEnvironment(temporaryRoot, dataDirectory, port),
      detached: process.platform !== "win32",
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  let diagnosticLog = "";
  let baseUrl = null;
  let exit = null;
  let spawnError = null;

  const record = (chunk) => {
    diagnosticLog = `${diagnosticLog}${chunk.toString("utf8")}`.slice(-MAX_DIAGNOSTIC_LOG);
    const plain = diagnosticLog.replace(ANSI_ESCAPE, "");
    const match = /Local:\s+http:\/\/127\.0\.0\.1:(\d+)\/?/.exec(plain);
    if (match) baseUrl = `http://127.0.0.1:${match[1]}`;
  };
  child.stdout.on("data", record);
  child.stderr.on("data", record);
  child.once("error", (error) => {
    spawnError = error;
  });
  child.once("exit", (code, signal) => {
    exit = { code, signal };
  });

  return {
    child,
    get baseUrl() {
      return baseUrl;
    },
    get diagnostics() {
      return diagnosticLog.replace(ANSI_ESCAPE, "");
    },
    get failure() {
      if (spawnError) return `Vinext failed to spawn: ${spawnError.message}`;
      if (exit) return `Vinext exited early (${exit.code ?? exit.signal})`;
      return null;
    },
  };
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function listenOnRandomLoopback(server) {
  await new Promise((resolve, reject) => {
    const onError = (error) => reject(error);
    server.once("error", onError);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", onError);
      resolve();
    });
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("failed to allocate a loopback test port");
  }
  return address.port;
}

async function closeServer(server) {
  if (!server.listening) return;
  await new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

async function availableLoopbackPort() {
  const server = createServer();
  try {
    return await listenOnRandomLoopback(server);
  } finally {
    await closeServer(server);
  }
}

async function startDecoyServer() {
  const requests = [];
  const server = createServer((request, response) => {
    requests.push({ url: request.url, headers: request.headers });
    response.statusCode = 418;
    response.end("decoy");
  });
  const port = await listenOnRandomLoopback(server);
  return { server, port, requests };
}

async function rawHttpRequest(baseUrl, path, headers = {}) {
  const target = new URL(path, baseUrl);
  return new Promise((resolve, reject) => {
    const request = httpRequest(
      {
        hostname: target.hostname,
        port: target.port,
        path: `${target.pathname}${target.search}`,
        method: "GET",
        headers,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          resolve({
            status: response.statusCode,
            body: Buffer.concat(chunks).toString("utf8"),
          });
        });
      },
    );
    request.setTimeout(5_000, () => request.destroy(new Error("raw HTTP request timed out")));
    request.once("error", reject);
    request.end();
  });
}

async function waitUntil(description, runtime, probe, timeout) {
  const deadline = Date.now() + timeout;
  let lastError = null;
  while (Date.now() < deadline) {
    if (runtime.failure) {
      throw new Error(`${runtime.failure}\n${runtime.diagnostics}`);
    }
    try {
      const value = await probe();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await delay(250);
  }
  throw new Error(
    [
      `Timed out waiting for ${description}`,
      lastError instanceof Error ? lastError.message : null,
      runtime.diagnostics,
    ]
      .filter(Boolean)
      .join("\n"),
  );
}

async function request(baseUrl, path, accept = "application/json", timeout = 30_000) {
  return fetch(new URL(path, baseUrl), {
    cache: "no-store",
    redirect: "manual",
    headers: { Accept: accept },
    signal: AbortSignal.timeout(timeout),
  });
}

async function postJson(baseUrl, path, payload, timeout = 30_000) {
  return fetch(new URL(path, baseUrl), {
    method: "POST",
    cache: "no-store",
    redirect: "manual",
    headers: {
      Accept: "application/json",
      "content-type": "application/json",
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(timeout),
  });
}

async function waitForServer(runtime, timeout = 90_000) {
  return waitUntil(
    "Vinext development URL",
    runtime,
    async () => runtime.baseUrl,
    timeout,
  );
}

async function waitForHealthyGeneration(runtime, baseUrl, generationId, timeout = 30_000) {
  return waitUntil(
    `healthy generation ${generationId}`,
    runtime,
    async () => {
      const response = await request(baseUrl, "/api/health", "application/json", 5_000);
      if (response.status !== 200) return null;
      const payload = await response.json();
      return payload.status === "healthy" && payload.generationId === generationId
        ? { response, payload }
        : null;
    },
    timeout,
  );
}

async function waitForUnhealthyGeneration(runtime, baseUrl, timeout = 20_000) {
  return waitUntil(
    "fail-closed unhealthy generation",
    runtime,
    async () => {
      const response = await request(baseUrl, "/api/health", "application/json", 5_000);
      if (response.status === 200) return null;
      const payload = await response.json().catch(() => null);
      return { response, payload };
    },
    timeout,
  );
}

function rollback(dataDirectory, generationId) {
  runPython([
    "-m",
    "pipeline.generations",
    "--data-dir",
    dataDirectory,
    "rollback",
    generationId,
  ]);
}

async function waitForExit(child, timeout) {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  await Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    delay(timeout),
  ]);
  return child.exitCode !== null || child.signalCode !== null;
}

async function stopProcessTree(child) {
  if (!child?.pid || child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      encoding: "utf8",
      timeout: 15_000,
      windowsHide: true,
    });
    if (!(await waitForExit(child, 10_000))) {
      throw new Error(`Vinext process tree ${child.pid} did not exit after taskkill`);
    }
    return;
  }

  try {
    process.kill(-child.pid, "SIGTERM");
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
  }
  await waitForExit(child, 5_000);
  if (child.exitCode === null && child.signalCode === null) {
    try {
      process.kill(-child.pid, "SIGKILL");
    } catch (error) {
      if (error?.code !== "ESRCH") throw error;
    }
    if (!(await waitForExit(child, 5_000))) {
      throw new Error(`Vinext process group ${child.pid} did not exit after SIGKILL`);
    }
  }
}

function assertGenerationMarkers(html, expectedCatalog, expectedSignal, rejectedCatalog, rejectedSignal) {
  assert.match(html, new RegExp(expectedCatalog));
  assert.match(html, new RegExp(expectedSignal));
  assert.doesNotMatch(html, new RegExp(rejectedCatalog));
  assert.doesNotMatch(html, new RegExp(rejectedSignal));
}

test(
  "serves one verified generation per real Vinext request and recovers without restart",
  { timeout: 300_000 },
  async () => {
    const temporaryRoot = await mkdtemp(join(tmpdir(), "rardar-vinext-http-"));
    const dataDirectory = join(temporaryRoot, "data");
    let runtime = null;
    try {
      const fixture = parseLastJsonLine(
        runPython([
          fixtureHelper,
          "prepare",
          "--source-data",
          join(repositoryRoot, "data"),
          "--target-data",
          dataDirectory,
        ]),
      );

      const vinextPort = await availableLoopbackPort();
      runtime = startVinext(temporaryRoot, dataDirectory, vinextPort);
      const originalPid = runtime.child.pid;
      const baseUrl = await waitForServer(runtime);
      const initialHealth = await waitForHealthyGeneration(
        runtime,
        baseUrl,
        fixture.generationA,
        90_000,
      );

      const decoy = await startDecoyServer();
      try {
        const forgedHostHealth = await rawHttpRequest(baseUrl, "/api/health", {
          Accept: "application/json",
          Host: `127.0.0.1:${decoy.port}`,
        });
        assert.equal(forgedHostHealth.status, 200);
        assert.equal(JSON.parse(forgedHostHealth.body).generationId, fixture.generationA);
        await delay(250);
        assert.deepEqual(
          decoy.requests,
          [],
          "an inbound Host header must not select the token-bearing bridge target",
        );
      } finally {
        await closeServer(decoy.server);
      }

      const homeA = await request(baseUrl, "/", "text/html");
      assert.equal(homeA.status, 200);
      const homeAText = await homeA.text();
      assertGenerationMarkers(
        homeAText,
        fixture.catalogMarkerA,
        fixture.signalMarkerA,
        fixture.catalogMarkerB,
        fixture.signalMarkerB,
      );

      const signals = await request(baseUrl, "/signals", "text/html");
      assert.equal(signals.status, 200);
      const search = await request(baseUrl, "/search", "text/html");
      assert.equal(search.status, 200);

      const d1Response = await request(
        baseUrl,
        "/api/actions?deviceId=vinext-http-test",
        "application/json",
      );
      assert.equal(d1Response.status, 200);
      const d1Payload = await d1Response.json();
      assert.ok(Array.isArray(d1Payload.actions));

      const actionDeviceId = "vinext-action-events";
      const recommendationPath = `/api/recommendations?deviceId=${encodeURIComponent(actionDeviceId)}`;
      const recommendationsBeforeResponse = await request(baseUrl, recommendationPath);
      assert.equal(recommendationsBeforeResponse.status, 200);
      const recommendationsBefore = await recommendationsBeforeResponse.json();
      const sharedAttempt = {
        deviceId: actionDeviceId,
        projectSlug: fixture.projectSlug,
        action: "tried",
        idempotencyKey: "vinext-concurrent-attempt-0001",
      };
      const concurrentResponses = await Promise.all(
        Array.from({ length: 8 }, () => postJson(baseUrl, "/api/actions", sharedAttempt)),
      );
      assert.deepEqual(concurrentResponses.map((response) => response.status), Array(8).fill(200));
      const concurrentPayloads = await Promise.all(concurrentResponses.map((response) => response.json()));
      assert.equal(concurrentPayloads.filter((payload) => payload.recorded).length, 1);
      assert.equal(concurrentPayloads.filter((payload) => payload.idempotentReplay).length, 7);

      const conflictingReplay = await postJson(baseUrl, "/api/actions", {
        ...sharedAttempt,
        action: "cloned",
      });
      assert.equal(conflictingReplay.status, 409);

      const repeatedAction = await postJson(baseUrl, "/api/actions", {
        ...sharedAttempt,
        idempotencyKey: "vinext-concurrent-attempt-0002",
      });
      assert.equal(repeatedAction.status, 200);
      assert.equal((await repeatedAction.json()).recorded, true);

      const stageResponses = await Promise.all(
        ["opened", "saved", "cloned", "reused"].map((action, index) => postJson(baseUrl, "/api/actions", {
          deviceId: actionDeviceId,
          projectSlug: fixture.projectSlug,
          action,
          idempotencyKey: `vinext-stage-${action}-000${index}`,
        })),
      );
      assert.deepEqual(stageResponses.map((response) => response.status), [200, 200, 200, 200]);

      const actionStateResponse = await request(
        baseUrl,
        `/api/actions?deviceId=${encodeURIComponent(actionDeviceId)}&projectSlug=${encodeURIComponent(fixture.projectSlug)}`,
      );
      assert.equal(actionStateResponse.status, 200);
      const actionState = await actionStateResponse.json();
      assert.equal(actionState.states.length, 1);
      assert.equal(actionState.states[0].highestStage, "reused");
      assert.deepEqual(
        actionState.actions.map((item) => item.action).sort(),
        ["cloned", "opened", "reused", "saved", "tried"],
      );

      const metricsResponse = await request(
        baseUrl,
        `/api/metrics?deviceId=${encodeURIComponent(actionDeviceId)}`,
      );
      assert.equal(metricsResponse.status, 200);
      const metricsPayload = await metricsResponse.json();
      assert.equal(metricsPayload.northStar.value, 1);
      assert.equal(metricsPayload.week.triedProjects, 1);
      assert.equal(metricsPayload.week.clonedProjects, 1);
      assert.equal(metricsPayload.week.reusedProjects, 1);

      const recommendationsAfterResponse = await request(baseUrl, recommendationPath);
      assert.equal(recommendationsAfterResponse.status, 200);
      assert.deepEqual(await recommendationsAfterResponse.json(), recommendationsBefore);

      const forgedTime = await postJson(baseUrl, "/api/actions", {
        ...sharedAttempt,
        idempotencyKey: "vinext-forged-time-0001",
        occurredAt: "2099-01-01T00:00:00Z",
      });
      assert.equal(forgedTime.status, 400);

      rollback(dataDirectory, fixture.generationB);
      const switchedHealth = await waitForHealthyGeneration(
        runtime,
        baseUrl,
        fixture.generationB,
      );
      const homeB = await request(baseUrl, "/", "text/html");
      assert.equal(homeB.status, 200);
      assertGenerationMarkers(
        await homeB.text(),
        fixture.catalogMarkerB,
        fixture.signalMarkerB,
        fixture.catalogMarkerA,
        fixture.signalMarkerA,
      );
      assert.equal(runtime.child.pid, originalPid);
      assert.equal(runtime.child.exitCode, null);

      runPython([fixtureHelper, "corrupt-pointer", "--data-dir", dataDirectory]);
      const unhealthy = await waitForUnhealthyGeneration(runtime, baseUrl);
      assert.equal(unhealthy.response.status, 503);
      assert.equal(unhealthy.payload?.status, "degraded");
      const failedHome = await request(baseUrl, "/", "text/html");
      assert.ok(failedHome.status >= 500);
      assert.doesNotMatch(await failedHome.text(), new RegExp(fixture.flatMarker));
      assert.equal(runtime.child.pid, originalPid);
      assert.equal(runtime.child.exitCode, null);

      rollback(dataDirectory, fixture.generationA);
      const recoveredHealth = await waitForHealthyGeneration(
        runtime,
        baseUrl,
        fixture.generationA,
      );
      const recoveredHome = await request(baseUrl, "/", "text/html");
      assert.equal(recoveredHome.status, 200);
      assertGenerationMarkers(
        await recoveredHome.text(),
        fixture.catalogMarkerA,
        fixture.signalMarkerA,
        fixture.catalogMarkerB,
        fixture.signalMarkerB,
      );
      assert.equal(runtime.child.pid, originalPid);
      assert.equal(runtime.child.exitCode, null);

      console.log(
        [
          `GET /: ${homeA.status}`,
          `GET /api/health: ${initialHealth.response.status}`,
          `GET /signals: ${signals.status}`,
          `GET /search: ${search.status}`,
          `current generation: ${initialHealth.payload.generationId}`,
          `generation after pointer switch: ${switchedHealth.payload.generationId}`,
          `damaged current status: health ${unhealthy.response.status}, home ${failedHome.status}`,
          `rollback status: health ${recoveredHealth.response.status}, home ${recoveredHome.status}`,
          `D1 API acceptance: ${d1Response.status}`,
          `action API concurrency: ${concurrentResponses.length} requests, 1 Event`,
          `action State highest stage: ${actionState.states[0].highestStage}`,
          `Weekly Acted Projects: ${metricsPayload.northStar.value}`,
          "recommendation regression: unchanged",
        ].join("\n"),
      );
    } finally {
      try {
        await stopProcessTree(runtime?.child);
      } finally {
        await rm(temporaryRoot, {
          recursive: true,
          force: true,
          maxRetries: 5,
          retryDelay: 200,
        });
      }
    }
  },
);
