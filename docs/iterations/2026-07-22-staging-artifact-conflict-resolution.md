# 2026-07-22 staging artifact 冲突解析

## 目标与边界

本轮针对 2026-07-17 至 2026-07-22 连续 18 次 refresh build 失败，建立单 repository、单 artifact、显式人工决策的 staging 冲突 resolver。fail-closed 的 candidate adoption 本身没有错误：它正确阻止了非等价 legacy v1 覆盖 current ready generation 中的 stable v2。

本轮不修改 Primary Runtime、正式 `data/`、最终验收时存在的 21 个 failed candidates（原 18 个加 2026-08-09 新增 3 个）、PR #9 或长期开发 worktree；不访问 3000 端口，不触发正式 refresh，不部署、不合并，也不开始 P1-6C。所有 apply 和 refresh 证明只允许发生在 Primary `data/` 的一次性完整副本。

## 权威证据结论

### n8n-analysis

结论：`KEEP_STABLE_ARCHIVE_LEGACY`。

| 证据 | legacy v1 | stable v2 |
| --- | --- | --- |
| repository | `n8n-io/n8n` | `n8n-io/n8n` |
| 路径 | `data/analysis/n8n-io--n8n.json` | `data/generations/20260716T000001945465Z-d7223e00847a/analysis/n8n-io-n8n--7da69635176c3b46af22.json` |
| Schema | 1 | 2 |
| projectId | 机械映射为 `n8n-io-n8n--7da69635176c3b46af22` | `n8n-io-n8n--7da69635176c3b46af22` |
| 文件 SHA-256 | `5242ac77837d5666b69ba7d3e30e9132d9d4a014652c7c7e8e3bd08a10f5c14b` | `cdce4c6e3fd427b26096ba1641cc1193ea8367e743e35eae4dc3399bb3e0d305` |
| payload normalized SHA-256 | `1517039585d5303abeab367f82a76a6444e5217b2fe81b4b0daaf9c97c5bb224` | `1f740463c375da651c1da5d76b4e8139d01328cdf3c67388718d69232e2fa8a7` |
| 机械 v2 normalized SHA-256 | `46f5c9a175420c88ca33af5fc775eb73431d975303416df9086ca7209a14d9c2` | 同上 |
| analyzed_at | `2026-07-11T00:02:46.409131+00:00` | `2026-07-16T00:02:49.538331+00:00` |
| 配对 snapshot pushed_at | `2026-07-10T23:35:12Z` | `2026-07-15T23:29:07Z` |
| source | `https://github.com/n8n-io/n8n` | `https://github.com/n8n-io/n8n` |

机械补齐身份字段后，事实差异仅为：`test_files` 5298→5337、`todo_markers` 471→472，以及 `.json` 764→696、`.md` 475→474、`.mjs` 150→148、`.ts` 9947→10029、`.vue` 27→11、`.yml` 260→269、`[none]` 65→64。stable 数据来自完整验证通过的 ready generation；manifest、全部 artifact hash、Schema 与跨文件 Audit 均健康。legacy 没有 stable 之后新增的可信事实。

时间线：

1. `20260711T183728486430Z-72ff8eefea0d`（bootstrap）首次保留 legacy SHA `5242ac…`；
2. `20260713T191537075729Z-4e2e9d09fae2`（derive）仍保留同一 legacy 字节；
3. `20260714T043706813062Z-2ce7a1c15e21`（refresh）产生更新 v1 SHA `04eb8aaa…`；
4. `20260715T000000899395Z-29832078140f`（refresh）产生更新 v1 SHA `91c5004f…`；
5. `20260716T000001945465Z-d7223e00847a`（refresh）产生当前 stable v2 SHA `cdce4c…`。

flat legacy 当前字节仍与 Git HEAD/index 相同，最后来源是 PR #2 的历史数据提交，而不是 stable 生成后的重新采集。18 个 failed candidates 都以 current stable 为 base：其 manifest 共核对 666 个 artifact hash，无不匹配；candidate 先复制 current stable，再 overlay 文件名不同的 flat legacy，因此在 adoption 阶段稳定复现同一冲突。文件系统 mtime 仅作为辅助线索，没有用作权威依据。

