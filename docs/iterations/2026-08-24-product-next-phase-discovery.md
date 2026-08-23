# PRODUCT-NEXT-PHASE-DISCOVERY-01

> 日期：2026-08-24
> 状态：Draft RFC / 等待人工产品审查
> 分支：`research/rardar-v2-product-rfc`
> 基线：`9b4c6e7d5171af4878d47290f0b902eccb7cc7a3`

## 1. 目标与边界

本轮在最多 6 小时时间盒内，为两个 P0 产品能力形成正式、可拆分的产品与架构 RFC：

- 今日爆发榜 v2：以客观 24h 新增 Star 回答“今天哪些项目正在爆发”；
- 找项目 v2：以需求理解、动态召回、静态证据和跨项目比较回答“当前任务能复用什么”。

同时设计最小 AI Analysis Runtime，使模型进入无人值守体系但不阻塞事实发布。高价值资产库只保留最低限度后台历史，产品 UI Deferred。

本轮只改 Markdown，不实现产品代码，不访问 Production，不调用付费 OpenAI API，不创建实现分支，不 Ready 或合并 PR。

## 2. 基线与隔离

- PR #24 已合并，merge SHA 与 `origin/main` 均为 `9b4c6e7d5171af4878d47290f0b902eccb7cc7a3`；对应 GitHub Verify run `32652464688` 为 SUCCESS。
- 原长期开发 worktree 保持未修改。
- 研究在独立 worktree `C:\Users\brilliant\Documents\rardar-worktrees\rardar-v2-product-rfc` 进行。
- 分支从已验证的最新 `origin/main` 创建。

## 3. 仓库审计结论

### 3.1 当前爆发排名并非精确 24h 榜

- GitHub collector 用 9 条 Search 查询，每条最多 30 个，主要按总 Star 召回。
- 首次观察用 `stars / age_days`，后续使用任意快照区间并归一为 24h；这些只能用于 proxy/ranking，不是严格窗口事实。
- Daily Five 采用近期动量最多 3 个、长期高热最多 2 个的综合排序；`attentionScore` 由 momentum/endurance 等规则决定。

### 3.2 当前找项目是 Catalog 内固定规则

- 只搜索当前、通常最多 30 个项目的 Catalog。
- 8 组前端规则覆盖 video/account、research/evidence、agent engineering、docs automation、GitHub intelligence、workflow automation、integration、knowledge graph。
- 不进行动态 GitHub Search、需求结构化、用户仓库兼容分析或同任务跨项目深度比较。

### 3.3 当前 Codex Queue 不是 AI Worker

它生成版本化 JSON 任务并要求外部草稿 ingest，但没有 provider 调用、queue lease、重试、预算、异步状态或自动结果发布。

### 3.4 可复用与需降级

可复用：Stable ID、immutable generation、Schema/Audit、GitHub REST 客户端、安全浅克隆、静态证据、source binding、Action/Feedback、单请求单 generation。

需降级/废弃：age proxy 作为 24h、任意区间归一为精确值、综合分覆盖爆发名次、当前 Catalog 作为全搜索空间、文件存在即证明可复用、Codex Queue 等于 Runtime。

## 4. 外部研究结论

### GitHub

- 第一方 Trending 页面提供日/周/月、语言筛选和 period stars，但没有发现受支持的官方 Trending API。
- Repository Search 能按 stars/created/pushed/language/topic 等召回，每 query 最多 1,000 结果，可能 incomplete，且不能按 Star 增长排序。
- 推荐自有连续 metadata observations 为 24h authority；Trending HTML 只低频辅助，需条款复核和熔断。

### Trendshift

- 提供日/周/月/年榜、历史 Trending、repository profile、engagement spike 和 USD 9/月 Signal API。
- 条款允许私有产品分析和派生洞察，但禁止原始 API 数据或其实质复制的公开再分发。
- 推荐仅作可关闭辅助信号，不作正式事实依赖；未购买或中断时主链正常。

### OpenAI

- 官方确认 `gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`，均支持 xhigh、Responses、Batch、Streaming、Structured Outputs。
- 2026-08-24 价格按每 1M input/output：Sol 4/20、Terra 2/12、Luna 0.2/1.2 美元；Sol 为促销价，实施前需重查。
- 推荐 Luna 快速层、Terra 主判断、Sol xhigh 高价值升级；默认禁用，人工配置 key、可用性和预算后才启用。
- 组织模型权限、实际 limits、ZDR 和 Trendshift API SLA/rate limit 均标为 UNVERIFIED。

## 5. 收敛的产品决策

1. 自有连续快照是 24h 新增 Star 唯一 authority。
2. 每 2 小时轻量观察，每日 08:00 形成正式审计榜。
3. 无基线项目进入“新入榜待验证”，不以外部值或 age proxy 进入精确榜。
4. 首页精确 Top 5、完整页 Top 20；条目不足不补弱数据。
5. AI 异步增强，失败时事实照常发布；旧分析必须证据版本完全一致才复用。
6. 最小 durable queue + Worker；非紧急 backlog 可用 Batch，交互任务用 Responses/background。
7. 找项目 v2 使用 100 → 30 → 12 → 5 → 3–5 的有界漏斗。
8. v1 只支持公开 GitHub 仓库，禁止执行代码。
9. 通用 Project Profile 可共享，任务匹配绑定 RequirementProfile hash 单独计算。
10. generation 只冻结 ready 且版本匹配的 AI 引用；在线 Job 不修改 current。

## 6. 交付文档

- [`../product/RARDAR_V2_PRODUCT_RFC.md`](../product/RARDAR_V2_PRODUCT_RFC.md)
- [`../product/TODAY_EXPLOSION_BOARD_V2.md`](../product/TODAY_EXPLOSION_BOARD_V2.md)
- [`../product/FIND_PROJECT_V2.md`](../product/FIND_PROJECT_V2.md)
- [`../product/AI_ANALYSIS_RUNTIME_V1.md`](../product/AI_ANALYSIS_RUNTIME_V1.md)
- [`../ROADMAP.md`](../ROADMAP.md) 的最小状态更新

## 7. 推荐实现顺序

选择事实优先的方案 B：

1. Trending Observation contract 与追加式 observation store；
2. audited 24h explosion artifact；
3. 无 AI 榜单 UI；
4. AI Runtime foundation；
5. 中文增强；
6. 找项目 request + dynamic recall；
7. static capability analysis v2；
8. cross-project matcher。

每一项均创建独立分支和 Draft PR，并在 merge 后更新治理状态。第一实现 PR 仍需人工批准，本轮不得自动创建。

## 8. 过度工程化门禁

本轮明确 Deferred：复杂多模型路由、向量数据库集群、私有仓库 GitHub App、自动执行第三方项目、高价值资产库完整 UI、复杂反作弊、全网社交监控、多租户和秒级榜。

## 9. 验证记录

- `npm run verify`：PASS；Python 488/488（33 skipped）、Node 87/87；Lint、Schema、Audit、build 和 production dependency audit 全部通过，0 vulnerabilities。
- Verify isolation guards：repository data、Git-visible contents 和 Runtime state 均未被改变或遗留。
- `git diff --check`：PASS。
- `git diff -- data`：空。
- 变更范围：仅本轮允许的 6 个 Markdown 文件；README、代码、contracts、workflow 和 data 均未修改。
- Production 未访问；无残留测试进程。
- Draft PR 的 GitHub Verify 必须为 SUCCESS 后本轮才可报告 PASS。

## 10. 停止条件

Draft PR 创建并通过 GitHub Verify 后停止，只进入人工产品审查。不得标记 Ready、合并、部署或自动开始任何实现。
