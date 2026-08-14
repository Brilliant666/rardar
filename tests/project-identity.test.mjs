import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  ProjectIdentityError,
  createProjectIdentityContext,
  identityForRepository,
  resolveProjectSelector,
  withCurrentProjectIdentity,
  withCurrentProjectIdentityIfPresent,
} from "../app/project-identity.mjs";
import {
  associateProjectsById,
  canonicalProjectPath,
  collectWatchStatusesByProjectId,
  indexStableProjectsById,
  isFreshClientRead,
  resultForGeneration,
  stableProjectSelector,
} from "../app/client-project-identity.mjs";

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

test("Worker Web Crypto identity matches every shared golden vector", async () => {
  for (const vector of vectors.valid) {
    const identity = await identityForRepository(vector.repository);
    assert.deepEqual(
      {
        projectIdVersion: identity.projectIdVersion,
        canonicalRepository: identity.canonicalRepository,
        humanPrefix: identity.humanPrefix,
        digest: identity.digest,
        projectId: identity.projectId,
      },
      {
        projectIdVersion: vectors.projectIdVersion,
        canonicalRepository: vector.canonicalRepository,
        humanPrefix: vector.humanPrefix,
        digest: vector.digest,
        projectId: vector.projectId,
      },
      vector.name,
    );
  }
  for (const vector of vectors.invalid) {
    await assert.rejects(
      identityForRepository(vector.repository),
      (error) => error instanceof ProjectIdentityError && error.code === vector.errorCode,
      vector.name,
    );
  }
});

function catalogProject(repository, slug, extra = {}) {
  return { repo: repository, slug, ...extra };
}

const TEST_PUBLISHED_AT = "2026-07-16T00:00:00.000001Z";

function createTestProjectIdentityContext(generationId, catalog) {
  return createProjectIdentityContext(generationId, catalog, TEST_PUBLISHED_AT);
}

test("request identity context derives v1/v2 identities and verifies v3 carried identity", async () => {
  const expected = await identityForRepository("Owner/Repo.Name");
  for (const schemaVersion of [1, 2]) {
    const sourceProject = catalogProject("Owner/Repo.Name", "owner--repo-name", {
      title: `Catalog v${schemaVersion} project`,
    });
    const context = await createTestProjectIdentityContext(`generation-v${schemaVersion}`, {
      schemaVersion,
      projects: [sourceProject],
    });
    assert.deepEqual(context.identityCatalog, {
      generationId: `generation-v${schemaVersion}`,
      publishedAt: TEST_PUBLISHED_AT,
      projects: [{
        projectIdVersion: 1,
        projectId: expected.projectId,
        projectSlug: "owner--repo-name",
        repository: "Owner/Repo.Name",
      }],
    });
    assert.deepEqual(context.stableProjects([sourceProject]), [{
      ...sourceProject,
      projectIdVersion: 1,
      projectId: expected.projectId,
    }]);
    assert.equal(sourceProject.projectId, undefined, "legacy Catalog input must not be mutated");
  }

  const v3Project = catalogProject("Owner/Repo.Name", "owner--repo-name", {
    title: "Catalog v3 project",
    projectIdVersion: 1,
    projectId: expected.projectId,
  });
  const v3 = await createTestProjectIdentityContext("generation-v3", {
    schemaVersion: 3,
    projectIdVersion: 1,
    projects: [v3Project],
  });
  assert.equal(v3.identityCatalog.projects[0].projectId, expected.projectId);
  assert.deepEqual(v3.stableProjects([v3Project]), [v3Project]);

  await assert.rejects(
    createTestProjectIdentityContext("forged-v3", {
      schemaVersion: 3,
      projectIdVersion: 1,
      projects: [catalogProject("Owner/Repo.Name", "owner--repo-name", {
        projectIdVersion: 1,
        projectId: "owner-other--0123456789abcdef0123",
      })],
    }),
    (error) => error instanceof ProjectIdentityError && error.code === "project_id_mismatch",
  );
  await assert.rejects(
    createTestProjectIdentityContext("wrong-version-v3", {
      schemaVersion: 3,
      projectIdVersion: 1,
      projects: [catalogProject("Owner/Repo.Name", "owner--repo-name", {
        projectIdVersion: 2,
        projectId: expected.projectId,
      })],
    }),
    (error) => error instanceof ProjectIdentityError && error.code === "unsupported_project_id_version",
  );
  await assert.rejects(
    createTestProjectIdentityContext("wrong-catalog-version-v3", {
      schemaVersion: 3,
      projectIdVersion: 2,
      projects: [],
    }),
    (error) => error instanceof ProjectIdentityError && error.code === "unsupported_project_id_version",
  );
});

