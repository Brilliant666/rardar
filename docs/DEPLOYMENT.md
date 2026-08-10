# Rardar Always-on Deployment v1

本文定义 Rardar 在单台 Linux 主机上的第一版可部署工程和操作者协议。它是运行手册，不是已经完成生产部署的证明。

## 1. 支持范围

目标环境：

```text
Ubuntu 24.04 LTS 或 Debian-compatible x86_64
Node.js >= 22.13.0
Python >= 3.10
systemd
single host
local persistent filesystem
```

Always-on v1 提供：

- systemd 管理的唯一 foreground Manager；
- Manager 管理的 Website 与唯一 Scheduler；
- exact code release 与 mutable state 分离；
- 版本控制的 systemd/environment 示例；
- 只读、fail-closed 的 offline 与 online deployment check；
- 停机备份、升级、三类回滚和故障排查协议；
- loopback 网站访问、SSH tunnel 和反向代理 sample 的边界。

Always-on v1 不执行：

- SSH 到真实服务器；
- DNS、TLS、Caddy/Nginx 正式配置或防火墙修改；
- 生产 secret 写入；
- Primary data 或 D1 向服务器迁移；
- 手工 refresh、修改 `nextRunAt` 或自动 catch-up 扩展；
- failed candidate cleanup；
- P1-6C2、TrendRadar/P2、新评分或新信源。

以上真实基础设施动作属于后续单独授权的 `PROD-DEPLOY-01`。

## 2. 架构与单一 ownership

```text
systemd
  └─ pipeline.runtime service       foreground Manager，唯一 systemd service
       ├─ Vinext website             127.0.0.1:3000
       ├─ pipeline.scheduler         唯一 Scheduler owner
       └─ Runtime status             127.0.0.1:3002

persistent state
  ├─ audited generation data
  ├─ Vinext/Miniflare D1 state
  ├─ Runtime telemetry and locks
  ├─ cache and logs
  └─ stopped-state backups
```

systemd 只管理 Manager。禁止再创建一个 systemd Scheduler service、cron refresh 或其他与 Manager Scheduler 并行的 owner。Manager 在启动任何 child 前验证环境、路径、端口和依赖；同一 runtime 或 canonical data directory 的第二个 Manager/Scheduler 必须在写 status 或 refresh 前失败。

服务用户固定为非 root `rardar`。`deploy/systemd/rardar.service` 使用 `Type=simple`、`Restart=on-failure`、有界 `RestartSec`/`TimeoutStopSec`、`KillMode=control-group` 和受限 writable roots。SIGTERM 先交给 Manager，由 Manager 停止 Scheduler 和 Website；systemd 的 control group 是最后的进程泄漏保护，不是第二套 supervisor。

## 3. Vinext compatibility entry

`npm run build` 始终是 release 和完整 Verify 的硬门禁。但是，当前 Vinext/Cloudflare 构建在本地 Node `vinext start` 目标中会因构建产物引用不受支持的 `cloudflare:` URL scheme 而启动失败。现有 generation host bridge、D1 和真实 HTTP 行为已经在 `vinext dev` 入口验证。

Manager 直接调用 Vite CLI，并由 `vite.config.ts` 加载 Vinext/Cloudflare 插件。`--configLoader runner` 避免在 release 的 `node_modules/.vite-temp` 写配置 bundle；runtime cache 固定写到外置 `RARDAR_VITE_CACHE_DIR/node_modules/.vite`；页面使用系统字体，不产生 `.vinext/fonts`。因此 Managed Runtime 无需写入 release 内的 `.vinext` 或其他代码路径，待激活 release 中由 build 生成的内容保持只读。

因此 Always-on v1 有意保留：

```text
node node_modules/vite/bin/vite.js \
  --configLoader runner \
  --host 127.0.0.1 \
  --port <RARDAR_VINEXT_PORT> \
  --strictPort
```

