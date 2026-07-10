import { Nav } from "../components/Nav";
import { SearchWorkbench } from "../components/SearchWorkbench";

export const metadata = { title: "找项目" };

export default function SearchPage() {
  return (
    <div className="app-shell dark-page">
      <Nav />
      <main className="page-main search-page-main">
        <header className="page-hero search-page-hero">
          <span className="eyebrow">Task Scout</span>
          <h1>说出目标，<br />让项目自己浮现。</h1>
          <p>系统会拆解功能、寻找完整产品和可复用模块，并说明为什么匹配。技术栈只是集成成本，不是第一道筛选门槛。</p>
        </header>
        <SearchWorkbench />
        <section className="search-principles">
          <div><span>完整产品</span><p>优先寻找已经覆盖主要流程、可直接试用的项目。</p></div>
          <div><span>可复用模块</span><p>识别 SDK、CLI、API 和可独立拆分的核心能力。</p></div>
          <div><span>组合方案</span><p>一个仓库不够时，给出多个项目的组合路线与风险。</p></div>
        </section>
      </main>
    </div>
  );
}
