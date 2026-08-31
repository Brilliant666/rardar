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
| GitHub 24h 爆发事实 | `<generation>/trending/explosion.json` | `trending-explosion-artifact.schema.json` | `schemaVersion` |
| 爆发事实来源副本 | `<generation>/trending/sources/*.json` | `trending-capture-bundle.schema.json` | `schemaVersion` |
| GitHub 近实时发现事实 | `data/artifacts/trending/discover/v1/generations/<id>/discover.json` | `trending-discover-artifact.schema.json` | `schemaVersion` |
| 发现 generation manifest / current | `data/artifacts/trending/discover/v1/{generations/<id>/manifest.json,current.json}` | `trending-discover-{manifest,current}.schema.json` | `schemaVersion` |

## Trending Observation v1

GitHub 趋势观察是 generation 之外的追加式原始事实账本。它与 `data/current.json`、retained generation、flat staging 和 D1 相互独立；generation 切换、回滚或清理 candidate 都不得改写观察历史。它是本地或 Production 运行事实而不是源码，因此 `data/observations/` 被 Git 忽略，但仍随完整 `RARDAR_DATA_DIR` 备份。

| 产物 | 路径 | Schema | 身份 |
| --- | --- | --- | --- |
| 单仓库事实 | capture bundle 的 `observations[]` | `trending-observation.schema.json` | GitHub numeric repository ID |
| 两小时采集包 | `data/observations/trending/v1/captures/YYYY/MM/DD/<captureId>.json` | `trending-capture-bundle.schema.json` | `policyVersion + scheduledAt` |

`captureId` 由固定策略与 UTC phase 机械生成，例如 `trending-v1-20260824T000000Z`。phase 必须是 `Asia/Shanghai` 时区下的偶数整点，cadence 固定为 120 分钟。`capturedAt` 是本轮 metadata 请求全部结束后的事实冻结时间；`captureDelaySeconds` 是它与 `scheduledAt` 的精确差，只有绝对延迟不超过 600 秒时 `windowEligible=true`。窗口资格是后续 24h 推导的输入条件，本层不计算 Star 增量或名次。

每条 observation 记录仓库当时的 Star、Fork、Issue、生命周期时间、默认分支、语言、topics、license、archived/disabled/fork/mirror 状态。GitHub Search 只负责候选召回；`/repositories/{numeric-id}` metadata 响应才是这些事实的权威。`recalledBy` 保存 query 或最近 26 小时 observation carry-forward 的来源、来源键、rank 与采集时间。当前复用 `pipeline.collect_github.candidate_queries` 的九条 Search query；当前 phase 之前 26 小时（含边界）的健康/降级 capture 中出现过的仓库都优先继续观察。carry-forward 超过全局 500 上限时以 `tracking_capacity_exceeded` 失败，不静默截断。

GitHub numeric repository ID 只保证 observation ledger 在 rename/transfer 前后的连续性。Catalog、路由、D1、Action 与 Feedback 继续使用 Rardar Stable Project ID；两套身份不能互相替代或在本协议内迁移。同一 capture 内，一个 ID 对应多个 `owner/name` 时返回 `repository_identity_changed_during_capture`，一个名称对应多个 ID 时返回 `repository_name_identity_collision`；不同 capture 之间允许同一 numeric ID 改名。

capture 使用严格 UTF-8 JSON、拒绝重复键与非有限数。顶层 `digest` 是去除 `digest` 字段后，对 Unicode、不转义、key 排序、无多余空白的 canonical JSON bytes 计算的 SHA-256。写入先在目标目录创建并 fsync 临时 regular file，完成 Schema、跨字段语义和 digest 验证后，再以 hard-link no-replace 原子发布；既有合法 slot 返回 `already_captured` 且不访问 GitHub，损坏、身份不符、symlink、junction/reparse point 或路径逃逸都 fail closed，永不覆盖。

Observer 使用独立于 generation data lock、Manager 和 Scheduler 的单实例锁。重叠调用立即返回 `skipped_overlap`，不等待、不访问 GitHub、不创建 bundle。CLI 不接受 token 参数，也不匿名降级；新采集必须从 `GITHUB_TOKEN` 环境变量取得 token：

```powershell
python -m pipeline.collect_trending_observations `
  --data-dir data `
  --scheduled-at 2026-08-24T08:00:00+08:00 `
  --timezone Asia/Shanghai `
  --limit 500 `
  --dry-run
