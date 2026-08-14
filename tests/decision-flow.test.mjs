import assert from "node:assert/strict";
import test from "node:test";
import {
  applyDecisionStateEvent,
  collectDecisionStateByProjectId,
  decisionStatusForProject,
  mergeGenerationBoundDecisionReads,
  projectDecisionSummary,
  replayDecisionStateEvents,
} from "../app/decision-flow.mjs";
import { identityForRepository } from "../app/project-identity.mjs";

function projectFixture(overrides = {}) {
  return {
    whyNow: "区间 Star 增长是当前关注信号。",
    recommendation: "隔离试用",
    risk: "静态分析不能替代实际运行验证。",
    scoreExplanations: {
      attention: { facts: ["事实 A", "事实 B", "事实 A"] },
      endurance: { facts: ["事实 C"] },
      engineeringReadiness: { facts: ["事实 D"] },
      evidenceCompleteness: { facts: ["事实 E"] },
    },
    evidence: [
      {
        label: "GitHub API",
        detail: "1,234 Star",
        href: "https://api.github.com/repos/owner/project",
      },
      {
        label: "静态检查",
        detail: "检测到测试与 CI",
        href: "https://github.com/owner/project",
      },
    ],
    ...overrides,
  };
}

test("builds a Decision Summary only from existing verified presentation fields", () => {
  const source = projectFixture();
  const summary = projectDecisionSummary(source, { maxFacts: 3, maxEvidence: 1 });

  assert.equal(summary.whyNow, source.whyNow);
  assert.deepEqual(summary.facts, ["事实 A", "事实 B", "事实 C"]);
  assert.deepEqual(summary.evidence, [source.evidence[0]]);
  assert.equal(summary.risk, source.risk);
  assert.equal(summary.recommendation, source.recommendation);
  assert.equal(Object.isFrozen(summary), true);
  assert.equal(Object.isFrozen(summary.facts), true);
  assert.equal(Object.isFrozen(summary.evidence), true);
  assert.doesNotMatch(JSON.stringify(summary), /AI 认为|爆发概率|预测增长|趋势百分比/);
});

test("omits an unavailable risk instead of inventing a low-risk conclusion", () => {
  const summary = projectDecisionSummary(projectFixture({ risk: "   " }));
  assert.equal(summary.risk, null);
  assert.doesNotMatch(JSON.stringify(summary), /低风险/);
});

test("keeps action, one-way Watch, and feedback independent by stable ID", async () => {
  const first = await identityForRepository("owner/foo.bar");
  const second = await identityForRepository("owner/foo-bar");
  const state = collectDecisionStateByProjectId(
    [
      { ...first, projectSlug: "legacy-collision", action: "saved" },
      { ...first, projectSlug: "legacy-collision", action: "tried" },
      { ...second, projectSlug: "legacy-collision", action: "reused" },
    ],
    [
      { ...first, projectSlug: "legacy-collision", value: "待确定" },
      { ...second, projectSlug: "legacy-collision", value: "有用" },
    ],
  );

  assert.equal(state.size, 2);
  assert.deepEqual(state.get(first.projectId).actions, ["saved", "tried"]);
  assert.deepEqual(decisionStatusForProject(state.get(first.projectId)), {
    stage: "tried",
    stageLabel: "已尝试",
    acted: true,
    watched: true,
    feedback: "待确定",
  });
  assert.deepEqual(decisionStatusForProject(state.get(second.projectId)), {
    stage: "reused",
    stageLabel: "已复用",
    acted: true,
    watched: false,
    feedback: "有用",
  });
});

test("feedback alone never becomes Watch state", async () => {
  const identity = await identityForRepository("owner/project");
  const state = collectDecisionStateByProjectId([], [{ ...identity, value: "待确定" }]);
  assert.deepEqual(decisionStatusForProject(state.get(identity.projectId)), {
    stage: null,
    stageLabel: "未处理",
    acted: false,
    watched: false,
    feedback: "待确定",
  });
});

test("accepts Decision State reads only when both APIs match the page generation", async () => {
  const identity = await identityForRepository("owner/project");
  const actions = [{ ...identity, action: "opened" }];
  const feedback = [{ ...identity, value: "有用" }];

  const current = mergeGenerationBoundDecisionReads(
    "generation-a",
    { generationId: "generation-a", actions },
    { generationId: "generation-a", feedback },
  );
  assert.equal(current.get(identity.projectId).actions[0], "opened");
  assert.equal(current.get(identity.projectId).feedback, "有用");

  for (const [actionGeneration, feedbackGeneration] of [
    ["generation-b", "generation-a"],
    ["generation-a", "generation-b"],
  ]) {
    assert.equal(mergeGenerationBoundDecisionReads(
      "generation-a",
      { generationId: actionGeneration, actions },
      { generationId: feedbackGeneration, feedback },
    ), null);
  }
  assert.equal(mergeGenerationBoundDecisionReads(
    "generation-a",
    { generationId: "generation-a", actions: null },
    { generationId: "generation-a", feedback },
  ), null);
});

test("applies successful mutation events without mutating an older read snapshot", async () => {
  const identity = await identityForRepository("owner/project");
  const initial = collectDecisionStateByProjectId(
    [{ ...identity, action: "opened" }],
    [{ ...identity, value: "待确定" }],
  );
  const afterAction = applyDecisionStateEvent(initial, { ...identity, action: "cloned" });
  const afterFeedback = applyDecisionStateEvent(afterAction, { ...identity, value: "复用" });

  assert.deepEqual(initial.get(identity.projectId).actions, ["opened"]);
  assert.equal(initial.get(identity.projectId).feedback, "待确定");
  assert.deepEqual(afterFeedback.get(identity.projectId).actions, ["opened", "cloned"]);
  assert.equal(afterFeedback.get(identity.projectId).feedback, "复用");
  assert.equal(decisionStatusForProject(afterFeedback.get(identity.projectId)).acted, true);
  assert.notStrictEqual(afterFeedback, initial);
});

test("replays only post-request mutations over a complete baseline without dropping peers", async () => {
  const first = await identityForRepository("owner/first-project");
  const second = await identityForRepository("owner/second-project");
  const baseline = collectDecisionStateByProjectId(
    [
      { ...first, action: "opened" },
      { ...second, action: "saved" },
    ],
    [
      { ...first, value: "待确定" },
      { ...second, value: "有用" },
    ],
  );

  const replayed = replayDecisionStateEvents(baseline, [
    { version: 2, detail: { ...second, action: "reused" } },
    { version: 3, detail: { ...first, action: "cloned" } },
    { version: 4, detail: { ...first, value: "复用" } },
  ], 2);

  assert.deepEqual(replayed.get(first.projectId).actions, ["opened", "cloned"]);
  assert.equal(replayed.get(first.projectId).feedback, "复用");
  assert.deepEqual(replayed.get(second.projectId), baseline.get(second.projectId));
  assert.deepEqual(baseline.get(first.projectId).actions, ["opened"]);
  assert.equal(baseline.get(first.projectId).feedback, "待确定");
  assert.notStrictEqual(replayed, baseline);
});

test("ignores mutation details without canonical stable identity", async () => {
  const identity = await identityForRepository("owner/project");
  const initial = collectDecisionStateByProjectId([], []);
  assert.strictEqual(
    applyDecisionStateEvent(initial, { projectSlug: "owner--project", action: "saved" }),
    initial,
  );
  assert.strictEqual(
    applyDecisionStateEvent(initial, { ...identity, projectIdVersion: 2, action: "saved" }),
    initial,
  );
});
