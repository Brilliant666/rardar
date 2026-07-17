# Rardar Data Model

本文记录 Rardar 的 JSON 数据契约、generation 发布边界和兼容规则。它描述结构与发布协议，不替代 `pipeline.audit_data` 的跨文件语义审计。

## 核心产物

| 产物 | 路径 | Schema | 版本字段 |
| --- | --- | --- | --- |
| 当前 generation 指针 | `data/current.json` | `current-generation.schema.json` | `schemaVersion` |
| generation manifest | `data/generations/<id>/manifest.json` | `generation-manifest.schema.json` | `schemaVersion` |
| GitHub 事实快照 | `<generation>/snapshots/latest.json`、`history/*.json` | `github-snapshot.schema.json` | `schema_version` |
| 技术动态 | `<generation>/signals/latest.json` | `technical-signals.schema.json` | `schemaVersion` |
| 只读静态证据 | `<generation>/analysis/*.json` | `static-evidence.schema.json` | `schemaVersion` |
| 项目中文画像 | `<generation>/enrichment/*.json` | `project-enrichment.schema.json` | `schemaVersion` |
| 动态中文画像 | `<generation>/signals/enrichment.json` | `signal-enrichment.schema.json` | `schemaVersion` |
| 前端目录 | `<generation>/catalog/latest.json` | `catalog.schema.json` | `schemaVersion` |
| Codex 队列 | `<generation>/queues/codex.json` | `codex-queue.schema.json` | `schemaVersion` |

这里的 `<generation>` 表示 `data/current.json` 一次解析后得到的 `data/generations/<generationId>/`。页面、API、审计命令和下一轮增长基线不得分别重读指针或拼接 flat 路径。

Schema 使用 JSON Schema Draft 2020-12，并限制必填字段、对象额外字段、字段类型、数组成员、枚举、时间、HTTP(S) URL、`owner/name` 仓库身份、字符串长度和数值范围。Schema 只引用仓库内文件，验证过程不会联网获取契约。

## Stable Project Identity v1

新项目级 JSON 产物使用以下版本化身份：

```json
{
  "projectIdVersion": 1,
  "projectId": "<human-readable-prefix>--<20 lowercase hex>"
}
```

identity v1 的唯一正式算法是：

1. 输入必须是严格合法且未做前后空白修剪的 ASCII GitHub `owner/repo`：owner 为 1–39 个字母、数字或内部单连字符，repository name 为 1–100 个字母、数字、点、下划线或连字符，且不接受空段、额外斜杠、`.`、`..`、URL、`.git` 后缀归一化、反斜杠或控制字符；
2. 仅为身份计算把完整 repository 转成 ASCII 小写，原始大小写的 `repo` / `repository` 字段继续作为来源与展示值；
3. 把规范化 repository 中每段连续的非 `[a-z0-9]` 字符替换为 `-`，移除首尾 `-`，取前 64 个字符并再次移除结尾 `-`，得到可读前缀；
4. 对规范化 repository 的 UTF-8 字节计算 SHA-256，取前 20 个小写十六进制字符（80 bit）；
5. 拼成 `<prefix>--<digest>`。输出只含小写字母、数字和连字符，最长 86 字符，可安全用于 JSON 文件名、URL path segment 和后续数据库字段。

80 bit 摘要在一百万个不同 repository 的生日碰撞概率约为 `4.1 × 10^-13`，同时保留便于日志识别的短 ID。算法仍不假设碰撞不可能：同一 generation 中 projectId 重复、同一规范化 repository 重复，或 projectId 不能从 payload repository 精确重算时，构建和 audit 必须失败。`owner/foo.bar` 与 `owner/foo-bar` 因摘要输入不同而得到不同 ID；`Owner/Repo` 与 `owner/repo` 得到同一 ID。owner 转移或仓库改名在 v1 中产生新身份，不推断 GitHub rename 连续性；仅大小写变化不产生新身份。

`contracts/project-identity-v1.vectors.json` 是跨语言 golden vectors。Python 提供正式算法，Node 测试读取同一 vectors 检查消费端行为，不能另行发明 TypeScript 变体。

新发布组合固定为：

| Artifact | 新版本 | 身份要求 |
| --- | --- | --- |
| Catalog | v3 | 顶层 `projectIdVersion: 1`；每个项目保存可重算且唯一的 projectId，legacy `slug` 只用于兼容/展示 |
| Static evidence | v2 | payload 同时绑定 repository 与 projectId，文件名为 `<projectId>.json` |
| Project enrichment | v2 | payload 同时绑定 repository 与 projectId，文件名为 `<projectId>.json` |
| Codex Queue | v2 | 项目 task ID、输入路径和输出路径绑定 projectId；顶层声明 `projectIdVersion: 1` |

