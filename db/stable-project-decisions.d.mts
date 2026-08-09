export const PROJECT_ID_VERSION: 1;
export const FEEDBACK_VALUES: readonly ["有用", "无用", "复用", "待确定"];

export type StableProjectIdentity = {
  projectIdVersion: 1;
  projectId: string;
  projectSlug: string;
  catalogGenerationId: string;
};

export type ProjectIdentityCatalog = {
  generationId: string;
  publishedAt: string;
  projects: readonly Array<{
    projectIdVersion: 1;
    projectId: string;
    projectSlug: string;
    repository: string;
  }>;
};

export type HistoricalIdentityBundle = {
  schemaVersion: 1;
  activeGenerationId: string;
  activePublishedAt: string;
  generationCount: number;
  mappingCount: number;
  generations: readonly Array<{
    generationId: string;
    generationCreatedAt: string;
    publishedAt: string | null;
    manifestSha256: string;
    catalogSchemaVersion: 1 | 2 | 3;
    active: boolean;
  }>;
  mappings: readonly Array<{
    generationId: string;
    generationCreatedAt: string;
    publishedAt: string | null;
    manifestSha256: string;
    catalogSchemaVersion: 1 | 2 | 3;
    projectIdVersion: 1;
    projectId: string;
    canonicalRepository: string;
    projectSlug: string;
    active: boolean;
  }>;
};

export type LegacyProjectIdentityDispositionPolicy = {
  schemaVersion: 1;
  policyVersion: string;
  entries: readonly Array<{
    projectSlug: string;
    disposition: "quarantine";
    reasonCode: "no_verified_repository_in_current_or_retained_catalogs";
    sourceTables: readonly Array<
      "feedback" | "decision_events"
    >;
  }>;
};

export type StableProjectActionEvent = StableProjectIdentity & {
  id: number;
  deviceId: string;
  action: "opened" | "saved" | "tried" | "cloned" | "reused";
  occurredAt: string;
  idempotencyKey: string;
};

export type StableProjectActionState = StableProjectIdentity & {
  deviceId: string;
  highestStage: StableProjectActionEvent["action"];
  openedAt: string | null;
  savedAt: string | null;
  triedAt: string | null;
  clonedAt: string | null;
  reusedAt: string | null;
  updatedAt: string;
};

export type StableFeedback = StableProjectIdentity & {
  deviceId: string;
  value: "有用" | "无用" | "复用" | "待确定";
  createdAt: string;
  updatedAt: string;
};

type PreparedStatement = {
  bind(...values: unknown[]): PreparedStatement;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  all<T = Record<string, unknown>>(): Promise<{ results?: T[] }>;
  run(): Promise<unknown>;
};

type Database = {
  prepare(statement: string): PreparedStatement;
  batch(statements: PreparedStatement[]): Promise<unknown[]>;
};

export class StableProjectDecisionError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown>;
}

export function adoptStableProjectIdentities(
  database: Database,
  context: ProjectIdentityCatalog | HistoricalIdentityBundle | {
    bundle: HistoricalIdentityBundle;
    policy?: LegacyProjectIdentityDispositionPolicy;
  },
  policy?: LegacyProjectIdentityDispositionPolicy,
): Promise<{
  status: "ready" | "ready_with_quarantine";
  generationId: string;
  projectCount: number;
  migratedProjectCount: number;
  migratedFactCount: number;
  quarantinedSlugCount: number;
  quarantinedFactCount: number;
  unresolvedBlockingCount: 0;
}>;

export function appendStableProjectActionEvent(
  database: Database,
  input: StableProjectIdentity & {
    deviceId: string;
    action: StableProjectActionEvent["action"];
    idempotencyKey: string;
  },
  occurredAt?: string,
): Promise<{
  status: "recorded" | "replayed" | "conflict";
  recorded: boolean;
  event: StableProjectActionEvent;
}>;

export function readStableProjectActionState(
  database: Database,
  deviceId: string,
  projectId?: string | null,
): Promise<StableProjectActionState[]>;

export function stableStateToActionProjection(states: StableProjectActionState[]): Array<
  StableProjectIdentity & { deviceId: string; action: StableProjectActionEvent["action"]; createdAt: string; occurredAt: string }
>;

export function readStableWeeklyActionMetrics(database: Database, deviceId: string, now?: string): Promise<{
  actedProjects: number;
  openedProjects: number;
  savedProjects: number;
  triedProjects: number;
  clonedProjects: number;
  reusedProjects: number;
}>;

export function upsertStableFeedback(
  database: Database,
  input: StableProjectIdentity & { deviceId: string; value: StableFeedback["value"] },
  now?: string,
): Promise<{ changed: boolean; feedback: StableFeedback }>;

export function readStableFeedback(
  database: Database,
  deviceId: string,
  projectId?: string | null,
): Promise<StableFeedback[]>;

export function readStableWeeklyFeedbackMetrics(database: Database, deviceId: string, now?: string): Promise<{
  effectiveDecisions: number;
  reuseDecisions: number;
  feedbackChanges: number;
}>;
