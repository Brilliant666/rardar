# 找项目 v2

> 状态：产品合同已接受 / 尚未授权实现
> 目标：为具体开发任务寻找可整套采用、局部复用、Provider 接入或架构参考的公开 GitHub 项目。
> 安全边界：只做只读元数据和有界静态分析，不执行任何用户或第三方代码。

## 1. 为什么需要 v2

当前“找项目”只在当前 Catalog 的少量项目中，以 8 组固定关键词进行客户端重排。它适合轻量探索，却无法回答任意任务，也不能证明候选是否真的具备所需 API、SDK、模块或部署接口。

v2 将搜索变成一条可审计漏斗：

```text
用户需求
→ RequirementProfile
→ 动态候选召回
→ metadata 门禁
→ 安全静态验证
→ 同任务跨项目比较
→ 可行动的复用建议
```

热门不是匹配。没有满足条件的项目时，结果必须明确降级，不得用流行仓库填空。

## 2. 双输入模式

### 2.1 模式 A：requirement_only

输入自然语言需求，例如：

> 我需要为 Django 项目增加公开视频下载任务，要求异步队列、断点恢复、限速和可观测性，不能依赖商业 SaaS。

系统解析目标和约束，召回可能可复用的完整产品、库、模块、Provider 或架构。

### 2.2 模式 B：requirement_with_repository

输入同样的需求，再附一个**公开** GitHub 仓库 URL。系统先对用户项目创建安全静态兼容画像，再比较候选与现有技术、目录和部署方式。

第一版读取：

- 主语言、框架和 package manifests；
- 目录与模块边界；
- 直接依赖与 lock file 规模；
- API 风格与服务入口的静态迹象；
- 数据库、队列、缓存、部署和容器配置迹象；
- 许可证与已有相似模块。

不执行安装、构建、测试、脚本、容器或仓库内指令。私有仓库 GitHub App 和上传代码摘要均 Deferred。

### 2.3 共享与差异

| 阶段 | 模式 A | 模式 B |
| --- | --- | --- |
| RequirementProfile | 共享 | 共享 |
| 候选召回与 provenance | 共享 | 共享 |
| 候选静态证据 | 共享 | 共享 |
| 用户项目画像 | 无 | 有，绑定 source revision |
| 技术兼容判断 | 来自显式约束 | 显式约束 + 用户项目事实 |
| 结果 | 通用复用计划 | 面向现有仓库的接入点与冲突 |

## 3. RequirementProfile

模型必须生成以下版本化结构；无论 Provider 是否原生支持 Structured Outputs，Rardar 都必须在本地完成 JSON parse、Schema、来源和版本验证，随后让用户确认或修订：

| 字段 | 含义 | 例子 |
| --- | --- | --- |
| `goal` | 想完成的结果 | “批量下载公开视频并进入分析队列” |
| `mustHave[]` | 缺失即不推荐 | 断点续传、任务队列 |
| `niceToHave[]` | 可提高排序 | Webhook、管理 UI |
| `constraints[]` | 技术/资源/合规限制 | 自托管、Linux、4GB RAM |
| `exclude[]` | 明确排除 | 商业 SaaS、执行浏览器扩展 |
| `technologyStack[]` | 已知或偏好栈 | Python、Django、Redis |
| `deployment[]` | 运行环境 | Docker、single-server |
| `licensePreference[]` | 许可偏好 | MIT/Apache 优先，拒绝未知 |
| `reuseGranularity[]` | 可接受复用级别 | module_or_library、workflow |
| `acceptanceCriteria[]` | 可验证成功条件 | 1000 个任务可恢复、失败可重试 |

每个字段保存来源：`explicit_user`、`repository_fact` 或 `model_inferred`。模型推断必须可编辑，不能升级为用户事实。确认后的 canonical JSON 计算 `profileHash`，后续查询、缓存和比较都绑定该 hash。

## 4. 首版复用类型

为避免一开始建设重型分类体系，v1 收敛为：

