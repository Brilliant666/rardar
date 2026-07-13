# Rardar Audit Baseline

> 文档角色：保存提交 `fa2e064` 时点的历史仓库审查结论，作为后续重构和长期迭代的事实基线。
> 仓库：`Brilliant666/rardar`
> 审查基线：当时的 `main`，核心提交 `fa2e064`
> 审查方式：仓库级静态审查；未执行第三方候选仓库代码。
> 文档状态：Baseline v1

> 历史快照说明：本文记录的是提交 `fa2e064` 时点的原始审查结论，不是最新 `main` 的实时问题清单。
> 已经完成的问题不能继续解释为当前待办；当前完成状态以本文下方状态表和最新 `main` 为准。

---

## 当前完成状态

| 原始 P1 项 | 当前状态 | 完成依据或下一步 |
| --- | --- | --- |
| P1-1 审计通过后发布 generation | 由 PR #4 实现；仅在 PR #4 合并到 main 后视为完成 | 合并前不得开始下一工程目标 |
| P1-2 严格数据 Schema | 已完成 | PR #2，提交 `048a2d9` |
| P1-3 行动事件 | PR #4 合并后的下一工程目标 | 合并后按 `CODEX_MASTER_INSTRUCTION.md` 开始独立工程轮 |
| P1-4 评分语义 | 待处理 | 尚未开始独立工程轮 |
| P1-5 verify/CI | 待处理 | 尚未开始独立工程轮 |
| P1-6 稳定项目 ID | 待处理 | 尚未开始独立工程轮 |

PR #4 合并前不得开始 P1-3 行动事件或其他后续工程目标。

以下章节保留 `fa2e064` 时点的原始审查内容，不重写历史事实。

---

## 1. 执行摘要

Rardar 已经不是普通的 GitHub Trending 页面，而是在形成一套面向个人开发者的开源软件情报与项目复用决策系统。

当前核心链路是：

```text
公开信源
→ GitHub 事实快照
→ 有边界的只读静态分析
→ 可解释评分
→ Codex 深读队列
→ 中文能力画像
→ 用户反馈与真实行动
→ 项目复用决策
```

项目最突出的优点是：

1. 事实与 AI 判断分层保存。
2. 第三方仓库默认只读，不安装依赖、不执行代码。
3. 首次观察与真实区间增长明确区分。
4. 有跨进程数据锁、批量原子替换和失败回滚。
5. Codex 不是自由浏览，而是接受带输入证据、输出字段和安全边界的任务队列。
6. 已经将“试用、浅克隆、确认复用”等真实工程行动与普通反馈分开。

项目当前最大风险不是“功能不足”，而是现有功能复杂度已经超过早期原型阶段，数据契约、发布原子性、指标语义和仓库级持续验证尚未完全跟上。

当前没有发现必须立即停用项目的 P0 安全问题。建议暂停继续堆叠更多信源和复杂 AI 能力，先完成可信数据基线。

---

## 2. 当前产品定位

### 2.1 当前定位

Rardar 用于帮助开发者回答：

- 最近真正发生了什么？
- 某项功能是否已经有成熟项目实现？
- 一个项目是否值得阅读、试用、浅克隆或复用？
- 项目的工程完整度、许可证和安全风险是什么？

### 2.2 建议的长期定位

> 个人开发者的开源软件情报、能力发现和复用决策中枢。

Rardar 的最终价值不应是榜单浏览量，而是帮助用户减少重复开发，并促成可验证的工程行动。

### 2.3 Rardar 与其他项目的关系

```text
Rardar
  ├── 发现候选能力
  ├── 形成静态证据和复用假设
  ├── 生成 Codex 深读或验证任务
  │
  ├── platform_lab
  │     └── 在隔离环境验证候选能力
  │
  ├── short-video-agent
  │     └── 接入已经验证的垂直模块
  │
  └── 其他个人项目
        └── 接收复用建议和迁移计划
```

---

## 3. 当前架构概览

```text
GitHub Search API ──────┐
                       ├─→ Python collectors
Official RSS ──────────┤
Community signals ─────┘
                               │
                               ▼
                    data/snapshots + signals
                               │
                               ▼
                    read-only static analysis
                               │
                               ▼
                       build_catalog.py
                               │
               ┌───────────────┴───────────────┐
               ▼                               ▼
        Next.js / React UI              data/queues/codex.json
                                               │
                                               ▼
                                       Codex enrichment
                                               │
                                               ▼
                                      data/enrichment/
                                               │
                                               └─→ data:derive
```

用户决策层：

```text
浏览器匿名设备 ID
→ feedback 当前状态
→ decision_events 历史反馈事件
→ project_actions 工程行动
→ 个性化排序和近 7 天北极星指标
```

---

## 4. 已验证的工程优势

### 4.1 第三方仓库安全边界

静态分析器已经实现：

