import Link from "next/link";
import { Nav } from "../components/Nav";
import { projects, formatNumber, snapshotNotice } from "../data";

export const metadata = { title: "候选池" };

export default function CandidatesPage() {
  return (
    <div className="app-shell">
      <Nav />
      <main className="page-main">
        <header className="page-hero compact-hero">
          <span className="eyebrow">Candidate pool</span>
          <h1>没进前五，<br />但仍值得看。</h1>
          <p>{snapshotNotice}</p>
        </header>
        <section className="candidate-table" aria-label="项目候选列表">
          <div className="candidate-header">
            <span>项目</span><span>类型</span><span>趋势</span><span>影响</span><span>复用</span><span>建议</span>
          </div>
          {projects.map((project) => (
            <Link href={`/projects/${project.slug}`} key={project.slug} className="candidate-row">
              <div><strong>{project.repo}</strong><small>★ {formatNumber(project.stars)} · {project.language}</small></div>
              <span>{project.category}</span>
              <span className={project.growthValue < 0 ? "trend-down" : "trend-up"}>{project.trend}</span>
              <b>{project.globalScore}</b>
              <b>{project.reuseScore}</b>
              <span>{project.recommendation}</span>
            </Link>
          ))}
        </section>
      </main>
    </div>
  );
}
