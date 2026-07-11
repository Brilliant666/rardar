import { Nav } from "../components/Nav";
import { WatchlistClient } from "../components/WatchlistClient";

export const metadata = { title: "观察列表" };

export default function WatchlistPage() {
  return (
    <div className="app-shell">
      <Nav />
      <main className="page-main">
        <header className="page-hero compact-hero">
          <span className="eyebrow">Watch later</span>
          <h1>值得回访的项目，<br />不要散落在记忆里。</h1>
          <p>“已收藏”和“待确定”的项目统一保存在这里。后续可根据版本发布、热度变化和新证据重新判断。</p>
        </header>
        <WatchlistClient />
      </main>
    </div>
  );
}
