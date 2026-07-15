import { ACTION_VALUES, LEGACY_IDEMPOTENCY_PREFIX } from "./project-actions.mjs";

export const PROJECT_ID_VERSION = 1;
export const FEEDBACK_VALUES = Object.freeze(["有用", "无用", "复用", "待确定"]);

const PROJECT_ID_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*--[0-9a-f]{20}$/;
const PROJECT_ID_MAX_LENGTH = 86;
const TIMESTAMP_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

const actionRankSql = (value) => `CASE ${value}
  WHEN 'opened' THEN 1 WHEN 'saved' THEN 2 WHEN 'tried' THEN 3
  WHEN 'cloned' THEN 4 WHEN 'reused' THEN 5 ELSE 0 END`;

export class StableProjectDecisionError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "StableProjectDecisionError";
    this.code = code;
    this.details = details;
  }
}

function requireTimestamp(value, label) {
  if (typeof value !== "string" || !TIMESTAMP_PATTERN.test(value) || Number.isNaN(Date.parse(value))) {
    throw new TypeError(`${label} must be a timezone-aware RFC3339 timestamp`);
  }
  return value;
}

function publicationMicroseconds(value) {
  requireTimestamp(value, "identity catalog publishedAt");
  const fraction = value.match(/\.([0-9]+)(?:Z|[+-][0-9]{2}:[0-9]{2})$/)?.[1] ?? "";
  const subMillisecond = Number(fraction.padEnd(6, "0").slice(3, 6));
  const result = (Date.parse(value) * 1000) + subMillisecond;
  if (!Number.isSafeInteger(result)) {
    throw new TypeError("identity catalog publishedAt is outside the supported range");
  }
  return result;
}

function requireIdentity(input) {
  if (!input || typeof input !== "object") throw new TypeError("stable project identity is required");
  for (const field of ["projectId", "projectSlug", "catalogGenerationId"]) {
    if (typeof input[field] !== "string" || !input[field]) throw new TypeError(`${field} is required`);
  }
  if (
    input.projectIdVersion !== PROJECT_ID_VERSION
    || input.projectId.length > PROJECT_ID_MAX_LENGTH
    || !PROJECT_ID_PATTERN.test(input.projectId)
  ) {
    throw new TypeError("invalid stable project identity");
  }
}

function requireActionInput(input) {
  requireIdentity(input);
  for (const field of ["deviceId", "idempotencyKey"]) {
    if (typeof input[field] !== "string" || !input[field]) throw new TypeError(`${field} is required`);
  }
  if (!ACTION_VALUES.includes(input.action)) throw new TypeError("invalid project action");
  if (input.idempotencyKey.startsWith(LEGACY_IDEMPOTENCY_PREFIX)) throw new TypeError("reserved idempotency key");
}

function actionEventFromRow(row) {
  return {
    id: Number(row.id), deviceId: String(row.deviceId),
    projectIdVersion: Number(row.projectIdVersion), projectId: String(row.projectId),
    projectSlug: String(row.projectSlug), catalogGenerationId: String(row.catalogGenerationId),
    action: String(row.action), occurredAt: String(row.occurredAt),
    idempotencyKey: String(row.idempotencyKey),
  };
}

async function findStableActionEvent(database, deviceId, idempotencyKey) {
  return database.prepare(`SELECT id, device_id AS deviceId,
    project_id_version AS projectIdVersion, project_id AS projectId,
    project_slug AS projectSlug, catalog_generation_id AS catalogGenerationId,
    action, occurred_at AS occurredAt, idempotency_key AS idempotencyKey
    FROM project_action_events_v2 WHERE device_id = ? AND idempotency_key = ? LIMIT 1`)
    .bind(deviceId, idempotencyKey).first();
}

function resultForStableExisting(row, input) {
  const event = actionEventFromRow(row);
  if (event.projectId !== input.projectId || event.action !== input.action) {
    return { status: "conflict", recorded: false, event };
  }
  return { status: "replayed", recorded: false, event };
}

function rethrowStableWriteError(error) {
  const message = String(error?.message ?? error);
  if (/stale stable project generation/i.test(message)) {
    throw new StableProjectDecisionError(
      "stale_project_identity_generation",
      "the request Catalog generation is no longer active",
    );
  }
  if (/unknown stable project identity/i.test(message)) {
    throw new StableProjectDecisionError(
      "unknown_project_identity_mapping",
      "the verified project identity mapping is unavailable",
    );
  }
  throw error;
}

export async function appendStableProjectActionEvent(database, input, occurredAt = new Date().toISOString()) {
  requireActionInput(input);
  requireTimestamp(occurredAt, "occurredAt");
  const existing = await findStableActionEvent(database, input.deviceId, input.idempotencyKey);
  if (existing) return resultForStableExisting(existing, input);

  const legacy = await database.prepare(`SELECT id, device_id AS deviceId,
    project_slug AS projectSlug, action, occurred_at AS occurredAt,
    idempotency_key AS idempotencyKey FROM project_action_events
    WHERE device_id = ? AND idempotency_key = ? LIMIT 1`)
    .bind(input.deviceId, input.idempotencyKey).first();
  if (legacy && (legacy.projectSlug !== input.projectSlug || legacy.action !== input.action)) {
    return { status: "conflict", recorded: false, event: legacy };
  }

  const effectiveTime = legacy ? String(legacy.occurredAt) : occurredAt;
  try {
    const inserted = await database.prepare(`INSERT INTO project_action_events_v2 (
      device_id, project_id_version, project_id, project_slug,
      catalog_generation_id, action, occurred_at, idempotency_key
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id, device_id AS deviceId,
      project_id_version AS projectIdVersion, project_id AS projectId,
      project_slug AS projectSlug, catalog_generation_id AS catalogGenerationId,
      action, occurred_at AS occurredAt, idempotency_key AS idempotencyKey`)
      .bind(input.deviceId, input.projectIdVersion, input.projectId, input.projectSlug,
        input.catalogGenerationId, input.action, effectiveTime, input.idempotencyKey).first();
    if (!inserted) throw new Error("stable project action insert returned no Event");
    return {
      status: legacy ? "replayed" : "recorded",
      recorded: !legacy,
      event: actionEventFromRow(inserted),
    };
  } catch (error) {
    const winner = await findStableActionEvent(database, input.deviceId, input.idempotencyKey);
    if (winner) return resultForStableExisting(winner, input);
    rethrowStableWriteError(error);
  }
}

export async function readStableProjectActionState(database, deviceId, projectId = null) {
  const where = projectId ? "WHERE device_id = ? AND project_id = ?" : "WHERE device_id = ?";
  const statement = database.prepare(`SELECT device_id AS deviceId,
    project_id_version AS projectIdVersion, project_id AS projectId,
    project_slug AS projectSlug, catalog_generation_id AS catalogGenerationId,
    highest_stage AS highestStage, opened_at AS openedAt, saved_at AS savedAt,
    tried_at AS triedAt, cloned_at AS clonedAt, reused_at AS reusedAt,
    updated_at AS updatedAt FROM project_action_state_v2 ${where}
    ORDER BY julianday(updated_at) DESC, project_id ASC`);
  const result = projectId
    ? await statement.bind(deviceId, projectId).all()
    : await statement.bind(deviceId).all();
  return result.results ?? [];
}

