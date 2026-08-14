import type { Evidence, StableProject } from "./data";

export type DecisionState = Readonly<{
  projectIdVersion: 1;
  projectId: string;
  actions: readonly string[];
  feedback: string | null;
}>;

export type ProjectDecisionPresentation = Readonly<{
  whyNow: string;
  facts: readonly string[];
  evidence: readonly Evidence[];
  risk: string | null;
  recommendation: string;
}>;

export function projectDecisionSummary(
  project: StableProject,
  options?: { maxFacts?: number; maxEvidence?: number },
): ProjectDecisionPresentation;
export function collectDecisionStateByProjectId(
  actions: readonly unknown[] | null | undefined,
  feedback: readonly unknown[] | null | undefined,
): Map<string, DecisionState>;
export function mergeGenerationBoundDecisionReads(
  expectedGenerationId: string,
  actionPayload: unknown,
  feedbackPayload: unknown,
): Map<string, DecisionState> | null;
export function applyDecisionStateEvent(
  current: Map<string, DecisionState>,
  detail: unknown,
): Map<string, DecisionState>;
export function replayDecisionStateEvents(
  current: Map<string, DecisionState>,
  entries: readonly { version: number; detail: unknown }[],
  afterVersion: number,
): Map<string, DecisionState>;
export function decisionStatusForProject(state: DecisionState | null | undefined): Readonly<{
  stage: "opened" | "tried" | "cloned" | "reused" | null;
  stageLabel: string;
  acted: boolean;
  watched: boolean;
  feedback: string | null;
}>;
