import Link from "next/link";
import { Nav } from "../components/Nav";
import { formatNumber } from "../data";
import { loadPublishedData } from "../server-data";

export const metadata = { title: "候选池" };
export const dynamic = "force-dynamic";

export default async function CandidatesPage() {
  const { catalog, projects, snapshotNotice } = await loadPublishedData();
  return (
    <div className="app-shell">
      <Nav growthMode={catalog.growthMode} />
      <main className="page-main">
        <header className="page-hero compact-hero">
          <span className="eyebrow">Candidate pool</span>
          <h1>没进前五，<br />但仍值得看。</h1>
          <p>{snapshotNotice}</p>
        </header>
        <section className="candidate-table" aria-label="项目候选列表">
          <div className="candidate-header">
            <span>项目</span><span>类型</span><span>趋势</span><span>关注</span><span>静态就绪</span><span>建议</span>
          </div>
          {projects.map((project) => (
            <Link href={`/projects/${project.slug}`} key={project.slug} className="candidate-row">
              <div><strong>{project.repo}</strong><small>★ {formatNumber(project.stars)} · {project.language}</small></div>
              <span>{project.category}<small>{project.heatLabel ?? "近期动量"}</small></span>
              <span className={project.growthValue < 0 ? "trend-down" : "trend-up"}>{project.trend}</span>
              <b>{project.attentionScore}</b>
              <b>{project.engineeringReadiness ?? "—"}</b>
              <span>{project.recommendation}</span>
            </Link>
          ))}
        </section>
      </main>
    </div>
  );
}
