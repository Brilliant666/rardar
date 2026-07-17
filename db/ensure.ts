import { env } from "cloudflare:workers";
import { loadHistoricalIdentityBundleFromBridge } from "../app/published-data-client";
import legacyProjectIdentityPolicySource from "../contracts/legacy-project-identity-dispositions.json?raw";
import stableProjectIdentityMigration from "../drizzle/0004_stable_project_identity.sql?raw";
import { parseLegacyProjectIdentityPolicy } from "./legacy-project-identity-policy.mjs";
import { prepareProjectActionSchema } from "./project-actions.mjs";
import {
  adoptStableProjectIdentities,
  type ProjectIdentityCatalog,
} from "./stable-project-decisions.mjs";

let schemaReady: Promise<void> | null = null;
let adoptedIdentityKey: string | null = null;
let identityAdoptionTail: Promise<void> = Promise.resolve();
const legacyProjectIdentityPolicy = parseLegacyProjectIdentityPolicy(
  legacyProjectIdentityPolicySource,
);

function identityCatalogKey(identityCatalog: ProjectIdentityCatalog) {
  return JSON.stringify(identityCatalog);
}

function prepareStableProjectMigration() {
  return stableProjectIdentityMigration
    .split("--> statement-breakpoint")
    .map((statement) => statement.trim())
    .filter(Boolean)
    .map((statement) => env.DB.prepare(statement));
}

function ensureBaseSchema() {
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
    ...prepareProjectActionSchema(env.DB),
    ...prepareStableProjectMigration(),
  ])
    .then(() => undefined)
    .catch((error) => {
      schemaReady = null;
      adoptedIdentityKey = null;
      throw error;
    });
  return schemaReady;
}

export async function ensureDecisionSchema(identityCatalog?: ProjectIdentityCatalog) {
  await ensureBaseSchema();
  if (!identityCatalog) return;

  const key = identityCatalogKey(identityCatalog);
  const adoption = identityAdoptionTail.then(async () => {
    if (adoptedIdentityKey === key) return;
    const historicalIdentityBundle = await loadHistoricalIdentityBundleFromBridge();
    if (
      historicalIdentityBundle.activeGenerationId !== identityCatalog.generationId
      || historicalIdentityBundle.activePublishedAt !== identityCatalog.publishedAt
    ) {
      throw new Error(
        "historical identity bundle does not match the request generation pointer",
      );
    }
    await adoptStableProjectIdentities(
      env.DB,
      historicalIdentityBundle,
      legacyProjectIdentityPolicy,
    );
    adoptedIdentityKey = key;
  });
  identityAdoptionTail = adoption.catch(() => undefined);
  return adoption;
}
