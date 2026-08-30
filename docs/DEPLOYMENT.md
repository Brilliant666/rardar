# Rardar Always-on Deployment v1

本文定义 Rardar 在单台 Linux 主机上的可部署工程和操作者协议。代码与依赖只能由 GitHub CI 为一个已经成功通过 `Verify` 的 exact commit 构建；Server Primary 只接受该 CI artifact，并负责离线激活。本文是运行手册，不是某个版本已经完成生产部署的证明。

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

CI-built Exact Release Artifact v1 的 ABI 支持面更窄：builder 与目标均为 Ubuntu 24.04 x86_64，Node 固定 `22.13.1`，Python wheel target 固定 `3.12`。扩大 OS、architecture 或 Python target 必须另行验证，不能把 manifest 字段改成模糊范围。

Always-on v1 提供：

- systemd 管理的唯一 foreground Manager；
- Manager 管理的 Website 与唯一 Scheduler；
- exact code release 与 mutable state 分离；
- 由成功的 main `Verify` exact SHA 触发的 Linux release artifact、manifest 与 SHA-256；
- 完整 `node_modules`、已验证的 `dist` 和离线 Python `wheelhouse`；
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

以上真实基础设施动作属于后续单独授权的生产任务。仓库或 CI 能力合并不自动授予服务器访问、激活或重启权限。

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

# Optional; required only for an explicitly reviewed public reverse proxy.
# __VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=rardar.cosflow.icu

RARDAR_SCHEDULE_AT=08:00
RARDAR_SCHEDULE_TIMEZONE=Asia/Shanghai
RARDAR_STALE_AFTER_HOURS=36
RARDAR_TRENDING_PRODUCER_ENABLED=false