作为 systemd Manager 的 compatibility entry。它只能监听 loopback，不能直接暴露到公网，也不能作为跳过 build 的理由。以后切换到 `vinext start`、独立 Node production target 或其他 host，必须在单独 PR 中重新验证：

- 一次请求只读取一个 generation；
- current/manifest/hash/Schema/Audit fail closed；
- pointer switch 无需重启即可生效；
- D1 binding 和持久 state 不丢失；
- fresh/stale/invalid HTTP 语义不回归。

## 4. 目录布局

Always-on v1 使用固定 canonical 布局；这些路径同时绑定 checker、EnvironmentFile 与 systemd `ReadWritePaths`，不是可随意改写的示例：

```text
/opt/rardar/current                 指向 active exact release 的原子 leaf symlink
/opt/rardar/releases/<commit>       待激活或已归档的 exact release

/var/lib/rardar/data                current、generations、staging、candidates
/var/lib/rardar/vinext-state        Vinext/Miniflare D1 state
/var/lib/rardar/runtime             Manager/Scheduler status 与进程日志
/var/lib/rardar/locks               canonical data locks
/var/lib/rardar/runtime/wrangler-registry
/var/lib/rardar/runtime/miniflare-registry
/var/backups/rardar                 停机备份

/var/cache/rardar/vite              Vite/Vinext cache
/var/log/rardar/wrangler            Wrangler 日志

/etc/rardar/rardar.env              非 secret 环境，root:rardar 0640
/etc/rardar/rardar.secret           可选 secret，root-owned 0600
```

部署 checker 要求除 `RARDAR_HOME` 外的所有配置路径：

- 已经存在；
- 是绝对、规范、真实目录；
- 任一祖先或目录本身都不经过 symlink；
- code、data、D1、runtime、locks、cache、backup 互不相同且不互相包含；
- `rardar` 对 mutable roots 有读、写和 traverse 权限；
- 满足 `RARDAR_DEPLOY_MIN_FREE_BYTES`，默认每个检查路径至少 1 GiB 可用空间。

checker 不创建目录、不修权限、不初始化空 D1，也不把 flat data bootstrap 成 generation。首次真实部署必须在另行授权的迁移步骤中提供已经验证的 audited generation 和完整 D1 state。

`RARDAR_HOME=/opt/rardar/current` 是唯一例外：只有最终 `current` 路径组件可以是用于原子激活的 symlink；它的所有祖先必须不是 symlink，解析目标必须是运行 checker 的 exact release，且该 release 的必需文件不得是 symlink。

自定义目录不属于 Always-on v1 支持面。若后续确需改变 canonical 布局，必须把生成的 unit/EnvironmentFile 或明确的 systemd drop-in 作为独立目标审查，并同步 checker 的固定映射、写权限边界、备份和回滚协议；仅修改环境变量不能构成受支持部署。

## 5. Environment contract 与 secrets

将 `deploy/systemd/rardar.env.example` 复制到 `/etc/rardar/rardar.env`；Always-on v1 的路径值必须保持为以下 canonical 值，并与版本控制中的 unit 精确一致：

```text
RARDAR_HOME=/opt/rardar/current
RARDAR_DATA_DIR=/var/lib/rardar/data
RARDAR_RUNTIME_DIR=/var/lib/rardar/runtime
RARDAR_VINEXT_STATE_DIR=/var/lib/rardar/vinext-state
RARDAR_DATA_LOCK_DIR=/var/lib/rardar/locks
RARDAR_VITE_CACHE_DIR=/var/cache/rardar/vite
RARDAR_BACKUP_DIR=/var/backups/rardar

RARDAR_NODE=/usr/bin/node
RARDAR_PYTHON=/opt/rardar/current/.venv/bin/python
RARDAR_VINEXT_PORT=3000
RARDAR_RUNTIME_STATUS_PORT=3002

RARDAR_SCHEDULE_AT=08:00
RARDAR_SCHEDULE_TIMEZONE=Asia/Shanghai
RARDAR_STALE_AFTER_HOURS=36

WRANGLER_LOG_PATH=/var/log/rardar/wrangler
WRANGLER_REGISTRY_PATH=/var/lib/rardar/runtime/wrangler-registry
MINIFLARE_REGISTRY_PATH=/var/lib/rardar/runtime/miniflare-registry
```

