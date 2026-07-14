export const SCORE_DIMENSION_KEYS = Object.freeze([
  "attention",
  "endurance",
  "engineeringReadiness",
  "reuseFit",
  "evidenceCompleteness",
]);

export const SCORE_DIMENSION_LABELS = Object.freeze({
  attention: "关注优先级",
  endurance: "持久热度",
  engineeringReadiness: "静态工程就绪度",
  reuseFit: "任务复用匹配",
  evidenceCompleteness: "证据完整度",
});

const LEGACY_SCORE_MODEL_VERSION = "legacy-v1";
const EVIDENCE_SCORE_MODEL_VERSION = "evidence-v2";

function requireObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value;
}

function requireScore(value, label) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 100) {
    throw new TypeError(`${label} must be a finite score from 0 to 100`);
  }
  return value;
}

function nullableScore(value, label) {
  if (value === null || value === undefined) return null;
  return requireScore(value, label);
}

function legacyExplanation({ score, summary, facts = [], proxies = [], limitations, upgradeConditions }) {
  return {
    score,
    summary,
    facts,
    proxies,
    limitations,
    upgradeConditions,
  };
}

function legacyScoreExplanations(project, attentionScore, enduranceScore) {
  const facts = [];
  if (typeof project.stars === "number") facts.push(`累计 Star：${project.stars}`);
  if (typeof project.growthLabel === "string" && project.growthLabel) {
    facts.push(`增长口径：${project.growthLabel}`);
  }

  const enduranceFacts = [];
  if (typeof project.heatObservationCount === "number" && typeof project.heatObservationWindow === "number") {
    enduranceFacts.push(`热度观察：${project.heatObservationCount}/${project.heatObservationWindow} 次快照`);
  }

  return {
    attention: legacyExplanation({
      score: attentionScore,
      summary: "沿用 Catalog v1 的热度综合分，仅解释为关注优先级代理。",
      facts,
      proxies: ["legacy globalScore 综合增长、新鲜度、维护、Star 与 Fork 等信号。"],
      limitations: ["不代表全球影响力、工程质量、运行可靠性或具体任务适配。"],
      upgradeConditions: ["由 evidence-v2 直接提供 attentionScore 及可追溯分项说明。"],
    }),
    endurance: legacyExplanation({
      score: enduranceScore,
      summary: enduranceScore === null
        ? "Catalog v1 没有可用的持久热度分数。"
        : "沿用 Catalog v1 的持久热度结果，并保留其代理性质。",
      facts: enduranceFacts,
      proxies: enduranceScore === null ? [] : ["可能包含仓库年龄、累计 Star、维护和有限快照持续性代理。"],
      limitations: ["有限历史快照不能证明长期持续霸榜。"],
      upgradeConditions: ["积累足够多周期快照并由 evidence-v2 标明长期事实与代理来源。"],
    }),
    engineeringReadiness: legacyExplanation({
      score: null,
      summary: "Catalog v1 无法给出可信的静态工程就绪度。",
      facts: typeof project.analysisState === "string" ? [`当前分析状态：${project.analysisState}`] : [],
      limitations: ["legacy reuseScore 不得映射为静态工程就绪度；存在 README、测试或 CI 也不等于可运行可靠。"],
      upgradeConditions: ["完成绑定当前源码版本的只读静态检查，并由 evidence-v2 分项记录代码、测试、文档与许可证证据。"],
    }),
    reuseFit: legacyExplanation({
      score: null,
      summary: "Catalog v1 没有结合当前任务计算任务复用匹配。",
      limitations: ["项目能力描述和适用场景只是待验证假设，不能冒充复用结论。"],
      upgradeConditions: ["提供具体任务、约束和集成边界后重新匹配，并在隔离环境验证关键路径。"],
    }),
    evidenceCompleteness: legacyExplanation({
      score: null,
      summary: "Catalog v1 没有独立、可解释的证据完整度分数。",
      facts: Array.isArray(project.evidence) ? [`当前列出 ${project.evidence.length} 条证据。`] : [],
      limitations: ["证据条数不能替代来源覆盖、新鲜度和版本绑定检查。"],
      upgradeConditions: ["由 evidence-v2 对事实来源、静态证据、时间绑定和未知项分别审计。"],
    }),
  };
}

function normalizeLegacyRiskCopy(value) {
  if (typeof value !== "string") return value;
  return value
    .replaceAll("复用评分上限已受限制", "静态工程就绪度仍为未知")
    .replaceAll("复用评分已受限制", "静态工程就绪度仍为未知");
}

function normalizeLegacyProject(value) {
  const project = requireObject(value, "Catalog v1 project");
  const attentionScore = requireScore(project.globalScore, "Catalog v1 globalScore");
  const enduranceScore = nullableScore(project.enduranceScore, "Catalog v1 enduranceScore");
  const canonical = { ...project };
  const legacyFit = canonical.fit;
  delete canonical.globalScore;
  delete canonical.reuseScore;
  delete canonical.momentumScore;
  delete canonical.enduranceScore;
  delete canonical.fit;
  canonical.risk = normalizeLegacyRiskCopy(project.risk);
  const recommendation = project.recommendation === "复用" || project.recommendation === "试用"
    ? "隔离试用"
    : project.recommendation;

  return {
    ...canonical,
    scoreModelVersion: LEGACY_SCORE_MODEL_VERSION,
    attentionScore,
    enduranceScore,
    engineeringReadiness: null,
    reuseFitScore: null,
    evidenceCompleteness: null,
    scoreExplanations: legacyScoreExplanations(project, attentionScore, enduranceScore),
    recommendation,
    fitHypothesis: typeof legacyFit === "string" ? legacyFit : "Catalog v1 未提供适用场景假设。",
  };
}

function normalizeEvidenceProject(value) {
  const project = requireObject(value, "Catalog v2 project");
  return {
    ...project,
    scoreModelVersion: EVIDENCE_SCORE_MODEL_VERSION,
  };
}

export function normalizeCatalogSnapshot(value) {
  const catalog = requireObject(value, "catalog");
  if (!Array.isArray(catalog.projects)) throw new TypeError("catalog.projects must be an array");

  if (catalog.schemaVersion === 1) {
    return {
      ...catalog,
      scoreModelVersion: LEGACY_SCORE_MODEL_VERSION,
      projects: catalog.projects.map(normalizeLegacyProject),
    };
  }

  if (catalog.schemaVersion === 2) {
    if (catalog.scoreModelVersion !== EVIDENCE_SCORE_MODEL_VERSION) {
      throw new Error(`unsupported Catalog v2 scoreModelVersion: ${String(catalog.scoreModelVersion)}`);
    }
    return {
      ...catalog,
      projects: catalog.projects.map(normalizeEvidenceProject),
    };
  }

  throw new Error(`unsupported catalog schemaVersion: ${String(catalog.schemaVersion)}`);
}

export function evidenceBaseScore(project) {
  const value = requireObject(project, "project");
  const attentionScore = requireScore(value.attentionScore, "attentionScore");
  const engineeringReadiness = nullableScore(value.engineeringReadiness, "engineeringReadiness");
  return engineeringReadiness === null
    ? attentionScore
    : attentionScore * 0.58 + engineeringReadiness * 0.42;
}