```

不提供 `--scheduled-at` 时选择距离当前时间最近的固定两小时 phase。`--dry-run` 可以请求 GitHub 并构造完整 bundle，但不创建或修改 data 路径。此工程轮不把 CLI 接入现有每日 Scheduler/Manager；长期 2h 调度与部署属于独立运维变更。

`python -m pipeline.audit_trending_observations --data-dir <isolated-data>` 是只读 store audit：验证目录与文件名、no-follow regular-file 边界、严格 JSON、Schema、phase/时间/窗口、digest、slot 唯一性、身份、计数、26 小时 carry-forward 完整性及仍在 retention store 内的跨 capture rank 引用、90 天 retention metadata 以及残留临时文件。无结构错误但含失败 query、incomplete Search 或 metadata failure 的历史返回 `degraded`；任何不可信字节或路径返回 `failed`，audit 不修复、删除或重写文件。

原始 capture 的 retention class 固定为 `raw_2h_observation`，`retainUntil` 必须机械等于 `capturedAt + 90 days`。本轮只记录保留元数据，不实现自动清理。历史事实只能通过后续 capture 补充，不能反向修改。

## Trending Explosion Artifact v1

`TrendingExplosionArtifact v1` 是 retained generation 内的可选事实产物。它把 generation 外的两小时 observation ledger 机械派生为一个正式 24 小时窗口，并冻结完整重算所需的源字节：

```text
<generation>/trending/explosion.json
<generation>/trending/sources/current.json
<generation>/trending/sources/baseline.json          # 端点存在时
<generation>/trending/sources/partial-XX.json        # pending 实际窗口需要时
<generation>/trending/sources/coverage-witness.json  # 证明 baseline_missing 时
```

`schemaVersion=1`、`policyVersion=trending-explosion-v1`，正式 `window.endedAt` 必须是 `08:00 Asia/Shanghai` 的固定两小时 phase，`durationHours=24`、`toleranceSeconds=600`。当前端点必须存在、通过 strict JSON、Schema、payload digest、capture ID、scheduled path 与 `windowEligible` 验证；否则 derive fail closed。T-24h 端点存在且 eligible 时为 `exact`；账本尚未覆盖完整 24 小时时为 `warming_up`；已有更早 capture 但目标端点缺失或不 eligible 时为 `baseline_missing` 并明确 degraded。

仓库连续性只按 GitHub numeric repository ID。`exactRanked` 机械计算 `current.totalStars - baseline.totalStars`，按 `observedStarDelta DESC`、`totalStars DESC`、`repository ASC` 排序，最多保存 500 个完整候选，而不是只保存 UI Top 20。rename/transfer 以当前名称展示并保存 `previousRepository`；archived、fork 和 mirror 保留独立事实。负增量、current disabled 或跨端点名称→numeric ID 冲突进入 `conflicts`，不得用外部 reported delta 修复。

没有合法 exact baseline 的 current 项进入独立 `pendingRanked`。derive 只扫描窗口内最多 11 个中间固定 phase，以最早实际观察和 current 计算 partial hours/delta；单观察点保持 null，不线性外推、不占 exact rank。`exactRanked`、`pendingRanked` 与 `conflicts` 的 numeric ID 必须互斥。

源 capture 先 no-follow stable-read，再以原始字节复制；每个 source reference 同时绑定 payload digest 与文件 SHA-256，全部副本和 explosion artifact 都进入 manifest artifact/hash 清单。semantic Audit 只使用 generation-local bytes 完整重算窗口、分区、delta、排序、rank、coverage 和 provenance。因此原始 90 天 observation store 删除后，retained generation 仍可独立完成 Schema、Audit、manifest integrity 和 rollback target verification。Artifact 不允许 AI 简介、爆发原因、confidence、模型、复用建议或工程成熟度字段；这些属于后续独立 AI artifact。

`python -m pipeline.derive_trending_explosion --data-dir <path> --window-end <RFC3339>` 在 generation 外准备来源，以 `operation=derive` 创建 candidate，经 Schema/Audit 后用既有 base-generation CAS 发布；`--dry-run` 不创建 candidate 或改动 current。同一窗口与端点 digest 返回 `already_derived`，较旧窗口返回 `stale_explosion_window`，同窗口不同端点字节返回 `explosion_source_conflict`。竞争 publisher 获胜后 loser 返回 `stale_base_generation`，ready candidate 保留诊断，winner current 不被覆盖。

无关 refresh/derive 会 byte-exact 保留 source 副本和事实窗口，只机械把 `explosion.json.generationId` 重绑定到新 enclosing generation，然后重新执行 Schema/Audit。专用 Explosion derive 才能用新窗口替换该 namespace。历史 generation 没有 `trending/` 仍然合法；一旦出现任一 Explosion 文件，artifact 与完整 source inventory 必须同时通过审计。

这里的 `<generation>` 表示 `data/current.json` 一次解析后得到的 `data/generations/<generationId>/`。页面、API、审计命令和下一轮增长基线不得分别重读指针或拼接 flat 路径。

## Trending Discover Artifact v1

`TrendingDiscoverArtifact v1/v2` 是与每日 generation 分离的近实时事实产物。它在每个合法 Observation 完成后读取最新 eligible capture、向前最多 26 小时的 eligible captures，以及当前已验证 Today Explosion exact 集合，并发布到独立 immutable store。store 路径为了 retained generation 与消费者兼容保持不变：

```text
data/artifacts/trending/discover/v1/current.json
data/artifacts/trending/discover/v1/generations/<discoverGenerationId>/
├─ manifest.json
├─ discover.json
└─ sources/
   ├─ capture-01.json ... capture-14.json
   ├─ today-explosion.json
   └─ today-manifest.json
