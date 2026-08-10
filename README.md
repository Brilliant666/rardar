# Rardar

Rardar 是一个证据优先的开源情报与项目复用雷达。它将技术事件、GitHub 仓库、能力标签、静态代码证据和用户反馈组织在一起，帮助开发者回答两个问题：

1. 最近真正发生了什么？
2. 我想实现的功能是否已经有项目做过？

## 当前版本

- 今日重点与候选池
- 近期动量与长期高热双赛道评分，每日重点默认平衡为 3 + 2
- 过去 48 小时 AI/技术动态中文简报与信源健康状态
- 关注优先级、持久热度、静态工程就绪度、任务复用匹配和证据完整度分层评分
- 自然语言任务拆解、中文能力画像和可解释匹配
- 项目证据页与风险提示
- `有用 / 无用 / 复用 / 待确定` 持久化反馈与偏好重排
- 打开、收藏、试用、浅克隆和确认复用的真实行动记录
- 公共项目只读浅克隆与静态分析工具
- 每日生成的本地 Codex 中文深读队列

当前网页只读取 `data/current.json` 指向的不可变 generation。Vinext Cloudflare Worker 不直接读取宿主文件；默认 `vinext dev` 通过仅接受回环请求和随机 token 的 Vite host 数据桥，在每次网页或 API 请求中让 Node host 完整解析一次 current、manifest 和全部 artifact 哈希，再把同一 generation 的一次性 bundle 交给 Worker。桥地址由本地 Vinext 配置固定，不信任或转发外部请求的 `Host`，避免回环 SSRF 与 token 泄露。每个 generation 同时保存真实 GitHub API 快照、目录、技术动态、中文画像和 Codex 队列，因此单个响应不会把不同代的数据混在一起；下一请求会立即观察到原子 pointer 切换，损坏 current 时直接失败而不回退 flat。首次采集只展示明确标注的“创建以来速度代理”；第二次刷新起会自动归档旧快照并计算真实观测区间增长。刷新流程还会对前五名执行隔离用户 Git 配置的只读浅克隆，静态检查代码、测试、文档与许可证，不执行仓库代码。浅克隆不继承输出管道；Windows 先以 suspended 状态创建进程、纳入禁止 breakaway 的 Job Object 后再恢复，POSIX 使用独立 session/process group。无论 clone 成功、失败还是超时，分析器都在返回前有界确认本次完整进程树已经退出；只有清理已确认时才允许读取 checkout 或改用 GitHub 官方源码归档。Windows Git 若把自有 partial-clone pack 标记为只读，分析器只会在整树退出后，对身份绑定、单链接、普通且确有 `READONLY` 位的自有文件清除该位并重试一次；leaf symlink 只删除链接本身且绝不跟随，ACL、共享占用、硬链接、junction/其他 reparse 或身份变化仍会 fail closed。归档先对最多 100,000 个成员做全量路径和类型预检，再按规范化路径确定性选择最多 12,000 个文件；下载上限为 120 MB，选中内容的解压上限为 600 MB，符号链接和不需要的二进制文件不会物化。技术动态来自官方 RSS 与可归因的社区补充源，先去重、标注信源健康，再由本地 Codex 为前五条生成中文要点。

近期动量与长期高热分开计算。长期高热在历史样本不足时只使用总 Star、仓库年龄、近期维护和 Fork 生态形成明确标注的结构代理，不把一次快照冒充为“长期持续霸榜”。持续性判断使用最近最多 30 次候选快照；同一仓库在至少 7 次快照中出现且覆盖率达到 70% 后，会自动升级为多周期持续热度验证。

Catalog v2 使用 `evidence-v2` 评分模型：Attention 只回答是否值得先看，Endurance 只回答是否有长期热度线索，Engineering Readiness 只使用与当前推送匹配的只读静态证据。通用目录没有你的具体任务、约束和验收标准，因此 Reuse Fit 保持未知；Evidence Completeness 只描述证据覆盖，不代表质量。每项都公开事实、代理、限制和升级条件。默认流水线从不运行第三方代码，所以最强建议只能是满足许可证与风险门槛后的“隔离试用”，不会把静态文件完整度写成“直接复用”。

