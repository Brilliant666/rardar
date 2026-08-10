# Codex Master Instruction for Rardar

你现在位于仓库：

```text
Brilliant666/rardar
```

本次任务不是立即把所有长期目标一次性实现，而是先理解项目治理文档，然后选择当前最高优先级、最小且可独立验证的一项改进，完成实现、测试和 Draft PR。

## 必须阅读

请完整阅读：

```text
README.md
AGENTS.md
docs/RARDAR_AUDIT_BASELINE.md
docs/RARDAR_NORTH_STAR.md
docs/RARDAR_EVOLUTION_PROTOCOL.md
```

阅读完成后，先用自己的话总结：

1. Rardar 的长期使命；
2. 北极星指标；
3. 不可退让的安全和数据原则；
4. 当前 P1 问题；
5. 本轮选择的唯一目标；
6. 为什么它是当前最高优先级；
7. 本轮明确不做什么。

然后直接执行，不要重复询问已经在文档中明确的信息。

## 已完成的评分语义工程轮

PR #6 已通过提交 `ab34119` 合并到 `main`，评分语义 P1-4 已完成：

> 修正评分名称与证据能力不一致的问题，明确区分关注优先级、持久热度、静态工程就绪度、具体任务复用适配度和证据完整度。

治理状态：本节保留已完成评分语义工程轮的验收与执行记录，不再是当前目标，也不得据此重复创建评分实现。

评分语义工程轮开始时要求以下前置条件全部满足：

- PR #5 已合并；
- 最新 `main` 已包含追加式 Event、独立 State 和按近 7 天 Event 计算的 Weekly Acted Projects；
- 工作区干净；
- 不存在尚未完成的行动事件修正 PR。

这些条件属于已完成工程轮的历史门槛。

选择这一任务的原因：

- 当前 `globalScore` 实际更接近关注优先级，却被页面描述成全球影响力；
- 当前 `reuseScore` 只依据静态文件存在性，却被描述成复用价值，并可能触发确定性的“复用”建议；
- 没有具体任务上下文或隔离运行证据时，Rardar 不能声称项目适合直接复用；
- 评分会直接影响每日五项、个性化排序和用户行动，语义夸大比单纯缺少功能更容易造成错误决策；
- 追加式行动事件合并后，评分语义曾是长期优先级中第一个尚未完成的目标。

## 已完成工程轮验收记录

至少完成：

1. 新 Catalog 明确发布 Attention Score、Endurance Score、Engineering Readiness、Reuse Fit Score 和 Evidence Completeness，不再发布含义模糊的 `globalScore`、`reuseScore` 或 `momentumScore`。
2. Engineering Readiness 只能来自与当前仓库推送匹配的只读静态证据；没有当前证据时必须为 `null`，且静态评分不得描述成运行可靠性。
3. 通用目录没有具体任务上下文时，Reuse Fit Score 必须为 `null`；中文能力画像和适用场景只能作为假设，不能冒充任务匹配事实。
4. 每项评分都必须携带结构化说明，区分事实、代理、未知或限制，以及升级条件。
5. 推荐不得输出“直接复用”；没有实际运行验证时，最强建议只能是带许可证和风险门槛的“隔离试用”。
6. Catalog 新增明确 Schema 版本；旧 v1 generation 仍能严格验证、审计、回滚和由网页保守读取，不能把旧 `reuseScore` 静默解释成新版 Engineering Readiness。
7. generation audit 对 v2 使用生产构建器重算评分、解释、推荐和排序；任一语义字段被篡改都必须阻止发布，v1 历史审计摘要保持不变。
8. 页面和推荐 API 统一使用新名称；任务搜索分数明确为任务匹配规则分而非复用概率；用户真实反馈“复用”和行动“确认复用”保持事实语义不变。
9. 测试至少覆盖 v1/v2 契约互斥、空值边界、静态证据当前性、无任务上下文、风险与许可证门槛、审计篡改、旧版网页兼容、个性化和真实 HTTP 读取。
10. 不顺带实现 verify/CI、稳定项目 ID、新信源、UI 重设计、第三方代码执行或部署；运行完整验证，创建 Draft PR，然后停止。

## 已完成的 Runtime Operational Readiness 工程轮