project enrichment 不是本次冲突：其 v1 机械转换后与 stable v2 完全等价，canonical SHA 均为 `8a1c8ba4a58c2eddf64a549f152e30f0adef995c112a0255db14a535aa8de5b8`。

### openhands-analysis

此冲突只是在隔离副本移除 n8n legacy 后顺序暴露，不授权修改 Primary：

- legacy：`data/analysis/openhands--openhands.json`，SHA `e357ab3fafe2fe28fd52c3d58a34644c63b19ddd08d4f9d4367c6793bfa6a86f`，analyzed `2026-07-11T00:03:11.577692+00:00`，配对 pushed `2026-07-10T23:37:20Z`；
- stable：generation `20260716T000001945465Z-d7223e00847a`，SHA `0c897c8fb4436789e81d78d496643554ceaefb0bc304ae85992200d673940b39`，analyzed `2026-07-16T00:03:30.674675+00:00`，配对 pushed `2026-07-15T23:35:38Z`；
- repository/source 均为 `OpenHands/OpenHands` / `https://github.com/OpenHands/OpenHands`，projectId 为 `openhands-openhands--e6277a5ec8933fa49612`；
- stable 的 `scanned_files`、`test_files`、`.py`、`.ts`、`.tsx` 分别为 2551、595、909、529、753，高于 legacy 的 2547、593、907、528、752。

副本演练的显式结论同样为 `KEEP_STABLE_ARCHIVE_LEGACY`。

### openscience-analysis

此冲突也只是在隔离副本中顺序暴露：

- legacy：`data/analysis/synthetic-sciences--openscience.json`，SHA `5210acab6ae446e48cdc7d20304ecd97311416da5ce1158b6007b5049bd76c5e`，analyzed `2026-07-11T00:01:14.158658+00:00`，配对 pushed `2026-07-10T18:32:16Z`；
- stable：generation `20260716T000001945465Z-d7223e00847a`，SHA `0aab6b47270089da2bee89eb78bedcb5c7f4cff51f7721673f6a35e73c1700fa`，analyzed `2026-07-16T00:01:04.459571+00:00`，配对 pushed `2026-07-11T11:04:02Z`；
- repository/source 均为 `synthetic-sciences/openscience` / `https://github.com/synthetic-sciences/openscience`，projectId 为 `synthetic-sciences-openscience--a389c9ae83b9b35fd548`；
- stable 的 `scanned_files`、`test_files`、`.ts` 分别为 3833、150、609，高于 legacy 的 3828、145、604。

副本演练的显式结论同样为 `KEEP_STABLE_ARCHIVE_LEGACY`。

## Resolver 协议

`pipeline.resolve_project_artifact_conflict`：

- 默认 dry-run；apply 必须显式给出 repository、kind、decision、legacy/stable 双 SHA，以及本文件中的非秘密证据引用；analysis 的 KEEP/PROMOTE 还必须给出与两端 snapshot 精确匹配的 legacy/stable `sourcePushedAt`；来源版本无法证明时，显式 BLOCKED 可报告 `sourceVersions: null` 和退出码 2，但不放宽 current、hash、Schema、身份或 URL 门禁；
- 只接受 `keep-stable`、`promote-legacy`、`blocked`，不提供通配符、批量模式或 `newest-wins`；
- 在 canonical data lock 内每次验证 current ready generation 的 pointer、manifest digest、全部 artifact hash、Schema、Audit、repository、projectId、文件名、source URL、两端 snapshot 版本、来源时间与 expected SHA；
- 证据引用必须是 worktree 内真实存在的 UTF-8 regular Markdown 及真实 heading anchor；证据、snapshot、audit record 与 archived bytes 都通过 no-follow open 绑定文件身份，prepared/resolved 记录绑定证据 SHA-256，证据文档改写后旧审计不能继续使用；
- 首次 apply 在仓库外先保留 legacy 原始字节和 prepared 审计；PROMOTE 再以原子 no-replace 创建机械 v2。legacy 以 no-replace move 进入同目录 quarantine 并核对精确字节，再复核 current/retained generation、flat stable、证据 SHA 与来源版本；通过后把同一文件对象原子移入同一文件系统上的外部归档 `detached-legacy.json` 并永久保留，不执行 pathname unlink；竞态换入的新内容只会被保留或安全失败，最后才推进 resolved；
- prepared 审计冻结已验证的 legacy sourceVersions；中断重试先重新验证健康 current 和审计绑定的 retained generation，只重验 retained stable 来源，不把旧 legacy 决策重新绑定到随后前进的 flat snapshot 或新 current；第二次 apply 为 no-op，PROMOTE no-op 仍检查 flat postcondition；
- 路径逃逸、Git/data 内归档、symlink、junction/reparse point、非等价或竞态出现的 stable target、未知时间关系全部 fail closed；
- 永远不修改 current pointer、retained generations、candidates、failed candidates 或 manifest。