WRANGLER_LOG_PATH=/var/log/rardar/wrangler
WRANGLER_REGISTRY_PATH=/var/lib/rardar/runtime/wrangler-registry
MINIFLARE_REGISTRY_PATH=/var/lib/rardar/runtime/miniflare-registry
```

三个持久工具路径同样是固定 canonical 路径：Wrangler 日志位于 `/var/log/rardar/wrangler`，两个 registry 位于 `/var/lib/rardar/runtime/` 下各自独立的目录。它们必须预先创建，不得通过 symlink 指向其他位置，也不得落入 release、data、D1、locks、cache 或 backup。

`__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS` 是可选的 Website Host 合同，不属于所有部署的必填变量。未配置时保持现有 loopback / tunnel 行为；配置时，Managed Runtime 和 offline/online checker 都只接受最多 8 个逗号分隔、无空白、无重复的 canonical lowercase ASCII FQDN，并把经过验证的 hostname 列表报告为 `websiteAllowedHosts`。URL、端口、路径、IP、`localhost`、leading-dot suffix、通配符和 `true` 都会在任何 child 启动前 fail closed。合法原始值只通过 Website 的正向环境 allowlist 传给 Vite 官方 `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS` 机制；不会启用 `allowedHosts: true`，也不会把完整 systemd environment 或 secret 暴露给 Website。

`RARDAR_TRENDING_PRODUCER_ENABLED` 是严格的非敏感布尔合同，只接受小写 `true` 或 `false`，未配置时为 `false`。`false` 保持既有 daily-refresh-only 行为；只有通过独立 Production 门禁把它设为 `true`，唯一 Managed Scheduler 才会同时拥有固定两小时 Observation 和每日 Explosion derive。启用时仍固定使用 `08:00 Asia/Shanghai` 产品调度，不能通过自定义 cadence 或第二套 profile 改写相位。

真实 GitHub 或 remote-analysis credential 只能写入 `/etc/rardar/rardar.secret` 或等价受限 EnvironmentFile。Producer 启用时 `GITHUB_TOKEN` 必须存在且非空；Manager 仅把完整受限环境交给 Scheduler child，Website 继续使用正向 allowlist，因此不会获得 `GITHUB_TOKEN` 或 Producer flag。不得：

- 把真实值写入 `.env.production.example` 或 `rardar.env.example`；
- 将 secret 放入 systemd unit、命令行参数、Git、PR 或诊断 JSON；
- 在故障报告中复制完整 environment；
- 给 read-only GitHub source credential 超出实际需要的权限。

修改 EnvironmentFile 不会热更新 Manager。必须由通过 Verify 的 exact release 执行受控 stop/start，并重新执行 offline/online checks。首次 Public Edge 激活或后续变更正式 Host 合同时，受审查的值必须明确写为 `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=rardar.cosflow.icu`；修改该值之后不得只 reload Nginx 或复用旧 Website 进程。

offline checker 会拒绝 release 根及 `deploy/systemd/` 中的 `.dev.vars*` 和除 `.env.production.example` 外的 `.env*`。真实配置只能来自受限 EnvironmentFile，不能让 Vite 从 release-local 文件隐式加载第二套环境。

## 6. 准备 exact release

不要在 `/opt/rardar/current` 内执行 `git pull`。不要在 Server Primary 上运行 `npm ci`、`npm install`、production build 或需要 registry 的 dependency audit。每个版本必须绑定审查通过的完整 commit SHA。

### 6.1 Builder phase — GitHub CI only

`.github/workflows/release-artifact.yml` 只监听 `Verify` 的 completed `workflow_run`，并且只接受同仓库 `main` push、结论 SUCCESS 的 exact `head_sha`。固定的 Ubuntu 24.04 x86_64 builder 对该 SHA 执行：

```text
checkout exact verified SHA
→ npm ci
→ npm run verify
→ npm run build
→ Python 3.12 wheelhouse
→ exact git archive staging
→ artifact verify
→ deterministic tar.gz + SHA-256
→ fresh extraction
→ offline Python venv install + pip check
→ upload immutable GitHub Actions artifact
```

artifact 名称与 archive 名称均包含完整 40 位 commit SHA：

```text
rardar-release-<40-char-sha>-linux-x86_64
rardar-release-<40-char-sha>-linux-x86_64.tar.gz
rardar-release-<40-char-sha>-linux-x86_64.tar.gz.sha256
```

artifact 根的 `release-manifest.json` 绑定 repository、exact commit、成功 Verify 的 run ID/head SHA、builder OS、architecture、Node/npm/Python wheel target，以及 artifact 内两份 lock 文件的 SHA-256。`pipeline.release_artifact` 使用 Python 标准库只读验证 manifest、锁、必需路径、wheel coverage、secret-like 文件名和 symlink 边界。

artifact 包含完整 `node_modules`，因为 Managed Runtime 仍直接依赖 Vite/Vinext compatibility entry；它同时包含已经通过 build 的 `dist` 与 Python `wheelhouse`。artifact 不包含 `data/`、`.git/`、builder venv、Wrangler/Miniflare/Vite cache、环境文件或 credentials。

### 6.2 Activation phase — Production only

单独授权的生产任务只能执行：

```text
按 exact SHA 选择成功的 GitHub artifact
→ 下载 archive、checksum 与 manifest copy
→ 在解包前核对完整 archive SHA-256
→ 安全解包到 /opt/rardar/releases/<exact-sha>
→ 对原始解包树运行 artifact verifier
→ 在 release 内新建 .venv
→ pip --no-index --find-links wheelhouse -r requirements.lock
→ pip check
→ 只读 deployment preflight
→ 停机备份
→ 原子切换 current leaf symlink
→ restart
→ online check
```

生产创建的 `.venv` 不是 artifact 内容。原始 artifact verifier 会拒绝打包进去的 `.venv`；deployment checker 只在离线安装后允许一个真实的顶层 `.venv`，并继续核对配置解释器 identity。最终 `/opt/rardar/releases/<commit>` 目录完成离线安装和验证后，才可通过临时 leaf symlink 加原子 rename 切换 `/opt/rardar/current`。

### 6.3 2026-08-18 installation incident

旧协议曾在 3.8 GiB RAM、无 swap 的 Server Primary 上执行长期 `npm ci`。registry `ECONNRESET` 与约 13 小时 50 分钟的失败安装和 production workerd 高内存同时发生，内核 OOM kill 导致服务重启并错过 08:00 自然调度；Scheduler 后续 catch-up 恢复了健康 generation。Production availability 已恢复，但该事件证明“服务与在线 dependency installation 共置”不再是受支持的发布方式。资源硬化另属 `OPS-RESOURCE-HARDEN-01`，不能替代本节的 release preparation 隔离。

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

反向代理只能作为经过单独审查的 Public Edge 配置，让 upstream 指向 `127.0.0.1:3000`。Nginx 必须保留外部请求的受审查 hostname，让 Website 自己执行 Host gate：

```nginx
location / {
    proxy_pass http://127.0.0.1:3000;
    proxy_set_header Host $host;
}
```

禁止用 `proxy_set_header Host 127.0.0.1` 绕过 Website Host 校验。允许列表属于版本化 Runtime 合同，不由代理伪造内部 Host。域名、TLS、认证、headers、rate limit、API/health 暴露范围和防火墙仍必须由独立 Public Edge 任务审查；3000 或 3002 不得直接开放到 `0.0.0.0`/`::`，3002 也不得进入代理。

当前 Public Edge 已按以下顺序完成首次激活；未来新增 hostname、变更 Host 合同或重建入口时仍必须复用同一门禁：合并 Host 合同变更 → main Verify 与 exact CI artifact 成功 → 部署该 exact release → 在 `/etc/rardar/rardar.env` 设置精确 FQDN → controlled Runtime restart → offline/online checks → 直接 Host-header 200/403 验收 → 最后才启用或切换 Nginx vhost。任一步失败都不得绕过 Website Host gate 或改变既有健康入口。

## 9. Offline deployment preflight

入口：

```bash
npm run deploy:preflight
# 等价的明确解释器入口
/opt/rardar/current/.venv/bin/python -m pipeline.deployment check --offline
```

offline check 是只读门禁，至少验证：

- Python、`RARDAR_PYTHON` identity、Node 路径和最低版本；
- release manifest 的 exact directory/commit/Verify identity、OS/architecture/Node/Python contract 与两份 lock SHA-256；
- release 必需文件、`node_modules/vite`、`node_modules/vinext`、build 输出与完整 wheelhouse coverage；
- release tree 不包含 `data/`、secret-like 文件、cache 或不安全 symlink；仅允许 Production 离线安装后产生的真实顶层 `.venv`；
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
下载、校验、解包并离线安装 exact CI release artifact
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

## 14. Scheduler、Producer 与 catch-up 副作用

Producer flag 默认关闭，因此 Always-on v1 继续保持 PR #14 已有 Scheduler 语义：

- 默认每天 `08:00 Asia/Shanghai`；
- 单周期临时故障每 5 分钟重试，最多 3 次；
- Manager/Scheduler 重启仍保留既有 12 小时 restart catch-up；
- `nextRunAt` 只能由 Scheduler 计算，status JSON 不是控制面。

当 `RARDAR_TRENDING_PRODUCER_ENABLED=true` 时，调度所有权仍只有：

```text
systemd
└─ Manager
   └─ Scheduler
      ├─ Observation  每个 Asia/Shanghai 偶数整点
      ├─ Discover     每个成功 Observation 后
      ├─ Refresh      每日 08:00
      └─ Explosion    每日 08:00
