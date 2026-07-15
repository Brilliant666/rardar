import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  appendProjectActionEvent,
  prepareProjectActionSchema,
  readProjectActionState,
  readWeeklyActionMetrics,
  stateToActionProjection,
} from "../db/project-actions.mjs";
import { identityForRepository } from "../app/project-identity.mjs";
import {
  adoptStableProjectIdentities,
  appendStableProjectActionEvent,
  readStableFeedback,
  readStableProjectActionState,
  readStableWeeklyActionMetrics,
  readStableWeeklyFeedbackMetrics,
  stableStateToActionProjection,
  upsertStableFeedback,
} from "../db/stable-project-decisions.mjs";

class SqliteD1Statement {
  constructor(database, sql, bindings = []) {
    this.database = database;
    this.sql = sql;
    this.bindings = bindings;
  }

  bind(...bindings) {
    return new SqliteD1Statement(this.database, this.sql, bindings);
  }

  async first() {
    return this.database.prepare(this.sql).get(...this.bindings) ?? null;
  }

  async all() {
    return { results: this.database.prepare(this.sql).all(...this.bindings) };
  }

  async run() {
    const result = this.database.prepare(this.sql).run(...this.bindings);
    return { success: true, meta: { changes: Number(result.changes) } };
  }
}

class SqliteD1Database {
  constructor() {
    this.raw = new DatabaseSync(":memory:");
    this.batchTail = Promise.resolve();
  }

  prepare(sql) {
    return new SqliteD1Statement(this.raw, sql);
  }

  async batch(statements) {
    const operation = this.batchTail.then(() => this.runBatch(statements));
    this.batchTail = operation.catch(() => undefined);
    return operation;
  }

  async runBatch(statements) {
    this.raw.exec("BEGIN IMMEDIATE");
    try {
      const results = [];
      for (const statement of statements) results.push(await statement.run());
      this.raw.exec("COMMIT");
      return results;
    } catch (error) {
      this.raw.exec("ROLLBACK");
      throw error;
    }
  }

  close() {
    this.raw.close();
  }
}

async function ensureActionSchema(database) {
  await database.batch(prepareProjectActionSchema(database));
}

async function ensureStableSchema(database) {
  await applyMigration(database, "../drizzle/0004_stable_project_identity.sql");
}

async function applyMigration(database, relativePath) {
  const sql = await readFile(new URL(relativePath, import.meta.url), "utf8");
  for (const statement of sql.split("--> statement-breakpoint").map((part) => part.trim()).filter(Boolean)) {
    database.raw.exec(statement);
  }
}

function rows(database, sql, ...bindings) {
  return database.raw.prepare(sql).all(...bindings).map((row) => ({ ...row }));
}

async function ensureLegacyDecisionTables(database, { triggers = true } = {}) {
  await applyMigration(database, "../drizzle/0000_organic_the_professor.sql");
  await applyMigration(database, "../drizzle/0001_melted_surge.sql");
  if (!triggers) return;
  database.raw.exec(`
    CREATE TRIGGER IF NOT EXISTS feedback_insert_decision_event
    AFTER INSERT ON feedback
    BEGIN
      INSERT INTO decision_events (device_id, project_slug, value, created_at)
      VALUES (NEW.device_id, NEW.project_slug, NEW.value, NEW.updated_at);
    END;
    CREATE TRIGGER IF NOT EXISTS feedback_update_decision_event
    AFTER UPDATE OF value ON feedback
    WHEN OLD.value <> NEW.value
    BEGIN
      INSERT INTO decision_events (device_id, project_slug, value, created_at)
      VALUES (NEW.device_id, NEW.project_slug, NEW.value, NEW.updated_at);
    END;
  `);
}

async function ensureLegacyProjectTables(database) {
  await applyMigration(database, "../drizzle/0002_broken_zaladane.sql");
  await applyMigration(database, "../drizzle/0003_flaky_spacker_dave.sql");
  await ensureActionSchema(database);
}

async function ensureLegacyDecisionSchema(database, options) {
  await ensureLegacyDecisionTables(database, options);
  await ensureLegacyProjectTables(database);
}

async function identityCatalog(
  generationId,
  projects = [{ repository: "Owner/Project", projectSlug: "owner/project" }],
  publishedAt = "2026-07-16T00:00:00.000000Z",
) {
  return {
    generationId,
    publishedAt,
    projects: await Promise.all(projects.map(async ({ repository, projectSlug }) => {
      const identity = await identityForRepository(repository);
      return {
        projectIdVersion: identity.projectIdVersion,
        projectId: identity.projectId,
        projectSlug,
        repository,
      };
    })),
  };
}

function stableIdentity(catalog, index = 0) {
  const project = catalog.projects[index];
  return {
    projectIdVersion: project.projectIdVersion,
    projectId: project.projectId,
    projectSlug: project.projectSlug,
    catalogGenerationId: catalog.generationId,
  };
}

function tableCounts(database, tableNames) {
  return Object.fromEntries(tableNames.map((table) => [
    table,
    Number(rows(database, `SELECT COUNT(*) AS count FROM ${table}`)[0].count),
  ]));
}

const ADOPTION_TABLES = Object.freeze([
  "project_identity_catalog",
  "project_identity_runtime",
  "project_action_events_v2",
  "project_action_state_v2",
  "feedback_v2",
  "decision_events_v2",
]);

test("migrates legacy actions with their original times and keeps rollback compatibility", async () => {
  const database = new SqliteD1Database();
  try {
    await applyMigration(database, "../drizzle/0002_broken_zaladane.sql");
    const insertLegacy = database.raw.prepare(`
      INSERT INTO project_actions (device_id, project_slug, action, created_at)
      VALUES (?, ?, ?, ?)
    `);
    insertLegacy.run("legacy-device", "owner/project", "saved", "2026-06-01 02:03:04");
    insertLegacy.run("legacy-device", "owner/project", "tried", "2026-06-08T10:11:12+08:00");

    await applyMigration(database, "../drizzle/0003_flaky_spacker_dave.sql");
    await ensureActionSchema(database);

    const migrated = rows(
      database,
      `SELECT action, occurred_at, idempotency_key FROM project_action_events ORDER BY id`,
    );
    assert.deepEqual(migrated, [
      {
        action: "saved",
        occurred_at: "2026-06-01 02:03:04",
        idempotency_key: "legacy-project-actions:1",
      },
      {
        action: "tried",
        occurred_at: "2026-06-08T10:11:12+08:00",
        idempotency_key: "legacy-project-actions:2",
      },
    ]);

    const [state] = await readProjectActionState(database, "legacy-device", "owner/project");
    assert.equal(state.highestStage, "tried");
    assert.equal(state.savedAt, "2026-06-01 02:03:04");
    assert.equal(state.triedAt, "2026-06-08T10:11:12+08:00");
    assert.equal(state.openedAt, null);
    assert.equal(state.clonedAt, null);
    assert.equal(state.reusedAt, null);
    assert.deepEqual(
      stateToActionProjection([state]).map((item) => item.action),
      ["saved", "tried"],
      "State must not invent stages missing from the legacy facts",
    );

    await ensureActionSchema(database);
    assert.equal(rows(database, "SELECT id FROM project_action_events").length, 2);

    const newEvent = await appendProjectActionEvent(
      database,
      {
        deviceId: "legacy-device",
        projectSlug: "owner/project",
        action: "reused",
        idempotencyKey: "new-runtime-reused-0001",
      },
      "2026-07-14T12:00:00Z",
    );
    assert.equal(newEvent.recorded, true);
    assert.equal(
      rows(
        database,
        "SELECT COUNT(*) AS count FROM project_actions WHERE device_id = ? AND action = 'reused'",
        "legacy-device",
      )[0].count,
      1,
      "new events must remain visible to the retained legacy table after rollback",
    );

    insertLegacy.run("legacy-device", "owner/project", "cloned", "2026-07-15 00:00:00");
    const legacyCaptured = rows(
      database,
      `SELECT occurred_at, idempotency_key
       FROM project_action_events
       WHERE device_id = 'legacy-device' AND project_slug = 'owner/project' AND action = 'cloned'`,
    );
    assert.equal(legacyCaptured.length, 1);
    assert.equal(legacyCaptured[0].occurred_at, "2026-07-15 00:00:00");
    assert.match(legacyCaptured[0].idempotency_key, /^legacy-project-actions:\d+$/);
    assert.equal(
      (await readProjectActionState(database, "legacy-device", "owner/project"))[0].highestStage,
      "reused",
      "a later lower-stage event must not reduce the current highest stage",
    );
  } finally {
    database.close();
  }
});