const ACTION_TIMESTAMP_FIELDS = Object.freeze([
  ["opened", "openedAt"], ["saved", "savedAt"], ["tried", "triedAt"],
  ["cloned", "clonedAt"], ["reused", "reusedAt"],
]);

export function stableStateToActionProjection(states) {
  return states.flatMap((state) => ACTION_TIMESTAMP_FIELDS.flatMap(([action, field]) => {
    if (!state[field]) return [];
    return [{
      deviceId: state.deviceId, projectIdVersion: state.projectIdVersion,
      projectId: state.projectId, projectSlug: state.projectSlug,
      catalogGenerationId: state.catalogGenerationId, action,
      createdAt: state[field], occurredAt: state[field],
    }];
  }));
}

export async function readStableWeeklyActionMetrics(database, deviceId, now = new Date().toISOString()) {
  requireTimestamp(now, "now");
  const row = await database.prepare(`SELECT
    COUNT(DISTINCT CASE WHEN action IN ('tried','cloned','reused') THEN project_id END) AS actedProjects,
    COUNT(DISTINCT CASE WHEN action = 'opened' THEN project_id END) AS openedProjects,
    COUNT(DISTINCT CASE WHEN action = 'saved' THEN project_id END) AS savedProjects,
    COUNT(DISTINCT CASE WHEN action = 'tried' THEN project_id END) AS triedProjects,
    COUNT(DISTINCT CASE WHEN action = 'cloned' THEN project_id END) AS clonedProjects,
    COUNT(DISTINCT CASE WHEN action = 'reused' THEN project_id END) AS reusedProjects
    FROM project_action_events_v2 WHERE device_id = ?
      AND julianday(occurred_at) >= julianday(?) - 7.0
      AND julianday(occurred_at) <= julianday(?)`)
    .bind(deviceId, now, now).first();
  return Object.fromEntries(["actedProjects", "openedProjects", "savedProjects", "triedProjects", "clonedProjects", "reusedProjects"]
    .map((key) => [key, Number(row?.[key] ?? 0)]));
}

function feedbackFromRow(row) {
  return {
    deviceId: String(row.deviceId), projectIdVersion: Number(row.projectIdVersion),
    projectId: String(row.projectId), projectSlug: String(row.projectSlug),
    catalogGenerationId: String(row.catalogGenerationId), value: String(row.value),
    createdAt: String(row.createdAt), updatedAt: String(row.updatedAt),
  };
}

export async function upsertStableFeedback(database, input, now = new Date().toISOString()) {
  requireIdentity(input);
  if (typeof input.deviceId !== "string" || !input.deviceId) throw new TypeError("deviceId is required");
  if (!FEEDBACK_VALUES.includes(input.value)) throw new TypeError("invalid feedback");
  requireTimestamp(now, "now");
  let changed;
  try {
    changed = await database.prepare(`INSERT INTO feedback_v2 (
      device_id, project_id_version, project_id, project_slug,
      catalog_generation_id, value, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (device_id, project_id) DO UPDATE SET
      project_id_version = excluded.project_id_version,
      project_slug = excluded.project_slug,
      catalog_generation_id = excluded.catalog_generation_id,
      value = excluded.value, updated_at = excluded.updated_at
    WHERE feedback_v2.value <> excluded.value
    RETURNING device_id AS deviceId, project_id_version AS projectIdVersion,
      project_id AS projectId, project_slug AS projectSlug,
      catalog_generation_id AS catalogGenerationId, value,
      created_at AS createdAt, updated_at AS updatedAt`)
      .bind(input.deviceId, input.projectIdVersion, input.projectId, input.projectSlug,
        input.catalogGenerationId, input.value, now, now).first();
  } catch (error) {
    rethrowStableWriteError(error);
  }
  const row = changed ?? await database.prepare(`SELECT device_id AS deviceId,
    project_id_version AS projectIdVersion, project_id AS projectId,
    project_slug AS projectSlug, catalog_generation_id AS catalogGenerationId,
    value, created_at AS createdAt, updated_at AS updatedAt FROM feedback_v2
    WHERE device_id = ? AND project_id = ? LIMIT 1`)
    .bind(input.deviceId, input.projectId).first();
  return { changed: Boolean(changed), feedback: feedbackFromRow(row) };
}

export async function readStableFeedback(database, deviceId, projectId = null) {
  const where = projectId ? "WHERE device_id = ? AND project_id = ?" : "WHERE device_id = ?";
  const statement = database.prepare(`SELECT device_id AS deviceId,
    project_id_version AS projectIdVersion, project_id AS projectId,
    project_slug AS projectSlug, catalog_generation_id AS catalogGenerationId,
    value, created_at AS createdAt, updated_at AS updatedAt FROM feedback_v2
    ${where} ORDER BY julianday(updated_at) DESC, project_id ASC`);
  const result = projectId
    ? await statement.bind(deviceId, projectId).all()
    : await statement.bind(deviceId).all();
  return result.results ?? [];
}

export async function readStableWeeklyFeedbackMetrics(database, deviceId, now = new Date().toISOString()) {
  requireTimestamp(now, "now");
  const row = await database.prepare(`SELECT
    COUNT(DISTINCT CASE WHEN value IN ('有用','复用') THEN project_id END) AS effectiveDecisions,
    COUNT(DISTINCT CASE WHEN value = '复用' THEN project_id END) AS reuseDecisions,
    COUNT(*) AS feedbackChanges FROM decision_events_v2 WHERE device_id = ?
      AND julianday(occurred_at) >= julianday(?) - 7.0
      AND julianday(occurred_at) <= julianday(?)`)
    .bind(deviceId, now, now).first();
  return {
    effectiveDecisions: Number(row?.effectiveDecisions ?? 0),
    reuseDecisions: Number(row?.reuseDecisions ?? 0),
    feedbackChanges: Number(row?.feedbackChanges ?? 0),
  };
}

function normalizeCatalogContext(context) {
  if (!context || typeof context !== "object" || typeof context.generationId !== "string" || !context.generationId) {
    throw new TypeError("identity catalog generationId is required");
  }
  const publishedAt = requireTimestamp(context.publishedAt, "identity catalog publishedAt");
  const publishedAtMicros = publicationMicroseconds(publishedAt);
  const projects = context.projects ?? context.entries;
  if (!Array.isArray(projects)) throw new TypeError("identity catalog projects are required");
  const ids = new Set();
  const repositories = new Set();
  const slugs = new Set();
  const normalized = projects.map((project) => {
    const value = {
      projectIdVersion: project?.projectIdVersion,
      projectId: project?.projectId,
      projectSlug: project?.projectSlug ?? project?.slug,
      repository: project?.repository ?? project?.repo,
    };
    requireIdentity({ ...value, catalogGenerationId: context.generationId });
    if (typeof value.repository !== "string" || !value.repository) throw new TypeError("repository is required");
    const canonicalRepository = value.repository.toLowerCase();
    if (ids.has(value.projectId) || repositories.has(canonicalRepository) || slugs.has(value.projectSlug)) {
      throw new StableProjectDecisionError(
        "ambiguous_project_identity",
        "identity catalog contains a duplicate project identity or legacy slug",
      );
    }
    ids.add(value.projectId); repositories.add(canonicalRepository); slugs.add(value.projectSlug);
    return { ...value, canonicalRepository };
  });
  return {
    generationId: context.generationId,
    publishedAt,
    publishedAtMicros,
    projects: normalized,
  };
}

