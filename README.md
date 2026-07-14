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

当前网页只读取 `data/current.json` 指向的不可变 generation。Vinext Cloudflare Worker 不直接读取宿主文件；默认 `vinext dev` 通过仅接受回环请求和随机 token 的 Vite host 数据桥，在每次网页或 API 请求中让 Node host 完整解析一次 current、manifest 和全部 artifact 哈希，再把同一 generation 的一次性 bundle 交给 Worker。桥地址由本地 Vinext 配置固定，不信任或转发外部请求的 `Host`，避免回环 SSRF 与 token 泄露。每个 generation 同时保存真实 GitHub API 快照、目录、技术动态、中文画像和 Codex 队列，因此单个响应不会把不同代的数据混在一起；下一请求会立即观察到原子 pointer 切换，损坏 current 时直接失败而不回退 flat。首次采集只展示明确标注的“创建以来速度代理”；第二次刷新起会自动归档旧快照并计算真实观测区间增长。刷新流程还会对前五名执行隔离用户 Git 配置的只读浅克隆，静态检查代码、测试、文档与许可证，不执行仓库代码；浅克隆不可用时，改用限制下载体积、解压体积和文件数的 GitHub 官方源码归档，并跳过符号链接。技术动态来自官方 RSS 与可归因的社区补充源，先去重、标注信源健康，再由本地 Codex 为前五条生成中文要点。

近期动量与长期高热分开计算。长期高热在历史样本不足时只使用总 Star、仓库年龄、近期维护和 Fork 生态形成明确标注的结构代理，不把一次快照冒充为“长期持续霸榜”。持续性判断使用最近最多 30 次候选快照；同一仓库在至少 7 次快照中出现且覆盖率达到 70% 后，会自动升级为多周期持续热度验证。

Catalog v2 使用 `evidence-v2` 评分模型：Attention 只回答是否值得先看，Endurance 只回答是否有长期热度线索，Engineering Readiness 只使用与当前推送匹配的只读静态证据。通用目录没有你的具体任务、约束和验收标准，因此 Reuse Fit 保持未知；Evidence Completeness 只描述证据覆盖，不代表质量。每项都公开事实、代理、限制和升级条件。默认流水线从不运行第三方代码，所以最强建议只能是满足许可证与风险门槛后的“隔离试用”，不会把静态文件完整度写成“直接复用”。

本地 Codex 生成的中文能力画像先发布到 flat staging 区 `data/enrichment/`。运行 `data:derive` 后，当前有效画像才会随完整候选 generation 一起验证并发布。画像只覆盖已核对 README 和静态证据的项目，并与 GitHub 事实分层保存；每份当前画像必须原样绑定仓库推送时间和静态证据分析时间，任一来源版本变化后都不会继续冒充当前结论。

每日刷新还会在候选 generation 中生成 `queues/codex.json`：它只收录重点范围内尚未完成中文画像的项目与动态，并把输入证据绑定到该 generation 的不可变路径，便于本地 Codex 按优先级继续阅读。Codex 写入 flat staging 画像后运行 `npm run data:derive`，只从当前已发布快照构建、校验并原子发布新 generation，不访问 GitHub、不推进增长基线，也不会把真实区间增长退回首次代理。

首页推荐默认以关注优先级为主；存在当前静态证据时，再有限纳入工程就绪度。浏览器产生反馈后，`/api/recommendations` 会生成匿名设备偏好画像：标记“无用”的项目和相近特征会降低曝光，“有用 / 复用”会提高相似未处理项目的机会，已处理项目本身会减少重复推荐；个性化只做有限调整，不覆盖事实、风险与证据边界。这里的“复用”是用户已经发生的反馈事实，不是系统预测的 Reuse Fit。

