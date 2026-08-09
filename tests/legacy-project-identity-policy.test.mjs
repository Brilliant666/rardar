import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  LegacyProjectIdentityPolicyError,
  parseLegacyProjectIdentityPolicy,
  validateLegacyProjectIdentityPolicy,
} from "../db/legacy-project-identity-policy.mjs";

const policyUrl = new URL(
  "../contracts/legacy-project-identity-dispositions.json",
  import.meta.url,
);

async function formalPolicy() {
  return JSON.parse(await readFile(policyUrl, "utf8"));
}

function rejectedPolicy(value) {
  assert.throws(
    () => validateLegacyProjectIdentityPolicy(value),
    (error) => error instanceof LegacyProjectIdentityPolicyError
      && error.code === "invalid_legacy_project_identity_policy",
  );
}

test("accepts the exact versioned officecli quarantine policy", async () => {
  const source = await formalPolicy();
  const parsed = validateLegacyProjectIdentityPolicy(source);
  assert.deepEqual(parsed, {
    schemaVersion: 1,
    policyVersion: "2026-07-18.1",
    entries: [{
      projectSlug: "officecli",
      disposition: "quarantine",
      reasonCode: "no_verified_repository_in_current_or_retained_catalogs",
      sourceTables: ["feedback"],
    }],
  });
  assert.ok(Object.isFrozen(parsed));
  assert.ok(Object.isFrozen(parsed.entries));
  assert.ok(Object.isFrozen(parsed.entries[0]));
  assert.ok(Object.isFrozen(parsed.entries[0].sourceTables));
  assert.deepEqual(parseLegacyProjectIdentityPolicy(JSON.stringify(source)), parsed);
});

test("rejects wildcard, unknown fields, duplicate slugs and duplicate tables", async () => {
  const source = await formalPolicy();
  rejectedPolicy({
    ...source,
    entries: [{ ...source.entries[0], projectSlug: "office*" }],
  });
  rejectedPolicy({ ...source, repository: "guessed/repository" });
  rejectedPolicy({
    ...source,
    entries: [{ ...source.entries[0], projectId: "guessed--00000000000000000000" }],
  });
  rejectedPolicy({
    ...source,
    entries: [
      source.entries[0],
      { ...source.entries[0], sourceTables: ["decision_events"] },
    ],
  });
  rejectedPolicy({
    ...source,
    entries: [{ ...source.entries[0], sourceTables: ["feedback", "feedback"] }],
  });
});

test("rejects stable or unknown source-table scope and all device fields", async () => {
  const source = await formalPolicy();
  for (const sourceTable of [
    "project_action_events",
    "feedback_v2",
    "project_identity_catalog",
    "*",
  ]) {
    rejectedPolicy({
      ...source,
      entries: [{ ...source.entries[0], sourceTables: [sourceTable] }],
    });
  }
  rejectedPolicy({ ...source, deviceId: "sensitive-device" });
  rejectedPolicy({
    ...source,
    entries: [{ ...source.entries[0], device_id: "sensitive-device" }],
  });
  assert.doesNotMatch(JSON.stringify(source), /device_?id/i);
});

test("rejects malformed JSON and invalid policy versions", async () => {
  const source = await formalPolicy();
  assert.throws(
    () => parseLegacyProjectIdentityPolicy("{not-json"),
    (error) => error instanceof LegacyProjectIdentityPolicyError,
  );
  rejectedPolicy({ ...source, policyVersion: "latest" });
  rejectedPolicy({ ...source, schemaVersion: 2 });
});
