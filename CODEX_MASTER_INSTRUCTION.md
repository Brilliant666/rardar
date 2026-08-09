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

## 当前 P1-6C1 Client Stable Project Identity 工程轮

PR #9 已通过 Squash merge 提交 `c24b7d6` 合并到 `main`，对应 `main` Verify 已通过。P1-6B 的正式 Primary Runtime D1 adoption、完整重启与重复只读 adoption no-op 也已验证通过；服务端 Stable Project ID、D1 兼容迁移与 API 边界已经完成。

P1-6 Stable IDs 大阶段仍在进行；本轮唯一目标是：

> P1-6C1：让网页路由、链接、组件交互、个性化关联和浏览器本地状态使用 Stable Project ID，同时为旧 slug URL 提供严格、无猜测的兼容解析。

当前分支：

```text
feat/stable-project-ui-identity
```

本轮只交付客户端与页面消费边界。所有项目级 UI identity、Action/feedback 请求、recommendation/watch 关联和 React key 都使用 `projectIdVersion: 1` 与 `projectId`；slug 只作为显示字段和 legacy URL 输入。canonical 详情 URL 固定为 `/project/v1/<projectId>`。旧 `/projects/<slug>` 只在同一次请求加载的 verified Catalog 中解析：唯一匹配返回 `302` 且 `Cache-Control: no-store`，未知返回 `404`，歧义返回 `409`；不得选择第一项、哈希 slug、信任客户端 repository 或回退陈旧 D1 映射。

Catalog v1/v2 消费时从 `repo` 机械派生 identity v1，Catalog v3 必须从 repository 重算并核对已发布 projectId。页面一次请求只使用同一个 published generation bundle；current 原子切换后的下一请求必须看到新 generation，退出现行 Catalog 的 projectId 必须 fail closed，而不能显示旧 generation 项目。

P1-6C1 不放宽现有 unresolved legacy slug collision 发布门禁。让新 generation 真正接受相同 legacy slug、处理 retained collision history 和定义兼容 URL 的长期歧义策略属于后续 P1-6C2 独立工程轮。

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
feat/stable-project-ui-identity
```

### 4. 实现

要求：

- 最小改动，复用 `app/project-identity.mjs` 的 identity v1、resolver 与共享 golden vectors；
- 服务端页面入口从同一次 verified published bundle 构造 generation-bound identity context；Catalog v1/v2 从 `repo` 机械派生，v3 从 repository 重算并严格核对；
- 所有页面项目、链接、React key、Action/feedback props 与请求、recommendation 关联和 watch/local 状态以 projectId 为键；slug 仅用于显示和 legacy URL 输入；
- canonical 详情页为 `/project/v1/<projectId>`，并严格拒绝错误版本、畸形、伪造、未知或已退出 current Catalog 的 ID；
- `/projects/<slug>` 唯一匹配时返回 `302` 与 `Cache-Control: no-store`，未知返回 `404`，歧义返回 `409`；不得把 redirect 当成 canonical identity；
- 页面与 API 的一次请求不得分别读取两次 current 或混合 generation；pointer 切换后下一请求读取新 generation；
- 保持现有 `drizzle/0004_stable_project_identity.sql`、D1 schema、adoption、API legacy selector 和 collision/unresolved 发布门禁不变；
- 不实现 P1-6C2 collision history，不修改 scheduler 配置，不清理 21 个 failed candidate，不开始 TrendRadar、P2、复杂 Agent 或新信源；
- 不执行第三方仓库代码，不部署，不修改 `main`，不自动合并。

### 5. 测试

测试必须验证行为。

至少覆盖：

- Catalog v1/v2 的 UI identity 机械派生和 Catalog v3 的已发布 identity 重算核对；
- canonical URL 构造、详情 SSR、错误版本、畸形/伪造/未知 ID、路径编码与 current Catalog 退役项目；
- legacy slug 唯一 `302 no-store`、未知 `404`、歧义 `409`，不得猜测目标；
- 两个项目即使提供相同显示 slug，recommendation、watch 状态、反馈、行动和 React key 仍按不同 projectId 隔离；
- Action/feedback 客户端 payload 只发送 stable identity pair，不再把 slug 当作 canonical selector；
- pointer 原子切换后，无需重启的下一次真实 Vinext HTTP 请求读取新 generation，单个响应不混代；
- Catalog v1 retained rollback 后 canonical/legacy 路由仍可保守读取；
- 现有 D1/API、Weekly metric、Schema/Audit、Primary Runtime、正式 data 与 3000 端口不回归；
- 完整 `npm run verify` 通过。

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
generation-bound UI 身份
canonical 项目路由
legacy slug redirect 与错误语义
Action、feedback、recommendation 与 watch 客户端迁移
Catalog v1/v2/v3 兼容
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
   - P1-6C1 客户端、页面路由与 legacy URL 兼容——当前唯一工程轮；
   - P1-6C2 collision history 与 legacy slug 发布门禁演进——P1-6C1 合并后的独立候选目标，本轮不得开始。

每轮只做一项，每轮创建 Draft PR，每轮完成后停止。
