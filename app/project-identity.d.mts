export const PROJECT_IDENTITY_VERSION: 1;

export type StableProjectIdentity = {
  projectIdVersion: 1;
  projectId: string;
  projectSlug: string;
  repository: string;
};

export type ProjectIdentityCatalog = {
  generationId: string;
  publishedAt: string;
  projects: readonly StableProjectIdentity[];
};

export type ProjectIdentitySelector = {
  projectIdVersion?: unknown;
  projectId?: unknown;
  projectSlug?: unknown;
};

export class ProjectIdentityError extends Error {
  readonly code: string;
  readonly status: number;
  constructor(code: string, message: string, status?: number);
}

export function canonicalizeRepository(repository: unknown): string;
export function isProjectId(value: unknown): value is string;
export function identityForRepository(repository: unknown): Promise<{
  projectIdVersion: 1;
  canonicalRepository: string;
  humanPrefix: string;
  digest: string;
  projectId: string;
}>;

export type ProjectIdentityContext = {
  identityCatalog: ProjectIdentityCatalog;
  currentProjectById(projectId: string): StableProjectIdentity | null;
  projectById(projectId: string): StableProjectIdentity;
  stableProjects<T extends { repo?: unknown }>(projects: readonly T[]): Array<T & {
    projectIdVersion: 1;
    projectId: string;
  }>;
};

export function createProjectIdentityContext(
  generationId: string,
  catalog: { schemaVersion?: unknown; projects?: unknown },
  publishedAt: string,
): Promise<ProjectIdentityContext>;

export function resolveProjectSelector(
  context: ProjectIdentityContext,
  selector: ProjectIdentitySelector,
  options?: { required?: boolean },
): StableProjectIdentity | null;

export function selectorFromSearchParams(searchParams: URLSearchParams): ProjectIdentitySelector;
export function selectorFromRecord(payload: Record<string, unknown>): ProjectIdentitySelector;
export function projectIdentityErrorResponse(error: unknown): Response | null;
export function validateStoredProjectIdentity<T extends object>(
  row: T,
): T & { projectIdVersion: 1; projectId: string };
export function withCurrentProjectIdentity<T extends object>(
  context: ProjectIdentityContext,
  row: T,
): T & { projectIdVersion: 1; projectId: string; projectSlug: string };
export function withCurrentProjectIdentityIfPresent<T extends object>(
  context: ProjectIdentityContext,
  row: T,
): (T & { projectIdVersion: 1; projectId: string; projectSlug: string }) | null;
