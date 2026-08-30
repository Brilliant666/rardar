# Trending Discover Artifact v1

日期：2026-08-31

任务：`RARDAR-DISCOVER-REALTIME-01`（Rardar producer half）

状态：实现与审查中；只有 PR 合并后才视为 Repository 完成，Production 仍未激活。

## 唯一目标

从现有两小时 Observation ledger 和当前 Today exact Explosion 派生独立、可审计、可回滚的近实时 Discover 事实，并把 derive 串行接入唯一 Managed Scheduler。Discover 回答“哪些项目刚出现、持续升温或接近完整 24 小时验证”，不建立第二个 Today 排行榜。

## 实现边界

- 新增 `TrendingDiscoverArtifact v1`、manifest 与 current JSON Schema；
- 使用最新 eligible capture、向前最多 26 小时 eligible captures 和当前已验证 Today exact exclusion；
- GitHub numeric repository ID 保持 rename/transfer 连续并执行 Today exact 排除；
- 三阶段和分区内顺序只由 source facts 确定，不使用 AI、用户行为或 24h 外推；
- source 原始字节、payload digest、文件 SHA-256、Today exclusion 与 manifest 全部冻结在独立 immutable generation；
- derive 支持 dry-run、already-derived、CAS publication、并发 settlement 与显式 rollback；
- 只读 Audit 从 generation-local sources 完整重算阶段、窗口、增量、排序、conflicts 与 coverage；
- 普通相位 Observation → Discover；08:00 Observation → Refresh → Explosion → Discover；失败只降级 `producer.discover` telemetry；
- 不新增 D1/PostgreSQL 表、service、timer、cron、daemon，不修改 Today Artifact，也不部署 Production。

## 验证合同

测试覆盖首次出现、连续观察、正增长、接近验证、Today 排除、rename、identity conflict、负增量、disabled、archived、fork、degraded coverage、source/Today tamper、阶段/排序篡改、already-derived、并发、pointer race、link/reparse、Audit recomputation、Scheduler 两种顺序、失败隔离和 telemetry 脱敏。最终门禁为 `npm run verify`、`git diff --check`、正式 `data/` 无差异以及 exact-head GitHub Verify。

## 后续边界

Rardar PR 合并后，TopicEye 才能固定最终 merge SHA 和契约 hash，完成 safe adapter、静态 profile、Discover 页面与本地真实数据闭环。Production Discover 必须等待独立 `RARDAR-DISCOVER-RUNTIME-ACTIVATION-01`；本迭代不得触发部署、重启或人工 derive。
