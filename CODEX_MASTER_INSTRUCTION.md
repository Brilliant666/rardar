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

## 当前行动事件工程轮

PR #4 合并后，下一工程轮默认任务是：

> 建立追加式项目行动事件模型，并修复 Weekly Acted Projects 跨周期漏计。

治理状态：当前行动事件分支正在实现本节目标；只有对应 PR 合并到 `main` 后才视为完成。本节验收与执行清单在合并前继续用于审查当前 PR，合并后不得据此重复创建行动事件实现。

只有以下前置条件全部满足，才允许开始行动事件目标：

- PR #4 已合并；
- 最新 `main` 已包含 audited generations；
- 工作区干净；
- 不存在尚未完成的 generation 修正 PR。

任一条件未满足时，停止并处理现有 PR 或工作区状态，不得提前创建行动事件分支或实现代码。

选择这一任务的原因：

- Weekly Acted Projects 是 Rardar 的北极星指标；
- 当前 `project_actions` 的全生命周期唯一状态会漏计同一项目、同一种行动在后续周期再次发生的事实；
- 指标需要追加式 Event，按钮和当前阶段需要独立 State，二者不能互相替代；
- 幂等键是同时保证网络重试安全与真实跨周期重复行动可计数的必要边界；
- audited generations 合并后，行动事件是长期优先级中第一个尚未完成的目标。

## 下一工程轮验收标准

至少完成：

1. 将历史事件与当前状态分离为 `project_action_events` 与 `project_action_state`，或语义等价的模型。
2. Event 必须是追加式记录，至少包含 device ID、project ID/slug、action、`occurredAt` 和 idempotency key。
3. 同一个项目、同一种行动在不同周再次发生时，必须生成新的有效事件并计入相应周度指标。
4. 网络重试或重复提交必须通过幂等键避免重复计数。
5. 当前按钮状态或最高行动阶段从 State 读取，不得用 State 代替历史事件。
6. Weekly Acted Projects 必须基于近 7 天 Event 计算发生行动的不同项目数，而不是基于全生命周期唯一状态。
7. 迁移已有 `project_actions` 时保留真实已有时间，不补造不存在的历史事件，并提供明确兼容和回滚方案。
8. 行为测试至少覆盖同周重复请求幂等、跨周重复行动重新计数、不同行动阶段、Event 与 State 一致性、迁移、API 并发、近 7 天边界时间，以及推荐和指标读取不回归。
9. 不顺带重做 audited generations，也不修改评分语义、CI、稳定项目 ID、UI 重设计或部署。
10. 运行完整验证，创建 Draft PR，然后停止。

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
feat/action-events
```

### 4. 实现

要求：

- 最小改动；
- 复用现有 D1/SQLite、API 验证和时间处理逻辑；
- 明确 Event、State、指标读取和迁移边界；
- 不引入大型框架；
- 不执行第三方仓库代码；
- 不部署；
- 不修改 main；
- 不自动合并。

### 5. 测试

测试必须验证行为。

至少覆盖：

- 同周相同幂等键重复请求只产生一个 Event；
- 同一项目、同一种行动跨周再次发生时产生新 Event，并在对应周期重新计数；
- 打开、收藏、试用、浅克隆和确认复用等不同行动阶段正确更新 State；
- Event 历史与 State 当前阶段保持一致；
- 已有 `project_actions` 迁移保留真实时间且不补造历史；
- 同幂等键的并发 API 请求只产生一个 Event，并得到确定的 State；
- 近 7 天窗口的包含、排除和时区边界正确；
- 推荐、按钮状态和 Weekly Acted Projects 指标读取不回归。

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
Event 与 State 模型
幂等与并发协议
Weekly Acted Projects 口径
兼容性
测试
安全边界
迁移
回滚
遗留问题
```

完成 Draft PR 后停止，等待审查。

## 当前行动事件 PR 合并后的下一默认任务

只有当前行动事件 PR 已合并、最新 `main` 已包含追加式 Event、独立 State 与按近 7 天 Event 计算的 Weekly Acted Projects、工作区干净且不存在尚未完成的行动事件修正 PR 时，才允许开始：

> 修正评分名称与证据能力不一致的问题，明确区分关注优先级、长期热度、静态工程就绪度、具体任务复用适配度和证据完整度。

建议分支：

```text
fix/scoring-semantics
```

该轮必须重新阅读 `docs/RARDAR_AUDIT_BASELINE.md` 的 P1-4 与 `docs/RARDAR_NORTH_STAR.md` 的评分原则，另建独立 Draft PR；不得在当前行动事件 PR 中提前修改评分、UI 文案或推荐公式。

## 后续迭代规则

每轮应结合最新 `main` 与 `docs/iterations/` 按照文档优先级处理：

1. 数据 Schema 和统一契约——已由 PR #2 完成；
2. audited generations——已由 PR #4（提交 `bf35575`）完成；
3. 追加式行动事件——由当前行动事件分支实现，仅在对应 PR 合并到 `main` 后视为完成；
4. 评分语义——当前行动事件 PR 合并后的第一个未完成项；
5. verify 和 GitHub Actions；
6. 稳定项目 ID。

每轮只做一项，每轮创建 Draft PR，每轮完成后停止。
