# 2026-07-16 P1-6B Stable Project Action Identity

## 本轮唯一目标

让 D1 项目行动、反馈、推荐及相关 API 使用 P1-6A 定义的 Stable Project ID，同时保持追加式 Event、独立 State、设备内幂等、Weekly Acted Projects 和旧代码回滚兼容。

P1-6A 已由 PR #8、提交 `d41033f` 合并到 `main` 并完成。本文件记录 P1-6B 独立工程轮的协议和验证边界；P1-6B 只有在本轮 Draft PR 合并到 `main` 后才视为完成。P1-6 整体仍未完成，下一项仅为 P1-6C UI、页面路由与 legacy URL 兼容，本轮没有开始 P1-6C。

## D1 v2 身份结构

迁移采用 additive 结构，不删除或改写旧代码依赖的表：

```text
project_identity_catalog
  generation_id
  project_id_version
  project_id
  canonical_repository
  project_slug

project_identity_runtime
  singleton active generation
  generation_id + published_at + published_at_micros

project_action_events_v2
  device_id + project_id_version + project_id
  project_slug + catalog_generation_id
  action + occurred_at + idempotency_key

project_action_state_v2
  device_id + project_id (project_id_version = 1)
  project_slug + catalog_generation_id
  highest_stage + per-stage timestamps

feedback_v2
  device_id + project_id (project_id_version = 1)
  project_slug + catalog_generation_id
  current value + timestamps

decision_events_v2
  legacy_event_id
  device_id + project_id_version + project_id
  project_slug + catalog_generation_id
  append-only value history
```

旧 `project_actions`、`project_action_events`、`project_action_state`、`feedback`、`decision_events` 及其兼容触发器继续保留。canonical v2 表是新代码的事实边界；legacy 表只作为映射迁移与应用代码回滚投影，不能重新成为 canonical 主键。

## Generation-bound 身份解析

每个相关请求只加载一次 published bundle，并从该 bundle 的 generation ID 与已验证 Catalog 构造完整身份上下文：

- Catalog v3 对每个 repository 机械重算 identity v1，并精确核对携带的 `projectIdVersion: 1` 和 projectId；
- Catalog v1/v2 从服务端 Catalog 的 `repo` 机械计算 identity v1；
- 规范化 repository、projectId 和 legacy slug 必须在当前 generation 内满足已声明的一致性与唯一性；
- 全部 retained generation 还必须保持 projectId ↔ canonical repository 一对一，同一 legacy slug 不得改绑另一个 projectId；
- D1 接收的是同一上下文导出的完整 `{ generationId, publishedAt, projects }` 映射，而不是客户端 repository 或陈旧的历史映射；
- legacy slug 无匹配、多匹配，projectId 不存在，或两个 selector 不一致时均 fail closed；只有经过全量历史证据验证且由 exact disposition policy 明确隔离的既有来源行例外，隔离不等于赋予身份；
- 不允许直接对 slug 哈希，也不允许从 owner 转移、仓库改名或文件顺序猜测归属。

Node 入口统一由 `app/project-identity.mjs` 提供 identity v1 重算、`createProjectIdentityContext(generationId, catalog, publishedAt)` 与 `resolveProjectSelector`；它使用 Web Crypto SHA-256 并继续受 P1-6A golden vectors 约束，不在各 API route 中复制另一套算法。

`project_identity_catalog` 保存不可改写的 generation-bound 关系，但 legacy 请求只能使用本次请求验证过的 generation 映射。全量 retained preflight、正式 INSERT trigger 与事务内 pairwise guard 都检查全局 identity/repository/legacy-slug 约束，preflight 后的并发 mapping 竞争也会以 `project_identity_collision` 失败。`project_identity_runtime` 同时保存 pointer 的 `publishedAt` 和微秒顺序并精确复核二者；较旧的慢请求不能把 active generation 回退，显式 rollback 因生成更新的 pointer 发布时间仍可重新激活 retained generation。active row、mutable State 重键、backfill 与兼容投影处于同一个 D1 原子 batch，对外只有全部成功后才可见；`unresolved_project_identity`、`conflicting_project_identity_mapping`、`stale_project_identity_generation`、`invalid_project_identity_runtime` 或 `invalid_legacy_project_row` 会阻止虚假的完成状态。