三个持久工具路径同样是固定 canonical 路径：Wrangler 日志位于 `/var/log/rardar/wrangler`，两个 registry 位于 `/var/lib/rardar/runtime/` 下各自独立的目录。它们必须预先创建，不得通过 symlink 指向其他位置，也不得落入 release、data、D1、locks、cache 或 backup。

真实 GitHub 或 remote-analysis credential 只能写入 `/etc/rardar/rardar.secret` 或等价受限 EnvironmentFile。不得：

- 把真实值写入 `.env.production.example` 或 `rardar.env.example`；
- 将 secret 放入 systemd unit、命令行参数、Git、PR 或诊断 JSON；
- 在故障报告中复制完整 environment；
- 给 read-only GitHub source credential 超出实际需要的权限。

修改 EnvironmentFile 不会热更新 Manager。必须走完整 stop/start，并重新执行 offline/online checks。

offline checker 会拒绝 release 根及 `deploy/systemd/` 中的 `.dev.vars*` 和除 `.env.production.example` 外的 `.env*`。真实配置只能来自受限 EnvironmentFile，不能让 Vite 从 release-local 文件隐式加载第二套环境。

## 6. 准备 exact release

不要在 `/opt/rardar/current` 内执行 `git pull`。每个版本必须绑定审查通过的完整 commit SHA。

推荐流程：

1. 在非 active 的 staging 目录取得目标 exact commit，或传入与该 commit 绑定的 release archive。
2. 核对 `git rev-parse HEAD` 精确等于目标 SHA，且 `git status --porcelain` 为空。
3. 运行 `npm ci`。
4. 在该 worktree 创建独立 `.venv`，运行 `.venv/bin/python -m pip install -r requirements.lock` 与 `pip check`。
5. 设置该 worktree 自有的绝对 `RARDAR_PYTHON`，运行 `npm run verify`。
6. 运行 `npm run build`；即使 compatibility entry 使用 direct Vite runner，build 也不能跳过。
7. 记录 commit SHA、Node/Python/npm 版本、Verify 结果和 artifact checksum。

在最终 `/opt/rardar/releases/<commit>` 目录准备并验证 release，再以临时 leaf symlink 加原子 rename 切换 `/opt/rardar/current`。checker 必须证明配置的 `RARDAR_HOME` 解析到正在运行它的 exact release；`current` 的祖先和 release 内必需文件仍不得是 symlink。

## 7. systemd 安装边界

版本控制的 unit：

```text
deploy/systemd/rardar.service
```

关键契约：

```ini
Type=simple
User=rardar
Group=rardar
WorkingDirectory=/opt/rardar/current
EnvironmentFile=/etc/rardar/rardar.env
EnvironmentFile=-/etc/rardar/rardar.secret
ExecStartPre=/opt/rardar/current/.venv/bin/python -m pipeline.deployment check --offline
ExecStart=/opt/rardar/current/.venv/bin/python -m pipeline.runtime service
Restart=on-failure
KillMode=control-group
```

在 CI 或没有 systemd 作为 PID 1 的隔离环境中，只执行 unit 静态验证和进程级 lifecycle 测试，不尝试控制宿主 systemd。真实服务器上的 `daemon-reload`、`enable`、`start` 和 `restart` 只允许在 `PROD-DEPLOY-01` 中执行。

不要用 `npm run local:start` 代替 unit 的 `ExecStart`：`local:start` 面向本地后台管理，systemd 必须直接拥有 foreground `pipeline.runtime service`。

