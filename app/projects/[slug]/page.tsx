import Link from "next/link";
import { notFound } from "next/navigation";
import { Nav } from "../../components/Nav";
import { FeedbackButtons } from "../../components/FeedbackButtons";
import { ProjectActions, TrackedRepositoryLink } from "../../components/ProjectActions";
import { formatNumber, getProject, projects } from "../../data";

export function generateStaticParams() {
  return projects.map((project) => ({ slug: project.slug }));
}

export default async function ProjectPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const project = getProject(slug);
  if (!project) notFound();

  return (
    <div className="app-shell">
      <Nav />
      <main className="project-page">
        <div className="project-breadcrumb"><Link href="/discover">发现</Link><span>/</span><span>{project.repo}</span></div>
        <header className="project-detail-hero">
          <div>
            <div className="project-card-topline"><span className="category-pill">{project.category}</span><span className="analysis-pill">{project.analysisState}</span></div>
            <TrackedRepositoryLink projectSlug={project.slug} repository={project.repo} />
            <h1>{project.title}</h1>
            <p>{project.description}</p>
            <FeedbackButtons projectSlug={project.slug} />
            <ProjectActions projectSlug={project.slug} />
          </div>
          <div className="detail-score-panel">
            <div><strong>{project.globalScore}</strong><span>全球影响力</span></div>
            <div><strong>{project.reuseScore}</strong><span>复用价值</span></div>
            <div className="detail-stat"><span>★ {formatNumber(project.stars)}</span><span className="trend-up" title={project.growthLabel}>{project.trend}</span></div>
          </div>
        </header>

        <section className="project-detail-grid">
          <div className="detail-main">
            <div className="detail-block highlight-block"><span className="section-label">Why now</span><h2>为什么现在值得看</h2><p>{project.whyNow}</p></div>
            <div className="detail-block"><span className="section-label">Capabilities</span><h2>实现了什么能力</h2><div className="capability-list large">{project.capabilities.map((item) => <span key={item}>{item}</span>)}</div></div>
            <div className="detail-block"><span className="section-label">Evidence</span><h2>结论依据</h2><div className="evidence-list">{project.evidence.map((item, index) => <a href={item.href} target="_blank" rel="noreferrer" key={`${item.href}-${index}`}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{item.label}</strong><p>{item.detail}</p></div><b>查看来源 ↗</b></a>)}</div></div>
          </div>
          <aside className="detail-sidebar">
            <div><span>建议行动</span><strong className="big-action">{project.recommendation}</strong></div>
            <div><span>适合怎么用</span><p>{project.fit}</p></div>
            <div><span>风险与未知</span><p>{project.risk}</p></div>
            <div className="fact-grid"><p><span>语言</span>{project.language}</p><p><span>许可证</span>{project.license}</p><p><span>增长口径</span>{project.growthLabel}</p><p><span>采集时间</span>{project.capturedAt}</p></div>
          </aside>
        </section>
      </main>
    </div>
  );
}
