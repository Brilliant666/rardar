# 2026-08-09 Remote analysis bounded completion

## 目标

本轮只修复远程只读静态分析的有界完成性：Git 浅克隆超时必须清理本次完整进程树，且大型 GitHub 官方源码归档必须在固定资源预算内产生确定性的部分静态证据。本轮不修改评分语义、Stable Project ID、页面 UI、正式数据或部署配置。

本文记录独立开发分支上的实现与验证。依赖安全 PR #11 已合并，本分支已同步包含该修复的 `main` 提交 `0de3e54`；只有本目标对应的 Analyzer PR 合并到 `main` 后，本目标才视为完成。本文不声称 Analyzer PR 已经合并或修复已经应用到 Primary Runtime。

## 触发事实与根因

2026-07-22 的真实外部 refresh 中，`n8n-io/n8n` 浅克隆达到 180 秒后，Python 只终止直接 `git.exe`。`git-remote-https` 后代仍持有继承的 stdout/stderr pipe，导致 `subprocess.run()` 的超时收尾继续等待 EOF，并留下三个后代进程。随后官方 ZIP fallback 又把 25,000 同时作为归档准入和扫描预算，在第 25,001 个合格文件处整体失败。

这两个现象都不是通过延长 timeout 或无限提高文件上限解决：前者需要明确的进程树所有权和截止时间，后者需要把“归档是否安全”与“本轮最多分析多少文件”拆成不同门槛。

## 实现边界

### Git 进程生命周期

- clone 使用 `Popen`，stdin/stdout/stderr 全部指向 `DEVNULL`，不再创建可被后代长期持有的 pipe；
- Windows 先以 `CREATE_SUSPENDED` 创建 clone，设置 `KILL_ON_JOB_CLOSE` 的 Job Object、完成 assignment 后才恢复主线程；子进程默认继承 Job 且不允许 breakaway。POSIX 使用独立 session/process group，必要时先 SIGTERM、短暂宽限后 SIGKILL；
- clone timeout 与 cleanup 各自有界；cleanup 内部共享固定的 10 秒截止时间，禁止按进程名或全机命令行批量终止 Git、Node、Python 或 Codex；
- clone 成功、非零退出、超时和 wait 异常都会在返回前检查同一 Job/process group；只有整树与 partial checkout 清理已确认，才允许读取 checkout 或进入官方 archive fallback；成功 root 留下后代会在清理后 fail closed；
- `Popen`/CreateProcess 在任何进程创建前失败时不存在待清理资源，因此可直接进入官方 archive fallback；
- 无法确认进程树、checkout 或临时目录清理时返回稳定 lifecycle error，保留可诊断临时目录，candidate fail closed，scheduler 标记 `retryable: false`；
- Windows Git 生成的自有 promisor pack 若带 `READONLY`，只在整树已退出、根和文件身份仍匹配、文件为单链接普通文件时清除此位并重试一次；leaf symlink 只删除链接自身且绝不跟随，ACL、共享占用、硬链接、junction/其他 reparse、路径逃逸或身份变化都保留原有 fail-closed 语义；
- 普通 clone、网络或 archive 失败仍保留既有 `analysisFailures` / degraded generation 语义。

### 大型官方 ZIP

- 下载先写 `source.zip.part`，Content-Length 与实际读取都受 120 MB 上限约束；
- 最多接纳 100,000 个 ZIP member，且全部成员在任何 checkout 写入前完成路径、根目录、类型、重复、碰撞和压缩方式预检；
- 硬成员门槛包含目录、跳过后缀和符号链接，不能被过滤规则绕过；
- 合格 regular file 按 NFC 规范化相对路径排序，确定性选择前 12,000 个；ZIP central-directory 顺序不影响结果；
- 只打开选中文件，选中声明与实际内容总量都受 600 MB 上限约束；每个选中 member 完整读取并核对 CRC；
- 超过 512 KB 的选中文本不保存内容，只生成空占位，但仍完成完整性读取；
- 提取写入唯一 sibling staging，所有文件成功后原子切换为 checkout；任何失败不暴露 partial checkout。

StaticEvidence 继续使用 v2 Schema，没有新增字段。发生确定性截断时，只在 `warnings` 中记录已选择文件数与合格文件总数。

## 行为验证

当前分支的聚焦验证已经覆盖：

