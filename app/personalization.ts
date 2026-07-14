import type { Project } from "./data";
import { evidenceBaseScore } from "./score-semantics.mjs";

export const feedbackValues = ["有用", "无用", "复用", "待确定"] as const;
export type FeedbackValue = (typeof feedbackValues)[number];

export type ProjectFeedback = {
  projectSlug: string;
  value: string;
};

export type PreferenceFeature = {
  feature: string;
  score: number;
};

export type RankedProject = {
  slug: string;
  personalizedScore: number;
  baseScore: number;
  adjustment: number;
  reasons: string[];
};

export type PersonalizationResult = {
  personalized: boolean;
  feedbackCount: number;
  profile: PreferenceFeature[];
  recommendations: RankedProject[];
};

type ProjectFeature = {
  key: string;
  label: string;
};

const preferenceWeights: Record<FeedbackValue, number> = {
  复用: 2.6,
  有用: 1.4,
  待确定: 0.15,
  无用: -1.7,
};

const directAdjustments: Record<FeedbackValue, number> = {
  复用: -18,
  有用: -12,
  待确定: 2.5,
  无用: -80,
};

const genericFeatures = new Set([
  "开源",
  "工具",
  "开发工具",
  "人工智能",
  "ai",
  "agent",
  "python 项目",
  "typescript 项目",
  "javascript 项目",
]);

function normalizeFeature(value: string) {
  return value.trim().toLocaleLowerCase("zh-CN").replace(/\s+/g, " ");
}

function projectFeatures(project: Project) {
  const features = new Map<string, ProjectFeature>();

  function add(prefix: string, value: string) {
    const normalized = normalizeFeature(value);
    if (normalized.length < 2 || genericFeatures.has(normalized)) return;
    const key = `${prefix}:${normalized}`;
    if (!features.has(key)) features.set(key, { key, label: value.trim() });
  }

  add("分类", project.category);
  project.capabilities.slice(0, 12).forEach((value) => add("能力", value));
  project.taskTerms.slice(0, 12).forEach((value) => add("任务", value));
  return [...features.values()];
}

function isFeedbackValue(value: string): value is FeedbackValue {
  return (feedbackValues as readonly string[]).includes(value);
}

function round(value: number) {
  return Math.round(value * 10) / 10;
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(maximum, Math.max(minimum, value));
}

function balanceHeatTracks<
  T extends { slug: string; heatTrack: "recent_momentum" | "long_term"; personalizedScore: number },
>(items: T[]) {
  const longTerm = items
    .filter((item) => item.heatTrack === "long_term" && item.personalizedScore >= 60)
    .slice(0, 2);
  const recentMomentum = items.filter((item) => item.heatTrack === "recent_momentum").slice(0, 3);
  const selected = new Set([...longTerm, ...recentMomentum].map((item) => item.slug));
  if (selected.size < 5) {
    for (const item of items) {
      selected.add(item.slug);
      if (selected.size === 5) break;
    }
  }
  return [
    ...items.filter((item) => selected.has(item.slug)),
    ...items.filter((item) => !selected.has(item.slug)),
  ];
}

export function rankProjects(
  projects: Project[],
  rawFeedback: ProjectFeedback[],
): PersonalizationResult {
  const projectBySlug = new Map(projects.map((project) => [project.slug, project]));
  const currentFeedback = new Map<string, FeedbackValue>();

  rawFeedback.forEach((item) => {
    if (projectBySlug.has(item.projectSlug) && isFeedbackValue(item.value)) {
      currentFeedback.set(item.projectSlug, item.value);
    }
  });

  if (currentFeedback.size === 0) {
    return {
      personalized: false,
      feedbackCount: 0,
      profile: [],
      recommendations: projects.map((project) => {
        const baseScore = round(evidenceBaseScore(project));
        return {
          slug: project.slug,
          personalizedScore: baseScore,
          baseScore,
          adjustment: 0,
          reasons: [project.engineeringReadiness === null
            ? "当前按关注优先级排序；静态工程就绪度未知"
            : "当前按关注优先级与静态工程就绪证据排序"],
        };
      }),
    };
  }

  const featureScores = new Map<string, { label: string; score: number }>();
  currentFeedback.forEach((value, slug) => {
    const project = projectBySlug.get(slug);
    if (!project) return;
    const features = projectFeatures(project);
    if (!features.length) return;
    const contribution = preferenceWeights[value] / Math.sqrt(features.length);
    features.forEach(({ key, label }) => {
      const previous = featureScores.get(key)?.score ?? 0;
      featureScores.set(key, { label, score: previous + contribution });
    });
  });

  const recommendations = projects.map((project, originalIndex) => {
    const baseScore = evidenceBaseScore(project);
    const matches = projectFeatures(project)
      .map((feature) => ({ ...feature, score: featureScores.get(feature.key)?.score ?? 0 }))
      .filter((feature) => feature.score !== 0);
    const rawAffinity = matches.reduce((total, feature) => total + feature.score, 0);
    const affinityAdjustment = clamp(
      matches.length ? (rawAffinity / Math.sqrt(matches.length)) * 4 : 0,
      -12,
      12,
    );
    const directFeedback = currentFeedback.get(project.slug);
    const directAdjustment = directFeedback ? directAdjustments[directFeedback] : 0;
    const adjustment = affinityAdjustment + directAdjustment;
    const positiveMatches = matches
      .filter((feature) => feature.score > 0)
      .sort((left, right) => right.score - left.score)
      .slice(0, 2)
      .map((feature) => feature.label);

    let reasons: string[];
    if (directFeedback === "无用") {
      reasons = ["你已标记为无用，降低重复曝光"];
    } else if (directFeedback === "待确定") {
      reasons = ["你标记为待确定，暂时保留复核机会"];
    } else if (directFeedback === "有用" || directFeedback === "复用") {
      reasons = [`你已标记为${directFeedback}，优先发现相似的新项目`];
    } else if (positiveMatches.length) {
      reasons = positiveMatches.map((feature) => `符合你的偏好：${feature}`);
    } else if (affinityAdjustment < 0) {
      reasons = ["与已标记无用的项目特征相近，轻度降权"];
    } else {
      reasons = ["保留关注优先级与静态工程就绪证据的基础排序"];
    }

    return {
      slug: project.slug,
      heatTrack: project.heatTrack ?? "recent_momentum",
      personalizedScore: round(baseScore + adjustment),
      baseScore: round(baseScore),
      adjustment: round(adjustment),
      reasons,
      originalIndex,
    };
  });

  recommendations.sort(
    (left, right) =>
      right.personalizedScore - left.personalizedScore || left.originalIndex - right.originalIndex,
  );
  const balancedRecommendations = balanceHeatTracks(recommendations);

  const profile = [...featureScores.values()]
    .sort((left, right) => Math.abs(right.score) - Math.abs(left.score))
    .slice(0, 8)
    .map((item) => ({ feature: item.label, score: round(item.score) }));

  return {
    personalized: true,
    feedbackCount: currentFeedback.size,
    profile,
    recommendations: balancedRecommendations.map((item) => ({
      slug: item.slug,
      personalizedScore: item.personalizedScore,
      baseScore: item.baseScore,
      adjustment: item.adjustment,
      reasons: item.reasons,
    })),
  };
}
