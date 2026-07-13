import Link from "next/link";
import { notFound } from "next/navigation";
import { Nav } from "../../components/Nav";
import { FeedbackButtons } from "../../components/FeedbackButtons";
import { ProjectActions, TrackedRepositoryLink } from "../../components/ProjectActions";
import { formatNumber, getProject } from "../../data";
import { SCORE_DIMENSION_KEYS, SCORE_DIMENSION_LABELS } from "../../score-semantics.mjs";
import { loadPublishedData } from "../../server-data";

export const dynamic = "force-dynamic";

export default async function ProjectPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const { catalog, projects } = await loadPublishedData();
  const project = getProject(projects, slug);
  if (!project) notFound();

  return (
    <div className="app-shell">
      <Nav growthMode={catalog.growthMode} />
      <main className="project-page">
        <div className="project-breadcrumb"><Link href="/discover">发现</Link><span>/</span><span>{project.repo}</span></div>
        <header className="project-detail-hero">
          <div>
            <div className="project-card-topline"><span className="category-pill">{project.category}</span><span className={`heat-pill ${project.heatTrack ?? "recent_momentum"}`}>{project.heatLabel ?? (project.growthKind === "observed" ? "近期动量 · 实际区间" : "近期动量 · 首次代理")}</span><span className="analysis-pill">{project.analysisState}</span></div>
            <TrackedRepositoryLink projectSlug={project.slug} repository={project.repo} />
            <h1>{project.title}</h1>
            <p>{project.description}</p>
            <FeedbackButtons projectSlug={project.slug} />
            <ProjectActions key={project.slug} projectSlug={project.slug} />
          </div>
          <div className="detail-score-panel">
            <div><strong>{project.attentionScore}</strong><span>关注优先级</span></div>
            <div><strong>{project.engineeringReadiness ?? "—"}</strong><span>静态工程就绪度</span></div>
            <div><strong>{project.enduranceScore ?? "—"}</strong><span>持久热度</span></div>
            <div className="detail-stat"><span>★ {formatNumber(project.stars)}</span><span className={project.growthValue < 0 ? "trend-down" : "trend-up"} title={project.growthLabel}>{project.trend}</span></div>
          </div>
        </header>

        <section className="project-detail-grid">
          <div className="detail-main">
            <div className="detail-block highlight-block"><span className="section-label">Why now</span><h2>为什么现在值得看</h2><p>{project.whyNow}</p></div>
            <div className="detail-block score-explanation-block">
              <span className="section-label">Score semantics</span>
              <h2>五类评分分别说明什么</h2>
              <div className="score-explanation-list">
                {SCORE_DIMENSION_KEYS.map((dimension) => {
                  const explanation = project.scoreExplanations[dimension];
                  return (
                    <article key={dimension} className="score-explanation-item">
                      <header>
                        <span>{SCORE_DIMENSION_LABELS[dimension]}</span>
                        <strong>{explanation.score ?? "—"}</strong>
                      </header>
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
            <div className="detail-block"><span className="section-label">Capabilities</span><h2>实现了什么能力</h2><div className="capability-list large">{project.capabilities.map((item) => <span key={item}>{item}</span>)}</div></div>
            <div className="detail-block"><span className="section-label">Evidence</span><h2>结论依据</h2><div className="evidence-list">{project.evidence.map((item, index) => <a href={item.href} target="_blank" rel="noreferrer" key={`${item.href}-${index}`}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{item.label}</strong><p>{item.detail}</p></div><b>查看来源 ↗</b></a>)}</div></div>
          </div>
          <aside className="detail-sidebar">
            <div><span>建议行动</span><strong className="big-action">{project.recommendation}</strong></div>
            <div><span>适用场景假设</span><p>{project.fitHypothesis}</p></div>
            <div><span>风险与未知</span><p>{project.risk}</p></div>
            <div className="fact-grid"><p><span>语言</span>{project.language}</p><p><span>许可证</span>{project.license}</p><p><span>增长口径</span>{project.growthLabel}</p><p><span>采集时间</span>{project.capturedAt}</p>{project.heatObservationWindow ? <p><span>热度观察</span>{project.heatObservationCount ?? 0}/{project.heatObservationWindow} 次快照</p> : null}</div>
          </aside>
        </section>
      </main>
    </div>
  );
}
