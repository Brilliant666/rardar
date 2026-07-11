import Link from "next/link";
import { Nav } from "./components/Nav";
import { SearchWorkbench } from "./components/SearchWorkbench";
import { DecisionMetrics } from "./components/DecisionMetrics";
import { SignalDigest } from "./components/SignalDigest";
import { PersonalizedDailyList } from "./components/PersonalizedDailyList";
import { catalog, dailyProjects, formatCapturedDate, formatNumber, snapshotNotice } from "./data";

export default function Home() {
  const leadProject = dailyProjects[0];
  const recentCount = catalog.dailyTrackCounts?.recentMomentum ?? 3;
  const longTermCount = catalog.dailyTrackCounts?.longTerm ?? 2;

  return (
    <div className="app-shell">
      <Nav />
      <main>
        <section className="hero">
          <div className="hero-copy">
            <span className="eyebrow">{formatCapturedDate(catalog.capturedAt)} · 已完成今日刷新</span>
            <h1>先看 5 个，<br />不用刷 500 个。</h1>
            <p>今天真正值得看的，不只是 Star 排名。Rardar 把近期爆发、长期高热和复用价值分开判断，让你先看到能解决问题的项目。</p>
            <div className="hero-actions">
              <Link className="primary-link" href="#daily-five">查看今日重点</Link>
              <Link className="secondary-link" href="/search">按任务找项目 <span>→</span></Link>
            </div>
          </div>
          <div className="hero-side">
            {leadProject ? (
              <Link className="hero-lead-card" href={`/projects/${leadProject.slug}`}>
                <div className="hero-lead-topline">
                  <span>今日 01 · {leadProject.heatTrack === "long_term" ? "长期高热" : "近期动量"}</span>
                  <b>{leadProject.recommendation} →</b>
                </div>
                <div>
                  <small>{leadProject.repo}</small>
                  <h2>{leadProject.title}</h2>
                  <p>{leadProject.description}</p>
                </div>
                <div className="hero-lead-metrics">
                  <div><strong>{leadProject.trend}</strong><span>区间增长</span></div>
                  <div><strong>{leadProject.reuseScore}</strong><span>复用价值</span></div>
                  <div><strong>{formatNumber(leadProject.stars)}</strong><span>累计 Star</span></div>
                </div>
              </Link>
            ) : (
              <div className="hero-lead-card hero-lead-empty">等待真实快照</div>
            )}
          </div>
        </section>

        <section className="briefing-strip" aria-label="今日雷达摘要">
          <div><span>今日重点</span><strong>{dailyProjects.length}</strong><small>{catalog.deepAnalysisCount} 个已完成中文深读</small></div>
          <div><span>近期动量</span><strong>{recentCount}</strong><small>基于区间 Star 变化</small></div>
          <div><span>长期高热</span><strong>{longTermCount}</strong><small>持续性与维护共同评分</small></div>
          <div><span>候选池</span><strong>{catalog.projectCount}</strong><small>{catalog.sourceCount} 条查询召回</small></div>
        </section>

        <section className="daily-section" id="daily-five">
          <div className="section-heading daily-heading">
            <div>
              <span className="section-label">Daily Five</span>
              <h2>今天最值得花时间的 5 个项目</h2>
              <p>{snapshotNotice}</p>
            </div>
            <div className="daily-track-guide" aria-label="每日重点构成">
              <span><i className="track-dot momentum" />{recentCount} 个近期动量</span>
              <span><i className="track-dot enduring" />{longTermCount} 个长期高热</span>
              <small>长期持续性将在累计 7 次快照后升级验证</small>
            </div>
          </div>
          <PersonalizedDailyList />
        </section>

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

        <DecisionMetrics />

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
