import { Nav } from "../components/Nav";
import { DataFreshnessNotice } from "../components/DataFreshnessNotice";
import { DecisionStateProvider } from "../components/DecisionStateProvider";
import { WatchlistClient } from "../components/WatchlistClient";
import { loadPublishedData } from "../server-data";

export const metadata = { title: "观察列表" };
export const dynamic = "force-dynamic";

export default async function WatchlistPage() {
  const { generationId, dataFreshness, catalog, projects } = await loadPublishedData();
  return (
    <div className="app-shell" data-generation={generationId} data-freshness={dataFreshness.freshness}>
      <DecisionStateProvider key={generationId} generationId={generationId}>
        <Nav growthMode={catalog.growthMode} />
        <main className="page-main">
        <DataFreshnessNotice dataFreshness={dataFreshness} />
        <header className="page-hero compact-hero">
          <span className="eyebrow">Watch later</span>
          <h1>值得回访的项目，<br />不要散落在记忆里。</h1>
          <p>这里仅显示你明确关注的项目。推荐质量反馈保持独立，不会自动改变观察列表。</p>
        </header>
        <WatchlistClient projects={projects} />
        </main>
      </DecisionStateProvider>
    </div>
  );
}