```

不得新增 cron、timer、第二个 service、daemon 或长期后台调度线程。普通偶数相位按 Observation → Discover；08:00 的顺序严格为 Observation → 原有 Refresh → Explosion → Discover，四项串行，使 Discover 使用最新 Today exact 排除集合。Observation 失败不运行该相位 Discover，但不阻止 Refresh；Refresh 最终失败不阻止基于仍可信 current 的 Explosion 尝试；Explosion 或 Discover 失败只进入各自嵌套 Producer telemetry，不回滚已成功的核心阶段，也不使 Manager 把一个 heartbeat 新鲜的 Scheduler 判为 stale。

Observation 收到的是固定相位的 intended `scheduledAt`，而不是实际启动时间。正常相位执行一次；只有明确的全源网络/HTTP 408、429 或 5xx 失败可在同一 10 分钟 eligibility 窗口内短重试一次。Scheduler 启动时只允许补最近一个且延迟不超过 10 分钟的 observation slot；超过窗口或错过多个 slot 时不回填。observer lock 冲突记录 `skipped_overlap`，不会启动第二个 observer。

既有 daily 12 小时 catch-up、最多三次尝试、五分钟间隔和 remote-clone non-retryable 分类保持不变。restart 后先处理合法 daily catch-up；当天 08:00 capture 已存在且 eligible 时，Explosion 可幂等 catch-up。capture 缺失或不 eligible 时只记录 `not_ready`，不得制造 capture。首次合法 08:00 capture 后的 `firstExactEligibleAt` 机械等于该 endpoint +24 小时；到时是否为 exact 仍取决于两个 endpoint 都 eligible。

Scheduler status 保留既有 top-level Refresh 字段，并增加 path-free `producer.observation`、`producer.explosion` 与 `producer.discover` telemetry。Discover 只公开状态、时间、capture/generation ID、阶段/发布/冲突/排除计数、有限 coverage 和稳定错误码。统一的进程内 status store 串行化 heartbeat 和事件更新；Manager 只转发来自当前受管 Scheduler PID 的 reviewed fields。token、Authorization、absolute capture/candidate path、上游错误正文和 stack trace 都不得进入 status 或日志。

Discover generation 位于 `data/artifacts/trending/discover/v1/`，与 `data/current.json` 和每日 retained generations 使用独立 pointer、manifest、lock 和 rollback。发布只依赖已验证 Observation source copies 与当前 Today Explosion exact exclusion；不会修改 D1。合并 Scheduler 集成不等于 Production Discover 激活，部署与首个自然 derive 必须由独立 `RARDAR-DISCOVER-RUNTIME-ACTIVATION-01` 完成。

因此真实部署或升级造成的停机可能在启动后自然触发合法 catch-up。daily refresh 可能访问 GitHub、运行只读静态分析、创建 candidate 并在全部门禁通过后推进 current；Producer 也可能只在上述窄窗口执行一个 observation 或幂等 Explosion。这是 Scheduler 行为，不是 release 安装器修改数据。部署应避开 07:30–08:30 和距下一两小时相位不足 15 分钟的窗口；不要用 restart 制造验收事件。

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
| `release_incomplete` / `release_artifact_invalid` / toolchain | exact SHA、manifest、archive/lock checksum、wheelhouse、离线 `.venv`、Node/Python 路径 | 在服务器重跑 `npm ci`、`npm install`、build 或在 active release 内 `git pull` |
| `disk_space_insufficient` | 各 mutable filesystem 的真实 free bytes | 删除 generation 或 failed candidates |
| `published_generation_*` | current、manifest/hash、Schema/Audit | 回退 flat data 或手改 pointer |
| `d1_database_missing` / `sqlite_integrity_failed` | 完整 Vinext state 路径和停机备份 | 初始化空 D1 覆盖原事实 |
| `runtime_listener_*` | 端口、loopback、PID owner、第二实例 | 公开绑定或批量杀死无关 Node/Python |
| `runtime_configuration_invalid`（Vite Host） | `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS` 是否为无空白、无重复的精确 ASCII FQDN | `allowedHosts=true`、通配符、leading-dot suffix 或代理改写成 `127.0.0.1` |
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

Release Artifact workflow 还必须在固定 Ubuntu 24.04 x86_64 runner 上对成功的 main Verify exact SHA 完成 archive、checksum、fresh extraction、只读 artifact verify、Python `--no-index` install、`pip check` 与 Vite/Vinext runnable acceptance。workflow 不替代现有 PR/main `Verify`，也不连接 Production。

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

仓库文档或代码迭代本身不授权真实部署。任何 Production 激活、重启、回滚或 Public Edge 变更仍需单独、明确的操作授权；代码合并不能被描述为已经部署。
