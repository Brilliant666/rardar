import { ACTION_VALUES, LEGACY_IDEMPOTENCY_PREFIX } from "./project-actions.mjs";
import { validateLegacyProjectIdentityPolicy } from "./legacy-project-identity-policy.mjs";

export const PROJECT_ID_VERSION = 1;
export const FEEDBACK_VALUES = Object.freeze(["有用", "无用", "复用", "待确定"]);

const PROJECT_ID_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*--[0-9a-f]{20}$/;
const PROJECT_ID_MAX_LENGTH = 86;
const TIMESTAMP_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const CANONICAL_REPOSITORY_PATTERN = /^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,38}\/(?!\.{1,2}$)[a-z0-9._-]{1,100}$/;
const POLICY_VERSION_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})$/;
const QUARANTINE_REASON = "no_verified_repository_in_current_or_retained_catalogs";
const QUARANTINE_SOURCE_TABLES = Object.freeze([
  "feedback",
  "decision_events",
]);

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

function requireNonEmptyString(value, label) {
  if (typeof value !== "string" || !value || value.includes("\0")) {
    throw new TypeError(`${label} must be a non-empty string`);
  }
  return value;
}

function legacyCatalogBundle(context) {
  if (!context || typeof context !== "object" || Array.isArray(context)) return null;
  if (typeof context.generationId !== "string" || !context.generationId) return null;
  const publishedAt = requireTimestamp(context.publishedAt, "identity catalog publishedAt");
  const projects = context.projects ?? context.entries;
  if (!Array.isArray(projects)) throw new TypeError("identity catalog projects are required");
  const generation = {
    generationId: context.generationId,
    generationCreatedAt: "1970-01-01T00:00:00Z",
    publishedAt,
    manifestSha256: "0".repeat(64),
    catalogSchemaVersion: 3,
    active: true,
  };
  return {
    schemaVersion: 1,
    activeGenerationId: context.generationId,
    activePublishedAt: publishedAt,
    generationCount: 1,
    mappingCount: projects.length,
    generations: [generation],
    mappings: projects.map((project) => ({
      ...generation,
      projectIdVersion: project?.projectIdVersion,
      projectId: project?.projectId,
      canonicalRepository: String(project?.repository ?? project?.repo ?? "").toLowerCase(),
      projectSlug: project?.projectSlug ?? project?.slug,
    })),
  };
}

function normalizePolicy(value) {
  const policy = value === undefined
    ? { schemaVersion: 1, policyVersion: "none-v1", entries: [] }
    : validateLegacyProjectIdentityPolicy(value);
  if (!policy || typeof policy !== "object" || Array.isArray(policy)
    || policy.schemaVersion !== 1
    || typeof policy.policyVersion !== "string"
    || !POLICY_VERSION_PATTERN.test(policy.policyVersion)
    || !Array.isArray(policy.entries)) {
    throw new TypeError("legacy project identity disposition policy is invalid");
  }
  const bySlugAndTable = new Map();
  const entries = policy.entries.map((entry) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
      throw new TypeError("legacy project identity disposition entry must be an object");
    }
    const projectSlug = requireNonEmptyString(entry.projectSlug, "legacy disposition slug");
    if (/[*?\[\]]/.test(projectSlug)) {
      throw new TypeError("legacy project identity dispositions cannot contain wildcards");
    }
    if (entry.disposition !== "quarantine" || entry.reasonCode !== QUARANTINE_REASON
      || !Array.isArray(entry.sourceTables) || !entry.sourceTables.length) {
      throw new TypeError("legacy project identity disposition entry is invalid");
    }
    const sourceTables = [];
    for (const table of entry.sourceTables) {
      if (!QUARANTINE_SOURCE_TABLES.includes(table) || sourceTables.includes(table)) {
        throw new TypeError("legacy project identity disposition source tables are invalid");
      }
      const key = rowKey(projectSlug, table);
      if (bySlugAndTable.has(key)) {
        throw new TypeError("legacy project identity disposition entries overlap");
      }
      bySlugAndTable.set(key, true);
      sourceTables.push(table);
    }
    return Object.freeze({
      projectSlug,
      disposition: "quarantine",
      reasonCode: QUARANTINE_REASON,
      sourceTables: Object.freeze(sourceTables),
    });
  });
  return Object.freeze({
    schemaVersion: 1,
    policyVersion: policy.policyVersion,
    entries: Object.freeze(entries),
    _bySlugAndTable: new Set(bySlugAndTable.keys()),
  });
}

function mappingOrder(left, right) {
  const instant = publicationMicroseconds(left.generationCreatedAt)
    - publicationMicroseconds(right.generationCreatedAt);
  if (instant !== 0) return instant;
  return left.generationId.localeCompare(right.generationId);
}

