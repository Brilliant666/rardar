# Codex 中文能力画像

每个 `*.json` 文件对应一个已经由本地 Codex 阅读过 README、GitHub 事实和静态检查结果的仓库。它用于补充中文标题、能力、任务词、复用路线和限制，不覆盖原始事实快照。

## 最低证据要求

1. 仓库必须存在于 `data/snapshots/latest.json`。
2. 优先阅读仓库 README、许可证和 `data/analysis/` 中的只读静态证据。
3. 功能声明必须能在 README 或代码结构中找到依据。
4. 不把 README 自述、基准测试或 Star 增长当作已经独立验证的效果。
5. `limitation` 必须说明至少一个实际风险、未知或许可证边界。

## 文件字段

- `schemaVersion`：新项目画像固定为 `2`；v0/v1 只作为 legacy staging 或 retained generation 兼容，不会被静默解释成 v2。
- `projectIdVersion`：新项目画像固定为 `1`。
- `projectId`：由规范化小写 `repository` 按 identity v1 重算得到的 Stable Project ID，必须与文件名完全一致。
- `repository`：`owner/name`。
- `analyzedAt`：分析时间。
- `sourcePushedAt`：从 Codex 队列原样复制的当前仓库推送时间。
- `sourceAnalysisAt`：从 Codex 队列原样复制的当前静态证据 `analyzed_at`。
- `model`：可选；填写时记录实际分析器，例如 `local-codex`。
- `titleZh`、`summaryZh`：中文决策摘要。
- `category`、`capabilities`、`taskTerms`：任务匹配输入。
- `bestFor`：适合什么目标。
- `reusePlan`：建议先复用或验证哪一层。
- `limitation`：限制与风险。
- `evidenceSummary`、`sourceUrl`：证据说明和原始仓库入口。

刷新数据后，Codex 应先检查每日前五中哪些项目仍为“事实初筛”或“静态分析”，只为证据足够的项目新增或更新画像。Queue v2 会提供 `projectIdVersion`、`projectId`、`repository` 和来源时间，Codex 必须原样复制，不能自行生成、归一化或改写。先把 JSON 写到 `data/` 之外的草稿路径，再用以下入口验证并原子写入 flat staging；不要直接覆盖本目录中的正式文件：

```bash
python -m pipeline.ingest_enrichment --kind project --input tmp/project-draft.json
npm run data:derive
```

两个来源时间必须是带时区 RFC3339，并与队列提供的字符串精确一致；`analyzedAt` 不能早于 `sourceAnalysisAt`。入口会核对类型、时间、URL、repository、重算后的 projectId 和 `<projectId>.json` 目标文件名，并与 refresh/derive 使用同一个数据目录锁。草稿的解析后路径必须在整个 `data/` 目录之外；验证或写入失败时保留已有 staging 画像。进入本目录不等于正式发布，只有后续 `data:derive` 通过 Schema、identity、audit、manifest/hash 和原子 pointer 门禁后才会成为网页数据。

具有可信来源字段的 v1 slug 文件只能通过显式 staging 迁移入口处理；无法机械升级的 legacy v0 会保留并报告。默认命令只预检和 dry-run；`--apply` 只迁移 flat `analysis/` 与 `enrichment/`，不修改 current 或 retained generations：

```bash
python -m pipeline.migrate_project_identity --data-dir data
python -m pipeline.migrate_project_identity --data-dir data --apply
```

同一 repository 已有内容等价的 Stable ID 文件时不会重写目标；若 legacy 源仍存在，apply 只完成可重试的源清理，正常完成后再次 apply 为 no-op。目标内容冲突、符号链接/路径逃逸或旧 slug 无法唯一映射时会失败并保留原文件，不得猜测归属。
