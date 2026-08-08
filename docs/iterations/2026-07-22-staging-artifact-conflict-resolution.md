# 2026-07-22 staging artifact 冲突解析

## 目标与边界

本轮针对 2026-07-17 至 2026-07-22 连续 18 次 refresh build 失败，建立单 repository、单 artifact、显式人工决策的 staging 冲突 resolver。fail-closed 的 candidate adoption 本身没有错误：它正确阻止了非等价 legacy v1 覆盖 current ready generation 中的 stable v2。

本轮不修改 Primary Runtime、正式 `data/`、18 个 failed candidates、PR #9 或长期开发 worktree；不访问 3000 端口，不触发正式 refresh，不部署、不合并，也不开始 P1-6C。所有 apply 和 refresh 证明只允许发生在 Primary `data/` 的一次性完整副本。

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

## 真实外部边界演练（降级，不计作严格 PASS）

为补足上述 deterministic 演练没有覆盖真实 GitHub、信号源和 HTTP 端口的限制，2026-08-09（Asia/Shanghai）又从 Primary `data/` 新建一份完整临时副本。复制前后均为 825 个文件、28,018,284 字节，全树 SHA-256 指纹为 `44fdf5c76e4fa2d8396b679c7e2b236bfe2442d4dba4474461c3ca9e23bed6c6`。三个 resolver 再次完成 dry-run、apply 和 no-op；副本只移出三个已审查 legacy analysis，原 current、5 个 retained generations 和 18 个 failed candidates 字节不变。

本次没有 stub `collect`、`collect_signals` 或 `analyze_remote`：scheduler 子进程使用当前 `gh` 身份的短期 `GITHUB_TOKEN`，执行真实 9 条 GitHub Search、真实外部信号采集和 Top 5 公开仓库静态分析。Vinext 同时只监听随机回环端口 `64424`，`RARDAR_DATA_DIR` 指向副本，`RARDAR_VINEXT_STATE_DIR` 指向临时 Miniflare state，`CLOUDFLARE_VITE_FORCE_LOCAL=true`；因此没有连接正式 D1，也没有访问或占用 Primary 3000。

真实外部结果：

- 9/9 GitHub Search healthy，得到 183 个候选；snapshot、catalog、signals 和 queue 时间均为 `2026-08-08T18:27:57.033718+00:00`；
- 5 个静态分析目标中 4 个产生本轮 Schema v2 evidence：`trycompai/crm`、`yc-software/qm`、`microsoft/skill-recorder` 和 `OpenHands/OpenHands`；
- `n8n-io/n8n` 的 shallow clone 在 180 秒后超时。Windows 上直接 clone 进程退出后，三个 `git-remote-https` 后代仍持有管道和网络连接，scheduler 只有心跳而不推进；本次只终止了这三个已核验的隔离 clone 后代，使现有官方 codeload fallback 得以继续。fallback 随后按安全上限拒绝超过 25,000 个文件的 archive，因此 catalog 正确记录 1 个 `analysisFailure`，没有伪造新 n8n evidence；
- 6 个信号源中 5 个 healthy；Hugging Face Blog 在 45 秒网络超时后记录为 failed，其他 OpenAI News、GitHub Changelog、AI News Radar、OpenGithubs Daily Rank 和 HelloGitHub 正常；
- scheduler 发布 generation `20260808T182757981298Z-c6581c48d8e8`，manifest 为 ready/refresh，`previousGenerationId` 和 `baseGenerationId` 均为 `20260716T000001945465Z-d7223e00847a`；Schema validation healthy（33 artifacts），Audit 无 error 但因上述两项产生 2 个 warning，状态为 degraded；
- 服务不重启即可从旧 generation 切换到新 generation。`GET /api/health`、`/`、`/signals`、`/search` 和使用临时本地 D1 的 `/api/actions` 均为 200；
- 发布后三个 resolver 再次全部 no-op；旧 5 个 retained generations、18 个 failed candidates 和 flat staging（除三个显式移出的 legacy 文件）逐字节不变；
- 隔离 Vinext、64424 端口、临时 D1、data copy、审计归档和分析临时目录均已清理。Primary 指纹、main HEAD、Manager/Website/Scheduler PID、3000 listener 和正式 generation 全部不变。

因此，本次真实外部演练证明：三个 staging identity conflict 不再阻止 candidate 构建和 ready generation 发布，随机端口消费者也能原子读取新 generation；但它**不能**证明无人干预且所有外部来源均 healthy 的严格验收。n8n clone 超时后的 Windows 子进程清理，以及 Hugging Face 的外部网络可用性，仍是独立于 resolver 的运行风险，必须在把本轮描述为完整外部 PASS 之前另行解决或明确接受 degraded 发布语义。

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

`npm run verify` 的结果：

- 使用本 worktree `.venv` 与 Node `v24.14.0` 执行；Lint、299 个 Python 测试（13 个环境相关 skip）、Schema validation、Data Audit、生产构建和 22 个 Node/真实 Vinext HTTP 测试全部通过；
- Verify 的 data、Git-visible 文件、临时 artifact 和隔离 Runtime guard 全部通过；
- 唯一失败阶段是 `npm run security:audit:prod`：未改动的 dependency lock 当前报告 `next@16.2.6`、`postcss@8.5.14`、`nanoid@3.3.12` 与 `sharp@0.34.5` 共 4 个 high severity package advisory；这些公告发布于 PR #9 的 2026-07-17 Verify 之后，不是 resolver 引入的依赖变化；
- npm 的完整修复建议会升级到 `next@16.3.0`，并需同步处理显式 PostCSS override 与 lockfile。依赖升级不属于本单 artifact Hotfix 的已授权提交范围，也不能通过降低审计门禁掩盖，因此本轮不修改依赖；Draft PR 必须保持阻塞并如实记录本地与远端 Verify。

本分支只在 `package.json` 增加 resolver 命令，没有修改 dependency 版本或 `package-lock.json`。供应链公告是当前 main 依赖基线问题，不是 resolver 引入的回归。
