# Codex 中文能力画像

每个 `*.json` 文件对应一个已经由本地 Codex 阅读过 README、GitHub 事实和静态检查结果的仓库。它用于补充中文标题、能力、任务词、复用路线和限制，不覆盖原始事实快照。

## 最低证据要求

1. 仓库必须存在于 `data/snapshots/latest.json`。
2. 优先阅读仓库 README、许可证和 `data/analysis/` 中的只读静态证据。
3. 功能声明必须能在 README 或代码结构中找到依据。
4. 不把 README 自述、基准测试或 Star 增长当作已经独立验证的效果。
5. `limitation` 必须说明至少一个实际风险、未知或许可证边界。

## 文件字段

- `schemaVersion`：当前项目画像固定为 `1`，未知版本会被拒绝。
- `repository`：`owner/name`。
- `analyzedAt`：分析时间。
- `model`：可选；填写时记录实际分析器，例如 `local-codex`。
- `titleZh`、`summaryZh`：中文决策摘要。
- `category`、`capabilities`、`taskTerms`：任务匹配输入。
- `bestFor`：适合什么目标。
- `reusePlan`：建议先复用或验证哪一层。
- `limitation`：限制与风险。
- `evidenceSummary`、`sourceUrl`：证据说明和原始仓库入口。

刷新数据后，Codex 应先检查每日前五中哪些项目仍为“事实初筛”或“静态分析”，只为证据足够的项目新增或更新画像。先把 JSON 写到 `data/` 之外的草稿路径，再用以下入口验证并原子发布；不要直接覆盖本目录中的正式文件：

```bash
python -m pipeline.ingest_enrichment --kind project --input tmp/project-draft.json
npm run data:derive
```

入口会核对类型、时间、URL、仓库身份和由 `repository` 推导的文件名，并与 refresh/derive 使用同一个数据目录锁。验证失败或写入失败时保留已有正式画像。
