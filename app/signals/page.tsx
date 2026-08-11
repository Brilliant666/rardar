import { Nav } from "../components/Nav";
import { RuntimeStatus } from "../components/RuntimeStatus";
import { DataFreshnessNotice } from "../components/DataFreshnessNotice";
import { SignalCard } from "../components/SignalCard";
import { formatSignalTime } from "../signals";
import { loadPublishedData } from "../server-data";

export const metadata = { title: "技术动态" };
export const dynamic = "force-dynamic";

export default async function SignalsPage() {
  const { generationId, dataFreshness, catalog, codexQueue, signalSnapshot } = await loadPublishedData();
  return (
    <div className="app-shell" data-generation={generationId} data-freshness={dataFreshness.freshness}>
      <Nav growthMode={catalog.growthMode} />
      <main className="page-main signals-page">
        <DataFreshnessNotice dataFreshness={dataFreshness} />
        <header className="page-hero compact-hero">
          <span className="eyebrow">AI & Tech signals</span>
          <h1>大新闻、官方更新，<br />和正在起飞的项目。</h1>
          <p>官方订阅源优先；聚合站、人工精选和第三方榜单作为补充。每条动态保留原始入口、时间和来源类型。当前有 {codexQueue.projectPendingCount} 个项目和 {codexQueue.signalPendingCount} 条动态等待本地 Codex 深读。</p>
        </header>

        <section className="source-health" aria-label="信源健康状态">
          <div className="source-health-summary">
            <span className="section-label">Source health</span>
            <strong>{signalSnapshot.healthySourceCount}/{signalSnapshot.sourceStatus.length}</strong>
            <p>信源健康 · 最近采集 {formatSignalTime(signalSnapshot.capturedAt)}</p>
          </div>
          <div className="source-health-list">
            {signalSnapshot.sourceStatus.map((source) => (
              <a href={source.url} target="_blank" rel="noreferrer" key={source.id}>
                <span className={source.state === "healthy" ? "source-ok" : "source-failed"}>{source.state === "healthy" ? "正常" : "失败"}</span>
                <strong>{source.name}</strong>
                <small>{source.itemCount} 条 · {source.latestItemAt ? formatSignalTime(source.latestItemAt) : "无新条目"}</small>
              </a>
            ))}
          </div>
          <RuntimeStatus />
        </section>

        <section className="all-signals">
          <div className="section-heading">
          <span className="section-label">{dataFreshness.freshness === "stale" ? "Last verified stream" : "48-hour stream"}</span>
            <h2>{dataFreshness.freshness === "stale" ? "最近一次已验证动态" : "全部去重动态"}</h2>
          </div>
          {signalSnapshot.signals.length ? <div className="signal-list">
            {signalSnapshot.signals.map((signal, index) => <SignalCard key={signal.id} signal={signal} index={index} />)}
          </div> : <div className="empty-state compact-empty"><span>0</span><h2>当前没有可用动态</h2><p>已验证 generation 中没有技术动态，Rardar 不会从未发布内容补位。</p></div>}
        </section>
      </main>
    </div>
  );
}