Catalog v3 只能配 Queue v2；Catalog v1/v2 继续配 Queue v1。未知组合、新旧字段混用或跨文件身份不一致均 fail closed。identity v1 能区分旧 slug 碰撞对，但在 P1-6C 完成路由和 UI 迁移前，当前 Catalog 的 legacy `slug` 仍必须唯一；若 snapshot 或 legacy artifact 暴露同一旧 slug 对应多个 repository，构建与 audit 都按 unresolved collision 拒绝发布。

## 验证顺序

```text
严格 JSON 解析
→ JSON Schema 校验
→ repository、projectId 与项目产物文件名核对
→ 完整候选 generation 的跨文件一致性审计
→ manifest 记录全部产物 SHA-256 与审计摘要
→ 候选目录原子重命名为不可变 generation
→ `current.json` 临时文件、fsync 与原子替换
```

`pipeline/schema_validation.py` 提供：

- `validate_payload`：返回包含 JSON Pointer 的全部结构错误；
- `require_valid`：失败时抛出 `ArtifactValidationError`；
- `load_validated_json`：严格解析并验证单个文件；
- `validate_data_tree`：直接验证给定 flat 树或候选 generation；
- `strict_json_loads`：拒绝重复键、`NaN` 和 `Infinity`；
- `strict_json_dumps`：禁止写出非标准数值；
- `atomic_write_validated_json`：校验产物类型与目标路径后，在同目录暂存并原子替换。

`npm run data:validate` 是独立结构验证命令：存在 current 指针时，它先验证指针、manifest、路径和哈希，再校验同一个 generation；不存在指针时才接受完整、合法的旧 flat 树用于迁移。`npm run data:audit` 对同一个已解析 generation 执行数量、时间、URL、增长、信源、历史和队列一致性检查。指针一旦存在，普通页面、调度、validate、audit 和正常 publish 的任何解析失败都直接失败，不回退到 flat 数据；只有用户显式指定 retained target 的 rollback 可以进入下述灾难恢复路径。

## Generation 发布协议

```text
读取并固定 base generation
→ 在 data/generations/.candidates/<id>/ 构建完整候选
→ Schema gate
→ 跨文件 audit gate
→ ready manifest + 全部产物哈希
→ 获取 data directory 跨进程锁
→ 精确比较 baseGenerationId（CAS）
→ 候选目录原子重命名
→ 原子替换 current.json
```

- `current.json` 是唯一可变发布状态，字段包含当前代、上一代、发布时间和 manifest 哈希；
- ready generation 不允许原地修改；读取时会再次核对 manifest 与全部产物哈希；
- Git 属性对 `data/current.json` 与 `data/generations/**` 禁用换行转换，保证不同平台 checkout 后仍保持 manifest 绑定的原始字节；
- `healthy` 或只有 warning 的 `degraded` 审计结果可以发布，`errorCount` 必须为 0；
- Schema、审计、临时写入、目录重命名、指针替换或并发 CAS 任一步失败，旧 current 和增长快照保持不变；
- 构建、Schema 或审计失败写入 failed manifest；发布冲突后的 ready candidate 与指针中断后的 orphan generation 保持不可变，错误码、candidate ID 和阶段由命令或 scheduler 状态记录；
- 中断后已重命名但尚未被指向的 orphan generation 可安全重试；
- `npm run data:generation:publish -- <generation-id>` 可重试 ready candidate 或 orphan 的同一套 CAS 发布协议；
- 回滚必须显式指定保留的 ready generation；在同一个 canonical data lock 内，先完整验证 generation ID 与路径、ready manifest、重新计算并复核 manifest digest、全部 artifact hash、Schema 和跨文件 audit，全部通过后才读取 current 并原子替换指针；
- `refresh` 必须产生晚于当前快照的新增长基线；`derive` 的快照和 history 哈希必须与 base generation 完全一致。

## 本地 Web 消费协议

默认 `vinext dev` 的 Cloudflare RSC Worker 不直接使用 `node:fs` 读取宿主工作区。Vite Node host 注册只接受 loopback socket 和当前进程随机 token 的内部数据桥；可信 Vinext 配置通过 Worker binding 固定桥 origin，Worker 不从外部请求的 `Host` 构造目标，并且每次只发起一次 no-store 请求：

