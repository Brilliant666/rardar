import { createHash } from "node:crypto";
import {
  lstatSync,
  readFileSync,
  realpathSync,
} from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";

const GENERATION_ID = /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$/;
const SHA256 = /^[a-f0-9]{64}$/;
const RFC3339_WITH_TIMEZONE =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

const REQUIRED_ARTIFACTS = Object.freeze({
  catalog: "catalog/latest.json",
  signals: "signals/latest.json",
  signalEnrichment: "signals/enrichment.json",
  codexQueue: "queues/codex.json",
});

function fail(message) {
  throw new Error(`published generation is unavailable: ${message}`);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function readJsonBuffer(buffer, source) {
  let payload;
  try {
    payload = JSON.parse(buffer.toString("utf8"));
  } catch (error) {
    fail(`invalid JSON at ${source}: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    fail(`${source} must contain a JSON object`);
  }
  return payload;
}

function requireRegularFile(path, label) {
  let status;
  try {
    status = lstatSync(path);
  } catch (error) {
    fail(`${label} cannot be read: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (status.isSymbolicLink() || !status.isFile()) {
    fail(`${label} must be a regular file and cannot be a symbolic link`);
  }
}

function requireDirectory(path, label) {
  let status;
  try {
    status = lstatSync(path);
  } catch (error) {
    fail(`${label} cannot be read: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (status.isSymbolicLink() || !status.isDirectory()) {
    fail(`${label} must be a real directory and cannot be a symbolic link`);
  }
}

function isWithin(parent, child) {
  const pathFromParent = relative(parent, child);
  return pathFromParent === "" || (
    pathFromParent !== ".." &&
    !pathFromParent.startsWith(`..${sep}`) &&
    !isAbsolute(pathFromParent)
  );
}

function requireGenerationId(value, field) {
  if (typeof value !== "string" || !GENERATION_ID.test(value)) {
    fail(`${field} is not a safe generation id`);
  }
  return value;
}

function requireTimestamp(value, field) {
  if (
    typeof value !== "string" ||
    !RFC3339_WITH_TIMEZONE.test(value) ||
    !Number.isFinite(Date.parse(value))
  ) {
    fail(`${field} must be a timezone-aware RFC3339 timestamp`);
  }
  return value;
}

function requireSafeArtifactPath(value) {
  if (
    typeof value !== "string" ||
    value.length < 6 ||
    value.length > 512 ||
    value.includes("\\") ||
    value.startsWith("/") ||
    !/^[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*\.json$/.test(value)
  ) {
    fail(`unsafe artifact path ${JSON.stringify(value)}`);
  }
  const parts = value.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) {
    fail(`unsafe artifact path ${JSON.stringify(value)}`);
  }
  return value;
}

function requireExactKeys(payload, required, source) {
  const actual = Object.keys(payload).sort();
  const expected = [...required].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    fail(`${source} has unexpected or missing fields`);
  }
}

function validatePointer(pointer) {
  requireExactKeys(
    pointer,
    ["schemaVersion", "generationId", "publishedAt", "previousGenerationId", "manifestSha256"],
    "current.json",
  );
  if (pointer.schemaVersion !== 1) fail("current.json uses an unsupported schema version");
  requireGenerationId(pointer.generationId, "current.json generationId");
  if (pointer.previousGenerationId !== null) {
    requireGenerationId(pointer.previousGenerationId, "current.json previousGenerationId");
  }
  requireTimestamp(pointer.publishedAt, "current.json publishedAt");
  if (typeof pointer.manifestSha256 !== "string" || !SHA256.test(pointer.manifestSha256)) {
    fail("current.json manifestSha256 must be a lowercase SHA-256 digest");
  }
}

function validateManifest(manifest, pointer, generationId) {
  requireExactKeys(
    manifest,
    [
      "schemaVersion",
      "generationId",
      "createdAt",
      "baseGenerationId",
      "operation",
      "state",
      "failureStage",
      "error",
      "artifacts",
      "hashes",
      "audit",
    ],
    "manifest.json",
  );
  if (manifest.schemaVersion !== 1) fail("manifest.json uses an unsupported schema version");
  if (manifest.generationId !== generationId || manifest.generationId !== pointer.generationId) {
    fail("manifest generationId does not match current.json and its directory");
  }
  requireTimestamp(manifest.createdAt, "manifest.json createdAt");
  if (manifest.baseGenerationId !== null) {
    requireGenerationId(manifest.baseGenerationId, "manifest.json baseGenerationId");
  }
  if (!new Set(["bootstrap", "refresh", "derive"]).has(manifest.operation)) {
    fail("manifest.json operation is unsupported");
  }
  if (manifest.state !== "ready") fail("current.json may only reference a ready generation");
  if (manifest.failureStage !== null || manifest.error !== null) {
    fail("a ready generation cannot carry a failure stage or error");
  }
  if (!Array.isArray(manifest.artifacts) || manifest.artifacts.length === 0) {
    fail("manifest.json artifacts must be a non-empty array");
  }
  if (!manifest.hashes || typeof manifest.hashes !== "object" || Array.isArray(manifest.hashes)) {
    fail("manifest.json hashes must be an object");
  }
  if (!manifest.audit || typeof manifest.audit !== "object" || Array.isArray(manifest.audit)) {
    fail("a ready generation must include an audit result");
  }
  requireExactKeys(
    manifest.audit,
    ["status", "errorCount", "warningCount", "validatedCount"],
    "manifest.json audit",
  );
  if (
    !new Set(["healthy", "degraded"]).has(manifest.audit.status) ||
    manifest.audit.errorCount !== 0 ||
    !Number.isInteger(manifest.audit.warningCount) ||
    manifest.audit.warningCount < 0 ||
    !Number.isInteger(manifest.audit.validatedCount) ||
    manifest.audit.validatedCount < 1
  ) {
    fail("manifest.json audit does not describe a publishable generation");
  }

  const artifacts = manifest.artifacts.map(requireSafeArtifactPath);
  if (new Set(artifacts).size !== artifacts.length) {
    fail("manifest.json contains duplicate artifact paths");
  }
  const hashPaths = Object.keys(manifest.hashes);
  if (
    hashPaths.length !== artifacts.length ||
    hashPaths.some((path) => !artifacts.includes(path))
  ) {
    fail("manifest.json hashes must exactly match its artifact paths");
  }
  for (const path of artifacts) {
    if (typeof manifest.hashes[path] !== "string" || !SHA256.test(manifest.hashes[path])) {
      fail(`manifest.json has an invalid SHA-256 digest for ${path}`);
    }
  }
  for (const required of Object.values(REQUIRED_ARTIFACTS)) {
    if (!artifacts.includes(required)) fail(`manifest.json does not list required artifact ${required}`);
  }
  return artifacts;
}

function readVerifiedArtifacts(generationRoot, artifacts, hashes) {
  const buffers = new Map();
  for (const artifact of artifacts) {
    const artifactPath = resolve(generationRoot, ...artifact.split("/"));
    if (!isWithin(generationRoot, artifactPath)) fail(`artifact path escapes its generation: ${artifact}`);

    let cursor = generationRoot;
    for (const part of artifact.split("/")) {
      cursor = resolve(cursor, part);
      let status;
      try {
        status = lstatSync(cursor);
      } catch (error) {
        fail(`artifact ${artifact} cannot be read: ${error instanceof Error ? error.message : String(error)}`);
      }
      if (status.isSymbolicLink()) fail(`artifact ${artifact} crosses a symbolic link`);
    }
    requireRegularFile(artifactPath, `artifact ${artifact}`);
    const realArtifactPath = realpathSync(artifactPath);
    if (!isWithin(generationRoot, realArtifactPath)) fail(`artifact path escapes its generation: ${artifact}`);
    const buffer = readFileSync(realArtifactPath);
    if (sha256(buffer) !== hashes[artifact]) fail(`artifact hash mismatch for ${artifact}`);
    buffers.set(artifact, buffer);
  }
  return buffers;
}

function deepFreeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  Object.freeze(value);
  for (const child of Object.values(value)) deepFreeze(child);
  return value;
}

/**
 * Resolve current.json once and load every web-facing artifact from the same
 * immutable, hash-verified generation directory.
 */
export function loadPublishedBundle(dataDirectory = process.env.RARDAR_DATA_DIR || resolve(process.cwd(), "data")) {
  const dataRoot = resolve(String(dataDirectory));
  const generationsRoot = resolve(dataRoot, "generations");
  requireDirectory(dataRoot, "data directory");
  requireDirectory(generationsRoot, "generations directory");
  const realGenerationsRoot = realpathSync(generationsRoot);

  const pointerPath = resolve(dataRoot, "current.json");
  requireRegularFile(pointerPath, "current.json");
  const pointerBuffer = readFileSync(pointerPath);
  const pointer = readJsonBuffer(pointerBuffer, "current.json");
  validatePointer(pointer);

  const generationId = pointer.generationId;
  const generationRoot = resolve(realGenerationsRoot, generationId);
  if (!isWithin(realGenerationsRoot, generationRoot)) fail("generation path escapes data/generations");
  requireDirectory(generationRoot, `generation ${generationId}`);
  const realGenerationRoot = realpathSync(generationRoot);
  if (!isWithin(realGenerationsRoot, realGenerationRoot)) fail("generation directory escapes data/generations");
  if (realGenerationRoot !== generationRoot) fail("generation directory cannot be an alias or symbolic link");

  const manifestPath = resolve(realGenerationRoot, "manifest.json");
  requireRegularFile(manifestPath, "manifest.json");
  const manifestBuffer = readFileSync(manifestPath);
  if (sha256(manifestBuffer) !== pointer.manifestSha256) {
    fail("manifest.json hash does not match current.json");
  }
  const manifest = readJsonBuffer(manifestBuffer, "manifest.json");
  const artifacts = validateManifest(manifest, pointer, generationId);
  const buffers = readVerifiedArtifacts(realGenerationRoot, artifacts, manifest.hashes);

  const parseArtifact = (path) => readJsonBuffer(buffers.get(path), path);
  return deepFreeze({
    generationId,
    publishedAt: pointer.publishedAt,
    previousGenerationId: pointer.previousGenerationId,
    manifest,
    catalog: parseArtifact(REQUIRED_ARTIFACTS.catalog),
    signals: parseArtifact(REQUIRED_ARTIFACTS.signals),
    signalEnrichment: parseArtifact(REQUIRED_ARTIFACTS.signalEnrichment),
    codexQueue: parseArtifact(REQUIRED_ARTIFACTS.codexQueue),
  });
}

export { REQUIRED_ARTIFACTS };
