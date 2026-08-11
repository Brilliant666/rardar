import { Nav } from "../components/Nav";
import { DataFreshnessNotice } from "../components/DataFreshnessNotice";
import { DecisionStateProvider } from "../components/DecisionStateProvider";
import { ProjectCard } from "../components/ProjectCard";
import { projectCategories } from "../data";
import { loadPublishedData } from "../server-data";

export const metadata = { title: "发现" };
export const dynamic = "force-dynamic";

export default async function DiscoverPage() {
  const { generationId, dataFreshness, catalog, projects, snapshotNotice } = await loadPublishedData();
  const categories = projectCategories(projects);
  return (
    <div className="app-shell" data-generation={generationId} data-freshness={dataFreshness.freshness}>
      <DecisionStateProvider key={generationId} generationId={generationId}>
        <Nav growthMode={catalog.growthMode} />
        <main className="page-main">
        <DataFreshnessNotice dataFreshness={dataFreshness} />
        <header className="page-hero">
          <span className="eyebrow">Discover</span>
          <h1>发现正在起飞，<br />也长期高热的项目</h1>
          <p>{snapshotNotice}</p>
        </header>
        <div className="category-row" aria-label="项目分类">
          {categories.map((category, index) => (
            <span className={index === 0 ? "active" : ""} key={category}>{category}</span>
          ))}
        </div>
        {projects.length ? <section className="discover-grid">
          {projects.map((project) => <ProjectCard key={project.projectId} project={project} />)}
        </section> : <div className="empty-state compact-empty">
          <span>0</span>
          <h2>当前没有可发现的项目</h2>
          <p>已验证 Catalog 为空，Rardar 不会从候选或 flat data 补造项目。</p>
        </div>}
        </main>
      </DecisionStateProvider>
    </div>
  );
}
