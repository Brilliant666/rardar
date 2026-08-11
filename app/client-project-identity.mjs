import { ProjectIdentityError, isProjectId } from "./project-identity.mjs";

function requireStableProjectIdentity(value) {
  if (
    !value
    || typeof value !== "object"
    || Array.isArray(value)
    || value.projectIdVersion !== 1
    || !isProjectId(value.projectId)
  ) {
    throw new ProjectIdentityError(
      "invalid_client_project_identity",
      "client project identity must use projectIdVersion 1 and a valid projectId",
      400,
    );
  }
  return Object.freeze({ projectIdVersion: 1, projectId: value.projectId });
}

export function stableProjectSelector(project) {
  return requireStableProjectIdentity(project);
}

export function isFreshClientRead(
  aborted,
  requestVersionAtStart,
  currentRequestVersion,
  mutationVersionAtStart,
  currentMutationVersion,
) {
  return !aborted
    && requestVersionAtStart === currentRequestVersion
    && mutationVersionAtStart === currentMutationVersion;
}

export function resultForGeneration(expectedGenerationId, result) {
  if (
    typeof expectedGenerationId !== "string"
    || !expectedGenerationId
    || !result
    || typeof result !== "object"
    || Array.isArray(result)
    || result.generationId !== expectedGenerationId
  ) {
    return null;
  }
  return result;
}

export function canonicalProjectPath(project) {
  const identity = requireStableProjectIdentity(project);
  return `/project/v1/${encodeURIComponent(identity.projectId)}`;
}

export function indexStableProjectsById(projects) {
  if (!Array.isArray(projects)) {
    throw new ProjectIdentityError(
      "invalid_client_project_collection",
      "client project collection must be an array",
      400,
    );
  }
  const byProjectId = new Map();
  for (const project of projects) {
    const identity = requireStableProjectIdentity(project);
    if (byProjectId.has(identity.projectId)) {
      throw new ProjectIdentityError(
        "duplicate_client_project_identity",
        `client project collection repeats ${identity.projectId}`,
        409,
      );
    }
    byProjectId.set(identity.projectId, project);
  }
  return byProjectId;
}

export function associateProjectsById(projects, records) {
  const byProjectId = indexStableProjectsById(projects);
  if (!Array.isArray(records)) return [];
  return records.flatMap((record) => {
    if (
      !record
      || typeof record !== "object"
      || Array.isArray(record)
      || record.projectIdVersion !== 1
      || !isProjectId(record.projectId)
    ) {
      return [];
    }
    const project = byProjectId.get(record.projectId);
    return project ? [{ project, record }] : [];
  });
}

function addWatchStatus(statusByProjectId, record, label) {
  if (
    !record
    || typeof record !== "object"
    || Array.isArray(record)
    || record.projectIdVersion !== 1
    || !isProjectId(record.projectId)
  ) {
    return;
  }
  const labels = statusByProjectId.get(record.projectId) ?? [];
  if (!labels.includes(label)) labels.push(label);
  statusByProjectId.set(record.projectId, labels);
}

export function collectWatchStatusesByProjectId(feedback, actions) {
  const statusByProjectId = new Map();
  for (const item of Array.isArray(actions) ? actions : []) {
    if (item?.action === "saved") addWatchStatus(statusByProjectId, item, "已关注");
  }
  return statusByProjectId;
}
