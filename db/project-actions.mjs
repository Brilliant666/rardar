export const ACTION_VALUES = Object.freeze(["opened", "saved", "tried", "cloned", "reused"]);
export const LEGACY_IDEMPOTENCY_PREFIX = "legacy-project-actions:";

const actionRankSql = (value) => `CASE ${value}
  WHEN 'opened' THEN 1
  WHEN 'saved' THEN 2
  WHEN 'tried' THEN 3
  WHEN 'cloned' THEN 4
  WHEN 'reused' THEN 5
  ELSE 0
END`;

const latestTimestampSql = (column) => `CASE
  WHEN excluded.${column} IS NULL THEN project_action_state.${column}
  WHEN project_action_state.${column} IS NULL
    OR julianday(excluded.${column}) >= julianday(project_action_state.${column})
    THEN excluded.${column}
  ELSE project_action_state.${column}
END`;

const stateProjectionTrigger = `
  CREATE TRIGGER IF NOT EXISTS project_action_events_sync_state
  AFTER INSERT ON project_action_events
  BEGIN
    INSERT INTO project_action_state (
      device_id,
      project_slug,
      highest_stage,
      opened_at,
      saved_at,
      tried_at,
      cloned_at,
      reused_at,
      updated_at
    ) VALUES (
      NEW.device_id,
      NEW.project_slug,
      NEW.action,
      CASE WHEN NEW.action = 'opened' THEN NEW.occurred_at END,
      CASE WHEN NEW.action = 'saved' THEN NEW.occurred_at END,
      CASE WHEN NEW.action = 'tried' THEN NEW.occurred_at END,
      CASE WHEN NEW.action = 'cloned' THEN NEW.occurred_at END,
      CASE WHEN NEW.action = 'reused' THEN NEW.occurred_at END,
      NEW.occurred_at
    )
    ON CONFLICT (device_id, project_slug) DO UPDATE SET
      highest_stage = CASE
        WHEN ${actionRankSql("excluded.highest_stage")} > ${actionRankSql("project_action_state.highest_stage")}
          THEN excluded.highest_stage
        ELSE project_action_state.highest_stage
      END,
      opened_at = ${latestTimestampSql("opened_at")},
      saved_at = ${latestTimestampSql("saved_at")},
      tried_at = ${latestTimestampSql("tried_at")},
      cloned_at = ${latestTimestampSql("cloned_at")},
      reused_at = ${latestTimestampSql("reused_at")},
      updated_at = ${latestTimestampSql("updated_at")};
  END
`;