test("keeps runtime bootstrap and the formal migration interoperable", async () => {
  const database = new SqliteD1Database();
  try {
    await applyMigration(database, "../drizzle/0002_broken_zaladane.sql");
    database.raw.prepare(`
      INSERT INTO project_actions (device_id, project_slug, action, created_at)
      VALUES (?, ?, ?, ?)
    `).run("upgrade-device", "owner/project", "tried", "2026-07-01 00:00:00");

    await ensureActionSchema(database);
    await applyMigration(database, "../drizzle/0003_flaky_spacker_dave.sql");
    await ensureActionSchema(database);

    assert.equal(rows(database, "SELECT id FROM project_action_events").length, 1);
    assert.equal(rows(database, "SELECT device_id FROM project_action_state").length, 1);
  } finally {
    database.close();
  }
});

test("fails closed instead of silently dropping invalid legacy actions or times", async () => {
  for (const [action, createdAt] of [
    ["unexpected", "2026-07-01 00:00:00"],
    ["tried", "not-a-timestamp"],
  ]) {
    const runtimeDatabase = new SqliteD1Database();
    try {
      await applyMigration(runtimeDatabase, "../drizzle/0002_broken_zaladane.sql");
      runtimeDatabase.raw.prepare(`
        INSERT INTO project_actions (device_id, project_slug, action, created_at)
        VALUES (?, ?, ?, ?)
      `).run("invalid-device", "owner/project", action, createdAt);
      await assert.rejects(ensureActionSchema(runtimeDatabase), /constraint failed/i);
      assert.equal(
        rows(
          runtimeDatabase,
          "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'project_action_events'",
        ).length,
        0,
        "the runtime schema batch must roll back instead of exposing a partial migration",
      );
    } finally {
      runtimeDatabase.close();
    }

    const migrationDatabase = new SqliteD1Database();
    try {
      await applyMigration(migrationDatabase, "../drizzle/0002_broken_zaladane.sql");
      migrationDatabase.raw.prepare(`
        INSERT INTO project_actions (device_id, project_slug, action, created_at)
        VALUES (?, ?, ?, ?)
      `).run("invalid-device", "owner/project", action, createdAt);
      await assert.rejects(
        applyMigration(migrationDatabase, "../drizzle/0003_flaky_spacker_dave.sql"),
        /constraint failed/i,
      );
    } finally {
      migrationDatabase.close();
    }
  }
});

test("uses idempotency keys without suppressing a later real action", async () => {
  const database = new SqliteD1Database();
  try {
    await ensureActionSchema(database);
    const input = {
      deviceId: "idem-device",
      projectSlug: "owner/project",
      action: "tried",
      idempotencyKey: "logical-attempt-0001",
    };
    const first = await appendProjectActionEvent(database, input, "2026-07-14T10:00:00Z");
    const replay = await appendProjectActionEvent(database, input, "2026-07-14T10:05:00Z");
    const collision = await appendProjectActionEvent(
      database,
      { ...input, action: "cloned" },
      "2026-07-14T10:06:00Z",
    );
    const laterAction = await appendProjectActionEvent(
      database,
      { ...input, idempotencyKey: "logical-attempt-0002" },
      "2026-07-21T10:00:00Z",
    );

    assert.equal(first.status, "recorded");
    assert.equal(replay.status, "replayed");
    assert.equal(replay.event.occurredAt, first.event.occurredAt);
    assert.equal(collision.status, "conflict");
    assert.equal(laterAction.status, "recorded");
    assert.equal(rows(database, "SELECT id FROM project_action_events").length, 2);
    assert.equal(
      rows(
        database,
        `SELECT created_at
         FROM project_actions
         WHERE device_id = ? AND project_slug = ? AND action = 'tried'`,
        input.deviceId,
        input.projectSlug,
      )[0].created_at,
      "2026-07-21 10:00:00.000",
      "the retained legacy State must project the latest occurrence for an immediate code rollback",
    );
    const [state] = await readProjectActionState(database, input.deviceId, input.projectSlug);
    assert.equal(state.highestStage, "tried");
    assert.equal(state.triedAt, "2026-07-21T10:00:00Z");
  } finally {
    database.close();
  }
});

test("keeps retained legacy weekly text queries correct at the seven-day cutoff", async () => {
  const database = new SqliteD1Database();
  const deviceId = "legacy-window-device";
  const now = "2026-07-14T12:00:00Z";
  const legacyWeeklyCount = () => rows(
    database,
    `SELECT COUNT(DISTINCT project_slug) AS count
     FROM project_actions
     WHERE device_id = ?
       AND action IN ('tried', 'cloned', 'reused')
       AND created_at >= datetime(?, '-7 days')`,
    deviceId,
    now,
  )[0].count;

  try {
    await ensureActionSchema(database);
    await appendProjectActionEvent(
      database,
      {
        deviceId,
        projectSlug: "owner/project",
        action: "tried",
        idempotencyKey: "legacy-window-old-0001",
      },
      "2026-07-07T01:00:00Z",
    );

    assert.equal(legacyWeeklyCount(), 0, "an event before the exact cutoff must stay excluded");
    assert.equal(
      rows(database, "SELECT created_at FROM project_actions WHERE device_id = ?", deviceId)[0].created_at,
      "2026-07-07 01:00:00.000",
      "the rollback projection must use the UTC text format expected by the old metric query",
    );

    await ensureActionSchema(database);
    assert.equal(
      rows(database, "SELECT id FROM project_action_events WHERE device_id = ?", deviceId).length,
      1,
      "replaying bootstrap must not capture the normalized projection as another Event",
    );

    await appendProjectActionEvent(
      database,
      {
        deviceId,
        projectSlug: "owner/project",
        action: "tried",
        idempotencyKey: "legacy-window-current-0002",
      },
      "2026-07-07T12:00:00.001Z",
    );
    assert.equal(legacyWeeklyCount(), 1, "an event inside the cutoff must remain visible after rollback");
    assert.equal(
      rows(database, "SELECT id FROM project_action_events WHERE device_id = ?", deviceId).length,
      2,
      "the compatibility projection must not fabricate a third Event",
    );
  } finally {
    database.close();
  }
});