## Migration 顺序

`drizzle/0004_stable_project_identity.sql` 是本轮唯一正式、版本化 DDL migration；runtime bootstrap 通过 raw module 直接拆分并重放该文件，本地或 fresh-D1 初始化不能维护另一套 stable schema。该 migration 同时安装 legacy feedback → decision history 与 canonical capture 的完整链路；运行时 adoption 只负责 verified generation map、State 重键与事实迁移。整体顺序为：

1. 保留或创建全部 legacy 表和索引；
2. 创建 identity mapping、canonical v2 Event/State、feedback/history、索引与触发器；
3. 从一次 verified Catalog 构造完整 generation-bound mapping；
4. 对全部 legacy 行预检映射、action/value、幂等键和原始时间；
5. 只按明确的一对一映射机械 backfill，保留真实 `occurred_at` / `created_at` 和行动阶段，不从 State 补造 Event；
6. 在一个不可分割的 D1 batch 内切换 active identity generation、把同 projectId 的 mutable Action State/feedback 重键到 current slug，并完成 backfill；
7. 重复初始化和任意 migration/runtime 先后顺序均为幂等结果。

正式 `0004` 不读取 generation 文件，也不把 slug 猜成 repository。它只建立 additive 结构；需要事实映射的 preflight、backfill 和 active switch 由持有 verified Catalog map 的运行时 adoption 完成，而不是由第二份 DDL 或数据库自行推断。未被 exact disposition policy 覆盖的 unresolved 行或任一非法 legacy 行会使整次 adoption fail closed；policy 覆盖的行只保留原事实并写入 immutable ledger，不会被静默迁移成 Stable identity。

## API compatibility

canonical JSON 写请求同时提供：

```json
{
  "projectIdVersion": 1,
  "projectId": "stable-project-id"
}
```

GET 查询使用 `projectIdVersion=1&projectId=...`。P1-6C 前继续接受只有 legacy slug 的当前 UI 请求；两种 selector 同时出现时必须解析到同一项目。客户端 `repository` 和 `occurredAt` 不被接受为写入事实。

身份错误保持结构化且 fail closed：

- selector 形状错误或 projectId pair 缺一：HTTP 400 `invalid_project_selector`；
- 必须指定单项目的写入/查询在两种 selector 都缺失时：HTTP 400 `missing_project_identity`；Action/feedback collection GET 可只给 `deviceId` 返回 current Catalog 中的集合；
- 非 identity v1：HTTP 400 `unsupported_project_id_version`；
- 畸形或未知 ID：HTTP 400 `invalid_project_id` / HTTP 404 `unknown_project_id`；
- 畸形、未知或歧义 slug：HTTP 400 `invalid_project_slug` / HTTP 404 `unknown_project_slug` / HTTP 409 `ambiguous_project_slug`；
- projectId 与 slug 不一致：HTTP 409 `project_identity_conflict`；
- Catalog 或 stored identity 的版本/格式无法可信解析：服务端错误，不继续写入或返回混合身份数据；结构合法但暂时退出 current Catalog 的历史 projectId 继续保留在 D1 与追加式历史中，当前集合和推荐省略它，不需要 slug 投影的全局反馈 State 聚合以及近 7 天 Event/decision 周指标仍按 projectId 计入。

Action、feedback 与观察状态项目记录返回 `projectIdVersion`、`projectId` 和临时兼容 `projectSlug`；历史行先由其不可变 `catalogGenerationId` 映射证明来源，响应再按当前 Catalog 的同一 projectId 投影当前兼容 slug，因此合法的 slug 变化不会让 Stable ID 记录变成 503。recommendation 项返回 Stable ID 并保留现有 `slug`，供尚未迁移的 P1-6C 消费者使用。metrics 继续返回聚合值，但按 canonical projectId 计算。反馈当前状态、decision history 和推荐关联都按 projectId 查询，不允许碰撞 slug 串项目。