export const PROJECT_ACTION_SCHEMA_SQL = Object.freeze([
  `
    CREATE TABLE IF NOT EXISTS project_actions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      device_id TEXT NOT NULL,
      project_slug TEXT NOT NULL,
      action TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
  `,
  `
    CREATE UNIQUE INDEX IF NOT EXISTS project_actions_device_project_action_idx
    ON project_actions (device_id, project_slug, action)
  `,
  `
    CREATE INDEX IF NOT EXISTS project_actions_device_created_idx
    ON project_actions (device_id, created_at)
  `,
  `
    CREATE TABLE IF NOT EXISTS project_action_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      device_id TEXT NOT NULL,
      project_slug TEXT NOT NULL,
      action TEXT NOT NULL CHECK (action IN ('opened', 'saved', 'tried', 'cloned', 'reused')),
      occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (julianday(occurred_at) IS NOT NULL),
      idempotency_key TEXT NOT NULL
    )
  `,
  `
    CREATE UNIQUE INDEX IF NOT EXISTS project_action_events_device_idempotency_idx
    ON project_action_events (device_id, idempotency_key)
  `,
  `
    CREATE INDEX IF NOT EXISTS project_action_events_device_occurred_idx
    ON project_action_events (device_id, occurred_at)
  `,
  `
    CREATE INDEX IF NOT EXISTS project_action_events_device_project_occurred_idx
    ON project_action_events (device_id, project_slug, occurred_at)
  `,
  `
    CREATE TABLE IF NOT EXISTS project_action_state (
      device_id TEXT NOT NULL,
      project_slug TEXT NOT NULL,
      highest_stage TEXT NOT NULL CHECK (highest_stage IN ('opened', 'saved', 'tried', 'cloned', 'reused')),
      opened_at TEXT,
      saved_at TEXT,
      tried_at TEXT,
      cloned_at TEXT,
      reused_at TEXT,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (device_id, project_slug)
    )
  `,
  `
    CREATE INDEX IF NOT EXISTS project_action_state_device_updated_idx
    ON project_action_state (device_id, updated_at)
  `,
  stateProjectionTrigger,
  `
    CREATE TRIGGER IF NOT EXISTS project_action_events_legacy_projection
    AFTER INSERT ON project_action_events
    BEGIN
      INSERT INTO project_actions (device_id, project_slug, action, created_at)
      VALUES (
        NEW.device_id,
        NEW.project_slug,
        NEW.action,
        strftime('%Y-%m-%d %H:%M:%f', NEW.occurred_at)
      )
      ON CONFLICT (device_id, project_slug, action) DO UPDATE SET
        created_at = CASE
          WHEN julianday(project_actions.created_at) IS NULL
            OR julianday(excluded.created_at) >= julianday(project_actions.created_at)
            THEN excluded.created_at
          ELSE project_actions.created_at
        END;
    END
  `,
  `
    CREATE TRIGGER IF NOT EXISTS project_actions_capture_legacy_event
    AFTER INSERT ON project_actions
    WHEN NOT EXISTS (
      SELECT 1
      FROM project_action_events
      WHERE device_id = NEW.device_id
        AND project_slug = NEW.project_slug
        AND action = NEW.action
        AND ABS(julianday(occurred_at) - julianday(NEW.created_at)) < (2.0 / 86400000.0)
    )
    BEGIN
      INSERT INTO project_action_events (
        device_id,
        project_slug,
        action,
        occurred_at,
        idempotency_key
      ) VALUES (
        NEW.device_id,
        NEW.project_slug,
        NEW.action,
        NEW.created_at,
        '${LEGACY_IDEMPOTENCY_PREFIX}' || NEW.id
      )
      ON CONFLICT (device_id, idempotency_key) DO NOTHING;
    END
  `,
  `
    CREATE TRIGGER IF NOT EXISTS project_action_events_reject_identity_replacement
    BEFORE INSERT ON project_action_events
    WHEN EXISTS (
      SELECT 1
      FROM project_action_events
      WHERE id = NEW.id
        OR (device_id = NEW.device_id AND idempotency_key = NEW.idempotency_key)
    )
    BEGIN
      SELECT RAISE(ABORT, 'project_action_events identity is immutable');
    END
  `,
  `
    CREATE TRIGGER IF NOT EXISTS project_action_events_reject_update
    BEFORE UPDATE ON project_action_events
    BEGIN
      SELECT RAISE(ABORT, 'project_action_events is append-only');
    END
  `,
  `
    CREATE TRIGGER IF NOT EXISTS project_action_events_reject_delete
    BEFORE DELETE ON project_action_events
    BEGIN
      SELECT RAISE(ABORT, 'project_action_events is append-only');
    END
  `,
  `
    INSERT INTO project_action_events (
      device_id,
      project_slug,
      action,
      occurred_at,
      idempotency_key
    )
    SELECT
      legacy.device_id,
      legacy.project_slug,
      legacy.action,
      legacy.created_at,
      '${LEGACY_IDEMPOTENCY_PREFIX}' || legacy.id
    FROM project_actions AS legacy
    WHERE NOT EXISTS (
      SELECT 1
      FROM project_action_events AS existing
      WHERE existing.device_id = legacy.device_id
        AND existing.project_slug = legacy.project_slug
        AND existing.action = legacy.action
        AND ABS(julianday(existing.occurred_at) - julianday(legacy.created_at)) < (2.0 / 86400000.0)
    )
    ON CONFLICT (device_id, idempotency_key) DO NOTHING
  `,
]);

export function prepareProjectActionSchema(database) {
  return PROJECT_ACTION_SCHEMA_SQL.map((statement) => database.prepare(statement));
}

function eventFromRow(row) {
  return {
    id: Number(row.id),
    deviceId: String(row.deviceId),
    projectSlug: String(row.projectSlug),
    action: String(row.action),
    occurredAt: String(row.occurredAt),
    idempotencyKey: String(row.idempotencyKey),
  };
}

function requireActionInput(input) {
  if (!input || typeof input !== "object") throw new TypeError("project action input is required");
  if (!ACTION_VALUES.includes(input.action)) throw new TypeError("invalid project action");
  for (const field of ["deviceId", "projectSlug", "idempotencyKey"]) {
    if (typeof input[field] !== "string" || !input[field]) {
      throw new TypeError(`${field} is required`);
    }
  }
  if (input.idempotencyKey.startsWith(LEGACY_IDEMPOTENCY_PREFIX)) {
    throw new TypeError("reserved idempotency key");
  }
}

async function findEventByIdempotencyKey(database, deviceId, idempotencyKey) {
  return database
    .prepare(`
      SELECT
        id,
        device_id AS deviceId,
        project_slug AS projectSlug,
        action,
        occurred_at AS occurredAt,
        idempotency_key AS idempotencyKey
      FROM project_action_events
      WHERE device_id = ? AND idempotency_key = ?
      LIMIT 1
    `)
    .bind(deviceId, idempotencyKey)
    .first();
}