PR #14 已通过 Squash merge 提交 `e61e3ff35390ab9f915818f72e5e3321896fd17e` 合并到 `main`。该提交对应的 main push Verify run `31351088836` 为 `SUCCESS`，因此显式 schedule、单一 Scheduler ownership、snapshot freshness 与 stale/corrupt 分流已经完成，不再是当前工程目标。

P1-6C2 collision history 仍未完成，但经用户明确决策继续 deferred。Always-on Deployment v1 是一次独立、显式授权的上线前工程轮；它不关闭 P1-6C2，也不得放宽 legacy slug collision gate。

## 当前 Always-on Deployment v1 工程轮

只有以下条件同时成立才开始本轮：

- 最新 `main` 包含 PR #14 的 `e61e3ff`；
- main push Verify run `31351088836` 已通过；
- 开发 worktree 干净且基于该提交；
- 不存在尚未完成的 Runtime Operational Readiness 修正 PR；
- Primary Runtime、正式 data、D1 和 failed candidates 不作为开发或测试夹具。

本轮唯一目标是：

> 让 Rardar 可以在标准 Ubuntu 24.04 LTS 或 Debian-compatible x86_64 单机上，由 systemd 托管唯一 foreground Manager 长期运行，并具备显式路径、离线/在线预检、停机备份、可诊断健康检查和可回滚发布协议。

当前分支：

```text
feat/always-on-deployment
```

本轮只完成“可部署工程化”。不得 SSH 到真实服务器、迁移 Primary data、配置 DNS/TLS/防火墙或执行真实生产部署；这些必须留给后续单独授权的 `PROD-DEPLOY-01`。

### 固定架构

```text
systemd
  └─ foreground Rardar Manager
       ├─ loopback Vinext website
       └─ single Scheduler
```

- systemd 只管理 Manager；禁止再创建独立 Scheduler service 或第二个 owner；
- service 使用非 root `rardar` 用户，网络 ready 后启动，失败有界重启，SIGTERM 必须收口全部 children；
- Website 和 Runtime status 只绑定 loopback，默认分别为 `127.0.0.1:3000` 与 `127.0.0.1:3002`；3002 不对反向代理开放；
- 第一版保留已经通过真实 HTTP 验证的 Vinext dev compatibility entry：Manager 直接运行 `vite --configLoader runner --host 127.0.0.1 --port <configured> --strictPort`，由 `vite.config.ts` 加载 Vinext/Cloudflare 插件；runtime cache 必须外置，系统字体不得生成 `.vinext/fonts`，release-local `.env*`/`.dev.vars*` 必须由 offline checker 拒绝。当前 `vinext start` 的本地 Node 启动会因构建产物中的 `cloudflare:` URL scheme 不受支持而失败，不能被文档伪装成已支持；`npm run build` 仍是完整 Verify 和 release 的硬门禁；
- 外部访问只能通过操作者显式建立的 SSH tunnel 或经过单独审查的反向代理；本轮最多提交 sample，不修改真实代理、证书或公网入口。

### 路径、环境和 secrets

代码 release 与 mutable state 必须完全分离。Always-on v1 只支持与版本控制 systemd unit 一致的固定 canonical profile：

```text
RARDAR_HOME=/opt/rardar/current
RARDAR_DATA_DIR=/var/lib/rardar/data
RARDAR_RUNTIME_DIR=/var/lib/rardar/runtime
RARDAR_VINEXT_STATE_DIR=/var/lib/rardar/vinext-state
RARDAR_DATA_LOCK_DIR=/var/lib/rardar/locks
RARDAR_VITE_CACHE_DIR=/var/cache/rardar/vite
RARDAR_BACKUP_DIR=/var/backups/rardar
WRANGLER_LOG_PATH=/var/log/rardar/wrangler
WRANGLER_REGISTRY_PATH=/var/lib/rardar/runtime/wrangler-registry
MINIFLARE_REGISTRY_PATH=/var/lib/rardar/runtime/miniflare-registry
RARDAR_NODE=/usr/bin/node
RARDAR_PYTHON=/opt/rardar/current/.venv/bin/python
RARDAR_VINEXT_PORT=3000
RARDAR_RUNTIME_STATUS_PORT=3002
RARDAR_SCHEDULE_AT=08:00
RARDAR_SCHEDULE_TIMEZONE=Asia/Shanghai
RARDAR_STALE_AFTER_HOURS=36
```

