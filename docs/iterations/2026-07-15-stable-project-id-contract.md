# 2026-07-15 · Stable Project ID contract and data adoption

## 目标

完成 P1-6 Stable IDs 的第一项独立迭代 P1-6A：用单一、版本化且抗旧 slug 碰撞的 Stable Project ID 统一新 Catalog、项目静态证据、项目画像、Codex Queue 和 generation audit。P1-6A 只覆盖 JSON 身份契约与数据层；其合并不代表整个 P1-6 完成。

## Identity v1 正式算法

身份输入是严格合法的 GitHub `owner/repo`，不接受自动 trim、URL、额外斜杠、空段、反斜杠、路径穿越、控制字符或 Unicode 近似字符：

- owner：1–39 个 ASCII 字母/数字，允许非首尾且不连续的 `-`；
- repository name：1–100 个 ASCII 字母、数字、`.`、`_` 或 `-`，但不能为 `.` 或 `..`；
- 原始大小写 repository 继续用于来源绑定和展示；身份计算使用完整 repository 的 ASCII 小写形式；
- 可读前缀把规范化 repository 中连续的非 `[a-z0-9]` 替换为 `-`、去除首尾 `-`、截断到 64 字符并再次去除尾部 `-`；
- 对规范化 repository 的 UTF-8 字节计算 SHA-256，取前 20 个小写十六进制字符；
- 最终 ID 为 `<prefix>--<digest>`，只含 `[a-z0-9-]`，最大长度 86。

20 个 hex 字符提供 80 bit 摘要；即使有一百万个不同 repository，生日碰撞概率也约为 `4.1 × 10^-13`。摘要长度降低碰撞风险，但不替代门禁：任何实际 projectId 重复、规范化 repository 重复或重算不一致都必须使构建和 audit 失败。

`owner/foo.bar`、`owner/foo-bar`、`owner/foo_bar` 和 `owner/foo--bar` 的可读前缀可以相同，但摘要不同，因此不会互相覆盖。`Owner/Repo` 与 `owner/repo` 得到同一 ID。owner 转移或 repository 改名在 v1 中产生新身份，不伪造 rename 连续性；只有大小写变化时身份保持不变。

`contracts/project-identity-v1.vectors.json` 是跨语言 golden vectors，覆盖碰撞对、大小写、最短/接近最大长度以及非法路径和字符。Python 是正式算法实现；Node 测试读取同一 vectors，不维护第二套自由解释。

## Artifact 版本

| Artifact | 新版本 | P1-6A 身份语义 |
| --- | --- | --- |
| Catalog | v3 | 顶层 `projectIdVersion: 1`；项目保存 `projectIdVersion` 与可重算的唯一 `projectId`；`slug` 仅为 legacy/display |
| Static evidence | v2 | payload 绑定 repository/projectId，文件名固定为 `<projectId>.json` |
| Project enrichment | v2 | payload 绑定 repository/projectId 与两个来源时间，文件名固定为 `<projectId>.json` |
| Codex Queue | v2 | 项目 task ID、项目字段、`inputPaths` 和 `outputPath` 全部绑定 projectId |

Catalog v3 必须与 Queue v2 配对。refresh 和 derive 使用同一身份规则；Schema、audit 和写入边界共同核对 Catalog、analysis、enrichment、queue 的 repository、projectId 与文件名/路径。碰撞或 unresolved legacy 映射使候选 generation 发布失败，CAS、manifest/hash、原子 pointer 与 rollback 协议不变。

## 迁移矩阵

| 分组 | 本轮边界 |
| --- | --- |
| P1-6A 本轮必须迁移 | identity v1 与 golden vectors；Catalog v3；static evidence v2；project enrichment v2；Queue v2；refresh/derive/Schema/audit 身份门禁；flat staging migration |
| P1-6B 后续 D1/API | `project_action_events`、`project_action_state`、legacy `project_actions`、feedback/recommendation/action API 的 `project_slug` 到 `projectId` 迁移、幂等与周指标兼容 |
| P1-6C 后续 UI/路由 | 项目详情 route、链接、React key、按钮/观察列表身份、旧 slug URL redirect |
| 保留的 legacy 入口 | retained Catalog v1/v2、Queue v1、static evidence v0/v1、project enrichment v0/v1；现有 D1/API/UI slug 消费边界 |

