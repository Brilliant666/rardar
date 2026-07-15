import { sql } from "drizzle-orm";
import { check, index, integer, primaryKey, sqliteTable, text, uniqueIndex } from "drizzle-orm/sqlite-core";

export const feedback = sqliteTable(
  "feedback",
  {
    id: integer("id").primaryKey({ autoIncrement: true }),
    deviceId: text("device_id").notNull(),
    projectSlug: text("project_slug").notNull(),
    value: text("value").notNull(),
    createdAt: text("created_at").notNull().default(sql`CURRENT_TIMESTAMP`),
    updatedAt: text("updated_at").notNull().default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [uniqueIndex("feedback_device_project_idx").on(table.deviceId, table.projectSlug)],
);

export const decisionEvents = sqliteTable(
  "decision_events",
  {
    id: integer("id").primaryKey({ autoIncrement: true }),
    deviceId: text("device_id").notNull(),
    projectSlug: text("project_slug").notNull(),
    value: text("value").notNull(),
    createdAt: text("created_at").notNull().default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [index("decision_events_device_created_idx").on(table.deviceId, table.createdAt)],
);

export const projectActions = sqliteTable(
  "project_actions",
  {
    id: integer("id").primaryKey({ autoIncrement: true }),
    deviceId: text("device_id").notNull(),
    projectSlug: text("project_slug").notNull(),
    action: text("action").notNull(),
    createdAt: text("created_at").notNull().default(sql`CURRENT_TIMESTAMP`),
  },
  (table) => [
    uniqueIndex("project_actions_device_project_action_idx").on(table.deviceId, table.projectSlug, table.action),
    index("project_actions_device_created_idx").on(table.deviceId, table.createdAt),
  ],
);

export const projectActionEvents = sqliteTable(
  "project_action_events",
  {
    id: integer("id").primaryKey({ autoIncrement: true }),
    deviceId: text("device_id").notNull(),
    projectSlug: text("project_slug").notNull(),
    action: text("action").notNull(),
    occurredAt: text("occurred_at").notNull().default(sql`(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))`),
    idempotencyKey: text("idempotency_key").notNull(),
  },
  (table) => [
    check("project_action_events_action_check", sql`${table.action} IN ('opened', 'saved', 'tried', 'cloned', 'reused')`),
    check("project_action_events_time_check", sql`julianday(${table.occurredAt}) IS NOT NULL`),
    uniqueIndex("project_action_events_device_idempotency_idx").on(table.deviceId, table.idempotencyKey),
    index("project_action_events_device_occurred_idx").on(table.deviceId, table.occurredAt),
    index("project_action_events_device_project_occurred_idx").on(
      table.deviceId,
      table.projectSlug,
      table.occurredAt,
    ),
  ],
);

export const projectActionState = sqliteTable(
  "project_action_state",
  {
    deviceId: text("device_id").notNull(),
    projectSlug: text("project_slug").notNull(),
    highestStage: text("highest_stage").notNull(),
    openedAt: text("opened_at"),
    savedAt: text("saved_at"),
    triedAt: text("tried_at"),
    clonedAt: text("cloned_at"),
    reusedAt: text("reused_at"),
    updatedAt: text("updated_at").notNull(),
  },
  (table) => [
    primaryKey({
      columns: [table.deviceId, table.projectSlug],
      name: "project_action_state_device_project_pk",
    }),
    check("project_action_state_stage_check", sql`${table.highestStage} IN ('opened', 'saved', 'tried', 'cloned', 'reused')`),
    index("project_action_state_device_updated_idx").on(table.deviceId, table.updatedAt),
  ],
);

export const projectIdentityCatalog = sqliteTable(
  "project_identity_catalog",
  {
    generationId: text("generation_id").notNull(),
    projectIdVersion: integer("project_id_version").notNull(),
    projectId: text("project_id").notNull(),
    canonicalRepository: text("canonical_repository").notNull(),
    projectSlug: text("project_slug").notNull(),
  },
  (table) => [
    primaryKey({
      columns: [table.generationId, table.projectId],
      name: "project_identity_catalog_generation_project_pk",
    }),
    uniqueIndex("project_identity_catalog_generation_repository_idx").on(
      table.generationId,
      table.canonicalRepository,
    ),
    index("project_identity_catalog_generation_slug_idx").on(table.generationId, table.projectSlug),
    check("project_identity_catalog_version_check", sql`${table.projectIdVersion} = 1`),
  ],
);

export const projectIdentityRuntime = sqliteTable(
  "project_identity_runtime",
  {
    singleton: integer("singleton").primaryKey(),
    generationId: text("generation_id").notNull(),
    publishedAt: text("published_at").notNull(),
    publishedAtMicros: integer("published_at_micros").notNull(),
  },
  (table) => [
    check("project_identity_runtime_singleton_check", sql`${table.singleton} = 1`),
    check(
      "project_identity_runtime_published_time_check",
      sql`julianday(${table.publishedAt}) IS NOT NULL`,
    ),
  ],
);

export const projectIdentityMigrationGuard = sqliteTable(
  "project_identity_migration_guard",
  { failure: integer("failure").notNull() },
  (table) => [check("project_identity_migration_guard_check", sql`${table.failure} = 0`)],
);

export const projectActionEventsV2 = sqliteTable(
  "project_action_events_v2",
  {
    id: integer("id").primaryKey({ autoIncrement: true }),
    deviceId: text("device_id").notNull(),
    projectIdVersion: integer("project_id_version").notNull(),
    projectId: text("project_id").notNull(),
    projectSlug: text("project_slug").notNull(),
    catalogGenerationId: text("catalog_generation_id").notNull(),
    action: text("action").notNull(),
    occurredAt: text("occurred_at").notNull().default(sql`(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))`),
    idempotencyKey: text("idempotency_key").notNull(),
  },
  (table) => [
    check("project_action_events_v2_version_check", sql`${table.projectIdVersion} = 1`),
    check("project_action_events_v2_action_check", sql`${table.action} IN ('opened', 'saved', 'tried', 'cloned', 'reused')`),
    check("project_action_events_v2_time_check", sql`julianday(${table.occurredAt}) IS NOT NULL`),
    uniqueIndex("project_action_events_v2_device_idempotency_idx").on(table.deviceId, table.idempotencyKey),
    index("project_action_events_v2_device_occurred_idx").on(table.deviceId, table.occurredAt),
    index("project_action_events_v2_device_project_occurred_idx").on(
      table.deviceId,
      table.projectId,
      table.occurredAt,
    ),
  ],
);

export const projectActionStateV2 = sqliteTable(
  "project_action_state_v2",
  {
    deviceId: text("device_id").notNull(),
    projectIdVersion: integer("project_id_version").notNull(),
    projectId: text("project_id").notNull(),
    projectSlug: text("project_slug").notNull(),
    catalogGenerationId: text("catalog_generation_id").notNull(),
    highestStage: text("highest_stage").notNull(),
    openedAt: text("opened_at"),
    savedAt: text("saved_at"),
    triedAt: text("tried_at"),
    clonedAt: text("cloned_at"),
    reusedAt: text("reused_at"),
    updatedAt: text("updated_at").notNull(),
  },
  (table) => [
    primaryKey({
      columns: [table.deviceId, table.projectId],
      name: "project_action_state_v2_device_project_pk",
    }),
    check("project_action_state_v2_version_check", sql`${table.projectIdVersion} = 1`),
    check("project_action_state_v2_stage_check", sql`${table.highestStage} IN ('opened', 'saved', 'tried', 'cloned', 'reused')`),
    index("project_action_state_v2_device_updated_idx").on(table.deviceId, table.updatedAt),
  ],
);

export const feedbackV2 = sqliteTable(
  "feedback_v2",
  {
    deviceId: text("device_id").notNull(),
    projectIdVersion: integer("project_id_version").notNull(),
    projectId: text("project_id").notNull(),
    projectSlug: text("project_slug").notNull(),
    catalogGenerationId: text("catalog_generation_id").notNull(),
    value: text("value").notNull(),
    createdAt: text("created_at").notNull(),
    updatedAt: text("updated_at").notNull(),
  },
  (table) => [
    primaryKey({ columns: [table.deviceId, table.projectId], name: "feedback_v2_device_project_pk" }),
    check("feedback_v2_version_check", sql`${table.projectIdVersion} = 1`),
    check("feedback_v2_value_check", sql`${table.value} IN ('有用', '无用', '复用', '待确定')`),
    check("feedback_v2_created_time_check", sql`julianday(${table.createdAt}) IS NOT NULL`),
    check("feedback_v2_updated_time_check", sql`julianday(${table.updatedAt}) IS NOT NULL`),
    index("feedback_v2_device_updated_idx").on(table.deviceId, table.updatedAt),
  ],
);

export const decisionEventsV2 = sqliteTable(
  "decision_events_v2",
  {
    id: integer("id").primaryKey({ autoIncrement: true }),
    legacyEventId: integer("legacy_event_id").notNull(),
    deviceId: text("device_id").notNull(),
    projectIdVersion: integer("project_id_version").notNull(),
    projectId: text("project_id").notNull(),
    projectSlug: text("project_slug").notNull(),
    catalogGenerationId: text("catalog_generation_id").notNull(),
    value: text("value").notNull(),
    occurredAt: text("occurred_at").notNull(),
  },
  (table) => [
    uniqueIndex("decision_events_v2_legacy_event_idx").on(table.legacyEventId),
    check("decision_events_v2_version_check", sql`${table.projectIdVersion} = 1`),
    check("decision_events_v2_value_check", sql`${table.value} IN ('有用', '无用', '复用', '待确定')`),
    check("decision_events_v2_time_check", sql`julianday(${table.occurredAt}) IS NOT NULL`),
    index("decision_events_v2_device_occurred_idx").on(table.deviceId, table.occurredAt),
    index("decision_events_v2_device_project_occurred_idx").on(
      table.deviceId,
      table.projectId,
      table.occurredAt,
    ),
  ],
);