- 除 `RARDAR_HOME` 外，所有持久路径必须预先创建、绝对且不经过 symlink；data、D1、runtime、locks、cache 与 backup 主根互不重叠，两个 registry 只允许位于上述固定的 runtime 子目录，Wrangler log 只允许位于固定 log 根；
- `RARDAR_HOME=/opt/rardar/current` 允许最后一个路径组件是用于原子切换的 symlink；它的所有祖先必须不是 symlink，解析目标必须是包含 exact commit、锁定依赖、build 输出和独立 `.venv` 的当前 release，且必需 release 文件不得是 symlink；不能在正在运行的 release 内 `git pull`；
- D1/Miniflare state、data、runtime telemetry/locks、cache、logs 和 backup 都不得落入代码 release；
- 仅修改环境变量来采用自定义目录不属于 v1 支持面；自定义布局必须作为后续独立目标生成并审查配套 unit/drop-in、checker 映射、写权限、备份和回滚协议；
- 版本控制只保存安全示例；真实凭据只能存在于 `/etc/rardar/rardar.secret` 或等价 root-owned 文件，不进入 Git、日志、PR 或命令回显。

### 发布、检查和回滚

- `deploy:preflight` 是只读、fail-closed 的 offline check：验证工具链、release、路径/权限/磁盘、完整 audited generation 与 D1 integrity；D1 source main、`-wal`/`-journal` 只按稳定字节复制到系统临时 scratch（systemd `PrivateTmp`），WAL recovery、`quick_check` 和表指纹只在副本执行，正式 source 不建立 SQLite 连接、不写入；它不得创建部署目录、修复数据、启动服务或执行 refresh；
- `deploy:check` 先重复 offline check，再验证 Manager/Website/Scheduler 的 PID 和命令身份、唯一 loopback listener、Runtime status、`/api/health`、首页、signals、search 以及检查期间 generation 不变；
- fresh 可接受 HTTP 200/healthy；明确的 stale 数据只允许 HTTP 200/degraded + `published_data_stale`；invalid/corrupt 或 HTTP 503 必须失败；
- 发布前必须停止 Managed Runtime，再对 data 和完整 Vinext/D1 state 制作同一停机点的备份；不得删除 failed candidates；
- 代码回滚只切回上一 exact release，通常保留 data/D1；generation 回滚只使用现有受锁、全量复核的显式 rollback；只有持久数据或 D1 迁移失败时才允许把 data + D1 作为同一备份单元恢复；
- 既有 Scheduler restart catch-up 语义保持不变。真实服务器停机或重启后可能自然触发 refresh；部署协议必须记录该副作用，不能修改 `nextRunAt`、手工 refresh 或把 pointer 前进误判为部署脚本改写数据。

## 执行流程

### 1. 检查仓库

```bash
git status
git branch --show-current
git log -5 --oneline
```

确认没有覆盖未提交修改。

### 2. 建立基线

运行：

```bash
npm run verify
```

本地运行必须让 `RARDAR_PYTHON` 指向当前 worktree 自有 `.venv` 的绝对解释器路径。准确记录通过、失败和未运行项，并确认 Verify 的正式 data、Git 状态与隔离 Runtime 门禁通过。

### 3. 创建分支

建议：

```text
feat/always-on-deployment
```

### 4. 实现

要求：

- 增加版本控制的环境示例、systemd unit、只读 offline/online deployment checker 和完整运维文档；
- Manager、Scheduler、data lock、Vinext/D1 state 与状态端点消费同一份经过严格验证的绝对路径和 loopback port 契约；
- `pipeline.runtime service` 保持 foreground，不 daemonize；第二个 Manager 或 Scheduler 必须在写状态、刷新或抢占端口前明确失败；
- systemd restart、SIGTERM 和失败重启后都只能剩一个 Manager、一个 Website 和一个 Scheduler；
- 保持 generation 的 pointer/manifest/hash/Schema/Audit 边界和 D1 additive schema 不变；
- 不修改 refresh、candidate、publish、评分、信源或 Stable ID；不清理 failed candidates；
- 不实现 P1-6C2、TrendRadar/P2 或复杂 Agent。本轮不执行真实部署。

### 5. 测试

测试必须验证行为。

至少覆盖：