新生成的 Catalog v3 在保持 `evidence-v2` 评分语义不变的同时采用 `projectIdVersion: 1`。Stable Project ID 以严格合法的 GitHub `owner/repo` 为输入，按 ASCII 小写规范化，使用最多 64 个字符的可读前缀和规范化 repository 的 SHA-256 前 20 个十六进制字符（80 bit）组成 `<prefix>--<digest>`。因此 `owner/foo.bar` 与 `owner/foo-bar` 得到不同身份；大小写变体是同一身份，owner 转移或仓库改名在 v1 中则是新身份。任何真实 ID 碰撞、规范化 repository 重复或跨产物身份不一致都会阻止候选 generation 发布。P1-6C1 把路由与 UI 消费者迁移到 projectId，但不放宽现有 unresolved legacy collision 发布门禁；让新 generation 接受相同 legacy slug 及其 retained history 属于后续 P1-6C2 独立工程轮。

本地 Codex 生成的中文能力画像先发布到 flat staging 区 `data/enrichment/`。运行 `data:derive` 后，当前有效画像才会随完整候选 generation 一起验证并发布。新项目画像 v2 同时绑定 `repository`、重算一致的 `projectId`、仓库推送时间和静态证据分析时间，并使用 `projectId.json` 作为文件名；任一身份或来源版本变化后都不会继续冒充当前结论。画像只覆盖已核对 README 和静态证据的项目，并与 GitHub 事实分层保存。

每日刷新还会在候选 generation 中生成 `queues/codex.json`：Queue v2 的项目任务使用 `projectId` 作为任务身份，并让 `inputPaths`、`outputPath`、payload repository 和 projectId 互相绑定；旧 slug 不再决定新画像归属。它只收录重点范围内尚未完成中文画像的项目与动态，并把输入证据绑定到该 generation 的不可变路径，便于本地 Codex 按优先级继续阅读。Codex 写入 flat staging 画像后运行 `npm run data:derive`，只从当前已发布快照构建、校验并原子发布新 generation，不访问 GitHub、不推进增长基线，也不会把真实区间增长退回首次代理。

首页推荐默认以关注优先级为主；存在当前静态证据时，再有限纳入工程就绪度。浏览器产生反馈后，`/api/recommendations` 会生成匿名设备偏好画像：标记“无用”的项目和相近特征会降低曝光，“有用 / 复用”会提高相似未处理项目的机会，已处理项目本身会减少重复推荐；个性化只做有限调整，不覆盖事实、风险与证据边界。这里的“复用”是用户已经发生的反馈事实，不是系统预测的 Reuse Fit。

真实行动使用 D1 中分离的 canonical v2 Event 与 State：所有新写入使用 `projectIdVersion: 1` 和 `projectId`，Event 只追加“发生过什么”，State 保存当前最高阶段和每个真实点击阶段的最近时间。客户端为一次行动意图生成幂等键并在网络重试中复用；同键、同项目和同行动是安全重放，同键绑定不同 projectId 或行动会冲突，成功后的下一次真实行动使用新键并继续追加。按钮与观察列表只读取 State，近 7 天指标只读取 Event，并在同一服务端时间窗口内按不同 projectId 去重。

Action、feedback、recommendation 与 metrics API 从一次请求加载的同一个 verified Catalog 建立 generation-bound 身份映射。Catalog v3 会核对携带的 projectId 与 repository，retained Catalog v1/v2 则从 `repo` 机械计算 identity v1；current pointer 的 `publishedAt` 同时作为 D1 active-generation 的单调激活顺序，旧慢请求不能回退 legacy capture 边界，而带有更新发布时间的显式 rollback 仍可重新激活 retained generation。全部 retained mappings 还必须保持 projectId ↔ canonical repository 一对一，且同一 legacy slug 不能跨代改绑另一个 projectId；预检、正式触发器和事务内 guard 任一发现碰撞都返回 `project_identity_collision`。P1-6C1 客户端对 Action/feedback 只提交 stable identity pair，recommendation、watch 状态和 React key 也只按 projectId 关联；API 的 legacy slug selector 仍作为旧客户端兼容边界，并且只有在该 Catalog 中唯一匹配时才转换。凡返回当前项目记录的 canonical 响应都返回 projectId，并把已验证历史记录的兼容 slug 投影为当前 Catalog 值；collection GET 可省略 selector 返回该设备在 current Catalog 中的记录。合法的历史 projectId 即使暂时退出 Catalog 仍保留在 D1 与追加式历史中，但不会出现在当前集合或推荐中；全局反馈 State 聚合和近 7 天 Event/decision 周指标仍直接按 projectId 计入。畸形或版本错误的 stored identity 继续让请求 fail closed，不能被静默过滤。metrics 继续返回聚合值并按 projectId 计算；反馈当前状态和 decision history 也使用相同 canonical 项目身份，推荐不再以可碰撞的 slug 关联偏好。

