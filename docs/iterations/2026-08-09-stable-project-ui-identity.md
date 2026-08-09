# 2026-08-09 P1-6C1 Client Stable Project Identity

## 本轮唯一目标

让 Rardar 的页面路由、链接、组件交互、个性化关联和浏览器本地状态使用 P1-6A/P1-6B 已建立的 Stable Project ID，并为旧 slug URL 提供严格、无猜测的兼容入口。

本轮分支：

```text
feat/stable-project-ui-identity
```

本轮基线是 PR #9 的 Squash merge 提交 `c24b7d6`。P1-6C1 只有在对应 Draft PR 合并到 `main` 后才视为完成；本文件记录分支上的实现协议和验证边界，不预先声称 `main` 已经完成客户端迁移。

## 已满足前置条件

- P1-6A JSON identity 契约已由 PR #8、提交 `d41033f` 完成；
- P1-6B D1/API Stable Identity 已由 PR #9、提交 `c24b7d6` 完成，`main` Verify 通过；
- Primary Runtime 的正式 D1 adoption、完整重启和重复只读 adoption no-op 已通过；
- 正式 Historical Identity Bundle 验证 6 个 ready generation、180 条 mapping、30 个 current 项目和 60 个历史 distinct projectId；
- `oomol-lab/open-connector` retained witness 与 `officecli` exact quarantine 均通过；
- current generation 仍为 `20260809T091719453761Z-69c6385c7279`，21 个历史 failed candidate 未清理；
- 本轮从最新 `main` 建立独立分支，不修改或停止 Primary Runtime。

## Generation-bound 页面身份

网页服务端入口必须从一次请求已经取得的同一个 verified published bundle 构造 identity context：

- Catalog v1/v2 从服务端 Catalog `repo` 机械派生 identity v1；
- Catalog v3 从 repository 重算 projectId，并精确核对 `projectIdVersion: 1` 与发布值；
- 页面一次请求不得分别读取两次 current，也不得混用两个 generation；
- current pointer 原子切换后，下一请求必须读取新 generation；
- 已退出 current Catalog 的历史 projectId 保留在 D1/历史事实中，但详情页必须 fail closed。

UI 不自行实现 slug hash、repository identity 或另一套 TypeScript 算法；正式 resolver 和 golden vectors 仍是唯一身份来源。

## Canonical 与 legacy 路由

canonical 详情 URL 固定为：

```text
/project/v1/<projectId>
```

版本必须精确为 `v1`。projectId 必须形状合法、存在于本次 current Catalog，并与 repository 重算一致。错误版本、畸形、伪造、未知或 retired ID 均返回 `404`；不得回退 flat staging、retained generation 或 legacy slug。

旧 URL：

```text
/projects/<slug>
```

只作为同一次 identity context 内的兼容 resolver：

- 唯一匹配：`302` 到 canonical URL，并设置 `Cache-Control: no-store`；
- 未知：`404`；
- 歧义：`409`；
- 不选择第一项，不按排序、时间、D1 历史映射或文件顺序猜测目标。

redirect 不得被当作 canonical identity，也不得缓存到跨 generation 失效。

## 客户端迁移矩阵

以下项目级边界统一使用 `projectIdVersion: 1` 与 projectId：

| 边界 | Stable identity 行为 |
| --- | --- |
| 项目详情链接 | 使用 canonical `/project/v1/<projectId>` |
| React key | 使用 projectId，不使用 slug |
| Action 按钮与 tracked repository | props、pending key、GET/POST payload 都使用 stable pair |
| Feedback | 当前状态、提交 payload 和客户端事件都使用 stable pair；提交单飞，成功 mutation 会使更早的状态 GET 失效 |
| Recommendations | API 返回它实际读取的 generationId；客户端只接纳与页面 generation 精确一致的响应，并按 projectId 关联项目 |
| Watch/local state | 状态 map、筛选和项目 key 使用 projectId |
| repository / slug | 只用于可读展示和 legacy URL 输入，不作为事实主键 |

两个项目即使暴露相同显示 slug，也不得在 recommendation、watch、反馈、行动或 React reconciliation 中串联。

## 兼容与安全边界

