import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  readlink,
  realpath,
  rm,
} from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const repositoryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const python = resolvePythonExecutable(process.env.RARDAR_PYTHON || "python");
const fixtureHelper = join(repositoryRoot, "tests", "http_generation_fixture.py");
const networkInterfacesProbe = join(
  repositoryRoot,
  "scripts",
  "systemd-network-interfaces-probe.mjs",
);
const cleanupTimeout = 80_000;

function parseUnitDirectives(unit) {
  const directives = new Map();
  let section = null;
  for (const rawLine of unit.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || line.startsWith(";")) continue;
    const sectionMatch = line.match(/^\[([^\]]+)\]$/);
    if (sectionMatch) {
      section = sectionMatch[1];
      continue;
    }
    const separator = line.indexOf("=");
    if (!section || separator < 1) continue;
    const name = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim();
    const key = `${section}.${name}`;
    const values = directives.get(key) ?? [];
    values.push(value);
    directives.set(key, values);
  }
  return directives;
}

function requireSingleDirective(directives, section, name) {
  const values = directives.get(`${section}.${name}`) ?? [];
  assert.equal(values.length, 1, `${section}.${name} must have one authoritative definition`);
  return values[0];
}

function resolvePythonExecutable(command) {
  const completed = spawnSync(command, ["-c", "import sys; print(sys.executable)"], {
    encoding: "utf8",
    timeout: 10_000,
    windowsHide: true,
  });
  const executable = completed.stdout?.trim();
  if (completed.error || completed.status !== 0 || !executable) {
    throw new Error(
      ["could not resolve the Python interpreter", completed.error?.message, completed.stderr]
        .filter(Boolean)
        .join("\n"),
    );
  }
  return executable;
}

