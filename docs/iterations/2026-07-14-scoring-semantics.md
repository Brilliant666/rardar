# 2026-07-14 · Evidence-bounded scoring semantics

## 目标

修正旧 `globalScore`、`reuseScore` 的名称与证据能力不一致问题，把“值得关注”“长期有热度”“静态工程材料就绪”“适合具体任务”和“证据覆盖”拆成可独立解释、可审计且允许未知的维度。没有任务上下文或运行证据时，不再生成确定性的直接复用结论。

## 本分支实现

- Catalog 升级为 `schemaVersion: 2` 与固定 `scoreModelVersion: evidence-v2`；
- 项目改为发布 `attentionScore`、`enduranceScore`、`engineeringReadiness`、`reuseFitScore` 与 `evidenceCompleteness`；
- 每项分数绑定同值的结构化 `scoreExplanations`，分别列出事实、代理、限制与升级条件；
- Engineering Readiness 只来自与仓库当前推送匹配的只读静态分析，没有当前证据时为 `null`，并明确不等于运行可靠性；
- 通用 Catalog 没有用户的任务、约束和验收标准，因此 Reuse Fit 始终为 `null`，中文能力画像只保留为“适用场景假设”；
- Evidence Completeness 只衡量 GitHub 事实、精确增长、当前静态证据、版本绑定画像和多周期证据的覆盖，不参与质量断言；
- 推荐只允许“了解 / 收藏 / 隔离试用 / 观望”，不会输出“直接复用”；隔离试用要求当前静态证据、工程就绪阈值、关注阈值、GitHub API 许可证且未触发风险关键词；
- 每日五项仍保持近期动量与长期高热 3 + 2 平衡，排序主干改用 Attention，再在存在静态证据时参考 Engineering Readiness；
- v2 generation audit 使用生产构建器从同一代事实与证据重算完整有序项目列表，分数、说明、推荐或顺序任一篡改都会失败；
- 网页在唯一服务端入口归一化 v1/v2，所有页面和推荐 API 使用统一的新语义；任务搜索分显示为规则匹配 `/100`，不再冒充复用概率。

## 证据与上限

```text
Attention
  = max(近期动量, Endurance × 0.92)
  风险关键词触发时上限 49

Engineering Readiness
  = README + LICENSE 文件 + tests + CI + docs + examples
    + package manifest + dependency lock + 测试文件深度
  仅在当前只读静态证据存在时计算
  风险关键词触发时上限 35

Reuse Fit
  = null
  直到存在具体任务、约束、验收标准和隔离验证

Evidence Completeness
  = GitHub 事实 25
    + 精确区间增长 15（首次速度代理仅 5）
    + 当前静态证据 30
    + 当前中文画像 20
    + 多周期持续性证据 10
```

这些分值不改变 GitHub 事实，也不把缺失证据当成零质量；`null` 表示尚无资格评分。

## v1 兼容与 generation 迁移

- `catalog.schema.json` 严格同时支持不可变 v1 与新 v2，未知版本和新旧字段混用都失败；
- 历史 v1 generation 继续使用原有 Schema 字段和原有 audit 行为，不新增 warning，ready manifest 的审计摘要保持可验证；
- 网页读取 v1 时只将旧 `globalScore` 映射为 Attention、保留 Endurance；Engineering Readiness、Reuse Fit 与 Evidence Completeness 均为未知；
- 旧 `reuseScore` 不会静默升级成 Engineering Readiness，旧“试用 / 复用”建议保守降为“隔离试用”；
- 当前事实通过 `data:derive` 生成独立 v2 generation；snapshot 与 history 哈希保持不变，旧 v1 generation 继续保留以供显式回滚；
- v2 候选仍必须经过 Schema、跨文件 audit、manifest/hash 与原子 current pointer 发布协议。

## 行为验证

- 构建器：静态证据当前性、未知边界、解释与分数一致、许可证/风险推荐门槛、长期与近期平衡；
- Schema：v1/v2 有效样例、未知版本、双向混合字段、空值边界、说明字段缺失/额外/错误类型；
- Audit：v2 健康重建，以及分数、说明、推荐和排序四种篡改；v1 不触发新版语义错误；
- Web 归一化：v1 保守兼容、v2 原样语义、未知版本 fail closed、个性化基础分不使用 Reuse Fit；
- 真实 HTTP：新标签、推荐 API、generation 健康读取与原有 D1 行动/反馈路径不回归；同一 Vinext 进程可显式回滚到保留的 v1 generation，以保守语义渲染后再切回 v2；

## 完整验证结果

- 运行时：以下 Node 命令均显式使用仓库要求的 Node.js `22.13.1`；
- `npm run lint`：通过，0 warning；
- `python -m unittest discover -s pipeline -p "test_*.py"`：共运行 181 项，177 项通过，4 项因 Windows 符号链接权限条件跳过；
- `npm run data:validate`：当前 generation 的 21 份产物通过，0 error；
- `npm run data:audit`：generation `20260713T191537075729Z-4e2e9d09fae2` 为 healthy，0 error、0 warning；
- `npm run build`：通过；
- `npm test`：17/17 通过，真实 Vinext HTTP 返回首页、健康端点、动态页和搜索页 200；同一进程回滚到保留的 v1 generation `20260711T183728486430Z-72ff8eefea0d` 后，首页以新标签和保守建议返回 200，并可切回 v2；损坏 current 时健康端点 503、首页 500，原进程 rollback 后恢复 200；D1 行动 API 200，8 个并发同键请求只生成 1 个 Event；
- `npm run security:audit:prod`：0 个已知生产依赖漏洞；
- 运行中的 `http://127.0.0.1:3000/` 已读取上述 v2 generation：首页、新搜索语义、推荐 API、项目详情五类说明和 D1 actions 均返回 200；
- v2 derive 前后的 snapshot latest、history、signals latest 与 signal enrichment SHA-256 完全一致，证明未采集新事实或推进增长基线。

## 安全边界与遗留风险

- 本轮不执行、安装、构建或测试第三方仓库代码；
- 本轮不引入用户私有任务上下文，Reuse Fit 保持未知；
- 静态工程就绪度不能证明安装成功、测试通过或运行安全；
- 许可证 API 结果与风险关键词只是隔离试用的最低门槛，不能代替人工核验；
- 个性化仍来自匿名本地设备反馈，不改变事实和风险主干；
- 稳定项目 ID、统一 verify/CI、新信源与 UI 重设计属于后续独立目标。

## 是否影响 North Star

不改变 Weekly Acted Projects 的定义或 Event 来源。本轮减少错误的强复用建议，使“试用 / 浅克隆 / 确认复用”更接近有证据的真实开发者行动；指标仍只从近 7 天追加式 Event 计算。

## 治理状态与下一项

本文件记录当前评分语义分支的实现与验证；只有对应 PR 合并到 `main` 后，P1-4 才视为完成。合并前不得开始 verify/CI 或其他下一目标。合并后按长期优先级重新检查，第一个候选未完成项是统一 `verify` 与 GitHub Actions；本轮不会实施。
