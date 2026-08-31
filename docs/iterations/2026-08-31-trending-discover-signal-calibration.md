# Trending Discover signal calibration

日期：2026-08-31

任务：`RARDAR-DISCOVER-SIGNAL-CALIBRATION-AND-UX-01`（Rardar producer half）

状态：实现与审查中；只有本 PR 合并后才视为 Repository 完成，Production Discover 仍未激活。

## 唯一目标

用真实 Observation 历史校准 Discover 的最低信号质量，并把选择结果固化成可重算的布尔门禁。该迭代不改变 Today、不调用 AI 排名、不部署 Production。

## 回放证据

- Production 只读归档提供 2026-08-26 14:00 至 2026-08-31 02:00 UTC 的 55 个连续 eligible captures（108 小时、缺失 0、重复 0）；
- 为保证每个 derive 点都有当时的有效 Today exclusion，正式政策对比使用 2026-08-28 00:00 至 2026-08-31 02:00 UTC 的 38 个 captures（74 小时）；
- v1 共形成 297 个发布事件：`just_discovered=2`、`rising=8`、`near_validation=287`，其中 69 个是大于 5,000 Star、观察至少 20 小时但只增长 1～4 Star 的弱事件；
- A（绝对增长）、B（绝对或相对增长）、C（双通道再加连续增长）均过滤了回放中的弱事件；选择 C，因为它同时把“持续升温”变成可审计的连续性事实；
- 选择阈值为绝对增长 `>=10`、相对增长 `>=1%`、最长连续正增长区间 `>=2`。实际分布中绝对增长 p90 为 9、相对增长 p90 为 0.746%，因此阈值位于真实分布上尾而非拍脑袋常量；
- C 在回放中发布 15 个事件、2 个唯一项目、23 个诚实空快照；两个项目之后都进入 Today exact。回放未出现“仅相对通道”样本，该路径由确定性小基数测试保护，不伪称生产证据。

仓库外机器报告：`%TEMP%/rardar-discover-calibration/20260831-110349/replay-report.json`。报告记录源归档 SHA-256、逐 derive 点结果、所有发布事件、分位数、A/B/C 对比、弱样本与取舍；不包含凭据。

## 发布合同

- `schemaVersion=2`、`policyVersion=trending-discover-v2`；保留对 retained v1 generation 的严格读取、重算和 rollback；
- `just_discovered`：最近 4 个计划小时首次出现，可为零增长，原因固定为 `first_seen_recently`；
- `rising`：至少两个有效 Observation 间隔，正总增量，通过绝对或相对门禁，并拥有至少两个连续正增长间隔；
- `near_validation`：连续观察至少 20 小时，且先通过 rising 的同一质量门禁；
- 每个发布项目冻结相对增长、正增长区间数、最长连续正增长数、最新区间增量、`publishReasonCodes` 和 `signalFacts`；
- 聚合 `suppressionSummary` 记录候选、阶段候选、发布、弱信号、Today exact、冲突以及八类可重算原因；
- 阶段内仍按 `observedStarDelta DESC → totalStars DESC → repository ASC`，没有 AI、分类或用户偏好排序；
- policy version 进入 generation identity，升级不会被旧 `already_derived` 指针吞掉。

## 验证边界

行为测试覆盖零增长首次发现、消失项目、连续/间歇/末次单跳增长、大仓库 `+1～+4`、小仓库高相对增长、20 小时弱信号、等待日榜、Today 排除、负增长、identity conflict、metadata/query degradation、v1/v2 replay、发布/抑制原因、Audit 重算、并发与 Scheduler 顺序。正式数据、D1、Today Artifact 和 Production 均不写入。
