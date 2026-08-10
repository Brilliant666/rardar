# 2026-08-10 Always-on Deployment v1

## 本轮唯一目标

让 Rardar 可以在标准 Ubuntu 24.04 LTS / Debian-compatible x86_64 单机上，由 systemd 托管唯一 foreground Manager 长期运行，并具备显式持久路径、只读部署检查、停机备份和可回滚发布协议。

本轮分支：

```text
feat/always-on-deployment
```

基线是 PR #14 的 Squash merge 提交 `e61e3ff35390ab9f915818f72e5e3321896fd17e`。该提交的 main push Verify run `31351088836` 为 `SUCCESS`，因此 Runtime Operational Readiness 已完成。P1-6C2 collision history 仍未完成并继续 deferred；Always-on v1 不放宽 legacy slug collision gate，也不代表 P1-6 或 Phase 0 整体完成。

## 范围边界

本轮完成可部署工程化。本轮不执行真实部署。明确不做：

- SSH、真实服务器状态修改、DNS、TLS、Caddy/Nginx 正式配置或防火墙变更；
- 生产 secret、正式域名或 Primary data/D1 迁移；
- 手工 refresh、Scheduler `--once`、修改 `nextRunAt`、扩展 catch-up；
- failed candidate cleanup；
- P1-6C2、TrendRadar/P2、新评分、新信源、复杂 Agent；
- Ready、merge 或自动开始 `PROD-DEPLOY-01`。

## Discovery matrix

| Concern | `e61e3ff` 行为 | 风险 | Always-on v1 契约 | 行为验证 |
| --- | --- | --- | --- | --- |
| Service owner | 本地后台 Manager 看护 Website/Scheduler | systemd 若分别管理 child 会形成双 owner | systemd 只拥有 foreground Manager | unit 静态检查、start/restart/stop PID 矩阵 |
| Website entry | Manager 原使用 `vinext dev` | `vinext start` 尚不能消费当前 Cloudflare 构建，默认 CLI 还会覆盖隔离端口 | Manager 直接运行 Vite runner/host/configured port/strictPort，由配置加载 Vinext 插件；build 仍为门禁 | 真实 HTTP、loopback owner、随机端口、build |
| Filesystem | 本地默认允许相对 `data/` 与项目内 state | release 切换可能覆盖 mutable facts | exact release 与 data/D1/runtime/locks/cache/backup 分离；Vite cache 外置到 `RARDAR_VITE_CACHE_DIR/node_modules/.vite`，无需写 release `.vinext`/`.vite-temp` | absolute/overlap/symlink/permission tests |
| D1 | Vinext state 可由本地默认位置决定 | release 更换或缓存清理可能丢失数据库 | 强制外置完整 `RARDAR_VINEXT_STATE_DIR` | SQLite read-only integrity、restart persistence |
| Preflight | `verify` 面向仓库与隔离测试 | 不能证明目标 host 的持久 state 可启动 | offline check 校验 release、路径、generation、D1、磁盘 | fail-closed、零写入测试 |
| Health | `/api/health` 区分 fresh/stale/invalid | 只测 TCP 或单端点可能漏掉 owner/数据代不一致 | online check 绑定 PID、listener、Runtime、HTTP 和 generation | fresh/stale accepted，invalid/public/mismatch rejected |
| Release | 本地开发 worktree | active `git pull` 难以审计和回滚 | exact commit 的真实 release 目录 | release 文件、toolchain、回滚 preservation |
| Backup | 尚无 Linux 停机协议 | data 与 D1 不同时间点会破坏身份/行动事实 | 同一停机点成对备份 data 与完整 D1 state | fixture checksum、SQLite/generation revalidation |

## Vinext compatibility 决策

本轮实际验证发现，`npm run build` 可以成功，但当前本地 Node `vinext start` 会因构建产物中的 `cloudflare:` URL scheme 不受支持而无法启动。现有 `vinext dev` 已覆盖 generation host bridge、D1、fresh/stale/invalid 和真实 HTTP。

因此 v1 不虚构 production start 支持，而是让 systemd Manager 直接运行 `vite --configLoader runner --host 127.0.0.1 --port <configured> --strictPort`，由 `vite.config.ts` 加载 Vinext/Cloudflare 插件。runner、外置 `RARDAR_VITE_CACHE_DIR/node_modules/.vite` 与系统字体避免运行期写 release 的 `node_modules/.vite-temp`、`.vinext/fonts` 或其他 `.vinext` 内容；offline checker 同时拒绝 release-local `.env*`/`.dev.vars*`。`npm run build` 仍是完整 Verify 与 release 的硬门禁；3000/3002 不得直接暴露公网。以后替换 compatibility entry 必须作为独立目标，重新通过同一 generation、D1 和 HTTP 门禁。

## 架构与配置

```text
systemd
  └─ pipeline.runtime service
       ├─ Vinext website on loopback
       └─ one pipeline.scheduler
```

Always-on v1 的路径 profile 由 checker、EnvironmentFile 与 systemd unit 共同固定：

```text
RARDAR_HOME=/opt/rardar/current
RARDAR_DATA_DIR=/var/lib/rardar/data
RARDAR_RUNTIME_DIR=/var/lib/rardar/runtime
RARDAR_VINEXT_STATE_DIR=/var/lib/rardar/vinext-state
RARDAR_DATA_LOCK_DIR=/var/lib/rardar/locks
RARDAR_VITE_CACHE_DIR=/var/cache/rardar/vite
RARDAR_BACKUP_DIR=/var/backups/rardar
WRANGLER_LOG_PATH=/var/log/rardar/wrangler
WRANGLER_REGISTRY_PATH=/var/lib/rardar/runtime/wrangler-registry
MINIFLARE_REGISTRY_PATH=/var/lib/rardar/runtime/miniflare-registry
RARDAR_NODE=/usr/bin/node
RARDAR_PYTHON=/opt/rardar/current/.venv/bin/python
RARDAR_VINEXT_PORT=3000
RARDAR_RUNTIME_STATUS_PORT=3002
```

