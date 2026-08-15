import Link from "next/link";
import { canonicalProjectPath } from "../client-project-identity.mjs";
import { formatSignalTime, signalKindLabels, type AssociatedTechnicalSignal } from "../signals";

export function SignalCard({ signal, index }: { signal: AssociatedTechnicalSignal; index?: number }) {
  const association = signal.projectAssociation;
  return (
    <article
      className="signal-item"
      data-signal-id={signal.id}
      data-signal-association={association ? "project" : "signal-only"}
    >
      <div className="signal-item-topline">
        {typeof index === "number" && <span>{String(index + 1).padStart(2, "0")}</span>}
        <b>{signal.categoryZh ?? signalKindLabels[signal.kind]}</b>
        <small>{signal.source}</small>
        <time dateTime={signal.publishedAt}>{formatSignalTime(signal.publishedAt)}</time>
      </div>
      <h3><a href={signal.url} target="_blank" rel="noreferrer">{signal.titleZh ?? signal.title} ↗</a></h3>
      <p><strong>核心现状</strong>{signal.takeawayZh ?? signal.summaryZh}</p>
      <div className="signal-decision-grid">
        <aside><strong>为什么它重要</strong>{signal.whyItMattersZh ?? "当前只有来源事实，尚无额外中文判断。"}</aside>
        <aside><strong>关键证据</strong>{signal.source} · {signal.sources.length} 个归因来源 · <a href={signal.url} target="_blank" rel="noreferrer">查看原始动态 ↗</a></aside>
        {association ? (
          <aside className="signal-project-association">
            <strong>关联项目</strong>
            <span>已通过同一 generation 的 GitHub repository identity 验证关联到 {association.repository}。</span>
            <Link className="signal-project-link" href={canonicalProjectPath(association)}>
              进入项目决策页 <span aria-hidden="true">→</span>
            </Link>
          </aside>
        ) : (
          <aside><strong>关联边界</strong>尚无经过 generation 验证的 Stable Project ID，当前保持 signal-only，不猜测项目归属。</aside>
        )}
      </div>
      <div className="signal-item-evidence">
        <span>信号分 {Math.round(signal.score * 100)}</span>
        <span>{signal.sources.length} 个归因来源</span>
        <span>{signal.titleZh ? "Codex 已读" : "等待中文深读"}</span>
      </div>
    </article>
  );
}