- 不执行候选仓库代码；
- 不安装候选仓库依赖；
- Git 浅克隆隔离全局和系统 Git 配置；
- 限制扫描文件数量；
- 限制单文件读取大小；
- 限制源码归档下载体积；
- 限制归档文件数量和解压总量；
- 跳过符号链接；
- 防止 ZIP 路径穿越；
- 浅克隆失败时退化到有边界的 GitHub 官方源码归档。

这部分应视为不可退让的安全基线。

### 4.2 事实增长口径

第一次采集只使用“创建以来 Star/日代理”，后续快照才使用精确 Star 差值和观察窗口。

这避免了将单次快照伪装成 24 小时增长，符合证据优先原则。

### 4.3 数据写入保护

当前已经拥有：

- 跨进程数据目录锁；
- JSON 临时文件写入；
- 多文件批量替换；
- 旧数据备份；
- 中途失败回滚；
- 非有限数值拒绝；
- 历史快照和当前基线分离。

### 4.4 Codex 队列

队列为每个任务保存：

- 任务 ID；
- 任务类型；
- 优先级；
- 进入队列原因；
- 当前证据状态；
- 输入路径；
- 输出路径；
- 必填字段；
- 安全要求；
- 源仓库更新时间；
- 旧画像分析时间。

这是 Rardar 与普通热点列表之间最重要的区别。

### 4.5 数据审计

现有 `audit_data.py` 已覆盖：

- JSON 结构；
- 时间一致性；
- 快照、目录和队列数量；
- 仓库名和 slug 唯一性；
- Star 值一致性；
- GitHub 查询覆盖状态；
- 信号源健康；
- 信号时间窗口和分数；
- URL 安全；
- Daily Five 双赛道平衡；
- 历史持续性；
- Codex 队列重建一致性；
- 精确增长差值。

---

## 5. P1 问题清单

## P1-1：正式数据先发布，之后才审计

### 当前行为

当前调度流程近似为：

```text
refresh()
→ 替换正式 snapshot/catalog/signals/queue
→ audit_data()
→ 审计失败则将调度状态标记为 failed
```

### 风险

如果业务规则或生成逻辑产生错误，但 JSON 本身可写：

- 页面可能先读取错误数据；
- 审计失败不会自动恢复上一代健康数据；
- 新快照可能已经成为下一轮增长基线；
- 重试可能在错误基线上继续计算。

### 目标方案

引入 generation：

```text
data/generations/<generation-id>/
  snapshots/
  catalog/
  signals/
  queues/
  manifest.json
```

流程：

```text
生成候选 generation
→ Schema 校验
→ 一致性审计
→ 全部通过
→ 原子更新 current generation 指针
→ 归档上一代
```

失败 generation 不得成为网页数据源或增长基线。

---

## P1-2：缺少严格的数据 Schema

### 当前行为

Codex enrichment 主要验证字段是否存在且不为空。

### 风险

无法阻止：

- 数组字段被输出成字符串；
- 非法 URL；
- 无效时间；
- repository 与文件名不匹配；
- 过长文本；
- 错误枚举；
- 字段含义漂移。

### 目标方案

建立 `contracts/`：

```text
contracts/
  github-snapshot.schema.json
  technical-signals.schema.json
  static-evidence.schema.json
  project-enrichment.schema.json
  signal-enrichment.schema.json
  catalog.schema.json
  codex-queue.schema.json
  runtime-status.schema.json
```

所有写入路径必须先验证。

项目画像还应绑定：

```json
{
  "schemaVersion": 2,
  "repository": "owner/repo",
  "sourceCommitSha": "...",
  "sourcePushedAt": "...",
  "analyzedAt": "..."
}
```

---

## P1-3：近 7 天真实行动指标漏计跨周期重复行动

### 当前行为

`project_actions` 对以下组合设置全生命周期唯一约束：

```text
device_id + project_slug + action
```

接口使用 `onConflictDoNothing()`。

### 风险

用户一个月前试用过项目，本周再次试用时不会生成新记录，但指标查询的是近 7 天事件，因此本周行动会被漏计。

### 目标方案

分成两个模型：

```text
project_action_events
  id
  device_id
  project_slug
  action
  occurred_at
  idempotency_key
```

```text
project_action_state
  device_id
  project_slug
  highest_stage
  updated_at
```

- Event 用于历史和周度指标。
- State 用于按钮状态和当前阶段。
- 客户端提交需要幂等键，避免网络重试造成重复事件。

---

## P1-4：评分名称超过其证据能力

### 当前问题

当前 `globalScore` 更接近“关注优先级”，主要依据增长、新鲜度、维护、Star、Fork 和关键词。

当前 `reuseScore` 更接近“静态工程证据完整度”，主要依据 README、LICENSE、测试、CI、文档、示例、依赖锁等。

但前端显示为：

- 全球影响；
- 复用价值；
- 建议复用。

这可能让用户误以为已经验证技术兼容性、接入成本和运行可靠性。

### 目标方案

拆分为：

