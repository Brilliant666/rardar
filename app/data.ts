import catalogJson from "../data/catalog/latest.json";

export type Evidence = {
  label: string;
  detail: string;
  href: string;
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
  globalScore: number;
  reuseScore: number;
  trend: string;
  analysisState: "事实初筛" | "静态分析" | "深度分析" | "画像待复核";
  sourcePushedAt?: string | null;
  analysisAnalyzedAt?: string | null;
  enrichmentAnalyzedAt?: string | null;
  whyNow: string;
  recommendation: "了解" | "收藏" | "试用" | "复用" | "观望";
  fit: string;
  reusePlan: string;
  risk: string;
  capabilities: string[];
  taskTerms: string[];
  evidence: Evidence[];
  capturedAt: string;
};

export type CatalogSnapshot = {
  schemaVersion: number;
  capturedAt: string;
  sourceCount: number;
  queryFailureCount: number;
  projectCount: number;
  deepAnalysisCount: number;
  pendingDeepAnalysis: string[];
  growthMode: "observed" | "mixed_observation" | "first_observation_proxy";
  notice: string;
  projects: Project[];
};

export const catalog = catalogJson as CatalogSnapshot;
export const projects = catalog.projects;
export const dailyProjects = projects.slice(0, 5);
export const candidateProjects = projects.slice(5);

export const categories = [
  "全部",
  ...Array.from(new Set(projects.map((project) => project.category))),
];

export const snapshotNotice = catalog.notice;

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

export function getProject(slug: string) {
  return projects.find((project) => project.slug === slug);
}