function normalizeHistoricalBundle(value) {
  const inlineLegacyBundle = legacyCatalogBundle(value);
  const bundle = inlineLegacyBundle ?? value;
  if (!bundle || typeof bundle !== "object" || Array.isArray(bundle)
    || bundle.schemaVersion !== 1
    || !Number.isSafeInteger(bundle.generationCount) || bundle.generationCount < 1
    || !Number.isSafeInteger(bundle.mappingCount) || bundle.mappingCount < 0
    || !Array.isArray(bundle.generations) || bundle.generationCount !== bundle.generations.length
    || !Array.isArray(bundle.mappings) || bundle.mappingCount !== bundle.mappings.length) {
    throw new TypeError("Historical Identity Bundle v1 is invalid");
  }
  const activeGenerationId = requireNonEmptyString(
    bundle.activeGenerationId,
    "Historical Identity Bundle activeGenerationId",
  );
  const activePublishedAt = requireTimestamp(
    bundle.activePublishedAt,
    "Historical Identity Bundle activePublishedAt",
  );
  const generationFacts = new Map();
  let activeGenerationCount = 0;
  const normalizedGenerations = bundle.generations.map((generation, index) => {
    if (!generation || typeof generation !== "object" || Array.isArray(generation)) {
      throw new TypeError(`Historical Identity Bundle generation ${index} must be an object`);
    }
    const generationId = requireNonEmptyString(
      generation.generationId,
      `generation ${index} generationId`,
    );
    const generationCreatedAt = requireTimestamp(
      generation.generationCreatedAt,
      `generation ${index} generationCreatedAt`,
    );
    const publishedAt = generation.publishedAt === null
      ? null
      : requireTimestamp(generation.publishedAt, `generation ${index} publishedAt`);
    if (typeof generation.manifestSha256 !== "string"
      || !SHA256_PATTERN.test(generation.manifestSha256)
      || ![1, 2, 3].includes(generation.catalogSchemaVersion)
      || typeof generation.active !== "boolean") {
      throw new TypeError(`Historical Identity Bundle generation ${index} provenance is invalid`);
    }
    const expectedActive = generationId === activeGenerationId;
    if (generation.active !== expectedActive
      || (expectedActive && publishedAt !== activePublishedAt)
      || (!expectedActive && publishedAt !== null)) {
      throw new StableProjectDecisionError(
        "invalid_historical_identity_bundle",
        "Historical Identity Bundle active generation provenance is inconsistent",
      );
    }
    if (generationFacts.has(generationId)) {
      throw new StableProjectDecisionError(
        "invalid_historical_identity_bundle",
        "Historical Identity Bundle repeats generation provenance",
      );
    }
    if (generation.active) activeGenerationCount += 1;
    const normalizedGeneration = Object.freeze({
      generationId,
      generationCreatedAt,
      publishedAt,
      manifestSha256: generation.manifestSha256,
      catalogSchemaVersion: generation.catalogSchemaVersion,
      active: generation.active,
    });
    generationFacts.set(generationId, normalizedGeneration);
    return normalizedGeneration;
  });
  if (activeGenerationCount !== 1 || !generationFacts.has(activeGenerationId)) {
    throw new StableProjectDecisionError(
      "invalid_historical_identity_bundle",
      "Historical Identity Bundle generation counts or active generation are inconsistent",
    );
  }
  const generationProjectIds = new Set();
  const generationRepositories = new Set();
  const generationSlugs = new Set();
  const projectIdToRepository = new Map();
  const repositoryToProjectId = new Map();
  const slugToProjectId = new Map();
  const normalized = bundle.mappings.map((mapping, index) => {
    if (!mapping || typeof mapping !== "object" || Array.isArray(mapping)) {
      throw new TypeError(`Historical Identity Bundle mapping ${index} must be an object`);
    }
    const generationId = requireNonEmptyString(mapping.generationId, `mapping ${index} generationId`);
    const generationFact = generationFacts.get(generationId);
    if (!generationFact) {
      throw new StableProjectDecisionError(
        "invalid_historical_identity_bundle",
        "Historical Identity Bundle mapping has no generation provenance",
      );
    }
    const generationCreatedAt = requireTimestamp(
      mapping.generationCreatedAt,
      `mapping ${index} generationCreatedAt`,
    );
    const publishedAt = mapping.publishedAt === null
      ? null
      : requireTimestamp(mapping.publishedAt, `mapping ${index} publishedAt`);
    if (typeof mapping.manifestSha256 !== "string" || !SHA256_PATTERN.test(mapping.manifestSha256)
      || ![1, 2, 3].includes(mapping.catalogSchemaVersion)
      || typeof mapping.active !== "boolean") {
      throw new TypeError(`Historical Identity Bundle mapping ${index} provenance is invalid`);
    }
    const projectIdVersion = mapping.projectIdVersion;
    const projectId = requireNonEmptyString(mapping.projectId, `mapping ${index} projectId`);
    const canonicalRepository = requireNonEmptyString(
      mapping.canonicalRepository,
      `mapping ${index} canonicalRepository`,
    );
    const projectSlug = requireNonEmptyString(mapping.projectSlug, `mapping ${index} projectSlug`);
    requireIdentity({ projectIdVersion, projectId, projectSlug, catalogGenerationId: generationId });
    if (canonicalRepository !== canonicalRepository.toLowerCase()
      || !CANONICAL_REPOSITORY_PATTERN.test(canonicalRepository)) {
      throw new TypeError(`Historical Identity Bundle mapping ${index} repository is not canonical`);
    }
    if (generationCreatedAt !== generationFact.generationCreatedAt
      || publishedAt !== generationFact.publishedAt
      || mapping.manifestSha256 !== generationFact.manifestSha256
      || mapping.catalogSchemaVersion !== generationFact.catalogSchemaVersion
      || mapping.active !== generationFact.active) {
      throw new StableProjectDecisionError(
        "invalid_historical_identity_bundle",
        "Historical Identity Bundle mapping provenance differs from its generation entry",
      );
    }

    for (const [set, key] of [
      [generationProjectIds, rowKey(generationId, projectId)],
      [generationRepositories, rowKey(generationId, canonicalRepository)],
      [generationSlugs, rowKey(generationId, projectSlug)],
    ]) {
      if (set.has(key)) {
        throw new StableProjectDecisionError(
          "ambiguous_project_identity",
          "Historical Identity Bundle repeats an identity within one generation",
        );
      }
      set.add(key);
    }
    const repositoryForId = projectIdToRepository.get(projectId);
    const idForRepository = repositoryToProjectId.get(canonicalRepository);
    const idForSlug = slugToProjectId.get(projectSlug);
    if ((repositoryForId && repositoryForId !== canonicalRepository)
      || (idForRepository && idForRepository !== projectId)
      || (idForSlug && idForSlug !== projectId)) {
      throw new StableProjectDecisionError(
        "project_identity_collision",
        "Historical Identity Bundle contains a project ID, repository, or legacy slug rebind",
      );
    }
    projectIdToRepository.set(projectId, canonicalRepository);
    repositoryToProjectId.set(canonicalRepository, projectId);
    slugToProjectId.set(projectSlug, projectId);
    return Object.freeze({
      generationId,
      generationCreatedAt,
      publishedAt,
      manifestSha256: mapping.manifestSha256,
      catalogSchemaVersion: mapping.catalogSchemaVersion,
      projectIdVersion,
      projectId,
      canonicalRepository,
      projectSlug,
      active: mapping.active,
    });
  });
  const mappingsBySlug = new Map();
  for (const mapping of normalized) {
    mappingsBySlug.set(mapping.projectSlug, [
      ...(mappingsBySlug.get(mapping.projectSlug) ?? []),
      mapping,
    ]);
  }
  const resolutionBySlug = new Map();
  for (const [slug, mappings] of mappingsBySlug) {
    const active = mappings.find((mapping) => mapping.active);
    const witness = active ?? [...mappings].sort(mappingOrder).at(-1);
    resolutionBySlug.set(slug, witness);
  }
  const activeMappings = normalized.filter((mapping) => mapping.active);
  return Object.freeze({
    schemaVersion: 1,
    activeGenerationId,
    activePublishedAt,
    activePublishedAtMicros: publicationMicroseconds(activePublishedAt),
    generationCount: bundle.generationCount,
    mappingCount: bundle.mappingCount,
    generations: Object.freeze(normalizedGenerations),
    mappings: Object.freeze(normalized),
    activeMappings: Object.freeze(activeMappings),
    resolutionBySlug,
    allowStoredHistoricalMappings: Boolean(inlineLegacyBundle),
  });
}