分析执行时间仅是程序化防回退门槛，不独立证明源码版本权威；本节记录的 generation、snapshot pushed_at、payload 差异和 SHA 才是 `KEEP_STABLE_ARCHIVE_LEGACY` 的人工证据。

## 隔离完整副本演练

2026-08-09（Asia/Shanghai）基于当时 Primary 完整 `data/` 新建系统临时副本，并在隔离的 `LOCALAPPDATA`、`RARDAR_DATA_DIR`、`RARDAR_RUNTIME_DIR` 下重新执行。未复制或访问 D1，`urllib` 与 socket 建连均被显式阻断；仅 Stub `pipeline.refresh.collect`、`collect_signals` 和 `analyze_remote` 三个外部边界，调度与发布仍走真实 `scheduler.run_cycle → refresh → candidate → Schema/Audit → publish` 路径。

结果：

- 三个已分别审查的 analysis 冲突都依次完成 `dry-run → applied → no-op`，`legacy.json` 与永久保留的 `detached-legacy.json` SHA 都与各 legacy expected SHA 完全相同；
- resolver 后副本 `data/` 只少了三个明确的 legacy analysis 文件；`current.json`、所有 retained generations 和 18 个原有 failed candidates 字节不变；
- refresh 从 `20260716T000001945465Z-d7223e00847a` 发布到一次性副本 generation `20260808T174213931329Z-e32582375a84`，manifest 为 ready/refresh，`previousGenerationId` 正确；
- snapshot 与 catalog 从 `2026-07-16T00:00:00.015326+00:00` 同步前进到本次隔离 scheduler 的 2026-08-08 17:42 UTC 采集时点，上一代增长基线保持精确绑定；
- refresh 的 `analysisFailures` 为空；新 generation 中 project artifact 不再含 Schema v1。既有 Schema v0 按长期契约原样保留，没有被猜测迁移；
- Python API Audit、`python -m pipeline.schema_validation --data-dir <copy>` 与 `python -m pipeline.audit_data --data-dir <copy>` 全部 healthy/pass；
- 发布后不重启进程，对三个原决策再次 apply 均返回 no-op，证明幂等不依赖 current generation 保持不变；
- refresh 后 flat staging 与 resolver 完成时逐字节相同；旧 retained generations、18 个 failed candidates 的 684 个文件和 Primary 全树指纹逐字节不变；
- 临时副本、隔离 runtime、data lock 与审计归档已全部自动清理，没有启动服务或占用端口。

演练中最先处理 n8n 后，OpenHands 和 openscience 冲突按顺序暴露。它们没有被批量或自动“选新”；本文件先逐项核对 repository、projectId、ready generation、双 SHA、analyzed time、snapshot pushed time 和事实差异，随后才在一次性副本中分别调用单仓库 resolver。

## 真实外部完整副本演练（发布成功，信号源降级）

PR #11（`0de3e54`）和 PR #12（`aa9c83e`）合并后，hotfix 分支 clean rebase 到最新 `main`。2026-08-09（Asia/Shanghai）从 Primary `data/` 新建一次性完整临时副本；复制前 Primary、复制结果和复制后 Primary 都是 939 个文件、31,874,130 字节，全树 SHA-256 为 `44b392528ab9a2b5f6978f15c571f278b2be387925866aef7ee99eca9eb955d7`。副本包含 5 个 retained generations 和 21 个 failed candidates。