```text
网页或 API 请求
→ Worker 读取配置固定的 127.0.0.1 bridge origin
→ token 保护的 Vite host bridge
→ loadPublishedBundle(data directory)
→ 一次读取 current.json
→ pointer、路径、manifest、ready、清单与全部 SHA-256 验证
→ 同一 generation 的 catalog/signals/enrichment/queue bundle
→ 单个 Worker 响应
```

桥不会缓存上一份健康数据，也不依赖 Vite HMR。`current.json` 原子切换后，下一次桥请求会读取新 generation；已经取得的 bundle 保持内部一致。伪造入站 `Host` 不能改变 token 的接收端。current、manifest 或任一 artifact 损坏时，桥返回 503，健康端点和页面 fail closed，不读取 flat 数据；显式 rollback 恢复后无需重启 Vinext。桥只定义 Rardar 的本地 `vinext dev` 消费边界，不表示 `vinext start` 或线上 Worker 可以访问宿主文件，也没有改变 Cloudflare D1 binding。

## 写入边界

以下入口在候选或 staging 写入前复用同一契约：

- GitHub 与技术动态采集 CLI；
- 第三方仓库只读静态分析输出；
- catalog 与 Codex queue 独立调试 CLI；
- `data:refresh` 完整候选生成与发布；
- `data:derive` 从当前事实和 flat enrichment staging 本地重建并发布。

候选内部的关联文件仍使用批量写入，先完成所有 payload 的验证和严格序列化再替换；对读者可见的边界则只有最后一次 `current.json` 切换。候选内部任一失败不会修改当前已发布 generation。

独立采集器和静态扫描器在共享锁外完成网络与磁盘扫描，并先验证候选 payload；锁内只重复边界验证、比较产物时间和执行原子替换。时间早于现有正式文件的候选会被拒绝，项目画像/静态证据也不能覆盖已属于另一个仓库的碰撞文件名，因此慢任务不会以旧结果回写，也不会长期占用数据锁。

Codex enrichment 采用显式草稿和 staging 边界：先将结果写到 `data/` 之外，再运行 `python -m pipeline.ingest_enrichment --kind project|signal --input <draft>`。入口会先解析 `..` 与符号链接并拒绝整个 `data/` 树内的草稿，再在共享数据锁内严格解析、校验、按仓库身份确定 flat staging 目标并原子替换。队列中的 `outputPath` 表示 staging 归属，不授权直接覆盖；只有后续 `data:derive` 通过 generation gates 后才会成为页面数据。

Project enrichment v2 同时绑定 identity v1 与两项来源版本：`projectId` 必须由 `repository` 精确重算，`sourcePushedAt` 必须与当前 Catalog v3 项目的同名字段字符串完全相同，`sourceAnalysisAt` 必须与当前 static evidence v2 的 `analyzed_at` 字符串完全相同。Codex 只能从 Queue v2 原样复制；repository、projectId、文件名或任一来源版本不一致，`analyzedAt` 无有效时区，或画像时间早于来源静态证据时，catalog 和 queue 都把画像判为无效或过期，generation audit 不允许发布。ingest 负责 Schema、草稿边界、身份和时间先后校验，不把进入 flat staging 等同于正式发布。Project enrichment v0/v1 只作为 legacy 输入或 retained generation 兼容，不会被静默升级为 v2。

## Catalog v2/v3 评分契约

Catalog v2 使用固定的 `scoreModelVersion: evidence-v2`，把不同证据能力拆成五个独立维度：

| 字段 | 回答的问题 | 证据边界 |
| --- | --- | --- |
| `attentionScore` | 现在是否值得先看 | 区间增长或明确代理、新鲜度、维护、召回信号、持久热度与风险降权 |
| `enduranceScore` | 是否有长期生态和持续维护线索 | 仓库年龄、总 Star、Fork、近期维护、多快照覆盖；未达阈值时必须标记结构代理 |
| `engineeringReadiness` | 静态工程材料是否就绪 | 只使用与当前推送匹配的只读静态检查；没有当前证据时为 `null`，永不代表运行可靠性 |
| `reuseFitScore` | 是否适合一个明确任务 | 通用目录没有用户任务、约束与验收标准，因此必须为 `null`；中文画像只提供场景假设 |
| `evidenceCompleteness` | 当前证据覆盖了多少层 | 事实快照、精确增长、当前静态证据、版本绑定画像和多周期证据的覆盖度；不是质量分 |