test("calculates the inclusive seven-day event window across timezone formats", async () => {
  const database = new SqliteD1Database();
  const now = "2026-07-14T12:00:00Z";
  try {
    await ensureActionSchema(database);
    const record = (projectSlug, action, idempotencyKey, occurredAt) => appendProjectActionEvent(
      database,
      { deviceId: "window-device", projectSlug, action, idempotencyKey },
      occurredAt,
    );

    await record("owner/repeated", "tried", "old-week-tried-0001", "2026-07-06T12:00:00Z");
    assert.equal((await readWeeklyActionMetrics(database, "window-device", now)).actedProjects, 0);
    assert.equal(
      (await readProjectActionState(database, "window-device", "owner/repeated"))[0].highestStage,
      "tried",
      "current UI State must survive after its historical Event leaves the weekly window",
    );
    await record("owner/repeated", "tried", "new-week-tried-0002", now);
    await record("owner/boundary", "cloned", "boundary-cloned-0001", "2026-07-07T20:00:00+08:00");
    await record("owner/outside", "reused", "outside-reused-0001", "2026-07-07T11:59:59Z");
    await record("owner/opened", "opened", "inside-opened-0001", "2026-07-14T11:00:00Z");
    await record("owner/saved", "saved", "inside-saved-00001", "2026-07-14T11:30:00Z");

    const metrics = await readWeeklyActionMetrics(database, "window-device", now);
    assert.deepEqual(metrics, {
      actedProjects: 2,
      openedProjects: 1,
      savedProjects: 1,
      triedProjects: 1,
      clonedProjects: 1,
      reusedProjects: 0,
    });
    assert.equal(
      rows(
        database,
        "SELECT COUNT(*) AS count FROM project_action_events WHERE project_slug = 'owner/repeated' AND action = 'tried'",
      )[0].count,
      2,
      "the same project and action must remain appendable across weeks",
    );
  } finally {
    database.close();
  }
});

test("keeps Event append-only while its trigger updates State atomically", async () => {
  const database = new SqliteD1Database();
  try {
    await ensureActionSchema(database);
    await appendProjectActionEvent(
      database,
      {
        deviceId: "append-only-device",
        projectSlug: "owner/project",
        action: "opened",
        idempotencyKey: "append-only-opened-0001",
      },
      "2026-07-14T00:00:00Z",
    );
    assert.equal((await readProjectActionState(database, "append-only-device"))[0].highestStage, "opened");
    assert.throws(
      () => database.raw.prepare("UPDATE project_action_events SET action = 'saved'").run(),
      /append-only/,
    );
    assert.throws(
      () => database.raw.prepare("DELETE FROM project_action_events").run(),
      /append-only/,
    );
    assert.throws(
      () => database.raw.prepare(`
        INSERT OR REPLACE INTO project_action_events (
          id, device_id, project_slug, action, occurred_at, idempotency_key
        ) VALUES (1, 'append-only-device', 'owner/replaced', 'saved', '2026-07-15T00:00:00Z', 'replace-attempt-0001')
      `).run(),
      /identity is immutable/,
    );
    assert.equal(rows(database, "SELECT id FROM project_action_events").length, 1);
    assert.equal(
      rows(database, "SELECT project_slug FROM project_action_events")[0].project_slug,
      "owner/project",
    );
  } finally {
    database.close();
  }
});

test("records canonical stable actions and feedback while preserving every legacy projection", async () => {
  const database = new SqliteD1Database();
  try {
    database.raw.exec("PRAGMA recursive_triggers = ON");
    await ensureLegacyDecisionSchema(database);
    await ensureStableSchema(database);
    const catalog = await identityCatalog("generation-empty-adoption");
    const identity = stableIdentity(catalog);

    assert.deepEqual(await adoptStableProjectIdentities(database, catalog), {
      status: "ready",
      generationId: catalog.generationId,
      projectCount: 1,
    });
    assert.deepEqual(tableCounts(database, ADOPTION_TABLES), {
      project_identity_catalog: 1,
      project_identity_runtime: 1,
      project_action_events_v2: 0,
      project_action_state_v2: 0,
      feedback_v2: 0,
      decision_events_v2: 0,
    });

    const input = {
      deviceId: "stable-empty-device",
      ...identity,
      action: "tried",
      idempotencyKey: "stable-empty-tried-0001",
    };
    const first = await appendStableProjectActionEvent(database, input, "2026-07-16T01:02:03Z");
    const replay = await appendStableProjectActionEvent(database, input, "2026-07-16T02:00:00Z");
    const conflict = await appendStableProjectActionEvent(
      database,
      { ...input, action: "cloned" },
      "2026-07-16T03:00:00Z",
    );
    assert.equal(first.status, "recorded");
    assert.equal(replay.status, "replayed");
    assert.equal(replay.event.occurredAt, first.event.occurredAt);
    assert.equal(conflict.status, "conflict");

    const [state] = await readStableProjectActionState(database, input.deviceId, identity.projectId);
    assert.equal(state.highestStage, "tried");
    assert.equal(state.triedAt, "2026-07-16T01:02:03Z");
    assert.deepEqual(
      stableStateToActionProjection([state]).map((item) => item.action),
      ["tried"],
    );
    assert.equal(rows(database, "SELECT COUNT(*) AS count FROM project_action_events")[0].count, 1);
    assert.equal(rows(database, "SELECT COUNT(*) AS count FROM project_action_state")[0].count, 1);
    assert.equal(rows(database, "SELECT COUNT(*) AS count FROM project_actions")[0].count, 1);

    const concurrentInput = {
      ...input,
      action: "reused",
      idempotencyKey: "stable-concurrent-reused-0001",
    };
    const concurrent = await Promise.all([
      appendStableProjectActionEvent(database, concurrentInput, "2026-07-16T03:10:00Z"),
      appendStableProjectActionEvent(database, concurrentInput, "2026-07-16T03:11:00Z"),
    ]);
    assert.deepEqual(concurrent.map((result) => result.status).sort(), ["recorded", "replayed"]);
    assert.equal(
      rows(
        database,
        "SELECT id FROM project_action_events_v2 WHERE idempotency_key = ?",
        concurrentInput.idempotencyKey,
      ).length,
      1,
    );

    const useful = await upsertStableFeedback(
      database,
      { deviceId: input.deviceId, ...identity, value: "有用" },
      "2026-07-16T04:00:00Z",
    );
    const usefulReplay = await upsertStableFeedback(
      database,
      { deviceId: input.deviceId, ...identity, value: "有用" },
      "2026-07-16T04:30:00Z",
    );
    const reused = await upsertStableFeedback(
      database,
      { deviceId: input.deviceId, ...identity, value: "复用" },
      "2026-07-16T05:00:00Z",
    );
    assert.equal(useful.changed, true);
    assert.equal(usefulReplay.changed, false);
    assert.equal(usefulReplay.feedback.updatedAt, "2026-07-16T04:00:00Z");
    assert.equal(reused.changed, true);
    assert.equal((await readStableFeedback(database, input.deviceId))[0].value, "复用");
    assert.deepEqual(rows(database, "SELECT value FROM feedback"), [{ value: "复用" }]);
    assert.deepEqual(
      rows(database, "SELECT value, created_at FROM decision_events ORDER BY id"),
      [
        { value: "有用", created_at: "2026-07-16T04:00:00Z" },
        { value: "复用", created_at: "2026-07-16T05:00:00Z" },
      ],
    );
    assert.equal(rows(database, "SELECT id FROM decision_events_v2").length, 2);
    assert.deepEqual(
      await readStableWeeklyFeedbackMetrics(database, input.deviceId, "2026-07-16T06:00:00Z"),
      { effectiveDecisions: 1, reuseDecisions: 1, feedbackChanges: 2 },
    );
  } finally {
    database.close();
  }
});