除 `RARDAR_HOME` 外，所有显式路径必须绝对、预先存在、在 unit 允许的边界内互不冲突且不经过 symlink。`RARDAR_HOME=/opt/rardar/current` 可以只在最终 `current` 路径组件使用原子 symlink；祖先必须不是 symlink，解析目标必须是运行 checker 的 exact release，且必需 release 文件不得是 symlink。Manager 在任何进程或状态写入前加载一次配置，并把同一份 home、data、runtime、lock、port 和 schedule 契约传给两个 children。真实 secret 只允许进入版本控制外的受限 EnvironmentFile。自定义目录必须留给后续独立生成并审查 unit/drop-in、checker、权限、备份与回滚协议的目标，不能在 v1 中只改环境变量。

## Deployment checks

offline check 只读验证：

- Python/Node identity 和最低版本；
- exact release 必需文件、锁定依赖与 build；
- 持久目录的路径安全、读写权限和磁盘空间；
- current pointer、ready manifest、manifest digest、全部 artifact hash、Schema 和 Audit；
- 完整 Vinext state 中至少一个 Rardar SQLite；source main 与现有 `-wal`/`-journal` 经身份和字节稳定校验复制到系统临时 scratch，WAL recovery、`quick_check` 与表指纹只在副本执行。

online check 先重复 offline gates，再验证：

- Manager、Website、Scheduler 三个不同且 live 的 PID；
- 命令身份和 Runtime telemetry；
- Website/status listener 只在 loopback 且归预期 PID；
- `/api/health`、`/`、`/signals`、`/search`；
- filesystem、health、Runtime status 使用同一 generation；
- 检查期间 pointer 不切换。

两种 check 都没有 repair mode，不会创建目录、启动服务、refresh、derive、publish、rollback、迁移或清理 candidate。
它们不通过 SQLite 连接正式 D1，不在 source 旁生成/更新 `-shm`，也不 replay 或 checkpoint source；systemd 的 `PrivateTmp` 隔离检查副本，副本不稳定或复制失败会明确 fail closed。

## Backup、rollback 与 catch-up

部署/升级前必须停止 Managed Runtime，并用同一个 backup ID 保存完整 data 与完整 Vinext/D1 state。回滚分三类：

1. code rollback：恢复上一 exact release，默认保留 data/D1；
2. generation rollback：只用现有显式、受锁且完整复核的 retained generation rollback；
3. data+D1 restore：只在持久 state 失败时使用同一停机点备份成对恢复，不拼接不同时间点。

既有 12 小时 restart catch-up 保持不变。真实停机后的启动可能自然触发 refresh，并在门禁通过后推进 current；操作者必须记录 pre/post generation 和 Scheduler telemetry。若 online check 与自然发布竞争，应等待 Scheduler 完成后重试，不修改 `nextRunAt`、不手工 refresh、不回滚健康发布，也不清理 failed candidates。

## 文档与交付物

- `.env.production.example`：安全变量示例，不含真实 secret；
- `deploy/systemd/rardar.env.example`：目标 Linux EnvironmentFile 示例；
- `deploy/systemd/rardar.service`：non-root、foreground、single-Manager unit；
- `docs/DEPLOYMENT.md`：安装、环境、systemd、访问、检查、备份、升级、回滚、health、scheduler 与 troubleshooting；
- README 和治理文档：PR #14 完成状态、当前唯一目标与真实部署边界。

## 验证要求

测试只能使用当前 worktree 的 `.venv`、临时 data/D1/runtime/locks/cache 和随机 loopback 端口；不得访问 Primary Runtime、正式 data 或 3000/3002。

至少覆盖：

- 路径/env/port/toolchain validation；
- code/mutable state 分离与 D1 SQLite integrity；
- `systemd-analyze verify` 与 systemd unit 静态契约；
- Manager foreground、SIGTERM、restart 和 single Scheduler；
- offline 零写入与 online PID/listener/generation/HTTP；
- fresh/stale accepted，invalid/corrupt/public listener rejected；
- backup/rollback preservation；
- 完整 `npm run verify`、`git diff --check`、无 formal data change、无残留进程或端口。

最终完整验证记录：

```text
npm run verify: PASS
Python: 437 项，411 PASS，26 platform/capability skip
Node: 73/73 PASS
Schema: healthy，21 validated，0 error
Audit: healthy，0 error，0 warning
Production build: PASS
Production dependency audit: 0 vulnerabilities
Isolation guards: repository data、Git-visible bytes/status、Runtime state cleanup 全部 PASS
systemd-analyze verify: Windows 本地明确跳过；Ubuntu GitHub Verify 必须实际执行
```

## 是否影响 North Star

不改变 Weekly Acted Projects、行动 Event/State、评分、Stable Project ID 或 generation 发布语义。该轮只让同一套证据和事实边界能够在长期 Linux Runtime 中被部署前验证、失败时诊断并按明确层级回滚。

## Draft PR

```text
本提交推送后创建 Draft PR；编号、URL、最终 head 与 CI 结论记录在 GitHub PR 元数据和最终交付报告中，避免在提交内容中形成自引用。
```

创建 Draft PR 后停止。不得转 Ready、合并、执行真实服务器部署或开始 P1-6C2/TrendRadar/P2。
