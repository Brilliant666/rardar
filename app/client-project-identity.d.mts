export type StableClientProjectIdentity = {
  projectIdVersion: 1;
  projectId: string;
};

export function stableProjectSelector(project: unknown): Readonly<StableClientProjectIdentity>;
export function isFreshClientRead(
  aborted: boolean,
  requestVersionAtStart: number,
  currentRequestVersion: number,
  mutationVersionAtStart: number,
  currentMutationVersion: number,
): boolean;
export function resultForGeneration<T>(
  expectedGenerationId: string,
  result: T,
): (T & { generationId: string }) | null;
export function canonicalProjectPath(project: unknown): string;
export function indexStableProjectsById<T>(projects: readonly T[]): Map<string, T>;
export function associateProjectsById<P, R>(
  projects: readonly P[],
  records: readonly R[] | null | undefined,
): Array<{ project: P; record: R }>;
export function collectWatchStatusesByProjectId(
  feedback: readonly unknown[] | null | undefined,
  actions: readonly unknown[] | null | undefined,
): Map<string, string[]>;