```

新发布合同为 `schemaVersion=2`、`policyVersion=trending-discover-v2`；retained v1 generation 仍按 v1 规则严格读取、重算和 rollback。每个 generation 复制 source 原始字节，manifest 绑定全部文件 SHA-256，artifact 另有 canonical payload digest；`current.json` 只通过独立 data lock、CAS 与原子替换推进。policy version 参与 generation identity；只有 policy、latest capture、Today generation 和 Today digest 都一致才返回 `already_derived`。并发第二写者不得覆盖胜者。Discover 不读取或写入 D1，也不改写每日 generation。

Today exact 排除只按 GitHub numeric repository ID。Today current、manifest、Explosion artifact 或 digest 不能安全验证时 derive fail closed；不得按 `owner/name` 猜测，也不得回退到旧或 flat 数据。rename/transfer 保持 numeric ID 连续，fork/mirror 保留为独立仓库事实；disabled、负 Star 增量、名称到 numeric ID 冲突进入 conflicts 或被排除。

一个 v2 发布项目只属于一个透明阶段：最近 4 个计划小时首次出现时进入 `just_discovered`，不强制增长；其他项目必须满足至少两个有效区间、正总增量、绝对增长 `>=10` 或相对增长 `>=1%`，并在窗口内形成至少 2 个连续正增长区间。通过门禁且连续观察不足 20 小时为 `rising`；连续观察至少 20 小时为 `near_validation`，产品文案为“待日榜验证”。页面分区顺序固定为刚刚发现、持续升温、待日榜验证；每个分区内部按 `observedStarDelta DESC`、`totalStars DESC`、`repository ASC`。每项冻结相对增长、正增长区间、最长连续正增长、最新区间增量与发布原因；聚合 suppression summary 冻结弱绝对/相对增长、缺少连续增长、Today exact、冲突和 metadata failure。所有事实均从 source bytes 重算，禁止 AI 排名以及预计、折算或外推 24 小时事实。

`python -m pipeline.audit_trending_discover --data-dir <path>` 只读验证 current、generation path、manifest/hash、Schema、payload digest、全部 source identity/hash/digest、Today exclusion source、numeric ID、阶段唯一性、门禁/发布原因/抑制原因/阶段/排序重算、实际窗口、delta、conflicts、临时文件及 link/reparse/path escape。Audit 只使用 generation-local source copies 完整重算，不修复或覆盖数据。CLI：

```powershell
python -m pipeline.derive_trending_discover --data-dir data derive
python -m pipeline.derive_trending_discover --data-dir data status
python -m pipeline.derive_trending_discover --data-dir data rollback <generation-id>
python -m pipeline.audit_trending_discover --data-dir data
```

Producer 启用时仍只有一个 Managed Scheduler。普通偶数整点严格执行 Observation → Discover；08:00 严格执行 Observation → Refresh → Explosion → Discover，使 Discover 使用最新 Today exact 排除集合。Discover 失败只降级 `producer.discover` telemetry，不回滚或终止 Observation、Refresh、Explosion，也不新增 timer、cron、service 或 daemon。Repository 合并只表示合同和编排可用；Production Discover 必须经过独立 Runtime activation 后才可称为 ACTIVE。

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

Catalog v3 只能配 Queue v2；Catalog v1/v2 继续配 Queue v1。未知组合、新旧字段混用或跨文件身份不一致均 fail closed。identity v1 能区分旧 slug 碰撞对，P1-6C1 也让客户端与 canonical 路由不再以 slug 为身份；但当前 Catalog 的 legacy `slug` 发布门禁在本轮仍保持唯一。若 snapshot 或 legacy artifact 暴露同一旧 slug 对应多个 repository，构建与 audit 都继续按 unresolved collision 拒绝发布，直到独立 P1-6C2 collision-history 工程轮明确迁移和回滚协议。

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
- refresh daily rollover 必须对 candidate 内克隆的 published base `snapshots/latest.json` 使用共享 stable-read，并从同一份被证明稳定的原始 bytes 解析增长语义、创建 history archive；history target 只能以同目录临时文件加原子 create-only/no-replace 首次创建，已存在（即使字节相同）、symlink/junction/reparse、非 regular 或竞态 target 都 fail closed，绝不覆盖；
- publication 在 pointer 切换前继续要求新 history 中恰好有一份与 base snapshot byte-exact 的 archive，并要求旧 history 集合及字节完全连续；语义相同但换行或其他表示不同不能通过该不变量；
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

远程静态分析把“证据获取失败”和“资源生命周期无法证明”分开处理。浅克隆不捕获 stdout/stderr 管道；Windows 在 clone 主线程执行前把 suspended process 纳入设置了 `KILL_ON_JOB_CLOSE`、不允许 breakaway 的独立 Job Object，POSIX 使用独立 session/process group。成功、非零退出、超时或 wait 异常都必须在固定截止时间内确认整树清空，才能读取 checkout、删除 partial checkout 或进入官方 archive fallback；成功根进程留下后代也会作为 lifecycle 异常拒绝。Windows partial clone 的 promisor pack 可能带 `FILE_ATTRIBUTE_READONLY`；整树退出后，清理器只对身份未变的自有根内、单链接、普通且确有只读位的文件清除此位并重试一次，不修改 ACL/所有权，不处理共享占用，也不跟随 symlink、junction 或其他 reparse point。无法确认 Job/process group、checkout 或分析临时目录清理时抛出稳定 lifecycle error，refresh 立即使 candidate 在 build 阶段失败，current 不变；scheduler 将该次运行标记为不可自动重试，并由 Runtime 状态透传错误码。进程尚未创建的 spawn 错误和已确认资源收口后的网络、clone 或 archive 内容失败仍属于普通证据获取失败，最终可记录为单仓 `analysisFailures`，并在 Schema/Audit 无 error 时发布 degraded generation。

官方源码 ZIP 的 100,000 成员上限是元数据准入门槛，不等于静态扫描数量。任何 checkout 写入前必须遍历全部成员，拒绝多根目录、绝对或穿越路径、NUL/反斜杠、重复或大小写碰撞、file-directory 冲突、加密与不支持的成员类型；即使成员最终会因目录、后缀或符号链接被跳过，也不能绕过预检。预检通过后按 NFC 规范化相对路径排序，只选择前 12,000 个合格文件，选中声明和实际内容均受 600 MB 上限约束并完成 CRC 检查；超过单文件文本上限的选中文件只生成空占位，但仍完整读取以验证完整性。提取先写唯一私有 staging，全部成功后才原子切换为 checkout；下载先写 `.part`，实际字节和可信 Content-Length 都受 120 MB 上限约束。

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

canonical JSON 写请求必须同时提供数值 `projectIdVersion: 1` 与 `projectId`；单项 GET 查询使用同名 query pair。P1-6C1 客户端只使用这个 stable pair；只有 slug 的 API 请求仍作为旧客户端兼容边界，双 selector 共存时必须指向同一项目。projectId pair 缺一或 selector 形状错误返回 `invalid_project_selector`，必须指定单项目的操作在两种 selector 都缺失时返回 `missing_project_identity`；Action/feedback collection GET 可以只给 `deviceId` 并返回 current Catalog 中的记录。错误版本返回 `unsupported_project_id_version`，畸形 ID/slug 返回 `invalid_project_id` / `invalid_project_slug`，未知 ID 或 slug 分别返回 `unknown_project_id` / `unknown_project_slug`，歧义 slug 返回 `ambiguous_project_slug`，双 selector 不一致返回 `project_identity_conflict`。Catalog 自身非法，或 stored identity 的版本/格式无法可信解析时服务端 fail closed。结构合法但暂时不在 current Catalog 的历史 projectId 不会被删除，也不会让集合 API 整体失败：当前集合和推荐会省略它；不需要 slug 投影的全局反馈 State 聚合以及近 7 天 Event/decision 周指标继续直接按 canonical projectId 汇总。canonical 写请求明确拒绝客户端 `repository` 和 `occurredAt`。Action、feedback 与 State 项返回 `projectIdVersion`、`projectId` 和临时兼容 `projectSlug`；历史记录以 projectId 和其不可变 generation mapping 证明身份，响应将当前记录的兼容 slug 归一为 current Catalog 值，不把 slug 当成跨 generation 身份不变量。recommendation 响应返回它实际读取的 `generationId`，每个项目项返回 Stable ID，并继续保留既有 `slug` 作为显示兼容字段；客户端只能把与当前页面 generation 精确一致的响应用于排序。

## P1-6C1 Web 与客户端 Stable Identity

服务端网页数据入口从同一次已验证 published bundle 建立 identity context。Catalog v1/v2 项目从 `repo` 机械派生 `projectIdVersion: 1` 与 projectId；Catalog v3 项目从 repository 重新计算并精确核对发布值。页面一次请求只使用这个 bundle 和 identity context，不分别读取两次 current；pointer 原子切换后的下一请求读取新 generation，已经取得的单个响应保持内部一致。

canonical 详情 URL 是 `/project/v1/<projectId>`。版本必须精确为 `v1`，projectId 必须形状合法、能在本次 current Catalog 中唯一解析且与 repository 重算一致；错误版本、畸形/伪造 ID、未知 ID 或已经退出 current Catalog 的历史 ID 都返回 `404`，不回退旧 generation、flat staging 或 slug。页面项目链接、React key、Action/feedback props 与 payload、recommendation 关联以及 watch/local 状态映射都使用 Stable ID；repository 和 slug 只用于展示。

旧 `/projects/<slug>` 是兼容解析入口，不是第二套详情身份。它只在同一 identity context 内解析：唯一匹配返回 `302` 到 canonical URL 并设置 `Cache-Control: no-store`，未知返回 `404`，歧义返回 `409`。不得选择第一项、按 slug 哈希、从 D1 retained mapping 猜测，或缓存可能跨 generation 失效的 redirect。P1-6C1 不修改 `0004`、D1 adoption、API legacy selector 或 unresolved collision 发布门禁；跨 retained history 接受相同 legacy slug 属于 P1-6C2。

### Historical Identity Bundle 与首次 adoption recovery

D1 adoption 的历史身份来源是版本化 `Historical Identity Bundle v1`，而不是 D1 中陈旧的 slug map。构建器在 canonical data lock 内读取一次 current pointer，并严格验证 current 与全部可见 retained final generation。独立 `generations` 清单保存 generation ID、manifest `createdAt`、manifest SHA-256、Catalog Schema 版本与 active 标记，即使某代 Catalog 没有项目也保留 provenance；`mappings` 保存 identity v1、canonical repository、legacy slug，并逐字段绑定所属 generation。active generation 的 `publishedAt` 必须精确等于 pointer；retained generation 因可能被多次 rollback 激活而只能为 `null`，不得用 manifest 创建时间冒充发布时间。`.candidates`、flat staging 和隐藏路径不参与解析，任何可见 final 的 hash、Schema、Audit、路径或身份冲突都会阻止整个 adoption。

`project_identity_generation_evidence` 保存每代不可变的 `generation_created_at + manifest_sha256 + catalog_schema_version`，并与 `project_identity_catalog` 成对校验；role-dependent pointer 时间只保存在 `project_identity_runtime`。retained legacy slug 若在多个已验证 generation 中始终属于同一 projectId，则使用 manifest 创建时间精确到微秒选择最新 witness；若归属不唯一则失败，绝不按文件顺序或字符串第一项猜测。事务内 `project_identity_adoption_session` 和 `project_identity_adoption_allowed_mapping` 只为本次 backfill 临时放行精确 historical mapping，写入仍需匹配原 legacy source，提交前两表必须清空；应用的普通写路径仍只能使用 active generation。

无法由 current 或 retained Catalog 证明 repository 的 legacy 行只能依据 exact、版本化 disposition policy 处置。当前 policy `2026-07-18.1` 仅把 `officecli` 的 `feedback` source 记入 append-only `project_identity_unresolved_legacy`：保留原 feedback/history，不写 repository、projectId 或 device ID，不进入 canonical metrics/recommendations。ledger 的 source table/key、slug、reason 和 policy version 不可 UPDATE/DELETE；未来解析必须使用单独、显式、可审计的 resolution migration，不能修改 policy 后把旧 quarantine 静默解释成新身份。`oomol-lab--open-connector` 有唯一 verified retained repository，因此按该 mapping 机械 backfill，而不是 quarantine。

## Canonical v2 Event、State 与 feedback

`project_action_events_v2` 的应用写入边界只允许 INSERT，数据库触发器拒绝 UPDATE、DELETE 和 identity replacement。新事件的 `occurred_at` 由 Worker 生成带时区的 RFC3339 UTC 时间；API 不接受客户端时间。幂等键在同一 `device_id` 内唯一：相同键与相同 projectId/行动是安全重放，不产生第二个 Event；相同键绑定不同 projectId 或行动返回冲突。一次用户意图的即时网络重试和页面内再次尝试复用同一键，成功后的新一次真实行动生成新键，因此跨周重复行动仍能追加。

Event INSERT 通过 `project_action_events_v2_sync_state` 在同一 SQLite 写入中更新 `project_action_state_v2`。State 以 `device_id + project_id` 唯一，`project_id_version` 固定为 1；`highest_stage` 按 `opened < saved < tried < cloned < reused` 单调推进，各阶段时间只在该阶段真实发生时记录，不因最高阶段倒推缺失步骤。按钮和观察列表由 State 投影，不能扫描 Event 充当当前状态。

Weekly Acted Projects 只查询 canonical v2 Event，在一次服务端 `now` 下使用包含下界的 `[now - 7 days, now]` 窗口，对 `tried`、`cloned`、`reused` 的不同 projectId 计数；`opened` 与 `saved` 仅作为辅助漏斗指标。同一项目在窗口内多次行动只计一次，旧事件离开窗口后的新真实行动可重新计入，State 不参与反推。

`feedback_v2` 保存每个设备和 projectId 的当前反馈，`decision_events_v2` 保存反馈变化历史；新反馈 INSERT 或真实值变化在同一数据库写入中追加 history。推荐、当前反馈计数和辅助反馈指标使用 canonical projectId，响应同时返回 `projectIdVersion`、`projectId` 和用于展示/旧客户端的兼容 slug；P1-6C1 客户端关联只使用 projectId。反馈仍不属于 Weekly Acted Projects，不能冒充试用、浅克隆或确认复用。

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
12. P1-6B 增加 canonical Stable Project ID D1/API 边界，同时完整保留旧 `project_slug` 表与兼容投影用于旧代码回滚。P1-6C1 把页面路由、链接、React key、组件交互和本地关联迁移到 projectId；legacy slug 只能经本次请求的 verified Catalog 严格解析，不能成为 canonical 事实主键。P1-6C2 才能独立演进 collision history 与发布门禁。

### 显式 staging artifact 冲突解析

同一 repository 的 legacy v1 与 stable v2 非等价时，candidate adoption 继续 fail closed；它不会根据 Schema 版本、mtime、目录顺序或单一时间字段选出“赢家”。只有完成来源证据审查后，操作者才能对一个 repository 和一种 artifact kind 调用 `pipeline.resolve_project_artifact_conflict`，并明确选择 `KEEP_STABLE_ARCHIVE_LEGACY`、`PROMOTE_LEGACY_TO_STABLE` 或 `BLOCKED_UNPROVABLE`。CLI 对应值为 `keep-stable`、`promote-legacy` 和 `blocked`。

resolver 的信任边界固定为：

1. 每次调用都在 canonical data lock 内完整解析 current pointer，并验证 ready manifest digest、artifact 清单与全部 SHA-256、Schema 和跨文件 Audit；prepared/resolved 重试还要重新验证审计绑定的 retained generation；
2. 从严格 repository 重算 projectId，以 current ready generation 内的 v2 文件作为 immutable stable reference；调用者提供的 legacy/stable SHA 必须与锁内重算结果精确一致。analysis 的可写结论还必须显式提供两端 `sourcePushedAt`：legacy 值与 flat snapshot 精确交叉核对，stable 值与绑定 generation snapshot 精确交叉核对；snapshot 中匹配 repository 的 URL 必须正确，item `captured_at` 必须与顶层 `captured_at` 原样一致且不晚于分析时间；
3. legacy 必须是 flat `analysis/` 或 `enrichment/` 下归属和文件名均合法的 v1 regular file；flat stable target 若存在，也必须与允许的 reference 或机械转换结果精确等价；
4. `keep-stable` 只接受 stable 的分析时间严格更新且 `sourcePushedAt` 不回退，并由人工证据确认 legacy 没有后续可信事实；`promote-legacy` 只接受 legacy 的分析时间严格更新、`sourcePushedAt` 不回退、可机械转换且不会覆盖非等价 stable target 的结论；来源版本缺失或无法比较时只能显式 `blocked`，报告 `sourceVersions: null`、CLI 退出 2 且保持零写入，但 current、hash、Schema、身份和来源 URL 门禁不放宽；
5. `docs/iterations/*.md#anchor` 必须真实存在于当前 worktree、是 UTF-8 regular file 且不经过链接；证据与两端 snapshot 都使用共享 stable-read：每份值由两次独立 no-follow FD 完整读取取得，只有 bytes、SHA-256 和允许的文件身份全部一致才接受。metadata 只用于路径、类型和对象替换的额外拒绝，不能单独证明同 inode、同长度内容未变。prepared/resolved 审计记录绑定证据 SHA-256、两端 source URL、snapshot captured time、`sourcePushedAt` 与分析时间，因此证据文档或来源绑定改变后不能静默复用旧决策；
6. apply 先在仓库外确定性目录保留 legacy 原始字节，再原子写 prepared 审计记录；PROMOTE 使用原子“只创建、不替换”写入机械 v2。legacy 通过平台原生 no-replace move 进入同目录 quarantine；核对移动后的精确字节后，必须重新验证健康 current、审计绑定的 retained generation、flat stable、证据 SHA 和来源版本。通过后再以 no-replace move 将同一文件对象转入同一文件系统上的外部归档 `detached-legacy.json`，并永久作为 resolved/no-op 后置条件保留；resolver 不对 quarantine 执行 pathname unlink。晚到 quarantine、竞态 target 或未知替换都保留且 fail closed，绝不删除未审查字节；最后才原子推进为 resolved；
7. prepared 审计冻结已验证的 legacy sourceVersions；中断重试不重新绑定随后会前进的 flat snapshot，只重新验证 retained stable 来源、健康 current 与其余审计字段。健康 current 正常切换后可以继续，current 损坏时拒绝继续；完整第二次 apply 为 no-op，PROMOTE no-op 仍要求机械 v2 flat postcondition 存在且等价；
8. current、retained generations、`.candidates`（包括 failed candidates）和 manifest 都是只读边界。resolver 不发布 generation，也不放宽后续 candidate 的 Schema/Audit/adoption 门禁。

默认归档根为用户本地 Rardar state 下的 `artifact-conflict-resolutions/`，必须位于 data 与 Git worktree 外，并与 staging 位于同一文件系统以支持原子 detachment；归档根、祖先、叶子及 staging 路径上的 symlink、junction/reparse point 或路径逃逸都会被拒绝。审计记录使用精确字段集合，只保存 repository、kind、显式 decision、legacy 相对路径、两个归档文件名、reference generation、两个 artifact SHA、source URL/版本/时间、仓库内证据引用及其 SHA、受限 reason code、preparedAt/resolvedAt 和 tool version，不保存 token、请求头或环境变量。分析执行时间只是程序化防回退门槛，不单独充当源码版本权威；KEEP/PROMOTE 的事实依据必须写入并由 `evidenceReference` 指向人工审查记录。

未知版本或未版本化的新数据会失败。以后收紧字段或改变含义时应新增 Schema 版本和显式迁移，不得静默把旧数据解释为新版。

### Runtime Operational Readiness 状态

Runtime status、Scheduler heartbeat 和 `/api/health` freshness 是可重建的本地运维 telemetry，不属于 immutable generation artifact，也不进入 generation manifest/hash 或增长历史。schedule 的权威来自 Manager 启动时验证并冻结的 `RARDAR_SCHEDULE_AT`、`RARDAR_SCHEDULE_TIMEZONE` 和 `RARDAR_STALE_AFTER_HOURS`；缺失时分别为 `08:00`、`Asia/Shanghai` 与 36 小时。Scheduler status 只报告当前 child PID、运行结果与它计算的 `nextRunAt`，不能作为配置输入；同一 canonical data directory 的 Scheduler 单实例锁独立于短期 data write lock，防止 Manager 与 standalone CLI 并行。

freshness 的权威是同一次 verified published bundle 中 `snapshots/latest.json captured_at` 所表示的 UTC instant。Catalog `capturedAt` 必须与它表示同一 instant，但允许等价的 RFC3339 offset 文本；不得使用文件 mtime、pointer publish time、process start 或 heartbeat 代替。Worker 每次请求重新计算 age：`ageSeconds <= staleAfterSeconds` 为 `fresh`，超过为 `stale`；最多五分钟未来偏差按 age 0 处理，超过偏差、日历时间非法或 current/manifest/hash/snapshot 不可信为 `invalid`。

`fresh` 且 Manager/Website/Scheduler 全部 healthy 时 overall 才是 `healthy`。仅 stale 时 health 为 HTTP 200 / overall `degraded`，generation 仍可读且首页展示轻量警告；stale 可与 Scheduler blocked/restarting 等其他 degraded 状态叠加。`invalid` 或 generation 损坏继续沿用 HTTP 503/页面 fail-closed，不回退 flat staging，也不缓存上一代健康内容。Manager 只接受与自己 control PID 一致的 Runtime status、与当前 Scheduler child PID 一致的 heartbeat，以及和 frozen config 一致的 Website health；旧/外来 telemetry 不能冒充健康。

### Always-on Deployment v1 持久化边界

Linux Always-on 部署不改变 generation、D1 或行动事件的数据模型，只把已经存在的 mutable state 从 exact code release 中显式分离。Always-on v1 的 systemd profile 使用以下固定 canonical 路径事实：

| 变量 | 内容 | 数据语义 |
| --- | --- | --- |
| `RARDAR_HOME` | `/opt/rardar/current` | 指向 exact commit release 的原子 leaf symlink；运行中不 `git pull`，不保存业务事实 |
| `RARDAR_DATA_DIR` | `/var/lib/rardar/data` | generation 协议的唯一正式数据根，包含 current、ready/retained generation、flat staging 与 candidates |
| `RARDAR_VINEXT_STATE_DIR` | `/var/lib/rardar/vinext-state` | Action、feedback、identity adoption 等本地 Vinext/Miniflare D1 持久事实 |
| `RARDAR_RUNTIME_DIR` | `/var/lib/rardar/runtime` | Manager control/status、Scheduler telemetry 与可重建进程日志，不进入 generation |
| `RARDAR_DATA_LOCK_DIR` | `/var/lib/rardar/locks` | canonical data directory 的跨进程锁；协调状态，不是事实或备份 |
| `RARDAR_VITE_CACHE_DIR` | `/var/cache/rardar/vite` | 可重建 Vite cache，实际写入其 `node_modules/.vite`；不得作为 D1 或 generation 来源，运行时无需写 release 内 `.vinext`/`.vite-temp` |
| `RARDAR_BACKUP_DIR` | `/var/backups/rardar` | 操作者创建的停机备份，位于其他数据根之外，不参与运行时读取 |

固定工具路径为 `WRANGLER_LOG_PATH=/var/log/rardar/wrangler`、`WRANGLER_REGISTRY_PATH=/var/lib/rardar/runtime/wrangler-registry` 与 `MINIFLARE_REGISTRY_PATH=/var/lib/rardar/runtime/miniflare-registry`。除 `RARDAR_HOME` 外，这些路径必须是预先存在、绝对、互不重叠（明确的 runtime 子目录除外）且不经过 symlink 的目录。`RARDAR_HOME` 的最终 `current` 路径组件可以是原子切换 symlink，但其祖先必须不是 symlink，解析目标必须是运行 checker 的 exact release，且必需 release 文件不得是 symlink。systemd 只启动 foreground Manager；Manager 将同一套已验证路径和 loopback port 冻结后传给 Website 与唯一 Scheduler。独立 Scheduler service、相对 `data/`、release 内 `.wrangler`/D1 或 status 反向控制配置都不属于部署契约。自定义目录需要后续独立审查并同步生成 unit/drop-in 与 checker 映射，不属于 v1。

`pipeline.deployment check --offline` 只读解析 current，并复核 ready manifest digest、全部 artifact hash、Schema 和 Audit。ready manifest 和 artifact 都从同一 stable bytes 完成 hash/parse/validate；manifest 已有的 expected SHA 是内容权威，metadata 不是。D1 source main 及其现有 `-wal`/`-journal` 分别完成两次独立 FD 全量读取和 bytes/SHA 一致性验证，再执行 source before/copy/after 比对并复制到系统临时 scratch；只有明确 concurrent change 才允许固定 3 次以内的重试。SQLite recovery、`quick_check` 和 Rardar 表指纹只在副本运行，正式 source 从不建立 SQLite connection、不创建/更新 `-shm`、不 replay 或 checkpoint。required release regular file 也必须完成双 snapshot，变化时返回 `release_file_unstable`。checker 不会 bootstrap、repair、refresh、迁移、清理 candidate 或创建部署目录。`--online` 先完成同一 offline 检查，再核对 Manager/Website/Scheduler 进程身份、loopback listener、Runtime status、健康端点和检查期间 generation 一致性。systemd 的 `PrivateTmp` 隔离 scratch；stale 可以显式降级，invalid/corrupt 或不稳定 D1 副本继续 fail closed。

发布备份必须在 Managed Runtime 停止后取得同一停机点的 `RARDAR_DATA_DIR` 和完整 `RARDAR_VINEXT_STATE_DIR`。代码回滚保留持久数据；generation 回滚只能显式指向重新验证通过的 retained generation；只有持久数据或数据库迁移失败时，才允许把 data 与 D1 作为同一备份单元恢复。任何路径都不得从 State 补造 Event、从 slug 猜测 Stable ID 或从备份时间补造事实时间。

## 安全与回滚

Schema 与 generation 验证不执行候选仓库代码、不安装其依赖、不读取用户 Git 配置，也不改变静态分析的资源上限。generation ID、manifest 产物路径与符号链接都经过逃逸检查；路径必须留在当前 data/generations 根内。

回滚 Stable ID 应用代码不能忽略 flat staging 中的 v2 artifact。顺序固定为：停止写入任务 → 备份 flat staging 与 D1 → 执行 `--to-legacy-v1` dry-run → 显式 `--to-legacy-v1 --apply` → 验证全部 staging 均为旧代码可读取的 v0/v1 → 在 P1-6B 代码仍运行时显式 rollback 到健康的 Catalog v1/v2 retained generation → 在目标 Runtime 的实际 D1 上发起一次预期会执行 adoption 的受控 GET，并核验 `project_identity_runtime` → 运行 Schema/Audit → 停止 Runtime → 回滚应用代码 → 恢复 Runtime。逆向 staging 迁移不接触 generation；后续 generation rollback 仍沿用下述严格目标验证与原子 pointer 协议。

显式回滚和灾难恢复都使用 `npm run data:generation:rollback -- <generation-id>`。目标验证失败时返回结构化错误，旧 `current.json` 的原始字节保持不变。目标健康后，若当前 generation 仍可严格解析，则继续沿用正常回滚逻辑。

只有 current 无法严格解析时才进入恢复分支。该分支仅对旧 pointer 中的 `generationId` 与 `publishedAt` 分别进行有限、独立验证：安全的 generation ID 可写入新 pointer 的 `previousGenerationId`。旧 `publishedAt` 只有在可合法解析且不晚于恢复时当前 UTC 加五分钟时，才参与新 pointer 的单调时间计算；新 `publishedAt` 必须严格晚于该可信时间。超过五分钟的异常未来值、无法解析的时间，或严格递增计算发生溢出时，旧时间均视为不可信并降级使用当前 UTC。

五分钟信任窗口与 UTC 降级只适用于 current 已损坏的显式 recovery。current 仍可严格解析时继续执行原有正常回滚时间规则，不使用该窗口，也不放宽 stale publication 冲突。旧 pointer 的 manifest digest、audit、`previousGenerationId` 以及 flat 数据均不受信任，恢复 rollback 永不读取或发布 flat 数据。

若 `current.json` 是 symlink 或 junction，恢复过程不会跟随或读取链接目标作为可信 pointer 元数据。目标 generation 通过全部门禁后，仅尝试原子替换 `current.json` 目录项；若平台拒绝安全替换，则返回结构化 `pointer_write_failed`，链接目标保持不变。恢复无需反向改写既有事实，也不得从缺失时间补造历史。

本地管理器在启动任何子服务前检查声明的 Python 运行依赖。缺失时只输出 `python -m pip install -r requirements.txt` 并退出，不自动安装，也不启动会持续失败重启的 scheduler。

Schema 不能单独解决跨文件一致性、历史增长、缓存新鲜度或身份碰撞。前三项由 generation audit、manifest/hash、请求级 generation 边界与上述 Runtime freshness telemetry 处理；Stable Project ID 由 identity v1 重算、跨产物审计和 collision/unresolved 门禁共同保证。追加式行动事件已由 PR #5 完成，评分语义已由 PR #6、提交 `ab34119` 完成，verify/CI 已由 PR #7、提交 `3430e30` 完成，P1-6A JSON 身份层已由 PR #8、提交 `d41033f` 完成。P1-6B 已由 PR #9、提交 `c24b7d6` 完成，正式 Primary Runtime D1 adoption、完整重启和重复 adoption no-op 均已通过。P1-6C1 client/UI Stable Identity 已由 PR #13、提交 `dfed8f0` 完成；P1-6 整体须在当前 deferred 的 P1-6C2 collision-history 边界完成后才能关闭。Runtime Operational Readiness 已由 PR #14、提交 `e61e3ff` 完成；当前独立工程轮是用户明确授权的 Always-on Deployment v1，只增加部署工程和运维验证，不改变上述数据语义或长期 P1-6 状态。
