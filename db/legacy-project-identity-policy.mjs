export const LEGACY_PROJECT_IDENTITY_POLICY_SCHEMA_VERSION = 1;
export const LEGACY_PROJECT_IDENTITY_DISPOSITION = "quarantine";
export const LEGACY_PROJECT_IDENTITY_REASON =
  "no_verified_repository_in_current_or_retained_catalogs";
export const LEGACY_PROJECT_SOURCE_TABLES = Object.freeze([
  "feedback",
  "decision_events",
]);

const POLICY_VERSION_PATTERN = /^\d{4}-\d{2}-\d{2}\.[1-9][0-9]*$/;
const PROJECT_SLUG_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/;
const WILDCARD_PATTERN = /[*?\[\]{}]/;
const TOP_LEVEL_KEYS = Object.freeze(["schemaVersion", "policyVersion", "entries"]);
const ENTRY_KEYS = Object.freeze([
  "projectSlug",
  "disposition",
  "reasonCode",
  "sourceTables",
]);
const SOURCE_TABLE_SET = new Set(LEGACY_PROJECT_SOURCE_TABLES);

export class LegacyProjectIdentityPolicyError extends Error {
  constructor(message) {
    super(message);
    this.name = "LegacyProjectIdentityPolicyError";
    this.code = "invalid_legacy_project_identity_policy";
  }
}

function fail(message) {
  throw new LegacyProjectIdentityPolicyError(message);
}

function isRecord(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function requireExactKeys(value, expected, label) {
  const actual = Object.keys(value).sort();
  const required = [...expected].sort();
  if (actual.length !== required.length || actual.some((key, index) => key !== required[index])) {
    fail(`${label} has missing or unknown fields`);
  }
}

function validateEntry(value, index) {
  if (!isRecord(value)) fail(`policy entry ${index} must be an object`);
  requireExactKeys(value, ENTRY_KEYS, `policy entry ${index}`);
  if (
    typeof value.projectSlug !== "string"
    || !PROJECT_SLUG_PATTERN.test(value.projectSlug)
    || WILDCARD_PATTERN.test(value.projectSlug)
  ) {
    fail(`policy entry ${index} has an invalid exact projectSlug`);
  }
  if (value.disposition !== LEGACY_PROJECT_IDENTITY_DISPOSITION) {
    fail(`policy entry ${index} has an unsupported disposition`);
  }
  if (value.reasonCode !== LEGACY_PROJECT_IDENTITY_REASON) {
    fail(`policy entry ${index} has an unsupported reasonCode`);
  }
  if (!Array.isArray(value.sourceTables) || value.sourceTables.length === 0) {
    fail(`policy entry ${index} must name at least one source table`);
  }
  const seenTables = new Set();
  const sourceTables = value.sourceTables.map((sourceTable) => {
    if (typeof sourceTable !== "string" || !SOURCE_TABLE_SET.has(sourceTable)) {
      fail(`policy entry ${index} names an invalid source table`);
    }
    if (seenTables.has(sourceTable)) {
      fail(`policy entry ${index} repeats a source table`);
    }
    seenTables.add(sourceTable);
    return sourceTable;
  });
  return Object.freeze({
    projectSlug: value.projectSlug,
    disposition: value.disposition,
    reasonCode: value.reasonCode,
    sourceTables: Object.freeze(sourceTables),
  });
}

export function validateLegacyProjectIdentityPolicy(value) {
  if (!isRecord(value)) fail("legacy project identity policy must be an object");
  requireExactKeys(value, TOP_LEVEL_KEYS, "legacy project identity policy");
  if (value.schemaVersion !== LEGACY_PROJECT_IDENTITY_POLICY_SCHEMA_VERSION) {
    fail("legacy project identity policy has an unsupported schemaVersion");
  }
  if (typeof value.policyVersion !== "string" || !POLICY_VERSION_PATTERN.test(value.policyVersion)) {
    fail("legacy project identity policy has an invalid policyVersion");
  }
  if (!Array.isArray(value.entries) || value.entries.length === 0 || value.entries.length > 128) {
    fail("legacy project identity policy must contain between 1 and 128 entries");
  }
  const seenSlugs = new Set();
  const entries = value.entries.map((entry, index) => {
    const parsed = validateEntry(entry, index);
    if (seenSlugs.has(parsed.projectSlug)) {
      fail("legacy project identity policy repeats a projectSlug");
    }
    seenSlugs.add(parsed.projectSlug);
    return parsed;
  });
  return Object.freeze({
    schemaVersion: LEGACY_PROJECT_IDENTITY_POLICY_SCHEMA_VERSION,
    policyVersion: value.policyVersion,
    entries: Object.freeze(entries),
  });
}

export function parseLegacyProjectIdentityPolicy(value) {
  if (typeof value !== "string") return validateLegacyProjectIdentityPolicy(value);
  let parsed;
  try {
    parsed = JSON.parse(value);
  } catch {
    fail("legacy project identity policy is not valid JSON");
  }
  return validateLegacyProjectIdentityPolicy(parsed);
}
