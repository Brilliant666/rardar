# RARDAR-PRODUCER-RUNTIME-INTEGRATION-01 — One Managed Scheduler for Trending Facts

日期：2026-08-26
状态：实现与审查中；尚未完成 Production 部署或首次自然 Observation 验收

## 唯一目标

把已经合并的 `TrendingObservation v1` 与 `TrendingExplosionArtifact v1` 接入现有唯一 Managed Scheduler，同时完整保留原有 daily refresh、generation、D1、Public Edge 和进程所有权合同。

```text
systemd
└─ Manager
   ├─ Website
   └─ Scheduler
      ├─ Observation  每个 Asia/Shanghai 偶数整点
      ├─ Refresh      每日 08:00
      └─ Explosion    每日 08:00
```

本轮不新增 cron、systemd timer、service、daemon、持久任务队列或第二个 Scheduler；不实现 TopicEye、Sub2API/AI、Find Project、P1-6C2，也不修改 Public Edge、Stable Project ID 或 D1 业务事实。

## Feature flag 与 secret

`RARDAR_TRENDING_PRODUCER_ENABLED` 是严格的非敏感布尔合同：未配置和 `false` 都保持 daily-refresh-only，只有精确小写 `true` 才启用 Producer；其他值在 Manager 写文件或 spawn child 之前 fail closed。Producer 只支持固定 `08:00 Asia/Shanghai` 产品调度和 120 分钟 cadence，不引入任意 profile。

正式 Observation 要求 `GITHUB_TOKEN`。真实值只存在受限 secret EnvironmentFile 中，继承给 Scheduler child；Website 的正向 allowlist 不包含 token 或 Producer flag。CLI 参数、status、capture、generation、日志、浏览器与 Git 都不保存 token。

## 固定事件与顺序

纯函数 `pipeline/producer_schedule.py` 只计算十二个固定 phase、下一事件、startup observation eligibility、daily event 和同相位优先级。每个事件保存 intended `scheduledAt`，不会用实际开始时间伪造固定相位。

同一天 08:00 的顺序严格为：

1. Observation；
2. 原有 daily Refresh；
3. Explosion derive。

三项由同一个 Scheduler 串行执行。Observation 失败仍继续 Refresh；Refresh 按既有合同最终失败后，Explosion 仍可基于未推进的可信 current 尝试；Explosion 失败只更新 Producer telemetry，不杀死 Scheduler，也不撤销 Observation 或 Refresh。

## Startup、catch-up 与 retry

- `--skip-initial` 保持 Manager restart 不主动刷新；
- daily Refresh 继续使用 12 小时 catch-up、最多三次尝试、五分钟间隔和 remote-clone non-retryable 分类；
- startup 只允许最近一个 observation slot 且实际延迟不超过 10 分钟，不补多个历史 slot；same-slot 已存在时由 create-only observer 返回 `already_captured`；
- 明确的全源 network、HTTP 408/429/5xx failure 最多短重试一次，仍使用相同 `scheduledAt`，总等待不越过 +10 分钟 eligibility deadline；
- observer lock 已被占用时返回 `skipped_overlap`，不等待也不创建第二个 observer；
- daily catch-up 后，当天 08:00 capture 已存在且 eligible 时可幂等 Explosion catch-up；缺失或不 eligible 时只记录 `not_ready`，不制造 capture。

首次合法且 eligible 的 08:00 capture 会记录 `first08CaptureAt`，并机械计算 `firstExactEligibleAt = first08CaptureAt + 24h`。这只是最早可能日期，不保证届时 exact；current 与 baseline 两个 endpoint 仍须都 eligible。

## Telemetry 与并发安全

既有 top-level Refresh 字段保持兼容：`state`、last run/success/error、`nextRunAt`、`retryAttempt`、generation 与 Audit summary。新增嵌套 `producer`，分别记录 Observation 和 Explosion 的 state、时间、counts、coverage、bounded error code 与 next run。