P1-6A 不修改 D1、Action API、Weekly Acted Projects、页面状态或路由。新 JSON 产物不再让 slug 决定归属，但 legacy slug 在 P1-6B/P1-6C 完成前继续服务旧边界。

## Legacy 读取与 unresolved 策略

- 不修改 retained generation 的任何字节；Catalog v1/v2、Queue v1 和旧项目产物继续按原 Schema 严格验证、审计、读取和显式 rollback；
- 未知 artifact 版本、Catalog/Queue 版本错配或新旧字段混用 fail closed；
- legacy 文件必须读取 payload 中的 repository 后核对归属，不能只相信文件名；
- 同一 repository 同时存在 legacy 和新文件时，只在新文件通过 v2 Schema、identity 与来源当前性门禁后选择新版本；内容等价的迁移目标不会被重写，apply 只继续安全清理中断遗留源，内容或归属冲突为 error；
- 两个不同 repository 映射到同一 legacy slug 时列为 unresolved collision，保留源文件并阻止候选发布或迁移 apply，不按时间、文件顺序或 Catalog 排名猜测。identity v1 的 projectId 会正确区分该碰撞对；但 P1-6C 尚未迁移的路由、React key 与 UI/API 消费者仍以 slug 定位，因此本轮必须继续阻止这类候选进入正式 Catalog，不能让数据层升级把歧义传给旧消费者。

## Flat staging migration

```bash
# 默认完整预检和 dry-run
python -m pipeline.migrate_project_identity --data-dir data

# 仅在人工确认报告后显式写入
python -m pipeline.migrate_project_identity --data-dir data --apply
```

迁移只允许处理 flat `data/analysis` 与 `data/enrichment` 中可机械升级的 v1 artifact；没有可信版本字段的 legacy v0 保留并报告。工具不访问或修改 `data/current.json`、`data/generations/`、manifest 或 published pointer，也不发布 generation。完整 preflight 先验证路径边界、非链接文件、严格 JSON、旧 Schema 与 repository 映射，再计算全部目标；任何 unresolved 或冲突都会在写入前失败。apply 使用同目录原子写入，不重写已存在的等价目标；中断后可验证目标并继续源清理，正常完成后再次 apply 为 no-op。当前开发 worktree 的正式 `data/` 不执行 apply，测试只使用临时副本。

## 回滚

代码回滚只需恢复本轮提交；不执行破坏性数据回迁。既有 retained generation 没有被重写，仍可通过现有显式 rollback 完整验证后切换。已经生成的 Catalog v3 generation 在旧代码无法识别时会 fail closed，操作员应显式回滚到健康的 Catalog v1/v2 retained generation，而不是改写 current 或从 flat 数据回退。

## 验证边界

行为测试覆盖 identity 确定性/大小写/碰撞对/非法输入/最大长度、Python 与 Node golden vectors、伪造 projectId、Catalog/Queue/analysis/enrichment 一致性、文件名 mismatch、重复 ID、legacy collision、refresh/derive 门禁、retained v1/v2 兼容，以及 migration dry-run/apply/no-op/冲突/链接/中断重试/unresolved。完整验收使用 worktree 自有 `.venv` 运行 `npm run verify`，并确认正式 data、Primary Runtime、3000 端口和长期进程均不受影响；实际通过项与远端 CI 状态只在完成执行后记录，不在文档中提前声称。

## 治理状态与下一项

PR #7 已通过提交 `3430e30` 完成 P1-5 Verify/CI。当前 P1-6 大阶段进行中，本轮只交付 P1-6A，且只有对应 PR 合并到 `main` 后 P1-6A 才视为完成。合并前不得开始 P1-6B；合并后第一个未完成项是 P1-6B D1/Action API，P1-6C UI/路由兼容在其后。任何单项完成都不能提前把整个 P1-6 标记完成。

## 是否影响 North Star

不改变 Weekly Acted Projects 的定义或数值。P1-6A 先消除 JSON 数据层的项目身份歧义；周指标从 `project_slug` 迁移到 `projectId` 属于 P1-6B。
