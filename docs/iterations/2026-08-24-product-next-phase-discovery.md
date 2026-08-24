# PRODUCT-NEXT-PHASE-DISCOVERY-01 / 02

> 日期：2026-08-24
> 状态：产品决策已接受 / PR #25 最终收口
> 分支：`research/rardar-v2-product-rfc`
> 基线：`9b4c6e7d5171af4878d47290f0b902eccb7cc7a3`

## 1. 目标与边界

本轮在最多 6 小时时间盒内，为两个 P0 产品能力形成正式、可拆分的产品与架构 RFC：

- 今日爆发榜 v2：以客观 24h 新增 Star 回答“今天哪些项目正在爆发”；
- 找项目 v2：以需求理解、动态召回、静态证据和跨项目比较回答“当前任务能复用什么”。

同时设计最小 AI Analysis Runtime，使模型进入无人值守体系但不阻塞事实发布。高价值资产库只保留最低限度后台历史，产品 UI Deferred。

Discovery-01 只形成 Draft RFC；Discovery-02 将用户已批准的产品与 AI Provider 决策写入同一 RFC，并在 exact-head Verify 门禁通过后收口 PR #25。两轮都只改 Markdown，不实现产品代码，不访问 Production，不调用付费 API，不配置密钥，也不创建实现分支。

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

- 官方 OpenAI 文档确认 `gpt-5.6-sol` 支持 Responses、Structured Outputs，以及 medium/high/xhigh reasoning effort；这些事实不证明自托管代理已透传相同能力。
- 用户选择自托管 Sub2API 作为 Rardar v1 Provider，预期入口标识为 `https://api.cosflow.icu`，Primary model 固定为 `gpt-5.6-sol`。
- 第一版不要求 Luna/Terra/Sol 多模型路由，也不设置固定货币硬预算；普通任务使用 medium/high，深度仓库分析、跨项目比较和高风险复核使用 xhigh。
- Sub2API endpoint join、模型权限、effort/Structured Outputs/store/usage/request ID/错误合同、exact version/commit 与安全状态全部在 versioned capability probe 前保持 **UNVERIFIED**。

## 5. 收敛的产品决策

1. 自有连续快照是 24h 新增 Star 唯一 authority。
2. 每 2 小时轻量观察，每日 08:00 形成正式审计榜；observer 重叠时跳过并记录 `skipped_overlap`。
3. 无基线项目立即进入“新入榜待验证”，显示实际观察窗口，不以外部值或 age proxy 进入精确榜。
4. 首页精确 Top 5、完整页 Top 20；条目不足不补弱数据。
5. Trendshift 只作可选辅助召回/交叉信号，未配置或失败不阻塞事实链，不再分发完整原始数据。
6. AI 异步增强，失败时事实照常发布；旧分析必须证据版本完全一致才复用。
7. AI Provider 为自托管 Sub2API，模型固定 `gpt-5.6-sol`；medium/high 用于普通任务，xhigh 用于深度与比较。
8. 暂不设置货币硬预算，但强制 concurrency、输入/输出、timeout、重试、backlog、幂等、usage accounting 和熔断。
9. Rardar 自己拥有 durable AIJob queue、独立 Worker 和 lease；主链使用完整非流式请求，Provider Background/Batch 仅为可选优化。
10. 找项目 v2 同时支持自然语言需求和需求加公开 GitHub URL；第一版不接入私有仓库、不执行代码。
11. 找项目使用 100 → 30 → 12 → 5 → 3–5 的有界漏斗，候选只能来自真实 API/索引。
12. 原始需求与 RequirementProfile 默认保留 30 天；长期个性化必须显式 opt-in。
13. 通用 Project Profile 可共享，任务匹配绑定 RequirementProfile hash 单独计算。
14. generation 只冻结 ready 且版本匹配的 AI 引用；在线 Job 不修改 current。
15. 复用结果固定为 whole_product、module_or_library、provider_or_connector、workflow、reference_only、not_recommended，参考细类进入 `referenceKinds`。
16. GitHub numeric repository ID 只作为 observation ledger 的 rename/transfer 连续性锚；现有 Stable Project ID、路由、D1、Action/Feedback 不在首个 observation PR 修改。
17. 高价值资产库完整产品建设 Deferred，只积累最低限度历史事实。

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
3. AI Runtime foundation（Provider interface、Sub2API adapter、AIJob、durable queue、独立 Worker、usage accounting、mock provider，默认 disabled）；
4. 今日爆发榜 UI（Top 5 / Top 20 / 新入榜待验证 / AI 状态槽位）；
5. 中文项目画像与 AI 爆发原因判断；
6. 找项目 RequirementProfile + Job contract；
7. 动态 GitHub 候选召回；
8. Capability Static Analysis v2；
9. Cross-project Matcher。

每一项均创建独立分支和 Draft PR，并在 merge 后更新治理状态。第一实现 PR 仍需人工批准，本轮不得自动创建。

## 8. 过度工程化门禁

本轮明确 Deferred：复杂多模型路由、Provider Background/Batch 必需依赖、Streaming、向量数据库集群、私有仓库 GitHub App、自动执行第三方项目、高价值资产库完整 UI、复杂反作弊、全网社交监控、多租户和秒级榜。

## 9. 验证记录

- `npm run verify`：PASS；Python 488/488（33 skipped）、Node 87/87；Lint、Schema、Audit、build 和 production dependency audit 全部通过，0 vulnerabilities。
- Verify isolation guards：repository data、Git-visible contents 和 Runtime state 均未被改变或遗留。
- `git diff --check`：PASS。
- `git diff -- data`：空。
- 变更范围：仅本轮允许的 6 个 Markdown 文件；README、代码、contracts、workflow 和 data 均未修改。
- Production 未访问；无残留测试进程。
- Draft PR 的 GitHub Verify 必须为 SUCCESS 后本轮才可报告 PASS。

## 10. Discovery-02 收口门禁

用户已经批准 RFC 的产品范围、榜单语义、Sub2API Provider、`gpt-5.6-sol` 单模型 effort 分层、无固定货币硬预算、Rardar-owned queue/Worker 与 30 天历史边界。剩余未验证共 7 项，仅限实施前 capability probe 结果、真实权限/rate limits、Structured Outputs 透传、canonical endpoint join、Sub2API exact version/security、Worker 最终 systemd 资源和真实 latency/token usage。

PR #25 只有在本地完整 Verify、exact-head GitHub Verify、0 review blocker、Ready/Squash merge 和 main push Verify 全部通过后才完成。完成后移除独立 research worktree并停止；不得自动创建 `feat/trending-observations`、访问 Production 或开始实现。
