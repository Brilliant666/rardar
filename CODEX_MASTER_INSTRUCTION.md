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

## 当前 P1-6A Stable Project ID 工程轮

PR #7 已通过提交 `3430e30` 合并到 `main`，P1-5 Verify/CI 已完成。P1-6 Stable IDs 大阶段正在进行；本轮唯一目标是：

> P1-6A：建立统一、抗碰撞的 Stable Project ID 契约，并让 Catalog、项目静态证据、项目画像、Codex Queue 和 generation audit 采用该身份。

当前分支：

```text
feat/stable-project-ids
```

本轮只交付身份与 JSON 数据层。D1、Action API、Weekly Acted Projects、页面路由、链接、按钮状态和旧 slug URL 重定向均不在本轮修改范围内。P1-6A 只有在对应 PR 合并到 `main` 后才视为完成；P1-6 整体仍未完成。P1-6A 合并后的下一项只能是 P1-6B（D1 与 Action API 采用 `projectId`），之后才是 P1-6C（UI、页面路由与 legacy URL 兼容）。

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
feat/stable-project-ids
```

### 4. 实现

要求：

- 最小改动；
- 建立单一版本化身份算法和 Python/Node 共享 golden vectors；
- 为新 Catalog、项目静态证据、项目画像和 Codex Queue 使用明确的新 Schema 版本；
- 让 refresh、derive、Schema 和 audit 对 repository、projectId、文件名和队列路径执行同一身份门禁；
- 提供只作用于 flat staging、默认 dry-run 的安全幂等迁移入口；
- 保留 Catalog v1/v2、旧证据/画像和 Queue v1 generation 的严格读取、审计与显式 rollback；
- 不引入大型框架；
- 不执行第三方仓库代码；
- 不迁移 D1、Action API 或页面路由；
- 不部署；
- 不修改 main；
- 不自动合并。

### 5. 测试

测试必须验证行为。

至少覆盖：

- 身份确定性、大小写规范化、文件名安全、最大合法长度和非法 repository 拒绝；
- `owner/foo.bar` 与 `owner/foo-bar` 等旧 slug 碰撞对得到不同 ID；
- Python 与 Node 读取同一份版本化 golden vectors，人工伪造 ID 被拒绝；
- Catalog v3、静态证据 v2、项目画像 v2 与 Queue v2 的 repository、projectId 和路径完全一致；
- 重复 ID、规范化 repository 重复、文件名/payload 不一致和 unresolved legacy collision 阻止候选发布；
- refresh 与 derive 都执行身份门禁，既有 Catalog v1/v2 generation 仍可验证和显式 rollback；
- staging migration 的 dry-run、apply、重复 apply、冲突、符号链接/逃逸、中断重试和 unresolved 行为；
- 完整 `npm run verify` 通过且正式数据、Primary Runtime 与 3000 端口不受影响。

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
identity v1 算法
版本化 artifact 变化
collision 与 unresolved 策略
legacy generation 兼容
staging migration
兼容性
测试
安全边界
迁移
回滚
P1-6B / P1-6C 非目标
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
   - P1-6A 身份契约与 JSON 数据层——当前独立工程轮，仅在对应 PR 合并后完成；
   - P1-6B D1 与 Action API 采用 `projectId`——P1-6A 合并后的下一工程轮；
   - P1-6C UI、页面路由与 legacy URL 兼容——P1-6B 之后处理。

每轮只做一项，每轮创建 Draft PR，每轮完成后停止。
