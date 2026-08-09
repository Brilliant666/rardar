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

## 当前 Runtime Operational Readiness 工程轮

P1-6C1 已由 PR #13 通过 Squash merge 提交 `dfed8f0` 合并到 `main`，PR head 与 main push Verify 均通过，Primary Runtime 也已在不执行 refresh、不改变 current generation、snapshot 或 D1 facts 的前提下完成代码同步。P1-6C2 collision history 仍未完成，但经用户明确决策暂时 deferred，不作为本轮上线前运维可靠性工作的 blocker，也不得被解释为已经完成。

本轮唯一目标是：

> 让 Managed Runtime 的每日 schedule 成为显式、可验证且只有一个 owner 的配置，并从当前 verified generation 所绑定的 snapshot `capturedAt` 主动暴露 data freshness，区分“服务存活但数据陈旧”与“published data 已损坏”。

当前分支：

```text
feat/runtime-operational-readiness
```

配置契约固定为：

```text
RARDAR_SCHEDULE_AT=08:00
RARDAR_SCHEDULE_TIMEZONE=Asia/Shanghai
RARDAR_STALE_AFTER_HOURS=36
```

环境缺失时使用以上默认值；时间必须是 canonical `HH:MM`，timezone 必须是可加载的 IANA 名称，阈值必须是正整数小时。非法配置在创建或停止任何进程、写入 Runtime 状态之前失败。Manager 启动时只读取一次配置并把显式参数传给唯一 Scheduler；运行中改变环境不会热更新，必须完整 `local:stop` / `local:start`。`nextRunAt` 仍只由 Scheduler 计算，status JSON 只是 telemetry，不是配置来源。对同一个 canonical data directory，第二个 Scheduler 必须在写 status 或 refresh 前失败。

freshness 的唯一权威是 verified current generation 的 `snapshots/latest.json captured_at`，并要求与同 generation Catalog `capturedAt` 表示同一 UTC instant。年龄不使用文件 mtime、进程启动时间、heartbeat 或 current.json mtime。默认阈值为 36 小时：小于或等于阈值是 `fresh`；超过阈值是 `stale`；非法时间、超出五分钟时钟偏差的未来时间、current/manifest/hash/snapshot 解析失败都是 `invalid` 并保持 fail closed。

仅 stale 时网页继续可读，`/api/health` 返回 HTTP 200、overall `degraded` 和结构化 freshness；`local:status` 必须显示 effective schedule、timezone、next run、last successful refresh、current generation、snapshot time/age/threshold 与 `STALE`；首页只显示轻量提示，不阻断浏览。结构损坏继续返回 503/页面错误，不得伪装成 stale。

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
feat/runtime-operational-readiness
```

### 4. 实现

要求：

- schedule 的 Python/Manager/Scheduler 参数使用一份严格配置契约，默认行为仍为 `08:00 Asia/Shanghai`；
- Manager 是托管模式下唯一 Scheduler owner；scheduler status 必须绑定当前 child PID 与 frozen config，外来或旧 telemetry 不得冒充 healthy；
- current generation loader 继续验证 pointer、manifest、artifact hashes 与 audit，并只把同 generation snapshot 的时间标量传入 Worker；
- freshness 使用固定 36 小时默认阈值与五分钟未来偏差，边界可注入时钟测试但生产没有跳过验证的入口；
- stale 与 corrupt 分流：stale 只降低 data readiness，不重启仍存活的网站；corrupt 继续 fail closed；
- `local:start`、`local:status`、`/api/health` 和首页展示同一个 effective schedule 与 freshness 事实；
- 不实现新的 missed-run catch-up，不改变 refresh/candidate/publish 算法，不启动第二 Scheduler；
- 不实现 P1-6C2，不修改 `0004`/D1/adoption，不清理 21 个 failed candidate，不部署，不开始 TrendRadar/P2、新信源或复杂 Agent。

### 5. 测试

测试必须验证行为。

至少覆盖：

- 默认/custom/非法 schedule、timezone 与 stale threshold，以及 CLI override 优先级；
- Manager child argv/env、完整 restart 后采用新配置、运行中不热更新、status 不能反向改 schedule；
- canonical data directory 的 Scheduler 单实例锁与外来 PID telemetry 拒绝；
- `<36h`、`=36h`、`>36h`、非法/未来 snapshot 时间；
- pointer switch 同时更新 generation 与 snapshot freshness，损坏 pointer/manifest/hash 不降级成 stale；
- stale 时 health HTTP 200/degraded、local status STALE、首页 banner；fresh 时不显示 banner；
- 真实隔离 Vinext HTTP 使用随机 loopback 端口且不占 3000/3002，结束后无残留进程或状态；
- 完整 `npm run verify`、production audit、正式 data/Git/Primary Runtime 隔离门禁通过。

### 6. 完整验证

输出：

```text
通过：
失败：
未运行：
原因：
```

### 7. Draft PR

PR 描述包含：

```text
背景
问题
修改
schedule 配置来源与默认值
Scheduler 单一 ownership
snapshot freshness 权威与边界
stale / corrupt HTTP 语义
local status 与首页提示
兼容性
测试
安全边界
回滚
P1-6C2 非目标
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
7. Runtime Operational Readiness——当前用户明确授权的唯一工程轮；完成 Draft PR 后停止。

每轮只做一项，每轮创建 Draft PR，每轮完成后停止。
