import Link from "next/link";
import { catalog } from "../data";

const links = [
  ["/", "今日"],
  ["/signals", "动态"],
  ["/discover", "发现"],
  ["/search", "找项目"],
  ["/candidates", "候选池"],
  ["/watchlist", "观察列表"],
];

export function Nav() {
  return (
    <header className="site-header">
      <Link className="brand" href="/" aria-label="Rardar 首页">
        <span className="brand-mark">R</span>
        <span>Rardar</span>
      </Link>
      <nav className="main-nav" aria-label="主导航">
        {links.map(([href, label]) => (
          <Link key={href} href={href}>
            {label}
          </Link>
        ))}
      </nav>
      <div className="header-status">
        <span className="live-dot" /> {catalog.growthMode === "observed" ? "真实增长快照" : "真实首轮快照"}
      </div>
    </header>
  );
}
