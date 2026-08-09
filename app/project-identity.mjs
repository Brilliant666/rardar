const PROJECT_ID_VERSION = 1;
const PROJECT_ID_PREFIX_MAX_LENGTH = 64;
const PROJECT_ID_DIGEST_HEX_LENGTH = 20;
const REPOSITORY_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}\/(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}$/;
const PROJECT_ID_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*--[0-9a-f]{20}$/;
const PROJECT_ID_MAX_LENGTH = PROJECT_ID_PREFIX_MAX_LENGTH + 2 + PROJECT_ID_DIGEST_HEX_LENGTH;
const PREFIX_UNSAFE_PATTERN = /[^a-z0-9]+/g;

export class ProjectIdentityError extends Error {
  constructor(code, message, status = 503) {
    super(message);
    this.name = "ProjectIdentityError";
    this.code = code;
    this.status = status;
  }
}

function fail(code, message, status = 503) {
  throw new ProjectIdentityError(code, message, status);
}

export function canonicalizeRepository(repository) {
  if (typeof repository !== "string") {
    fail("invalid_repository_type", "repository must be a string in exact GitHub owner/repo form");
  }
  if (!REPOSITORY_PATTERN.test(repository)) {
    fail(
      "invalid_repository_format",
      "repository must be an exact GitHub owner/repo",
    );
  }
  return repository.toLowerCase();
}

export function isProjectId(value) {
  return typeof value === "string"
    && value.length <= PROJECT_ID_MAX_LENGTH
    && PROJECT_ID_PATTERN.test(value);
}

async function sha256Hex(value) {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) fail("identity_crypto_unavailable", "Web Crypto SHA-256 is unavailable");
  const digest = await subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export async function identityForRepository(repository) {
  const canonicalRepository = canonicalizeRepository(repository);
  const humanPrefix = canonicalRepository
    .replace(PREFIX_UNSAFE_PATTERN, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, PROJECT_ID_PREFIX_MAX_LENGTH)
    .replace(/-+$/g, "");
  if (!humanPrefix) fail("invalid_repository_format", "repository has no safe project ID prefix");
  const digest = (await sha256Hex(canonicalRepository)).slice(0, PROJECT_ID_DIGEST_HEX_LENGTH);
  const projectId = `${humanPrefix}--${digest}`;
  if (!isProjectId(projectId)) fail("invalid_project_id", "identity v1 produced an invalid project ID");
  return Object.freeze({
    projectIdVersion: PROJECT_ID_VERSION,
    canonicalRepository,
    humanPrefix,
    digest,
    projectId,
  });
}

function requireCatalogProject(project, index) {
  if (!project || typeof project !== "object" || Array.isArray(project)) {
    fail("invalid_catalog_project", `catalog project ${index} must be an object`);
  }
  if (typeof project.slug !== "string" || !project.slug) {
    fail("invalid_catalog_project", `catalog project ${index} has no legacy slug`);
  }
  return project;
}

function requireGenerationId(value) {
  if (typeof value !== "string" || !value) {
    fail("invalid_generation_identity", "generationId is required for project identity resolution");
  }
  return value;
}

function requirePublishedAt(value) {
  if (
    typeof value !== "string"
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)
    || Number.isNaN(Date.parse(value))
  ) {
    fail(
      "invalid_generation_publication_time",
      "publishedAt must be a timezone-aware RFC3339 timestamp",
    );
  }
  return value;
}

/**
 * Build one request-scoped identity view from one already verified published
 * generation. The serializable `identityCatalog` is the only value passed to
 * D1 initialization; lookup Maps remain private to this request.
 */