async function allRows(database, sql, ...bindings) {
  const result = await database.prepare(sql).bind(...bindings).all();
  return result.results ?? [];
}

function unresolvedError(rows) {
  const examples = rows.slice(0, 5).map((row) => String(row.projectSlug ?? row.project_slug ?? ""));
  return new StableProjectDecisionError(
    "unresolved_project_identity",
    `legacy project rows cannot be uniquely mapped (${rows.length})`,
    { count: rows.length, examples },
  );
}

function rowKey(...values) {
  return values.map((value) => String(value)).join("\u0000");
}

function timestampMilliseconds(value) {
  if (typeof value !== "string" || !value) return Number.NaN;
  const sqliteUtc = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$/;
  return Date.parse(sqliteUtc.test(value) ? `${value.replace(" ", "T")}Z` : value);
}

function timestampIsValid(value) {
  return !Number.isNaN(timestampMilliseconds(value));
}

function invalidLegacyError(table, row, reason) {
  return new StableProjectDecisionError(
    "invalid_legacy_project_row",
    `${table} contains a legacy row that cannot be mechanically preserved`,
    { table, projectSlug: row.projectSlug, reason },
  );
}

function conflictingProjectionError(table, row, reason) {
  return new StableProjectDecisionError(
    "conflicting_project_projection",
    `${table} conflicts with an existing stable project row`,
    { table, projectSlug: row.projectSlug, reason },
  );
}

function exactCatalogMapping(existing, expected) {
  return existing.projectIdVersion === expected.projectIdVersion
    && existing.projectId === expected.projectId
    && existing.projectSlug === expected.projectSlug
    && existing.canonicalRepository === expected.canonicalRepository;
}

function exactActionProjection(stable, legacy) {
  return stable.deviceId === legacy.deviceId
    && stable.projectSlug === legacy.projectSlug
    && stable.action === legacy.action
    && stable.occurredAt === legacy.occurredAt
    && stable.idempotencyKey === legacy.idempotencyKey;
}

function exactFeedbackFacts(stable, legacy) {
  return stable.deviceId === legacy.deviceId
    && stable.value === legacy.value
    && stable.createdAt === legacy.createdAt
    && stable.updatedAt === legacy.updatedAt;
}

function exactActionStateFacts(stable, legacy) {
  return stable.deviceId === legacy.deviceId
    && stable.highestStage === legacy.highestStage
    && stable.openedAt === legacy.openedAt
    && stable.savedAt === legacy.savedAt
    && stable.triedAt === legacy.triedAt
    && stable.clonedAt === legacy.clonedAt
    && stable.reusedAt === legacy.reusedAt
    && stable.updatedAt === legacy.updatedAt;
}

function exactDecisionProjection(stable, legacy) {
  return stable.legacyEventId === legacy.id
    && stable.deviceId === legacy.deviceId
    && stable.projectSlug === legacy.projectSlug
    && stable.value === legacy.value
    && stable.occurredAt === legacy.createdAt;
}

async function readAdoptionRows(database) {
  const [
    actionEvents, actions, states, feedback, decisions,
    stableActions, stableStates, stableFeedback, stableDecisions,
  ] =
    await Promise.all([
      allRows(database, `SELECT id, device_id AS deviceId, project_slug AS projectSlug,
        action, occurred_at AS occurredAt, idempotency_key AS idempotencyKey
        FROM project_action_events ORDER BY id`),
      allRows(database, `SELECT id, device_id AS deviceId, project_slug AS projectSlug,
        action, created_at AS createdAt FROM project_actions`),
      allRows(database, `SELECT device_id AS deviceId, project_slug AS projectSlug,
        highest_stage AS highestStage, opened_at AS openedAt, saved_at AS savedAt,
        tried_at AS triedAt, cloned_at AS clonedAt, reused_at AS reusedAt,
        updated_at AS updatedAt FROM project_action_state`),
      allRows(database, `SELECT device_id AS deviceId, project_slug AS projectSlug,
        value, created_at AS createdAt, updated_at AS updatedAt FROM feedback`),
      allRows(database, `SELECT id, device_id AS deviceId, project_slug AS projectSlug,
        value, created_at AS createdAt FROM decision_events`),
      allRows(database, `SELECT id, device_id AS deviceId,
        project_id_version AS projectIdVersion, project_id AS projectId,
        project_slug AS projectSlug, catalog_generation_id AS catalogGenerationId,
        action, occurred_at AS occurredAt, idempotency_key AS idempotencyKey
        FROM project_action_events_v2`),
      allRows(database, `SELECT device_id AS deviceId,
        project_id_version AS projectIdVersion, project_id AS projectId,
        project_slug AS projectSlug, catalog_generation_id AS catalogGenerationId,
        highest_stage AS highestStage, opened_at AS openedAt, saved_at AS savedAt,
        tried_at AS triedAt, cloned_at AS clonedAt, reused_at AS reusedAt,
        updated_at AS updatedAt FROM project_action_state_v2`),
      allRows(database, `SELECT device_id AS deviceId,
        project_id_version AS projectIdVersion, project_id AS projectId,
        project_slug AS projectSlug, catalog_generation_id AS catalogGenerationId,
        value, created_at AS createdAt, updated_at AS updatedAt FROM feedback_v2`),
      allRows(database, `SELECT id, legacy_event_id AS legacyEventId,
        device_id AS deviceId, project_id_version AS projectIdVersion,
        project_id AS projectId, project_slug AS projectSlug,
        catalog_generation_id AS catalogGenerationId, value,
        occurred_at AS occurredAt FROM decision_events_v2`),
    ]);
  return {
    actionEvents,
    actions,
    states,
    feedback,
    decisions,
    stableActions,
    stableStates,
    stableFeedback,
    stableDecisions,
  };
}

function validateExistingMappings(existingMappings, catalog) {
  if (!existingMappings.length) return;
  if (existingMappings.length !== catalog.projects.length) {
    throw new StableProjectDecisionError(
      "conflicting_project_identity_mapping",
      "existing generation identity mapping differs from the verified catalog",
    );
  }
  const byProjectId = new Map(existingMappings.map((mapping) => [mapping.projectId, mapping]));
  for (const expected of catalog.projects) {
    const existing = byProjectId.get(expected.projectId);
    if (!existing || !exactCatalogMapping(existing, expected)) {
      throw new StableProjectDecisionError(
        "conflicting_project_identity_mapping",
        "existing generation identity mapping differs from the verified catalog",
      );
    }
  }
}

function validateGlobalIdentityMappings(existingMappings, catalog) {
  const byId = new Map();
  const byRepository = new Map();
  const byLegacySlug = new Map();
  for (const existing of [...existingMappings, ...catalog.projects]) {
    const sameId = byId.get(existing.projectId);
    const sameRepository = byRepository.get(existing.canonicalRepository);
    const sameLegacySlug = byLegacySlug.get(existing.projectSlug);
    if ((sameId && sameId.canonicalRepository !== existing.canonicalRepository)
      || (sameRepository && sameRepository.projectId !== existing.projectId)
      || (sameLegacySlug && sameLegacySlug.projectId !== existing.projectId)) {
      throw new StableProjectDecisionError(
        "project_identity_collision",
        "a Stable Project ID, canonical repository, or legacy slug conflicts with a retained generation",
        {
          generationId: existing.generationId,
          projectId: existing.projectId,
          canonicalRepository: existing.canonicalRepository,
        },
      );
    }
    byId.set(existing.projectId, existing);
    byRepository.set(existing.canonicalRepository, existing);
    byLegacySlug.set(existing.projectSlug, existing);
  }
}

