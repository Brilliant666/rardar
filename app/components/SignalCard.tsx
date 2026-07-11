import { formatSignalTime, signalKindLabels, type TechnicalSignal } from "../signals";

export function SignalCard({ signal, index }: { signal: TechnicalSignal; index?: number }) {
  return (
    <article className="signal-item">
      <div className="signal-item-topline">
        {typeof index === "number" && <span>{String(index + 1).padStart(2, "0")}</span>}
        <b>{signal.categoryZh ?? signalKindLabels[signal.kind]}</b>
        <small>{signal.source}</small>
        <time dateTime={signal.publishedAt}>{formatSignalTime(signal.publishedAt)}</time>
      </div>
      <h3><a href={signal.url} target="_blank" rel="noreferrer">{signal.titleZh ?? signal.title} ↗</a></h3>
      <p>{signal.takeawayZh ?? signal.summaryZh}</p>
      {signal.whyItMattersZh && <aside><strong>为什么值得看</strong>{signal.whyItMattersZh}</aside>}
      <div className="signal-item-evidence">
        <span>信号分 {Math.round(signal.score * 100)}</span>
        <span>{signal.sources.length} 个归因来源</span>
        <span>{signal.titleZh ? "Codex 已读" : "等待中文深读"}</span>
      </div>
    </article>
  );
}