export async function createProjectIdentityContext(generationId, catalog, publishedAt) {
  requireGenerationId(generationId);
  requirePublishedAt(publishedAt);
  if (!catalog || typeof catalog !== "object" || Array.isArray(catalog)) {
    fail("invalid_catalog_identity", "catalog must be an object");
  }
  if (![1, 2, 3].includes(catalog.schemaVersion) || !Array.isArray(catalog.projects)) {
    fail("invalid_catalog_identity", "catalog schemaVersion or projects are invalid");
  }
  if (catalog.schemaVersion === 3 && catalog.projectIdVersion !== PROJECT_ID_VERSION) {
    fail("unsupported_project_id_version", "Catalog v3 must use projectIdVersion 1");
  }

  const derivedProjects = await Promise.all(catalog.projects.map(async (value, index) => {
    const project = requireCatalogProject(value, index);
    const identity = await identityForRepository(project.repo);
    if (catalog.schemaVersion === 3) {
      if (project.projectIdVersion !== PROJECT_ID_VERSION) {
        fail("unsupported_project_id_version", `catalog project ${index} must use projectIdVersion 1`);
      }
      if (!isProjectId(project.projectId)) {
        fail("invalid_project_id", `catalog project ${index} carries an invalid projectId`);
      }
      if (project.projectId !== identity.projectId) {
        fail("project_id_mismatch", `catalog project ${index} projectId does not match repo`);
      }
    }
    return {
      canonicalRepository: identity.canonicalRepository,
      project: Object.freeze({
        projectIdVersion: PROJECT_ID_VERSION,
        projectId: identity.projectId,
        projectSlug: project.slug,
        repository: project.repo,
      }),
      source: project,
    };
  }));

  const byCanonicalRepository = new Map();
  const byProjectId = new Map();
  const byProjectSlug = new Map();
  for (const item of derivedProjects) {
    if (byCanonicalRepository.has(item.canonicalRepository)) {
      fail(
        "duplicate_normalized_repository",
        `catalog repeats normalized repository ${item.canonicalRepository}`,
      );
    }
    const existingId = byProjectId.get(item.project.projectId);
    if (existingId) {
      fail(
        "project_id_collision",
        `catalog projectId ${item.project.projectId} belongs to multiple repositories`,
      );
    }
    byCanonicalRepository.set(item.canonicalRepository, item.project);
    byProjectId.set(item.project.projectId, item.project);
    const slugProjects = byProjectSlug.get(item.project.projectSlug) ?? [];
    slugProjects.push(item.project);
    byProjectSlug.set(item.project.projectSlug, slugProjects);
  }

  const identityCatalog = Object.freeze({
    generationId,
    publishedAt,
    projects: Object.freeze(derivedProjects.map((item) => item.project)),
  });

  function projectById(projectId) {
    const project = byProjectId.get(projectId);
    if (!project) {
      fail("unresolved_project_identity", "stored projectId is absent from the current Catalog");
    }
    return project;
  }

  function currentProjectById(projectId) {
    return byProjectId.get(projectId) ?? null;
  }

  function stableProjects(projects) {
    if (!Array.isArray(projects) || projects.length !== derivedProjects.length) {
      fail("invalid_catalog_identity", "project list does not match the identity Catalog");
    }
    return projects.map((project, index) => {
      if (project !== derivedProjects[index].source) {
        const canonicalRepository = canonicalizeRepository(project?.repo);
        if (canonicalRepository !== derivedProjects[index].canonicalRepository) {
          fail("invalid_catalog_identity", "project order does not match the identity Catalog");
        }
      }
      return {
        ...project,
        projectIdVersion: PROJECT_ID_VERSION,
        projectId: derivedProjects[index].project.projectId,
      };
    });
  }

  return Object.freeze({
    identityCatalog,
    currentProjectById,
    projectById,
    stableProjects,
    _byProjectId: byProjectId,
    _byProjectSlug: byProjectSlug,
  });
}

function normalizeSelector(selector) {
  if (!selector || typeof selector !== "object" || Array.isArray(selector)) {
    fail("invalid_project_selector", "project selector must be an object", 400);
  }
  const hasProjectId = Object.hasOwn(selector, "projectId") && selector.projectId !== undefined;
  const hasProjectIdVersion = Object.hasOwn(selector, "projectIdVersion")
    && selector.projectIdVersion !== undefined;
  const hasProjectSlug = Object.hasOwn(selector, "projectSlug") && selector.projectSlug !== undefined;
  if (hasProjectId !== hasProjectIdVersion) {
    fail(
      "invalid_project_selector",
      "projectIdVersion and projectId must be provided together",
      400,
    );
  }
  if (hasProjectIdVersion && selector.projectIdVersion !== PROJECT_ID_VERSION) {
    fail("unsupported_project_id_version", "projectIdVersion must be 1", 400);
  }
  if (hasProjectId && !isProjectId(selector.projectId)) {
    fail("invalid_project_id", "projectId does not match identity v1", 400);
  }
  if (hasProjectSlug && (typeof selector.projectSlug !== "string" || !selector.projectSlug)) {
    fail("invalid_project_slug", "projectSlug must be a non-empty exact Catalog slug", 400);
  }
  return { hasProjectId, hasProjectSlug };
}