function normalizeAdoptionContext(context, policyArgument) {
  const wrapped = context && typeof context === "object" && !Array.isArray(context)
    && context.bundle ? context : null;
  const bundle = normalizeHistoricalBundle(wrapped?.bundle ?? context);
  const policy = normalizePolicy(policyArgument ?? wrapped?.policy);
  return {
    bundle,
    policy,
    generationId: bundle.activeGenerationId,
    publishedAt: bundle.activePublishedAt,
    publishedAtMicros: bundle.activePublishedAtMicros,
    projects: bundle.activeMappings.map((mapping) => ({
      projectIdVersion: mapping.projectIdVersion,
      projectId: mapping.projectId,
      projectSlug: mapping.projectSlug,
      repository: mapping.canonicalRepository,
      canonicalRepository: mapping.canonicalRepository,
    })),
    generations: bundle.generations,
    mappings: bundle.mappings,
    resolutionBySlug: bundle.resolutionBySlug,
    allowStoredHistoricalMappings: bundle.allowStoredHistoricalMappings,
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

function exactGenerationEvidence(existing, expected) {
  return existing.generationCreatedAt === expected.generationCreatedAt
    && existing.manifestSha256 === expected.manifestSha256
    && existing.catalogSchemaVersion === expected.catalogSchemaVersion;
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
    unresolvedLegacy, adoptionSessions, adoptionAllowedMappings,
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
      allRows(database, `SELECT id, device_id AS deviceId, project_slug AS projectSlug,
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
      allRows(database, `SELECT source_table AS sourceTable, source_key AS sourceKey,
        project_slug AS projectSlug, disposition, reason_code AS reasonCode,
        policy_version AS policyVersion, first_seen_generation_id AS firstSeenGenerationId,
        created_at AS createdAt FROM project_identity_unresolved_legacy`),
      allRows(database, `SELECT singleton, session_id AS sessionId,
        active_generation_id AS activeGenerationId, policy_version AS policyVersion,
        created_at AS createdAt FROM project_identity_adoption_session`),
      allRows(database, `SELECT session_id AS sessionId, project_slug AS projectSlug,
        generation_id AS generationId, project_id_version AS projectIdVersion,
        project_id AS projectId FROM project_identity_adoption_allowed_mapping`),
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
    unresolvedLegacy,
    adoptionSessions,
    adoptionAllowedMappings,
  };
}

function validateExistingMappings(existingMappings, catalog) {
  const expectedByGeneration = new Map();
  for (const mapping of catalog.mappings) {
    expectedByGeneration.set(mapping.generationId, [
      ...(expectedByGeneration.get(mapping.generationId) ?? []),
      mapping,
    ]);
  }
  for (const [generationId, expectedMappings] of expectedByGeneration) {
    const existingForGeneration = existingMappings.filter(
      (mapping) => mapping.generationId === generationId,
    );
    if (!existingForGeneration.length) continue;
    if (existingForGeneration.length !== expectedMappings.length) {
      throw new StableProjectDecisionError(
        "conflicting_project_identity_mapping",
        "existing generation identity mapping differs from the verified historical Catalog",
        { generationId },
      );
    }
    const byProjectId = new Map(existingForGeneration.map((mapping) => [mapping.projectId, mapping]));
    for (const expected of expectedMappings) {
      const existing = byProjectId.get(expected.projectId);
      if (!existing || !exactCatalogMapping(existing, expected)) {
        throw new StableProjectDecisionError(
          "conflicting_project_identity_mapping",
          "existing generation identity mapping differs from the verified historical Catalog",
          { generationId },
        );
      }
    }
  }
}

function validateGlobalIdentityMappings(existingMappings, catalog) {
  const byId = new Map();
  const byRepository = new Map();
  const byLegacySlug = new Map();
  for (const existing of [...existingMappings, ...catalog.mappings]) {
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

function exactUnresolvedDisposition(existing, expected) {
  return existing.projectSlug === expected.projectSlug
    && existing.disposition === expected.disposition
    && existing.reasonCode === expected.reasonCode
    && existing.policyVersion === expected.policyVersion;
}

function quarantineFact(catalog, rows, table, row, identity, quarantineFacts) {
  const sourceKey = String(row.id);
  const existing = rows.unresolvedLegacy.find(
    (candidate) => candidate.sourceTable === table && candidate.sourceKey === sourceKey,
  );
  if (identity) {
    if (existing) {
      throw new StableProjectDecisionError(
        "quarantined_project_identity_requires_resolution",
        "a quarantined legacy fact cannot be adopted without an explicit resolution migration",
        { sourceTable: table, sourceKey, projectSlug: row.projectSlug },
      );
    }
    return false;
  }
  if (!Number.isSafeInteger(Number(row.id)) || Number(row.id) < 1) {
    throw invalidLegacyError(table, row, "source row has no stable integer identity");
  }
  if (!catalog.policy._bySlugAndTable.has(rowKey(row.projectSlug, table))) {
    throw unresolvedError([row]);
  }
  const expected = {
    sourceTable: table,
    sourceKey,
    projectSlug: row.projectSlug,
    disposition: "quarantine",
    reasonCode: QUARANTINE_REASON,
    policyVersion: catalog.policy.policyVersion,
  };
  if (existing && !exactUnresolvedDisposition(existing, expected)) {
    throw new StableProjectDecisionError(
      "conflicting_unresolved_legacy_disposition",
      "an existing unresolved legacy disposition differs from the exact policy",
      { sourceTable: table, sourceKey, projectSlug: row.projectSlug },
    );
  }
  quarantineFacts.push({ ...expected, existing: Boolean(existing) });
  return true;
}

function preflightLegacyRows(catalog, rows, allMappings) {
  if (rows.adoptionSessions.length || rows.adoptionAllowedMappings.length) {
    throw new StableProjectDecisionError(
      "active_project_identity_adoption_session",
      "a project identity adoption session was left active",
    );
  }
  const quarantineFacts = [];
  const identityBySlug = new Map(catalog.resolutionBySlug);
  if (catalog.allowStoredHistoricalMappings) {
    for (const mapping of allMappings) {
      if (!identityBySlug.has(mapping.projectSlug)) identityBySlug.set(mapping.projectSlug, mapping);
    }
  }
  const identityByProjectId = new Map(catalog.projects.map((project) => [project.projectId, project]));
  const historicalProjectIdBySlug = new Map();
  for (const mapping of [...allMappings, ...catalog.mappings]) {
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
    if (quarantineFact(catalog, rows, "feedback", row, identity, quarantineFacts)) continue;
    const projectId = historicalProjectIdBySlug.get(row.projectSlug);
    const target = projectId
      ? stableFeedbackByTarget.get(rowKey(row.deviceId, projectId))
      : null;
    const stableKey = rowKey(row.deviceId, projectId);
    if (legacyFeedbackByStableTarget.has(stableKey)) {
      throw conflictingProjectionError("feedback", row, "multiple legacy State rows map to one projectId");
    }
    legacyFeedbackByStableTarget.set(stableKey, row);
    if (target) {
      if (!exactFeedbackFacts(target, row)) {
        throw conflictingProjectionError("feedback", row, "stable State has different facts");
      }
      if (identity && target.projectIdVersion !== identity.projectIdVersion) {
        throw conflictingProjectionError("feedback", row, "stable identity version differs from catalog");
      }
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
    if (quarantineFact(catalog, rows, "decision_events", row, identity, quarantineFacts)) continue;
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
    }
  }
  const migratedProjectIds = new Set();
  let migratedFactCount = 0;
  for (const row of rows.actionEvents) {
    const identity = identityBySlug.get(row.projectSlug);
    if (identity) {
      migratedProjectIds.add(identity.projectId);
      migratedFactCount += 1;
    }
  }
  for (const row of rows.feedback) {
    const identity = identityBySlug.get(row.projectSlug);
    if (identity) {
      migratedProjectIds.add(identity.projectId);
      migratedFactCount += 1;
    }
  }
  for (const row of rows.decisions) {
    const identity = identityBySlug.get(row.projectSlug);
    if (identity) {
      migratedProjectIds.add(identity.projectId);
      migratedFactCount += 1;
    }
  }
  return {
    rows,
    resolutionBySlug: identityBySlug,
    quarantineFacts,
    migratedProjectCount: migratedProjectIds.size,
    migratedFactCount,
    quarantinedSlugCount: new Set(quarantineFacts.map((fact) => fact.projectSlug)).size,
    quarantinedFactCount: quarantineFacts.length,
  };
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
  const allEvidence = await allRows(database, `SELECT generation_id AS generationId,
    generation_created_at AS generationCreatedAt,
    manifest_sha256 AS manifestSha256, catalog_schema_version AS catalogSchemaVersion
    FROM project_identity_generation_evidence`);
  validateGlobalIdentityMappings(allMappings, catalog);
  validateExistingMappings(allMappings, catalog);
  const expectedEvidence = new Map(
    catalog.generations.map((generation) => [generation.generationId, generation]),
  );
  const mappedGenerations = new Set(allMappings.map((mapping) => mapping.generationId));
  const evidencedGenerations = new Set(allEvidence.map((evidence) => evidence.generationId));
  for (const generationId of new Set([...mappedGenerations, ...evidencedGenerations])) {
    if (mappedGenerations.has(generationId) !== evidencedGenerations.has(generationId)
      && !expectedEvidence.has(generationId)) {
      throw new StableProjectDecisionError(
        "unverified_project_identity_evidence",
        "stored project identity mappings and immutable generation evidence are incomplete",
        { generationId },
      );
    }
  }
  for (const existing of allEvidence) {
    const expected = expectedEvidence.get(existing.generationId);
    if (expected && !exactGenerationEvidence(existing, expected)) {
      throw new StableProjectDecisionError(
        "conflicting_project_identity_evidence",
        "existing generation evidence differs from the verified Historical Identity Bundle",
        { generationId: existing.generationId },
      );
    }
  }
  return preflightLegacyRows(catalog, await readAdoptionRows(database), allMappings);
}

function migrationGuard(database, condition, ...bindings) {
  return database.prepare(`INSERT INTO project_identity_migration_guard (failure)
    SELECT 1 WHERE ${condition}`).bind(...bindings);
}

function feedbackFactsMatchSql(stable, legacy) {
  return `${stable}.device_id IS ${legacy}.device_id
    AND ${stable}.value IS ${legacy}.value
    AND ${stable}.created_at IS ${legacy}.created_at
    AND ${stable}.updated_at IS ${legacy}.updated_at`;
}

function addInBatchAdoptionGuards(statements, database, catalog, plan, sessionId) {
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_identity_runtime AS runtime
    WHERE runtime.singleton = 1 AND (
      runtime.published_at_micros > ?
      OR (runtime.published_at_micros = ? AND runtime.generation_id <> ?)
    )
  )`, catalog.publishedAtMicros, catalog.publishedAtMicros, catalog.generationId));
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
  const expectedGenerationIds = catalog.generations.map((generation) => generation.generationId);
  const expectedGenerationPlaceholders = expectedGenerationIds.map(() => "?").join(", ");
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_identity_catalog AS identity
    WHERE NOT EXISTS (
      SELECT 1 FROM project_identity_generation_evidence AS evidence
      WHERE evidence.generation_id = identity.generation_id
    )
  ) OR EXISTS (
    SELECT 1 FROM project_identity_generation_evidence AS evidence
    WHERE NOT EXISTS (
      SELECT 1 FROM project_identity_catalog AS identity
      WHERE identity.generation_id = evidence.generation_id
    ) AND evidence.generation_id NOT IN (${expectedGenerationPlaceholders})
  )`, ...expectedGenerationIds));
  const mappingsByGeneration = new Map(
    catalog.generations.map((generation) => [generation.generationId, []]),
  );
  for (const mapping of catalog.mappings) {
    mappingsByGeneration.set(mapping.generationId, [
      ...(mappingsByGeneration.get(mapping.generationId) ?? []),
      mapping,
    ]);
  }
  for (const generation of catalog.generations) {
    statements.push(migrationGuard(database, `NOT EXISTS (
      SELECT 1 FROM project_identity_generation_evidence
      WHERE generation_id = ? AND generation_created_at = ?
        AND manifest_sha256 = ? AND catalog_schema_version = ?
    )`, generation.generationId, generation.generationCreatedAt,
    generation.manifestSha256, generation.catalogSchemaVersion));
  }
  for (const [generationId, mappings] of mappingsByGeneration) {
    statements.push(migrationGuard(database,
      `(SELECT COUNT(*) FROM project_identity_catalog WHERE generation_id = ?) <> ?`,
      generationId, mappings.length));
  }
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_events
    WHERE action NOT IN ('opened','saved','tried','cloned','reused')
      OR julianday(occurred_at) IS NULL
  ) OR EXISTS (
    SELECT 1 FROM project_actions
    WHERE action NOT IN ('opened','saved','tried','cloned','reused')
      OR julianday(created_at) IS NULL
  ) OR EXISTS (
    SELECT 1 FROM project_action_state
    WHERE highest_stage NOT IN ('opened','saved','tried','cloned','reused')
      OR julianday(updated_at) IS NULL
      OR (opened_at IS NOT NULL AND julianday(opened_at) IS NULL)
      OR (saved_at IS NOT NULL AND julianday(saved_at) IS NULL)
      OR (tried_at IS NOT NULL AND julianday(tried_at) IS NULL)
      OR (cloned_at IS NOT NULL AND julianday(cloned_at) IS NULL)
      OR (reused_at IS NOT NULL AND julianday(reused_at) IS NULL)
  ) OR EXISTS (
    SELECT 1 FROM feedback WHERE value NOT IN ('有用','无用','复用','待确定')
      OR julianday(created_at) IS NULL OR julianday(updated_at) IS NULL
  ) OR EXISTS (
    SELECT 1 FROM decision_events WHERE value NOT IN ('有用','无用','复用','待确定')
      OR julianday(created_at) IS NULL
  )`));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_events AS legacy
    WHERE NOT EXISTS (
      SELECT 1 FROM project_identity_adoption_allowed_mapping AS allowed
      WHERE allowed.session_id = ? AND allowed.project_slug = legacy.project_slug
    ) AND NOT EXISTS (
      SELECT 1 FROM project_action_events_v2 AS stable
      WHERE stable.device_id = legacy.device_id
        AND stable.idempotency_key = legacy.idempotency_key
    )
  )`, sessionId));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM feedback AS legacy
    WHERE NOT EXISTS (
      SELECT 1 FROM project_identity_adoption_allowed_mapping AS allowed
      WHERE allowed.session_id = ? AND allowed.project_slug = legacy.project_slug
    ) AND NOT EXISTS (
      SELECT 1 FROM project_identity_unresolved_legacy AS unresolved
      WHERE unresolved.source_table = 'feedback'
        AND unresolved.source_key = CAST(legacy.id AS TEXT)
        AND unresolved.project_slug = legacy.project_slug
    )
  )`, sessionId));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM decision_events AS legacy
    WHERE NOT EXISTS (
      SELECT 1 FROM project_identity_adoption_allowed_mapping AS allowed
      WHERE allowed.session_id = ? AND allowed.project_slug = legacy.project_slug
    ) AND NOT EXISTS (
      SELECT 1 FROM project_identity_unresolved_legacy AS unresolved
      WHERE unresolved.source_table = 'decision_events'
        AND unresolved.source_key = CAST(legacy.id AS TEXT)
        AND unresolved.project_slug = legacy.project_slug
    )
  )`, sessionId));
  for (const fact of plan.quarantineFacts) {
    const table = fact.sourceTable;
    if (table === "feedback") {
      const source = plan.rows.feedback.find((row) => String(row.id) === fact.sourceKey);
      statements.push(migrationGuard(database, `NOT EXISTS (
        SELECT 1 FROM feedback WHERE id = ? AND device_id IS ? AND project_slug IS ?
          AND value IS ? AND created_at IS ? AND updated_at IS ?
      )`, source.id, source.deviceId, source.projectSlug,
      source.value, source.createdAt, source.updatedAt));
    } else if (table === "decision_events") {
      const source = plan.rows.decisions.find((row) => String(row.id) === fact.sourceKey);
      statements.push(migrationGuard(database, `NOT EXISTS (
        SELECT 1 FROM decision_events WHERE id = ? AND device_id IS ? AND project_slug IS ?
          AND value IS ? AND created_at IS ?
      )`, source.id, source.deviceId, source.projectSlug, source.value, source.createdAt));
    }
  }
}

function addPostAdoptionGuards(statements, database, plan, sessionId) {
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_events AS legacy
    JOIN project_identity_adoption_allowed_mapping AS allowed
      ON allowed.session_id = ? AND allowed.project_slug = legacy.project_slug
    WHERE NOT EXISTS (
      SELECT 1 FROM project_action_events_v2 AS stable
      JOIN project_identity_catalog AS identity
        ON identity.generation_id = stable.catalog_generation_id
        AND identity.project_id_version = stable.project_id_version
        AND identity.project_id = stable.project_id
        AND identity.project_slug = stable.project_slug
      WHERE stable.device_id IS legacy.device_id
        AND stable.idempotency_key IS legacy.idempotency_key
        AND stable.action IS legacy.action AND stable.occurred_at IS legacy.occurred_at
        AND stable.project_id = allowed.project_id
    )
  )`, sessionId));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM feedback AS legacy
    JOIN project_identity_adoption_allowed_mapping AS allowed
      ON allowed.session_id = ? AND allowed.project_slug = legacy.project_slug
    WHERE NOT EXISTS (
      SELECT 1 FROM feedback_v2 AS stable
      JOIN project_identity_catalog AS identity
        ON identity.generation_id = stable.catalog_generation_id
        AND identity.project_id_version = stable.project_id_version
        AND identity.project_id = stable.project_id
        AND identity.project_slug = stable.project_slug
      WHERE stable.device_id IS legacy.device_id AND stable.project_id = allowed.project_id
        AND ${feedbackFactsMatchSql("stable", "legacy")}
    )
  )`, sessionId));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM decision_events AS legacy
    JOIN project_identity_adoption_allowed_mapping AS allowed
      ON allowed.session_id = ? AND allowed.project_slug = legacy.project_slug
    WHERE NOT EXISTS (
      SELECT 1 FROM decision_events_v2 AS stable
      JOIN project_identity_catalog AS identity
        ON identity.generation_id = stable.catalog_generation_id
        AND identity.project_id_version = stable.project_id_version
        AND identity.project_id = stable.project_id
        AND identity.project_slug = stable.project_slug
      WHERE stable.legacy_event_id = legacy.id AND stable.device_id IS legacy.device_id
        AND stable.project_id = allowed.project_id AND stable.value IS legacy.value
        AND stable.occurred_at IS legacy.created_at
    )
  )`, sessionId));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_action_events_v2 AS event
    WHERE NOT EXISTS (
      SELECT 1 FROM project_action_state_v2 AS state
      WHERE state.device_id = event.device_id AND state.project_id = event.project_id
    )
  )`));
  for (const fact of plan.quarantineFacts) {
    statements.push(migrationGuard(database, `NOT EXISTS (
      SELECT 1 FROM project_identity_unresolved_legacy
      WHERE source_table = ? AND source_key = ? AND project_slug = ?
        AND disposition = 'quarantine' AND reason_code = ? AND policy_version = ?
    )`, fact.sourceTable, fact.sourceKey, fact.projectSlug,
    fact.reasonCode, fact.policyVersion));
  }
}

function adoptionSessionId(catalog) {
  const suffix = globalThis.crypto?.randomUUID?.()
    ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${catalog.generationId}:${suffix}`;
}

