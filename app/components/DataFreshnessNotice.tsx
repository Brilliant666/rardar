import { formatCapturedDate } from "../data";
import { formatSnapshotAge, type DataFreshness } from "../runtime-readiness.mjs";

export function DataFreshnessNotice({ dataFreshness }: { dataFreshness: DataFreshness }) {
  if (dataFreshness.freshness !== "stale") return null;
  return (
    <aside className="data-freshness-warning" role="status" data-freshness="stale">
      <strong>数据更新已延迟</strong>
      <span>
        当前展示最近一次已验证数据：{formatCapturedDate(dataFreshness.snapshotCapturedAt)}
        {" · "}数据年龄：{formatSnapshotAge(dataFreshness.ageSeconds)}
      </span>
    </aside>
  );
}