test("client helpers keep projects with the same legacy slug isolated by stable ID", async () => {
  const firstIdentity = await identityForRepository("owner/foo.bar");
  const secondIdentity = await identityForRepository("owner/foo-bar");
  const first = {
    slug: "legacy-collision",
    repo: "owner/foo.bar",
    projectIdVersion: 1,
    projectId: firstIdentity.projectId,
  };
  const second = {
    slug: "legacy-collision",
    repo: "owner/foo-bar",
    projectIdVersion: 1,
    projectId: secondIdentity.projectId,
  };

  assert.deepEqual(stableProjectSelector(first), {
    projectIdVersion: 1,
    projectId: firstIdentity.projectId,
  });
  assert.equal(
    canonicalProjectPath(first),
    `/project/v1/${encodeURIComponent(firstIdentity.projectId)}`,
  );

  const byId = indexStableProjectsById([first, second]);
  assert.equal(byId.size, 2);
  assert.strictEqual(byId.get(firstIdentity.projectId), first);
  assert.strictEqual(byId.get(secondIdentity.projectId), second);

  const records = [
    {
      projectIdVersion: 1,
      projectId: secondIdentity.projectId,
      projectSlug: "legacy-collision",
      rank: 1,
    },
    {
      projectIdVersion: 1,
      projectId: firstIdentity.projectId,
      projectSlug: "legacy-collision",
      rank: 2,
    },
    {
      projectIdVersion: 1,
      projectId: "unknown-project--0123456789abcdef0123",
      projectSlug: "legacy-collision",
      rank: 3,
    },
  ];
  const associated = associateProjectsById([first, second], records);
  assert.deepEqual(
    associated.map(({ project, record }) => [project.projectId, record.rank]),
    [[secondIdentity.projectId, 1], [firstIdentity.projectId, 2]],
  );

  const statuses = collectWatchStatusesByProjectId(
    [{
      projectIdVersion: 1,
      projectId: firstIdentity.projectId,
      projectSlug: "legacy-collision",
      value: "待确定",
    }],
    [{
      projectIdVersion: 1,
      projectId: secondIdentity.projectId,
      projectSlug: "legacy-collision",
      action: "saved",
    }],
  );
  assert.deepEqual(
    [...statuses.keys()].sort(),
    [secondIdentity.projectId],
  );
  assert.equal(statuses.has(firstIdentity.projectId), false);
  assert.equal(statuses.get(secondIdentity.projectId).length, 1);
  assert.equal(statuses.get(secondIdentity.projectId)[0], "已关注");

  assert.equal(isFreshClientRead(false, 7, 7, 2, 2), true);
  assert.equal(isFreshClientRead(true, 7, 7, 2, 2), false);
  assert.equal(
    isFreshClientRead(false, 7, 7, 2, 3),
    false,
    "a successful mutation must invalidate an older feedback read",
  );
  const generationAResult = { generationId: "generation-a", recommendations: [] };
  assert.strictEqual(resultForGeneration("generation-a", generationAResult), generationAResult);
  assert.equal(
    resultForGeneration("generation-a", { generationId: "generation-b", recommendations: [] }),
    null,
    "a recommendation response from another generation must be rejected",
  );

  assert.throws(
    () => stableProjectSelector({ ...first, projectIdVersion: 2 }),
    (error) => error instanceof ProjectIdentityError
      && error.code === "invalid_client_project_identity",
  );
  assert.throws(
    () => indexStableProjectsById([first, { ...second, projectId: first.projectId }]),
    (error) => error instanceof ProjectIdentityError
      && error.code === "duplicate_client_project_identity",
  );
});