- Linux 绝对路径、空值/相对路径、重叠目录、symlink 和 code/mutable-state 分离；
- env、toolchain、Node/Python 版本、磁盘、可写性、完整 generation 与 SQLite quick check 的失败路径；
- systemd unit 的 foreground Manager、non-root、EnvironmentFile、network-online、restart、stop timeout 与单一 ownership 契约；
- 随机 loopback 端口上的真实 Manager lifecycle：start、SIGTERM、restart、旧 children 退出、无第二 Scheduler；
- offline check 零写入；online check 的 PID/command/listener ownership、同一 generation 和四个 HTTP 入口；
- fresh 与 stale 允许，invalid/corrupt/503、公开 listener、D1 缺失/损坏和 generation 变化拒绝；
- code rollback、generation rollback 与 data+D1 成对恢复的 preservation 边界；
- 测试只使用临时 data、D1、runtime、locks、cache 和随机端口，不访问 Primary Runtime、不占 3000/3002，结束后无残留进程或状态；
- 完整 `npm run verify`、production audit、正式 data/Git/Primary Runtime 隔离门禁通过。

### 6. 完整验证

运行：

```bash
npm run verify
git diff --check
git diff -- data
git status --short --untracked-files=all
```

最终报告必须区分实际通过、失败和未运行项，并使用以下结构：

```text
任务结果：PASS / BLOCKED / FAIL

基线：
branch：
base：
head：

Architecture：
systemd owner：
website entry：
website bind：
persistent data：
env/secrets：

Deployment checks：
offline：
online：
generation：
D1 integrity：
freshness：
single Manager / Website / Scheduler：

Backup / rollback：
stopped-state backup：
code rollback：
generation rollback：
data + D1 rollback：
catch-up side effect：

Verify：
通过：
失败：
未运行：
原因：

Draft PR：
URL：
status：Draft

边界：
是否真实部署：否
是否 SSH/DNS/TLS/防火墙：否
是否 refresh：否
是否删除 failed candidates：否
是否开始 P1-6C2：否
是否开始 TrendRadar/P2：否
```

### 7. Draft PR

PR 描述包含：

```text
背景
问题
修改
Linux 与 systemd 架构
exact release 与路径契约
Manager / Scheduler 单一 ownership
Vinext dev compatibility（direct Vite runner）与 vinext start 限制
offline / online deployment check
持久 data、D1、runtime、locks 与 cache
停机备份与三类 rollback
catch-up 副作用
兼容性
测试
安全边界
回滚
P1-6C2 非目标
PROD-DEPLOY-01 非目标
遗留问题
```

完成 Draft PR 后停止，等待审查。

## 后续迭代规则

每轮应结合最新 `main` 与 `docs/iterations/` 按照文档优先级处理：

1. 数据 Schema 和统一契约——已由 PR #2 完成；
2. audited generations——已由 PR #4（提交 `bf35575`）完成；
3. 追加式行动事件——已由 PR #5（提交 `238b572`）完成；
4. 评分语义——已由 PR #6（提交 `ab34119`）完成；
5. verify 和 GitHub Actions——已由 PR #7（提交 `3430e30`）完成；
6. 稳定项目 ID——大阶段正在进行：
   - P1-6A 身份契约与 JSON 数据层——已由 PR #8、提交 `d41033f` 完成；
   - P1-6B D1 与 Action API 采用 `projectId`——已由 PR #9、提交 `c24b7d6` 完成，正式 Primary Runtime adoption/restart/no-op 已通过；
   - P1-6C1 客户端、页面路由与 legacy URL 兼容——已由 PR #13、提交 `dfed8f0` 完成；
   - P1-6C2 collision history 与 legacy slug 发布门禁演进——仍未完成，但经用户明确选择在本轮 deferred。
7. Runtime Operational Readiness——已由 PR #14、提交 `e61e3ff` 完成，main Verify run `31351088836` 通过；
8. Always-on Deployment v1——当前用户明确授权的唯一工程轮，只完成可部署工程化；
9. `PROD-DEPLOY-01`——只有 Always-on v1 合并且用户再次明确授权后，才允许对真实服务器执行部署。

P1-6C2 仍是未完成项，但当前继续 deferred；Always-on v1 不改变该长期状态。每轮只做一项，每轮创建 Draft PR，每轮完成后停止。
