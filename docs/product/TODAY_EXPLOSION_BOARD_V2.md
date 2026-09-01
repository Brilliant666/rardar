# 今日爆发榜 v2

> 状态：产品合同已接受 / 24h 事实 Artifact 为当前 Draft 实现
> 主问题：过去 24 小时，GitHub 上哪些项目获得了最多新增关注？
> 排名合同：只按 Rardar 自有连续快照计算的 24h 新增 Star 降序。

正式覆盖文案为：

> 基于 Rardar 多源候选召回与自有连续观察形成的 GitHub 24h 爆发榜。

产品目标可以探索“全网现在最值得关注什么”，但数据页面不得声称已经扫描全 GitHub、榜单等于 GitHub 绝对全站 Top 20，或所有爆发项目都必然被召回。

当前工程合同见 [`2026-08-25-trending-explosion-artifact.md`](../iterations/2026-08-25-trending-explosion-artifact.md)。正式事实路径为 `<generation>/trending/explosion.json`，重算来源冻结于 `<generation>/trending/sources/*.json`；本 Draft 只实现 Artifact、derive 与 Audit，不实现页面或 AI。

## 1. 产品语义

“今日”是固定的滚动 24 小时事实窗口，不是仓库年龄归一化速度、GitHub 搜索的当前总 Star，也不是模型或规则综合分。

对于榜单发布时间 `T`：

```text
observedStarDelta = totalStars(T) - totalStars(T - 24h)
```

只有两个端点都来自 GitHub repository metadata 的 Rardar 自有观察，且时间均满足窗口容差，才能标记 `exact_window`。推荐每日正式发布点为 08:00 Asia/Shanghai，端点容差为 ±10 分钟。2 小时采集使用固定相位，因此正常情况下每天都有同相位基线。

若缺失严格端点：

- 不插值离散 Star 数；
- 不把任意区间线性归一化后称为精确 24h；
- 不使用 `stars / repository_age`；
- 可以记录实际窗口及 `partial`，但不进入精确主榜。

## 2. 事实源与权威顺序

| 优先级 | 来源 | 用途 | 能否决定名次 |
| --- | --- | --- | --- |
| 1 | Rardar 自有 GitHub metadata 连续快照 | 计算 `observedStarDelta` | **唯一可以** |
| 2 | GitHub Trending 页面 | 新候选、`sourceRank`、`reportedStarDelta` 交叉信号 | 否 |
| 3 | Trendshift Signal | 历史 Trending、engagement spike、召回补充 | 否 |
| 4 | HelloGitHub、GitHubDaily、OpenGithubs、AI News Radar、官方技术 Signal | 发现候选和背景 | 否 |

冲突时不做“多数投票”。自有精确窗口保持排名，外部差异记录为 `source_disagreement`。如果自有事实明显异常，则该条目标记 conflict 并从精确榜隔离，不能拿外部数静默替换。

### 2.1 GitHub Trending 的定位

