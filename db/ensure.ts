import { env } from "cloudflare:workers";

let schemaReady: Promise<void> | null = null;

export function ensureDecisionSchema() {
  if (schemaReady) return schemaReady;
  schemaReady = env.DB.batch([
    env.DB.prepare(`
      CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        project_slug TEXT NOT NULL,
        value TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      )
    `),
    env.DB.prepare(`
      CREATE UNIQUE INDEX IF NOT EXISTS feedback_device_project_idx
      ON feedback (device_id, project_slug)
    `),
    env.DB.prepare(`
      CREATE TABLE IF NOT EXISTS decision_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        project_slug TEXT NOT NULL,
        value TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      )
    `),
    env.DB.prepare(`
      CREATE INDEX IF NOT EXISTS decision_events_device_created_idx
      ON decision_events (device_id, created_at)
    `),
    env.DB.prepare(`
      CREATE TABLE IF NOT EXISTS project_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        project_slug TEXT NOT NULL,
        action TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      )
    `),
    env.DB.prepare(`
      CREATE UNIQUE INDEX IF NOT EXISTS project_actions_device_project_action_idx
      ON project_actions (device_id, project_slug, action)
    `),
    env.DB.prepare(`
      CREATE INDEX IF NOT EXISTS project_actions_device_created_idx
      ON project_actions (device_id, created_at)
    `),
  ])
    .then(() => undefined)
    .catch((error) => {
      schemaReady = null;
      throw error;
    });
  return schemaReady;
}
