import Link from "next/link";
import { notFound } from "next/navigation";
import { DataFreshnessNotice } from "../../../components/DataFreshnessNotice";
import { DecisionStateProvider } from "../../../components/DecisionStateProvider";
import { FeedbackButtons } from "../../../components/FeedbackButtons";
import { Nav } from "../../../components/Nav";
import { ProjectActions, TrackedRepositoryLink } from "../../../components/ProjectActions";
import { ProjectDecisionSummary } from "../../../components/ProjectDecisionSummary";
import { WatchButton } from "../../../components/WatchButton";
import { formatNumber, getProjectById } from "../../../data";
import {
  ProjectIdentityError,
  resolveProjectSelector,
} from "../../../project-identity.mjs";
import { SCORE_DIMENSION_KEYS, SCORE_DIMENSION_LABELS } from "../../../score-semantics.mjs";
import { loadPublishedData } from "../../../server-data";

export const dynamic = "force-dynamic";

export default async function ProjectPage({
  params,
}: {
  params: Promise<{ projectIdVersion: string; projectId: string }>;
}) {
  const { projectIdVersion, projectId } = await params;
  if (projectIdVersion !== "v1") notFound();
  const {
    generationId,
    dataFreshness,
    catalog,
    identityContext,
    projects,
  } = await loadPublishedData();
  try {
    resolveProjectSelector(identityContext, { projectIdVersion: 1, projectId });
  } catch (error) {
    if (error instanceof ProjectIdentityError && error.status < 500) notFound();
    throw error;
  }
  const project = getProjectById(projects, projectId);
  if (!project) notFound();

  return (
    <div
      className="app-shell"
      data-generation={generationId}
      data-freshness={dataFreshness.freshness}
      data-project-id={project.projectId}
    >
      <DecisionStateProvider key={generationId} generationId={generationId}>
        <Nav growthMode={catalog.growthMode} />
        <main className="project-page">
          <DataFreshnessNotice dataFreshness={dataFreshness} />
          <div className="project-breadcrumb"><Link href="/discover">发现</Link><span>/</span><span>{project.repo}</span></div>
          <header className="project-detail-hero">
            <div>
              <div className="project-card-topline">
                <span className="category-pill">{project.category}</span>
                <span className={`heat-pill ${project.heatTrack ?? "recent_momentum"}`}>
                  {project.heatLabel ?? (project.growthKind === "observed" ? "近期动量 · 实际区间" : "近期动量 · 首次代理")}
                </span>
                <span className="analysis-pill">{project.analysisState}</span>
              </div>
              <span className="repo-name">{project.repo}</span>
              <h1>{project.title}</h1>
              <p>{project.description}</p>
            </div>
            <div className="detail-score-panel" aria-label="已有评分事实">
              <div><strong>{project.attentionScore}</strong><span>关注优先级</span></div>
              <div><strong>{project.engineeringReadiness ?? "—"}</strong><span>静态工程就绪度</span></div>
              <div><strong>{project.enduranceScore ?? "—"}</strong><span>持久热度</span></div>
              <div className="detail-stat"><span>★ {formatNumber(project.stars)}</span><span className={project.growthValue < 0 ? "trend-down" : "trend-up"} title={project.growthLabel}>{project.trend}</span></div>
            </div>
          </header>

          <section className="project-decision-flow" aria-label="项目决策路径">
            <ProjectDecisionSummary
              project={project}
              variant="detail"
              showFeedback={false}
              showControls={false}
            />
            <section className="decision-action-panel" aria-labelledby="decision-action-title">
              <header>
                <div><span className="section-label">Next action</span><h2 id="decision-action-title">决定下一步，而不是只看分数</h2></div>
                <strong>{project.recommendation}</strong>
              </header>
              <div className="decision-primary-actions">
                <TrackedRepositoryLink
                  projectIdVersion={project.projectIdVersion}
                  projectId={project.projectId}
                  repository={project.repo}
                />
                <WatchButton
                  projectIdVersion={project.projectIdVersion}
                  projectId={project.projectId}
                />
              </div>
              <ProjectActions
                key={`actions:${project.projectId}`}
                projectIdVersion={project.projectIdVersion}
                projectId={project.projectId}
              />
              <details className="decision-feedback detail-feedback">
                <summary>评价 Rardar 的推荐判断</summary>
                <p>这项反馈用于改进排序，不会改变你的行动或关注状态。</p>
                <FeedbackButtons
                  key={`feedback:${project.projectId}`}
                  projectIdVersion={project.projectIdVersion}
                  projectId={project.projectId}
                />
              </details>
            </section>
          </section>

          <section className="project-detail-grid" aria-label="支持信息">
            <div className="detail-main">
              <div className="detail-block score-explanation-block">
                <span className="section-label">Score semantics</span>
                <h2>五类评分分别说明什么</h2>
                <div className="score-explanation-list">
                  {SCORE_DIMENSION_KEYS.map((dimension) => {
                    const explanation = project.scoreExplanations[dimension];
                    return (
                      <article key={dimension} className="score-explanation-item">
                        <header><span>{SCORE_DIMENSION_LABELS[dimension]}</span><strong>{explanation.score ?? "—"}</strong></header>
                        <p>{explanation.summary}</p>
                        <dl>
                          <div><dt>事实</dt><dd>{explanation.facts.length ? explanation.facts.join("；") : "暂无直接事实"}</dd></div>
                          <div><dt>代理</dt><dd>{explanation.proxies.length ? explanation.proxies.join("；") : "未使用代理"}</dd></div>
                          <div><dt>未知</dt><dd>{explanation.limitations.length ? explanation.limitations.join("；") : "暂无额外限制"}</dd></div>
                          <div><dt>升级条件</dt><dd>{explanation.upgradeConditions.length ? explanation.upgradeConditions.join("；") : "暂无"}</dd></div>
                        </dl>
                      </article>
                    );
                  })}
                </div>
              </div>
              <div className="detail-block">
                <span className="section-label">Capabilities</span>
                <h2>实现了什么能力</h2>
                {project.capabilities.length
                  ? <div className="capability-list large">{project.capabilities.map((item) => <span key={item}>{item}</span>)}</div>
                  : <p className="decision-empty">当前没有经过验证的能力画像。</p>}
              </div>
            </div>
            <aside className="detail-sidebar" aria-label="项目补充事实">
              <div><span>适用场景假设</span><p>{project.fitHypothesis}</p></div>
              <div><span>复用验证建议</span><p>{project.reusePlan}</p></div>
              <div className="fact-grid">
                <p><span>语言</span>{project.language}</p>
                <p><span>许可证</span>{project.license}</p>
                <p><span>增长口径</span>{project.growthLabel}</p>
                <p><span>采集时间</span>{project.capturedAt}</p>
                {project.heatObservationWindow ? <p><span>热度观察</span>{project.heatObservationCount ?? 0}/{project.heatObservationWindow} 次快照</p> : null}
              </div>
            </aside>
          </section>
        </main>
      </DecisionStateProvider>
    </div>
  );
}
