import { Nav } from "../components/Nav";
import { ProjectCard } from "../components/ProjectCard";
import { projectCategories } from "../data";
import { loadPublishedData } from "../server-data";

export const metadata = { title: "发现" };
export const dynamic = "force-dynamic";

export default async function DiscoverPage() {
  const { catalog, projects, snapshotNotice } = await loadPublishedData();
  const categories = projectCategories(projects);
  return (
    <div className="app-shell">
      <Nav growthMode={catalog.growthMode} />
      <main className="page-main">
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
        <section className="discover-grid">
          {projects.map((project) => <ProjectCard key={project.slug} project={project} />)}
        </section>
      </main>
    </div>
  );
}
