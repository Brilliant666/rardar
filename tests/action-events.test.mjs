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
  }

  prepare(sql) {
    return new SqliteD1Statement(this.raw, sql);
  }

  async batch(statements) {
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

async function applyMigration(database, relativePath) {
  const sql = await readFile(new URL(relativePath, import.meta.url), "utf8");
  for (const statement of sql.split("--> statement-breakpoint").map((part) => part.trim()).filter(Boolean)) {
    database.raw.exec(statement);
  }
}

function rows(database, sql, ...bindings) {
  return database.raw.prepare(sql).all(...bindings).map((row) => ({ ...row }));
}

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
