# 2026-08-10 Runtime Operational Readiness

## 本轮唯一目标

让 Managed Runtime 的每日 schedule 成为显式、可验证且只有一个 owner 的配置，并主动暴露“进程存活但 current generation 数据已经陈旧”的状态。

本轮分支：

```text
feat/runtime-operational-readiness
```

基线为 PR #13 的 Squash merge 提交 `dfed8f0ffbb67ff080dc783839f57514dfa05e87`。PR #13 的 PR Verify run `31320900294` 与 main push Verify run `31323603841` 均为 `SUCCESS`，因此 P1-6C1 已完成。P1-6C2 仍未完成且经用户明确决定 deferred；本轮不放宽 legacy slug collision gate，也不代表 P1-6 整体完成。

## Discovery matrix

| Concern | 旧来源 | 问题 | 本轮目标契约 | 行为验证 |
| --- | --- | --- | --- | --- |
| Schedule time | `runtime.py` child argv 与 package script 固定 08:00 | Manager、standalone CLI 与 status 来源分散 | `RARDAR_SCHEDULE_AT`，默认 08:00；CLI 显式值覆盖 env | 默认/custom/非法、argv/env、restart/no-hot-update |
| Timezone | Manager/package 固定 Asia/Shanghai | 不能显式配置，Windows 环境还依赖 timezone database | `RARDAR_SCHEDULE_TIMEZONE`，默认 Asia/Shanghai，严格 IANA | custom/invalid、tzdata dependency、next run telemetry |
| Scheduler ownership | 只有 Manager lock 与短期 data write lock | standalone scheduler 可与 managed child 并行 | canonical data directory 的长生命周期单实例锁；冲突在 status/refresh 前退出 | 跨进程第二 owner 拒绝、无 status/data 写 |
| Scheduler telemetry | status 带 PID，但 Manager 未绑定 | 旧或外来 heartbeat 可冒充当前 child | Manager config 是 authority，telemetry 必须匹配当前 child PID | foreign PID/status schedule 拒绝 |
| Data age | health 只返回 generationId | PID 全活时无法识别长期未更新 | current generation snapshot capturedAt，默认阈值 36h | `<` / `=` / `>` 阈值、future/invalid |
| Health | 只有 healthy 或 503 degraded | stale 与 corrupt 混为一类 | stale = HTTP 200/degraded；invalid/corrupt = 503 fail-closed | health/home/rollback/pointer switch |
| Operator status | `local:status` 主要展示进程 | 无年龄、阈值、effective schedule | 输出 schedule、next run、last success、generation、snapshot age/freshness | JSON contract、stale nonzero、PID/control identity |

## Schedule 配置与 ownership

三项环境变量构成一份共享契约：

```text
RARDAR_SCHEDULE_AT=08:00
RARDAR_SCHEDULE_TIMEZONE=Asia/Shanghai
RARDAR_STALE_AFTER_HOURS=36
```

- 环境缺失使用默认值；存在但为空同样非法；
- schedule 只接受 canonical `HH:MM`，timezone 必须由 IANA/ZoneInfo 加载，阈值为 1～8760 的整数小时；
- Scheduler 显式 CLI `--at` / `--timezone` 优先于 env；
- `local:start` 与内部 Manager 在创建/停止进程或写状态前验证配置；
- Manager 启动时冻结配置并把显式 argv/env 传给唯一 child；运行中改变 env 不会热更新，必须完整 stop/start；
- `nextRunAt` 由 Scheduler 自己计算，status 文件不能改变 schedule；
- 每个 canonical data directory 有一个独立于短期 writer lock 的 Scheduler instance lock；第二个 daemon 或 `--once` 在写 status/refresh 前稳定退出，Manager 不把 ownership conflict 变成两秒 crash loop；
- Manager 只接纳当前 child PID 的 heartbeat/nextRun telemetry，Runtime status 也必须与 control PID 一致。

本轮没有增加新的 missed-run catch-up。既有 12 小时 restart catch-up 和同周期 5 分钟、最多 3 次的 retry 语义保持原样。

## Freshness 与 Health

Web host 继续严格解析一次 current pointer、manifest、全部 artifact hashes 与 audit。`snapshots/latest.json` 现在是必需 artifact，但 bridge 只把同 generation 的 `snapshotCapturedAt` 标量传给 Worker，不传整份快照。Catalog `capturedAt` 必须与 snapshot 时间表示同一 UTC instant。