每个维度都在 `scoreExplanations` 中重复绑定当前分值，并分别列出 `facts`、`proxies`、`limitations` 和 `upgradeConditions`。v2 recommendation 只允许“了解 / 收藏 / 隔离试用 / 观望”；默认流水线没有运行第三方代码，因此不能输出“直接复用”。“隔离试用”还要求当前静态工程证据、足够就绪度、关注阈值、GitHub API 许可证和无风险关键词。

`pipeline.audit_data` 对 v2/v3 从同一 generation 的快照、history、对应版本静态证据和画像调用生产构建器重算完整有序 projects；分数、说明、推荐、排序或 v3 身份任一不一致都会使候选 generation 发布失败。v1 不走这条重算规则，以保持既有 ready manifest 的历史审计摘要和显式 rollback 不变。v3 只增加 identity v1 契约，不改变 `evidence-v2` 的评分含义。

网页在一个服务端入口归一化三种版本。v3 按 identity v1 与 `evidence-v2` 字段读取，v2 继续按原评分字段与 legacy slug 读取；v1 只把旧 `globalScore` 保守映射成 Attention，把旧 Endurance 保留，其余三项均显示未知。旧 `reuseScore` 不会被解释成 Engineering Readiness，旧“试用 / 复用”建议也只会显示为“隔离试用”。未知 Catalog 版本直接失败。

## P1-6B D1 Stable Project Identity

真实项目行动、反馈与个性化状态保存在 Cloudflare D1，而不是 generation JSON 或浏览器存储中。P1-6B 增加 generation-bound 身份目录和 canonical v2 表；已有 slug 表保持原结构，作为旧代码回滚边界而不是新的事实主键：

```text
project_identity_catalog
  generation_id
  project_id_version
  project_id
  canonical_repository
  project_slug

project_identity_runtime
  singleton active generation
  generation_id + published_at + published_at_micros

project_action_events_v2
  id
  device_id
  project_id_version + project_id
  project_slug + catalog_generation_id
  action
  occurred_at
  idempotency_key

project_action_state_v2
  device_id + project_id (project_id_version = 1)
  project_slug + catalog_generation_id
  highest_stage
  opened_at / saved_at / tried_at / cloned_at / reused_at
  updated_at

feedback_v2
  device_id + project_id (project_id_version = 1)
  project_slug + catalog_generation_id
  value
  created_at / updated_at

decision_events_v2
  id
  legacy_event_id
  device_id
  project_id_version + project_id
  project_slug + catalog_generation_id
  value
  occurred_at
```

每个 Action、feedback、recommendation 或 metrics 请求先从一次 published-data bridge 解析取得同一个 generation 和 Catalog，再为该 generation 构造完整身份目录。Catalog v3 项目必须携带 `projectIdVersion: 1`，且 projectId 能从 `repo` 精确重算；Catalog v1/v2 没有身份字段时，从其 `repo` 机械计算 identity v1。规范化 repository、projectId 和 legacy slug 在该 generation 内都必须形成可验证的一对一关系；任一重复、伪造、缺失或歧义都会在 D1 写入前失败。

`project_identity_catalog` 记录已验证的 generation、规范化 repository、projectId 和兼容 slug，映射建立后拒绝 UPDATE、DELETE 与非等价 replacement；全部 retained mappings 还必须同时满足 projectId ↔ canonical repository 的全局一对一关系，且同一 legacy slug 不能跨代改绑另一个 projectId。JS 全量 preflight、正式 INSERT trigger 与事务内 pairwise guard 都会检查该约束，两个 publisher 在 preflight 后竞争也不能提交碰撞关系；错误稳定为 `project_identity_collision`。`project_identity_runtime` 同时保存 generation、pointer `publishedAt` 与微秒顺序，并要求文本时间能精确重算同一个微秒值。adoption 在同一 D1 原子 batch 内完成完整 legacy preflight、active row、mutable State 重键、backfill 和兼容投影，任一步失败都整体回滚。只有发布时间更新的 verified pointer 能推进或显式回滚 active generation；较旧慢请求和相同发布时间的不同 generation 均 fail closed，不能回退全局 legacy capture 边界。API 的 legacy slug 解析只查询本次请求 generation 的 verified 映射，不能把数据库中其他 retained generation 的历史映射当作当前事实。客户端提交的 repository、发生时间或单独 slug 哈希都不构成身份来源；同时提交 projectId 与 slug 时必须解析到同一项目，否则返回冲突。

