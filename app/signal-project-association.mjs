import {
  ProjectIdentityError,
  canonicalizeRepository,
  identityForRepository,
} from "./project-identity.mjs";

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/**
 * Derive a Signal association from its explicit GitHub repository and the
 * request-scoped identity context built from the same published generation.
 * Source-supplied project identity fields and explanatory text are never read.
 */
export async function associateSignalWithCatalog(signal, identityContext) {
  if (!isRecord(signal) || signal.repo === undefined) return null;

  let signalIdentity;
  try {
    signalIdentity = await identityForRepository(signal.repo);
  } catch (error) {
    if (error instanceof ProjectIdentityError) return null;
    throw error;
  }

  if (!identityContext || typeof identityContext.currentProjectById !== "function") {
    throw new TypeError("signal association requires a verified project identity context");
  }
  const project = identityContext.currentProjectById(signalIdentity.projectId);
  if (!project) return null;

  let catalogRepository;
  try {
    catalogRepository = canonicalizeRepository(project.repository);
  } catch (error) {
    if (error instanceof ProjectIdentityError) return null;
    throw error;
  }
  if (
    project.projectIdVersion !== 1
    || project.projectId !== signalIdentity.projectId
    || catalogRepository !== signalIdentity.canonicalRepository
  ) {
    return null;
  }

  return Object.freeze({
    associationType: "github_repository",
    projectIdVersion: 1,
    projectId: project.projectId,
    repository: project.repository,
  });
}

/**
 * Attach derived associations to the complete Signal list. topSignals may
 * only reuse a row with the same ID from that audited list; a top-only row is
 * deliberately rendered as signal-only rather than becoming a second source
 * of project identity.
 */
export async function associateSignalSnapshotWithCatalog(signalSnapshot, identityContext) {
  if (
    !isRecord(signalSnapshot)
    || !Array.isArray(signalSnapshot.signals)
    || !Array.isArray(signalSnapshot.topSignals)
  ) {
    throw new TypeError("signal association requires a valid Signal snapshot");
  }

  const signals = await Promise.all(signalSnapshot.signals.map(async (signal) => ({
    ...signal,
    projectAssociation: await associateSignalWithCatalog(signal, identityContext),
  })));
  const signalById = new Map();
  for (const signal of signals) {
    if (typeof signal.id !== "string" || !signal.id || signalById.has(signal.id)) {
      throw new TypeError("signal association requires non-empty, unique Signal IDs");
    }
    signalById.set(signal.id, signal);
  }
  const topSignals = signalSnapshot.topSignals.map((signal) => {
    const completeSignal = isRecord(signal) && typeof signal.id === "string"
      ? signalById.get(signal.id)
      : null;
    return completeSignal ?? { ...signal, projectAssociation: null };
  });

  return {
    ...signalSnapshot,
    signals,
    topSignals,
  };
}