| 类型 | 定义 |
| --- | --- |
| `whole_product` | 可作为产品基础整体采用或 fork |
| `module_or_library` | 可引入包、目录或相对独立模块 |
| `provider_or_connector` | 可接入外部系统、模型、存储或平台 |
| `workflow` | 可复用流程、pipeline 或自动化编排 |
| `reference_only` | 不宜直接复制，但可作为架构、UI、流程、知识或基础设施参考 |
| `not_recommended` | 存在 must-have 缺失、许可、成熟度或集成风险 |

`sdk` 在 v1 归入 `module_or_library`，`provider`/`connector` 合并。`reference_only` 可以附加 `referenceKinds[]`，第一版允许 `architecture`、`ui`、`workflow_design`、`knowledge`、`infrastructure`；它们不是新的主分类。旧的过窄参考分类不再发布。

## 5. 候选召回漏斗

### 5.1 搜索优先级

1. 当前 Catalog 和历史 generations 中已有的版本化 Project Profile。
2. 过去静态/AI profile 索引，只接受证据仍 current 的缓存。
3. Watchlist 和用户历史中明确相关的项目；只影响召回，不覆盖匹配事实。
4. GitHub Search 动态查询。
5. GitHub Trending 和现有 Signal 作为候选线索。
6. 可选 Trendshift 辅助召回；其原始数据不在结果页再分发。

不需要首版引入向量数据库集群。项目规模可先使用结构化字段、倒排文本索引和受限 query generation；只有 benchmark 证明召回不足时再评估 embeddings。

### 5.2 有界漏斗

```text
最多 6 条 GitHub Search query
→ 最多 100 个去重 recall candidates
→ 最多 30 个 metadata candidates
→ 最多 12 个安全静态分析
→ 最多 5 个深度横向比较
→ 最终展示 3–5 个，或诚实无结果
```

每一层都保存进入、淘汰和失败原因。上限是成本与延迟合同，不能由模型自行放宽。

### 5.3 Query 生成门禁

模型只输出语义 tokens 和结构化意图；服务端编译 GitHub query：

- 最多 6 条；
- 总字符、关键词数和 OR 分支有硬上限；
- 只允许 `in:name,description,readme`、`language`、`topic`、`stars`、`pushed`、`created`、`archived:false`、`fork:false`、`license` 等 allowlisted qualifier；
- 禁止 `user:`/`org:`，除非用户明确指定；
- 时间范围和最小 Star 由服务端策略给出，模型不能生成未来时间或无界查询；
- 记录 query ID、query text、响应时间、`incomplete_results`、页码和结果 hash；
- 候选只能来自实际 API 响应或已验证索引，绝不接受模型凭空给出的 repository。

GitHub Search 每个 query 最多提供 1,000 个结果且可能 incomplete，因此多样化受限 queries 比一个宽泛 query 更可靠。动态查询只召回，不能证明匹配。

## 6. 静态验证

### 6.1 复用现有安全边界

继续复用现有浅克隆 / source archive fallback 及其文件数、单文件大小、总大小、压缩包、超时、路径逃逸、symlink/reparse 和名称碰撞门禁。任何分析器都不能执行仓库代码。

### 6.2 V2 第一版新增探针

| 需要判断 | 可接受的静态证据 | 不得声称 |
| --- | --- | --- |
| 公开 API | OpenAPI/GraphQL schema、route declarations、public exports | API 实际可用或兼容 |
| SDK/package | package manifest、published package metadata、export map | 安装一定成功 |
| CLI | entry points、bin scripts、arg parser 定义 | 命令运行通过 |
| 服务入口 | main/app/server definitions、container command | 服务可启动 |
| 目录模块 | package/workspace/module boundaries、imports | 模块完全解耦 |
| Plugin/Provider/Adapter | interface/registry/config examples | 任意扩展都兼容 |
| 配置方式 | env sample、config schema/defaults | Production 安全 |
| 依赖规模 | manifests、direct dependency counts、workspace graph | transitive 风险已穷尽 |
| 部署 | Dockerfile、compose、Kubernetes、systemd、README instructions | 部署已验证 |
| 可拆分性 | public exports、依赖方向、独立 package | 集成成本确定 |