test("captures rollback-era legacy writes into stable storage with recursive triggers enabled", async () => {
  const database = new SqliteD1Database();
  try {
    database.raw.exec("PRAGMA recursive_triggers = ON");
    await ensureLegacyDecisionSchema(database);
    await ensureStableSchema(database);
    const catalog = await identityCatalog("generation-rollback-capture");
    const identity = stableIdentity(catalog);
    await adoptStableProjectIdentities(database, catalog);

    await appendProjectActionEvent(
      database,
      {
        deviceId: "rollback-device",
        projectSlug: identity.projectSlug,
        action: "cloned",
        idempotencyKey: "rollback-cloned-0001",
      },
      "2026-07-16T06:00:00Z",
    );
    const [capturedAction] = rows(database, `SELECT project_id AS projectId,
      catalog_generation_id AS generationId, action FROM project_action_events_v2`);
    assert.deepEqual(capturedAction, {
      projectId: identity.projectId,
      generationId: catalog.generationId,
      action: "cloned",
    });

    database.raw.prepare(`INSERT INTO feedback (
      device_id, project_slug, value, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?)`).run(
      "rollback-device",
      identity.projectSlug,
      "待确定",
      "2026-07-16T06:30:00Z",
      "2026-07-16T06:30:00Z",
    );
    database.raw.prepare(`UPDATE feedback SET value = ?, updated_at = ?
      WHERE device_id = ? AND project_slug = ?`).run(
      "无用",
      "2026-07-16T07:00:00Z",
      "rollback-device",
      identity.projectSlug,
    );
    const [capturedFeedback] = await readStableFeedback(database, "rollback-device");
    assert.equal(capturedFeedback.projectId, identity.projectId);
    assert.equal(capturedFeedback.value, "无用");
    assert.equal(capturedFeedback.createdAt, "2026-07-16T06:30:00Z");
    assert.equal(capturedFeedback.updatedAt, "2026-07-16T07:00:00Z");
    assert.deepEqual(
      rows(database, `SELECT value, occurred_at AS occurredAt
        FROM decision_events_v2 ORDER BY id`),
      [
        { value: "待确定", occurredAt: "2026-07-16T06:30:00Z" },
        { value: "无用", occurredAt: "2026-07-16T07:00:00Z" },
      ],
    );

    assert.throws(
      () => database.raw.prepare("UPDATE project_action_events_v2 SET action = 'reused'").run(),
      /append-only/,
    );
    assert.throws(
      () => database.raw.prepare("DELETE FROM project_action_events_v2").run(),
      /append-only/,
    );
    assert.throws(
      () => database.raw.prepare(`INSERT OR REPLACE INTO project_action_events_v2 (
        id, device_id, project_id_version, project_id, project_slug,
        catalog_generation_id, action, occurred_at, idempotency_key
      ) VALUES (1, ?, 1, ?, ?, ?, 'reused', ?, ?)`)
        .run("rollback-device", identity.projectId, identity.projectSlug,
          catalog.generationId, "2026-07-16T08:00:00Z", "rollback-replace-0001"),
      /identity is immutable/,
    );
    assert.throws(
      () => database.raw.prepare("UPDATE decision_events_v2 SET value = '有用'").run(),
      /append-only/,
    );
    assert.throws(
      () => database.raw.prepare("DELETE FROM decision_events_v2").run(),
      /append-only/,
    );
  } finally {
    database.close();
  }
});

test("adopts legacy action and decision facts with original times and is idempotent", async () => {
  const database = new SqliteD1Database();
  try {
    await ensureLegacyDecisionTables(database, { triggers: false });
    await ensureLegacyProjectTables(database);
    await appendProjectActionEvent(
      database,
      {
        deviceId: "migration-device",
        projectSlug: "owner/project",
        action: "reused",
        idempotencyKey: "migration-reused-0001",
      },
      "2026-06-30T01:02:03Z",
    );
    database.raw.prepare(`INSERT INTO feedback (
      device_id, project_slug, value, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?)`).run(
      "migration-device", "owner/project", "有用",
      "2026-06-29T00:00:00Z", "2026-06-30T02:00:00Z",
    );
    database.raw.prepare(`INSERT INTO decision_events (
      device_id, project_slug, value, created_at
    ) VALUES (?, ?, ?, ?)`).run(
      "migration-device", "owner/project", "有用", "2026-06-30T02:00:00Z",
    );

    await ensureStableSchema(database);
    const catalog = await identityCatalog("generation-migrate-existing");
    await adoptStableProjectIdentities(database, catalog);
    const firstCounts = tableCounts(database, ADOPTION_TABLES);

    assert.deepEqual(
      rows(database, `SELECT action, occurred_at AS occurredAt,
        idempotency_key AS idempotencyKey FROM project_action_events_v2`),
      [{
        action: "reused",
        occurredAt: "2026-06-30T01:02:03Z",
        idempotencyKey: "migration-reused-0001",
      }],
    );
    assert.deepEqual(
      rows(database, `SELECT value, created_at AS createdAt, updated_at AS updatedAt
        FROM feedback_v2`),
      [{
        value: "有用",
        createdAt: "2026-06-29T00:00:00Z",
        updatedAt: "2026-06-30T02:00:00Z",
      }],
    );
    assert.deepEqual(
      rows(database, "SELECT value, occurred_at AS occurredAt FROM decision_events_v2"),
      [{ value: "有用", occurredAt: "2026-06-30T02:00:00Z" }],
    );
    assert.equal((await readStableProjectActionState(database, "migration-device"))[0].reusedAt,
      "2026-06-30T01:02:03Z");

    await adoptStableProjectIdentities(database, catalog);
    assert.deepEqual(tableCounts(database, ADOPTION_TABLES), firstCounts);
  } finally {
    database.close();
  }
});

test("keeps formal migration and runtime bootstrap interoperable in both orders", async () => {
  for (const order of ["runtime-first", "migration-first"]) {
    const database = new SqliteD1Database();
    try {
      database.raw.exec("PRAGMA recursive_triggers = ON");
      await ensureLegacyDecisionSchema(database);
      if (order === "runtime-first") {
        await ensureStableSchema(database);
        await applyMigration(database, "../drizzle/0004_stable_project_identity.sql");
      } else {
        await applyMigration(database, "../drizzle/0004_stable_project_identity.sql");
        await ensureStableSchema(database);
      }
      const catalog = await identityCatalog(`generation-${order}`);
      const identity = stableIdentity(catalog);
      await adoptStableProjectIdentities(database, catalog);
      await appendStableProjectActionEvent(database, {
        deviceId: `${order}-device`,
        ...identity,
        action: "opened",
        idempotencyKey: `${order}-opened-0001`,
      }, "2026-07-16T08:00:00Z");
      assert.equal(rows(database, "SELECT id FROM project_action_events_v2").length, 1, order);
      assert.equal(rows(database, "SELECT id FROM project_action_events").length, 1, order);
      assert.equal(rows(database, "SELECT device_id FROM project_action_state_v2").length, 1, order);
    } finally {
      database.close();
    }
  }
});

