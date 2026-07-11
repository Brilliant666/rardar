# Codex 中文能力画像

每个 `*.json` 文件对应一个已经由本地 Codex 阅读过 README、GitHub 事实和静态检查结果的仓库。它用于补充中文标题、能力、任务词、复用路线和限制，不覆盖原始事实快照。

## 最低证据要求

1. 仓库必须存在于 `data/snapshots/latest.json`。
2. 优先阅读仓库 README、许可证和 `data/analysis/` 中的只读静态证据。
3. 功能声明必须能在 README 或代码结构中找到依据。
4. 不把 README 自述、基准测试或 Star 增长当作已经独立验证的效果。
5. `limitation` 必须说明至少一个实际风险、未知或许可证边界。

## 文件字段

- `repository`：`owner/name`。
- `analyzedAt`：分析时间。
- `model`：当前填写 `local-codex`。
- `titleZh`、`summaryZh`：中文决策摘要。
- `category`、`capabilities`、`taskTerms`：任务匹配输入。
- `bestFor`：适合什么目标。
- `reusePlan`：建议先复用或验证哪一层。
- `limitation`：限制与风险。
- `evidenceSummary`、`sourceUrl`：证据说明和原始仓库入口。

刷新数据后，Codex 应先检查每日前五中哪些项目仍为“事实初筛”或“静态分析”，只为证据足够的项目新增或更新画像，再运行 `npm run data:build`。
