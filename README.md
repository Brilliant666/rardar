# Rardar

Rardar 是一个证据优先的开源情报与项目复用雷达。它将技术事件、GitHub 仓库、能力标签、静态代码证据和用户反馈组织在一起，帮助开发者回答两个问题：

1. 最近真正发生了什么？
2. 我想实现的功能是否已经有项目做过？

## 当前版本

- 今日重点与候选池
- 近期动量与长期高热双赛道评分，每日重点默认平衡为 3 + 2
- 过去 48 小时 AI/技术动态中文简报与信源健康状态
- 近期动量、长期热度、全球影响力与复用价值分层评分
- 自然语言任务拆解、中文能力画像和可解释匹配
- 项目证据页与风险提示
- `有用 / 无用 / 复用 / 待确定` 持久化反馈与偏好重排
- 打开、收藏、试用、浅克隆和确认复用的真实行动记录
- 公共项目只读浅克隆与静态分析工具
- 每日生成的本地 Codex 中文深读队列

当前网页只读取 `data/current.json` 指向的不可变 generation。每个 generation 同时保存真实 GitHub API 快照、目录、技术动态、中文画像和 Codex 队列；网页一次请求只解析一次指针，因此不会把不同代的数据混在一起。首次采集只展示明确标注的“创建以来速度代理”；第二次刷新起会自动归档旧快照并计算真实观测区间增长。刷新流程还会对前五名执行隔离用户 Git 配置的只读浅克隆，静态检查代码、测试、文档与许可证，不执行仓库代码；浅克隆不可用时，改用限制下载体积、解压体积和文件数的 GitHub 官方源码归档，并跳过符号链接。技术动态来自官方 RSS 与可归因的社区补充源，先去重、标注信源健康，再由本地 Codex 为前五条生成中文要点。

近期动量与长期高热分开计算。长期高热在历史样本不足时只使用总 Star、仓库年龄、近期维护和 Fork 生态形成明确标注的结构代理，不把一次快照冒充为“长期持续霸榜”。持续性判断使用最近最多 30 次候选快照；同一仓库在至少 7 次快照中出现且覆盖率达到 70% 后，会自动升级为多周期持续热度验证。

本地 Codex 生成的中文能力画像先发布到 flat staging 区 `data/enrichment/`。运行 `data:derive` 后，当前有效画像才会随完整候选 generation 一起验证并发布。画像只覆盖已核对 README 和静态证据的项目，并与 GitHub 事实分层保存；每份当前画像必须原样绑定仓库推送时间和静态证据分析时间，任一来源版本变化后都不会继续冒充当前结论。

每日刷新还会在候选 generation 中生成 `queues/codex.json`：它只收录重点范围内尚未完成中文画像的项目与动态，并把输入证据绑定到该 generation 的不可变路径，便于本地 Codex 按优先级继续阅读。Codex 写入 flat staging 画像后运行 `npm run data:derive`，只从当前已发布快照构建、校验并原子发布新 generation，不访问 GitHub、不推进增长基线，也不会把真实区间增长退回首次代理。

首页推荐默认沿用事实热度与复用价值排序。浏览器产生反馈后，`/api/recommendations` 会生成匿名设备偏好画像：标记“无用”的项目和相近特征会降低曝光，“有用 / 复用”会提高相似未处理项目的机会，已处理项目本身会减少重复推荐；个性化只做有限调整，不覆盖证据评分主干。

## 开发

需要 Node.js 22.13 或更高版本、Python 3.10 或更高版本，以及 Git。

```bash
python -m pip install -r requirements.txt
npm install
npm run data:generation:status
npm run data:validate
npm run data:refresh
npm run data:audit
npm run dev
npm run build
npm run security:audit:prod
```

`data:audit` 只读核对快照、目录、动态、历史和 Codex 队列的时间、数量、唯一性与 URL 安全边界；`security:audit:prod` 使用 npm 官方漏洞库检查会进入运行环境的依赖。本地构建工具仍应结合完整 `npm audit` 与实际暴露面单独复核。

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
# 显式回滚到仍保留且重新验证通过的 generation
npm run data:generation:rollback -- <generation-id>
```

候选目录位于 `data/generations/.candidates/`，构建、Schema 或审计失败会留下 failed manifest，但不会进入 Git；已经 ready 的候选在发布冲突时保持不可变，指针中断后的 orphan generation 也会保留，稳定错误码和 candidate ID 记录在命令输出与 scheduler 状态中。首次迁移机械复制既有事实和画像，只重建 Codex 队列的证据路径并生成 manifest/current，不补造采集或分析时间。`current.json` 一旦存在，损坏的指针、缺失目录或哈希不一致都会直接失败，不会静默退回 flat 数据。

兼容规则不会伪造历史事实：GitHub snapshot v1 保留既有 `schema_version` 字段和早期 history 形状；两份因对应静态证据缺少可信 `analyzed_at` 而无法绑定的画像，以及一份早于当前静态证据的历史画像，显式保留为 `schemaVersion: 0`，永远不视为当前证据；signal enrichment v1 继续允许旧条目使用顶层 `generatedAt` 作为分析时间回退。旧 flat 树只在 `current.json` 尚不存在时用于一次迁移或作为 Codex enrichment staging，网页和增长基线不会绕过 current 指针。详细模型见 `docs/DATA_MODEL.md`。

Windows 上可以直接双击项目根目录的 `打开 Rardar.cmd`。它会启动一个隐藏的本地管理器，同时看护网站和每日刷新任务，并打开本地首页。管理器会在任一子服务异常退出后自动重启它；调度器即使进程仍存在，只要心跳持续过期，也会在启动宽限期后被自动恢复。运行心跳、PID 和日志保存在 Windows 本地应用数据目录，不会因频繁写入而触发网站热更新；每份日志超过 5 MB 后滚动，并保留最近两份历史。

也可以使用命令管理：

```bash
npm run local:start
npm run local:status
npm run local:stop
```

`local:start` 会在创建后台管理器前检查必要 Python 依赖；缺失时直接停止并提示运行 `python -m pip install -r requirements.txt`，不会自动安装或让 scheduler 进入反复重启。

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
- 全球影响力与个人复用价值独立评分。
- 陌生仓库默认只读分析，禁止自动执行代码。
- 北极星指标按近 7 天发生“试用 / 浅克隆 / 确认复用”的不同项目数计算；反馈只用于学习排序，不再冒充实际结果。
- 官方 RSS 优先；AI News Radar、OpenGithubs 和 HelloGitHub 只作为可归因的补充信号，第三方榜单增长必须由 Rardar 自有快照验证。
