import assert from "node:assert/strict";
import test from "node:test";
import {
  SCORE_DIMENSION_KEYS,
  evidenceBaseScore,
  normalizeCatalogSnapshot,
} from "../app/score-semantics.mjs";

function explanation(score, summary) {
  return {
    score,
    summary,
    facts: score === null ? [] : ["fact"],
    proxies: [],
    limitations: score === null ? ["unknown"] : [],
    upgradeConditions: ["upgrade"],
  };
}

test("normalizes Catalog v1 conservatively without laundering legacy reuseScore", () => {
  const legacyProject = {
    slug: "owner--repo",
    stars: 123,
    growthLabel: "观测 +3 / 24h",
    globalScore: 81,
    reuseScore: 97,
    momentumScore: 75,
    enduranceScore: 64,
    analysisState: "静态分析",
    heatObservationCount: 2,
    heatObservationWindow: 7,
    evidence: [{ label: "snapshot" }],
    recommendation: "复用",
    fit: "可能适合任务自动化",
    risk: "证据不足，复用评分上限已受限制；旧画像的复用评分已受限制。",
  };
  const catalog = normalizeCatalogSnapshot({
    schemaVersion: 1,
    projects: [legacyProject],
  });

  assert.equal(catalog.scoreModelVersion, "legacy-v1");
  const [project] = catalog.projects;
  assert.equal(project.scoreModelVersion, "legacy-v1");
  assert.equal(project.attentionScore, 81);
  assert.equal(project.enduranceScore, 64);
  assert.equal(project.engineeringReadiness, null);
  assert.equal(project.reuseFitScore, null);
  assert.equal(project.evidenceCompleteness, null);
  assert.equal(project.recommendation, "隔离试用");
  assert.equal(project.fitHypothesis, "可能适合任务自动化");
  assert.equal(
    project.risk,
    "证据不足，静态工程就绪度仍为未知；旧画像的静态工程就绪度仍为未知。",
  );
  assert.equal(
    legacyProject.risk,
    "证据不足，复用评分上限已受限制；旧画像的复用评分已受限制。",
    "Web compatibility must not mutate the persisted v1 object",
  );
  assert.equal("globalScore" in project, false);
  assert.equal("reuseScore" in project, false);
  assert.equal("momentumScore" in project, false);
  assert.equal("fit" in project, false);
  assert.deepEqual(Object.keys(project.scoreExplanations), SCORE_DIMENSION_KEYS);
  assert.equal(project.scoreExplanations.engineeringReadiness.score, null);
  assert.match(
    project.scoreExplanations.engineeringReadiness.limitations.join(" "),
    /legacy reuseScore 不得映射/,
  );
  for (const item of Object.values(project.scoreExplanations)) {
    assert.deepEqual(
      Object.keys(item),
      ["score", "summary", "facts", "proxies", "limitations", "upgradeConditions"],
    );
  }
});

test("downgrades both legacy trial recommendations and preserves non-strong recommendations", () => {
  const projects = [
    { globalScore: 60, enduranceScore: 30, recommendation: "试用", fit: "trial" },
    { globalScore: 50, enduranceScore: 20, recommendation: "收藏", fit: "save" },
  ];
  const normalized = normalizeCatalogSnapshot({ schemaVersion: 1, projects });
  assert.deepEqual(normalized.projects.map((project) => project.recommendation), ["隔离试用", "收藏"]);
});

test("maps evidence-v2 projects to canonical fields without reinterpreting scores", () => {
  const scoreExplanations = {
    attention: explanation(88, "attention"),
    endurance: explanation(73, "endurance"),
    engineeringReadiness: explanation(69, "readiness"),
    reuseFit: explanation(null, "task context required"),
    evidenceCompleteness: explanation(91, "evidence"),
  };
  const rawProject = {
    slug: "owner--v2",
    attentionScore: 88,
    enduranceScore: 73,
    engineeringReadiness: 69,
    reuseFitScore: null,
    evidenceCompleteness: 91,
    scoreExplanations,
    fitHypothesis: "适合证据整理任务",
    recommendation: "隔离试用",
    risk: "v2 原样保留风险事实，即使文本含复用评分已受限制。",
  };
  const catalog = normalizeCatalogSnapshot({
    schemaVersion: 2,
    scoreModelVersion: "evidence-v2",
    notice: "verified",
    projects: [rawProject],
  });

  assert.equal(catalog.scoreModelVersion, "evidence-v2");
  assert.deepEqual(catalog.projects[0], {
    ...rawProject,
    scoreModelVersion: "evidence-v2",
  });
  assert.strictEqual(catalog.projects[0].scoreExplanations, scoreExplanations);
  assert.equal(catalog.projects[0].risk, rawProject.risk, "v2 copy must not be rewritten");
});