canonical JSON 写请求必须同时提供数值 `projectIdVersion: 1` 与 `projectId`；单项 GET 查询使用同名 query pair。P1-6C 前，只有 slug 的 legacy 请求继续兼容；双 selector 共存时必须指向同一项目。projectId pair 缺一或 selector 形状错误返回 `invalid_project_selector`，必须指定单项目的操作在两种 selector 都缺失时返回 `missing_project_identity`；Action/feedback collection GET 可以只给 `deviceId` 并返回 current Catalog 中的记录。错误版本返回 `unsupported_project_id_version`，畸形 ID/slug 返回 `invalid_project_id` / `invalid_project_slug`，未知 ID 或 slug 分别返回 `unknown_project_id` / `unknown_project_slug`，歧义 slug 返回 `ambiguous_project_slug`，双 selector 不一致返回 `project_identity_conflict`。Catalog 自身非法，或 stored identity 的版本/格式无法可信解析时服务端 fail closed。结构合法但暂时不在 current Catalog 的历史 projectId 不会被删除，也不会让集合 API 整体失败：当前集合和推荐会省略它；不需要 slug 投影的全局反馈 State 聚合以及近 7 天 Event/decision 周指标继续直接按 canonical projectId 汇总。canonical 写请求明确拒绝客户端 `repository` 和 `occurredAt`。Action、feedback 与 State 项返回 `projectIdVersion`、`projectId` 和临时兼容 `projectSlug`；历史记录以 projectId 和其不可变 generation mapping 证明身份，响应将当前记录的兼容 slug 归一为 current Catalog 值，不把 slug 当成跨 generation 身份不变量。recommendation 项返回 Stable ID 并在 P1-6C 前保留既有 `slug` 字段。

### Historical Identity Bundle 与首次 adoption recovery

D1 adoption 的历史身份来源是版本化 `Historical Identity Bundle v1`，而不是 D1 中陈旧的 slug map。构建器在 canonical data lock 内读取一次 current pointer，并严格验证 current 与全部可见 retained final generation。独立 `generations` 清单保存 generation ID、manifest `createdAt`、manifest SHA-256、Catalog Schema 版本与 active 标记，即使某代 Catalog 没有项目也保留 provenance；`mappings` 保存 identity v1、canonical repository、legacy slug，并逐字段绑定所属 generation。active generation 的 `publishedAt` 必须精确等于 pointer；retained generation 因可能被多次 rollback 激活而只能为 `null`，不得用 manifest 创建时间冒充发布时间。`.candidates`、flat staging 和隐藏路径不参与解析，任何可见 final 的 hash、Schema、Audit、路径或身份冲突都会阻止整个 adoption。

`project_identity_generation_evidence` 保存每代不可变的 `generation_created_at + manifest_sha256 + catalog_schema_version`，并与 `project_identity_catalog` 成对校验；role-dependent pointer 时间只保存在 `project_identity_runtime`。retained legacy slug 若在多个已验证 generation 中始终属于同一 projectId，则使用 manifest 创建时间精确到微秒选择最新 witness；若归属不唯一则失败，绝不按文件顺序或字符串第一项猜测。事务内 `project_identity_adoption_session` 和 `project_identity_adoption_allowed_mapping` 只为本次 backfill 临时放行精确 historical mapping，写入仍需匹配原 legacy source，提交前两表必须清空；应用的普通写路径仍只能使用 active generation。

无法由 current 或 retained Catalog 证明 repository 的 legacy 行只能依据 exact、版本化 disposition policy 处置。当前 policy `2026-07-18.1` 仅把 `officecli` 的 `feedback` source 记入 append-only `project_identity_unresolved_legacy`：保留原 feedback/history，不写 repository、projectId 或 device ID，不进入 canonical metrics/recommendations。ledger 的 source table/key、slug、reason 和 policy version 不可 UPDATE/DELETE；未来解析必须使用单独、显式、可审计的 resolution migration，不能修改 policy 后把旧 quarantine 静默解释成新身份。`oomol-lab--open-connector` 有唯一 verified retained repository，因此按该 mapping 机械 backfill，而不是 quarantine。

## Canonical v2 Event、State 与 feedback

`project_action_events_v2` 的应用写入边界只允许 INSERT，数据库触发器拒绝 UPDATE、DELETE 和 identity replacement。新事件的 `occurred_at` 由 Worker 生成带时区的 RFC3339 UTC 时间；API 不接受客户端时间。幂等键在同一 `device_id` 内唯一：相同键与相同 projectId/行动是安全重放，不产生第二个 Event；相同键绑定不同 projectId 或行动返回冲突。一次用户意图的即时网络重试和页面内再次尝试复用同一键，成功后的新一次真实行动生成新键，因此跨周重复行动仍能追加。