项目详情 canonical URL 为 `/project/v1/<projectId>`。服务端从该请求已经取得的 verified bundle 构造 identity context：Catalog v1/v2 从 `repo` 机械派生 ID，Catalog v3 从 repository 重算并核对发布值；错误版本、畸形、伪造、未知或已退出 current Catalog 的 ID 都 fail closed。旧 `/projects/<slug>` 不再渲染详情：在同一 Catalog 中唯一匹配时返回 `302` 和 `Cache-Control: no-store` 到 canonical URL，未知返回 `404`，歧义返回 `409`，绝不选择第一项或使用陈旧映射。一次页面请求只消费一个 generation，pointer 原子切换后的下一请求立即读取新代。

迁移保持 additive 和 rollback-safe：正式 `drizzle/0004_stable_project_identity.sql` 定义版本化 DDL、反馈历史链和完整触发器边界，runtime bootstrap 直接拆分并重放该文件，不维护第二份 stable DDL；既有 `project_actions`、`project_action_events`、`project_action_state`、feedback、decision history 和兼容触发器均保留，不执行破坏性 down migration。legacy 行只通过明确 Catalog 映射机械迁移，原始发生时间、行动阶段和反馈事实保持不变，不从 State 补造 Event；除 exact disposition policy 明确隔离且保留原事实的行外，无匹配、多匹配、非法行动或非法时间都会明确阻止完成。新 canonical 写入投影到旧 slug 边界，旧代码期间成功写入的事实会在再次升级后被捕获，双向投影使用稳定身份和时间去重，不能制造第二个 Event。若同一 projectId 在新 generation 只改变兼容 slug，adoption 会在同一个原子 batch 内把 mutable `project_action_state`、`feedback` 及对应 canonical State 重键到当前 slug/generation；append-only Action Event、`project_actions` 和 decision history 保留原始 slug 与时间。目标 slug 已被其他 State/feedback 占用时整批 fail closed，重复 adoption 为 no-op，因此旧代码无需等待下一次写入即可读到回滚前的按钮和反馈状态。

旧代码仍按 legacy slug 计算它自己的周指标。若一个 Stable Project ID 的历史 Event 跨多个兼容 slug 保留，且回滚后又在当前别名产生行动，旧版指标可能把这些别名分别计数；canonical v2 Weekly Acted Projects 始终按 projectId 去重，不受此限制。迁移不会为修饰旧指标而改写 append-only Event 或补造历史。

### Historical Identity Bundle 与 unresolved legacy 隔离

Stable D1 首次 adoption 不再只看 current Catalog。Vite host 会在同一个 data lock 中严格验证 `current.json` 和全部可见 retained final generation，逐代复核 ready manifest、manifest digest、全部 artifact hash、JSON Schema 与跨文件 Audit，再生成一次性的 Historical Identity Bundle。Bundle 用独立 `generations` 清单保存每一代 provenance，因此合法的空 Catalog 也不会丢失 generation 证据；`mappings` 只保存项目关系并必须与所属 generation 精确一致。Catalog v1/v2 只从已验证的 `repo` 机械计算 identity v1；Catalog v3 必须按 repository 重算并与携带的 Stable ID 完全一致。flat staging、`.candidates` 和隐藏目录不属于历史证据；任一可见 final 损坏、碰撞或把同一 legacy slug 改绑给另一项目时，整个 Bundle 与 adoption 都 fail closed。

`publishedAt` 是 pointer 的激活事实，不是 generation 的不可变创建事实。Bundle 只给 active occurrence 写入 current pointer 的真实 `publishedAt`，retained occurrence 明确为 `null`；D1 的 immutable generation evidence 只持久化 manifest `createdAt`、manifest SHA-256 和 Catalog Schema 版本。这样 generation 从 active 变为 retained、再由带有更新 pointer 时间的 rollback 重新激活时，不会伪造历史或毒化不可变证据。

首次 backfill 可以在事务内使用唯一、已验证的 retained mapping，但仅限原 legacy 行的机械投影。临时 adoption session 和精确 allowlist 在同一个 D1 batch 内建立并清理；inactive generation 的插入还必须逐字段匹配真实 legacy source。普通 Action/feedback 写入继续只能使用 current generation，不能借 recovery 通道向 retired 项目写新事实。