## 8. 外部访问

默认端口：

```text
Website:       127.0.0.1:3000
Runtime status:127.0.0.1:3002
```

3002 只供本机部署检查和网页 Runtime status 使用，不应进入外部代理。

最小人工访问方式是操作者显式建立 SSH tunnel：

```bash
ssh -L 3000:127.0.0.1:3000 <operator>@<server>
```

然后本机访问 `http://127.0.0.1:3000/`。这条命令只是运行手册示例；本轮不连接任何服务器。

反向代理只能作为后续配置 sample，例如让一个经过单独审查的 Caddy/Nginx upstream 指向 `127.0.0.1:3000`。必须由 `PROD-DEPLOY-01` 单独决定监听地址、认证、域名、TLS、headers、rate limit 和防火墙；不得把 3000 或 3002 直接开放到 `0.0.0.0`/`::`。

## 9. Offline deployment preflight

入口：

```bash
npm run deploy:preflight
# 等价的明确解释器入口
/opt/rardar/current/.venv/bin/python -m pipeline.deployment check --offline
```

offline check 是只读门禁，至少验证：

- Python、`RARDAR_PYTHON` identity、Node 路径和最低版本；
- release 必需文件、`node_modules/vinext` 和 build 输出；
- 全部固定 canonical 路径的绝对性、主状态根互不重叠、工具子目录边界、权限和磁盘空间，以及仅 `RARDAR_HOME` 最终 leaf symlink 的受限例外；
- current pointer、generation ID/path、ready manifest、manifest digest、全部 artifact hash、JSON Schema 和跨文件 Audit；
- audit `errorCount == 0`，允许 healthy 或只有 warning 的 degraded；
- Vinext state 中至少存在一个 Rardar SQLite；checker 对 source main、`-wal` 与 `-journal` 做打开前后身份/字节稳定校验，只把稳定字节复制到系统临时 scratch（systemd 下位于该服务的 `PrivateTmp`），然后仅在副本上执行 WAL recovery、`PRAGMA quick_check` 和 Rardar 表指纹检查。

失败返回非零和结构化错误。它不得：

- 创建/删除/重命名文件；
- bootstrap、repair、migration 或 rollback；
- 启动 Manager、Website 或 Scheduler；
- refresh、derive、publish 或清理 failed candidates；
- 在 generation 损坏时回退 flat data。

checker 从不通过 SQLite 连接正式 D1 source，不在 source 旁创建或更新 `-shm`，不 replay/checkpoint source WAL，也不修改 main、`-wal` 或 `-journal`。复制期间任何 source 身份、大小、时间或 SHA-256 变化都会有界重试并最终 fail closed；scratch 在检查进程结束时销毁。

## 10. Online deployment check

服务启动后运行：

```bash
npm run deploy:check
# 等价入口
/opt/rardar/current/.venv/bin/python -m pipeline.deployment check --online
```

online check 先重新执行全部 offline gates，然后验证：

- 一个 live Manager、一个 Website、一个 Scheduler，PID 互不相同；
- PID command identity 与 Runtime telemetry 匹配；
- Website/status listener 只存在于 loopback，并由预期 PID 持有；
- `/api/health`、`/`、`/signals`、`/search` 均返回允许状态；
- health、Runtime status 和 filesystem current 使用同一 generation；
- 检查开始到结束 current generation 没有切换。

允许结果：

```text
HTTP 200 + healthy
HTTP 200 + degraded + published_data_stale
```

禁止结果：

```text
HTTP 503
invalid/corrupt generation
非 loopback listener
PID/command/listener owner 不一致
检查期间 pointer 改变
```

若 Scheduler 正在完成一次真实自然 refresh，online check 可能因为 generation 在检查期间切换而 fail closed。等待该 Scheduler run 完成并确认发布结果后重跑；不要停止任务、回退 pointer 或手工 refresh 来让检查通过。

