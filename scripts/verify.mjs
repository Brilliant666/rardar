import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dataRoot = join(repositoryRoot, "data");
const npmExecPath = process.env.npm_execpath;
const python = process.env.RARDAR_PYTHON || "python";

function assertSupportedNode() {
  const [major, minor] = process.versions.node.split(".").map(Number);
  if (major < 22 || (major === 22 && minor < 13)) {
    throw new Error(
      `Node.js 22.13 or newer is required; current version is ${process.versions.node}`,
    );
  }
  if (!npmExecPath) {
    throw new Error("Run the complete gate through `npm run verify`");
  }
}

function hashFile(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function inventoryTree(root) {
  const entries = [];

  function visit(path) {
    const stat = lstatSync(path);
    const name = relative(root, path).split("\\").join("/") || ".";

    if (stat.isSymbolicLink()) {
      entries.push({ name, type: "symlink", target: readlinkSync(path) });
      return;
    }
    if (stat.isDirectory()) {
      entries.push({ name, type: "directory" });
      for (const child of readdirSync(path).sort()) {
        visit(join(path, child));
      }
      return;
    }
    if (stat.isFile()) {
      entries.push({ name, type: "file", size: stat.size, sha256: hashFile(path) });
      return;
    }
    entries.push({ name, type: "other" });
  }

  visit(root);
  return entries;
}

function inventoryDiff(before, after) {
  const beforeByName = new Map(before.map((entry) => [entry.name, entry]));
  const afterByName = new Map(after.map((entry) => [entry.name, entry]));
  const added = [...afterByName.keys()].filter((name) => !beforeByName.has(name));
  const removed = [...beforeByName.keys()].filter((name) => !afterByName.has(name));
  const changed = [...beforeByName.keys()].filter(
    (name) =>
      afterByName.has(name) &&
      JSON.stringify(beforeByName.get(name)) !== JSON.stringify(afterByName.get(name)),
  );
  return { added, removed, changed };
}

function runGit(args) {
  const result = spawnSync("git", args, {
    cwd: repositoryRoot,
    encoding: "buffer",
    shell: false,
    windowsHide: true,
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(
      `git ${args.join(" ")} failed with exit code ${result.status}: ${result.stderr.toString("utf8")}`,
    );
  }
  return result.stdout;
}

function gitStatus() {
  return runGit(["status", "--porcelain=v1", "-z", "--untracked-files=all"]);
}

function gitVisibleInventory() {
  const names = runGit([
    "ls-files",
    "-z",
    "--cached",
    "--others",
    "--exclude-standard",
  ])
    .toString("utf8")
    .split("\0")
    .filter(Boolean)
    .sort();

  return names.map((name) => {
    const path = join(repositoryRoot, name);
    let stat;
    try {
      stat = lstatSync(path);
    } catch (error) {
      if (error.code === "ENOENT") {
        return { name, type: "missing" };
      }
      throw error;
    }
    if (stat.isSymbolicLink()) {
      return { name, type: "symlink", target: readlinkSync(path) };
    }
    if (stat.isFile()) {
      return {
        name,
        type: "file",
        executable: Boolean(stat.mode & 0o111),
        size: stat.size,
        sha256: hashFile(path),
      };
    }
    return { name, type: stat.isDirectory() ? "directory" : "other" };
  });
}

function displayGitStatus(status) {
  const text = status.toString("utf8").split("\0").filter(Boolean).join("\n");
  return text || "(clean)";
}

function createIsolation() {
  const root = mkdtempSync(join(tmpdir(), "rardar-verify-"));
  try {
    const paths = {
      home: join(root, "home"),
      localAppData: join(root, "localappdata"),
      runtime: join(root, "runtime"),
      state: join(root, "state"),
      cache: join(root, "cache"),
      config: join(root, "config"),
      temporary: join(root, "tmp"),
      vinext: join(root, "vinext-state"),
      wranglerRegistry: join(root, "wrangler-registry"),
      wranglerLogs: join(root, "wrangler-logs"),
      miniflareRegistry: join(root, "miniflare-registry"),
    };
    for (const path of Object.values(paths)) {
      mkdirSync(path, { recursive: true });
    }

    const environment = {
      ...process.env,
      APPDATA: paths.localAppData,
      CI: "true",
      HOME: paths.home,
      LOCALAPPDATA: paths.localAppData,
      MINIFLARE_REGISTRY_PATH: paths.miniflareRegistry,
      PYTHONDONTWRITEBYTECODE: "1",
      RARDAR_DATA_DIR: dataRoot,
      RARDAR_RUNTIME_DIR: paths.runtime,
      RARDAR_VINEXT_STATE_DIR: paths.vinext,
      TEMP: paths.temporary,
      TMP: paths.temporary,
      TMPDIR: paths.temporary,
      USERPROFILE: paths.home,
      WRANGLER_LOG_PATH: paths.wranglerLogs,
      WRANGLER_REGISTRY_PATH: paths.wranglerRegistry,
      WRANGLER_WRITE_LOGS: "false",
      XDG_CACHE_HOME: paths.cache,
      XDG_CONFIG_HOME: paths.config,
      XDG_STATE_HOME: paths.state,
    };
    environment.RARDAR_PYTHON = python;
    if (isAbsolute(python)) {
      const pathKey = Object.keys(environment).find((key) => key.toUpperCase() === "PATH") || "PATH";
      environment[pathKey] = `${dirname(python)}${delimiter}${environment[pathKey] || ""}`;
    }

    const secretNames = new Set(["GH_TOKEN", "GITHUB_TOKEN", "NODE_AUTH_TOKEN", "NPM_TOKEN"]);
    for (const key of Object.keys(environment)) {
      if (secretNames.has(key.toUpperCase())) {
        delete environment[key];
      }
    }

    return { root, environment };
  } catch (error) {
    try {
      rmSync(root, { force: true, maxRetries: 3, recursive: true, retryDelay: 100 });
    } catch (cleanupError) {
      throw new AggregateError(
        [error, cleanupError],
        `Could not initialize or clean the Verify isolation directory ${root}`,
      );
    }
    throw error;
  }
}

function npmCommand(script) {
  return [process.execPath, [npmExecPath, "run", script]];
}

function runGate(name, command, args, environment) {
  console.log(`\n=== Verify: ${name} ===`);
  const result = spawnSync(command, args, {
    cwd: repositoryRoot,
    env: environment,
    shell: false,
    stdio: "inherit",
    windowsHide: true,
  });
  if (result.error) {
    throw new Error(`${name} could not start: ${result.error.message}`);
  }
  if (result.status !== 0) {
    const outcome = result.signal ? `signal ${result.signal}` : `exit code ${result.status}`;
    throw new Error(`${name} failed with ${outcome}`);
  }
  console.log(`=== Verify passed: ${name} ===`);
}

function summarizeInventoryDiff(diff) {
  return [
    ...diff.added.map((name) => `added: ${name}`),
    ...diff.removed.map((name) => `removed: ${name}`),
    ...diff.changed.map((name) => `changed: ${name}`),
  ].join("\n");
}

let isolation;
const failures = [];

try {
  assertSupportedNode();
  const dataBefore = inventoryTree(dataRoot);
  const gitFilesBefore = gitVisibleInventory();
  const gitBefore = gitStatus();
  isolation = createIsolation();

  try {
    const gates = [
      ["Lint", ...npmCommand("lint")],
      [
        "Python tests",
        python,
        ["-m", "unittest", "discover", "-s", "pipeline", "-p", "test_*.py"],
      ],
      ["Schema validation", ...npmCommand("data:validate")],
      ["Data audit", ...npmCommand("data:audit")],
      ["Production build", ...npmCommand("build")],
      ["Node tests", ...npmCommand("test:node")],
      ["Production dependency security audit", ...npmCommand("security:audit:prod")],
    ];

    for (const [name, command, args] of gates) {
      runGate(name, command, args, isolation.environment);
    }
  } catch (error) {
    failures.push(error);
  } finally {
    try {
      const dataAfter = inventoryTree(dataRoot);
      const diff = inventoryDiff(dataBefore, dataAfter);
      if (diff.added.length || diff.removed.length || diff.changed.length) {
        failures.push(
          new Error(`Repository data changed during Verify:\n${summarizeInventoryDiff(diff)}`),
        );
      } else {
        console.log("\n=== Verify guard passed: repository data unchanged ===");
      }
    } catch (error) {
      failures.push(new Error(`Repository data guard failed: ${error.message}`));
    }

    try {
      const gitFilesAfter = gitVisibleInventory();
      const diff = inventoryDiff(gitFilesBefore, gitFilesAfter);
      if (diff.added.length || diff.removed.length || diff.changed.length) {
        failures.push(
          new Error(
            `Git-visible file contents changed during Verify:\n${summarizeInventoryDiff(diff)}`,
          ),
        );
      } else {
        console.log("=== Verify guard passed: Git-visible file contents unchanged ===");
      }

      const gitAfter = gitStatus();
      if (!gitBefore.equals(gitAfter)) {
        failures.push(
          new Error(
            `Verify left Git-visible changes\nBefore:\n${displayGitStatus(gitBefore)}\nAfter:\n${displayGitStatus(gitAfter)}`,
          ),
        );
      } else {
        console.log("=== Verify guard passed: no Git-visible artifacts ===");
      }
    } catch (error) {
      failures.push(new Error(`Git worktree guard failed: ${error.message}`));
    }
  }
} catch (error) {
  failures.push(error);
} finally {
  if (isolation) {
    try {
      rmSync(isolation.root, { force: true, maxRetries: 3, recursive: true, retryDelay: 100 });
      console.log("=== Verify guard passed: isolated Runtime state removed ===");
    } catch (error) {
      failures.push(new Error(`Could not remove isolated Verify state: ${error.message}`));
    }
  }
}

if (failures.length) {
  console.error("\n=== Verify failed ===");
  for (const failure of failures) {
    console.error(`- ${failure.message}`);
  }
  process.exitCode = 1;
} else {
  console.log("\n=== Verify passed: all 7 gates and isolation guards succeeded ===");
}
