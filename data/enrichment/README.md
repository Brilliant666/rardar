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

具有可信来源字段的 v1 slug 文件只能通过显式 staging 迁移入口处理；无法机械升级的 legacy v0 会保留并报告。应用代码回滚前，`--to-legacy-v1` 可把可机械降级的 v2 staging 恢复为旧代码可读取的 v1。两个方向默认都只预检和 dry-run，额外指定 `--apply` 才写入；工具只处理 flat `analysis/` 与 `enrichment/`，不读取或修改 current、retained generations、candidates 或 manifest：

```bash
python -m pipeline.migrate_project_identity --data-dir data
python -m pipeline.migrate_project_identity --data-dir data --apply
python -m pipeline.migrate_project_identity --data-dir data --to-legacy-v1
python -m pipeline.migrate_project_identity --data-dir data --to-legacy-v1 --apply
```

正向迁移与逆向降级都先对全部文件完成 preflight。逆向模式只把 `schemaVersion` 从 2 改为 1，移除 `projectIdVersion`/`projectId` 并恢复 legacy slug 文件名，其他事实、来源时间和内容原样保留。多个 projectId 降级到同一 slug、已有非等价目标、符号链接、junction 或路径逃逸会使整批零写入、零删除；apply 先原子写入并验证全部目标，再删除源。等价目标不重写，写入或源清理中断后可安全重试，完整执行后的第二次 apply 为 no-op。

refresh/derive 候选中同一 repository 的 v1/v2 共存时，不得仅凭 v2 Schema 版本选择 v2：只有把 v1 机械转换成预期 v2 后与现有 v2 完全相等，才允许保留 v2 并清理 v1；否则返回 `conflicting_project_artifact_versions`。analysis/enrichment 全部完成 preflight 前不修改文件，一个项目冲突会使整批 adoption 零写入、零删除。
