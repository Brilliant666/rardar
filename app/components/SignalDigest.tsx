import Link from "next/link";
import { signalSnapshot } from "../signals";
import { SignalCard } from "./SignalCard";

export function SignalDigest() {
  return (
    <section className="signal-digest">
      <div className="section-heading inline-heading">
        <div>
          <span className="section-label">AI & Tech brief</span>
          <h2>过去 48 小时，真正发生了什么</h2>
          <p>{signalSnapshot.healthySourceCount} 个健康信源 · {signalSnapshot.signalCount} 条去重动态 · 前 5 条已由本地 Codex 生成中文要点。</p>
        </div>
        <Link href="/signals">查看全部动态与信源 →</Link>
      </div>
      <div className="signal-list">
        {signalSnapshot.topSignals.map((signal, index) => <SignalCard key={signal.id} signal={signal} index={index} />)}
      </div>
    </section>
  );
}