- Windows 真实 root → child → grandchild 的超时树全部退出，root 非零退出遗留 child 也会在 fallback 前清理，独立 sibling 保持存活；成功 root 若留下 child，会在清理后返回 lifecycle failure；
- process-tree cleanup 失败不进入 archive fallback；
- lifecycle failure 使 refresh candidate 在 build 阶段失败、current 字节不变、后续项目和 signals 不执行；
- scheduler 持久化 lifecycle error code 和 `retryable: false`，同周期不重试，Runtime 状态透传；
- 普通单仓分析失败仍发布 Audit degraded、0 errors 的新 generation；
- 25,001 个合格文件不再触发旧门槛，且只物化确定性选择集合；
- 全成员上限包含目录、跳过文件和链接；尾部 traversal、多根、大小写/NFC 重复、file-directory 冲突和原始 NUL 路径在写入前拒绝；
- 选中内容预算、未选成员不读取、大文件占位、读取中断、staging identity 和原子 publish 失败；
- 下载 Content-Length、实际流量上限和 `.part` 清理；
- Windows 自有只读 promisor pack 清理、非只读 AccessDenied、外部硬链接、根身份替换和 descendant junction；普通 symlink 无创建权限时明确按平台跳过；
- 既有 checkout 原样保留。

分析器、refresh、scheduler 和 runtime 共发现 93 项聚焦测试：91 项在 Windows 通过，1 项 POSIX 专属 process-group 测试和 1 项需要普通 symlink 创建权限的测试按平台跳过。调度测试还明确证明 lifecycle failure 只禁止同一调度周期重试；有可信完成时间的前一周期失败不会阻止下一周期 catch-up，缺失或非法完成时间继续 fail closed。

PR #11 合并并同步 `main` 后，本分支使用与 CI 一致的 Node 22.13.1、npm 10.9.2 和自身隔离 Python venv 重新运行完整 `npm run verify`，七道门禁与隔离守卫全部通过：lint 通过；完整 Python 套件 300 项中 288 项通过、12 项按平台或权限跳过；Schema 校验 21 个 artifact、0 error；跨文件 Audit 为 healthy、0 error、0 warning；production build 通过；Node 与真实 Vinext HTTP/D1 共 22 项全部通过；production dependency audit 为 0 vulnerabilities。Verify 还确认仓库 data、Git 可见文件保持不变，没有遗留 Git 可见 artifact，并清理了隔离 Runtime 状态。本分支没有复制依赖改动或弱化审计门禁。

## 真实 n8n 分析器回归

2026-08-09（Asia/Shanghai）在系统临时目录对公开仓库 `n8n-io/n8n` 单独执行了两次真实 `analyze_remote`，没有读取 Primary data、没有访问 3000，也没有执行仓库代码。第一次运行使一个未被合成测试覆盖的 Windows 细节显性化：clone 以普通失败结束且 Job 内已无 Git 后代，但 partial-clone promisor pack 的 4 个 `.idx`/`.pack` 文件带 `READONLY|ARCHIVE`，普通 `shutil.rmtree` 因 `WinError 5` fail closed，并保留了 26,255 个文件、245,317,296 字节的诊断目录。该失败没有进入 archive fallback，也没有遗留 `git` 或 `git-remote-https`。

增加身份绑定的自有树只读清理和相应安全测试后，第二次真实运行在 81 秒内无需人工终止进程即可完成。浅克隆以 exit 128 普通失败，随后官方 archive fallback 通过全成员预检，从 26,191 个合格文件中确定性选择并扫描 12,000 个，输出 repository、projectId 和 Schema v2 均合法的静态证据；没有遗留 n8n Git 后代或新的 analyzer 临时目录。证据输出 SHA-256 为 `363f916f45082f9fe9a1304c414a33442a1db93b1c1a5f9df97da90e94217804`，仅用于本次脱敏验收，未提交。首次保留的诊断目录和第二次输出目录随后都通过同一身份绑定清理器验证并删除。

## 数据、Runtime 与回滚

- 所有行为测试使用临时目录、合成 ZIP 和本地睡眠进程，不读取或修改 Primary 正式 generation；
- 不运行 Primary refresh，不删除既有 failed candidates，不停止 Primary manager、website 或 scheduler；
- 不访问 3000 端口，不部署，不修改 PR #9 或 PR #10；
- 没有 Schema 或数据迁移；应用代码回滚只需回退本 PR，既有 generation 继续按原字节读取；
- 对应 PR 合并后，仍需在完整 Primary data 副本上做一次真实外部 refresh 与随机回环 HTTP 验收，确认 n8n 不再留下 Git 后代，并由独立授权流程决定是否处理 Primary staging 和刷新。

## 治理状态

本轮是对真实 refresh 事故的独立安全修复，不改变 `CODEX_MASTER_INSTRUCTION.md` 中的长期工程优先级。依赖安全 PR #11 已由人工审查并合并；本分支已同步最新 `main` 且完整 Verify 通过，下一步只创建 Analyzer Draft PR 并停止。未经用户明确批准，不得将 Analyzer PR 转 Ready、合并、部署或应用到 Primary Runtime。
