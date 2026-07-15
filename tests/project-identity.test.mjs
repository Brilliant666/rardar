import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repositoryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const python = process.env.RARDAR_PYTHON || "python";
const vectors = JSON.parse(
  readFileSync(join(repositoryRoot, "contracts", "project-identity-v1.vectors.json"), "utf8"),
);

function runIdentityCli(repository) {
  const result = spawnSync(
    python,
    ["-m", "pipeline.project_identity", `--repository=${repository}`],
    {
      cwd: repositoryRoot,
      encoding: "utf8",
      maxBuffer: 1024 * 1024,
      windowsHide: true,
    },
  );
  if (result.error) throw result.error;
  const lines = result.stdout.trim().split(/\r?\n/).filter(Boolean);
  assert.ok(lines.length > 0, `identity CLI returned no JSON for ${JSON.stringify(repository)}`);
  return { ...result, payload: JSON.parse(lines.at(-1)) };
}

test("canonical Python identity CLI matches every valid golden vector", () => {
  assert.equal(vectors.schemaVersion, 1);
  assert.equal(vectors.algorithm, "rardar-project-id-v1");
  assert.equal(vectors.projectIdVersion, 1);
  const projectIdPattern = new RegExp(vectors.projectIdPattern);

  for (const vector of vectors.valid) {
    const result = runIdentityCli(vector.repository);
    assert.equal(result.status, 0, `${vector.name}: ${result.stderr}`);
    assert.deepEqual(
      {
        status: result.payload.status,
        algorithm: result.payload.algorithm,
        projectIdVersion: result.payload.projectIdVersion,
        canonicalRepository: result.payload.canonicalRepository,
        humanPrefix: result.payload.humanPrefix,
        digest: result.payload.digest,
        projectId: result.payload.projectId,
      },
      {
        status: "ok",
        algorithm: vectors.algorithm,
        projectIdVersion: vectors.projectIdVersion,
        canonicalRepository: vector.canonicalRepository,
        humanPrefix: vector.humanPrefix,
        digest: vector.digest,
        projectId: vector.projectId,
      },
      vector.name,
    );
    assert.match(result.payload.projectId, projectIdPattern, vector.name);
  }
});

test("canonical Python identity CLI rejects every string invalid golden vector", () => {
  for (const vector of vectors.invalid) {
    if (typeof vector.repository !== "string") {
      // argparse intentionally accepts strings only. The canonical Python
      // shared-vector test covers this non-string contract boundary directly.
      assert.equal(vector.repository, null, vector.name);
      assert.equal(vector.errorCode, "invalid_repository_type", vector.name);
      continue;
    }
    const result = runIdentityCli(vector.repository);
    assert.equal(result.status, 2, `${vector.name}: ${result.stderr}`);
    assert.equal(result.payload.status, "error", vector.name);
    assert.equal(result.payload.algorithm, vectors.algorithm, vector.name);
    assert.equal(result.payload.errorCode, vector.errorCode, vector.name);
  }
});

test("golden vectors preserve case identity and separate legacy slug collisions", () => {
  const caseGroups = Map.groupBy(
    vectors.valid.filter((vector) => vector.caseGroup),
    (vector) => vector.caseGroup,
  );
  for (const [name, group] of caseGroups) {
    assert.equal(new Set(group.map((vector) => vector.projectId)).size, 1, name);
  }

  const collisionGroups = Map.groupBy(
    vectors.valid.filter((vector) => vector.collisionGroup),
    (vector) => vector.collisionGroup,
  );
  for (const [name, group] of collisionGroups) {
    assert.equal(new Set(group.map((vector) => vector.legacySlug)).size, 1, name);
    assert.equal(new Set(group.map((vector) => vector.projectId)).size, group.length, name);
  }
});