async function source(relativePath) {
  return readFile(new URL(`../${relativePath}`, import.meta.url), "utf8");
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function randomLoopbackPort() {
  while (true) {
    const server = createServer();
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const address = server.address();
    await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
    if (address && typeof address !== "string" && address.port !== 3000 && address.port !== 3002) {
      return address.port;
    }
  }
}

function futureSchedule() {
  for (const timezone of ["UTC", "Asia/Shanghai", "Pacific/Honolulu", "Europe/Berlin"]) {
    const parts = Object.fromEntries(
      new Intl.DateTimeFormat("en-GB", {
        hour: "2-digit",
        hourCycle: "h23",
        minute: "2-digit",
        timeZone: timezone,
      })
        .formatToParts(new Date())
        .filter((part) => part.type !== "literal")
        .map((part) => [part.type, part.value]),
    );
    const minutes = Number(parts.hour) * 60 + Number(parts.minute);
    if (minutes <= 23 * 60 + 40) {
      const target = minutes + 15;
      return {
        at: `${String(Math.floor(target / 60)).padStart(2, "0")}:${String(target % 60).padStart(2, "0")}`,
        timezone,
      };
    }
  }
  throw new Error("could not choose a future scheduler time for the isolated rehearsal");
}

function prepareFixture(targetData) {
  const completed = spawnSync(
    python,
    [fixtureHelper, "prepare", "--source-data", join(repositoryRoot, "data"), "--target-data", targetData],
    {
      cwd: repositoryRoot,
      encoding: "utf8",
      maxBuffer: 10 * 1024 * 1024,
      timeout: 180_000,
      windowsHide: true,
    },
  );
  if (completed.error || completed.status !== 0) {
    throw new Error(
      ["fixture preparation failed", completed.error?.message, completed.stdout, completed.stderr]
        .filter(Boolean)
        .join("\n"),
    );
  }
  return JSON.parse(completed.stdout.trim().split(/\r?\n/).filter(Boolean).at(-1));
}

async function sha256(path) {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}

async function comparablePath(path) {
  const canonical = await realpath(path);
  return process.platform === "win32" ? canonical.toLowerCase() : canonical;
}

async function releaseTreeSnapshot(root) {
  let rootMetadata;
  try {
    rootMetadata = await lstat(root);
  } catch (error) {
    if (error?.code === "ENOENT") return { exists: false, entries: [] };
    throw error;
  }
  if (rootMetadata.isSymbolicLink()) {
    return { exists: true, type: "symlink", target: await readlink(root), entries: [] };
  }
  if (!rootMetadata.isDirectory()) {
    return {
      exists: true,
      type: rootMetadata.isFile() ? "file" : "other",
      sha256: rootMetadata.isFile() ? await sha256(root) : null,
      entries: [],
    };
  }
  const entries = [];
  const visit = async (directory, prefix = "") => {
    const children = await readdir(directory, { withFileTypes: true });
    children.sort((left, right) => left.name.localeCompare(right.name));
    for (const child of children) {
      const relativePath = prefix ? `${prefix}/${child.name}` : child.name;
      const absolutePath = join(directory, child.name);
      if (child.isSymbolicLink()) {
        entries.push({ path: relativePath, type: "symlink", target: await readlink(absolutePath) });
      } else if (child.isDirectory()) {
        entries.push({ path: relativePath, type: "directory" });
        await visit(absolutePath, relativePath);
      } else if (child.isFile()) {
        entries.push({ path: relativePath, type: "file", sha256: await sha256(absolutePath) });
      } else {
        entries.push({ path: relativePath, type: "other" });
      }
    }
  };
  await visit(root);
  return { exists: true, type: "directory", entries };
}

async function releaseWriteTargetSnapshot() {
  return {
    ".vinext": await releaseTreeSnapshot(join(repositoryRoot, ".vinext")),
    ".wrangler": await releaseTreeSnapshot(join(repositoryRoot, ".wrangler")),
    "node_modules/.vite-temp": await releaseTreeSnapshot(
      join(repositoryRoot, "node_modules", ".vite-temp"),
    ),
  };
}

async function candidateSnapshot(dataDirectory) {
  const candidatesRoot = join(dataDirectory, "generations", ".candidates");
  let entries;
  try {
    entries = await readdir(candidatesRoot, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") {
      return { allCandidates: [], failedCandidateCount: 0, failedCandidates: [] };
    }
    throw error;
  }
  const allCandidates = entries.map((entry) => entry.name).sort();
  const failedCandidates = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    try {
      const manifest = JSON.parse(
        await readFile(join(candidatesRoot, entry.name, "manifest.json"), "utf8"),
      );
      if (manifest?.state === "failed") failedCandidates.push(entry.name);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
  failedCandidates.sort();
  return {
    allCandidates,
    failedCandidateCount: failedCandidates.length,
    failedCandidates,
  };
}

function inspectRardarSqlite(stateDirectory, environment) {
  const script = String.raw`
import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
fingerprint = {"feedback", "decision_events", "project_actions"}
sqlite_files = []
rardar_databases = []
for path in sorted(root.rglob("*")):
    if path.is_symlink() or not path.is_file():
        continue
    try:
        with path.open("rb") as stream:
            header = stream.read(16)
        if header != b"SQLite format 3\x00":
            continue
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            tables = sorted(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            )
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as error:
        raise RuntimeError(f"failed to inspect {path}: {error}") from error
    relative = path.relative_to(root).as_posix()
    sqlite_files.append(relative)
    if fingerprint.issubset(tables):
        rardar_databases.append({"path": relative, "tables": tables})
print(json.dumps({
    "sqliteFileCount": len(sqlite_files),
    "sqliteFiles": sqlite_files,
    "rardarDatabases": rardar_databases,
}))
`;
  const completed = spawnSync(python, ["-c", script, stateDirectory], {
    cwd: repositoryRoot,
    encoding: "utf8",
    env: environment,
    maxBuffer: 10 * 1024 * 1024,
    timeout: 30_000,
    windowsHide: true,
  });
  if (completed.error || completed.status !== 0) {
    throw new Error(
      ["temporary D1 inspection failed", completed.error?.message, completed.stdout, completed.stderr]
        .filter(Boolean)
        .join("\n"),
    );
  }
  return JSON.parse(completed.stdout.trim().split(/\r?\n/).filter(Boolean).at(-1));
}

function runOnlineDeploymentCheck(environment) {
  const wrapper = String.raw`
import os
from pathlib import Path
from pipeline import deployment

# The production CLI is intentionally pinned to the systemd v1 filesystem.
# This process-level test runs from a mutable source checkout, which is
# deliberately not a CI release artifact. Release manifest/content checks have
# their own Python integration suite; keep every Runtime/data/D1/HTTP gate real
# here while replacing only that impossible source-checkout gate.
names = tuple(deployment.CANONICAL_SYSTEMD_PATHS)
deployment.CANONICAL_SYSTEMD_PATHS.clear()
deployment.CANONICAL_SYSTEMD_PATHS.update({
    name: Path(os.environ[name]).expanduser().absolute()
    for name in names
})
def isolated_source_release(home):
    if home.resolve(strict=True) != deployment.APPLICATION_ROOT.resolve(strict=True):
        raise RuntimeError("isolated release root differs from the running source tree")
    return {
        "requiredPathCount": len(deployment.REQUIRED_RELEASE_PATHS),
        "artifact": {"status": "isolated_source_tree_not_an_artifact"},
    }
deployment._check_release = isolated_source_release
raise SystemExit(deployment.main(["check", "--online"]))
`;
  const completed = spawnSync(
    python,
    ["-B", "-c", wrapper],
    {
      cwd: repositoryRoot,
      encoding: "utf8",
      env: environment,
      maxBuffer: 10 * 1024 * 1024,
      timeout: 120_000,
      windowsHide: true,
    },
  );
  if (completed.error) {
    throw new Error(`online deployment check failed to execute: ${completed.error.message}`);
  }
  const line = completed.stdout.trim().split(/\r?\n/).filter(Boolean).at(-1);
  if (!line) {
    throw new Error(`online deployment check returned no JSON\n${completed.stderr}`);
  }
  let payload;
  try {
    payload = JSON.parse(line);
  } catch (error) {
    throw new Error(`online deployment check returned invalid JSON: ${error}\n${completed.stdout}\n${completed.stderr}`);
  }
  return { completed, payload };
}

function startService(environment) {
  const child = spawn(python, ["-m", "pipeline.runtime", "service"], {
    cwd: repositoryRoot,
    detached: process.platform !== "win32",
    env: environment,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  let diagnostics = "";
  const record = (chunk) => {
    diagnostics = `${diagnostics}${chunk.toString("utf8")}`.slice(-64 * 1024);
  };
  child.stdout.on("data", record);
  child.stderr.on("data", record);
  return {
    child,
    logDirectory: join(environment.RARDAR_RUNTIME_DIR, "logs"),
    get diagnostics() { return diagnostics; },
  };
}

async function serviceDiagnostics(runtime) {
  const sections = [];
  if (runtime.diagnostics) sections.push(runtime.diagnostics);
  try {
    const entries = await readdir(runtime.logDirectory, { withFileTypes: true });
    for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
      if (!entry.isFile()) continue;
      const content = await readFile(join(runtime.logDirectory, entry.name), "utf8");
      sections.push(`${entry.name}:\n${content.slice(-64 * 1024)}`);
    }
  } catch (error) {
    if (error?.code !== "ENOENT") sections.push(`log read failed: ${error}`);
  }
  return sections.join("\n");
}

async function waitFor(description, runtime, probe, timeout = 90_000) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    if (runtime.child.exitCode !== null || runtime.child.signalCode !== null) {
      throw new Error(
        `${description}: manager exited (${runtime.child.exitCode ?? runtime.child.signalCode})\n${await serviceDiagnostics(runtime)}`,
      );
    }
    try {
      const result = await probe();
      if (result) return result;
    } catch (error) {
      lastError = error;
    }
    await delay(250);
  }
  throw new Error(
    `${description} timed out${lastError instanceof Error ? `: ${lastError.message}` : ""}\n${await serviceDiagnostics(runtime)}`,
  );
}

async function waitForExit(child, timeout) {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  await Promise.race([
    new Promise((resolve) => child.once("exit", resolve)),
    delay(timeout),
  ]);
  return child.exitCode !== null || child.signalCode !== null;
}

function forceStopOwnedTree(runtime) {
  if (!runtime?.child || runtime.child.exitCode !== null || runtime.child.signalCode !== null) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(runtime.child.pid), "/T", "/F"], {
      encoding: "utf8",
      timeout: 10_000,
      windowsHide: true,
    });
    return;
  }
  try {
    process.kill(-runtime.child.pid, "SIGKILL");
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
  }
}