三个 resolver 再次逐项完成 `dry-run → applied → no-op`。dry-run 没有创建归档或修改 data；apply 只移出三个已审查 legacy analysis；no-op 不再写入。外部归档为 3 个 entry、9 个文件、10,795 字节，SHA-256 为 `83386bfbfe96b2725107195be09229e58bf34eebe64cf068248dee0d316e50a7`；每项 `legacy.json` 和永久保留的 `detached-legacy.json` 都与 expected legacy SHA 精确一致，审计状态均为 resolved。原 5 个 retained generations 保持 118 个文件、4,350,767 字节、SHA `9f5507518c685cec74d7f1a21bebc94495fec08e6f88c0688b080edddc9b4925`；21 个 failed candidates 保持 798 个文件、26,990,922 字节、SHA `b8ccb0cbd3d27684a1e0df697cbc9f44ad1d1c3a8550443f18c86a71c3c81c86`。

演练没有 stub `collect`、`collect_signals` 或 `analyze_remote`。隔离 scheduler 真实执行 `run_cycle → refresh → candidate → Schema/Audit → publish`，只把当前 `gh` 身份的短期 `GITHUB_TOKEN` 作为子进程环境变量传入；所有 data lock、runtime、临时目录、Wrangler/Miniflare state 和 D1 都位于高熵系统临时根。Vinext 始终由同一 PID 监听 `127.0.0.1:59780`，没有访问或占用 Primary 3000。

第一次真实 refresh 的结果：

- 9/9 GitHub Search healthy，得到 183 个候选；Top 5 静态分析没有 failure，`analysisFailureCount=0`、`staticAnalysisRequiredCount=0`；
- `n8n-io/n8n` shallow clone 以 exit 128 结束后，由 PR #12 的有界 official archive fallback 自动继续；没有人工终止 Git 进程或后代，最终产生本轮 Schema v2 evidence，确定性扫描 26,191 个合格文件中的前 12,000 个；
- 发布 generation `20260809T064423351957Z-1d71d2452f3d`；manifest 为 ready/refresh，`baseGenerationId` 和 pointer `previousGenerationId` 都是 `20260716T000001945465Z-d7223e00847a`，snapshot 与 catalog 的 `capturedAt` 同为 `2026-08-09T06:44:22.268455+00:00`；
- Schema validation healthy（33 个文件、0 error）。Audit 为 0 error、1 aggregate warning：6 个信号源中 Hugging Face Blog 连接超时，AI News Radar 被远端断开，故 4 healthy / 2 failed，状态如实为 degraded；
- 发布后不重启 Vinext，`GET /api/health`、`/`、`/signals`、`/search` 都返回 200；健康端点和首页读取新 generation。临时 D1 的 `/api/actions` 首次写入 `recorded=true`，相同幂等键重放 `idempotentReplay=true`，GET 可读回一条 Event 和一条 State；
- current 切换后，三个 resolver 原参数再次 apply 均为 no-op，继续绑定原 retained generation `20260716T000001945465Z-d7223e00847a`。

为排除瞬时信号源故障，在同一隔离副本上只重试一次完整 scheduler refresh。第二次同样是 9/9 GitHub 查询、0 analysis failure，并发布 ready/refresh generation `20260809T065803120955Z-805fa5a1f330`；其 base 和 previous 都精确指向上一演练 generation。AI News Radar 恢复，Hugging Face Blog 仍在 45 秒后以 `WinError 10060` 超时，因此 Audit 仍为 degraded、0 error、1 warning，但改善为 5 healthy / 1 failed。Schema validation healthy（37 个文件、0 error）。同一 Vinext PID 再次无需重启便由健康端点和首页看到第二个 generation；其他页面仍为 200，三个 resolver 在第二次 pointer 切换后仍全部 no-op。

最终 generation 的 pointer/manifest digest、ready 状态、36 个 artifact hash 和跨文件关系全部通过；growth chain 从 `2026-08-09T06:44:22.268455+00:00` 精确前进到 `2026-08-09T06:58:01.915455+00:00`，上一 snapshot 以原字节进入 history。原 5 个 retained generations 和 21 个 failed candidates 的上述指纹保持不变；三个 active legacy 都不存在，审计和 detached postcondition 均成立，无 quarantine。scheduler 自行结束后没有 Git、`git-remote-https` 或 analyzer 进程/临时目录残留。

