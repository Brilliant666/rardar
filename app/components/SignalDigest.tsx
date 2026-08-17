import Link from "next/link";
import type { AssociatedSignalSnapshot, CodexQueueSnapshot } from "../signals";
import { SignalCard } from "./SignalCard";

export function SignalDigest({
  codexQueue,
  signalSnapshot,
  stale = false,
}: {
  codexQueue: CodexQueueSnapshot;
  signalSnapshot: AssociatedSignalSnapshot;
  stale?: boolean;
}) {
  return (
    <section className="signal-digest">
      <div className="section-heading inline-heading">
        <div>
          <span className="section-label">AI & Tech brief</span>
          <h2>{stale ? "最近一次已验证动态" : "过去 48 小时，真正发生了什么"}</h2>
          <p>{signalSnapshot.healthySourceCount} 个健康信源 · {signalSnapshot.signalCount} 条去重动态 · {codexQueue.pendingCount} 条等待本地 Codex 深读。</p>
        </div>
        <Link href="/signals">查看全部动态与信源 →</Link>
      </div>
      {signalSnapshot.topSignals.length ? <div className="signal-list">
        {signalSnapshot.topSignals.map((signal, index) => <SignalCard key={signal.id} signal={signal} index={index} />)}
      </div> : <div className="empty-state compact-empty"><span>0</span><h2>当前没有可用动态</h2><p>已验证 generation 中没有技术动态。</p></div>}
    </section>
  );
}
