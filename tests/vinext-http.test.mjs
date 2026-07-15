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

function visibleServerHtml(html) {
  return html
    .replaceAll(/<script\b[\s\S]*?<\/script>/gi, "")
    .replaceAll(/<style\b[\s\S]*?<\/style>/gi, "")
    .replaceAll(/<!--[\s\S]*?-->/g, "");
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
      assert.match(homeAText, /关注优先级/);
      assert.match(homeAText, /静态工程就绪度/);
      assert.doesNotMatch(homeAText, /全球影响力|复用价值|建议：复用/);

      const signals = await request(baseUrl, "/signals", "text/html");
      assert.equal(signals.status, 200);
      const search = await request(baseUrl, "/search", "text/html");
      assert.equal(search.status, 200);
      const searchText = await search.text();
      assert.match(searchText, /任务匹配/);
      assert.match(searchText, /\/100/);
      assert.doesNotMatch(searchText, /复用价值/);

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
      assert.ok(recommendationsBefore.recommendations.every((item) => Number.isFinite(item.baseScore)));
      assert.doesNotMatch(JSON.stringify(recommendationsBefore), /复用价值|全球影响力/);
      const sharedAttempt = {
        deviceId: actionDeviceId,
        projectIdVersion: 1,
        projectId: fixture.projectId,
        action: "tried",
        idempotencyKey: "vinext-concurrent-attempt-0001",
      };
      const serverTimeLowerBound = Date.now() - 2_000;
      const concurrentResponses = await Promise.all(
        Array.from({ length: 8 }, () => postJson(baseUrl, "/api/actions", sharedAttempt)),
      );
      const serverTimeUpperBound = Date.now() + 2_000;
      assert.deepEqual(concurrentResponses.map((response) => response.status), Array(8).fill(200));
      const concurrentPayloads = await Promise.all(concurrentResponses.map((response) => response.json()));
      assert.equal(concurrentPayloads.filter((payload) => payload.recorded).length, 1);
      assert.equal(concurrentPayloads.filter((payload) => payload.idempotentReplay).length, 7);
      assert.ok(concurrentPayloads.every((payload) => payload.projectIdVersion === 1));
      assert.ok(concurrentPayloads.every((payload) => payload.projectId === fixture.projectId));
      assert.ok(concurrentPayloads.every((payload) => payload.projectSlug === fixture.projectSlug));
      assert.ok(concurrentPayloads.every((payload) => {
        const occurredAt = Date.parse(payload.event?.occurredAt);
        return Number.isFinite(occurredAt)
          && occurredAt >= serverTimeLowerBound
          && occurredAt <= serverTimeUpperBound;
      }), "Action occurredAt must be generated by the server during this request window");

      const legacyReplay = await postJson(baseUrl, "/api/actions", {
        deviceId: actionDeviceId,
        projectSlug: fixture.projectSlug,
        action: "tried",
        idempotencyKey: sharedAttempt.idempotencyKey,
      });
      assert.equal(legacyReplay.status, 200);
      assert.equal((await legacyReplay.json()).idempotentReplay, true);

      const conflictingReplay = await postJson(baseUrl, "/api/actions", {
        ...sharedAttempt,
        action: "cloned",
      });
      assert.equal(conflictingReplay.status, 409);

      const repeatedAction = await postJson(baseUrl, "/api/actions", {
        deviceId: actionDeviceId,
        projectSlug: fixture.projectSlug,
        action: "tried",
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

      const consistentDualSelector = await postJson(baseUrl, "/api/actions", {
        deviceId: actionDeviceId,
        projectIdVersion: 1,
        projectId: fixture.projectId,
        projectSlug: fixture.projectSlug,
        action: "opened",
        idempotencyKey: "vinext-consistent-dual-0001",
      });
      assert.equal(consistentDualSelector.status, 200);

      const mismatchedDualSelector = await postJson(baseUrl, "/api/actions", {
        deviceId: actionDeviceId,
        projectIdVersion: 1,
        projectId: fixture.secondProjectId,
        projectSlug: fixture.projectSlug,
        action: "opened",
        idempotencyKey: "vinext-mismatched-dual-0001",
      });
      assert.equal(mismatchedDualSelector.status, 409);
      assert.equal((await mismatchedDualSelector.json()).error, "project_identity_conflict");

      const forgedProjectId = `${fixture.projectId.slice(0, -1)}${fixture.projectId.endsWith("0") ? "1" : "0"}`;
      const forgedProject = await postJson(baseUrl, "/api/actions", {
        ...sharedAttempt,
        projectId: forgedProjectId,
        idempotencyKey: "vinext-forged-project-0001",
      });
      assert.equal(forgedProject.status, 404);
      assert.equal((await forgedProject.json()).error, "unknown_project_id");

      const wrongProjectIdVersion = await postJson(baseUrl, "/api/actions", {
        ...sharedAttempt,
        projectIdVersion: 2,
        idempotencyKey: "vinext-wrong-version-0001",
      });
      assert.equal(wrongProjectIdVersion.status, 400);
      assert.equal((await wrongProjectIdVersion.json()).error, "unsupported_project_id_version");

      const crossProjectReplay = await postJson(baseUrl, "/api/actions", {
        ...sharedAttempt,
        projectId: fixture.secondProjectId,
      });
      assert.equal(crossProjectReplay.status, 409);

      const actionStateResponse = await request(
        baseUrl,
        `/api/actions?deviceId=${encodeURIComponent(actionDeviceId)}&projectIdVersion=1&projectId=${encodeURIComponent(fixture.projectId)}`,
      );
      assert.equal(actionStateResponse.status, 200);
      const actionState = await actionStateResponse.json();
      assert.equal(actionState.states.length, 1);
      assert.equal(actionState.states[0].highestStage, "reused");
      assert.equal(actionState.states[0].projectIdVersion, 1);
      assert.equal(actionState.states[0].projectId, fixture.projectId);
      assert.equal(actionState.states[0].projectSlug, fixture.projectSlug);
      assert.deepEqual(
        actionState.actions.map((item) => item.action).sort(),
        ["cloned", "opened", "reused", "saved", "tried"],
      );
      assert.ok(actionState.actions.every((item) => item.projectId === fixture.projectId));
      const legacyActionState = await request(
        baseUrl,
        `/api/actions?deviceId=${encodeURIComponent(actionDeviceId)}&projectSlug=${encodeURIComponent(fixture.projectSlug)}`,
      );
      assert.equal(legacyActionState.status, 200);
      assert.deepEqual(await legacyActionState.json(), actionState);

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

      const canonicalFeedback = await postJson(baseUrl, "/api/feedback", {
        deviceId: actionDeviceId,
        projectIdVersion: 1,
        projectId: fixture.projectId,
        value: "有用",
      });
      assert.equal(canonicalFeedback.status, 200);
      const canonicalFeedbackPayload = await canonicalFeedback.json();
      assert.equal(canonicalFeedbackPayload.changed, true);
      assert.equal(canonicalFeedbackPayload.projectId, fixture.projectId);
      assert.equal(canonicalFeedbackPayload.projectSlug, fixture.projectSlug);

      const legacyFeedback = await request(
        baseUrl,
        `/api/feedback?deviceId=${encodeURIComponent(actionDeviceId)}&projectSlug=${encodeURIComponent(fixture.projectSlug)}`,
      );
      assert.equal(legacyFeedback.status, 200);
      const legacyFeedbackPayload = await legacyFeedback.json();
      assert.equal(legacyFeedbackPayload.feedback.projectIdVersion, 1);
      assert.equal(legacyFeedbackPayload.feedback.projectId, fixture.projectId);
      assert.equal(legacyFeedbackPayload.feedback.projectSlug, fixture.projectSlug);
      assert.equal(legacyFeedbackPayload.feedback.value, "有用");

      const feedbackReplay = await postJson(baseUrl, "/api/feedback", {
        deviceId: actionDeviceId,
        projectSlug: fixture.projectSlug,
        value: "有用",
      });
      assert.equal(feedbackReplay.status, 200);
      assert.equal((await feedbackReplay.json()).changed, false);

      const personalizedResponse = await request(baseUrl, recommendationPath);
      assert.equal(personalizedResponse.status, 200);
      const personalized = await personalizedResponse.json();
      assert.equal(personalized.personalized, true);
      assert.equal(personalized.feedbackCount, 1);
      assert.ok(personalized.recommendations.every((item) => item.projectIdVersion === 1));
      assert.ok(personalized.recommendations.every((item) => typeof item.projectId === "string"));
      assert.ok(personalized.recommendations.every((item) => typeof item.slug === "string"));

      const feedbackMetricsResponse = await request(
        baseUrl,
        `/api/metrics?deviceId=${encodeURIComponent(actionDeviceId)}`,
      );
      assert.equal(feedbackMetricsResponse.status, 200);
      const feedbackMetrics = await feedbackMetricsResponse.json();
      assert.equal(feedbackMetrics.current.useful, 1);
      assert.equal(feedbackMetrics.current.total, 1);
      assert.equal(feedbackMetrics.week.feedbackDecisions, 1);
      assert.equal(feedbackMetrics.week.feedbackChanges, 1);

      const removedProjectAction = await postJson(baseUrl, "/api/actions", {
        deviceId: actionDeviceId,
        projectIdVersion: 1,
        projectId: fixture.removedProjectId,
        projectSlug: fixture.removedProjectSlug,
        action: "tried",
        idempotencyKey: "vinext-removed-project-tried-0001",
      });
      assert.equal(removedProjectAction.status, 200);
      const removedProjectFeedback = await postJson(baseUrl, "/api/feedback", {
        deviceId: actionDeviceId,
        projectIdVersion: 1,
        projectId: fixture.removedProjectId,
        projectSlug: fixture.removedProjectSlug,
        value: "\u590d\u7528",
      });
      assert.equal(removedProjectFeedback.status, 200);

      const forgedTime = await postJson(baseUrl, "/api/actions", {
        ...sharedAttempt,
        idempotencyKey: "vinext-forged-time-0001",
        occurredAt: "2099-01-01T00:00:00Z",
      });
      assert.equal(forgedTime.status, 400);
      assert.equal((await forgedTime.json()).error, "client_project_evidence_not_allowed");

      const forgedRepository = await postJson(baseUrl, "/api/actions", {
        ...sharedAttempt,
        idempotencyKey: "vinext-forged-repository-0001",
        repository: fixture.projectRepository,
      });
      assert.equal(forgedRepository.status, 400);
      assert.equal((await forgedRepository.json()).error, "client_project_evidence_not_allowed");
      const forgedRepoAlias = await postJson(baseUrl, "/api/actions", {
        ...sharedAttempt,
        idempotencyKey: "vinext-forged-repo-alias-0001",
        repo: fixture.projectRepository,
      });
      assert.equal(forgedRepoAlias.status, 400);
      assert.equal((await forgedRepoAlias.json()).error, "client_project_evidence_not_allowed");

      rollback(dataDirectory, fixture.generationB);
      const switchedHealth = await waitForHealthyGeneration(
        runtime,
        baseUrl,
        fixture.generationB,
      );
      const homeB = await request(baseUrl, "/", "text/html");
      assert.equal(homeB.status, 200);
      const homeBText = await homeB.text();
      assertGenerationMarkers(
        homeBText,
        fixture.catalogMarkerB,
        fixture.signalMarkerB,
        fixture.catalogMarkerA,
        fixture.signalMarkerA,
      );
      assert.equal(runtime.child.pid, originalPid);
      assert.equal(runtime.child.exitCode, null);
      const switchedGenerationAction = await postJson(baseUrl, "/api/actions", {
        deviceId: "vinext-generation-switch",
        projectIdVersion: 1,
        projectId: fixture.projectId,
        projectSlug: fixture.projectSlug,
        action: "saved",
        idempotencyKey: "vinext-generation-b-saved-0001",
      });
      assert.equal(switchedGenerationAction.status, 200);
      const switchedGenerationActionPayload = await switchedGenerationAction.json();
      assert.equal(switchedGenerationActionPayload.projectId, fixture.projectId);
      assert.equal(
        switchedGenerationActionPayload.event.catalogGenerationId,
        fixture.generationB,
      );

      rollback(dataDirectory, fixture.generationWithRemoval);
      const removalHealth = await waitForHealthyGeneration(
        runtime,
        baseUrl,
        fixture.generationWithRemoval,
      );
      const currentActionsAfterRemoval = await request(
        baseUrl,
        `/api/actions?deviceId=${encodeURIComponent(actionDeviceId)}`,
      );
      assert.equal(currentActionsAfterRemoval.status, 200);
      const currentActionsAfterRemovalPayload = await currentActionsAfterRemoval.json();
      assert.ok(currentActionsAfterRemovalPayload.states.length >= 1);
      assert.ok(currentActionsAfterRemovalPayload.states.every(
        (item) => item.projectId !== fixture.removedProjectId,
      ));
      assert.ok(currentActionsAfterRemovalPayload.actions.every(
        (item) => item.projectId !== fixture.removedProjectId,
      ));

      const currentFeedbackAfterRemoval = await request(
        baseUrl,
        `/api/feedback?deviceId=${encodeURIComponent(actionDeviceId)}`,
      );
      assert.equal(currentFeedbackAfterRemoval.status, 200);
      const currentFeedbackAfterRemovalPayload = await currentFeedbackAfterRemoval.json();
      assert.equal(currentFeedbackAfterRemovalPayload.feedback.length, 1);
      assert.ok(currentFeedbackAfterRemovalPayload.feedback.every(
        (item) => item.projectId !== fixture.removedProjectId,
      ));

      const recommendationsAfterRemoval = await request(baseUrl, recommendationPath);
      assert.equal(recommendationsAfterRemoval.status, 200);
      const recommendationsAfterRemovalPayload = await recommendationsAfterRemoval.json();
      assert.equal(recommendationsAfterRemovalPayload.feedbackCount, 1);
      assert.doesNotMatch(JSON.stringify(recommendationsAfterRemovalPayload), new RegExp(fixture.removedProjectId));

      const metricsAfterRemoval = await request(
        baseUrl,
        `/api/metrics?deviceId=${encodeURIComponent(actionDeviceId)}`,
      );
      assert.equal(metricsAfterRemoval.status, 200);
      const metricsAfterRemovalPayload = await metricsAfterRemoval.json();
      assert.equal(metricsAfterRemovalPayload.current.total, 2);
      assert.equal(metricsAfterRemovalPayload.northStar.value, 2);
      assert.equal(metricsAfterRemovalPayload.week.triedProjects, 2);
      assert.equal(metricsAfterRemovalPayload.week.feedbackDecisions, 2);

      const removedProjectSelector = await request(
        baseUrl,
        `/api/actions?deviceId=${encodeURIComponent(actionDeviceId)}&projectIdVersion=1&projectId=${encodeURIComponent(fixture.removedProjectId)}`,
      );
      assert.equal(removedProjectSelector.status, 404);
      assert.equal((await removedProjectSelector.json()).error, "unknown_project_id");

      rollback(dataDirectory, fixture.legacyGeneration);
      const legacyHealth = await waitForHealthyGeneration(
        runtime,
        baseUrl,
        fixture.legacyGeneration,
      );
      const legacyHome = await request(baseUrl, "/", "text/html");
      assert.equal(legacyHome.status, 200);
      const legacyHomeText = await legacyHome.text();
      const legacyRenderedText = visibleServerHtml(legacyHomeText);
      assert.match(legacyRenderedText, /关注优先级/);
      assert.match(legacyRenderedText, /静态工程就绪度/);
      assert.match(legacyRenderedText, /建议：隔离试用/);
      assert.doesNotMatch(
        legacyRenderedText,
        /全球影响力|复用价值|建议：复用|建议：试用/,
      );
      assert.doesNotMatch(
        legacyHomeText,
        new RegExp(`${fixture.catalogMarkerA}|${fixture.catalogMarkerB}`),
      );
      assert.equal(runtime.child.pid, originalPid);
      assert.equal(runtime.child.exitCode, null);
      const retainedV1Action = await postJson(baseUrl, "/api/actions", {
        deviceId: "vinext-retained-v1",
        projectIdVersion: 1,
        projectId: fixture.legacyProjectId,
        projectSlug: fixture.legacyProjectSlug,
        action: "saved",
        idempotencyKey: "vinext-retained-v1-saved-0001",
      });
      assert.equal(retainedV1Action.status, 200);
      const retainedV1ActionPayload = await retainedV1Action.json();
      assert.equal(retainedV1ActionPayload.projectId, fixture.legacyProjectId);
      assert.equal(retainedV1ActionPayload.projectSlug, fixture.legacyProjectSlug);
      assert.equal(retainedV1ActionPayload.event.catalogGenerationId, fixture.legacyGeneration);

      rollback(dataDirectory, fixture.generationB);
      await waitForHealthyGeneration(runtime, baseUrl, fixture.generationB);
      const restoredV2Home = await request(baseUrl, "/", "text/html");
      assert.equal(restoredV2Home.status, 200);
      assertGenerationMarkers(
        await restoredV2Home.text(),
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
          `generation after project removal: ${removalHealth.payload.generationId}`,
          `retained v1 generation: ${legacyHealth.payload.generationId}`,
          `damaged current status: health ${unhealthy.response.status}, home ${failedHome.status}`,
          `rollback status: health ${recoveredHealth.response.status}, home ${recoveredHome.status}`,
          `D1 API acceptance: ${d1Response.status}`,
          `action API concurrency: ${concurrentResponses.length} requests, 1 Event`,
          `action State highest stage: ${actionState.states[0].highestStage}`,
          `Weekly Acted Projects: ${metricsPayload.northStar.value}`,
          `retired project weekly continuity: ${metricsAfterRemovalPayload.northStar.value}`,
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