```text
attentionScore
enduranceScore
engineeringReadiness
reuseFitScore
evidenceCompleteness
```

其中：

- `engineeringReadiness` 不能宣称实际运行可靠。
- `reuseFitScore` 需要具体任务或用户项目上下文。
- 未实际验证的项目不能仅凭静态文件直接得到强“复用”结论。

---

## P1-5：缺少仓库级统一验证和 CI

### 当前问题

Python 流水线测试较丰富，但默认 `npm test` 没有包含 Python 测试。

Node 测试主要读取源码并进行正则断言，不能替代 API、数据库和浏览器行为测试。

仓库当前没有稳定的 GitHub Actions 验证记录。

### 目标方案

建立：

```bash
npm run verify
```

至少执行：

```bash
npm run lint
python -m unittest discover -s pipeline -p "test_*.py"
npm run data:audit
npm run build
node --test tests/rendered-html.test.mjs
npm run security:audit:prod
```

新增 GitHub Actions：

- Ubuntu；
- Windows；
- Node.js 22；
- Python 3.11/3.12；
- 不访问真实外部服务的单元测试；
- 可手动触发的外部集成测试。

---

## P1-6：slug 和证据文件名存在碰撞可能

### 当前问题

仓库名经正则转换后：

```text
owner/foo.bar
owner/foo-bar
```

可能映射成相同 slug 或文件名。

### 风险

- 静态分析报告被覆盖；
- Codex 画像被覆盖；
- 页面路由冲突；
- 用户反馈关联错误。

### 目标方案

采用稳定且抗碰撞的 ID：

```text
owner--repo--<sha256(owner/repo) 前 8 位>
```

或使用可逆编码。

---

## 6. P2 改进项

### 6.1 历史数据无限增长

最近持续性只使用最多 30 次快照，但历史目录会持续增长。

建议：

- 保留最近 30 份完整快照；
- 更早数据按周或月聚合；
- 或将时间序列迁入 SQLite；
- Git 仓库只保存必要数据和样例。

### 6.2 英文短关键词误命中

关键词 `"ai"` 使用简单子串匹配，可能命中 `available`、`maintainer` 等词。

建议英文采用单词边界或 token，中文保持短语匹配。

### 6.3 第三方榜单解析失败可能被误标健康

固定 Markdown 格式变化后，可能得到 0 条内容但仍显示 healthy。

建议状态扩展为：

```text
healthy
empty
schema_changed
failed
```

### 6.4 无强匹配时仍显示“优先匹配”

找项目页面在没有直接命中时会回退到基础排序，但 UI 仍可能暗示成功匹配。

应明确显示：

> 未找到强匹配，以下为可能相关的高质量候选。

### 6.5 本地运行管理器缺少指数退避

进程持续崩溃时固定短时间重启，可能形成无限重启循环。

建议：

```text
2s → 5s → 15s → 30s → 60s
```

连续失败超过阈值后进入 blocked。

### 6.6 环境与开源治理文档不完整

README 只明确 Node.js 版本，但实际依赖 Python、Git 和平台能力。

公开仓库当前还应补充：

```text
LICENSE
CONTRIBUTING.md
SECURITY.md
ARCHITECTURE.md
DATA_MODEL.md
```

---

## 7. 测试审查结论

### 优点

Python 测试已覆盖多项重要失败路径：

- 文件替换失败回滚；
- 非标准 JSON；
- 多快照增长；
- 静态证据过期；
- 风险项目降权；
- 信号非法 URL；
- 信号时间窗口；
- 调度补跑；
- 数据目录锁。

### 不足

- 前端 API 缺少真正的请求级测试；
- D1 触发器和迁移缺少行为测试；
- 个性化算法缺少独立测试套件；
- 行动指标缺少跨周测试；
- Node 测试大量依赖源码文本正则；
- 没有仓库级 CI 作为合并门槛。

---

## 8. 建议实施顺序

建议按独立 PR 实施：

1. **数据契约和 Schema**
2. **审计通过后发布 generation**
3. **追加式真实行动事件**
4. **评分语义修正**
5. **统一 verify 和 GitHub Actions**
6. **稳定项目标识**
7. **环境、许可证和治理文档**
8. **能力检测升级**
9. **任务上下文和个人项目适配**
10. **隔离验证闭环**

每个 PR 必须：

- 目标单一；
- 可测试；
- 可回滚；
- 更新相关文档；
- 默认创建 Draft PR；
- 不直接合并 main。

---

## 9. 结论

Rardar 值得继续作为长期基础设施项目。

项目当前最应坚持的顺序是：

```text
数据正确性
> 安全边界
> 可解释性
> 可恢复性
> 自动测试
> 用户体验
> 开发速度
> 技术新颖性
```

完成 P1 后，Rardar 才适合继续扩展：

- 更深的代码能力识别；
- 任务到项目匹配；
- 用户现有项目上下文；
- platform_lab 隔离验证；
- 实际复用结果回流。