function resultForExistingEvent(existing, input) {
  const event = eventFromRow(existing);
  if (event.projectSlug !== input.projectSlug || event.action !== input.action) {
    return { status: "conflict", recorded: false, event };
  }
  return { status: "replayed", recorded: false, event };
}

export async function appendProjectActionEvent(database, input, occurredAt = new Date().toISOString()) {
  requireActionInput(input);
  if (
    typeof occurredAt !== "string"
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(occurredAt)
    || Number.isNaN(Date.parse(occurredAt))
  ) {
    throw new TypeError("occurredAt must be a timezone-aware RFC3339 timestamp");
  }

  const existingBeforeInsert = await findEventByIdempotencyKey(
    database,
    input.deviceId,
    input.idempotencyKey,
  );
  if (existingBeforeInsert) return resultForExistingEvent(existingBeforeInsert, input);

  let inserted;
  try {
    inserted = await database
      .prepare(`
        INSERT INTO project_action_events (
          device_id,
          project_slug,
          action,
          occurred_at,
          idempotency_key
        ) VALUES (?, ?, ?, ?, ?)
        RETURNING
          id,
          device_id AS deviceId,
          project_slug AS projectSlug,
          action,
          occurred_at AS occurredAt,
          idempotency_key AS idempotencyKey
      `)
      .bind(input.deviceId, input.projectSlug, input.action, occurredAt, input.idempotencyKey)
      .first();
  } catch (error) {
    const concurrentWinner = await findEventByIdempotencyKey(
      database,
      input.deviceId,
      input.idempotencyKey,
    );
    if (concurrentWinner) return resultForExistingEvent(concurrentWinner, input);
    throw error;
  }

  if (inserted) {
    return { status: "recorded", recorded: true, event: eventFromRow(inserted) };
  }
  throw new Error("project action insert returned no Event");
}

export async function readProjectActionState(database, deviceId, projectSlug = null) {
  const where = projectSlug ? "WHERE device_id = ? AND project_slug = ?" : "WHERE device_id = ?";
  const statement = database.prepare(`
    SELECT
      device_id AS deviceId,
      project_slug AS projectSlug,
      highest_stage AS highestStage,
      opened_at AS openedAt,
      saved_at AS savedAt,
      tried_at AS triedAt,
      cloned_at AS clonedAt,
      reused_at AS reusedAt,
      updated_at AS updatedAt
    FROM project_action_state
    ${where}
    ORDER BY julianday(updated_at) DESC, project_slug ASC
  `);
  const result = projectSlug
    ? await statement.bind(deviceId, projectSlug).all()
    : await statement.bind(deviceId).all();
  return result.results ?? [];
}

const ACTION_TIMESTAMP_FIELDS = Object.freeze([
  ["opened", "openedAt"],
  ["saved", "savedAt"],
  ["tried", "triedAt"],
  ["cloned", "clonedAt"],
  ["reused", "reusedAt"],
]);

export function stateToActionProjection(states) {
  return states.flatMap((state) => ACTION_TIMESTAMP_FIELDS.flatMap(([action, field]) => {
    const timestamp = state[field];
    if (!timestamp) return [];
    return [{
      deviceId: state.deviceId,
      projectSlug: state.projectSlug,
      action,
      createdAt: timestamp,
      occurredAt: timestamp,
    }];
  }));
}

export async function readWeeklyActionMetrics(database, deviceId, now = new Date().toISOString()) {
  if (typeof now !== "string" || Number.isNaN(Date.parse(now))) {
    throw new TypeError("now must be a valid timestamp");
  }
  const row = await database.prepare(`
    SELECT
      COUNT(DISTINCT CASE WHEN action IN ('tried', 'cloned', 'reused') THEN project_slug END) AS actedProjects,
      COUNT(DISTINCT CASE WHEN action = 'opened' THEN project_slug END) AS openedProjects,
      COUNT(DISTINCT CASE WHEN action = 'saved' THEN project_slug END) AS savedProjects,
      COUNT(DISTINCT CASE WHEN action = 'tried' THEN project_slug END) AS triedProjects,
      COUNT(DISTINCT CASE WHEN action = 'cloned' THEN project_slug END) AS clonedProjects,
      COUNT(DISTINCT CASE WHEN action = 'reused' THEN project_slug END) AS reusedProjects
    FROM project_action_events
    WHERE device_id = ?
      AND julianday(occurred_at) >= julianday(?) - 7.0
      AND julianday(occurred_at) <= julianday(?)
  `).bind(deviceId, now, now).first();
  return {
    actedProjects: Number(row?.actedProjects ?? 0),
    openedProjects: Number(row?.openedProjects ?? 0),
    savedProjects: Number(row?.savedProjects ?? 0),
    triedProjects: Number(row?.triedProjects ?? 0),
    clonedProjects: Number(row?.clonedProjects ?? 0),
    reusedProjects: Number(row?.reusedProjects ?? 0),
  };
}
