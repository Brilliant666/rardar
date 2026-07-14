# 2026-07-14 · Append-only project action events

## 目标

把 `project_actions` 的全生命周期唯一状态拆成追加式历史 Event 与独立当前 State，使同一项目和行动在后续周期再次发生时能够形成新事实，并让 Weekly Acted Projects 严格基于近 7 天 Event 计算。

## 本分支实现

- 新增追加式 `project_action_events`，保存 device ID、project slug、action、服务端发生时间与幂等键；
- 新增每个设备和项目一行的 `project_action_state`，保存最高阶段、五个真实阶段的最近发生时间和更新时间；
- Event INSERT 通过 D1 触发器原子更新 State，UPDATE 与 DELETE Event 由数据库触发器拒绝；
- POST `/api/actions` 要求设备内唯一幂等键，同键同 payload 返回安全重放，同键不同项目或行动返回 409；
- 客户端在一次网络重试和页面内失败重试中复用幂等键，成功后的再次点击使用新键；
- GET `/api/actions` 只读取 State，并保留由 State 生成的兼容 `actions` 投影；
- Weekly Acted Projects 和打开、收藏、试用、浅克隆、复用辅助指标只查询近 7 天 Event；
- 按钮不再因已达到某阶段而永久禁止以后再次记录，同时使用每阶段 in-flight guard 防止快速双击形成两个用户意图；
- 生成 Drizzle migration，并把相同幂等 DDL、触发器和迁移接入运行时 schema 初始化。

## 迁移、兼容与回滚

- 每个 legacy `project_actions` 行机械迁移为一个 Event，原样保留 `created_at`，使用 `legacy-project-actions:<id>` 稳定键；
- 不补造旧唯一表已经丢失的跨周重复，也不根据最高阶段补造低阶段；
- 多次运行初始化不会重复迁移；
- legacy 表保留，新 Event 将对应旧阶段推进到最近真实时间，并规范化为旧版周查询兼容的 UTC SQLite 时间文本；反向触发器不会把该投影重复捕获为 Event，旧代码成功插入的新阶段仍会被捕获；
- 代码回滚不执行破坏性 down migration，Event 与 State 都保留；旧版本在回滚期间仍无法表达同阶段跨周重复，这是明确的兼容限制。

## 行为验证

- SQLite/Drizzle migration：旧行数量、原始时间、确定性幂等键和缺失阶段均保持真实；重复初始化不重复写入；
- 幂等：同键同 payload 只有一个 Event，同键不同 payload 冲突，新键允许同一行动再次追加；
- 跨周期：八天前的同一行动不进入当前窗口，本周用新键再次行动后重新计入；
- 时间窗口：包含恰好七天的带时区边界，排除早一秒的事件，也排除窗口后的未来事件；
- 回滚窗口：legacy 投影通过旧版文本时间查询时，同样排除边界日中早于精确七天 cutoff 的事件，重放初始化不制造重复 Event；
- State：五个阶段只记录真实发生时间，最高阶段单调推进，较晚低阶段不会降级；
- 追加式约束：直接 UPDATE 或 DELETE Event 被数据库拒绝；
- 真实 Vinext/D1：8 个并发同键 HTTP 请求只有 1 个 `recorded: true`，其余为安全重放；
- 推荐与指标：行动写入前后推荐响应不变，Weekly Acted Projects 对同一项目只计一次。

## 完整验证结果

- `npm run lint`：通过；
- `python -m unittest discover -s pipeline -p "test_*.py"`：共运行 174 项，170 项通过，4 项因 Windows 符号链接权限条件跳过；
- `npm run data:validate`：21 项数据验证通过；
- `npm run data:audit`：当前 generation 审计健康，0 error、0 warning；
- `npm run build`：通过；
- `npm test`：12 项通过，包含 SQLite 迁移/幂等/边界/回滚兼容行为和真实 Vinext/D1 并发 HTTP 验收；
- `npm run security:audit:prod`：0 个已知生产依赖漏洞。

## 新发现

- 旧按钮把“是否曾发生某阶段”作为独立状态，而历史审查提出单一最高阶段；State 同时保存最高阶段与各阶段真实最近时间，可以满足两种读取且不会反向伪造步骤；
- 保留 legacy 表并使用双向兼容触发器，比删除或改名更适合可回滚发布；
- 网络失败后的再次点击也必须复用未决幂等键，仅在一次函数调用内重试不足以覆盖响应丢失。

## 遗留风险

- 匿名 device ID 仍来自本地浏览器存储；本轮不引入账号或跨设备同步；
- legacy 版本在代码回滚期间仍受终身唯一约束，无法保存同阶段重复事实；
- 项目仍使用 slug 关联行动，稳定项目 ID 属于后续独立目标；
- `node:sqlite` 行为测试在 Node.js 22.13 中会输出实验性 API 警告，但真实 Vinext/D1 HTTP 验收同时覆盖生产运行路径。

## 是否影响 North Star

不改变 Weekly Acted Projects 的定义，但修复其历史来源和跨周期准确性。指标现在只来自追加式 Event，同一项目在窗口内多阶段仍只计一次，后续周期的新真实行动可以再次进入对应窗口。

## 治理状态与下一项

本文件记录 PR #5 的实现与验证；该 PR 已通过提交 `238b572` 合并到 `main`，P1-3 现已完成。合并并确认工作区干净后，独立评分语义工程轮才从最新 `main` 建立；行动事件 PR 本身没有夹带评分修改。
