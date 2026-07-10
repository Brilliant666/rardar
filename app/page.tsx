import Link from "next/link";
import { Nav } from "./components/Nav";
import { SearchWorkbench } from "./components/SearchWorkbench";
import { DecisionMetrics } from "./components/DecisionMetrics";
import { SignalDigest } from "./components/SignalDigest";
import { PersonalizedDailyList } from "./components/PersonalizedDailyList";
import { catalog, dailyProjects, formatCapturedDate, formatNumber, snapshotNotice } from "./data";

export default function Home() {
  const leadProject = dailyProjects[0];

  return (
    <div className="app-shell">
      <Nav />
      <main>
        <section className="hero">
          <div className="hero-copy">
            <span className="eyebrow">{formatCapturedDate(catalog.capturedAt)} · 今日开源情报</span>
            <h1>今天真正值得看的，<br />不只是 Star 排名。</h1>
            <p>从全球热点中找出有证据、有实现、能复用的项目。先理解它为什么重要，再决定是否值得你的时间。</p>
          </div>
          <div className="hero-side">
            <div className="signal-card">
              <span>全局最强信号</span>
              <strong>{leadProject ? formatNumber(leadProject.growthValue) : "—"}</strong>
              <p>{leadProject?.repo ?? "等待真实快照"}<br />{leadProject?.growthLabel ?? "暂无增长信号"}</p>
            </div>
            <div className="hero-metrics">
              <div><strong>5</strong><span>重点</span></div>
              <div><strong>{catalog.sourceCount}</strong><span>召回</span></div>
              <div><strong>{catalog.projectCount}</strong><span>候选</span></div>
            </div>
          </div>
        </section>

        <DecisionMetrics />

        <SignalDigest />

        <section className="home-search">
          <div className="section-heading inline-heading">
            <div>
              <span className="section-label">任务侦察</span>
              <h2>别从零开始，先找已经实现的部分</h2>
            </div>
            <Link href="/search">打开完整工作台 →</Link>
          </div>
          <SearchWorkbench compact />
        </section>

        <section className="daily-section">
          <div className="section-heading">
            <span className="section-label">Daily Five</span>
            <h2>今天最值得花时间的 5 条线索</h2>
            <p>{snapshotNotice}</p>
          </div>
          <PersonalizedDailyList />
        </section>

        <section className="evidence-manifesto">
          <div>
            <span className="section-label">Evidence first</span>
            <h2>推荐必须能回答：<br />依据是什么？</h2>
          </div>
          <div className="manifesto-grid">
            <div><span>01</span><strong>事实与判断分开</strong><p>数据、来源和采集时间清晰可见，AI 推断明确标注。</p></div>
            <div><span>02</span><strong>功能优先于技术栈</strong><p>先判断能解决什么问题，再讨论语言和集成成本。</p></div>
            <div><span>03</span><strong>低热度也能高价值</strong><p>复用价值独立评分，不让真正有用的项目被流量淹没。</p></div>
          </div>
        </section>
      </main>
      <footer className="site-footer">
        <strong>Rardar</strong>
        <p>开源情报与项目复用雷达 · 首版产品原型</p>
      </footer>
    </div>
  );
}
