export const SCORE_DIMENSION_KEYS: readonly [
  "attention",
  "endurance",
  "engineeringReadiness",
  "reuseFit",
  "evidenceCompleteness",
];

export const SCORE_DIMENSION_LABELS: Readonly<Record<(typeof SCORE_DIMENSION_KEYS)[number], string>>;

export type ScoreExplanation = {
  score: number | null;
  summary: string;
  facts: string[];
  proxies: string[];
  limitations: string[];
  upgradeConditions: string[];
};

export type CanonicalProjectScores = {
  scoreModelVersion: "legacy-v1" | "evidence-v2";
  attentionScore: number;
  enduranceScore: number | null;
  engineeringReadiness: number | null;
  reuseFitScore: number | null;
  evidenceCompleteness: number | null;
  scoreExplanations: Record<(typeof SCORE_DIMENSION_KEYS)[number], ScoreExplanation>;
};

export function normalizeCatalogSnapshot(
  catalog: unknown,
): Record<string, unknown> & {
  scoreModelVersion: "legacy-v1" | "evidence-v2";
  projects: Array<Record<string, unknown> & CanonicalProjectScores>;
};

export function evidenceBaseScore(project: {
  attentionScore: number;
  engineeringReadiness: number | null;
}): number;