test("formal 0004 alone installs the complete stable identity integrity boundary", async () => {
  const database = new SqliteD1Database();
  try {
    database.raw.exec("PRAGMA recursive_triggers = ON");
    await applyMigration(database, "../drizzle/0000_organic_the_professor.sql");
    await applyMigration(database, "../drizzle/0001_melted_surge.sql");
    await applyMigration(database, "../drizzle/0002_broken_zaladane.sql");
    await applyMigration(database, "../drizzle/0003_flaky_spacker_dave.sql");
    await applyMigration(database, "../drizzle/0004_stable_project_identity.sql");

    const formalSql = await readFile(
      new URL("../drizzle/0004_stable_project_identity.sql", import.meta.url),
      "utf8",
    );
    const formalTriggerStatements = formalSql
      .split("--> statement-breakpoint")
      .map((statement) => statement.trim())
      .filter((statement) => statement.startsWith("CREATE TRIGGER"))
      .map((statement) => statement.replace(/\s+/g, " "))
      .sort();
    assert.equal(formalTriggerStatements.length, 42);
    const expectedTriggers = formalTriggerStatements
      .map((statement) => statement.match(/TRIGGER IF NOT EXISTS\s+(\w+)/)?.[1])
      .filter(Boolean)
      .sort();
    assert.equal(new Set(expectedTriggers).size, expectedTriggers.length);
    for (const required of [
      "feedback_insert_decision_event",
      "feedback_update_decision_event",
      "project_identity_catalog_reject_global_collision",
      "project_identity_catalog_reject_replacement",
      "project_action_events_v2_validate_active_generation",
      "project_action_events_v2_reject_update",
      "project_action_events_v2_reject_delete",
      "project_action_state_v2_validate_time_insert",
      "project_action_state_v2_validate_time_update",
      "project_action_state_v2_validate_projection_update",
      "feedback_v2_validate_active_generation_update",
      "feedback_v2_reject_delete",
      "decision_events_v2_reject_update",
      "decision_events_v2_reject_delete",
    ]) assert.ok(expectedTriggers.includes(required), required);
    const installedTriggers = rows(
      database,
      "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name",
    ).map((row) => row.name).filter((name) => expectedTriggers.includes(name)).sort();
    assert.deepEqual(installedTriggers, expectedTriggers);

    const catalog = await identityCatalog("generation-formal-only");
    const identity = stableIdentity(catalog);
    await adoptStableProjectIdentities(database, catalog);
    await appendStableProjectActionEvent(database, {
      deviceId: "formal-only-device",
      ...identity,
      action: "opened",
      idempotencyKey: "formal-only-opened-0001",
    }, "2026-07-16T08:00:00Z");
    assert.equal(rows(database, "SELECT id FROM project_action_events").length, 1);
    await upsertStableFeedback(database, {
      deviceId: "formal-only-device",
      ...identity,
      value: "有用",
    });
    await upsertStableFeedback(database, {
      deviceId: "formal-only-device",
      ...identity,
      value: "复用",
    });
    assert.equal(rows(database, "SELECT id FROM decision_events").length, 2);
    assert.equal(rows(database, "SELECT id FROM decision_events_v2").length, 2);
    await assert.rejects(
      Promise.resolve().then(() => database.raw.exec(
        "UPDATE project_action_events_v2 SET action = 'tried'",
      )),
      /append-only/,
    );
    await assert.rejects(
      Promise.resolve().then(() => database.raw.exec(
        "UPDATE project_action_state_v2 SET highest_stage = 'reused'",
      )),
      /not an Event projection/,
    );
    const stateBeforeInvalidTime = rows(
      database,
      "SELECT * FROM project_action_state_v2 ORDER BY device_id, project_id",
    );
    assert.throws(
      () => database.raw.exec(
        "UPDATE project_action_state_v2 SET updated_at = 'not-a-time'",
      ),
      /invalid timestamp/,
    );
    assert.throws(
      () => database.raw.exec(
        "UPDATE project_action_state_v2 SET opened_at = 'not-a-time'",
      ),
      /invalid timestamp/,
    );
    assert.deepEqual(
      rows(database, "SELECT * FROM project_action_state_v2 ORDER BY device_id, project_id"),
      stateBeforeInvalidTime,
      "invalid State timestamps must not change the Event projection",
    );
    await assert.rejects(
      Promise.resolve().then(() => database.raw.exec("DELETE FROM project_identity_catalog")),
      /immutable/,
    );
    for (const recursive of ["OFF", "ON"]) {
      database.raw.exec(`PRAGMA recursive_triggers = ${recursive}`);
      await assert.rejects(
        Promise.resolve().then(() => database.raw.prepare(`INSERT OR REPLACE INTO project_identity_catalog (
          generation_id, project_id_version, project_id, canonical_repository, project_slug
        ) VALUES (?, ?, ?, ?, ?)`).run(
          catalog.generationId,
          identity.projectIdVersion,
          identity.projectId,
          catalog.projects[0].repository.toLowerCase(),
          "forged/replacement",
        )),
        /mapping is immutable/,
      );
    }
    const conflictingSlugCatalog = await identityCatalog("generation-formal-slug-conflict", [
      { repository: "owner/other", projectSlug: identity.projectSlug },
    ], "2026-07-16T08:00:00.000001Z");
    const conflictingSlug = stableIdentity(conflictingSlugCatalog);
    assert.throws(
      () => database.raw.prepare(`INSERT INTO project_identity_catalog (
        generation_id, project_id_version, project_id, canonical_repository, project_slug
      ) VALUES (?, ?, ?, ?, ?)`).run(
        conflictingSlugCatalog.generationId,
        conflictingSlug.projectIdVersion,
        conflictingSlug.projectId,
        conflictingSlugCatalog.projects[0].repository.toLowerCase(),
        conflictingSlug.projectSlug,
      ),
      /stable project identity collision/,
    );
    assert.equal(
      rows(database, "SELECT project_slug AS projectSlug FROM project_identity_catalog")[0].projectSlug,
      identity.projectSlug,
    );
  } finally {
    database.close();
  }
});

test("fails identity adoption closed before any write for unresolved, invalid, or unsupported legacy facts", async (t) => {
  const failureCase = async (name, arrange, expectedCode = "invalid_legacy_project_row") => {
    await t.test(name, async () => {
      const database = new SqliteD1Database();
      try {
        await ensureLegacyDecisionSchema(database);
        await ensureStableSchema(database);
        await arrange(database);
        const catalog = await identityCatalog(`generation-failure-${name.replaceAll(" ", "-")}`);
        const before = tableCounts(database, ADOPTION_TABLES);
        await assert.rejects(
          adoptStableProjectIdentities(database, catalog),
          (error) => error?.code === expectedCode,
        );
        assert.deepEqual(tableCounts(database, ADOPTION_TABLES), before);
        assert.equal(rows(database, "SELECT generation_id FROM project_identity_runtime").length, 0);
      } finally {
        database.close();
      }
    });
  };

  await failureCase("unresolved slug", async (database) => {
    await appendProjectActionEvent(database, {
      deviceId: "unresolved-device",
      projectSlug: "removed/project",
      action: "tried",
      idempotencyKey: "unresolved-tried-0001",
    }, "2026-07-16T00:00:00Z");
  }, "unresolved_project_identity");

  await failureCase("invalid feedback", async (database) => {
    database.raw.prepare(`INSERT INTO feedback (
      device_id, project_slug, value, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?)`).run(
      "invalid-feedback-device", "owner/project", "invalid",
      "2026-07-16T00:00:00Z", "2026-07-16T00:00:00Z",
    );
  });

  for (const [name, action, occurredAt] of [
    ["invalid action", "invalid", "2026-07-16T00:00:00Z"],
    ["invalid action time", "tried", "not-a-timestamp"],
  ]) {
    await failureCase(name, async (database) => {
      database.raw.exec(`
        DROP TRIGGER project_action_events_sync_state;
        DROP TRIGGER project_action_events_legacy_projection;
        PRAGMA ignore_check_constraints = ON;
      `);
      database.raw.prepare(`INSERT INTO project_action_events (
        device_id, project_slug, action, occurred_at, idempotency_key
      ) VALUES (?, ?, ?, ?, ?)`).run(
        "invalid-action-device", "owner/project", action, occurredAt, `${name}-0001`,
      );
      database.raw.exec("PRAGMA ignore_check_constraints = OFF");
      await ensureActionSchema(database);
    });
  }

  await failureCase("state without event", async (database) => {
    database.raw.prepare(`INSERT INTO project_action_state (
      device_id, project_slug, highest_stage, tried_at, updated_at
    ) VALUES (?, ?, ?, ?, ?)`).run(
      "unsupported-state-device", "owner/project", "tried",
      "2026-07-16T00:00:00Z", "2026-07-16T00:00:00Z",
    );
  });
});