[GitHub Trending](https://github.com/trending) 第一方页面展示 Today / This week / This month、语言筛选和 period stars，但没有找到受支持的官方 Trending API。其 HTML 是可变实现细节。

若实施低频采集，必须：

- 单独 feature flag，默认失败可降级；
- 每个时间范围与语言使用固定、低频请求；
- 保存采集时间、URL、parser version 和内容摘要 hash；
- parser fixture 变化时 fail closed，不用空列表覆盖上一条事实；
- 遵守 GitHub 当时的 [Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies)；
- 不把 `stars today` 政名为 Rardar `observedStarDelta`。

### 2.2 Trendshift 的定位

[Trendshift Signal](https://trendshift.io/signal) 可低成本补充历史和 spike 信号，但它的 [Terms](https://trendshift.io/tos) 禁止再分发原始 API 数据，且不承诺可用性或完整性。因此第一版不把它设为必要依赖；未配置、限流或中断时，候选覆盖降低但事实榜正常发布。

## 3. 首次发现

首次观察没有 `T-24h` 基线，必须**立即**进入独立的“新入榜待验证”区，不得隐藏 24 小时：

| 字段 | 显示规则 |
| --- | --- |
| 当前总 Star | 显示，来源为 GitHub API |
| 外部 period stars | 可显示为“GitHub/Trendshift 报告”，不得标“精确” |
| 首次发现时间 | 显示 Rardar `firstSeenAt` |
| 实际观察窗口 | 显示例如 `已观察 2h：+X`，不得外推为 24h |
| 精确 24h | 显示“等待完整窗口”，不得显示 proxy |
| 排名 | 使用区域内的外部 source rank 或当前 Star，仅明确标注为待验证；不占精确榜名次 |

24 小时后，如果两个端点完整，则机械进入精确候选；否则继续 `partial`，直到取得完整相位窗口。不能因主榜条目不足而提前升级。

## 4. 采集节奏与容量

### 4.1 推荐节奏

- 每 2 小时：轻量候选召回与 repository metadata observation。
- 每日 08:00：创建同相位 24h 窗口；完成 Schema/Audit 后发布正式 generation。
- AI：榜单事实发布后异步运行；ready 结果最早由下一次安全 derive/publish 采用。

每 2 小时路径只允许：

- 候选召回；
- GitHub repository metadata；
- Star、Fork、`pushedAt`、`archived`、`disabled`；
- 外部榜单来源与事实 capture bundle。

每 2 小时路径不得执行浅克隆、完整静态分析、深度 AI 或完整 generation publication。如果上一轮 observer 尚未结束，新一轮必须跳过并记录 `skipped_overlap`；不得启动第二个 observer。

与替代方案相比：

- 每日一次无法及时观察首次发现和漏跑，也难以诊断外部异常。
- 每 4 小时成本更低，但新爆发发现延迟翻倍。
- 实时或分钟级收益有限，违反第一版的资源与复杂度目标。

### 4.2 规划容量

初始上限建议：

- 每轮最多 500 个去重候选；
- 12 轮/日，最多 6,000 条 repository observations/日；
- 现有 9 条 Search 查询为 108 次/日；动态语言/Topic 查询总数设置全局上限，不超过 20 次/轮；
- Search 请求串行或小并发，遵守 `x-ratelimit-*`、`retry-after` 和 secondary limits；
- repository metadata 使用 ETag conditional requests；
- 新原始 2h observations 保留 45 天；历史 90 天 bundle 按其原始 `retainUntil` 保留；每日 rollup 长期保留。

该规模对小型单机属于轻量元数据工作；真正重资源的浅克隆和 AI 分析不在 2h 全量路径中。

### 4.3 推荐存储形态

第一版采用与正式 `data/` 一起备份、但位于 retained generations 之外的追加式事实账本：每轮采集先形成一个 immutable capture bundle，bundle 内含 observation 数组、source/query 状态、capturedAt、schemaVersion 和内容 digest。一个轮次一个 bundle，避免每仓库一个小文件。

- 临时文件完整写入、Schema 校验和 digest 通过后再原子改名；
- capture ID 和 repository/GitHub ID 唯一键让重复 catch-up 成为 no-op；
- 不修改历史 bundle；修正以新 observation 表达；
- 每日 derive 记录实际消费的 capture IDs/digests，随后把榜单 artifact 冻结进 generation；
- D1 继续只承载用户 Action/Feedback 等业务状态，不混入高频 GitHub 事实；
- Capture 完成自身 45 天（历史 bundle 为 90 天）合同且不再被 retained generation 引用后，才可由统一 Retention 的 digest-bound 计划清理；daily rollup 和 generation-local provenance 继续保留。

具体目录名属于第一个实现 PR 的合同评审内容；RFC 不预先修改 `data/` 或 `contracts/`。

## 5. 候选召回

### 5.1 来源组合

1. 现有 9 条 GitHub Search 查询。
2. GitHub Trending：all languages，以及按历史命中和当前需求选择的少量语言页。
3. 当前和历史 Catalog、Watchlist。
4. 已有官方 Signal、HelloGitHub、GitHubDaily、OpenGithubs、AI News Radar。
5. 可选 Trendshift daily/weekly、engagement spike。

每个候选保存 `recalledBy[]`，包括 source、source item key、query、source rank、capturedAt 和原始响应 hash。模型不能直接写入候选；它只能生成受限 query hints，最终候选必须来自真实来源响应。

### 5.2 去重与身份

- 同次采集优先以 GitHub numeric repository ID 去重。
- repository rename/transfer 时保留同一 GitHub ID 的连续 observation，并记录 old/new repository。
- GitHub numeric repository ID 只作为 observation ledger 的外部连续性锚。产品仍通过当前 Stable Project ID 进入 Catalog、路由、D1、Action 和 Feedback；第一个 observation PR 不修改 Stable Project ID v1、canonical routes、D1 identity 或历史行动/反馈。
- fork、mirror 与 source repository 分别保留身份，不把 Star 相加。

## 6. 入榜与排序

### 6.1 精确榜资格

候选必须同时满足：

1. 当前与基线 observation 都通过 Schema；
2. repository / GitHub ID 连续性明确；
3. 窗口端点符合 ±10 分钟容差；
4. `totalStars(T) >= totalStars(T-24h)`；若下降，标记 conflict 并人工/后续验证；
5. 当前仓库不是 deleted、disabled；archive、fork、mirror 可以保留但必须显著标记；
6. 没有 source identity collision 或不安全路径问题。

### 6.2 排序键

```text
1. observedStarDelta DESC
2. totalStars DESC
3. repository ASC
```

第二、第三键只用于稳定处理相同增量，不能引入 AI 或综合评分。

### 6.3 榜单长度

- 首页首屏：精确 Top 5。
- 完整页面：精确 Top 20。
- 待验证区：首页最多 3，完整页最多 20。
- 若精确项不足，显示实际数量和原因，不用 partial 或外部值补满。

## 7. AI 的职责

AI 只能补充：

- 中文一句话；
- 项目形态与核心能力；
- **AI 爆发原因判断**：为什么可能在当前窗口爆发；
- 局限与证据引用。

AI 不能：

- 修改、插入或过滤精确排名；
- 把外部 reported delta 写入 observed delta；
- 用“质量”“相关性”或用户反馈改变全局榜顺序；
- 在证据版本变化后继续显示旧结论为 current。

每条 AI 爆发原因判断必须包含 `evidenceRefs`、`confidence`、`limitations`、`sourceRevision`、`model`、`reasoningEffort`、`promptVersion` 和 `generatedAt`。页面必须把 24h Star、Trending、Release、Push 等事实与模型判断分栏，不得把“可能由什么推动”显示成确定因果。

状态行为：

| 状态 | 页面 | 发布 |
| --- | --- | --- |
| pending/running | 显示事实与“分析中” | 不阻塞 |
| retryable_failed | 显示事实与“稍后重试” | 不阻塞 |
| permanent_failed | 显示事实与“暂无 AI 分析” | 不阻塞 |
| stale | 隐藏旧判断或明确标“基于旧版本”，默认不进入新 generation | 不阻塞 |
| ready | 证据版本完全匹配时显示 | 可由下一 generation 引用 |

## 8. UI 信息层级

### 8.1 首屏卡片

- 排名与 `+N stars / 24h`；
- 总 Star；
- repository、可读名称；
- 中文一句话（ready 时）；
- `精确 24h` / `待验证` / `冲突`；
- AI 状态小标签；
- 查看证据、Watch、采取行动。

### 8.2 详情抽屉或项目页

- observation 窗口和端点；
- GitHub Trending 名次与 reported stars（注明来源）；
- 首次发现、连续精确上榜次数；
- 核心能力、项目形态、AI 爆发原因判断、局限；
- fork/mirror/archive/异常标记；
- 全部来源与 generation；
- 候选来源、成功查询数、召回候选数、观察覆盖状态、数据更新时间和 degraded source；
- Engineering Readiness、Reuse Fit、Evidence Completeness；
- Endurance/长期趋势；
- 兼容的“综合关注”分，但明确不影响今日榜。

## 9. 异常与降级

第一版只做可解释标记，不建设完整反作弊系统：

- `unusual_star_spike`：增量相对项目历史分布异常；只标记，不自动判假。
- `source_disagreement`：外部 reported delta 与自有 delta 明显不一致。
- `first_seen` / `partial_window`。
- `repository_renamed` / `repository_transferred`。
- `fork` / `mirror` / `archived` / `disabled`。
- `deleted_or_unavailable`：保留历史，不进入 current 精确榜。

机器人或可疑 Star 的完整识别 Deferred。2026-07 起 GitHub 对 Stargazers 列表增加访问限制，第一版不枚举所有 stargazer，也不存储用户级 Star 身份。

### 9.1 故障矩阵

| 故障 | 行为 |
| --- | --- |
| GitHub Search 失败 | 使用其他召回源和已有观察；记录覆盖 degraded |
| GitHub metadata 失败 | 不生成该候选新 observation，不复用旧数冒充 current |
| Trending parser 失败 | 熔断该辅助源，主事实继续 |
| Trendshift 失败/未配置 | 忽略辅助源，主事实继续 |
| AI 全部失败 | 发布纯事实榜 |
| 08:00 基线缺失 | 受影响项目不进入 exact；显示 partial/待验证 |
| Schema/Audit 失败 | 不切换 current，继续服务上一健康 generation |

## 10. 五维评分处置

| 当前维度 | v2 处置 |
| --- | --- |
| Attention | 改称“综合关注”，仅保留兼容/详情，不参与爆发榜 |
| Endurance | 移到长期趋势详情，未来支持 sustained/revived |
| Engineering Readiness | 详情与找项目比较继续使用 |
| Reuse Fit | 详情展示；找项目改为针对 RequirementProfile 的任务级结果 |
| Evidence Completeness | 详情展示，作为判断置信度背景，不改变 Star 排名 |

## 11. 测试与迁移计划

按独立 PR 逐步验证：

1. observation 合同：时间区、负数、身份冲突、重复捕获幂等、路径安全。
2. 窗口计算：严格 24h、边界容差、漏点、首次发现、Star 下降、同分稳定排序。
3. 多源召回：去重、source provenance、`incomplete_results`、429/secondary limit、parser fixture 变化。
4. observer ownership：单实例、未完成轮次跳过、`skipped_overlap`、无第二个 observer。
5. generation：artifact hash、Schema、cross-file Audit、发布中断、并发 publisher、rollback。
6. 页面：Top 5/20、不足不补、待验证隔离、覆盖说明、AI 六态、单请求单 generation。
7. 真实 HTTP：pointer 切换、损坏 current fail closed、AI outage 仍返回事实榜。

迁移期保留现有 Daily Five。新榜达到验收门槛后再通过独立 UI PR 将其设为首页第一主榜；回滚只切回旧页面和旧 artifact loader，不删除 observation 历史或 retained generations。

collector 首次启用后至少 warm up 24 小时才允许产生第一份精确榜；warm-up 期间只显示现有综合关注和“新入榜待验证”，不得导入历史 proxy 伪造基线。

## 12. 验收标准

- 连续 30 天样本中，GitHub 明显热门 Top 20 在精确榜与待验证区的合计漏报率 ≤10%。
- 运行 48 小时后，完整候选的精确窗口覆盖率 ≥90%。
- 首次发现标记正确率 100%，不存在 age proxy。
- AI ready 的 Top 20 中文一句话覆盖率在事实发布后 6 小时内 ≥90%。
- AI 完全不可用时事实榜可用率 100%。
- observation 新鲜度 p95 ≤150 分钟；每日正式榜 08:15 前完成或明确 degraded。
- 排名、窗口、来源与 generation 的可追溯率 100%。

## 13. 分阶段范围

### V2 第一版必须

- append-only observations、严格窗口、Top 5/20、待验证区、基础异常标记。
- 多源召回 provenance，GitHub API 元数据最终验证。
- AI 不阻塞与版本一致性。

### V2 后续

- momentum lifecycle、更多语言/Topic 覆盖、异常分布模型、历史图表。

### Deferred

- stargazer 用户枚举、复杂刷 Star 检测、秒级榜、社交媒体全网监控、资产库 UI。