所有推断都携带 evidence refs、analyzer version 和 confidence；证据缺失输出 unknown，而非 false。

### 6.3 后续探针

- 更精确的多语言符号/调用图；
- CVE、维护者 bus factor 和 release compatibility；
- 沙箱内可重复构建或测试；
- license compatibility solver。

这些不属于 v1，尤其不得在首版自动执行第三方代码。

## 7. 跨项目比较

深度比较必须把同一 RequirementProfile、用户项目画像（若有）、最多 5 个候选的**同类型结构化证据**放入一次比较任务。模型不得为每个仓库分别写宣传文案后再拼接。

每个结果必须输出：

1. `repository` 与中文简介；
2. `whyMatched`；
3. `mustHaveCoverage`；
4. `missingCapabilities` 与 `unknownCapabilities`；
5. `technicalCompatibility`；
6. `reuseType` 与适用时的 `referenceKinds`；
7. `integrationCost` 与 `integrationWorkItems`；
8. `engineeringEvidence`；
9. `licenseAndRisk`；
10. `evidenceRefs`、`confidence` 与 `nextValidationAction`。

不得只输出综合匹配分、宣传性摘要，或没有证据的“推荐使用”。跨项目比较必须把同一个 RequirementProfile 与最多 5 个候选的标准化证据放入同一次比较任务；不得分别生成宣传文案后拼接排名。

比较矩阵至少包含：

| 维度 | 语义 |
| --- | --- |
| capability coverage | must-have / nice-to-have 的逐项证据 |
| technical compatibility | 语言、框架、协议、数据和部署兼容 |
| reuse path | 整体、模块、connector、workflow 或参考 |
| integration cost | low/medium/high/unknown，并列出工作项 |
| engineering maturity | 事实性工程信号，不等于“质量分” |
| license and risk | SPDX/unknown、copyleft、维护和安全未知 |
| confidence | 证据充分度与模型不确定性 |

## 8. 异步 UX

推荐“快速结果 + 异步深度结果”：

### 8.1 快速阶段，目标 p95 <10 秒

- 校验输入和公开 GitHub URL；
- 生成 RequirementProfile；
- 返回最多 10 个 metadata-level 快速候选；
- 让用户确认/修订需求；
- 明确显示哪些只是 recalled、尚未验证。

### 8.2 深度阶段，目标 p95 <5 分钟

- 安全静态分析最多 12 个；
- 画像缓存校验；
- 深度比较最多 5 个；
- 逐步更新 `AIJobState`；
- 完成时显示 3–5 个行动卡。

用户可以离开页面后凭 Job ID 返回；取消只停止尚未领取的工作，不删除已经产生的事实或审计记录。

## 9. 结果状态与无结果

| 状态 | 条件 | 用户文案与行为 |
| --- | --- | --- |
| `analysis_pending` | 事实候选已有，静态/AI 未完成 | 展示快速候选和进度，不下最终结论 |
| `needs_extended_search` | 当前 6 条 queries 覆盖不足但仍有新方向 | 解释缺口，需用户确认后开启新的有界 Job |
| `weak_match` | 有部分证据，但 must-have 有缺口 | 明确缺口和可作为参考的范围 |
| `no_match` | 没有项目达到最低门槛 | 不展示无关热门项目；给出放宽约束建议 |
| `ready` | 比较和合同均通过 | 展示 3–5 个结果或明确 no_match |

最低门槛：存在至少一个可验证的 must-have 证据、没有命中 exclude、许可证没有明确冲突。否则最多是 weak_match。

## 10. 历史、隐私与个性化

