import { Nav } from "../components/Nav";
import { SearchWorkbench } from "../components/SearchWorkbench";
import { loadPublishedData } from "../server-data";

export const metadata = { title: "找项目" };
export const dynamic = "force-dynamic";

export default async function SearchPage() {
  const { catalog, projects } = await loadPublishedData();
  return (
    <div className="app-shell search-page">
      <Nav growthMode={catalog.growthMode} />
      <main className="page-main search-page-main">
        <header className="page-hero search-page-hero">
          <div className="search-hero-copy">
            <span className="eyebrow">Find projects · Task scout</span>
            <h1>站在已有成果上，<br />更快抵达目标。</h1>
            <p>描述你想完成的任务。Rardar 会拆解能力、检索已有产品与模块，再用真实增长、静态分析和任务匹配证据解释为什么值得验证。</p>
          </div>
          <div className="radar-field" aria-hidden="true">
            <span className="radar-core">✦</span>
            <i className="radar-ring ring-one" />
            <i className="radar-ring ring-two" />
            <i className="radar-ring ring-three" />
            <b className="radar-point point-one" />
            <b className="radar-point point-two" />
            <b className="radar-point point-three" />
          </div>
        </header>
        <SearchWorkbench projects={projects} />
        <section className="search-principles">
          <div><span>01 · 完整产品</span><p>优先寻找已经覆盖主要流程、可直接试用的项目。</p></div>
          <div><span>02 · 可复用模块</span><p>识别 SDK、CLI、API 和可独立拆分的核心能力。</p></div>
          <div><span>03 · 组合方案</span><p>一个仓库不够时，给出多个项目的组合路线与风险。</p></div>
        </section>
      </main>
    </div>
  );
}
