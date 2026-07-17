export const LEGACY_PROJECT_IDENTITY_POLICY_SCHEMA_VERSION: 1;
export const LEGACY_PROJECT_IDENTITY_DISPOSITION: "quarantine";
export const LEGACY_PROJECT_IDENTITY_REASON:
  "no_verified_repository_in_current_or_retained_catalogs";
export const LEGACY_PROJECT_SOURCE_TABLES: readonly [
  "feedback",
  "decision_events",
];

export type LegacyProjectSourceTable = (typeof LEGACY_PROJECT_SOURCE_TABLES)[number];

export type LegacyProjectIdentityDisposition = {
  readonly projectSlug: string;
  readonly disposition: "quarantine";
  readonly reasonCode: "no_verified_repository_in_current_or_retained_catalogs";
  readonly sourceTables: readonly LegacyProjectSourceTable[];
};

export type LegacyProjectIdentityPolicy = {
  readonly schemaVersion: 1;
  readonly policyVersion: string;
  readonly entries: readonly LegacyProjectIdentityDisposition[];
};

export class LegacyProjectIdentityPolicyError extends Error {
  readonly code: "invalid_legacy_project_identity_policy";
}

export function validateLegacyProjectIdentityPolicy(
  value: unknown,
): LegacyProjectIdentityPolicy;
export function parseLegacyProjectIdentityPolicy(
  value: unknown,
): LegacyProjectIdentityPolicy;