Scheduler 使用一个进程内 `SchedulerStatusStore` 和原子 replace，保证 Refresh heartbeat、Producer heartbeat、Observation 与 Explosion 不互相覆盖 sibling fields。restart 只恢复明确列入合同的 path-free scalar；Manager 只在 status PID 与当前受管 Scheduler identity 匹配时转发 reviewed telemetry。warming/degraded Producer 不改变 Scheduler heartbeat 健康，也不触发 Manager restart。

telemetry 明确拒绝 capture/candidate absolute path、Authorization、secret 和 stack trace。Observation CLI 可以返回内部 path 供操作者使用，但 Scheduler 不把它复制到 status。

## 数据隔离

Observation 继续只通过 append-only create-only store 写入：

```text
data/observations/trending/v1/captures/YYYY/MM/DD/<captureId>.json
```

它不写 `current.json`、generation、Catalog、Signals 或 D1。Explosion 继续只通过 audited derive candidate、Schema/Audit 和 base-generation CAS 发布；它不写 D1。成功写入的 Observation 是历史事实，即使应用回滚也不删除。

## 验证

开发测试使用可注入时钟、临时 data/runtime、Fake GitHub client 和隔离端口，不调用 live GitHub。定向回归在文档形成时为：

```text
188 tests passed
12 platform-conditional tests skipped
```

覆盖 schedule/跨午夜/08:00 order、9/11 分钟 startup、bounded retry、token redaction、failure isolation、三次 Refresh retry、non-retryable、warming/already-derived/blocked、status sibling preservation、restart telemetry filter、Manager trust、Website environment filtering，以及既有 Observation/Explosion/Deployment contracts。

最终 Windows `npm run verify` 已通过：Python 591 项通过、36 项平台条件跳过，Schema 21/21 healthy，Audit healthy 且 warning 0，Production build 通过，Node 87/87，通过生产依赖审计且漏洞 0；repository data、Git-visible contents、残留 artifacts 与隔离 Runtime cleanup guards 全部通过。Ubuntu 24.04 exact-head Verify、systemd static verify 与 GitHub CI 结果仍是合并门禁，不能由 Windows 结果替代。

## 部署与回滚

代码合并不表示 Producer 已部署。Production 只接受本任务 squash merge SHA 对应、且绑定成功 main Verify 的新 Linux x86_64 Release Artifact；服务器不运行 `npm ci`、`npm install`、联网 pip 或 build。

激活前必须只读确认 Runtime/data/D1/资源无漂移、没有 writer/refresh/retry、避开 07:30–08:30 以及距下一 phase 不足 15 分钟，并在隔离临时 data 上用 Production `GITHUB_TOKEN` 完成 observer dry-run。随后备份环境与 Runtime 证据，离线安装 exact artifact，把 flag 设为 `true`，原子切换 current，并只执行一次受控 `rardar.service` restart。

restart 后 generation、current、Snapshot、Catalog、Signals、D1 和 candidate counts 必须不变，且在下一个自然相位前不得产生 capture、Refresh 或 Explosion。首个自然 Observation 必须通过 capture Schema/digest/Audit，并证明 generation/current/Catalog/Signals/D1 未改变。

若 Runtime、single Scheduler、Refresh compatibility、generation/D1 isolation、secret、systemd 或资源边界失败，则恢复旧 EnvironmentFile、关闭 flag、切回旧 release 并只做一次受控 restart。已经成功追加的 Observation 保留，不删除。

## 首次自然 Observation

本记录合入代码时仍为：

```text
Production deployment: PENDING
first natural scheduledAt: PENDING
first08CaptureAt: PENDING
firstExactEligibleAt: PENDING
```

只有独立 Production 验收实际完成后，运行报告才能声明 `RARDAR-PRODUCER-SCHEDULER = ACTIVE`。后续只允许单独观察首个自然 08:00 Explosion derive；不得由本轮自动开始 TopicEye、AI 或人工补跑。