freshness 固定按 snapshot 时间计算：

- `ageSeconds <= staleAfterSeconds`：`fresh`；
- `ageSeconds > staleAfterSeconds`：`stale`；
- capturedAt 缺失、日历时间非法、无 timezone、超过五分钟的未来时间，或 current/manifest/hash 不可信：`invalid`。

不使用文件 mtime、process time、scheduler heartbeat、pointer publishedAt 或 current.json mtime。Worker 每个请求重新计算；Manager 允许响应在传输中跨过恰好 36 小时边界，但会校验 Worker age 与自身时钟在 15 秒内一致。

`/api/health` 合同：

- fresh：HTTP 200，`status: healthy`；
- stale：HTTP 200，`status: degraded`，`reason: published_data_stale`，并返回 generation、snapshot age/threshold 与 effective schedule；
- invalid/corrupt：HTTP 503，`reason: published_generation_unavailable`，不声称可信 generation。

首页 stale 时显示“数据更新已延迟”、最近成功快照和当前数据年龄；页面不阻断，也不删除旧 generation。fresh 时不显示警告。RuntimeStatus 对 status JSON 做字段、PID、时间、freshness 与 overall 组合的运行时校验，未来/旧/畸形 telemetry 不显示为运行中。

## 兼容、安全与回滚

- 默认 schedule 与旧行为完全一致；合法 fresh generation 的页面/API 行为保持不变；
- stale 只是可用性降级，不改变 Schema/Audit/manifest，也不触发 refresh；
- 不修改 refresh、candidate、publish、评分、信源、D1、`0004` 或 Stable ID；
- 不修改/删除 Primary data、current、retained generation 或 21 个 failed candidate；
- 不实现 P1-6C2、TrendRadar/P2、复杂 Agent、部署或 always-on hosting；
- 回滚只需恢复本轮应用代码并 stop/start Managed Runtime；没有数据或数据库迁移需要回滚。

测试只使用 worktree 自有 `.venv`、临时 data/D1/runtime/state 和随机 loopback 端口，明确排除 3000/3002。真实 Vinext 测试同时覆盖 fresh、stale、pointer switch、current 损坏 503 与原进程 rollback 恢复；结束后只清理自有进程和临时根。

## 验证结果

最终提交树统一运行：

```text
$env:RARDAR_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
npm run verify
git diff --check
git diff -- data
```

最终提交树统一运行 `npm run verify` 并通过，结果为：

- Node.js `22.13.1`、npm `10.9.2`；
- Lint PASS；
- Python 382 项：366 PASS、16 个按平台或权限安全跳过；
- Schema validation：21 个 artifact、0 error；
- Data audit：healthy、0 error、0 warning；
- Production build PASS；
- Node 69 项全部 PASS，包含真实 Vinext HTTP、临时 D1、fresh/stale、pointer switch、current 损坏与原进程 rollback；
- production dependency audit：0 vulnerability；
- repository data、Git-visible bytes、Git-visible artifact 与 isolated Runtime cleanup 四项 guard 全部 PASS；
- 七道 Verify 门禁全部 PASS。

## 是否影响 North Star

不改变 Weekly Acted Projects 的定义、时间窗口、Stable Project ID 去重键或行动集合。该工程轮只让操作者和用户能够辨认驱动决策的 verified snapshot 是否及时，避免把长时间未更新的数据误当作“今日情报”。

## 交付门槛

本轮在完整 Verify、文档、提交、Push 与 Draft PR 完成后停止。Draft PR 不转 Ready、不合并、不部署。always-on deployment 仍是后续独立阶段；P1-6C2 保持 deferred，除非用户另行明确恢复该目标。

## 最终合并状态

上述交付门槛记录的是 PR 合并前的执行边界。人工审查完成后，PR #14 已以 Squash merge 提交 `e61e3ff35390ab9f915818f72e5e3321896fd17e` 合并到 `main`；main push Verify run `31351088836` 为 `SUCCESS`。因此 Runtime Operational Readiness 已完成，不得在后续轮次重复实施。

用户随后明确选择 Always-on Deployment v1 作为独立上线前工程轮。该选择不恢复或完成 P1-6C2，也不授权 SSH、真实服务器部署、DNS、TLS、防火墙、生产 secret、Primary data 迁移、refresh 或 failed candidate cleanup。
