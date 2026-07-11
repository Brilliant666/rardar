import Link from "next/link";
import { formatNumber, type Project } from "../data";
import { FeedbackButtons } from "./FeedbackButtons";

export function ProjectCard({
  project,
  index,
  compact = false,
  rankingReason = "",
}: {
  project: Project;
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
          <Link className="repo-name" href={`/projects/${project.slug}`}>
            {project.repo}
          </Link>
          <h2>
            <Link href={`/projects/${project.slug}`}>{project.title}</Link>
          </h2>
          <p className="project-description">{project.description}</p>
        </div>
        <div className="score-stack" aria-label="项目评分">
          <div>
            <strong>{project.globalScore}</strong>
            <span>全球影响</span>
          </div>
          <div>
            <strong>{project.reuseScore}</strong>
            <span>复用价值</span>
          </div>
        </div>
      </div>
      {!compact && <p className="why-now"><span>为什么现在</span>{project.whyNow}</p>}
      <div className="project-meta">
        <span>★ {formatNumber(project.stars)}</span>
        <span className={project.growthValue < 0 ? "trend-down" : "trend-up"} title={project.growthLabel}>{project.trend}</span>
        <span>{project.language}</span>
        <span>{project.license}</span>
        <span className="action-tag">建议：{project.recommendation}</span>
      </div>
      {!compact && <FeedbackButtons projectSlug={project.slug} />}
    </article>
  );
}