Event INSERT 通过 `project_action_events_v2_sync_state` 在同一 SQLite 写入中更新 `project_action_state_v2`。State 以 `device_id + project_id` 唯一，`project_id_version` 固定为 1；`highest_stage` 按 `opened < saved < tried < cloned < reused` 单调推进，各阶段时间只在该阶段真实发生时记录，不因最高阶段倒推缺失步骤。按钮和观察列表由 State 投影，不能扫描 Event 充当当前状态。

Weekly Acted Projects 只查询 canonical v2 Event，在一次服务端 `now` 下使用包含下界的 `[now - 7 days, now]` 窗口，对 `tried`、`cloned`、`reused` 的不同 projectId 计数；`opened` 与 `saved` 仅作为辅助漏斗指标。同一项目在窗口内多次行动只计一次，旧事件离开窗口后的新真实行动可重新计入，State 不参与反推。

`feedback_v2` 保存每个设备和 projectId 的当前反馈，`decision_events_v2` 保存反馈变化历史；新反馈 INSERT 或真实值变化在同一数据库写入中追加 history。推荐、当前反馈计数和辅助反馈指标使用 canonical projectId，响应同时返回 `projectIdVersion`、`projectId` 和 P1-6C 前的兼容 slug。反馈仍不属于 Weekly Acted Projects，不能冒充试用、浅克隆或确认复用。

## D1 迁移与旧代码回滚

`drizzle/0004_stable_project_identity.sql` 是本轮正式、版本化的 additive DDL：它先保留全部 legacy 表，再创建 `project_identity_catalog`、`project_identity_runtime`、canonical v2 表、索引、反馈历史链和完整触发器边界。DDL 不读取 generation 文件，也不从 slug 猜测身份；runtime bootstrap 通过 Vite raw module 直接拆分并重放这一个正式文件，fresh-D1 与 migration 路径不能维护另一套漂移的 stable schema。运行时 adoption 不是第二份 DDL migration，它只接收本次请求从 verified generation 导出的完整映射，完成 legacy preflight、mutable State 重键、backfill 和 active-generation 切换。因此正式 `0004` 先运行或由初始化重放时都得到相同结构，多次执行为 no-op；只执行正式 0000..0004 也必须让 canonical feedback INSERT/真实值变化生成 legacy 与 canonical decision history。

legacy adoption 只通过明确 Catalog 映射进行。`project_actions`、`project_action_events`、`project_action_state`、`feedback` 和 `decision_events` 的原始 action/value、`occurred_at`/`created_at` 与真实阶段原样保留；确定性 legacy 幂等键继续复用，不从 State 补造 Event，也不推断 owner 转移、仓库改名或缺失的历史。除 exact disposition policy 明确隔离、保留并写入 immutable ledger 的来源行外，任一 slug 无匹配或多匹配、映射冲突、非法行动或无法解析的时间都会返回稳定错误并阻止 `project_identity_runtime` 切换，不能静默跳过后声称迁移完成。

旧 `project_actions`、`project_action_events`、`project_action_state`、`feedback`、`decision_events` 及其触发器不会删除。`project_action_events_v2_legacy_projection` 和 feedback 的 legacy projection 把 canonical 写入投影到旧 slug 边界；`project_action_events_capture_stable` 以及 feedback/decision capture 只在 active generation 存在唯一映射时捕获旧代码写入。跨版本捕获按 device、project identity、action/value、幂等键和等价时间识别已投影事实，不因 UTC 文本格式化或触发器回环制造第二个 Event/history；unresolved identity 不做错误投影。若同一 projectId 的 current Catalog slug 变化，adoption 会在上述同一个原子 batch 内仅把 mutable `project_action_state`、`feedback`、`project_action_state_v2` 与 `feedback_v2` 重键到当前 slug/generation；`project_actions`、两套 append-only Action Event 和两套 decision history 的原始 slug、时间及行数不变。目标 slug 已存在另一条 State/feedback 时整批 `conflicting_project_projection`，不会先切 active row；成功后的重复 adoption 为 no-op。

应用代码回滚不执行破坏性 down migration，也不删除 canonical 或 legacy 表。回滚前的新写入以及 generation slug 切换后的 mutable State 已投影到 current slug，旧代码无需等待新写即可读取最近按钮与反馈状态；旧代码期间实际成功保存的行动和反馈会在再次升级后被捕获。旧版本仍受其全生命周期唯一约束，所以回滚期间无法保存的同阶段重复行动不会被补造。immutable legacy Events 会保留它们发生时的历史 slug；若旧代码回滚后又在当前别名产生行动，旧版按 slug 去重的周指标可能把同一 Stable ID 的多个别名分别计数，canonical v2 指标仍按 projectId 正确去重。迁移不会为了修饰旧版指标而改写或补造 Event。

