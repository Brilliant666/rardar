import Link from "next/link";
import { canonicalProjectPath } from "../client-project-identity.mjs";
import { formatNumber, type StableProject } from "../data";
import { ProjectDecisionSummary } from "./ProjectDecisionSummary";

export function ProjectCard({
  project,
  index,
  compact = false,
  rankingReason = "",
}: {
  project: StableProject;
  index?: number;
  compact?: boolean;
  rankingReason?: string;
}) {
  return (
    <article className={`project-card ${compact ? "compact" : ""}`}>
      <div className="project-card-topline">
        {typeof index === "number" && (
          <span className="rank">{String(index + 1).padStart(2, "0")}</span>
        )}
        <span className="category-pill">{project.category}</span>
        <span className={`heat-pill ${project.heatTrack ?? "recent_momentum"}`}>
          {project.heatLabel ?? (project.growthKind === "observed" ? "近期动量 · 实际区间" : "近期动量 · 首次代理")}
        </span>
        <span className="analysis-pill">{project.analysisState}</span>
      </div>
      {rankingReason && (
        <p className="ranking-reason"><span>偏好重排</span>{rankingReason}</p>
      )}
      <div className="project-card-main">
        <div>
          <Link className="repo-name" href={canonicalProjectPath(project)}>
            {project.repo}
          </Link>
          <h2>
            <Link href={canonicalProjectPath(project)}>{project.title}</Link>
          </h2>
          <p className="project-description">{project.description}</p>
        </div>
        <div className="score-stack" aria-label="项目评分">
          <div>
            <strong>{project.attentionScore}</strong>
            <span>关注优先级</span>
          </div>
          <div>
            <strong>{project.engineeringReadiness ?? "—"}</strong>
            <span>静态工程就绪度</span>
          </div>
        </div>
      </div>
      <div className="project-meta">
        <span>★ {formatNumber(project.stars)}</span>
        <span className={project.growthValue < 0 ? "trend-down" : "trend-up"} title={project.growthLabel}>{project.trend}</span>
        <span>{project.language}</span>
        <span>{project.license}</span>
      </div>
      <ProjectDecisionSummary
        project={project}
        variant={compact ? "compact" : "card"}
        showFeedback={!compact}
      />
    </article>
  );
}
