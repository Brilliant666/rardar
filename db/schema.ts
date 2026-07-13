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