test("request identity selector accepts only canonical pairs or unique current Catalog slugs", async () => {
  const first = await identityForRepository("owner/foo.bar");
  const second = await identityForRepository("owner/foo-bar");
  const context = await createTestProjectIdentityContext("selector-generation", {
    schemaVersion: 2,
    projects: [
      catalogProject("owner/foo.bar", "legacy-ambiguous"),
      catalogProject("owner/foo-bar", "legacy-ambiguous"),
      catalogProject("owner/unique", "legacy-unique"),
    ],
  });
  const unique = context.identityCatalog.projects[2];

  assert.equal(
    resolveProjectSelector(context, {
      projectIdVersion: 1,
      projectId: first.projectId,
    }).projectId,
    first.projectId,
  );
  assert.equal(resolveProjectSelector(context, { projectSlug: "legacy-unique" }).projectId, unique.projectId);
  assert.equal(
    resolveProjectSelector(context, {
      projectIdVersion: 1,
      projectId: unique.projectId,
      projectSlug: "legacy-unique",
    }).projectId,
    unique.projectId,
  );
  assert.equal(resolveProjectSelector(context, {}, { required: false }), null);

  for (const [selector, code, status] of [
    [{}, "missing_project_identity", 400],
    [{ projectId: first.projectId }, "invalid_project_selector", 400],
    [{ projectIdVersion: 2, projectId: first.projectId }, "unsupported_project_id_version", 400],
    [{ projectIdVersion: 1, projectId: "forged" }, "invalid_project_id", 400],
    [{ projectIdVersion: 1, projectId: `${first.projectId.slice(0, -1)}0` }, "unknown_project_id", 404],
    [{ projectSlug: "missing" }, "unknown_project_slug", 404],
    [{ projectSlug: "legacy-ambiguous" }, "ambiguous_project_slug", 409],
    [{
      projectIdVersion: 1,
      projectId: second.projectId,
      projectSlug: "legacy-unique",
    }, "project_identity_conflict", 409],
  ]) {
    assert.throws(
      () => resolveProjectSelector(context, selector),
      (error) => error instanceof ProjectIdentityError
        && error.code === code
        && error.status === status,
      code,
    );
  }

  assert.throws(
    () => withCurrentProjectIdentity(context, {
      projectIdVersion: 1,
      projectId: `${unique.projectId.slice(0, -1)}${unique.projectId.endsWith("0") ? "1" : "0"}`,
      projectSlug: "legacy-unique",
    }),
    (error) => error instanceof ProjectIdentityError
      && error.code === "unresolved_project_identity"
      && error.status === 503,
  );
  assert.equal(
    withCurrentProjectIdentity(context, {
      projectIdVersion: 1,
      projectId: unique.projectId,
      projectSlug: "historical-slug",
    }).projectSlug,
    "legacy-unique",
    "a verified stable row must expose the current compatibility slug after a Catalog rename",
  );
});

test("current collection projection omits valid retired identities but rejects malformed storage", async () => {
  const current = await identityForRepository("owner/current");
  const retired = await identityForRepository("owner/retired");
  const context = await createTestProjectIdentityContext("current-collection-generation", {
    schemaVersion: 2,
    projects: [catalogProject("owner/current", "owner--current")],
  });

  assert.deepEqual(
    withCurrentProjectIdentityIfPresent(context, {
      projectIdVersion: 1,
      projectId: current.projectId,
      projectSlug: "historical-current-slug",
    }),
    {
      projectIdVersion: 1,
      projectId: current.projectId,
      projectSlug: "owner--current",
    },
  );
  assert.equal(
    withCurrentProjectIdentityIfPresent(context, {
      projectIdVersion: 1,
      projectId: retired.projectId,
      projectSlug: "owner--retired",
    }),
    null,
    "a structurally valid historical identity outside the current Catalog is retained in D1 but omitted",
  );
  assert.throws(
    () => withCurrentProjectIdentityIfPresent(context, {
      projectIdVersion: 1,
      projectId: "not-a-stable-project-id",
      projectSlug: "forged",
    }),
    (error) => error instanceof ProjectIdentityError
      && error.code === "invalid_stored_project_identity",
  );
});

test("request identity context rejects duplicate normalized repositories before use", async () => {
  await assert.rejects(
    createTestProjectIdentityContext("duplicate-generation", {
      schemaVersion: 2,
      projects: [
        catalogProject("Owner/Repo", "first"),
        catalogProject("owner/repo", "second"),
      ],
    }),
    (error) => error instanceof ProjectIdentityError
      && error.code === "duplicate_normalized_repository",
  );
});