## Idempotency 与 Weekly metric

canonical Event 继续只允许 INSERT，`occurredAt` 由服务端生成。相同 device、幂等键、projectId 与 action 是安全重放；相同键绑定不同 projectId 或 action 返回冲突。成功后的新用户意图使用新键并形成新的历史 Event。

State 以 `device_id + project_id` 唯一并要求 `project_id_version = 1`，最高阶段单调推进，各阶段时间只由真实 Event 写入。State 只服务按钮或观察状态，不能反向补造 Event。

Weekly Acted Projects 的定义不变：一次服务端 `now` 下，在包含下界的 `[now - 7 days, now]` 内，只查询 canonical `tried`、`cloned`、`reused` Event，并按不同 projectId 去重；`opened` 与 `saved` 仍只是辅助漏斗。同一项目一周多次行动计一个，旧事件离开窗口后的新真实行动可以再次计入。

## 回滚与再次升级

本轮不执行破坏性 down migration。canonical 写入通过 `project_action_events_v2_legacy_projection` 及 feedback legacy projection 同步到旧 slug 读取边界；旧代码回滚期间实际成功写入的事实，由 `project_action_events_capture_stable` 和 feedback/decision capture 在再次升级时捕获。

generation 只改变同一 projectId 的兼容 slug 时，adoption 仅原子重键 mutable `project_action_state`、`feedback` 及对应 canonical State 的 slug/generation；append-only Action Event、`project_actions` 与 decision history 保留原始 slug、时间和行数，不新增或改写历史。目标 slug 已有另一条 State/feedback 时整批 fail closed；重复 adoption 为 no-op。由此旧代码在没有任何新写入时也能立即按 current slug 读取回滚前最新按钮和反馈状态。

双向触发器以 identity、设备、action/value、幂等键与等价时间识别同一事实，防止格式化时间、触发器回环或同键重放制造第二个 Event/history。只有 active generation 中存在唯一 verified mapping 时才允许跨版本投影；unresolved identity 不做错误投影。

旧代码回滚后仍受旧表全生命周期唯一状态的既有限制，因此其无法保存的跨周期同阶段重复行动不会被补造。immutable legacy Event 跨历史 slug 保留；若回滚后又在当前别名产生行动，旧版按 slug 的周指标可能分别计算同一 Stable ID 的多个别名，canonical v2 指标仍按 projectId 去重。可逆性保证的是已真实保存事实的双向投影，不是推断不存在的历史。

只回滚 P1-6B 到仍支持 Catalog v3 的 PR #8 时，停止写入、备份 D1 并确认 current legacy State/feedback 投影后即可让旧代码接管，无需破坏 canonical 表。若完整回滚 Stable ID 到 pre-v3，则先降级 flat staging，并在 P1-6B 代码仍运行时 rollback pointer 到健康 Catalog v1/v2，再在目标 Runtime 的实际 D1 上发起一次预期会执行 adoption 的受控 GET，验证 `project_identity_runtime` 已激活目标 generation；Schema/Audit 通过并停止 Runtime 后，才回滚应用代码。

## 验证与安全边界

本轮验证必须覆盖 resolver v1/v2/v3、D1 空库与 legacy adoption、正式 0004 独立反馈历史、两种 migration 顺序、重复初始化、unresolved/非法行、跨 retained collision 与 adoption publisher 竞争、Action/feedback/recommendation/metrics API、服务端发生时间、幂等冲突、State 单调、七天边界、双向投影和真实 Vinext HTTP。slug rename 用例必须在没有新写时证明旧代码立即读取 Action State/feedback、Event/history 不增不改、重复 adoption no-op，并证明目标冲突整批零写入。交付前统一运行 `npm run verify`，并由 Draft PR 的 GitHub `Verify` 检查当前 head。