只回滚 P1-6B 应用代码到仍支持 Catalog v3 的 PR #8 时，可保留当前 pointer 与 additive D1，前提是先停止写入、备份 D1，并确认当前 generation 的 legacy State/feedback 投影完成。完整回滚 Stable ID 到 pre-v3 时，还必须先降级 flat staging；在 P1-6B 代码仍运行时显式 rollback 到健康 Catalog v1/v2，并在目标 Runtime 的实际 D1 上发起一次预期会执行 adoption 的受控 GET，让 `project_identity_runtime` 验证并激活目标 generation，随后执行 Schema/Audit、停止 Runtime，最后才回滚应用代码。不得让旧代码在 D1 active mapping 仍指向另一代时接管写入。

## 兼容与迁移

历史兼容与 generation 迁移遵循：

1. Snapshot v1 保留既有 snake_case `schema_version`。早期 history 没有查询健康字段；Schema 接受该基础形状，latest 的查询覆盖继续由审计验证。
2. 五份带可信 `analyzed_at` 的静态证据迁移为 v1。两份缺少可信分析时间的历史证据标记为 v0；没有补造时间，v0 也不会被当作当前证据。
3. 四份能与可信当前静态证据形成真实时间顺序的项目画像，从 catalog 和 analysis 文件机械补入 `sourcePushedAt`、`sourceAnalysisAt`。两份静态证据没有可信 `analyzed_at`，另有一份画像早于当前静态证据；三者均保留为 legacy v0，不补造时间且永远不视为当前画像。
4. Signal enrichment v1 保留旧式条目的顶层 `generatedAt` 回退；新条目应保存逐条时间绑定。
5. Catalog 内项目级 `capturedAt` 是显示文本，顶层 `capturedAt` 才是 RFC3339 时间，两者不会混用。
6. 首个 generation 从合法旧 flat 树机械复制产物，不修改快照、history、静态证据、画像或评分；只把 Codex 队列的输入证据路径重建为该不可变 generation，并生成 manifest/current 的发布元数据。
7. `data/current.json` 缺失时，完整旧 flat 树仍可通过 `data:generation:bootstrap` 一次迁移；current 存在后，flat 的 snapshot、catalog、signals 和 queue 不再是网页或增长基线。
8. flat `analysis/`、`enrichment/` 和 `signals/enrichment.json` 继续作为静态分析/Codex staging。创建新候选时，只有目标缺失或 staging 的真实来源时间严格更新，才允许覆盖 base generation，避免旧 flat 文件回写新 generation。同一 repository 的 project artifact v1/v2 共存时必须先把 v1 在内存中机械转换为预期 v2；只有与现有 v2 payload 完全相等才清理 v1，任何字段不同都返回 `conflicting_project_artifact_versions`。analysis/enrichment 的全部文件先完成 preflight，一个冲突会使整批 adoption 零写入、零删除；不得按 Schema 版本、时间或文件顺序猜测权威版本。
9. Catalog v1 generation 保持字节、Schema 和历史审计语义不变，可继续显式回滚。评分语义迭代中派生的 v2 generation 不采集新 GitHub 事实、不修改 snapshot/history，也不把缺失证据补造成分数；只按 `evidence-v2` 重建 catalog 与依赖 catalog 的 Codex queue，并由完整 generation gate 发布。
10. P1-6A 不改写 retained generations。Catalog v1/v2、static evidence v0/v1、project enrichment v0/v1 和 Queue v1 保留各自 validator、audit 与显式 rollback；新 refresh/derive 才生成 Catalog v3、static evidence v2、project enrichment v2 和 Queue v2。
11. flat staging 可用 `python -m pipeline.migrate_project_identity --data-dir data` 预检和 dry-run，再显式加 `--apply` 迁移可信 v1 artifact；无法机械升级的 legacy v0 只报告并保留。应用代码回滚前，显式 `--to-legacy-v1` 模式（同样默认 dry-run，写入还需 `--apply`）把 static evidence/project enrichment v2 机械降为 v1：`schemaVersion: 2 → 1`、移除 `projectIdVersion`/`projectId`、恢复 legacy slug 文件名，其他事实、时间和内容原样保留。两个方向都只处理 `data/analysis` 与 `data/enrichment`，不跟随 symlink/junction，不访问或修改 current、retained generations、candidates、manifest，也不发布 generation。完整 preflight 必须在任何写入前发现 legacy slug collision、非等价目标、归属冲突和路径逃逸；apply 先原子写入并验证全部目标，再删除源。等价目标不重写，写入或源清理中断后可安全重试，完整执行后的重复 apply 为 no-op。
12. P1-6B 增加 canonical Stable Project ID D1/API 边界，同时完整保留旧 `project_slug` 表与兼容投影用于旧代码回滚。页面路由、链接、React key 和组件身份仍由 P1-6C 迁移；legacy slug 只能经本次请求的 verified Catalog 唯一解析，不能成为 canonical 事实主键。