正式 disposition policy `2026-07-18.1` 只处理已经人工确认的 `officecli` legacy feedback：没有 current 或 retained repository 证据，因此不生成 repository/projectId，不迁移到 canonical 表，也不进入 Stable metrics 或 recommendations。原 feedback/history 保持原样；`project_identity_unresolved_legacy` 只记录 source table/key、exact slug、reason、policy version、首次见到的 generation 与审计时间，不保存 device ID。ledger 拒绝 UPDATE/DELETE；以后只有新的显式 resolution migration 才能处理该事实。`oomol-lab--open-connector` 则只通过 retained generation 中唯一验证的 `oomol-lab/open-connector` mapping 迁移。policy 内容有任何变化都必须提升 `policyVersion`，不能复用既有版本表达不同处置。

### PR #9 正式 Primary Runtime adoption（2026-08-09）

PR #9 已以 Squash merge 提交 `c24b7d6` 合并到 `main`，对应 `main` Verify 通过。停止 Runtime 并完成 data/D1 备份后，正式 D1 adoption 在 current generation `20260809T091719453761Z-69c6385c7279` 上通过：Historical Identity Bundle 验证 6 个 ready generation、180 条 generation mapping、30 个 current 项目和 60 个历史 distinct projectId（其中 30 个仅存在于 retained history）。`oomol-lab/open-connector` 的唯一 retained witness 迁移通过；无 repository 证据的 `officecli` 仅保留原事实并生成 1 条不含 device ID 的 exact quarantine ledger，没有获得 Stable ID。

完整 Runtime 重启后再次执行同一组只读 GET，canonical/legacy 表计数和逻辑摘要保持不变，证明重复 adoption 为 no-op；Manager、Website、Scheduler、`/api/health` 与 3000 端口均保持 healthy。current pointer、正式 generation 数据和 21 个历史 failed candidate 未被修改或清理。

## 开发

需要 Node.js 22.13 或更高版本、Python 3.10 或更高版本，以及 Git。