测试只使用开发 worktree 的临时 D1、隔离 `RARDAR_DATA_DIR` 与随机端口；不访问 Primary Runtime，不修改正式 `data/`，不占用 3000，不部署。实际验证结果记录在本轮 Draft PR，不能在检查成功前由本文预先声称通过。

## 首次真实数据 adoption 审查修正

首次隔离 D1 演练发现两个 legacy slug 已退出 current Catalog，不能继续把“current 无匹配”笼统当成同一种错误。本轮在 P1-6B 内增加严格 Historical Identity Bundle 与显式 disposition：

- `oomol-lab--open-connector` 只通过 retained ready generation 中唯一验证的 `oomol-lab/open-connector` 关系 backfill；
- `officecli` 在 current 和全部 retained Catalog 中都没有 repository 证据，只对实际存在的 `feedback` source 应用 policy `2026-07-18.1`，保留 legacy 事实并写入不含 device ID 的 immutable unresolved ledger；
- policy 未覆盖、来源表不符、同 slug 多身份、损坏 retained final、provenance 不完整或任何非等价既有目标都会使整个 adoption 零部分写入；
- backfill 的 inactive mapping 只在同一事务的临时 session/allowlist 中有效，且必须匹配精确 legacy source；普通 API 写入仍只接受 current generation；
- 旧 head 已创建部分 `0004` 结构时，新 migration replay 会安全替换语义已变化的 active-generation 与 unresolved-ledger integrity triggers，不删除 legacy/canonical 事实；
- immutable evidence 不保存 role-dependent `publishedAt`，并覆盖 Historical Bundle 的 A → B → rollback A 生命周期；retained witness 以 RFC3339 微秒精度排序。
- Bundle 以独立 `generations` 清单保存 provenance，合法空 Catalog 的 `mappingCount` 可为 0；D1 仍会保存该代 evidence，并对每代 mapping 数做精确校验。

### 隔离真实副本演练（2026-07-18）

演练只使用既有 Primary Runtime D1/data 备份的工作副本、随机回环端口和临时 Runtime 状态目录；没有读取或写入正式 D1/data，也没有占用 3000。结果如下：

- Historical Identity Bundle 严格验证 5 个 generation、150 条 mapping；`oomol-lab--open-connector` 唯一解析为 `oomol-lab/open-connector`，事实绑定到最近的 verified retained witness `20260713T191537075729Z-4e2e9d09fae2`；
- `officecli` 只在 legacy `feedback` 中存在 1 条事实，adoption 后 legacy 行保持不变、canonical 行为 0、immutable quarantine ledger 恰好 1 条且不含 device ID；
- 其他事实机械迁移为 5 条 Action Event、1 条 State、3 条 feedback 和 4 条 decision history，原始 legacy 计数与逻辑摘要保持不变；临时 adoption session、allowlist 和 migration guard 在成功后均为空；
- 完全停止并重启新代码后再次执行只读 GET，stable/D1 逻辑摘要不变，证明重复 adoption 为 no-op；
- PR #8 代码在同一已迁移副本上通过 `/api/health`、actions、feedback、metrics 和 recommendations 的只读 GET，均返回 HTTP 200；没有执行 destructive down migration；
- 副本 generation 的 Schema 与 Audit 均为 healthy，所有临时 Vinext 进程和端口在演练后已关闭。

本修正仍属于 Draft PR #9 的 P1-6B 范围，不开始 P1-6C。完整本地 Verify 与 GitHub Verify 仍以本轮实际执行结果为准。

## P1-6C 明确非目标

本轮不修改页面 route、链接、React key、组件 identity、按钮 props、观察列表本地映射或 legacy URL redirect。P1-6B Draft PR 合并前不得开始 P1-6C；合并后仍须从最新 `main` 创建独立工程轮。

## 是否影响 North Star

不改变 Weekly Acted Projects 的行为定义，只把“不同项目”的去重键从可碰撞 legacy slug 修正为 Stable Project ID，并保持追加式 Event、服务端时间窗口及 feedback 非行动事实边界。