function adoptionStatements(database, catalog, plan, sessionId) {
  const statements = [];
  for (const evidence of catalog.generations) {
    statements.push(database.prepare(`INSERT INTO project_identity_generation_evidence (
      generation_id, generation_created_at, manifest_sha256, catalog_schema_version
    ) VALUES (?, ?, ?, ?) ON CONFLICT (generation_id) DO NOTHING`).bind(
      evidence.generationId, evidence.generationCreatedAt,
      evidence.manifestSha256, evidence.catalogSchemaVersion,
    ));
  }
  for (const project of catalog.mappings) {
    statements.push(database.prepare(`INSERT INTO project_identity_catalog (
      generation_id, project_id_version, project_id, canonical_repository, project_slug
    ) VALUES (?, ?, ?, ?, ?) ON CONFLICT (generation_id, project_id) DO NOTHING`)
      .bind(project.generationId, project.projectIdVersion, project.projectId,
        project.canonicalRepository, project.projectSlug));
  }
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_identity_adoption_session
  ) OR EXISTS (SELECT 1 FROM project_identity_adoption_allowed_mapping)`));
  statements.push(database.prepare(`INSERT INTO project_identity_adoption_session (
    singleton, session_id, active_generation_id, policy_version, created_at
  ) VALUES (1, ?, ?, ?, ?)`).bind(
    sessionId, catalog.generationId, catalog.policy.policyVersion, new Date().toISOString(),
  ));
  for (const mapping of plan.resolutionBySlug.values()) {
    statements.push(database.prepare(`INSERT INTO project_identity_adoption_allowed_mapping (
      session_id, project_slug, generation_id, project_id_version, project_id
    ) VALUES (?, ?, ?, ?, ?)`).bind(
      sessionId, mapping.projectSlug, mapping.generationId,
      mapping.projectIdVersion, mapping.projectId,
    ));
  }
  for (const fact of plan.quarantineFacts) {
    if (fact.existing) continue;
    statements.push(database.prepare(`INSERT INTO project_identity_unresolved_legacy (
      source_table, source_key, project_slug, disposition, reason_code,
      policy_version, first_seen_generation_id
    ) VALUES (?, ?, ?, 'quarantine', ?, ?, ?)
    ON CONFLICT (source_table, source_key) DO NOTHING`).bind(
      fact.sourceTable, fact.sourceKey, fact.projectSlug, fact.reasonCode,
      fact.policyVersion, catalog.generationId,
    ));
  }
  addInBatchAdoptionGuards(statements, database, catalog, plan, sessionId);
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
  ) SELECT legacy.device_id, allowed.project_id_version, allowed.project_id,
    legacy.project_slug, allowed.generation_id, legacy.action, legacy.occurred_at,
    legacy.idempotency_key FROM project_action_events AS legacy
    JOIN project_identity_adoption_allowed_mapping AS allowed
      ON allowed.session_id = ? AND allowed.project_slug = legacy.project_slug
    WHERE NOT EXISTS (
      SELECT 1 FROM project_action_events_v2 AS stable
      WHERE stable.device_id = legacy.device_id
        AND stable.idempotency_key = legacy.idempotency_key
    ) ORDER BY julianday(legacy.occurred_at), legacy.id`).bind(sessionId));
  statements.push(database.prepare(`INSERT INTO feedback_v2 (
    device_id, project_id_version, project_id, project_slug,
    catalog_generation_id, value, created_at, updated_at
  ) SELECT legacy.device_id, allowed.project_id_version, allowed.project_id,
    legacy.project_slug, allowed.generation_id, legacy.value,
    legacy.created_at, legacy.updated_at FROM feedback AS legacy
    JOIN project_identity_adoption_allowed_mapping AS allowed
      ON allowed.session_id = ? AND allowed.project_slug = legacy.project_slug
    WHERE NOT EXISTS (
      SELECT 1 FROM feedback_v2 AS stable
      WHERE stable.device_id = legacy.device_id
        AND stable.project_id = allowed.project_id
    )`).bind(sessionId));
  statements.push(database.prepare(`INSERT INTO decision_events_v2 (
    legacy_event_id, device_id, project_id_version, project_id, project_slug,
    catalog_generation_id, value, occurred_at
  ) SELECT legacy.id, legacy.device_id, allowed.project_id_version,
    allowed.project_id, legacy.project_slug, allowed.generation_id,
    legacy.value, legacy.created_at FROM decision_events AS legacy
    JOIN project_identity_adoption_allowed_mapping AS allowed
      ON allowed.session_id = ? AND allowed.project_slug = legacy.project_slug
    WHERE NOT EXISTS (
      SELECT 1 FROM decision_events_v2 AS stable
      WHERE stable.legacy_event_id = legacy.id
    ) ORDER BY legacy.id`).bind(sessionId));
  addPostAdoptionGuards(statements, database, plan, sessionId);
  statements.push(database.prepare(`DELETE FROM project_identity_adoption_allowed_mapping
    WHERE session_id = ?`).bind(sessionId));
  statements.push(database.prepare(`DELETE FROM project_identity_adoption_session
    WHERE singleton = 1 AND session_id = ?`).bind(sessionId));
  statements.push(migrationGuard(database, `EXISTS (
    SELECT 1 FROM project_identity_adoption_session
  ) OR EXISTS (SELECT 1 FROM project_identity_adoption_allowed_mapping)`));
  return statements;
}