function preflightLegacyRows(catalog, rows, allMappings) {
  const identityBySlug = new Map(catalog.projects.map((project) => [project.projectSlug, project]));
  const identityByProjectId = new Map(catalog.projects.map((project) => [project.projectId, project]));
  const historicalProjectIdBySlug = new Map();
  for (const mapping of [...allMappings, ...catalog.projects]) {
    historicalProjectIdBySlug.set(mapping.projectSlug, mapping.projectId);
  }
  const stableActionByKey = new Map(rows.stableActions.map((row) => [
    rowKey(row.deviceId, row.idempotencyKey), row,
  ]));
  const legacyEventsByFact = new Map();
  const legacyEventsByProject = new Map();
  for (const row of rows.actionEvents) {
    if (!ACTION_VALUES.includes(row.action) || !timestampIsValid(row.occurredAt)) {
      throw invalidLegacyError("project_action_events", row, "invalid action or timestamp");
    }
    const factKey = rowKey(row.deviceId, row.projectSlug, row.action);
    const facts = legacyEventsByFact.get(factKey) ?? [];
    facts.push(row);
    legacyEventsByFact.set(factKey, facts);
    const projectKey = rowKey(row.deviceId, row.projectSlug);
    const projectEvents = legacyEventsByProject.get(projectKey) ?? [];
    projectEvents.push(row);
    legacyEventsByProject.set(projectKey, projectEvents);

    const stable = stableActionByKey.get(rowKey(row.deviceId, row.idempotencyKey));
    const identity = identityBySlug.get(row.projectSlug);
    const historicalProjectId = historicalProjectIdBySlug.get(row.projectSlug);
    if (stable) {
      if (!exactActionProjection(stable, row)) {
        throw conflictingProjectionError("project_action_events", row, "idempotency key has different facts");
      }
      if ((historicalProjectId && stable.projectId !== historicalProjectId)
        || (identity && stable.projectIdVersion !== identity.projectIdVersion)) {
        throw conflictingProjectionError("project_action_events", row, "stable identity differs from catalog");
      }
    } else if (!identity) {
      throw unresolvedError([row]);
    }
  }

  const legacyActionsByFact = new Map();
  for (const row of rows.actions) {
    if (!ACTION_VALUES.includes(row.action) || !timestampIsValid(row.createdAt)) {
      throw invalidLegacyError("project_actions", row, "invalid action or timestamp");
    }
    const factKey = rowKey(row.deviceId, row.projectSlug, row.action);
    if (legacyActionsByFact.has(factKey)) {
      throw invalidLegacyError("project_actions", row, "duplicate legacy action projection");
    }
    legacyActionsByFact.set(factKey, row);
    const facts = legacyEventsByFact.get(factKey) ?? [];
    const latestInstant = Math.max(...facts.map((event) => timestampMilliseconds(event.occurredAt)));
    if (!facts.length || Math.abs(timestampMilliseconds(row.createdAt) - latestInstant) >= 2) {
      throw invalidLegacyError("project_actions", row, "State row is not the latest Event projection");
    }
  }
  for (const [factKey, facts] of legacyEventsByFact) {
    if (!legacyActionsByFact.has(factKey)) {
      throw invalidLegacyError(
        "project_actions",
        facts[0],
        "latest Event has no rollback-compatible legacy projection",
      );
    }
  }

  const stageRanks = new Map(ACTION_VALUES.map((action, index) => [action, index + 1]));
  const stableEventsByProject = new Map();
  for (const event of rows.stableActions) {
    const key = rowKey(event.deviceId, event.projectId);
    stableEventsByProject.set(key, [...(stableEventsByProject.get(key) ?? []), event]);
  }
  const stableStateByProject = new Map();
  for (const state of rows.stableStates) {
    const key = rowKey(state.deviceId, state.projectId);
    if (stableStateByProject.has(key)) {
      throw conflictingProjectionError("project_action_state_v2", state, "duplicate stable State");
    }
    const events = stableEventsByProject.get(key) ?? [];
    if (!events.length) {
      throw conflictingProjectionError("project_action_state_v2", state, "State has no retained stable Events");
    }
    const expectedHighest = events.reduce((highest, event) => (
      (stageRanks.get(event.action) ?? 0) > (stageRanks.get(highest) ?? 0)
        ? event.action
        : highest
    ), "opened");
    if (state.highestStage !== expectedHighest) {
      throw conflictingProjectionError("project_action_state_v2", state, "highest stage is not the Event projection");
    }
    for (const [action, field] of [
      ["opened", "openedAt"], ["saved", "savedAt"], ["tried", "triedAt"],
      ["cloned", "clonedAt"], ["reused", "reusedAt"],
    ]) {
      const actionEvents = events.filter((event) => event.action === action);
      if (!actionEvents.length) {
        if (state[field] !== null && state[field] !== undefined) {
          throw conflictingProjectionError("project_action_state_v2", state, `${action} time has no Event`);
        }
        continue;
      }
      const latest = Math.max(...actionEvents.map((event) => timestampMilliseconds(event.occurredAt)));
      if (!timestampIsValid(state[field]) || timestampMilliseconds(state[field]) !== latest) {
        throw conflictingProjectionError("project_action_state_v2", state, `${action} time is not the latest Event`);
      }
    }
    const latest = Math.max(...events.map((event) => timestampMilliseconds(event.occurredAt)));
    if (timestampMilliseconds(state.updatedAt) !== latest) {
      throw conflictingProjectionError("project_action_state_v2", state, "updated time is not the latest Event");
    }
    stableStateByProject.set(key, state);
  }

  const legacyStatesByProject = new Map();
  const legacyStateByStableProject = new Map();
  for (const row of rows.states) {
    if (!ACTION_VALUES.includes(row.highestStage) || !timestampIsValid(row.updatedAt)) {
      throw invalidLegacyError("project_action_state", row, "invalid stage or updated timestamp");
    }
    const projectKey = rowKey(row.deviceId, row.projectSlug);
    if (legacyStatesByProject.has(projectKey)) {
      throw invalidLegacyError("project_action_state", row, "duplicate legacy State projection");
    }
    legacyStatesByProject.set(projectKey, row);
    const projectEvents = legacyEventsByProject.get(projectKey) ?? [];
    const projectId = historicalProjectIdBySlug.get(row.projectSlug);
    const stableState = projectId
      ? stableStateByProject.get(rowKey(row.deviceId, projectId))
      : null;
    if (stableState) {
      if (!exactActionStateFacts(stableState, row)) {
        throw conflictingProjectionError("project_action_state", row, "stable State has different facts");
      }
      const stableKey = rowKey(row.deviceId, projectId);
      if (legacyStateByStableProject.has(stableKey)) {
        throw conflictingProjectionError("project_action_state", row, "multiple legacy State rows map to one projectId");
      }
      legacyStateByStableProject.set(stableKey, row);
    } else {
      if (!identityBySlug.has(row.projectSlug)) {
        throw unresolvedError([row]);
      }
      if (!projectEvents.length) {
        throw invalidLegacyError("project_action_state", row, "State has no retained Events");
      }
      const expectedHighest = projectEvents.reduce((highest, event) => (
        (stageRanks.get(event.action) ?? 0) > (stageRanks.get(highest) ?? 0)
          ? event.action
          : highest
      ), "opened");
      if (row.highestStage !== expectedHighest) {
        throw invalidLegacyError("project_action_state", row, "highest_stage is not the Event projection");
      }
      for (const [action, timestamp] of [
        ["opened", row.openedAt], ["saved", row.savedAt], ["tried", row.triedAt],
        ["cloned", row.clonedAt], ["reused", row.reusedAt],
      ]) {
        const facts = legacyEventsByFact.get(rowKey(row.deviceId, row.projectSlug, action)) ?? [];
        if (!facts.length) {
          if (timestamp !== null && timestamp !== undefined) {
            throw invalidLegacyError("project_action_state", row, `${action} timestamp has no Event`);
          }
          continue;
        }
        const latestInstant = Math.max(...facts.map((event) => timestampMilliseconds(event.occurredAt)));
        if (!timestampIsValid(timestamp) || timestampMilliseconds(timestamp) !== latestInstant) {
          throw invalidLegacyError("project_action_state", row, `${action} timestamp is not the latest Event projection`);
        }
      }
      const latestProjectInstant = Math.max(
        ...projectEvents.map((event) => timestampMilliseconds(event.occurredAt)),
      );
      if (timestampMilliseconds(row.updatedAt) !== latestProjectInstant) {
        throw invalidLegacyError("project_action_state", row, "updated_at is not the latest Event projection");
      }
    }
  }
  for (const [projectKey, events] of legacyEventsByProject) {
    if (legacyStatesByProject.has(projectKey)) continue;
    const stableEvents = events.map((event) => stableActionByKey.get(
      rowKey(event.deviceId, event.idempotencyKey),
    ));
    const projectIds = new Set(stableEvents.filter(Boolean).map((event) => event.projectId));
    const deviceId = events[0].deviceId;
    const projectId = projectIds.size === 1 ? [...projectIds][0] : null;
    if (stableEvents.some((event) => !event)
      || !projectId
      || !stableStateByProject.has(rowKey(deviceId, projectId))
      || !legacyStateByStableProject.has(rowKey(deviceId, projectId))) {
      throw invalidLegacyError(
        "project_action_state",
        events[0],
        "retained Events have no rollback-compatible State projection",
      );
    }
  }
  for (const [stableKey, state] of stableStateByProject) {
    const legacy = legacyStateByStableProject.get(stableKey);
    if (!legacy || !exactActionStateFacts(state, legacy)) {
      throw conflictingProjectionError(
        "project_action_state_v2",
        state,
        "stable State has no rollback-compatible legacy projection",
      );
    }
    const current = identityByProjectId.get(state.projectId);
    if (current && state.projectSlug !== current.projectSlug) {
      if (legacy.projectSlug !== state.projectSlug
        || rows.states.some((candidate) => candidate !== legacy
          && candidate.deviceId === state.deviceId
          && candidate.projectSlug === current.projectSlug)) {
        throw conflictingProjectionError(
          "project_action_state",
          legacy,
          "current legacy slug target is occupied or source State is inconsistent",
        );
      }
    }
  }

  const stableFeedbackByTarget = new Map(rows.stableFeedback.map((row) => [
    rowKey(row.deviceId, row.projectId), row,
  ]));
  const legacyFeedbackByStableTarget = new Map();
  for (const row of rows.feedback) {
    if (!FEEDBACK_VALUES.includes(row.value)
      || !timestampIsValid(row.createdAt) || !timestampIsValid(row.updatedAt)) {
      throw invalidLegacyError("feedback", row, "invalid value or timestamp");
    }
    const identity = identityBySlug.get(row.projectSlug);
    const projectId = historicalProjectIdBySlug.get(row.projectSlug);
    const target = projectId
      ? stableFeedbackByTarget.get(rowKey(row.deviceId, projectId))
      : null;
    if (target) {
      if (!exactFeedbackFacts(target, row)) {
        throw conflictingProjectionError("feedback", row, "stable State has different facts");
      }
      if (identity && target.projectIdVersion !== identity.projectIdVersion) {
        throw conflictingProjectionError("feedback", row, "stable identity version differs from catalog");
      }
      const stableKey = rowKey(row.deviceId, projectId);
      if (legacyFeedbackByStableTarget.has(stableKey)) {
        throw conflictingProjectionError("feedback", row, "multiple legacy State rows map to one projectId");
      }
      legacyFeedbackByStableTarget.set(stableKey, row);
    } else if (!identity) {
      throw unresolvedError([row]);
    }
  }
  for (const [stableKey, stable] of stableFeedbackByTarget) {
    const legacy = legacyFeedbackByStableTarget.get(stableKey);
    if (!legacy || !exactFeedbackFacts(stable, legacy)) {
      throw conflictingProjectionError(
        "feedback_v2",
        stable,
        "stable State has no rollback-compatible legacy projection",
      );
    }
    const current = identityByProjectId.get(stable.projectId);
    if (current && stable.projectSlug !== current.projectSlug) {
      if (legacy.projectSlug !== stable.projectSlug
        || rows.feedback.some((candidate) => candidate !== legacy
          && candidate.deviceId === stable.deviceId
          && candidate.projectSlug === current.projectSlug)) {
        throw conflictingProjectionError(
          "feedback",
          legacy,
          "current legacy slug target is occupied or source State is inconsistent",
        );
      }
    }
  }

  const stableDecisionByLegacyId = new Map(rows.stableDecisions.map((row) => [row.legacyEventId, row]));
  for (const row of rows.decisions) {
    if (!FEEDBACK_VALUES.includes(row.value) || !timestampIsValid(row.createdAt)) {
      throw invalidLegacyError("decision_events", row, "invalid value or timestamp");
    }
    const identity = identityBySlug.get(row.projectSlug);
    const historicalProjectId = historicalProjectIdBySlug.get(row.projectSlug);
    const stable = stableDecisionByLegacyId.get(row.id);
    if (stable) {
      if (!exactDecisionProjection(stable, row)) {
        throw conflictingProjectionError("decision_events", row, "legacy Event ID has different facts");
      }
      if ((historicalProjectId && stable.projectId !== historicalProjectId)
        || (identity && stable.projectIdVersion !== identity.projectIdVersion)) {
        throw conflictingProjectionError("decision_events", row, "stable identity differs from catalog");
      }
    } else if (!identity) {
      throw unresolvedError([row]);
    }
  }
}