- 不修改 `drizzle/0004_stable_project_identity.sql`、D1 schema、adoption 或已完成的迁移事实；
- 不删除 API 的 legacy slug selector；旧客户端仍由 P1-6B 既有 verified Catalog resolver 保护；
- 不放宽 current/retained projectId ↔ repository 与 legacy slug collision guard；
- 不让 UI 信任客户端 repository、发生时间或未经 Catalog 验证的 ID；
- 不缓存上一份健康 generation，current 损坏时页面继续 fail closed；
- 不执行候选仓库代码，不访问 Primary Runtime D1/data，不占用 3000，不部署。

## 行为测试矩阵

至少覆盖：

1. Catalog v1、v2 从 `repo` 机械派生 UI identity，Catalog v3 对发布 identity 重算核对；
2. canonical 详情 SSR 成功，并在 HTML 中绑定当前 generation；
3. 错误版本、畸形/伪造/未知 ID、路径编码和 retired project 均 fail closed；
4. legacy slug 唯一 `302 no-store`、未知 `404`、歧义 `409`；
5. 两个相同显示 slug、不同 projectId 的 recommendation/watch/feedback/action 状态隔离；
6. Action/feedback 客户端请求只发送 stable pair，不以 slug 作为 canonical selector；
7. pointer 切换后不重启 Vinext 即读到新 generation，单个响应不混代；
8. Catalog v1 retained rollback 后 canonical 页面和 legacy redirect 仍可保守读取；
9. current 损坏时页面和健康端点 fail closed，rollback 后原进程恢复；
10. 现有 Action/feedback/recommendation/metrics D1 HTTP、Weekly metric、Schema/Audit 和 Primary Runtime 隔离门禁不回归。

真实 HTTP 测试只使用临时 D1、隔离 `RARDAR_DATA_DIR` 和随机端口；不得修改正式 data、D1、current 或 3000。

## 回滚

P1-6C1 不迁移数据库和 generation 数据。应用回滚只恢复旧页面/客户端代码；P1-6B 的 additive D1 表、canonical facts、legacy 投影和现有 API 兼容边界保持不变。回滚不会删除或重写 Stable ID 历史，也不需要 destructive migration。

## 明确非目标

- P1-6C2：接受 collision generation、处理 retained collision history 或放宽 legacy slug 发布门禁；
- 修改 `0004`、D1 adoption 或 API legacy selector；
- scheduler 配置、计划时间覆盖或自然调度人工触发；
- 清理 21 个历史 failed candidate；
- TrendRadar、P2、复杂 Agent、向量检索或新信源；
- UI 视觉重设计、部署或线上发布。

## 验证结果

完整验证统一运行：

```text
npm run verify
```

最终分支树本地结果：`PASS`

- Node.js `22.13.1`，npm `10.9.2`；
- Python：360 项，PASS；其中 16 项按平台能力安全跳过；
- Schema：healthy，21 个 artifact，0 error；
- Audit：healthy，0 error，0 warning；
- production build：PASS，canonical `/project/:projectIdVersion/:projectId` 与 legacy `/projects/:slug` 路由均进入产物；
- Node：65/65 PASS，包含真实 Vinext HTTP、临时 D1、pointer 切换、损坏 current fail-closed 与 rollback 恢复；
- production dependency audit：0 vulnerability；
- Verify 七道门禁、正式 data 不变、Git 可见内容不变、无残留 artifact、隔离 Runtime 清理守卫全部 PASS。

本记录只声明当前分支树的本地完整 Verify。Draft PR head 的 GitHub Verify 仍是后续合并门禁；在其真实完成前不得声称 GitHub Verify 已通过，也不得声称本工程轮已进入 `main`。

## 是否影响 North Star

不改变 Weekly Acted Projects 的定义、时间窗口或行动集合。P1-6C1 让页面和客户端真实使用已经作为周指标去重键的 Stable Project ID，消除 slug 串项目的交互风险，使一次用户行动更可靠地归属到唯一项目。

## 合并门槛与下一目标

本轮必须完成行为测试、完整 `npm run verify`、文档与 Draft PR 后停止。只有 Draft PR 人工审查并合并到 `main` 后，P1-6C1 才完成；此前不得开始 P1-6C2。P1-6C2 仍需独立定义 collision history、迁移、兼容 URL 和回滚协议，不能在本轮顺带实现。