test("preserves Catalog v3 stable identity without changing evidence-v2 scores", () => {
  const scoreExplanations = {
    attention: explanation(90, "attention"),
    endurance: explanation(80, "endurance"),
    engineeringReadiness: explanation(70, "readiness"),
    reuseFit: explanation(null, "task context required"),
    evidenceCompleteness: explanation(95, "evidence"),
  };
  const rawProject = {
    projectIdVersion: 1,
    projectId: "owner-repo--65e817eec8cd71edae74",
    slug: "owner--repo",
    attentionScore: 90,
    enduranceScore: 80,
    engineeringReadiness: 70,
    reuseFitScore: null,
    evidenceCompleteness: 95,
    scoreExplanations,
  };
  const catalog = normalizeCatalogSnapshot({
    schemaVersion: 3,
    projectIdVersion: 1,
    scoreModelVersion: "evidence-v2",
    projects: [rawProject],
  });

  assert.equal(catalog.projectIdVersion, 1);
  assert.deepEqual(catalog.projects[0], {
    ...rawProject,
    scoreModelVersion: "evidence-v2",
  });
  assert.equal(catalog.projects[0].projectId, rawProject.projectId);
  assert.strictEqual(catalog.projects[0].scoreExplanations, scoreExplanations);
});

test("fails closed when Catalog v3 stable identity is missing or unsupported", () => {
  const project = {
    projectIdVersion: 1,
    projectId: "owner-repo--65e817eec8cd71edae74",
  };
  assert.throws(
    () => normalizeCatalogSnapshot({
      schemaVersion: 3,
      scoreModelVersion: "evidence-v2",
      projects: [project],
    }),
    /Catalog v3 must use projectIdVersion 1/,
  );
  assert.throws(
    () => normalizeCatalogSnapshot({
      schemaVersion: 3,
      projectIdVersion: 1,
      scoreModelVersion: "evidence-v2",
      projects: [{ projectIdVersion: 1 }],
    }),
    /projectId must be a non-empty string/,
  );
  assert.throws(
    () => normalizeCatalogSnapshot({
      schemaVersion: 3,
      projectIdVersion: 1,
      scoreModelVersion: "evidence-v2",
      projects: [{ projectIdVersion: 2, projectId: project.projectId }],
    }),
    /Catalog v3 project must use projectIdVersion 1/,
  );
  assert.throws(
    () => normalizeCatalogSnapshot({
      schemaVersion: 3,
      projectIdVersion: 1,
      scoreModelVersion: "future-v4",
      projects: [project],
    }),
    /unsupported Catalog v3 scoreModelVersion/,
  );
});

test("fails closed for unknown catalog or score model versions", () => {
  assert.throws(
    () => normalizeCatalogSnapshot({ schemaVersion: 4, projects: [] }),
    /unsupported catalog schemaVersion: 4/,
  );
  assert.throws(
    () => normalizeCatalogSnapshot({ schemaVersion: 2, scoreModelVersion: "future-v3", projects: [] }),
    /unsupported Catalog v2 scoreModelVersion/,
  );
});

test("uses engineering readiness only when it is known in the evidence base score", () => {
  assert.equal(
    evidenceBaseScore({ attentionScore: 80, engineeringReadiness: 50 }),
    80 * 0.58 + 50 * 0.42,
  );
  assert.equal(
    evidenceBaseScore({ attentionScore: 80, engineeringReadiness: null }),
    80,
  );
});