async function stopService(runtime, environment) {
  if (!runtime?.child || runtime.child.exitCode !== null || runtime.child.signalCode !== null) return;
  if (process.platform === "win32") {
    spawnSync(python, ["-m", "pipeline.runtime", "stop"], {
      cwd: repositoryRoot,
      env: environment,
      encoding: "utf8",
      timeout: 15_000,
      windowsHide: true,
    });
  } else {
    runtime.child.kill("SIGTERM");
  }
  if (await waitForExit(runtime.child, 15_000)) return;
  forceStopOwnedTree(runtime);
  if (!(await waitForExit(runtime.child, 10_000))) {
    throw new Error(`isolated manager process tree ${runtime.child.pid} did not exit`);
  }
}

async function cleanupOwnedRuntimeTrees(runtimes, environment, temporaryRoot) {
  let timer;
  const cleanup = async () => {
    const failures = [];
    for (const runtime of [...runtimes].reverse()) {
      try {
        await stopService(runtime, environment);
      } catch (error) {
        failures.push(error);
        forceStopOwnedTree(runtime);
      }
    }
    try {
      await rm(temporaryRoot, { force: true, maxRetries: 3, recursive: true, retryDelay: 100 });
    } catch (error) {
      failures.push(error);
    }
    if (failures.length > 0) {
      throw new AggregateError(failures, "isolated deployment rehearsal cleanup failed");
    }
  };
  try {
    await Promise.race([
      cleanup(),
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          for (const runtime of runtimes) forceStopOwnedTree(runtime);
          reject(new Error(`isolated deployment rehearsal cleanup exceeded ${cleanupTimeout}ms`));
        }, cleanupTimeout);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function requestJson(url, path, timeout = 20_000) {
  const response = await fetch(new URL(path, url), {
    cache: "no-store",
    redirect: "manual",
    signal: AbortSignal.timeout(timeout),
  });
  const payload = await response.json().catch(() => null);
  return { response, payload };
}

async function postJson(url, path, body, timeout = 60_000) {
  const response = await fetch(new URL(path, url), {
    method: "POST",
    cache: "no-store",
    redirect: "manual",
    headers: {
      Accept: "application/json",
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeout),
  });
  const payload = await response.json().catch(() => null);
  return { response, payload };
}

test("Always-on deployment keeps one foreground manager behind loopback", async () => {
  const [unit, journal, runtime, example, systemdExample, layout, packageJson, verify] = await Promise.all([
    source("deploy/systemd/rardar.service"),
    source("deploy/systemd/60-rardar-journal.conf"),
    source("pipeline/runtime.py"),
    source(".env.production.example"),
    source("deploy/systemd/rardar.env.example"),
    source("app/layout.tsx"),
    source("package.json").then(JSON.parse),
    source("scripts/verify.mjs"),
  ]);

  assert.match(unit, /^Type=simple$/m);
  assert.match(unit, /^User=rardar$/m);
  assert.match(unit, /^ExecStart=.*-m pipeline\.runtime service$/m);
  assert.match(unit, /^ExecStartPre=.*-m pipeline\.deployment check --offline$/m);
  assert.match(unit, /^KillMode=control-group$/m);
  assert.match(unit, /^StandardOutput=journal$/m);
  assert.match(unit, /^StandardError=journal$/m);
  assert.match(unit, /^SyslogIdentifier=rardar$/m);
  assert.match(journal, /^Storage=persistent$/m);
  assert.match(journal, /^MaxRetentionSec=14day$/m);
  assert.match(journal, /^SystemMaxUse=3G$/m);
  assert.match(journal, /^SystemKeepFree=8G$/m);
  assert.match(unit, /^Restart=on-failure$/m);
  assert.match(unit, /^StartLimitIntervalSec=300$/m);
  assert.match(unit, /^StartLimitBurst=5$/m);
  assert.doesNotMatch(unit, /pipeline\.scheduler/);
  assert.doesNotMatch(unit, /local:start/);
  assert.doesNotMatch(layout, /next\/font\/(?:google|local)/);
  assert.match(runtime, /"service"/);
  assert.match(runtime, /"127\.0\.0\.1"/);
  assert.match(runtime, /"node_modules" \/ "vite" \/ "bin" \/ "vite\.js"/);
  assert.match(
    runtime,
    /"--configLoader",\s*"runner",\s*"--host",\s*RUNTIME_HOST,\s*"--port",\s*str\(resolved_layout\.vinext_port\),\s*"--strictPort"/,
  );
  assert.equal(packageJson.scripts["deploy:preflight"], "python -m pipeline.deployment check --offline");
  assert.equal(packageJson.scripts["deploy:check"], "python -m pipeline.deployment check --online");
  assert.match(verify, /systemd-analyze/);

  for (const name of [
    "RARDAR_HOME",
    "RARDAR_DATA_DIR",
    "RARDAR_RUNTIME_DIR",
    "RARDAR_VINEXT_STATE_DIR",
    "RARDAR_DATA_LOCK_DIR",
    "RARDAR_VITE_CACHE_DIR",
    "RARDAR_VINEXT_PORT",
    "RARDAR_RUNTIME_STATUS_PORT",
  ]) {
    assert.match(example, new RegExp(`^${name}=`, "m"));
  }
  assert.doesNotMatch(example, /ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+/);
  assert.doesNotMatch(example, /GITHUB_TOKEN|replace-with-a-read-only-github-token/);
  assert.match(
    systemdExample,
    /^# __VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=rardar\.cosflow\.icu$/m,
  );
  assert.doesNotMatch(systemdExample, /^__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=/m);
  assert.match(runtime, /VITE_ADDITIONAL_ALLOWED_HOSTS_ENV/);
  assert.match(systemdExample, /^RARDAR_TRENDING_DISCOVER_ENABLED=false$/m);
  assert.match(systemdExample, /^RARDAR_RETENTION_ENABLED=false$/m);
  assert.match(systemdExample, /^RARDAR_STORAGE_WARNING_PERCENT=85$/m);
  assert.match(systemdExample, /^RARDAR_STORAGE_HARD_PERCENT=90$/m);
  assert.match(systemdExample, /^RARDAR_STORAGE_MINIMUM_FREE_BYTES=8589934592$/m);
});

test("systemd sandbox grants exactly the address families required by the runtime", async () => {
  const [unit, runtimeSettings] = await Promise.all([
    source("deploy/systemd/rardar.service"),
    source("pipeline/runtime_settings.py"),
  ]);
  const directives = parseUnitDirectives(unit);
  const familyValue = requireSingleDirective(
    directives,
    "Service",
    "RestrictAddressFamilies",
  );
  const familyTokens = familyValue.split(/\s+/).filter(Boolean);
  const families = new Set(familyTokens);

  assert.equal(familyTokens.length, families.size, "address families must not be duplicated");
  assert.deepEqual(
    [...families].sort(),
    ["AF_INET", "AF_INET6", "AF_NETLINK", "AF_UNIX"],
  );
  assert.equal(families.has("AF_PACKET"), false);
  assert.equal(families.has("AF_RAW"), false);

  for (const [name, expected] of [
    ["User", "rardar"],
    ["NoNewPrivileges", "true"],
    ["CapabilityBoundingSet", ""],
    ["AmbientCapabilities", ""],
    ["ProtectSystem", "strict"],
    ["ProtectHome", "true"],
    ["PrivateDevices", "true"],
    ["ProtectKernelTunables", "true"],
    ["ProtectKernelModules", "true"],
    ["ProtectKernelLogs", "true"],
    ["ProtectControlGroups", "true"],
    ["RestrictSUIDSGID", "true"],
    ["LockPersonality", "true"],
    ["KillMode", "control-group"],
  ]) {
    assert.equal(requireSingleDirective(directives, "Service", name), expected);
  }
  assert.match(runtimeSettings, /^RUNTIME_HOST = "127\.0\.0\.1"$/m);
  assert.doesNotMatch(unit, /0\.0\.0\.0|\[::\]/);
});

test("network interfaces probe exercises only the local Node API", async () => {
  const probe = await source("scripts/systemd-network-interfaces-probe.mjs");
  assert.match(probe, /from "node:os"/);
  assert.match(probe, /networkInterfaces\(\)/);
  assert.doesNotMatch(probe, /fetch\(|https?:|\.listen\(|\.bind\(/);

  const completed = spawnSync(process.execPath, [networkInterfacesProbe], {
    cwd: repositoryRoot,
    encoding: "utf8",
    timeout: 10_000,
    windowsHide: true,
  });
  assert.ifError(completed.error);
  assert.equal(completed.status, 0, completed.stderr);
  assert.equal(completed.stdout.trim(), "AF_NETLINK_PROBE_OK");
});

test("runtime telemetry is proxied through the website without exposing the status port", async () => {
  const [component, route, vite] = await Promise.all([
    source("app/components/RuntimeStatus.tsx"),
    source("app/api/runtime-status/route.ts"),
    source("vite.config.ts"),
  ]);

  assert.match(component, /const runtimeStatusUrl = "\/api\/runtime-status"/);
  assert.doesNotMatch(component, /127\.0\.0\.1:3002/);
  assert.match(route, /parsed\.hostname !== "127\.0\.0\.1"/);
  assert.match(route, /AbortSignal\.timeout\(2_000\)/);
  assert.match(route, /maximumStatusBytes/);
  assert.match(vite, /RARDAR_RUNTIME_STATUS_ORIGIN/);
  assert.match(vite, /RARDAR_VITE_CACHE_DIR/);
  assert.match(vite, /RARDAR_VINEXT_PORT and RARDAR_RUNTIME_STATUS_PORT must differ/);
  assert.doesNotMatch(vite, /allowedHosts\s*:\s*true/);
});

test("deployment v1 does not add a second scheduler or a public listener", async () => {
  const [unit, deployment, guide] = await Promise.all([
    source("deploy/systemd/rardar.service"),
    source("pipeline/deployment.py"),
    source("docs/DEPLOYMENT.md"),
  ]);

  assert.doesNotMatch(unit, /0\.0\.0\.0/);
  assert.doesNotMatch(unit, /ExecStart=.*npm run local:start/);
  assert.match(deployment, /127\.0\.0\.1/);
  assert.match(guide, /127\.0\.0\.1:3000/);
  assert.match(guide, /SSH tunnel|SSH 隧道/i);
  assert.match(guide, /vinext dev/);
  assert.match(guide, /vinext start/);
  assert.match(guide, /--configLoader runner/);
  assert.match(guide, /--port <RARDAR_VINEXT_PORT>/);
  assert.match(guide, /--strictPort/);
  assert.match(guide, /proxy_set_header Host \$host/);
  assert.doesNotMatch(guide, /^\s*proxy_set_header Host 127\.0\.0\.1;\s*$/m);
  assert.match(guide, /__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=rardar\.cosflow\.icu/);
  assert.doesNotMatch(guide, /^vinext dev --hostname/m);
  assert.match(guide, /代码合并不能被描述为已经部署/);
});

test("foreground manager survives an isolated reboot without split data or a second scheduler", { timeout: 300_000 }, async () => {
  const temporaryRoot = await mkdtemp(join(tmpdir(), "rardar-always-on-"));
  const dataDirectory = join(temporaryRoot, "data");
  const runtimeDirectory = join(temporaryRoot, "runtime");
  const lockDirectory = join(temporaryRoot, "locks");
  const vinextState = join(temporaryRoot, "vinext-state");
  const viteCache = join(temporaryRoot, "vite-cache");
  const backups = join(temporaryRoot, "backups");
  const websitePort = await randomLoopbackPort();
  let statusPort = await randomLoopbackPort();
  while (statusPort === websitePort) statusPort = await randomLoopbackPort();
  const schedule = futureSchedule();
  let serviceEnvironment;
  const ownedRuntimes = [];

  try {
    const fixture = prepareFixture(dataDirectory);
    for (const path of [
      runtimeDirectory,
      lockDirectory,
      vinextState,
      viteCache,
      backups,
      join(temporaryRoot, "wrangler-logs"),
      join(temporaryRoot, "wrangler-registry"),
      join(temporaryRoot, "miniflare-registry"),
      join(temporaryRoot, "home"),
    ]) {
      await mkdir(path, { recursive: true });
    }
    const currentPath = join(dataDirectory, "current.json");
    const currentHashBefore = await sha256(currentPath);
    const candidatesBefore = await candidateSnapshot(dataDirectory);
    const releaseTreesBefore = await releaseWriteTargetSnapshot();
    assert.deepEqual(candidatesBefore, {
      allCandidates: [],
      failedCandidateCount: 0,
      failedCandidates: [],
    });
    const bypass = [process.env.NO_PROXY, process.env.no_proxy, "127.0.0.1", "localhost"]
      .filter(Boolean)
      .join(",");
    const environment = {
      ...process.env,
      HOME: join(temporaryRoot, "home"),
      USERPROFILE: join(temporaryRoot, "home"),
      LOCALAPPDATA: join(temporaryRoot, "localappdata"),
      PYTHONDONTWRITEBYTECODE: "1",
      RARDAR_HOME: repositoryRoot,
      RARDAR_DATA_DIR: dataDirectory,
      RARDAR_RUNTIME_DIR: runtimeDirectory,
      RARDAR_DATA_LOCK_DIR: lockDirectory,
      RARDAR_VINEXT_STATE_DIR: vinextState,
      RARDAR_VITE_CACHE_DIR: viteCache,
      RARDAR_BACKUP_DIR: backups,
      RARDAR_NODE: process.execPath,
      RARDAR_PYTHON: python,
      RARDAR_VINEXT_PORT: String(websitePort),
      RARDAR_RUNTIME_STATUS_PORT: String(statusPort),
      RARDAR_SCHEDULE_AT: schedule.at,
      RARDAR_SCHEDULE_TIMEZONE: schedule.timezone,
      RARDAR_STALE_AFTER_HOURS: "8760",
      WRANGLER_WRITE_LOGS: "false",
      WRANGLER_LOG_PATH: join(temporaryRoot, "wrangler-logs"),
      WRANGLER_REGISTRY_PATH: join(temporaryRoot, "wrangler-registry"),
      MINIFLARE_REGISTRY_PATH: join(temporaryRoot, "miniflare-registry"),
      CLOUDFLARE_VITE_FORCE_LOCAL: "true",
      NO_PROXY: bypass,
      no_proxy: bypass,
    };
    for (const name of ["GH_TOKEN", "GITHUB_TOKEN", "NODE_AUTH_TOKEN", "NPM_TOKEN"]) {
      delete environment[name];
    }
    serviceEnvironment = environment;
    const websiteUrl = `http://127.0.0.1:${websitePort}`;

    const boot = async () => {
      const runtime = startService(environment);
      ownedRuntimes.push(runtime);
      const health = await waitFor("isolated website health", runtime, async () => {
        const result = await requestJson(websiteUrl, "/api/health", 5_000);
        return result.response.status === 200 ? result : null;
      });
      assert.equal(health.payload.generationId, fixture.generationA);
      const status = await waitFor("same-origin runtime status", runtime, async () => {
        const result = await requestJson(websiteUrl, "/api/runtime-status", 5_000);
        if (result.response.status !== 200 || result.payload?.state !== "healthy") {
          throw new Error(`runtime status is not healthy: ${JSON.stringify(result.payload)}`);
        }
        return result.payload;
      });
      if (process.platform !== "win32") {
        assert.equal(status.managerPid, runtime.child.pid);
      } else {
        assert.ok(Number.isInteger(status.managerPid) && status.managerPid > 0);
      }
      assert.equal(
        await comparablePath(status.runtime.dataDir),
        await comparablePath(dataDirectory),
      );
      assert.equal(status.runtime.vinextPort, websitePort);
      assert.equal(status.runtime.runtimeStatusPort, statusPort);
      assert.equal(status.services.scheduler.telemetryTrusted, true);
      assert.equal(status.services.scheduler.reportedProcessId, status.services.scheduler.pid);
      assert.equal(status.schedule.at, schedule.at);
      assert.equal(status.schedule.timezone, schedule.timezone);
      return { runtime, status };
    };

    const first = await boot();
    assert.equal(
      (await lstat(join(viteCache, "node_modules", ".vite"))).isDirectory(),
      true,
      "Vite optimize cache must stay under the external cache root",
    );
    const actionDeviceId = `always-on-reboot-${randomUUID()}`;
    const idempotencyKey = `always-on-reboot-action-${randomUUID()}`;
    const canonicalActionPath = `/api/actions?deviceId=${encodeURIComponent(actionDeviceId)}&projectIdVersion=1&projectId=${encodeURIComponent(fixture.projectId)}`;
    const recordedAction = await postJson(websiteUrl, "/api/actions", {
      deviceId: actionDeviceId,
      projectIdVersion: 1,
      projectId: fixture.projectId,
      action: "saved",
      idempotencyKey,
    });
    assert.equal(recordedAction.response.status, 200);
    assert.equal(recordedAction.payload?.recorded, true);
    assert.equal(recordedAction.payload?.idempotentReplay, false);
    assert.equal(recordedAction.payload?.projectId, fixture.projectId);
    assert.equal(recordedAction.payload?.event?.idempotencyKey, idempotencyKey);
    const firstActions = await requestJson(
      websiteUrl,
      canonicalActionPath,
      60_000,
    );
    assert.equal(firstActions.response.status, 200);
    assert.equal(firstActions.payload?.states?.length, 1);
    assert.equal(firstActions.payload?.states?.[0]?.projectId, fixture.projectId);
    assert.equal(firstActions.payload?.states?.[0]?.highestStage, "saved");
    assert.deepEqual(firstActions.payload?.actions?.map((item) => item.action), ["saved"]);
    const statusBeforeRestart = await requestJson(websiteUrl, "/api/runtime-status", 5_000);
    assert.equal(statusBeforeRestart.response.status, 200);
    const lastRunStartedAtBefore = statusBeforeRestart.payload?.services?.scheduler?.lastRunStartedAt ?? null;
    assert.equal(lastRunStartedAtBefore, null, "isolated future schedule must not run before reboot");
    await stopService(first.runtime, environment);
    const currentHashAfterFirstStop = await sha256(currentPath);
    const candidatesAfterFirstStop = await candidateSnapshot(dataDirectory);
    assert.equal(
      currentHashAfterFirstStop,
      currentHashBefore,
      "service start must not refresh or move current",
    );
    assert.deepEqual(
      candidatesAfterFirstStop,
      candidatesBefore,
      "service start must not create or change failed candidates",
    );
    const d1Inspection = inspectRardarSqlite(vinextState, environment);
    assert.ok(d1Inspection.sqliteFileCount > 0, JSON.stringify(d1Inspection));
    assert.ok(d1Inspection.rardarDatabases.length > 0, JSON.stringify(d1Inspection));
    assert.ok(
      d1Inspection.rardarDatabases.some((database) => (
        database.tables.includes("feedback")
        && database.tables.includes("decision_events")
        && database.tables.includes("project_actions")
      )),
      JSON.stringify(d1Inspection),
    );

    const second = await boot();
    assert.notEqual(second.status.services.scheduler.pid, first.status.services.scheduler.pid);
    const secondActions = await requestJson(
      websiteUrl,
      canonicalActionPath,
      60_000,
    );
    assert.equal(secondActions.response.status, 200);
    assert.deepEqual(secondActions.payload, firstActions.payload);
    const onlineCheck = runOnlineDeploymentCheck(environment);
    if (process.platform === "linux") {
      assert.equal(
        onlineCheck.completed.status,
        0,
        `${onlineCheck.completed.stdout}\n${onlineCheck.completed.stderr}`,
      );
      assert.equal(onlineCheck.payload.status, "healthy");
      assert.equal(onlineCheck.payload.mode, "online");
      assert.equal(onlineCheck.payload.generation?.generationId, fixture.generationA);
      assert.equal(onlineCheck.payload.http?.generationId, fixture.generationA);
    } else {
      assert.equal(
        onlineCheck.completed.status,
        1,
        "non-Linux online checks must fail closed instead of skipping process ownership",
      );
      assert.equal(onlineCheck.payload.status, "failed");
      assert.equal(
        onlineCheck.payload.error?.code,
        "online_platform_unsupported",
        JSON.stringify(onlineCheck.payload),
      );
    }
    const lastRunStartedAtAfter = second.status.services.scheduler.lastRunStartedAt ?? null;
    assert.equal(
      lastRunStartedAtAfter,
      lastRunStartedAtBefore,
      "reboot with a future schedule must not trigger catch-up",
    );
    await stopService(second.runtime, environment);
    const currentHashAfterRestart = await sha256(currentPath);
    const candidatesAfterRestart = await candidateSnapshot(dataDirectory);
    const releaseTreesAfter = await releaseWriteTargetSnapshot();
    assert.equal(
      currentHashAfterRestart,
      currentHashBefore,
      "reboot must preserve the published pointer",
    );
    assert.deepEqual(
      candidatesAfterRestart,
      candidatesBefore,
      "reboot must not create or change failed candidates",
    );
    assert.deepEqual(
      releaseTreesAfter,
      releaseTreesBefore,
      "start/restart/stop must not write release .vinext, .wrangler or node_modules/.vite-temp",
    );
  } finally {
    await cleanupOwnedRuntimeTrees(
      ownedRuntimes,
      serviceEnvironment ?? process.env,
      temporaryRoot,
    );
  }
});