本地 Verify 必须使用当前 worktree 自己的 Python 虚拟环境。以下 Windows PowerShell 命令只使用系统 Python 启动器创建隔离环境，依赖全部安装到 `.venv`，不会安装到 Windows 全局 Python，也不会使用或修改 Primary Runtime 的虚拟环境：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.lock
.\.venv\Scripts\python.exe -m pip check
npm ci
$env:RARDAR_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
```

`RARDAR_PYTHON` 是当前 shell 的临时变量，必须指向该 worktree 虚拟环境的绝对解释器路径。Verify 会让 Python tests、Schema、Audit 和 Node HTTP fixture 统一使用它。完成验证后可运行 `Remove-Item Env:RARDAR_PYTHON -ErrorAction SilentlyContinue` 清理变量；不要删除仍供该 worktree 使用的 `.venv`。

其他按需入口包括：

```bash
npm run data:generation:status
npm run data:validate
npm run data:refresh
npm run data:audit
npm run dev
npm run build
npm run security:audit:prod
```

完整验证只有一个推荐入口：

```bash
npm run verify
```

`verify` 依次运行 lint、Python 单元测试、Schema 校验、跨文件数据审计、production build、Node 行为与真实 Vinext HTTP 测试，以及 production dependency security audit。每个阶段都有独立日志，任一阶段、数据保护或清理失败都会返回非零退出码。`requirements.txt` 保留运行时依赖范围，`requirements.lock` 固定 Verify/CI 的完整 Python 直接与传递依赖。

失败阶段可按以下命令定位，修复后仍需重新运行完整 `npm run verify`：

| 阶段 | 诊断命令 |
| --- | --- |
| Lint | `npm run lint` |
| Python tests | `& $env:RARDAR_PYTHON -m unittest discover -s pipeline -p "test_*.py"` |
| Schema validation | `& $env:RARDAR_PYTHON -m pipeline.schema_validation --data-dir data` |
| Data audit | `& $env:RARDAR_PYTHON -m pipeline.audit_data --data-dir data` |
| Production build | `npm run build` |
| Node tests | `npm run test:node`（先确保 build 已成功） |
| Production dependency security audit | `npm run security:audit:prod` |

若 build 报告缺少 Rolldown 等可选原生 binding，请确认 Node.js 版本后重新执行 `npm ci`，不要改写 lockfile。Windows 无符号链接权限时会明确跳过对应 4 项真实链接测试；Ubuntu CI 会实际执行它们。

Verify 会在运行前后核对整个 `data/` 树和 Git 可见状态，并把 Runtime、Wrangler、Miniflare 与临时状态重定向到一次性目录后清理。它不会启动 manager、scheduler 或 3000 端口，不会执行 `data:refresh`、`data:derive`、generation publish/rollback，不访问 Primary Runtime，也不要求 `GITHUB_TOKEN` 或项目秘密。Node HTTP 测试只在随机回环端口使用临时 data 与 D1 状态；生产依赖审计仅访问 npm 官方漏洞库，不采集 GitHub 或其他实时业务数据。

GitHub Actions 在指向 `main` 的 Pull Request 和 push 到 `main` 时，于 Ubuntu 上使用 Node.js 22.13.1 和最低支持版本 Python 3.10。workflow 显式创建一次性 `.venv-ci`，将 `requirements.lock` 安装到该虚拟环境并执行 `pip check`，再把绝对路径 `${{ github.workspace }}/.venv-ci/bin/python` 作为 `RARDAR_PYTHON` 交给同一个 `npm run verify`。CI 不依赖 runner 的系统级 Python 包，也不使用或修改 Primary Runtime 虚拟环境。

`data:audit` 只读核对快照、目录、动态、历史和 Codex 队列的时间、数量、唯一性与 URL 安全边界；对 Catalog v2 还会从同一 generation 的事实与证据重算分数、说明、推荐和顺序。`security:audit:prod` 使用 npm 官方漏洞库检查会进入运行环境的依赖。本地构建工具仍应结合完整 `npm audit` 与实际暴露面单独复核。

## 数据契约

`contracts/` 为 GitHub 快照、技术动态、静态证据、项目画像、动态画像、目录、Codex 队列、generation manifest 和 current 指针保存版本化 JSON Schema，并通过 `project-identity-v1.vectors.json` 固定 Python 与 Node 共用的身份样例。`pipeline.schema_validation` 是统一验证入口：它拒绝未知版本、错误字段类型、非字符串数组成员、无时区时间、非 HTTP(S) URL、非法仓库名、危险产物路径、重复 JSON 键、非有限数值、超长文本，以及新版本产物中无法由 repository 重算得到的 projectId。

```bash
npm run data:validate
npm run data:audit
```

`data:validate` 先严格解析 current 指针、manifest 和产物哈希，再检查该 generation 的单文件结构与身份；`data:audit` 对同一 generation 继续检查跨文件时间、数量、增长、历史和队列一致性。每日刷新和 `data:derive` 都先在私有候选目录完成生成、Schema 校验与审计，只有全部通过后才原子切换 `current.json`。Schema 失败、审计失败、写入中断或并发冲突都不会改变上一代健康数据与增长基线。Codex 画像必须先写到正式 `data/` 路径之外的草稿，再通过受锁保护的 ingest 入口进入 flat staging：

```bash
python -m pipeline.ingest_enrichment --kind project --input tmp/project-draft.json
python -m pipeline.ingest_enrichment --kind signal --input tmp/signal-draft.json
```

入口会从项目画像中的 `repository` 计算唯一目标文件名；草稿解析真实路径后必须位于整个 `data/` 目录之外，不能用 `..` 或符号链接绕过，也不能用一种产物覆盖另一种正式路径。

具有可信 v1 身份来源的旧 flat staging 文件可通过显式迁移工具转换为 `projectId.json`；无法机械升级的 legacy v0 保留并明确报告。应用代码回滚前，同一工具的 `--to-legacy-v1` 模式可把可机械降级的 v2 staging 恢复为旧代码能读取的 v1 文件。两个方向默认都只做完整预检和 dry-run，只有额外显式 `--apply` 才会写入；它们只处理 flat `data/analysis` 与 `data/enrichment`，不会读取或修改 `data/current.json`、retained generations、candidates、manifest，也不会发布 generation：

```bash
python -m pipeline.migrate_project_identity --data-dir data
python -m pipeline.migrate_project_identity --data-dir data --apply
python -m pipeline.migrate_project_identity --data-dir data --to-legacy-v1
python -m pipeline.migrate_project_identity --data-dir data --to-legacy-v1 --apply
```

正向迁移和逆向降级都先对全部 analysis/enrichment 文件完成路径、Schema、身份、目标内容和碰撞 preflight，再开始任何写入或删除。逆向模式只执行 `schemaVersion: 2 → 1`、移除 `projectIdVersion` 与 `projectId`、改回 legacy slug 文件名，其他事实、时间和内容原样保留；多个 projectId 降级到同一 slug、已有非等价目标、符号链接、junction 或路径逃逸都会使整批操作 fail closed。apply 先原子写入并验证全部目标，再删除源；等价目标不重写，中断后可安全重试，完整执行后的第二次 apply 为 no-op。

refresh/derive 的 candidate adoption 也遵循同一无损原则：同一 repository 的 legacy v1 与 stable v2 共存时，先在内存中把 v1 机械转换为预期 v2 payload；只有结果与现有 v2 完全相等，才保留 v2 并清理 v1。任何字段不同都返回 `conflicting_project_artifact_versions`；所有项目完成全量 preflight 前不写入或删除，因此一个项目冲突会使整批 adoption 保持零写入、零删除，不能按 Schema 版本、时间、文件顺序或排名猜测权威版本。

非等价冲突只能在人工完成来源证据审查后，用单仓库显式 resolver 处理。命令必须给出 repository、artifact kind、三种允许结论之一，以及当前 legacy 和已发布 stable reference 的精确 SHA-256；默认只输出 dry-run 报告，只有额外指定 `--apply` 才会改变 flat staging。`keep-stable` 将 legacy 原始字节与两阶段审计记录保存到仓库外的本地归档后，再从 active staging 移除；`promote-legacy` 仅在 legacy 来源时间严格更新且机械 v2 目标可验证时原子写入 stable staging；`blocked` 永远零写入。工具每次只允许处理一个 repository 和一种 artifact，不提供通配符、批量解析或 `newest-wins`：

```bash
python -m pipeline.resolve_project_artifact_conflict \
  --data-dir data \
  --repository owner/repository \
  --kind analysis \
  --decision keep-stable \
  --expected-legacy-sha256 <legacy-sha256> \
  --expected-stable-sha256 <current-ready-reference-sha256> \
  --legacy-source-pushed-at <legacy-snapshot-pushed-at> \
  --stable-source-pushed-at <stable-generation-pushed-at> \
  --evidence-reference docs/iterations/<review>.md#<artifact>