## 11. 停机备份

备份必须在 Managed Runtime 完全停止后取得一致停机点：

1. 记录当前 code SHA、generation ID、snapshot、D1 logical counts、failed candidate 数和 schedule telemetry。
2. `systemctl stop rardar`，等待 Manager、Website、Scheduler 全部退出。
3. 确认 3000/3002 已释放，Manager/Scheduler locks 不再被持有。
4. 使用同一个 UTC backup ID 保存：
   - 完整 `RARDAR_DATA_DIR`；
   - 完整 `RARDAR_VINEXT_STATE_DIR`，不能只复制猜测中的一个 SQLite 文件；
   - 当前 EnvironmentFile 的无 secret 清单和 release SHA。
5. 生成文件清单、大小和 SHA-256；备份目录不得位于任何被备份目录内部。
6. 对备份中的 generation 运行 Schema/Audit，对 SQLite 副本运行只读 integrity check。

不要在 SQLite/Miniflare state 正在写入时直接复制目录。不要因为部署而删除 `.candidates`、failed candidates、retained generations 或历史 audit。

## 12. 升级协议

真实升级只允许在后续授权中按以下顺序执行：

```text
准备并验证 exact release
→ 记录 pre facts
→ 停止 systemd Manager
→ 制作并验证同一停机点 backup
→ 原子切换 current leaf symlink 到已验证的新 exact release
→ offline preflight
→ 启动 systemd Manager
→ online deployment check
→ 记录 post facts
```

任何门禁失败都停止，不自动尝试另一个 generation、不修改 D1、不运行 refresh、不清理 failed candidates。正在运行的 release 不能由 `git pull` 原地覆盖。

## 13. 三类 rollback

### 13.1 Code release rollback

适用于新代码、依赖、unit 兼容或 Website 启动失败，而 persistent data 未损坏：

```text
停止新 Manager
→ 保留失败 release 和日志
→ 原子切回上一 exact code release
→ 保持 data 和 D1 原样
→ offline preflight
→ 启动上一 release
→ online check
```

不要因为代码回滚默认恢复 D1。P1-6B 的 additive `0004` 和既有兼容边界继续保留。

### 13.2 Generation rollback

适用于 current generation 业务数据需要回退，但 filesystem 和 D1 整体仍健康。只使用：

```bash
npm run data:generation:rollback -- <retained-generation-id>
```

该入口在 canonical data lock 内重新验证 retained target 的 generation/path、ready manifest、manifest digest、全部 artifact hash、Schema 和 Audit，再原子替换 pointer。不得手改 `current.json`、复制 flat data 或把 candidate 当作 target。完成后运行 online check，确认 D1 active identity adoption 与目标 current 一致。

### 13.3 Data + D1 paired restore

仅在持久数据或数据库 migration/写入失败，且代码或 generation rollback 无法恢复时使用：

```text
停止服务
→ 保留损坏现场和诊断
→ 选择同一个 backup ID
→ 成对恢复完整 data 与完整 Vinext/D1 state
→ offline generation + SQLite checks
→ 启动服务
→ online check 与逻辑计数核对
```

不能从不同时间点拼接 data 与 D1，也不能只恢复 `current.json` 或单个猜测的 SQLite 文件。恢复不得补造 Event、State、反馈、身份 mapping 或时间。

## 14. Scheduler 与 catch-up 副作用

Always-on v1 不改变 PR #14 已有 Scheduler 语义：

- 默认每天 `08:00 Asia/Shanghai`；
- 单周期临时故障每 5 分钟重试，最多 3 次；
- Manager/Scheduler 重启仍保留既有 12 小时 restart catch-up；
- `nextRunAt` 只能由 Scheduler 计算，status JSON 不是控制面。

因此真实部署或升级造成的停机可能在启动后自然触发 catch-up refresh。该 refresh 可能访问 GitHub、运行只读静态分析、创建 candidate 并在全部门禁通过后推进 current。这是 Scheduler 行为，不是 release 安装器修改数据。

