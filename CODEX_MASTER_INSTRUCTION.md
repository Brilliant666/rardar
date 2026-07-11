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

## 下一工程轮默认任务

本治理文档 PR 合并后，优先处理：

> 建立 generation 的候选生成、验证、审计和原子发布协议。只有审计通过的 generation 才能成为网页数据源和后续增长基线；任何失败都必须保留上一代健康数据。

选择这一任务的原因：

- 核心 JSON Schema 和统一验证入口已经由 PR #2 建立；
- 当前流水线仍可能先覆盖正式数据，再执行一致性审计；
- 审计失败的数据一旦被页面或下一轮增长计算读取，就会污染事实主干；
- generation 发布边界是长期自动运行、可回滚和可信推荐的共同前提；
- 该目标可以作为一个有边界、可独立测试和可回滚的 PR 完成。

## 下一工程轮验收标准

至少完成：

1. 定义并版本化 generation 目录、manifest 和当前 generation 指针的契约。
2. 在不改变当前正式 generation 的前提下生成完整候选数据。
3. 发布前依次对候选 generation 执行 Schema 验证和跨文件一致性审计。
4. 仅在全部检查通过后原子更新当前 generation 指针。
5. 页面数据源与后续增长基线必须读取同一个已发布 generation。
6. Schema 失败、审计失败、写入中断或发布冲突时，上一代健康数据和增长基线保持不变。
7. 提供并发保护、可诊断失败状态和明确回滚路径，不补造历史事实。
8. 增加行为测试，至少覆盖成功发布、Schema 失败、审计失败、中断写入、并发发布和旧 generation 保留。
9. 提供对现有正式数据的兼容或迁移说明，并更新相关架构和数据模型文档。
10. 运行完整验证，创建 Draft PR，然后停止；不得部署、合并或顺带处理行动事件、评分、CI、项目 ID 或 UI。

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
npm run data:audit
npm run build
npm test
npm run security:audit:prod
```

准确记录通过、失败和未运行项。

### 3. 创建分支

建议：

```text
feat/data-contracts
```

### 4. 实现

要求：

- 最小改动；
- 复用现有审计和 JSON 读写逻辑；
- 不引入大型框架；
- 不执行第三方仓库代码；
- 不部署；
- 不修改 main；
- 不自动合并。

### 5. 测试

测试必须验证行为。

至少覆盖：

- 合法 enrichment；
- capabilities 为字符串而不是数组；
- taskTerms 含非字符串；
- 非法 URL；
- 无效时间；
- repository 不匹配；
- 未知 schemaVersion；
- catalog 嵌套字段错误；
- audit 对 Schema 错误的报告。

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
Schema 设计
兼容性
测试
安全边界
迁移
回滚
遗留问题
```

完成 Draft PR 后停止，等待审查。

## 后续迭代规则

本 PR 合并后，下一轮应按照文档优先级处理：

1. 审计通过后发布 generation；
2. 追加式行动事件；
3. 评分语义；
4. verify 和 GitHub Actions；
5. 稳定项目 ID。

每轮只做一项，每轮创建 Draft PR，每轮完成后停止。