export function resolveProjectSelector(context, selector, { required = true } = {}) {
  const { hasProjectId, hasProjectSlug } = normalizeSelector(selector);
  if (!hasProjectId && !hasProjectSlug) {
    if (!required) return null;
    fail("missing_project_identity", "projectId or projectSlug is required", 400);
  }

  let canonicalProject = null;
  if (hasProjectId) {
    canonicalProject = context._byProjectId.get(selector.projectId) ?? null;
    if (!canonicalProject) fail("unknown_project_id", "projectId is not in the current Catalog", 404);
  }

  let legacyProject = null;
  if (hasProjectSlug) {
    const matches = context._byProjectSlug.get(selector.projectSlug) ?? [];
    if (matches.length === 0) fail("unknown_project_slug", "projectSlug is not in the current Catalog", 404);
    if (matches.length !== 1) {
      fail("ambiguous_project_slug", "projectSlug maps to multiple current Catalog projects", 409);
    }
    [legacyProject] = matches;
  }

  if (canonicalProject && legacyProject && canonicalProject.projectId !== legacyProject.projectId) {
    fail("project_identity_conflict", "projectId and projectSlug identify different projects", 409);
  }
  return canonicalProject ?? legacyProject;
}

export function selectorFromSearchParams(searchParams) {
  const projectIdVersion = searchParams.get("projectIdVersion");
  const projectId = searchParams.get("projectId");
  const projectSlug = searchParams.get("projectSlug");
  return {
    ...(projectIdVersion !== null
      ? { projectIdVersion: projectIdVersion === "1" ? 1 : projectIdVersion }
      : {}),
    ...(projectId !== null ? { projectId } : {}),
    ...(projectSlug !== null ? { projectSlug } : {}),
  };
}

export function selectorFromRecord(payload) {
  return {
    ...(Object.hasOwn(payload, "projectIdVersion")
      ? { projectIdVersion: payload.projectIdVersion }
      : {}),
    ...(Object.hasOwn(payload, "projectId") ? { projectId: payload.projectId } : {}),
    ...(Object.hasOwn(payload, "projectSlug") ? { projectSlug: payload.projectSlug } : {}),
  };
}

export function projectIdentityErrorResponse(error) {
  const stableDecisionError = error
    && typeof error === "object"
    && error.name === "StableProjectDecisionError"
    && typeof error.code === "string";
  if (!(error instanceof ProjectIdentityError) && !stableDecisionError) return null;
  return Response.json(
    { error: error.code },
    {
      status: error instanceof ProjectIdentityError ? error.status : 503,
      headers: { "cache-control": "no-store" },
    },
  );
}

export function validateStoredProjectIdentity(row) {
  if (!row || typeof row !== "object" || Array.isArray(row)) {
    fail("invalid_stored_project_identity", "stored project decision must be an object");
  }
  if (row.projectIdVersion !== PROJECT_ID_VERSION || !isProjectId(row.projectId)) {
    fail("invalid_stored_project_identity", "stored project decision has an invalid projectId");
  }
  return row;
}

export function withCurrentProjectIdentity(context, row) {
  validateStoredProjectIdentity(row);
  const project = context.projectById(row.projectId);
  return {
    ...row,
    projectIdVersion: PROJECT_ID_VERSION,
    projectId: project.projectId,
    projectSlug: project.projectSlug,
  };
}

export function withCurrentProjectIdentityIfPresent(context, row) {
  validateStoredProjectIdentity(row);
  const project = context.currentProjectById(row.projectId);
  if (!project) return null;
  return {
    ...row,
    projectIdVersion: PROJECT_ID_VERSION,
    projectId: project.projectId,
    projectSlug: project.projectSlug,
  };
}

export const PROJECT_IDENTITY_VERSION = PROJECT_ID_VERSION;
