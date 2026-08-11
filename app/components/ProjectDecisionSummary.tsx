"use client";

import Link from "next/link";
import { canonicalProjectPath } from "../client-project-identity.mjs";
import {
  decisionStatusForProject,
  projectDecisionSummary,
} from "../decision-flow.mjs";
import type { StableProject } from "../data";
import { useDecisionState } from "./DecisionStateProvider";
import { FeedbackButtons } from "./FeedbackButtons";
import { WatchButton } from "./WatchButton";

export function ProjectDecisionStatus({ project }: { project: StableProject }) {
  const { state, loading, error, staleGeneration } = useDecisionState(project.projectId);
  const status = decisionStatusForProject(state);
  return (
    <div
      className={status.acted ? "decision-status acted" : "decision-status"}
      data-acted={status.acted ? "true" : "false"}
    >
      <span>{loading
        ? "状态读取中"
        : error
          ? staleGeneration ? "数据已更新，需刷新" : "状态暂不可用"
          : status.stageLabel}</span>
      <span>{status.watched ? "已关注" : "未关注"}</span>
      {status.feedback ? <span>反馈：{status.feedback}</span> : null}
    </div>
  );
}

export function ProjectDecisionSummary({
  project,
  variant = "card",
  showFeedback = variant === "card",
  showControls = true,
}: {
  project: StableProject;
  variant?: "hero" | "card" | "compact" | "detail";
  showFeedback?: boolean;
  showControls?: boolean;
}) {
  const detail = variant === "detail";
  const compact = variant === "compact" || variant === "hero";
  const summary = projectDecisionSummary(project, {
    maxFacts: compact ? 2 : 3,
    maxEvidence: detail ? project.evidence.length : compact ? 1 : 2,
  });

  return (
    <section className={`project-decision-summary decision-${variant}`} aria-label={`${project.title} 决策摘要`}>
      <div className="decision-section decision-why">
        <h3>为什么现在值得看</h3>
        <p>{summary.whyNow}</p>
        {summary.facts.length ? (
          <ul>{summary.facts.map((fact) => <li key={fact}>{fact}</li>)}</ul>
        ) : null}
      </div>
      <div className="decision-section decision-evidence">
        <h3>关键证据</h3>
        {summary.evidence.length ? (
          <div>
            {summary.evidence.map((item) => (
              <a href={item.href} target="_blank" rel="noreferrer" key={`${item.href}:${item.label}`}>
                <strong>{item.label}</strong>
                <small>{item.detail}</small>
              </a>
            ))}
          </div>
        ) : <p className="decision-empty">当前没有可展开的证据入口。</p>}
      </div>
      <div className="decision-section decision-risk">
        <h3>风险 / 注意事项</h3>
        <p>{summary.risk ?? "暂无结构化风险说明；不要把信息缺失理解为低风险。"}</p>
      </div>
      <div className="decision-next-step">
        <div>
          <span>下一步</span>
          <strong>{summary.recommendation}</strong>
          <ProjectDecisionStatus project={project} />
        </div>
        {showControls ? <div className="decision-controls">
          <WatchButton
            projectIdVersion={project.projectIdVersion}
            projectId={project.projectId}
          />
          <Link className="decision-detail-link" href={canonicalProjectPath(project)}>查看详情与证据</Link>
        </div> : null}
      </div>
      {showFeedback ? (
        <details className="decision-feedback">
          <summary>评价这条推荐</summary>
          <p>反馈只用于改进推荐质量，不代表你已经采取工程行动。</p>
          <FeedbackButtons
            projectIdVersion={project.projectIdVersion}
            projectId={project.projectId}
          />
        </details>
      ) : null}
    </section>
  );
}