test("rejects ambiguous catalogs and non-equivalent stable targets without partial adoption", async () => {
  const ambiguousDatabase = new SqliteD1Database();
  try {
    await ensureLegacyDecisionSchema(ambiguousDatabase);
    await ensureStableSchema(ambiguousDatabase);
    const ambiguous = await identityCatalog("generation-ambiguous", [
      { repository: "owner/one", projectSlug: "shared/slug" },
      { repository: "owner/two", projectSlug: "shared/slug" },
    ]);
    await assert.rejects(
      adoptStableProjectIdentities(ambiguousDatabase, ambiguous),
      (error) => error?.code === "ambiguous_project_identity",
    );
    assert.deepEqual(tableCounts(ambiguousDatabase, ADOPTION_TABLES), {
      project_identity_catalog: 0,
      project_identity_runtime: 0,
      project_action_events_v2: 0,
      project_action_state_v2: 0,
      feedback_v2: 0,
      decision_events_v2: 0,
    });
  } finally {
    ambiguousDatabase.close();
  }

  const conflictDatabase = new SqliteD1Database();
  try {
    await ensureLegacyDecisionSchema(conflictDatabase);
    await ensureStableSchema(conflictDatabase);
    const catalog = await identityCatalog("generation-conflicting-target", [
      { repository: "owner/project", projectSlug: "owner/project" },
      { repository: "owner/unrelated", projectSlug: "owner/unrelated" },
    ], "2026-07-16T00:00:00.000002Z");
    const identity = stableIdentity(catalog);
    const seedCatalog = await identityCatalog(
      "generation-conflicting-seed",
      undefined,
      "2026-07-16T00:00:00.000001Z",
    );
    const seedIdentity = stableIdentity(seedCatalog);
    const insertMapping = conflictDatabase.raw.prepare(`INSERT INTO project_identity_catalog (
      generation_id, project_id_version, project_id, canonical_repository, project_slug
    ) VALUES (?, ?, ?, ?, ?)`);
    insertMapping.run(
      seedCatalog.generationId,
      seedIdentity.projectIdVersion,
      seedIdentity.projectId,
      seedCatalog.projects[0].repository.toLowerCase(),
      seedIdentity.projectSlug,
    );
    conflictDatabase.raw.prepare(`INSERT INTO project_identity_runtime (
      singleton, generation_id, published_at, published_at_micros
    ) VALUES (1, ?, ?, ?)`).run(
      seedCatalog.generationId,
      seedCatalog.publishedAt,
      Date.parse(seedCatalog.publishedAt) * 1000 + 1,
    );
    conflictDatabase.raw.exec(`
      DROP TRIGGER feedback_v2_legacy_projection_insert;
      DROP TRIGGER feedback_insert_stable_capture;
    `);
    conflictDatabase.raw.prepare(`INSERT INTO feedback_v2 (
      device_id, project_id_version, project_id, project_slug,
      catalog_generation_id, value, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`).run(
      "conflict-device", identity.projectIdVersion, identity.projectId, identity.projectSlug,
      seedCatalog.generationId, "有用", "2026-07-15T00:00:00Z", "2026-07-15T00:00:00Z",
    );
    conflictDatabase.raw.prepare(`INSERT INTO feedback (
      device_id, project_slug, value, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?)`).run(
      "conflict-device", identity.projectSlug, "无用",
      "2026-07-16T00:00:00Z", "2026-07-16T00:00:00Z",
    );
    conflictDatabase.raw.exec("DELETE FROM project_identity_runtime");
    await ensureStableSchema(conflictDatabase);
    const before = Object.fromEntries(ADOPTION_TABLES.map((table) => [
      table,
      rows(conflictDatabase, `SELECT * FROM ${table} ORDER BY rowid`),
    ]));
    await assert.rejects(
      adoptStableProjectIdentities(conflictDatabase, catalog),
      (error) => error?.code === "conflicting_project_projection",
    );
    const after = Object.fromEntries(ADOPTION_TABLES.map((table) => [
      table,
      rows(conflictDatabase, `SELECT * FROM ${table} ORDER BY rowid`),
    ]));
    assert.deepEqual(after, before);
    assert.equal(rows(conflictDatabase, "SELECT generation_id FROM project_identity_runtime").length, 0);
    assert.equal(
      rows(conflictDatabase, "SELECT project_id FROM project_identity_catalog").length,
      1,
      "a conflict in one project must prevent unrelated current-generation mappings",
    );
  } finally {
    conflictDatabase.close();
  }
});

test("rechecks adoption inside the transaction when legacy facts race the preflight", async () => {
  const database = new SqliteD1Database();
  try {
    await ensureLegacyDecisionSchema(database);
    await ensureStableSchema(database);
    const catalog = await identityCatalog("generation-raced-adoption");
    const originalBatch = database.batch.bind(database);
    let injectBeforeNextBatch = true;
    database.batch = async (statements) => {
      if (injectBeforeNextBatch) {
        injectBeforeNextBatch = false;
        database.raw.prepare(`INSERT INTO feedback (
          device_id, project_slug, value, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)`).run(
          "raced-device",
          "removed/project",
          "有用",
          "2026-07-16T09:00:00Z",
          "2026-07-16T09:00:00Z",
        );
      }
      return originalBatch(statements);
    };

    await assert.rejects(
      adoptStableProjectIdentities(database, catalog),
      (error) => error?.code === "unresolved_project_identity",
    );
    assert.deepEqual(tableCounts(database, ADOPTION_TABLES), {
      project_identity_catalog: 0,
      project_identity_runtime: 0,
      project_action_events_v2: 0,
      project_action_state_v2: 0,
      feedback_v2: 0,
      decision_events_v2: 0,
    });
    assert.equal(rows(database, "SELECT device_id FROM feedback").length, 1);
    assert.equal(rows(database, "SELECT device_id FROM decision_events").length, 1);
  } finally {
    database.close();
  }
});

