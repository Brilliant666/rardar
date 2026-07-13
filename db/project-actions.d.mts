export const ACTION_VALUES: readonly ["opened", "saved", "tried", "cloned", "reused"];
export const LEGACY_IDEMPOTENCY_PREFIX: "legacy-project-actions:";
export const PROJECT_ACTION_SCHEMA_SQL: readonly string[];

export type ProjectAction = (typeof ACTION_VALUES)[number];

export type ProjectActionEvent = {
  id: number;
  deviceId: string;
  projectSlug: string;
  action: ProjectAction;
  occurredAt: string;
  idempotencyKey: string;
};

export type ProjectActionState = {
  deviceId: string;
  projectSlug: string;
  highestStage: ProjectAction;
  openedAt: string | null;
  savedAt: string | null;
  triedAt: string | null;
  clonedAt: string | null;
  reusedAt: string | null;
  updatedAt: string;
};

type PreparedStatement = {
  bind(...values: unknown[]): PreparedStatement;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  all<T = Record<string, unknown>>(): Promise<{ results?: T[] }>;
};

type Database = {
  prepare(statement: string): PreparedStatement;
};

export function prepareProjectActionSchema<TStatement>(
  database: { prepare(statement: string): TStatement },
): TStatement[];

export function appendProjectActionEvent(
  database: Database,
  input: {
    deviceId: string;
    projectSlug: string;
    action: ProjectAction;
    idempotencyKey: string;
  },
  occurredAt?: string,
): Promise<{
  status: "recorded" | "replayed" | "conflict";
  recorded: boolean;
  event: ProjectActionEvent;
}>;

export function readProjectActionState(
  database: Database,
  deviceId: string,
  projectSlug?: string | null,
): Promise<ProjectActionState[]>;

export function stateToActionProjection(states: ProjectActionState[]): Array<{
  deviceId: string;
  projectSlug: string;
  action: ProjectAction;
  createdAt: string;
  occurredAt: string;
}>;

export function readWeeklyActionMetrics(
  database: Database,
  deviceId: string,
  now?: string,
): Promise<{
  actedProjects: number;
  openedProjects: number;
  savedProjects: number;
  triedProjects: number;
  clonedProjects: number;
  reusedProjects: number;
}>;