操作要求：

- 在 pre/post facts 中记录 generation、snapshot、last run、next run；
- 不修改 `nextRunAt`、不使用 `--once`、不手工 refresh 来制造验收结果；
- 若 online check 与自然 refresh 竞争，等待 Scheduler 完成后重新检查；
- refresh 失败按数据流水线故障处理，不能用反复 systemd restart 掩盖；
- 不因部署清理新旧 failed candidates。

## 15. Health 与 stale data

freshness 的唯一事实来源仍是 verified current generation 内的 snapshot `captured_at`，并与同 generation Catalog 时间表示同一 UTC instant。

- `fresh`：`/api/health` 返回 200/healthy；
- `stale`：返回 200/degraded、reason=`published_data_stale`，页面继续读取完整旧 generation；
- `invalid`：pointer、manifest、hash、Schema、Audit 或 snapshot 不可信，返回 503 并 fail closed。

stale 不是自动 refresh、回滚或 restart 的授权。invalid 也不得回退 flat data；先保留诊断，再决定 code rollback、显式 generation rollback 或 paired restore。

## 16. 故障排查

先保留原始结构化错误，再查看：

```bash
journalctl -u rardar
/var/lib/rardar/runtime/logs/
/var/log/rardar/wrangler/
```

常见类别：

| 错误 | 检查 | 禁止的“修复” |
| --- | --- | --- |
| `deployment_path_*` | EnvironmentFile、绝对路径、symlink、目录归属和 overlap | 自动创建替代目录或跟随 symlink |
| `release_incomplete` / toolchain | exact SHA、`.venv`、`npm ci`、build、Node/Python 路径 | 在 active release 内 `git pull` |
| `disk_space_insufficient` | 各 mutable filesystem 的真实 free bytes | 删除 generation 或 failed candidates |
| `published_generation_*` | current、manifest/hash、Schema/Audit | 回退 flat data 或手改 pointer |
| `d1_database_missing` / `sqlite_integrity_failed` | 完整 Vinext state 路径和停机备份 | 初始化空 D1 覆盖原事实 |
| `runtime_listener_*` | 端口、loopback、PID owner、第二实例 | 公开绑定或批量杀死无关 Node/Python |
| `runtime_generation_changed` | 是否自然 refresh 正在发布 | 手工 refresh、修改 `nextRunAt` 或回滚健康发布 |
| `published_data_stale` | Scheduler telemetry、host availability、外部 source | 把 stale 伪装成 healthy |
| `vinext start` 的 `cloudflare:` scheme 错误 | v1 compatibility 是否仍由 direct Vite runner 加载 Vinext/Cloudflare 插件 | 跳过 build 或直接公开 dev server |

部署 checker 没有 repair mode。若同一根因重复出现，停止发布并报告具体 error code、路径、PID、generation 和日志时间；不要通过无限 restart 掩盖。

## 17. 验证与交付

开发和 CI 必须使用隔离的临时 data、D1、runtime、locks、cache 和随机 loopback 端口，不访问 Primary Runtime，也不占用 3000/3002。最终至少运行：

```bash
npm run verify
git diff --check
git diff -- data
git status --short --untracked-files=all
```

Linux 还需静态验证 systemd unit，并以进程级测试证明：

```bash
systemd-analyze verify deploy/systemd/rardar.service
```

若 CI 容器没有可运行的 systemd PID 1，只执行上述静态验证和隔离 process-level lifecycle；不得尝试控制宿主 service。生命周期证据必须证明：

```text
start   → Manager 1 / Website 1 / Scheduler 1
restart → old children 0 / new Manager 1 / Website 1 / Scheduler 1
stop    → all owned processes 0
```

本轮完成后只创建 Draft PR。不得转 Ready 或合并。本轮不执行真实部署。