test("rejects retained identity collisions and closes the preflight-to-batch race", async (t) => {
  await t.test("retained mappings are validated against each other", async () => {
    const database = new SqliteD1Database();
    try {
      await ensureLegacyDecisionSchema(database);
      await ensureStableSchema(database);
      database.raw.exec("DROP TRIGGER project_identity_catalog_reject_global_collision");
      const first = await identityCatalog("generation-corrupt-one", [
        { repository: "owner/one", projectSlug: "legacy/one" },
      ]);
      const second = await identityCatalog("generation-corrupt-two", [
        { repository: "owner/two", projectSlug: "legacy/two" },
      ]);
      const insert = database.raw.prepare(`INSERT INTO project_identity_catalog (
        generation_id, project_id_version, project_id, canonical_repository, project_slug
      ) VALUES (?, ?, ?, ?, ?)`);
      insert.run(first.generationId, 1, first.projects[0].projectId, "owner/shared", "legacy/one");
      insert.run(second.generationId, 1, second.projects[0].projectId, "owner/shared", "legacy/two");
      const current = await identityCatalog("generation-unrelated", [
        { repository: "owner/current", projectSlug: "owner/current" },
      ], "2026-07-16T00:00:00.000001Z");
      const before = tableCounts(database, ADOPTION_TABLES);
      await assert.rejects(
        adoptStableProjectIdentities(database, current),
        (error) => error?.code === "project_identity_collision",
      );
      assert.deepEqual(tableCounts(database, ADOPTION_TABLES), before);
      assert.equal(rows(database, "SELECT generation_id FROM project_identity_runtime").length, 0);
    } finally {
      database.close();
    }
  });

  await t.test("a conflicting retained mapping inserted after preflight aborts the whole adoption", async () => {
    const database = new SqliteD1Database();
    try {
      await ensureLegacyDecisionSchema(database);
      await ensureStableSchema(database);
      const current = await identityCatalog("generation-raced-identity", undefined,
        "2026-07-16T00:00:00.000002Z");
      const identity = stableIdentity(current);
      const originalBatch = database.batch.bind(database);
      let injectBeforeNextBatch = true;
      database.batch = async (statements) => {
        if (injectBeforeNextBatch) {
          injectBeforeNextBatch = false;
          database.raw.prepare(`INSERT INTO project_identity_catalog (
            generation_id, project_id_version, project_id, canonical_repository, project_slug
          ) VALUES (?, ?, ?, ?, ?)`).run(
            "generation-race-winner",
            identity.projectIdVersion,
            identity.projectId,
            "different/repository",
            "different/repository",
          );
        }
        return originalBatch(statements);
      };
      await assert.rejects(
        adoptStableProjectIdentities(database, current),
        (error) => error?.code === "project_identity_collision",
      );
      assert.deepEqual(
        rows(database, "SELECT generation_id AS generationId FROM project_identity_catalog"),
        [{ generationId: "generation-race-winner" }],
      );
      assert.equal(rows(database, "SELECT generation_id FROM project_identity_runtime").length, 0);
      assert.equal(rows(database, "SELECT id FROM project_action_events_v2").length, 0);
    } finally {
      database.close();
    }
  });
});

test("fails a slug rekey closed when the legacy target is occupied", async (t) => {
  const exercise = async (kind) => {
    const database = new SqliteD1Database();
    try {
      await ensureLegacyDecisionSchema(database);
      await ensureStableSchema(database);
      const first = await identityCatalog(`generation-${kind}-rekey-source`, [
        { repository: "owner/project", projectSlug: "legacy/project" },
      ], "2026-07-16T00:00:00.000001Z");
      await adoptStableProjectIdentities(database, first);
      const identity = stableIdentity(first);
      if (kind === "state") {
        await appendStableProjectActionEvent(database, {
          deviceId: "rekey-conflict-device",
          ...identity,
          action: "tried",
          idempotencyKey: "rekey-conflict-tried-0001",
        }, "2026-07-16T01:00:00Z");
        database.raw.prepare(`INSERT INTO project_action_state (
          device_id, project_slug, highest_stage, reused_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)`).run(
          "rekey-conflict-device",
          "renamed/project",
          "reused",
          "2026-07-16T02:00:00Z",
          "2026-07-16T02:00:00Z",
        );
      } else {
        await upsertStableFeedback(database, {
          deviceId: "rekey-conflict-device",
          ...identity,
          value: "有用",
        }, "2026-07-16T01:00:00Z");
        database.raw.prepare(`INSERT INTO feedback (
          device_id, project_slug, value, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)`).run(
          "rekey-conflict-device",
          "renamed/project",
          "无用",
          "2026-07-16T02:00:00Z",
          "2026-07-16T02:00:00Z",
        );
      }
      const second = await identityCatalog(`generation-${kind}-rekey-target`, [
        { repository: "owner/project", projectSlug: "renamed/project" },
      ], "2026-07-16T00:00:00.000002Z");
      const tracked = [...ADOPTION_TABLES, "project_action_state", "feedback", "decision_events"];
      const before = Object.fromEntries(tracked.map((table) => [
        table,
        rows(database, `SELECT * FROM ${table} ORDER BY rowid`),
      ]));
      await assert.rejects(
        adoptStableProjectIdentities(database, second),
        (error) => error?.code === "conflicting_project_projection",
      );
      const after = Object.fromEntries(tracked.map((table) => [
        table,
        rows(database, `SELECT * FROM ${table} ORDER BY rowid`),
      ]));
      assert.deepEqual(after, before, `${kind} conflict must not partially switch or rekey`);
      assert.deepEqual(
        rows(database, "SELECT generation_id AS generationId FROM project_identity_runtime"),
        [{ generationId: first.generationId }],
      );
    } finally {
      database.close();
    }
  };
  await t.test("action State", () => exercise("state"));
  await t.test("feedback State", () => exercise("feedback"));
});

test("serializes competing Catalog publishers and rejects corrupted activation metadata", async (t) => {
  await t.test("the newer publisher deterministically owns the active generation", async () => {
    const database = new SqliteD1Database();
    try {
      await ensureLegacyDecisionSchema(database);
      await ensureStableSchema(database);
      const older = await identityCatalog("generation-publisher-older", undefined,
        "2026-07-16T00:00:00.000001Z");
      const newer = await identityCatalog("generation-publisher-newer", undefined,
        "2026-07-16T00:00:00.000002Z");
      const [olderResult, newerResult] = await Promise.allSettled([
        adoptStableProjectIdentities(database, older),
        adoptStableProjectIdentities(database, newer),
      ]);
      assert.equal(newerResult.status, "fulfilled");
      if (olderResult.status === "rejected") {
        assert.ok([
          "stale_project_identity_generation",
          "concurrent_project_identity_adoption_conflict",
        ].includes(olderResult.reason?.code));
      }
      assert.deepEqual(
        rows(database, `SELECT generation_id AS generationId, published_at AS publishedAt
          FROM project_identity_runtime`),
        [{ generationId: newer.generationId, publishedAt: newer.publishedAt }],
      );
      assert.equal(
        rows(database, `SELECT COUNT(*) AS count FROM project_identity_catalog
          WHERE generation_id = ?`, newer.generationId)[0].count,
        newer.projects.length,
      );
    } finally {
      database.close();
    }
  });

  await t.test("publishedAt text and microsecond order must remain an exact pair", async () => {
    const database = new SqliteD1Database();
    try {
      await ensureLegacyDecisionSchema(database);
      await ensureStableSchema(database);
      const first = await identityCatalog("generation-runtime-valid", undefined,
        "2026-07-16T00:00:00.000001Z");
      await adoptStableProjectIdentities(database, first);
      database.raw.exec(`UPDATE project_identity_runtime
        SET published_at_micros = published_at_micros + 1 WHERE singleton = 1`);
      const before = rows(database, "SELECT * FROM project_identity_runtime");
      const later = await identityCatalog("generation-runtime-rejected", undefined,
        "2026-07-16T00:00:00.000002Z");
      await assert.rejects(
        adoptStableProjectIdentities(database, later),
        (error) => error?.code === "invalid_project_identity_runtime",
      );
      assert.deepEqual(rows(database, "SELECT * FROM project_identity_runtime"), before);
      assert.equal(
        rows(database, "SELECT generation_id FROM project_identity_catalog WHERE generation_id = ?",
          later.generationId).length,
        0,
      );
    } finally {
      database.close();
    }
  });
});

