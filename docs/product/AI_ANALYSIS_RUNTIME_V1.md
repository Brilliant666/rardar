# AI Analysis Runtime v1

> 状态：产品合同已接受 / 尚未授权实现
> 决策日期：2026-08-24
> 目标：让模型判断进入 24 小时无人值守体系，同时永远不成为事实采集和 generation 发布的单点故障。
> 本 RFC 没有访问 Sub2API 管理后台、Production credential 或真实 API Key，也没有发起任何付费模型调用。

## 1. Provider 与模型合同

Rardar 第一版 AI Provider 固定为用户自托管的 **Sub2API**。预期入口标识为：

```text
https://api.cosflow.icu
```

该字符串是待 capability probe 规范化的配置候选，不代表 `/v1/responses` 已经验证可用。RFC 不记录 API Key、完整请求头、管理后台信息或服务器部署细节。

第一版 Primary model 固定为：

```text
gpt-5.6-sol
```

[OpenAI 官方模型页](https://developers.openai.com/api/docs/models/gpt-5.6-sol)确认底层模型支持 Responses、Structured Outputs 和 medium/high/xhigh reasoning effort。该事实只描述 OpenAI 官方接口，不证明当前 Sub2API 部署已经透传模型、effort、Structured Outputs、usage 或任何其他字段。所有代理能力在 versioned capability probe 前均为 **UNVERIFIED**。

第一版不要求 Luna / Terra / Sol 多模型路由，不实现自动模型竞价或 fallback。Rardar 使用同一个 `gpt-5.6-sol`，按任务选择 reasoning effort：

| 任务 | Effort | 说明 |
| --- | --- | --- |
| 中文一句话简介 | `medium` | 短输出、事实引用明确 |
| 简单项目形态与能力提取 | `medium` 或 `high` | 证据简单时 medium，存在多模块时 high |
| AI 爆发原因判断 | `high` | 必须区分事实与模型判断 |
| 找项目 RequirementProfile | `high` | 用户可以修订模型推断 |
| 候选能力匹配 | `high` | 逐项核对 must-have 与缺口 |
| 深度仓库分析 | `xhigh` | 高价值、长证据、复杂边界 |
| 跨项目横向比较 | `xhigh` | 同一任务、最多 5 个候选一次比较 |
| 低置信、高风险或候选接近时复核 | `xhigh` | 由服务端规则升级 |

不得默认所有候选、摘要或翻译都使用 `xhigh`。模型不能自行提高 effort；Job 创建器根据版本化策略决定并保存实际值。

## 2. 货币预算与强制运行边界

第一版**不设置固定货币硬预算**。先前建议的 `USD 3/日`、`USD 90/月` 不再是产品合同，也不是启用 AI 的必要条件。不限货币预算不等于无限调用、无限重试或重复分析相同证据。

v1 强制边界：

- AI Worker provider concurrency 固定为 `1`；
- pending backlog 总上限 `500`，达到上限后拒绝低优先级新 Job并记录 `backlog_limit_reached`；
- 同一个 `jobType + sourceRevision + promptVersion + schemaVersion + model + effort` 只有一个 active Job；
- 每个 Job 有最大输入、最大输出、deadline、最大尝试次数和稳定错误分类；
- 所有重试复用同一 idempotency key，不创建第二个业务结果；
- 连续错误触发 provider circuit，事实采集和 generation publication 继续；
- 每次调用记录 usage 和 latency，即使当前没有金额上限；
- AI 总开关默认 `false`，capability probe 未通过时并发强制为 `0`。

建议的 v1 默认 Job ceilings：

| Job | 最大输入 tokens | 最大输出 tokens | 单次超时 | 最大总尝试 |
| --- | ---: | ---: | --- | ---: |
| 中文一句话简介 | 16K | 1.2K | 90s | 2 |
| 项目形态/能力提取 | 48K | 4K | 5min | 2 |
| AI 爆发原因判断 | 32K | 2K | 3min | 2 |
| RequirementProfile | 16K | 3K | 2min | 2 |
| 候选能力匹配 | 96K | 6K | 10min | 2 |
| 深度仓库分析 | 160K | 10K | 20min | 1 |
| 跨项目横向比较/复核 | 192K | 12K | 20min | 1 |

实现 PR 可以在不扩大上述上限的前提下收紧默认值；任何扩容必须是版本化策略变更并重新评测。

## 3. Sub2API Capability Probe 门禁

AI 启用前必须运行独立、显式、版本化的 `AI-PROVIDER-CAPABILITY-PROBE-01`。Probe 只验证 Provider 合同，不创建正式 AIJob、不写 generation、不修改 current，也不把 credential 写入输出。

以下内容全部为 **UNVERIFIED UNTIL PROBE**：

- canonical base URL 与 endpoint 拼接；
- `/v1/models`；
- `/v1/responses`；
- `gpt-5.6-sol` 是否真实可调用；
- `reasoning.effort=xhigh` 是否透传；
- Structured Outputs 是否原生透传；
- `store=false` 是否透传；
- usage、cached tokens 与 request ID 字段；
- 429、5xx 与 timeout 合同；
- Provider Background、Batch 与 prompt cache 字段；
- Sub2API implementation/fork、exact version/commit、部署日期和相关安全公告状态；
- API Key 权限、并发限制、rate limits 与日志脱敏状态。

最低可启用能力：

1. 请求可以认证；
2. 可以调用 `gpt-5.6-sol`；
3. 可以发送并验证 reasoning effort；
4. 可以获得完整非流式响应；
5. 可以获得可解析文本或结构化内容；
6. 4xx、429、5xx、timeout 和内容错误可以稳定分类；
7. 响应 Content-Type 与 body 是预期 JSON，而不是 HTML 前端页面。

Probe 输出至少绑定：

```text
probeVersion
provider=sub2api
baseUrlIdentifier
normalizedBaseUrl
responsesEndpoint
model
testedEfforts
structuredOutputMode
storeFalseObserved
usageShape
requestIdShape
errorContractSummary
sub2apiVersionEvidence
securityReviewState
probedAt
result
```

API Key、Authorization header、完整响应中的敏感字段和管理后台数据不得进入 probe artifact、日志、Git 或 PR。

### 3.1 Provider URL 规范化

Provider adapter 必须先规范化 base URL，再通过一个受测试的 endpoint join 函数生成请求地址。只能在 Probe 后确定以下哪种配置有效：

```text
方案 A
base_url=https://api.cosflow.icu
endpoint=/v1/responses

方案 B
base_url=https://api.cosflow.icu/v1
endpoint=/responses
```

必须拒绝：

- `https://api.cosflow.icu/v1/v1/responses`；
- 将请求误发到根前端后得到 HTML；
- 非 HTTPS、userinfo、fragment、query 注入或 endpoint path traversal；
- 重定向到非 allowlisted origin；
- HTTP 成功但 Content-Type 或 JSON body 不符合 Provider 合同。

### 3.2 Structured Output 模式

Probe 只能选择以下一种模式：

- `NATIVE`：Sub2API 已证明原生透传 Structured Outputs；
- `LOCAL_SCHEMA_VALIDATION_FALLBACK`：Provider 能稳定返回完整 JSON，但不保证原生 Schema 约束。

无论哪种模式，Rardar 都必须执行本地 JSON parse、Schema validation、evidence ref validation、source version validation，以及 prompt/schema version validation。自由文本永远不能直接写入正式 AI artifact。

## 4. Rardar-owned 异步权威

异步权威是 Rardar 自己的 durable AIJob queue 与独立 Worker，不依赖 Sub2API 或 OpenAI 官方 Background mode：

```text
事实或静态证据 ready
→ 创建幂等 AIJob
→ 独立 Worker 领取有期限 lease
→ 同步、非流式调用 Sub2API
→ 解析完整响应
→ 本地 Schema / evidence / source version 校验
→ 原子发布版本化 AI 结果
→ 下一 generation 可选引用 ready 结果
```

Provider Background 是可选优化；Provider Batch 是可选优化；prompt cache 是可选成本优化；Streaming Deferred。这些能力均不存在时，Runtime 仍必须通过普通完整 Responses 请求工作。

现有 Codex Queue 只能复用输入合同思路。它没有 lease、provider 调用、重试、熔断或结果状态，不能冒充 durable Runtime。

### 4.1 AIJob 最低字段

- job ID/type、idempotency key；
- repository/projectId 或 requestId；
- generationId；
- source revision、evidence refs 与 input hash；
- provider、base URL identifier、model、reasoning effort；
- prompt version、Schema version；
- priority、notBefore、deadline；
- attempt、lease owner、lease expiry；
- state、error code、result ref；
- createdAt、startedAt、completedAt；
- usage accounting。

状态固定为：

```text
pending
running
retryable_failed
permanent_failed
stale
ready
```

Worker 在每次外部调用前重新验证 source revision、Job lease、circuit 和 active-job 唯一性。输入已经过期时转 `stale`，不调用 Provider；响应返回后 source revision 已变化时保留审计事实但不得发布结果。

### 4.2 请求审计与 usage accounting

每次尝试必须记录：

- provider；
- base URL identifier，但不记录 Key；
- model；
- reasoning effort；
- provider request ID；
- input tokens；
- cached tokens（若 Provider 提供）；
- output tokens；
- latency；
- attempt count；
- error code；
- createdAt；
- completedAt。

无法可信解析 usage 时结果可以按内容合同处理，但必须标记 `usage_unverified` 并参与 circuit；不得伪造 token 数。

## 5. 重试、幂等与熔断

- 429：尊重可验证的 `Retry-After`，否则使用指数退避和 jitter；不绕过 Provider 限制。
- timeout/connection/5xx：只在 Job 的最大总尝试内重试。
- 400/401/403/404：配置或请求错误，转 permanent failure；401/403 立即打开 provider circuit。
- refusal/content policy：不自动改 prompt 绕过。
- 非 JSON、HTML、Schema 无效、evidence ref 不存在：允许在最大尝试内重试；仍失败则 permanent failure。
- source revision 改变：`stale`，不重试旧输入。

以下任一条件打开 circuit，停止领取新的 Provider 调用，但不停止事实链：

- 任意一次认证失败；
- 连续 3 次配置、URL join、Content-Type 或响应合同错误；
- 连续 5 次 timeout/connection/5xx；
- 10 分钟内 retryable failure 比例超过 30%，且至少发生 10 次调用；
- usage 长期不可解析；
- prompt/Schema/model policy 不在 allowlist；
- secret gate 命中。

认证/配置 circuit 只能在配置变化并重新通过 Probe 后关闭；临时 Provider circuit 至少冷却 15 分钟，并以半开单请求验证。队列继续保留，超过 backlog 上限时只拒绝新低优先级 Job，不删除已有事实或结果。

## 6. 证据、Prompt injection 与数据最小化

README、Issue、文档、配置注释和源码都是不可信数据，不是指令。模型不获得 shell、网络、MCP、computer-use、部署或第三方代码执行工具。

进入 Provider 前执行 allowlist builder、长度限制和 secret scanner。绝不发送：

- GitHub token、Sub2API key 或其他 credential；
- Production secret、SSH key、certificate private key；
- Basic Auth credential 或 hash；
- D1 device ID、Action、Feedback、decision history；
- EnvironmentFile、`.env`、Runtime environment 或私有仓库内容。

允许发送的范围仅为公开 GitHub metadata、受限的公开 README/license/manifest/source snippets、Rardar 静态事实和匿名 RequirementProfile。secret pattern 命中后 fail closed，不自动“清洗后继续”。

每条判断必须引用输入中真实存在的 evidence ID。不存在、重复归属或版本不匹配的引用使结果无效。URL、路径、package 名和命令仍作为不可信显示数据处理，不自动执行。

## 7. AI 结果产品语义

“为什么爆发”在合同和 UI 中命名为：

> **AI 爆发原因判断**

不得显示成确定的爆发原因。页面必须分开：

- 事实：24h `+N Star`、GitHub Trending 名次、最近 Release、最近 Push；
- 模型判断：可能由什么推动、代表什么趋势、为什么当前值得理解。

每条 AI 判断至少包含：

```text
evidenceRefs
confidence
limitations
sourceRevision
model
reasoningEffort
promptVersion
schemaVersion
generatedAt
```

AI 不得修改、过滤、插入或补齐今日爆发榜名次，也不得把用户反馈写成全局事实。

## 8. Generation 集成

- AI result 采用 immutable object + digest；
- current generation 只引用发布时已 ready、digest 正确、source revision 完全匹配的结果；
- generation 发布后，新 AI 结果不能改变该代页面；
- 在线找项目 Job 固定 generationId、RequirementProfile hash、候选和 profile revisions；
- Job 完成不修改 `current.json`；
- AI store 损坏或 Worker 停止时隐藏增强并报告 degraded，事实 generation 继续可读；
- AI 网络调用期间不得持有 generation data lock；
- AI Worker 不直接发布未经本地校验的结果，也不拥有 pointer 写权限。

首版 AI profile 对爆发榜是 optional artifact；AI 未配置、排队、失败或 stale 都不阻塞事实 publication。

## 9. 进程与 Secret 隔离

推荐拓扑：

```text
rardar.service
└─ Manager
   ├─ Website
   └─ Scheduler

rardar-ai-worker.service
└─ AI Worker
```

AI Worker 故障不得导致 Website/Scheduler 重启，也不得阻塞事实采集或 generation publication。Worker 第一版 concurrency 固定为 `1`。

非敏感配置建议：

```text
RARDAR_AI_ENABLED=false
RARDAR_AI_PROVIDER=sub2api
RARDAR_AI_BASE_URL=https://api.cosflow.icu
RARDAR_AI_MODEL=gpt-5.6-sol
RARDAR_AI_DEFAULT_EFFORT=high
RARDAR_AI_DEEP_EFFORT=xhigh
RARDAR_AI_MAX_CONCURRENCY=1
```

敏感配置只有：

```text
RARDAR_AI_API_KEY
```

建议未来放入独立 root-owned secret 文件，例如 `/etc/rardar/rardar-ai.secret`；本文不声称该文件已经创建。Key 只能由 AI Worker 继承，不得进入 Git、README、generation、AIJob payload、D1、Website environment、浏览器、Nginx、普通 Runtime 日志或 PR body。

Worker 不获得 Website 不需要的 secret、D1 用户表读取权限、第三方代码执行权限、Production 部署权限或 `current.json` 修改权限。最终 systemd 内存/CPU/IO 边界必须在独立实现与部署 PR 中用真实资源数据确定。

## 10. Adapter 合同

v1 只实现窄 Sub2API adapter：

```text
normalizeBaseUrl(config) -> normalized provider config
joinEndpoint(normalized config, responses) -> exact HTTPS URL
executeComplete(jobSpec, schemaMode) -> complete provider response
classify(error/response) -> stable error code + retryability
extractUsage(response) -> observed usage facts | unverified
```

adapter 不负责业务重试、Job lease、证据版本、effort 选择、Schema/evidence 验证或结果发布；这些属于 Rardar。Background/Batch 若未来启用，应新增版本化 adapter capability，不能改变普通完整请求的最低可用路径。

## 11. 测试与启用门禁

实现 PR 至少覆盖：

- fake Provider 下的六态、lease、crash recovery、active-job 唯一性和重复请求幂等；
- concurrency=1、backlog=500、输入/输出/timeout/retry 上限；
- 429/5xx/timeout/401/refusal/HTML/invalid JSON/invalid Schema 分类；
- circuit 打开、半开、Probe 后恢复，事实 publication 始终继续；
- source revision 在排队、运行和完成后变化时都不发布旧结果；
- URL A/B join、双 `/v1`、根 HTML、重定向、Content-Type 和 JSON body；
- NATIVE 与 LOCAL_SCHEMA_VALIDATION_FALLBACK；
- prompt injection、虚假 evidence ID、超长输入和 secret pattern；
- usage 字段存在/缺失/非法，不补造 token；
- generation 只引用 ready/current profile，AI outage 不阻塞纯事实榜；
- Worker secret 不进入 Website、Job、D1、generation、日志或状态接口。

Provider capability probe、真实模型 smoke test、secret 配置与 Production 启用必须是后续独立高风险任务。本 RFC 不执行这些操作。

## 12. 已接受与仍待验证

已接受：

- Sub2API Provider 与预期 `api.cosflow.icu` 标识；
- `gpt-5.6-sol` 单模型；
- medium/high/xhigh 任务分层；
- 暂不设置货币硬预算；
- Rardar-owned durable queue + 独立 Worker；
- Background/Batch/prompt cache 可选，Streaming Deferred；
- 默认 disabled、独立 secret、concurrency=1；
- AI 失败不阻塞事实榜。

仍待独立 Probe/实施验证：

1. Sub2API exact capability probe 结果；
2. 实际 API 权限与 rate limits；
3. Structured Outputs 透传或本地 fallback；
4. canonical endpoint join；
5. Sub2API exact version/commit、部署日期和安全状态；
6. AI Worker 最终 systemd 资源值；
7. 真实调用 latency 与 token usage。

这些未验证项不能被文档或代码默认值伪装成已通过。