验收结束后，只终止已记录的隔离 Vinext/workerd 进程树；59780 监听关闭。1,029 个演练文件的精确 GitHub token 与常见凭据模式扫描均为 0，临时根随后由 identity-bound owned-tree 清理器删除。Primary 后置状态仍为 939 个文件、31,874,130 字节和同一全树 SHA；current SHA `e249460ce5ec538e20ba80ccd948a3943424a16cbd83dab9341d1c82d7d7c284`、正式 generation、5 个 retained generations、21 个 failed candidates、Manager/Website/Scheduler PID、3000 listener 与 `/api/health` generation 全部不变。

因此，staging resolver 和 PR #12 Analyzer 的目标已经由无人干预的真实 refresh、ready publish、运行中 pointer 切换和 D1 HTTP 链路证明。唯一没有达到“六个外部信号源全部 healthy”的严格条件是 Hugging Face Blog 的重复网络超时；它没有造成 Schema/Audit error，也没有阻止 fail-closed generation 发布。本记录将总体外部结果明确标为 **degraded**，不把第三方网络可用性伪装成严格全源 PASS，也不为此扩大 resolver hotfix 范围。

## 行为测试

`pipeline.test_resolve_project_artifact_conflict` 使用最小脱敏 n8n 形状，而不是提交 Primary 业务数据。49 项测试覆盖：

- dry-run 零 data/archive 写入、双 SHA、repository/path traversal、current/projectId 严格失败；
- data/Git worktree 内归档、symlink、Windows junction/reparse point；
- KEEP 原字节归档、证据 Markdown/SHA 与两端 snapshot 版本绑定、严格审计、published/真实 `.candidates` 字节不变、后续 derive；
- PROMOTE 较新 v1 的机械 v2、原子 no-replace、后续 derive/Schema/Audit；
- 第二个真实 repository、enrichment kind、机械等价 facts 正常 adoption、BLOCKED 零写入、相同时间拒绝与非等价 flat stable 拒绝；
- archive/prepared、原子 quarantine/detachment、cleanup 与 resolved 审计中断窗口、current 切换/损坏后的 retry/no-op；
- final legacy swap 恢复、late KEEP target、PROMOTE postcondition 与结构化 CLI 错误；
- 审计精确字段、严格整数 Schema version、无额外字段；
- stable 创建失败时，legacy 与 prepared 归档保持可重试；resolved 必须保留 detached postcondition；audit/archive/evidence/snapshot 的晚替换、链接和同长度原地改写都 fail closed。

当前 Windows 会话没有创建普通 symlink 的权限，该用例安全 skip；无需管理员权限的 junction 用例实际执行并通过。

## 验证状态

最终实现先对 Primary `data/` 做全量只读比较，确认 5 个 analysis v1 配对中 2 个机械等价、3 个非等价，4 个 enrichment 配对全部机械等价且没有未配对 v1；随后只在完整副本中对三个冲突执行 resolver。正式 data 未调用 apply，Primary 全树前后指纹一致。

PR #11 已修复并合并生产依赖安全升级，PR #12 已合并 bounded remote analysis。hotfix clean rebase 到 `aa9c83e` 后，在 Node `v22.13.1`、npm `10.9.2` 和本 worktree `.venv` Python `3.10.10` 上执行 `npm ci` 与完整 `npm run verify`：

- Lint 通过；Python 349 项（334 pass、15 个平台或权限相关 skip）通过；
- Schema validation 验证正式 current 的 21 个 artifact，0 error；Data Audit healthy；
- 生产构建通过；22 个 Node/真实 Vinext HTTP/D1 测试全部通过；
- `npm run security:audit:prod` 为 0 vulnerabilities；
- repository data、Git-visible 文件、临时 artifact、隔离 Runtime 和本地服务 guard 全部通过。

上述结果来自文档收尾后的最终提交树，完整 Verify 用时约 12 分钟；PR 保持 Draft，不部署、不合并，也不修改 Primary Runtime 数据。
