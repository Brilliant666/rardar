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
          <h1>暂时不决定，<br />继续观察变化。</h1>
          <p>标记为“待确定”的项目会保存在这里。后续可根据版本发布、热度变化和新证据重新提醒。</p>
        </header>
        <WatchlistClient />
      </main>
    </div>
  );
}
