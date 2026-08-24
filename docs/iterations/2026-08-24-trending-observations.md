# TRENDING-OBSERVATIONS-01 — Append-only GitHub Trending Observation Foundation

日期：2026-08-24
状态：Draft 实现；只有对应 PR 合并到 `main` 后才算完成

## 唯一目标

建立 Rardar v2 今日爆发榜所需的第一层原始事实基础：每两个小时以确定性 phase 召回候选，从 GitHub repository metadata 取得权威事实，并将其写为可审计、不可覆盖的 capture bundle。

本轮不回答“24 小时增长是多少”，也不生成榜单。它只回答：在某个已声明 phase，Rardar 实际观察了哪些 GitHub repository ID、取得了哪些 metadata、召回覆盖怎样，以及这些字节是否仍可验证。

## 合同与边界

新增两个 Draft 2020-12 JSON Schema：

- `contracts/trending-observation.schema.json`：单仓库 metadata 事实与召回 provenance；
- `contracts/trending-capture-bundle.schema.json`：phase、覆盖状态、query/metadata 结果、retention 和 payload digest。

这两类 artifact 已注册到统一 `ArtifactKind`。它们拒绝额外字段，因此 AI 解释、24h delta、Attention/Reuse 分数或用户状态不能混入事实层。

capture 路径固定为：

```text
data/observations/trending/v1/captures/YYYY/MM/DD/<captureId>.json
```

该 namespace 位于 retained generations 之外；collector 和 audit 不读写 `data/current.json`、generation、candidate、flat staging 或 D1。原始事实 retention metadata 固定为 90 天，本轮不实现删除器。

## 候选召回与事实权威

1. 机械复用现有 `candidate_queries()` 的九条 GitHub Search query，并为每条记录稳定的 `query-01`…`query-09`、query 文本、result count、incomplete 状态与受限错误；
2. 读取当前 scheduled phase 之前 26 小时（含边界）的 healthy/degraded capture，将其中全部 numeric repository ID 优先 carry forward；
3. carry-forward 超过 500 时返回 `tracking_capacity_exceeded`，不截断历史跟踪集合；
4. 剩余容量按 query 顺序、source rank、numeric ID 确定性加入 Search 新候选；
5. Search 只贡献候选 ID、当时名称和 provenance；Star/Fork/Issue 等正式 observation 只来自 `/repositories/{numeric-id}`；
6. 全部 query 失败或全部 metadata 失败时不产生 bundle；部分 query 失败、incomplete Search 或部分 metadata 失败时产生可审计的 degraded bundle；失败项目不复用旧数伪装 current observation。

GitHub numeric ID 只服务 observation 连续性。跨 capture 的 rename/transfer 保留同一 ID；同一 capture 出现 ID→多名称或名称→多 ID 时 fail closed。现有 Stable Project ID、Catalog、路由、Action/Feedback/D1 均未改变。

## 时间、幂等与追加式写入

- `scheduleTimezone=Asia/Shanghai`，`cadenceMinutes=120`；
- `scheduledAt` 必须落在固定偶数整点 phase，并规范为 UTC；
- `captureId` 由 policy + scheduledAt 机械生成；
- `capturedAt` 是本轮 metadata 完成后的事实冻结时间；
- `windowEligible` 仅在 `abs(capturedAt-scheduledAt) <= 600 seconds` 时成立；
- 未显式传入 phase 时选择距离当前时间最近的固定 phase。

同一 policy/phase 只有一个文件。启动时先 no-follow stable-read 既有目标：完整合法则返回 `already_captured` 且不调用 GitHub；损坏、digest 不符、路径不符、symlink/reparse 或错误身份则拒绝覆盖。

新文件先在目标目录完成 canonical serialization、fsync、严格 JSON、Schema、语义和 SHA-256 digest 校验，再通过 hard-link no-replace 创建。并发竞争只允许一个 creator；另一个读取 winner 后返回 `already_captured`。故障注入证明临时写入、publish 竞争或中断不会留下部分 capture 或 `.tmp`。

Observer lock 是独立的长时单实例锁，不复用 generation data lock。第二个 observer 立即输出 `skipped_overlap`，不等待、不调用 GitHub、不写 bundle；锁在成功或异常退出路径均释放。

## CLI 与凭据

```powershell
python -m pipeline.collect_trending_observations `
  --data-dir <isolated-data> `
  --scheduled-at <timezone-aware-phase> `
  --timezone Asia/Shanghai `
  --limit 500 `
  --dry-run
```

Token 只能来自 `GITHUB_TOKEN` 环境变量，CLI 不提供 token 参数且没有 anonymous fallback。错误只保存受限 code/message；Bearer/token 形态和实际 token 都会脱敏。测试全部使用本地 fake client，没有访问 GitHub。

本轮只提供 deterministic CLI/service contract，没有把它接入现有每日 Scheduler、Manager、systemd 或 Production。

## Audit

`python -m pipeline.audit_trending_observations --data-dir <isolated-data>` 只读验证：

- 年/月/日目录、filename、captureId 与 scheduledAt 一致；
- 所有目录和 leaf 都是 no-follow 安全类型；
- strict UTF-8 JSON、无重复 key、无 NaN/Infinity；
- Schema、query policy、phase、delay、window 和 coverage 语义；
- payload digest；
- slot、numeric ID、repository name 与 candidate outcome 唯一性；
- 26 小时 carry-forward 候选完整性，以及 source capture、rank、ID 与时间的交叉引用；
- observation/query/metadata counts；
- `retainUntil = capturedAt + 90 days`；
- residual temporary file。

报告包含 healthy/degraded/failed、capture/observation 数、最早/最晚 capture、eligible/degraded 计数和稳定 issue code。Audit 不 repair、不删除、不写任何文件。

## 行为测试

测试覆盖：

- 两个 Schema 的合法/非法边界、重复 key、非有限数与 digest；
- 九查询复用、provenance 合并、全失败、部分失败和 incomplete Search；
- metadata 事实权威、部分/全部失败和 token 脱敏；
- 同 capture identity collision 与跨 capture rename/transfer；
- 26 小时边界、carry-forward 优先、500 上限；
- already-captured 无网络、损坏目标、错误 slot、path traversal、symlink/reparse gate；
- create-only 并发、写入中断、临时文件清理和旧 capture 字节不变；
- observer overlap 与 lock release；
- audit 的健康、降级、digest、count、retention、链接和 `.tmp` 故障；
- current、generation 和 D1 sentinel 字节不变，dry-run 不创建 data 路径。

完整 Verify 仍是合并门禁；正式 `data/`、Primary Runtime、3000/3002 和 Production 不属于本轮。

## 回滚

应用代码可以回滚而不迁移 observation store：旧代码不会读取这一独立 namespace。已经写入的合法 capture 是历史事实，不随代码回滚删除；未来代码重新启用时仍须按 v1 Schema/digest 审计。若尚未合并，只需关闭 Draft PR，不对正式 data 执行清理。

## 下一步门禁

只有本 PR 合并且最新 `main` Verify 成功后，才能另开独立工程轮设计 audited 24h explosion artifact。下一轮可以消费 eligible observation，但不得为了形成榜单改写本层历史，也不得把 reported delta 或单次 snapshot 冒充自有精确 24h 增量。