async function preflightCatalogAdoption(database, catalog) {
  const runtime = await database.prepare(`SELECT generation_id AS generationId,
    published_at AS publishedAt, published_at_micros AS publishedAtMicros
    FROM project_identity_runtime WHERE singleton = 1`).first();
  if (runtime) {
    const currentMicros = Number(runtime.publishedAtMicros);
    let expectedMicros = Number.NaN;
    try {
      expectedMicros = publicationMicroseconds(runtime.publishedAt);
    } catch {
      // Converted below to the stable fail-closed runtime error.
    }
    if (!Number.isSafeInteger(currentMicros) || currentMicros !== expectedMicros) {
      throw new StableProjectDecisionError(
        "invalid_project_identity_runtime",
        "active project identity publication metadata is invalid",
      );
    }
    if (currentMicros > catalog.publishedAtMicros) {
      throw new StableProjectDecisionError(
        "stale_project_identity_generation",
        "an older published Catalog cannot replace the active identity generation",
      );
    }
    if (currentMicros === catalog.publishedAtMicros
      && runtime.generationId !== catalog.generationId) {
      throw new StableProjectDecisionError(
        "conflicting_project_identity_publication",
        "two Catalog generations share the same publication order",
      );
    }
  }
  const allMappings = await allRows(database, `SELECT generation_id AS generationId,
    project_id AS projectId,
    project_id_version AS projectIdVersion, project_slug AS projectSlug,
    canonical_repository AS canonicalRepository FROM project_identity_catalog`);
  validateGlobalIdentityMappings(allMappings, catalog);
  const existingMappings = allMappings.filter(
    (mapping) => mapping.generationId === catalog.generationId,
  );
  validateExistingMappings(existingMappings, catalog);
  preflightLegacyRows(catalog, await readAdoptionRows(database), allMappings);
}