# 完整核对 dry-run 后，才可显式写入 flat staging / 仓库外审计归档
python -m pipeline.resolve_project_artifact_conflict <相同参数> --apply
```

resolver 在 canonical data lock 内每次重新验证 current pointer、ready manifest、全部 generation artifact hash、Schema、跨文件 Audit、repository/projectId/文件名、两个 expected SHA 和来源版本；analysis 还要求把 legacy flat snapshot 与 stable generation snapshot 中的 `pushed_at` 原样传入，并核对 repository URL、repository item 与顶层 snapshot 的同源 `captured_at`。首次决策必须绑定一个真实存在、不含凭据的 `docs/iterations/*.md#anchor`；证据和 snapshot 都通过 no-follow、文件身份绑定读取，审计同时保存文档 SHA-256，后续改写会使重试 fail closed。显式 `blocked` 在来源版本无法证明时返回 `sourceVersions: null` 和退出码 2，但仍严格验证 current、两个 hash、Schema 与项目身份，并保持零写入。

它不会修改 `current.json`、retained generation、candidate、failed candidate 或 manifest。归档默认位于 `%LOCALAPPDATA%/Rardar/artifact-conflict-resolutions/`（非 Windows 使用用户本地 state 目录），保存 legacy 原始字节、严格字段审计记录，以及从 staging 原子移出的 `detached-legacy.json`。prepared/resolved 两阶段记录与同目录原子 quarantine 使归档、stable 创建、legacy 移出或审计推进中断后可以安全重试；quarantine 使用 OS 级 no-replace move，已审查字节隔离后会再次核对当前与 retained generation、flat target、证据 SHA 和来源版本，随后再以 no-replace move 转入同一文件系统上的外部审计目录。resolver 不对这个 retained detached artifact 执行路径 unlink；它是 resolved/no-op 的必备后置条件，因此未知或竞态换入的字节只会被保留或导致安全失败。prepared 记录冻结已审查的 legacy 来源版本，重试只重新验证 retained stable 来源，因此 flat snapshot 正常前进和健康 current 切换不会破坏幂等恢复；损坏 current 则拒绝继续。路径逃逸、跨文件系统归档、symlink、junction、reparse point、非等价或竞态出现的 flat stable target、无法证明的可写决策都会 fail closed。

generation 管理命令：

```bash
npm run data:generation:status
# 仅用于没有 current.json 的旧 flat 数据迁移
npm run data:generation:bootstrap
# 重试一个 ready candidate 或指针中断后保留的 orphan
npm run data:generation:publish -- <generation-id>
# 显式回滚/灾难恢复到仍保留且重新验证通过的 generation
npm run data:generation:rollback -- <generation-id>
```

候选目录位于 `data/generations/.candidates/`，构建、Schema 或审计失败会留下 failed manifest，但不会进入 Git；已经 ready 的候选在发布冲突时保持不可变，指针中断后的 orphan generation 也会保留，稳定错误码和 candidate ID 记录在命令输出与 scheduler 状态中。首次迁移机械复制既有事实和画像，只重建 Codex 队列的证据路径并生成 manifest/current，不补造采集或分析时间。`current.json` 一旦存在，普通页面、调度、`data:validate`、`data:audit` 和正常 publish 遇到损坏指针、缺失目录或哈希不一致都会直接失败，不会静默退回 flat 数据。唯一例外是用户明确指定目标 generation 的 rollback：它先在数据锁内完整验证 retained target，再允许原子替换损坏的 current；恢复过程仍不读取 flat 数据。

兼容规则不会伪造历史事实：GitHub snapshot v1 保留既有 `schema_version` 字段和早期 history 形状；两份因对应静态证据缺少可信 `analyzed_at` 而无法绑定的画像，以及一份早于当前静态证据的历史画像，显式保留为 `schemaVersion: 0`，永远不视为当前证据；signal enrichment v1 继续允许旧条目使用顶层 `generatedAt` 作为分析时间回退。Catalog v1/v2、静态证据 v0/v1、项目画像 v0/v1 和 Queue v1 generation 保持原字节与原 Schema，仍可严格验证、审计和显式 rollback；只有新生成的 Catalog v3、静态证据 v2、项目画像 v2 与 Queue v2 采用 Stable ID。网页对旧评分继续保守归一化，未知版本 fail closed。旧 flat 树只在 `current.json` 尚不存在时用于一次迁移或作为受控 staging，网页和增长基线不会绕过 current 指针。详细模型见 `docs/DATA_MODEL.md`。

只回滚 P1-6B 应用代码到已支持 Catalog v3 的 PR #8 时，不做破坏性 D1 down migration：先停止写入并备份 D1，确认当前 generation 的 legacy State/feedback 投影已完成，再让旧代码接管。若要完整回滚 Stable ID 到 pre-v3 数据契约，则还必须让 flat staging 回到旧代码可读状态，并在 P1-6B 代码仍运行时先激活目标 D1 mapping：停止写入任务 → 备份 flat staging 与 D1 → 执行 `--to-legacy-v1` dry-run → 显式 `--to-legacy-v1 --apply` → 验证 staging 为 v0/v1 → 显式 rollback 到健康的 Catalog v1/v2 retained generation → 在目标 Runtime 的实际 D1 上发起一次预期会执行 adoption 的受控 GET，并确认 `project_identity_runtime` 已采用目标 generation → 运行 Schema/Audit → 停止 Runtime → 回滚应用代码 → 恢复 Runtime。存在 v2 staging 时不能只恢复代码；也不能在 D1 active mapping 仍指向另一代时让旧代码接管。

Windows 上可以直接双击项目根目录的 `打开 Rardar.cmd`。它会启动一个隐藏的本地管理器，同时看护网站和每日刷新任务，并打开本地首页。管理器会在任一子服务异常退出后自动重启它；调度器即使进程仍存在，只要心跳持续过期，也会在启动宽限期后被自动恢复。运行心跳、PID 和日志保存在 Windows 本地应用数据目录，不会因频繁写入而触发网站热更新；每份日志超过 5 MB 后滚动，并保留最近两份历史。

也可以使用命令管理：

```bash
npm run local:start
npm run local:status
npm run local:stop
```

`local:start` 会在创建后台管理器前检查必要 Python 依赖；缺失时直接停止并提示运行 `python -m pip install -r requirements.txt`，不会自动安装或让 scheduler 进入反复重启。Managed Runtime 的显式配置为：

```text
RARDAR_SCHEDULE_AT=08:00
RARDAR_SCHEDULE_TIMEZONE=Asia/Shanghai
RARDAR_STALE_AFTER_HOURS=36
```

环境缺失时使用以上默认值。时间必须是 canonical `HH:MM`，timezone 必须是可加载的 IANA 名称，陈旧阈值必须是 1～8760 的整数小时；非法值在启动子进程或写 Runtime 状态前失败。Manager 启动时冻结一次 effective config，并把 schedule 显式传给唯一 Scheduler；运行中修改环境不会热更新，必须先 `npm run local:stop`，再带新环境运行 `npm run local:start`。`nextRunAt` 仍由 Scheduler 计算，直接编辑 status JSON 不能改变计划。

本地网站通过 `/api/health` 实际加载并验证当前 published generation、manifest、artifact hash 与该 generation 的 snapshot。数据年龄只按 snapshot `captured_at` 计算，不使用文件 mtime、current.json mtime、进程时间或 heartbeat。小于等于阈值为 `fresh`；超过阈值为 `stale`；非法时间、明显未来时间或 published generation 损坏为 `invalid`。

`fresh` 时 health 返回 HTTP 200 / `healthy`。仅数据 stale 时仍返回 HTTP 200，但 overall 为 `degraded`、reason 为 `published_data_stale`，页面继续读取旧的完整 generation；首页和 `local:status` 会明确显示快照时间、年龄、阈值与 `STALE`。结构损坏继续返回 503 并 fail closed，不会被伪装成 stale。HTTP 失败会记录简短诊断，但不会仅因数据 stale 或一个仍存活的 Vinext 数据错误而反复重启网站；数据经 rollback 恢复后，同一进程会在下一次健康探测中重新分类。

仅在 Managed Runtime 已停止时，才可单独调试每日刷新守护进程：

```bash
npm run data:schedule
```

该命令使用相同 env/default 配置；显式 CLI `--at` / `--timezone` 仍可覆盖 env。对同一个 canonical data directory，Scheduler 在任何 status 写入或 refresh 前持有独立单实例锁，因此 Managed Scheduler 存活时第二个命令会明确失败，不会并行采集或竞争发布。默认仍在 `Asia/Shanghai` 每天 08:00 刷新 GitHub 快照、前五静态检查和技术动态。

刷新期间调度器会持续写入绑定自身 PID 的运行心跳；Manager 只信任当前 child PID 的 telemetry，status 中的 schedule 不能反向改写 frozen config。既有 12 小时重启补跑和每 5 分钟、单轮最多 3 次的临时故障 retry 语义保持不变，本轮没有新增 missed-run 自动追赶。若 Git 进程树或临时 checkout 的清理无法确认，本轮 candidate 会 fail closed，scheduler 写入 `retryable: false` 和 `remoteAnalysisErrorCode`，不在同一周期自动重试；普通且已安全收口的单仓分析失败仍可记录为 degraded。守护进程不会部署网站，也不会执行候选仓库代码。

“动态”页面从本地管理器的实时心跳读取运行状态；状态必须绑定 control/manager PID，Scheduler telemetry 必须绑定当前 child PID，旧状态超过 35 秒、未来超过五分钟偏差或字段非法都会 fail closed，不再把过期或外来 `scheduled` 文件误报为正在运行。`local:status` 输出 effective schedule/timezone、Scheduler 计算的 next run、last successful refresh、current generation、snapshot time/age/threshold 和 freshness；overall 只有在服务与数据都 healthy/fresh 时才是 `healthy`。

默认本地预览地址：<http://127.0.0.1:3000/>。项目默认不发布线上版本，除非用户明确提出部署要求。always-on deployment 仍是独立后续阶段，不属于本轮 Runtime readiness，也不会由 stale watchdog 自动触发。

静态分析工具只读取文件，不执行仓库代码或安装陌生依赖：

```bash
python -m pipeline.analyze_repository --path .
python -m pipeline.analyze_repository --repo owner/name
python -m pipeline.collect_github --out data/snapshots/latest.json
python -m pipeline.collect_signals --out data/signals/latest.json
npm run data:derive
```

独立 collector 命令用于调试或生成 flat staging，不会直接切换网页数据源；正式发布入口是 `data:refresh` 或 `data:derive`。

## 数据原则

- 事实与 AI 判断分开保存。
- 每条结论尽量附带来源、采集时间和置信度。
- 功能目标优先于编程语言。
- 关注、持久、静态工程就绪、任务复用匹配和证据完整度必须分开；未知值不能由代理分数补造。
- 陌生仓库默认只读分析，禁止自动执行代码。
- 北极星指标按近 7 天发生“试用 / 浅克隆 / 确认复用”的不同项目数计算；反馈只用于学习排序，不再冒充实际结果。
- 行动 Event 只追加且由服务端生成发生时间；State 由数据库触发器在同一写入内更新，不能代替历史事件参与周指标。
- 新 JSON 数据产物、canonical D1/API 状态以及页面组件、路由、链接和客户端关联都以 Stable Project ID 作为唯一项目身份；P1-6C1 已由 PR #13、提交 `dfed8f0` 完成。旧 slug 只保留为显示或经 verified Catalog 严格解析的 legacy 兼容字段；跨 retained history 放宽 slug collision 门禁仍留给当前 deferred 的 P1-6C2。
- 官方 RSS 优先；AI News Radar、OpenGithubs 和 HelloGitHub 只作为可归因的补充信号，第三方榜单增长必须由 Rardar 自有快照验证。