export async function adoptStableProjectIdentities(database, context, policy) {
  const catalog = normalizeAdoptionContext(context, policy);
  const plan = await preflightCatalogAdoption(database, catalog);
  const sessionId = adoptionSessionId(catalog);
  try {
    await database.batch(adoptionStatements(database, catalog, plan, sessionId));
  } catch (error) {
    try {
      await preflightCatalogAdoption(database, catalog);
    } catch (preflightError) {
      throw preflightError;
    }
    if (/constraint|unique|guard|immutable|projection|stale|adoption/i.test(String(error?.message ?? error))) {
      throw new StableProjectDecisionError(
        "concurrent_project_identity_adoption_conflict",
        "identity adoption lost a concurrent write race; no rows were adopted",
        { cause: String(error?.message ?? error) },
      );
    }
    throw error;
  }
  return {
    status: plan.quarantinedFactCount ? "ready_with_quarantine" : "ready",
    generationId: catalog.generationId,
    projectCount: catalog.projects.length,
    migratedProjectCount: plan.migratedProjectCount,
    migratedFactCount: plan.migratedFactCount,
    quarantinedSlugCount: plan.quarantinedSlugCount,
    quarantinedFactCount: plan.quarantinedFactCount,
    unresolvedBlockingCount: 0,
  };
}