function migrationGuard(database, condition, ...bindings) {
  return database.prepare(`INSERT INTO project_identity_migration_guard (failure)
    SELECT 1 WHERE ${condition}`).bind(...bindings);
}

function stateFactsMatchSql(stable, legacy) {
  return `${stable}.device_id IS ${legacy}.device_id
    AND ${stable}.highest_stage IS ${legacy}.highest_stage
    AND ${stable}.opened_at IS ${legacy}.opened_at
    AND ${stable}.saved_at IS ${legacy}.saved_at
    AND ${stable}.tried_at IS ${legacy}.tried_at
    AND ${stable}.cloned_at IS ${legacy}.cloned_at
    AND ${stable}.reused_at IS ${legacy}.reused_at
    AND ${stable}.updated_at IS ${legacy}.updated_at`;
}

function feedbackFactsMatchSql(stable, legacy) {
  return `${stable}.device_id IS ${legacy}.device_id
    AND ${stable}.value IS ${legacy}.value
    AND ${stable}.created_at IS ${legacy}.created_at
    AND ${stable}.updated_at IS ${legacy}.updated_at`;
}

function addInBatchAdoptionGuards(statements, database, catalog) {
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_identity_runtime AS runtime
    WHERE runtime.singleton = 1 AND (
      runtime.published_at_micros > ?
      OR (runtime.published_at_micros = ? AND runtime.generation_id <> ?)
    )
  )`, catalog.publishedAtMicros, catalog.publishedAtMicros, catalog.generationId));
  statements.push(migrationGuard(database,
    `(SELECT COUNT(*) FROM project_identity_catalog WHERE generation_id = ?) <> ?`,
    catalog.generationId, catalog.projects.length));
  for (const project of catalog.projects) {
    statements.push(migrationGuard(database, `NOT EXISTS (
      SELECT 1 FROM project_identity_catalog WHERE generation_id = ?
        AND project_id_version = ? AND project_id = ?
        AND canonical_repository = ? AND project_slug = ?
    )`, catalog.generationId, project.projectIdVersion, project.projectId,
      project.canonicalRepository, project.projectSlug));
  }
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_identity_catalog AS left_mapping
    JOIN project_identity_catalog AS right_mapping ON
      (left_mapping.project_id = right_mapping.project_id
        AND left_mapping.canonical_repository <> right_mapping.canonical_repository)
      OR (left_mapping.canonical_repository = right_mapping.canonical_repository
        AND left_mapping.project_id <> right_mapping.project_id)
      OR (left_mapping.project_slug = right_mapping.project_slug
        AND left_mapping.project_id <> right_mapping.project_id)
  )`));

  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_events
    WHERE action NOT IN ('opened','saved','tried','cloned','reused')
      OR julianday(occurred_at) IS NULL
  )`));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_actions
    WHERE action NOT IN ('opened','saved','tried','cloned','reused')
      OR julianday(created_at) IS NULL
  )`));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_state
    WHERE highest_stage NOT IN ('opened','saved','tried','cloned','reused')
      OR julianday(updated_at) IS NULL
      OR (opened_at IS NOT NULL AND julianday(opened_at) IS NULL)
      OR (saved_at IS NOT NULL AND julianday(saved_at) IS NULL)
      OR (tried_at IS NOT NULL AND julianday(tried_at) IS NULL)
      OR (cloned_at IS NOT NULL AND julianday(cloned_at) IS NULL)
      OR (reused_at IS NOT NULL AND julianday(reused_at) IS NULL)
  )`));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM feedback WHERE value NOT IN ('有用','无用','复用','待确定')
      OR julianday(created_at) IS NULL OR julianday(updated_at) IS NULL
  )`));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM decision_events WHERE value NOT IN ('有用','无用','复用','待确定')
      OR julianday(created_at) IS NULL
  )`));

  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_events AS legacy
    JOIN project_action_events_v2 AS stable
      ON stable.device_id = legacy.device_id
      AND stable.idempotency_key = legacy.idempotency_key
    LEFT JOIN project_identity_catalog AS identity
      ON identity.generation_id = ? AND identity.project_slug = legacy.project_slug
    WHERE stable.project_slug IS NOT legacy.project_slug
      OR stable.action IS NOT legacy.action
      OR stable.occurred_at IS NOT legacy.occurred_at
      OR (identity.project_id IS NOT NULL AND (
        stable.project_id_version IS NOT identity.project_id_version
        OR stable.project_id IS NOT identity.project_id
      ))
  )`, catalog.generationId));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_events AS legacy
    WHERE NOT EXISTS (
      SELECT 1 FROM project_action_events_v2 AS stable
      WHERE stable.device_id = legacy.device_id
        AND stable.idempotency_key = legacy.idempotency_key
    ) AND NOT EXISTS (
      SELECT 1 FROM project_identity_catalog AS identity
      WHERE identity.generation_id = ? AND identity.project_slug = legacy.project_slug
    )
  )`, catalog.generationId));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_actions AS legacy
    WHERE NOT EXISTS (
      SELECT 1 FROM project_action_events AS event
      WHERE event.device_id = legacy.device_id
        AND event.project_slug = legacy.project_slug
        AND event.action = legacy.action
        AND ABS(julianday(event.occurred_at) - julianday(legacy.created_at)) < (2.0 / 86400000.0)
    )
  )`));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_events AS event
    WHERE NOT EXISTS (
      SELECT 1 FROM project_actions AS legacy
      WHERE legacy.device_id = event.device_id
        AND legacy.project_slug = event.project_slug
        AND legacy.action = event.action
        AND ABS(
          julianday(legacy.created_at) - (
            SELECT MAX(julianday(candidate.occurred_at))
            FROM project_action_events AS candidate
            WHERE candidate.device_id = event.device_id
              AND candidate.project_slug = event.project_slug
              AND candidate.action = event.action
          )
        ) < (2.0 / 86400000.0)
    )
  )`));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_state AS state
    WHERE NOT EXISTS (
      SELECT 1 FROM project_identity_catalog AS mapping
      JOIN project_action_state_v2 AS stable
        ON stable.device_id = state.device_id AND stable.project_id = mapping.project_id
      WHERE mapping.project_slug = state.project_slug
        AND ${stateFactsMatchSql("stable", "state")}
    ) AND (
      NOT EXISTS (
        SELECT 1 FROM project_action_events AS event
        WHERE event.device_id = state.device_id AND event.project_slug = state.project_slug
      )
      OR ${actionRankSql("state.highest_stage")} <> (
        SELECT MAX(${actionRankSql("event.action")}) FROM project_action_events AS event
        WHERE event.device_id = state.device_id AND event.project_slug = state.project_slug
      )
      OR (state.opened_at IS NULL) <> NOT EXISTS (
        SELECT 1 FROM project_action_events AS event WHERE event.device_id = state.device_id
          AND event.project_slug = state.project_slug AND event.action = 'opened')
      OR (state.opened_at IS NOT NULL AND julianday(state.opened_at) <> (
        SELECT MAX(julianday(event.occurred_at)) FROM project_action_events AS event
        WHERE event.device_id = state.device_id AND event.project_slug = state.project_slug
          AND event.action = 'opened'))
      OR (state.saved_at IS NULL) <> NOT EXISTS (
        SELECT 1 FROM project_action_events AS event WHERE event.device_id = state.device_id
          AND event.project_slug = state.project_slug AND event.action = 'saved')
      OR (state.saved_at IS NOT NULL AND julianday(state.saved_at) <> (
        SELECT MAX(julianday(event.occurred_at)) FROM project_action_events AS event
        WHERE event.device_id = state.device_id AND event.project_slug = state.project_slug
          AND event.action = 'saved'))
      OR (state.tried_at IS NULL) <> NOT EXISTS (
        SELECT 1 FROM project_action_events AS event WHERE event.device_id = state.device_id
          AND event.project_slug = state.project_slug AND event.action = 'tried')
      OR (state.tried_at IS NOT NULL AND julianday(state.tried_at) <> (
        SELECT MAX(julianday(event.occurred_at)) FROM project_action_events AS event
        WHERE event.device_id = state.device_id AND event.project_slug = state.project_slug
          AND event.action = 'tried'))
      OR (state.cloned_at IS NULL) <> NOT EXISTS (
        SELECT 1 FROM project_action_events AS event WHERE event.device_id = state.device_id
          AND event.project_slug = state.project_slug AND event.action = 'cloned')
      OR (state.cloned_at IS NOT NULL AND julianday(state.cloned_at) <> (
        SELECT MAX(julianday(event.occurred_at)) FROM project_action_events AS event
        WHERE event.device_id = state.device_id AND event.project_slug = state.project_slug
          AND event.action = 'cloned'))
      OR (state.reused_at IS NULL) <> NOT EXISTS (
        SELECT 1 FROM project_action_events AS event WHERE event.device_id = state.device_id
          AND event.project_slug = state.project_slug AND event.action = 'reused')
      OR (state.reused_at IS NOT NULL AND julianday(state.reused_at) <> (
        SELECT MAX(julianday(event.occurred_at)) FROM project_action_events AS event
        WHERE event.device_id = state.device_id AND event.project_slug = state.project_slug
          AND event.action = 'reused'))
      OR julianday(state.updated_at) <> (
        SELECT MAX(julianday(event.occurred_at)) FROM project_action_events AS event
        WHERE event.device_id = state.device_id AND event.project_slug = state.project_slug)
    )
  )`));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_state_v2 AS stable
    WHERE (
      SELECT COUNT(DISTINCT legacy.rowid) FROM project_action_state AS legacy
      JOIN project_identity_catalog AS mapping
        ON mapping.project_slug = legacy.project_slug AND mapping.project_id = stable.project_id
      WHERE ${stateFactsMatchSql("stable", "legacy")}
    ) <> 1
  )`));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_events AS legacy
    WHERE NOT EXISTS (
      SELECT 1 FROM project_action_state AS state WHERE state.device_id = legacy.device_id
        AND state.project_slug = legacy.project_slug
    ) AND NOT EXISTS (
      SELECT 1 FROM project_action_events_v2 AS stable WHERE stable.device_id = legacy.device_id
        AND stable.idempotency_key = legacy.idempotency_key
    )
  ) OR EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE NOT EXISTS (
      SELECT 1 FROM project_action_state_v2 AS state WHERE state.device_id = event.device_id
        AND state.project_id = event.project_id
    )
  )`));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_state_v2 AS stable
    JOIN project_identity_catalog AS current
      ON current.generation_id = ? AND current.project_id = stable.project_id
    WHERE stable.project_slug <> current.project_slug AND (
      NOT EXISTS (
        SELECT 1 FROM project_action_state AS source
        WHERE source.device_id = stable.device_id AND source.project_slug = stable.project_slug
          AND ${stateFactsMatchSql("stable", "source")}
      ) OR EXISTS (
        SELECT 1 FROM project_action_state AS target
        WHERE target.device_id = stable.device_id AND target.project_slug = current.project_slug
      )
    )
  )`, catalog.generationId));

  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM feedback AS legacy
    WHERE NOT EXISTS (
      SELECT 1 FROM project_identity_catalog AS mapping
      JOIN feedback_v2 AS stable
        ON stable.device_id = legacy.device_id AND stable.project_id = mapping.project_id
      WHERE mapping.project_slug = legacy.project_slug
        AND ${feedbackFactsMatchSql("stable", "legacy")}
    ) AND NOT EXISTS (
      SELECT 1 FROM project_identity_catalog AS current
      WHERE current.generation_id = ? AND current.project_slug = legacy.project_slug
    )
  )`, catalog.generationId));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM feedback_v2 AS stable
    WHERE (
      SELECT COUNT(DISTINCT legacy.rowid) FROM feedback AS legacy
      JOIN project_identity_catalog AS mapping
        ON mapping.project_slug = legacy.project_slug AND mapping.project_id = stable.project_id
      WHERE ${feedbackFactsMatchSql("stable", "legacy")}
    ) <> 1
  )`));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM feedback_v2 AS stable
    JOIN project_identity_catalog AS current
      ON current.generation_id = ? AND current.project_id = stable.project_id
    WHERE stable.project_slug <> current.project_slug AND (
      NOT EXISTS (
        SELECT 1 FROM feedback AS source
        WHERE source.device_id = stable.device_id AND source.project_slug = stable.project_slug
          AND ${feedbackFactsMatchSql("stable", "source")}
      ) OR EXISTS (
        SELECT 1 FROM feedback AS target
        WHERE target.device_id = stable.device_id AND target.project_slug = current.project_slug
      )
    )
  )`, catalog.generationId));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM decision_events AS legacy
    LEFT JOIN project_identity_catalog AS identity
      ON identity.generation_id = ? AND identity.project_slug = legacy.project_slug
    LEFT JOIN decision_events_v2 AS stable ON stable.legacy_event_id = legacy.id
    WHERE (stable.legacy_event_id IS NOT NULL AND (
        stable.device_id IS NOT legacy.device_id
        OR stable.project_slug IS NOT legacy.project_slug
        OR stable.value IS NOT legacy.value
        OR stable.occurred_at IS NOT legacy.created_at
        OR (identity.project_id IS NOT NULL AND (
          stable.project_id_version IS NOT identity.project_id_version
          OR stable.project_id IS NOT identity.project_id
        ))
      )) OR (stable.legacy_event_id IS NULL AND identity.project_id IS NULL)
  )`, catalog.generationId));
}

function adoptionStatements(database, catalog) {
  const statements = [];
  for (const project of catalog.projects) {
    statements.push(database.prepare(`INSERT INTO project_identity_catalog (
      generation_id, project_id_version, project_id, canonical_repository, project_slug
    ) VALUES (?, ?, ?, ?, ?) ON CONFLICT (generation_id, project_id) DO NOTHING`)
      .bind(catalog.generationId, project.projectIdVersion, project.projectId,
        project.canonicalRepository, project.projectSlug));
  }
  addInBatchAdoptionGuards(statements, database, catalog);
  statements.push(database.prepare(`INSERT INTO project_identity_runtime (
      singleton, generation_id, published_at, published_at_micros
    ) VALUES (1, ?, ?, ?) ON CONFLICT (singleton) DO UPDATE SET
      generation_id = excluded.generation_id,
      published_at = excluded.published_at,
      published_at_micros = excluded.published_at_micros
    WHERE project_identity_runtime.published_at_micros <= excluded.published_at_micros`)
    .bind(catalog.generationId, catalog.publishedAt, catalog.publishedAtMicros));
  statements.push(database.prepare(`UPDATE project_action_state AS legacy
    SET project_slug = (
      SELECT current.project_slug FROM project_action_state_v2 AS stable
      JOIN project_identity_catalog AS current
        ON current.generation_id = ? AND current.project_id = stable.project_id
      WHERE stable.device_id = legacy.device_id AND stable.project_slug = legacy.project_slug
      LIMIT 1
    )
    WHERE EXISTS (
      SELECT 1 FROM project_action_state_v2 AS stable
      JOIN project_identity_catalog AS current
        ON current.generation_id = ? AND current.project_id = stable.project_id
      WHERE stable.device_id = legacy.device_id AND stable.project_slug = legacy.project_slug
        AND stable.project_slug <> current.project_slug
    )`).bind(catalog.generationId, catalog.generationId));
  statements.push(database.prepare(`UPDATE feedback AS legacy
    SET project_slug = (
      SELECT current.project_slug FROM feedback_v2 AS stable
      JOIN project_identity_catalog AS current
        ON current.generation_id = ? AND current.project_id = stable.project_id
      WHERE stable.device_id = legacy.device_id AND stable.project_slug = legacy.project_slug
      LIMIT 1
    )
    WHERE EXISTS (
      SELECT 1 FROM feedback_v2 AS stable
      JOIN project_identity_catalog AS current
        ON current.generation_id = ? AND current.project_id = stable.project_id
      WHERE stable.device_id = legacy.device_id AND stable.project_slug = legacy.project_slug
        AND stable.project_slug <> current.project_slug
    )`).bind(catalog.generationId, catalog.generationId));
  statements.push(database.prepare(`UPDATE project_action_state_v2 AS stable
    SET project_slug = (
      SELECT current.project_slug FROM project_identity_catalog AS current
      WHERE current.generation_id = ? AND current.project_id = stable.project_id
    ), catalog_generation_id = ?
    WHERE EXISTS (
      SELECT 1 FROM project_identity_catalog AS current
      WHERE current.generation_id = ? AND current.project_id = stable.project_id
        AND (stable.project_slug <> current.project_slug
          OR stable.catalog_generation_id <> current.generation_id)
    )`).bind(catalog.generationId, catalog.generationId, catalog.generationId));
  statements.push(database.prepare(`UPDATE feedback_v2 AS stable
    SET project_slug = (
      SELECT current.project_slug FROM project_identity_catalog AS current
      WHERE current.generation_id = ? AND current.project_id = stable.project_id
    ), catalog_generation_id = ?
    WHERE EXISTS (
      SELECT 1 FROM project_identity_catalog AS current
      WHERE current.generation_id = ? AND current.project_id = stable.project_id
        AND (stable.project_slug <> current.project_slug
          OR stable.catalog_generation_id <> current.generation_id)
    )`).bind(catalog.generationId, catalog.generationId, catalog.generationId));
  statements.push(database.prepare(`INSERT INTO project_action_events_v2 (
    device_id, project_id_version, project_id, project_slug,
    catalog_generation_id, action, occurred_at, idempotency_key
  ) SELECT legacy.device_id, identity.project_id_version, identity.project_id,
    legacy.project_slug, identity.generation_id, legacy.action, legacy.occurred_at,
    legacy.idempotency_key FROM project_action_events AS legacy
    JOIN project_identity_catalog AS identity ON identity.generation_id = ?
      AND identity.project_slug = legacy.project_slug
    WHERE NOT EXISTS (
      SELECT 1 FROM project_action_events_v2 AS stable
      WHERE stable.device_id = legacy.device_id
        AND stable.idempotency_key = legacy.idempotency_key
    )`).bind(catalog.generationId));
  statements.push(database.prepare(`INSERT INTO feedback_v2 (
    device_id, project_id_version, project_id, project_slug,
    catalog_generation_id, value, created_at, updated_at
  ) SELECT legacy.device_id, identity.project_id_version, identity.project_id,
    legacy.project_slug, identity.generation_id, legacy.value,
    legacy.created_at, legacy.updated_at FROM feedback AS legacy
    JOIN project_identity_catalog AS identity ON identity.generation_id = ?
      AND identity.project_slug = legacy.project_slug
    WHERE NOT EXISTS (
      SELECT 1 FROM feedback_v2 AS stable
      WHERE stable.device_id = legacy.device_id
        AND stable.project_id = identity.project_id
    )`).bind(catalog.generationId));
  statements.push(database.prepare(`INSERT INTO decision_events_v2 (
    legacy_event_id, device_id, project_id_version, project_id, project_slug,
    catalog_generation_id, value, occurred_at
  ) SELECT legacy.id, legacy.device_id, identity.project_id_version,
    identity.project_id, legacy.project_slug, identity.generation_id,
    legacy.value, legacy.created_at FROM decision_events AS legacy
    JOIN project_identity_catalog AS identity ON identity.generation_id = ?
      AND identity.project_slug = legacy.project_slug
    WHERE NOT EXISTS (
      SELECT 1 FROM decision_events_v2 AS stable
      WHERE stable.legacy_event_id = legacy.id
    )`).bind(catalog.generationId));
  return statements;
}

export async function adoptStableProjectIdentities(database, context) {
  const catalog = normalizeCatalogContext(context);
  await preflightCatalogAdoption(database, catalog);
  try {
    await database.batch(adoptionStatements(database, catalog));
  } catch (error) {
    try {
      await preflightCatalogAdoption(database, catalog);
    } catch (preflightError) {
      throw preflightError;
    }
    if (/constraint|unique|guard|immutable|projection|stale/i.test(String(error?.message ?? error))) {
      throw new StableProjectDecisionError(
        "concurrent_project_identity_adoption_conflict",
        "identity adoption lost a concurrent write race; no rows were adopted",
        { cause: String(error?.message ?? error) },
      );
    }
    throw error;
  }
  return {
    status: "ready",
    generationId: catalog.generationId,
    projectCount: catalog.projects.length,
  };
}
