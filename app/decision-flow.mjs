import { isProjectId } from "./project-identity.mjs";

const ACTION_STAGE_ORDER = ["reused", "cloned", "tried", "opened"];
const ACTION_STAGE_LABELS = Object.freeze({
  opened: "已打开",
  tried: "已尝试",
  cloned: "已克隆",
  reused: "已复用",
});

function cleanText(value) {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function uniqueText(values, limit) {
  const result = [];
  for (const value of values) {
    const text = cleanText(value);
    if (!text || result.includes(text)) continue;
    result.push(text);
    if (result.length >= limit) break;
  }
  return result;
}

function stableRecord(value) {
  return Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && value.projectIdVersion === 1
    && isProjectId(value.projectId),
  );
}

export function projectDecisionSummary(project, options = {}) {
  if (!project || typeof project !== "object" || Array.isArray(project)) {
    throw new TypeError("project decision summary requires a project object");
  }
  const maxFacts = Number.isSafeInteger(options.maxFacts) ? Math.max(0, options.maxFacts) : 3;
  const maxEvidence = Number.isSafeInteger(options.maxEvidence)
    ? Math.max(0, options.maxEvidence)
    : 3;
  const explanations = project.scoreExplanations ?? {};
  const facts = uniqueText([
    ...(explanations.attention?.facts ?? []),
    ...(explanations.endurance?.facts ?? []),
    ...(explanations.engineeringReadiness?.facts ?? []),
    ...(explanations.evidenceCompleteness?.facts ?? []),
  ], maxFacts);
  const evidence = (Array.isArray(project.evidence) ? project.evidence : [])
    .filter((item) => (
      item
      && typeof item === "object"
      && cleanText(item.label)
      && cleanText(item.detail)
      && cleanText(item.href)
    ))
    .slice(0, maxEvidence)
    .map((item) => Object.freeze({
      label: item.label.trim(),
      detail: item.detail.trim(),
      href: item.href.trim(),
    }));

  return Object.freeze({
    whyNow: cleanText(project.whyNow) ?? "当前已验证数据没有提供 Why now 说明。",
    facts: Object.freeze(facts),
    evidence: Object.freeze(evidence),
    risk: cleanText(project.risk),
    recommendation: cleanText(project.recommendation) ?? "查看现有证据",
  });
}

function emptyDecisionState(projectId) {
  return {
    projectIdVersion: 1,
    projectId,
    actions: [],
    feedback: null,
  };
}

function copyDecisionState(value) {
  return {
    projectIdVersion: 1,
    projectId: value.projectId,
    actions: [...value.actions],
    feedback: value.feedback,
  };
}

export function collectDecisionStateByProjectId(actions, feedback) {
  const states = new Map();
  for (const record of Array.isArray(actions) ? actions : []) {
    if (!stableRecord(record) || typeof record.action !== "string") continue;
    const state = states.get(record.projectId) ?? emptyDecisionState(record.projectId);
    if (!state.actions.includes(record.action)) state.actions.push(record.action);
    states.set(record.projectId, state);
  }
  for (const record of Array.isArray(feedback) ? feedback : []) {
    if (!stableRecord(record) || typeof record.value !== "string") continue;
    const state = states.get(record.projectId) ?? emptyDecisionState(record.projectId);
    state.feedback = record.value;
    states.set(record.projectId, state);
  }
  return new Map([...states].map(([projectId, state]) => [projectId, Object.freeze({
    ...state,
    actions: Object.freeze([...state.actions]),
  })]));
}

export function mergeGenerationBoundDecisionReads(
  expectedGenerationId,
  actionPayload,
  feedbackPayload,
) {
  if (
    typeof expectedGenerationId !== "string"
    || !expectedGenerationId
    || !actionPayload
    || !feedbackPayload
    || actionPayload.generationId !== expectedGenerationId
    || feedbackPayload.generationId !== expectedGenerationId
    || !Array.isArray(actionPayload.actions)
    || !Array.isArray(feedbackPayload.feedback)
  ) {
    return null;
  }
  return collectDecisionStateByProjectId(actionPayload.actions, feedbackPayload.feedback);
}

export function applyDecisionStateEvent(current, detail) {
  if (!(current instanceof Map) || !stableRecord(detail)) return current;
  const next = new Map(current);
  const previous = copyDecisionState(
    next.get(detail.projectId) ?? emptyDecisionState(detail.projectId),
  );
  if (typeof detail.action === "string" && !previous.actions.includes(detail.action)) {
    previous.actions.push(detail.action);
  }
  if (typeof detail.value === "string") previous.feedback = detail.value;
  next.set(detail.projectId, Object.freeze({
    ...previous,
    actions: Object.freeze([...previous.actions]),
  }));
  return next;
}

export function replayDecisionStateEvents(current, entries, afterVersion) {
  let next = current;
  for (const entry of Array.isArray(entries) ? entries : []) {
    if (
      entry
      && typeof entry === "object"
      && Number.isSafeInteger(entry.version)
      && entry.version > afterVersion
    ) {
      next = applyDecisionStateEvent(next, entry.detail);
    }
  }
  return next;
}

export function decisionStatusForProject(state) {
  const actions = Array.isArray(state?.actions) ? state.actions : [];
  const highestStage = ACTION_STAGE_ORDER.find((action) => actions.includes(action)) ?? null;
  return Object.freeze({
    stage: highestStage,
    stageLabel: highestStage ? ACTION_STAGE_LABELS[highestStage] : "未处理",
    acted: highestStage === "tried" || highestStage === "cloned" || highestStage === "reused",
    watched: actions.includes("saved"),
    feedback: cleanText(state?.feedback),
  });
}
