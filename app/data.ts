export type Evidence = {
  label: string;
  detail: string;
  href: string;
};

export type ScoreModelVersion = "legacy-v1" | "evidence-v2";

export type ScoreExplanation = {
  score: number | null;
  summary: string;
  facts: string[];
  proxies: string[];
  limitations: string[];
  upgradeConditions: string[];
};

export type ScoreExplanations = {
  attention: ScoreExplanation;
  endurance: ScoreExplanation;
  engineeringReadiness: ScoreExplanation;
  reuseFit: ScoreExplanation;
  evidenceCompleteness: ScoreExplanation;
};

export type Project = {
  slug: string;
  repo: string;
  title: string;
  description: string;
  category: string;
  language: string;
  license: string;
  stars: number;
  growthValue: number;
  growthLabel: string;
  growthKind: "observed" | "velocity_proxy";
  scoreModelVersion: ScoreModelVersion;
  attentionScore: number;
  enduranceScore: number | null;
  engineeringReadiness: number | null;
  reuseFitScore: number | null;
  evidenceCompleteness: number | null;
  scoreExplanations: ScoreExplanations;
  heatTrack?: "recent_momentum" | "long_term";
  heatLabel?: string;
  longTermEvidenceKind?: "structural_proxy" | "multi_snapshot" | null;
  heatObservationCount?: number;
  heatObservationWindow?: number;
  trend: string;
  analysisState: "事实初筛" | "静态分析" | "深度分析" | "画像待复核";
  sourcePushedAt?: string | null;
  analysisAnalyzedAt?: string | null;
  enrichmentAnalyzedAt?: string | null;
  whyNow: string;
  recommendation: "了解" | "收藏" | "隔离试用" | "观望";
  fitHypothesis: string;
  reusePlan: string;
  risk: string;
  capabilities: string[];
  taskTerms: string[];
  evidence: Evidence[];
  capturedAt: string;
};

export type CatalogSnapshot = {
  schemaVersion: 1 | 2;
  scoreModelVersion: ScoreModelVersion;
  capturedAt: string;
  sourceCount: number;
  queryFailureCount: number;
  projectCount: number;
  deepAnalysisCount: number;
  pendingDeepAnalysis: string[];
  dailyTrackCounts?: { recentMomentum: number; longTerm: number };
  heatHistory?: {
    snapshotCount: number;
    maximumSnapshotCount: number;
    minimumPersistenceSnapshots: number;
    verifiedLongTermCount: number;
  };
  growthMode: "observed" | "mixed_observation" | "first_observation_proxy";
  notice: string;
  projects: Project[];
};

export function projectCategories(projects: Project[]) {
  return [
    "全部",
    ...Array.from(new Set(projects.map((project) => project.category))),
  ];
}

export function formatNumber(value: number) {
  return new Intl.NumberFormat("zh-CN", { notation: "compact" }).format(value);
}

export function formatCapturedDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(date);
}

export function getProject(projects: Project[], slug: string) {
  return projects.find((project) => project.slug === slug);
}