test("switches Catalog generations without rewriting stable history or double-counting renamed slugs", async () => {
  const database = new SqliteD1Database();
  try {
    database.raw.exec("PRAGMA recursive_triggers = ON");
    await ensureLegacyDecisionSchema(database);
    await ensureStableSchema(database);
    const firstCatalog = await identityCatalog("generation-before-rename", [
      { repository: "owner/project", projectSlug: "legacy/project" },
      { repository: "owner/passive", projectSlug: "owner/passive" },
    ], "2026-07-16T00:00:00.000001Z");
    await adoptStableProjectIdentities(database, firstCatalog);
    const firstIdentity = stableIdentity(firstCatalog);
    const passiveIdentity = stableIdentity(firstCatalog, 1);

    const record = (identity, action, key, time) => appendStableProjectActionEvent(database, {
      deviceId: "generation-device",
      ...identity,
      action,
      idempotencyKey: key,
    }, time);
    await record(firstIdentity, "tried", "renamed-old-week-0001", "2026-07-08T08:00:00Z");
    await record(firstIdentity, "tried", "renamed-current-week-0002", "2026-07-15T10:00:00Z");
    await record(passiveIdentity, "opened", "passive-opened-0001", "2026-07-15T11:00:00Z");
    await record(passiveIdentity, "saved", "passive-saved-0001", "2026-07-15T12:00:00Z");
    await upsertStableFeedback(database, {
      deviceId: "generation-device",
      ...firstIdentity,
      value: "有用",
    }, "2026-07-15T12:30:00Z");
    const historyBeforeRename = tableCounts(database, [
      "project_action_events",
      "project_action_events_v2",
      "decision_events",
      "decision_events_v2",
    ]);

    const secondCatalog = await identityCatalog("generation-after-rename", [
      { repository: "owner/project", projectSlug: "renamed/project" },
      { repository: "owner/passive", projectSlug: "owner/passive" },
    ], "2026-07-16T00:00:00.000002Z");
    await adoptStableProjectIdentities(database, secondCatalog);
    const renamedIdentity = stableIdentity(secondCatalog);
    assert.equal(renamedIdentity.projectId, firstIdentity.projectId);
    assert.deepEqual(
      (await readProjectActionState(database, "generation-device", "renamed/project"))
        .map((row) => ({ ...row })),
      [{
        deviceId: "generation-device",
        projectSlug: "renamed/project",
        highestStage: "tried",
        openedAt: null,
        savedAt: null,
        triedAt: "2026-07-15T10:00:00Z",
        clonedAt: null,
        reusedAt: null,
        updatedAt: "2026-07-15T10:00:00Z",
      }],
      "old code must read the latest action State at the current Catalog slug before any new action",
    );
    assert.equal(
      (await readProjectActionState(database, "generation-device", "legacy/project")).length,
      0,
    );
    assert.deepEqual(
      rows(database, `SELECT project_slug AS projectSlug, value, created_at AS createdAt,
        updated_at AS updatedAt FROM feedback WHERE device_id = ?`, "generation-device"),
      [{
        projectSlug: "renamed/project",
        value: "有用",
        createdAt: "2026-07-15T12:30:00Z",
        updatedAt: "2026-07-15T12:30:00Z",
      }],
      "old code must read the current feedback State without creating a decision Event",
    );
    assert.deepEqual(tableCounts(database, [
      "project_action_events",
      "project_action_events_v2",
      "decision_events",
      "decision_events_v2",
    ]), historyBeforeRename);
    const stateAfterRename = rows(database, `SELECT * FROM project_action_state
      WHERE device_id = ? ORDER BY project_slug`, "generation-device");
    const feedbackAfterRename = rows(database, `SELECT * FROM feedback
      WHERE device_id = ? ORDER BY project_slug`, "generation-device");
    await adoptStableProjectIdentities(database, secondCatalog);
    assert.deepEqual(rows(database, `SELECT * FROM project_action_state
      WHERE device_id = ? ORDER BY project_slug`, "generation-device"), stateAfterRename);
    assert.deepEqual(rows(database, `SELECT * FROM feedback
      WHERE device_id = ? ORDER BY project_slug`, "generation-device"), feedbackAfterRename);
    assert.deepEqual(tableCounts(database, [
      "project_action_events",
      "project_action_events_v2",
      "decision_events",
      "decision_events_v2",
    ]), historyBeforeRename, "repeated adoption must not fabricate Event/history rows");
    await assert.rejects(
      adoptStableProjectIdentities(database, {
        ...firstCatalog,
        publishedAt: secondCatalog.publishedAt,
      }),
      (error) => error?.code === "conflicting_project_identity_publication",
      "different generations cannot share one activation order",
    );
    await assert.rejects(
      adoptStableProjectIdentities(database, firstCatalog),
      (error) => error?.code === "stale_project_identity_generation",
      "a slow request must not reactivate an older published Catalog",
    );
    await assert.rejects(
      record(firstIdentity, "reused", "stale-generation-reused-0001", "2026-07-16T09:00:00Z"),
      (error) => error?.code === "stale_project_identity_generation",
    );
    await record(renamedIdentity, "cloned", "renamed-cloned-0001", "2026-07-16T10:00:00Z");

    assert.deepEqual(
      rows(database, `SELECT generation_id AS generationId FROM project_identity_runtime`),
      [{ generationId: secondCatalog.generationId }],
    );
    assert.equal(rows(database, "SELECT generation_id FROM project_identity_catalog").length, 4);
    assert.deepEqual(
      rows(database, `SELECT project_slug AS projectSlug, catalog_generation_id AS generationId,
        action FROM project_action_events_v2
        WHERE project_id = ? ORDER BY id`, firstIdentity.projectId),
      [
        { projectSlug: "legacy/project", generationId: firstCatalog.generationId, action: "tried" },
        { projectSlug: "legacy/project", generationId: firstCatalog.generationId, action: "tried" },
        { projectSlug: "renamed/project", generationId: secondCatalog.generationId, action: "cloned" },
      ],
    );
    const [state] = await readStableProjectActionState(
      database,
      "generation-device",
      firstIdentity.projectId,
    );
    assert.equal(state.projectSlug, "renamed/project");
    assert.equal(state.catalogGenerationId, secondCatalog.generationId);
    assert.equal(state.highestStage, "cloned");

    assert.deepEqual(
      await readStableWeeklyActionMetrics(database, "generation-device", "2026-07-16T12:00:00Z"),
      {
        actedProjects: 1,
        openedProjects: 1,
        savedProjects: 1,
        triedProjects: 1,
        clonedProjects: 1,
        reusedProjects: 0,
      },
      "weekly action metrics must count stable projectId, not old and renamed slugs",
    );

    const rollbackCatalog = {
      ...firstCatalog,
      publishedAt: "2026-07-16T00:00:00.000003Z",
    };
    await adoptStableProjectIdentities(database, rollbackCatalog);
    await record(firstIdentity, "reused", "rollback-generation-reused-0001", "2026-07-16T11:00:00Z");
    assert.deepEqual(
      rows(database, `SELECT generation_id AS generationId, published_at AS publishedAt
        FROM project_identity_runtime`),
      [{ generationId: firstCatalog.generationId, publishedAt: rollbackCatalog.publishedAt }],
      "an explicit rollback may reactivate a retained generation only with a newer pointer time",
    );
  } finally {
    database.close();
  }
});
