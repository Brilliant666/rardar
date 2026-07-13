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

## 当前评分语义工程轮

PR #5 已通过提交 `238b572` 合并到 `main`。当前第一个尚未完成的工程目标是：

> 修正评分名称与证据能力不一致的问题，明确区分关注优先级、持久热度、静态工程就绪度、具体任务复用适配度和证据完整度。

治理状态：当前评分语义分支正在实现本节目标；只有对应 PR 合并到 `main` 后才视为完成。本节验收与执行清单在合并前继续用于审查当前 PR，合并后不得据此重复创建评分实现。

只有以下前置条件全部满足，才允许开始评分语义目标：

- PR #5 已合并；
- 最新 `main` 已包含追加式 Event、独立 State 和按近 7 天 Event 计算的 Weekly Acted Projects；
- 工作区干净；
- 不存在尚未完成的行动事件修正 PR。

任一条件未满足时，停止并处理现有 PR 或工作区状态，不得提前创建评分分支或实现代码。

选择这一任务的原因：

- 当前 `globalScore` 实际更接近关注优先级，却被页面描述成全球影响力；
- 当前 `reuseScore` 只依据静态文件存在性，却被描述成复用价值，并可能触发确定性的“复用”建议；
- 没有具体任务上下文或隔离运行证据时，Rardar 不能声称项目适合直接复用；
- 评分会直接影响每日五项、个性化排序和用户行动，语义夸大比单纯缺少功能更容易造成错误决策；
- 追加式行动事件合并后，评分语义是长期优先级中第一个尚未完成的目标。

## 当前工程轮验收标准

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
npm run lint
python -m unittest discover -s pipeline -p "test_*.py"
npm run data:validate
npm run data:audit
npm run build
npm test
npm run security:audit:prod
```

准确记录通过、失败和未运行项。

### 3. 创建分支

建议：

```text
fix/scoring-semantics
```

### 4. 实现

要求：

- 最小改动；
- 复用现有事实快照、静态证据、generation 发布和网页数据桥；
- 明确评分名称、证据上限、未知值和版本兼容边界；
- 不引入大型框架；
- 不执行第三方仓库代码；
- 不部署；
- 不修改 main；
- 不自动合并。

### 5. 测试

测试必须验证行为。

至少覆盖：

- v1 与 v2 Catalog 均严格通过各自 Schema，未知版本和新旧字段混用失败；
- 没有当前静态证据时 Engineering Readiness 为未知，当前静态证据只产生静态就绪度；
- 没有具体任务上下文时 Reuse Fit Score 为未知；
- 风险、许可证、证据完整度和推荐上限正确；
- v2 分数、解释、推荐或排序被修改后跨文件审计失败；
- 旧 v1 generation 可回滚并由网页保守显示，不能恢复强“复用”结论；
- 个性化、任务搜索和真实 Vinext HTTP 页面统一使用新语义；
- 当前 v2 generation 能通过完整 Schema、audit、hash 和网页读取链路。

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
评分模型与证据边界
Catalog v2 与 v1 兼容
审计重算协议
页面和推荐语义
兼容性
测试
安全边界
迁移
回滚
遗留问题
```

完成 Draft PR 后停止，等待审查。

## 当前评分语义 PR 合并后的下一默认任务

只有当前评分语义 PR 已合并、最新 `main` 已包含 evidence-v2 评分与旧 generation 兼容、工作区干净且不存在尚未完成的评分修正 PR 时，才允许开始：

> 建立仓库级统一 `verify` 命令与 GitHub Actions 合并门槛。

建议分支：

```text
feat/verify-ci
```

该轮必须重新阅读 `docs/RARDAR_AUDIT_BASELINE.md` 的 P1-5，另建独立 Draft PR；不得在当前评分语义 PR 中提前修改 CI、分支保护或稳定项目 ID。

## 后续迭代规则

每轮应结合最新 `main` 与 `docs/iterations/` 按照文档优先级处理：

1. 数据 Schema 和统一契约——已由 PR #2 完成；
2. audited generations——已由 PR #4（提交 `bf35575`）完成；
3. 追加式行动事件——已由 PR #5（提交 `238b572`）完成；
4. 评分语义——由当前评分语义分支实现，仅在对应 PR 合并到 `main` 后视为完成；
5. verify 和 GitHub Actions——当前评分语义 PR 合并后的第一个未完成项；
6. 稳定项目 ID。

每轮只做一项，每轮创建 Draft PR，每轮完成后停止。