未知版本或未版本化的新数据会失败。以后收紧字段或改变含义时应新增 Schema 版本和显式迁移，不得静默把旧数据解释为新版。

## 安全与回滚

Schema 与 generation 验证不执行候选仓库代码、不安装其依赖、不读取用户 Git 配置，也不改变静态分析的资源上限。generation ID、manifest 产物路径与符号链接都经过逃逸检查；路径必须留在当前 data/generations 根内。

回滚 Stable ID 应用代码不能忽略 flat staging 中的 v2 artifact。顺序固定为：停止写入任务 → 备份 flat staging 与 D1 → 执行 `--to-legacy-v1` dry-run → 显式 `--to-legacy-v1 --apply` → 验证全部 staging 均为旧代码可读取的 v0/v1 → 在 P1-6B 代码仍运行时显式 rollback 到健康的 Catalog v1/v2 retained generation → 在目标 Runtime 的实际 D1 上发起一次预期会执行 adoption 的受控 GET，并核验 `project_identity_runtime` → 运行 Schema/Audit → 停止 Runtime → 回滚应用代码 → 恢复 Runtime。逆向 staging 迁移不接触 generation；后续 generation rollback 仍沿用下述严格目标验证与原子 pointer 协议。

显式回滚和灾难恢复都使用 `npm run data:generation:rollback -- <generation-id>`。目标验证失败时返回结构化错误，旧 `current.json` 的原始字节保持不变。目标健康后，若当前 generation 仍可严格解析，则继续沿用正常回滚逻辑。

只有 current 无法严格解析时才进入恢复分支。该分支仅对旧 pointer 中的 `generationId` 与 `publishedAt` 分别进行有限、独立验证：安全的 generation ID 可写入新 pointer 的 `previousGenerationId`。旧 `publishedAt` 只有在可合法解析且不晚于恢复时当前 UTC 加五分钟时，才参与新 pointer 的单调时间计算；新 `publishedAt` 必须严格晚于该可信时间。超过五分钟的异常未来值、无法解析的时间，或严格递增计算发生溢出时，旧时间均视为不可信并降级使用当前 UTC。

五分钟信任窗口与 UTC 降级只适用于 current 已损坏的显式 recovery。current 仍可严格解析时继续执行原有正常回滚时间规则，不使用该窗口，也不放宽 stale publication 冲突。旧 pointer 的 manifest digest、audit、`previousGenerationId` 以及 flat 数据均不受信任，恢复 rollback 永不读取或发布 flat 数据。

若 `current.json` 是 symlink 或 junction，恢复过程不会跟随或读取链接目标作为可信 pointer 元数据。目标 generation 通过全部门禁后，仅尝试原子替换 `current.json` 目录项；若平台拒绝安全替换，则返回结构化 `pointer_write_failed`，链接目标保持不变。恢复无需反向改写既有事实，也不得从缺失时间补造历史。

本地管理器在启动任何子服务前检查声明的 Python 运行依赖。缺失时只输出 `python -m pip install -r requirements.txt` 并退出，不自动安装，也不启动会持续失败重启的 scheduler。

Schema 不能单独解决跨文件一致性、历史增长、缓存新鲜度或身份碰撞。前三项由 generation audit、manifest/hash 和请求级 generation 边界处理；Stable Project ID 由 identity v1 重算、跨产物审计和 collision/unresolved 门禁共同保证。追加式行动事件已由 PR #5 完成，评分语义已由 PR #6、提交 `ab34119` 完成，verify/CI 已由 PR #7、提交 `3430e30` 完成，P1-6A JSON 身份层已由 PR #8、提交 `d41033f` 完成。P1-6B 当前只在独立 Draft PR 中迁移 D1/API 身份，合并前不视为 `main` 完成；P1-6 整体须在后续 P1-6C 完成后才能关闭。
