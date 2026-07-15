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
- 同一 repository 同时存在 legacy v1 和 stable v2 时，先在内存中把 v1 机械转换为预期 v2 payload；只有转换结果与现有 v2 完全相等，才保留 v2 并清理 v1。任何字段不同都返回 `conflicting_project_artifact_versions`，不按版本、来源时间或文件顺序猜测；analysis/enrichment 的全部项目先完成 preflight，一个项目冲突会使整批 adoption 零写入、零删除；
- 两个不同 repository 映射到同一 legacy slug 时列为 unresolved collision，保留源文件并阻止候选发布或迁移 apply，不按时间、文件顺序或 Catalog 排名猜测。identity v1 的 projectId 会正确区分该碰撞对；但 P1-6C 尚未迁移的路由、React key 与 UI/API 消费者仍以 slug 定位，因此本轮必须继续阻止这类候选进入正式 Catalog，不能让数据层升级把歧义传给旧消费者。

## Flat staging migration

```bash
# 默认完整预检和 dry-run
python -m pipeline.migrate_project_identity --data-dir data

# 仅在人工确认报告后显式写入
python -m pipeline.migrate_project_identity --data-dir data --apply

# 应用代码回滚前，默认 dry-run 降级 flat staging
python -m pipeline.migrate_project_identity --data-dir data --to-legacy-v1

# 仅在人工确认降级报告和备份后显式写入
python -m pipeline.migrate_project_identity --data-dir data --to-legacy-v1 --apply
```

迁移只允许处理 flat `data/analysis` 与 `data/enrichment`。默认方向机械升级可信 v1 artifact；没有可信版本字段的 legacy v0 保留并报告。`--to-legacy-v1` 只把 v2 的 `schemaVersion` 降为 1、移除 `projectIdVersion`/`projectId` 并改用 legacy slug 文件名，其他事实、时间和内容原样保留。工具不读取或修改 `data/current.json`、retained generations、candidates、manifest 或 published pointer，也不发布 generation。

两个方向都先为全部文件完成路径边界、非 symlink/junction、严格 JSON、Schema、repository 映射、目标内容和碰撞 preflight；任何 unresolved、多个 projectId 对应同一 legacy slug，或已有非等价目标都会在写入前使整批失败。apply 先原子写入并验证全部目标，再删除源；等价目标不重写，中断后可验证已写目标并继续清理，源清理中断也可安全重试，完整执行后的第二次 apply 为 no-op。当前开发 worktree 的正式 `data/` 不执行任一 apply，测试只使用临时副本，并核对 current 与 generations 字节不变。

## 回滚

存在 v2 flat staging 时不能只恢复应用代码。回滚顺序固定为：停止写入任务 → 备份 flat staging 与 D1 → 执行 `--to-legacy-v1` dry-run → 显式 `--to-legacy-v1 --apply` → 验证全部 staging 均为旧代码可读取的 v0/v1 → 在 P1-6B 代码仍运行时显式 rollback 到健康的 Catalog v1/v2 retained generation → 在目标 Runtime 的实际 D1 上发起一次预期会执行 adoption 的受控 GET，并核验 `project_identity_runtime` → 运行 Schema/Audit → 停止 Runtime → 回滚应用代码 → 恢复 Runtime。既有 retained generation 不被迁移工具改写；generation rollback 仍必须完整验证目标，不能手改 current 或让网页回退 flat 数据。

## 验证边界

行为测试覆盖 identity 确定性/大小写/碰撞对/非法输入/最大长度、Python 与 Node golden vectors、伪造 projectId、Catalog/Queue/analysis/enrichment 一致性、文件名 mismatch、重复 ID、legacy collision、refresh/derive 门禁、retained v1/v2 兼容；candidate adoption 还覆盖等价清理、任意字段或来源时间不等价时整批零写入/零删除，以及中断重试。双向 migration 覆盖 dry-run/apply/repeat、legacy slug collision、非等价目标、symlink/junction/逃逸、写入与源清理中断、v1 Schema，以及 current/generations 字节不变。完整验收使用 worktree 自有 `.venv` 运行 `npm run verify`，并确认正式 data、Primary Runtime、3000 端口和长期进程均不受影响；实际通过项与远端 CI 状态只在完成执行后记录，不在文档中提前声称。

## 治理状态与下一项

本文件记录的 P1-6A 已由 PR #8、提交 `d41033f` 合并到 `main` 并完成。P1-6 大阶段仍在进行；当前第一个未完成项是独立的 P1-6B D1/Action API 身份工程轮，P1-6C UI/路由兼容只能在 P1-6B 合并后开始。任何单项完成都不能提前把整个 P1-6 标记完成。

## 是否影响 North Star

不改变 Weekly Acted Projects 的定义或数值。P1-6A 已消除 JSON 数据层的项目身份歧义；周指标从 `project_slug` 迁移到 `projectId` 由当前 P1-6B 独立工程轮交付。