- 原始需求、确认后的 RequirementProfile、候选和结果默认保留 30 天。
- 提供按 Job 删除；删除语义和备份到期策略必须在实现 PR 明确。
- 默认不把需求全文用于长期个性化。
- 用户显式同意后，只提取结构化偏好（例如 Python、自托管、许可偏好），不保留不必要的源码片段。
- Action/Feedback 继续通过既有 append-only 事实模型记录；找项目不能直接写“已复用”。
- 不发送 D1 用户数据、GitHub token、Basic Auth、Production secret 或 EnvironmentFile 给模型。
- v1 只读取公开仓库；对 404 不猜测它是私有还是不存在。

## 11. 一致性与缓存

- ProjectSearchRequest 在开始时固定 `generationId`。
- 每个 candidate 固定 repository/projectId、metadata revision、static evidence ref 和 AI profile revision。
- RequirementProfile 以 canonical JSON hash 绑定。
- 一次 Job 内 pointer 切换不会混入新 generation；用户可显式“用最新数据重新运行”创建新 Job。
- 通用 AIProjectProfile 只有证据指纹完全匹配才复用。
- ProjectMatchResult 仅在 RequirementProfile hash、候选集合 hash 和 profile revisions 全部一致时复用。

## 12. 测试计划

### 12.1 行为测试

- 两种输入模式和非法/非 GitHub/私有 URL。
- RequirementProfile 的显式、推断、用户修订来源。
- 6-query、100/30/12/5 上限与模型越权 query 拒绝。
- GitHub `incomplete_results`、429、超时、重复候选和 rename。
- 模型编造 repository 不进入候选。
- current/history/watchlist/dynamic/trending 多源 provenance。
- static probe 的存在、未知与不得声称边界。
- 同任务矩阵、must-have 缺口、许可证冲突和不推荐。
- `no_match`、`weak_match`、`needs_extended_search`、`analysis_pending`。
- Job 幂等、并发领取、租约过期、取消、有界重试、backlog 和 Provider 熔断。
- pointer 切换时 Job 仍保持一个 generation，新 Job 能看到新 generation。
- 删除历史、30 天到期和未授权个性化。

### 12.2 安全测试

- README/Issue/文档 prompt injection；
- 超长文本、binary、archive bomb、symlink/junction、path traversal、Unicode collision；
- 仓库脚本永不执行；
- URL encoding、重定向、host allowlist、SSRF；
- 私有/不存在仓库不泄露授权状态；
- 模型输入 secret-pattern gate。

### 12.3 真实 HTTP

- server-rendered 输入和 Job 页面；
- 快速阶段、轮询/推送状态和最终结果；
- AI outage 仍返回事实候选和明确状态；
- 同一响应不混 generation；
- 测试使用隔离 `RARDAR_DATA_DIR`、随机端口和临时 D1，结束清理进程。

## 13. 验收标准

- 100+ 人工需求集的关键字段解析正确率 ≥90%。
- must-have 加权覆盖率 ≥85%，明显不匹配误报率 ≤10%。
- 候选 source/query 和最终 evidence 可追溯率 100%；最终证据覆盖率 ≥90%。
- 人工认为复用计划有用的比例 ≥70%。
- benchmark 的诚实 no-result 处理正确率 100%。
- 快速阶段 p95 <10 秒，深度阶段 p95 <5 分钟。
- 所有 Job 都满足版本化 input/output、timeout、retry、backlog 和 concurrency 边界；不因货币预算暂无限制而重复分析相同证据。
- 模型、GitHub 或静态分析失败不会把低相关热门项目伪装成推荐。

## 14. 分阶段范围

### V2 第一版必须

- 双输入、公开仓库、RequirementProfile、动态有界召回。
- static capability probes、跨项目矩阵、渐进式 Job UX。
- 3–5 个行动结果或诚实 no-result。

### V2 后续

- 更细的 SDK/UI/knowledge reuse labels、更多语言分析器、评测驱动的 embeddings。

### Deferred

- 私有仓库 GitHub App、源码上传、自动运行候选、向量数据库集群、多租户共享搜索历史。