真实行动使用 D1 中分离的 Event 与 State：`project_action_events` 只追加“发生过什么”，`project_action_state` 保存当前最高阶段和每个真实点击阶段的最近时间。客户端为一次行动意图生成幂等键，并在网络重试中复用；同键重试不会重复写 Event，同一项目和行动在以后再次发生时使用新键，仍会成为新的历史事实。按钮与观察列表只读取 State，近 7 天指标只读取 Event。旧 `project_actions` 会在运行时按原始 `created_at` 逐行迁移且继续保留为回滚兼容投影；新 Event 会把该投影推进到同阶段的最近真实时间，并规范化为旧版周指标可安全比较的 UTC SQLite 时间文本，但不会补造缺失阶段或历史事件。

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

`contracts/` 为 GitHub 快照、技术动态、静态证据、项目画像、动态画像、目录、Codex 队列、generation manifest 和 current 指针保存版本化 JSON Schema。`pipeline.schema_validation` 是统一验证入口：它拒绝未知版本、错误字段类型、非字符串数组成员、无时区时间、非 HTTP(S) URL、非法仓库名、危险产物路径、重复 JSON 键、非有限数值和超长文本。

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

兼容规则不会伪造历史事实：GitHub snapshot v1 保留既有 `schema_version` 字段和早期 history 形状；两份因对应静态证据缺少可信 `analyzed_at` 而无法绑定的画像，以及一份早于当前静态证据的历史画像，显式保留为 `schemaVersion: 0`，永远不视为当前证据；signal enrichment v1 继续允许旧条目使用顶层 `generatedAt` 作为分析时间回退。Catalog v1 generation 仍可严格验证和回滚；网页只把旧 `globalScore` 保守显示为关注优先级，旧 `reuseScore` 不会升级成新版工程就绪度，旧强推荐也会降级为隔离试用。旧 flat 树只在 `current.json` 尚不存在时用于一次迁移或作为 Codex enrichment staging，网页和增长基线不会绕过 current 指针。详细模型见 `docs/DATA_MODEL.md`。

Windows 上可以直接双击项目根目录的 `打开 Rardar.cmd`。它会启动一个隐藏的本地管理器，同时看护网站和每日刷新任务，并打开本地首页。管理器会在任一子服务异常退出后自动重启它；调度器即使进程仍存在，只要心跳持续过期，也会在启动宽限期后被自动恢复。运行心跳、PID 和日志保存在 Windows 本地应用数据目录，不会因频繁写入而触发网站热更新；每份日志超过 5 MB 后滚动，并保留最近两份历史。

也可以使用命令管理：

```bash
npm run local:start
npm run local:status
npm run local:stop
```

`local:start` 会在创建后台管理器前检查必要 Python 依赖；缺失时直接停止并提示运行 `python -m pip install -r requirements.txt`，不会自动安装或让 scheduler 进入反复重启。

本地网站通过 `/api/health` 实际加载并验证当前 published generation。管理器只有在该端点返回 `200`、`status: healthy` 和安全的 `generationId` 时才把网站标为 healthy；HTTP 失败会记录简短诊断并标为 degraded，但不会重启一个仍存活的 Vinext 进程。数据经 rollback 恢复后，同一进程会在下一次健康探测中自动恢复 healthy。

如需单独调试每日刷新守护进程：

```bash
npm run data:schedule
```

默认在 `Asia/Shanghai` 每天 08:00 刷新 GitHub 快照、前五静态检查和技术动态。刷新期间调度器会持续写入运行心跳；若进程中途退出，管理器重启后会在 12 小时窗口内补跑，网络等临时故障则每 5 分钟重试，单轮最多 3 次。守护进程不会部署网站，也不会执行候选仓库代码。

“动态”页面从本地管理器的实时心跳读取运行状态；旧状态超过 35 秒就会显示需要重新启动，不再把过期的 `scheduled` 文件误报为正在运行。

默认本地预览地址：<http://127.0.0.1:3000/>。项目默认不发布线上版本，除非用户明确提出部署要求。

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
- 官方 RSS 优先；AI News Radar、OpenGithubs 和 HelloGitHub 只作为可归因的补充信号，第三方榜单增长必须由 Rardar 自有快照验证。
