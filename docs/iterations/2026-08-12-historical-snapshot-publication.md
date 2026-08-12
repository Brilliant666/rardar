# 2026-08-12 Historical Snapshot Publication

## 本轮唯一目标

修复 2026-08-12 Server Primary 第一次无人值守自然 refresh 在 publication 阶段被
historical snapshot byte-exact invariant 拒绝的问题。只修 snapshot history producer；
不修改 Scheduler、Runtime、systemd、D1、评分、信源或页面。

分支与基线：

```text
branch: fix/historical-snapshot-publication
base: d3794cb4a36b14cf8e10613968ac17a90818717e
```

## Production incident

Server Primary 的同一个既有 Scheduler 在 `2026-08-12 08:00 Asia/Shanghai` 自然触发，
没有人工 refresh、`--once`、restart 或 `nextRunAt` 修改。它按既有 retry policy 运行三次：

| Attempt | Candidate | Created at | Base | Candidate state | Schema/Audit |
| --- | --- | --- | --- | --- | --- |
| 1 | `20260812T000003003777Z-2e3f1d854788` | `2026-08-12T00:00:00.002158Z` | `20260811T000002511216Z-7001f2018eb9` | ready/unpublished | healthy, 41 validated, 0 error, 0 warning |
| 2 | `20260812T000550808731Z-d2ce599a166f` | `2026-08-12T00:05:47.985213Z` | 同上 | ready/unpublished | healthy, 41 validated, 0 error, 0 warning |
| 3 | `20260812T001136685807Z-5a032f2392d3` | `2026-08-12T00:11:33.816651Z` | 同上 | ready/unpublished | healthy, 41 validated, 0 error, 0 warning |

三次都在 `pipeline.generations._enforce_operation_baseline()` 被同一错误拒绝：

```text
code: refresh_base_snapshot_not_archived
stage: conflict
message: refresh history must contain exactly one byte-exact archive of the current snapshot
path: snapshots/history/20260811000000.json
```

publication 拒绝后，`current.json` 仍指向
`20260811T000002511216Z-7001f2018eb9`；21 个 historical failed candidates 未变；
三份新 candidate 保持 `ready`，没有被错误标成 failed。

## Last candidate evidence

最后一次 candidate 的 snapshot 与 Catalog 都是
`2026-08-12T00:11:33.816651Z`。GitHub Search 9/9 query 成功、184 candidates；
analysis failure 0；6/6 signal sources healthy，共 30 signals；Schema validated 41；
Audit healthy、0 error、0 warning。

这证明 source/build/Schema/Audit 已完成，失败只发生在 authoritative publication 前。
`lastSuccessfulRefreshAt` 因此没有推进，符合“只有 current pointer 成功切换才算成功 refresh”的语义。

## Exact invariant 与 RCA

publication 顺序保持：

```text
building candidate
→ Schema/Audit
→ ready manifest + artifact hashes
→ canonical data lock
→ base-generation CAS
→ byte-exact history invariant
→ retained generation placement
→ atomic current pointer replace
```

三份 candidate 都只包含一个语义匹配的 base archive，且都撞到同一个确定性路径。
生产字节证据：

```text
published current snapshots/latest.json:
  capturedAt: 2026-08-11T00:00:00.010989Z
  SHA-256: 5876cea6543c7c49a2f27d1c135973c08657125cb6e15f3b5c78a1b882bbde69
  bytes: 202191
  CRLF: 5849

each candidate snapshots/history/20260811000000.json:
  same capturedAt and JSON object
  SHA-256: 03bd2dbb8c0aa66b18da8cad5c057641136de24c632ecf43695d3095208a9816
  bytes: 196342
  CRLF: 0
  LF: 5849
```

换行规范化后两者完全相同。Windows cutover 保留了 current snapshot 的 CRLF 原始字节，
Linux refresh 先解析 JSON，再用平台文本 writer 把 `previous` 重序列化为 LF。语义和
archive key 都正确，但 producer 丢失了 immutable base 的原始 content identity；
publication gate 正确地拒绝了非 byte-exact archive。

根因分类为 **Case A：producer 生成了不等价的 archive bytes**。这不是 Case B 的
pre-existing idempotent target，也不是 Case C 的 key collision，因此本轮不增加
`existing == success` 语义，也不放宽 publication invariant。

## New contract

refresh producer 现在：

1. 对 candidate 内克隆的 base `snapshots/latest.json` 使用共享 `stable_read`，取得两次
   独立 no-follow FD 读取证明稳定的原始 bytes 与 SHA；
2. 从同一份原始 bytes 严格解析、Schema validate `previous`，所有 growth/Catalog 语义
   继续使用该对象；
3. archive 写入使用原始 bytes，不再重序列化；
4. history target 必须缺失，并通过同目录临时文件、flush/fsync 与 hard-link create-only
   原子提交；出现既有或竞态 target 时 fail closed，绝不覆盖；
5. archive 与其余 candidate JSON 保持同一 batch rollback 边界；publication 仍再次通过
   manifest hash、Audit、base CAS 与 byte-exact invariant。

如果 target 已存在，即使字节完全相同也继续拒绝，因为正常 daily producer 应只创建一次
新的 archive identity。重复或碰撞 target 是需要诊断的 candidate build 问题，不应被
静默当作 idempotent success。ready candidate 的 publication retry 仍由既有 immutable
candidate/manifest/pointer 协议处理。

## Atomicity 与 fail-closed boundaries

- 不使用 `exists → normal write` 的 TOCTOU 模式；
- 不覆盖、不 unlink 既有 archive；
- symlink、junction/reparse 或非 regular leaf/ancestor 被拒绝；
- stable-read、UTF-8、strict JSON 或 Schema 任一失败都会在 candidate build 内停止；
- create-only 竞态发生时，batch 恢复此前被替换的 candidate artifact，并保留竞争者字节；
- publication gate、retained rename 与 current pointer CAS/atomic replace 均未改变；
- 不会出现 pointer 已推进但 history 缺失；candidate ready 后仍不可变。

## Deterministic reproduction

在未修改的 `d3794cb` 上，以隔离临时 data 构造带 CRLF 的 published base，并机械生成
同语义 LF archive。10/10 次都得到：

```text
refresh_base_snapshot_not_archived
refresh history must contain exactly one byte-exact archive of the current snapshot
```

10/10 的 current pointer 与 current generation 都保持不变，不依赖 sleep 或网络。

## Regression matrix

| Contract | Expected |
| --- | --- |
| target absent / normal first archive | stable source bytes create-only，publication PASS |
| identical pre-existing target | FAIL CLOSED，不把 producer collision 当幂等成功 |
| same path / different bytes | FAIL CLOSED，existing bytes unchanged |
| same path / same length mutation | FAIL CLOSED，existing bytes unchanged |
| corrupt existing history | FAIL CLOSED，existing bytes unchanged |
| symlink/junction target | FAIL CLOSED，不跟随、不修改外部 target |
| ready candidate publication retry | 既有 orphan/pointer retry 保持幂等，不重新写 ready artifact |
| Aug-11 CRLF → Aug-12 Linux refresh | archive byte-exact，ready generation publication PASS |
| pointer CAS conflict | 既有 `stale_base_generation` 语义保持 |
| rollback | retained generation 重新完整验证后原子 repoint，读取正常 |

## Unpublished candidate policy

生产三份 ready/unpublished candidate 是本次事故的不可变 forensic evidence。本 PR：

- 不删除、修改、重命名或重新 publish；
- 不把它们标记为 failed；
- 修复后也不 retroactively publish；
- 后续仅在独立、明确授权的维护任务中处理 retained forensic cleanup；
- 真正的修复验证应由部署新 release 后的未来自然 refresh 完成。

21 个历史 failed candidates 同样保持原样。`failed != ready-unpublished` 的状态语义不变。

## Verification 与后续

最终交付必须记录：old-main 10/10 reproduction、Windows full `npm run verify`、Linux
scratch exact-head full Verify 与 GitHub PR Verify。Linux scratch 只能使用独立
`/opt/rardar/releases/<hotfix-head>` 和 isolated temporary data；不能切换
`/opt/rardar/current`、restart service 或接触 production data/D1。

本轮只创建 Draft PR 并停止。后续顺序固定为：人工审查 → Ready/Squash merge → main
Verify → 部署 exact release 且保留当前 published data → 等待下一次自然 08:00 →
`SERVER-NATURAL-RUN-02`。

## 非目标

- Scheduler retry/schedule/catch-up；
- Runtime、systemd、D1；
- 手工 refresh、current/candidate/failed-candidate 维护；
- PR #18；
- Public Edge、P1-6C2、TrendRadar/P2、评分或信源。
