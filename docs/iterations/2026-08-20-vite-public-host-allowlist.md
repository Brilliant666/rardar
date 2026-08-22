# Vite Exact Public Host Contract

## 状态与范围

本迭代从 `main` 的 `c02f75012e024bb17d470c6fddb5006495792338`
开始，唯一目标是修复 `PROD-DEPLOY-02` 暴露的
`BLOCKED_VITE_HOST_ALLOWLIST`。它只改变 Managed Website 的 Host 配置合同、
部署预检、测试和文档；不访问 Production，不修改 Nginx、DNS、TLS、Basic
Auth、systemd unit、Scheduler、generation、D1 或产品数据。

Public Edge 在本迭代中继续保持 inactive。本文件记录实现和验证证据；PR 的
Ready、merge 与 artifact 状态以 GitHub 和最新 `main` 为准，任何仓库状态都不
代表已经部署或启用 Public Edge。

## 事故与根因

Production release `c02f75012e024bb17d470c6fddb5006495792338` 的 Website
在 `127.0.0.1:3000` 上直接返回 200，但 Nginx 正确保留
`Host: rardar.cosflow.icu` 转发时，Vite 返回 403：该 hostname 没有进入
Vite 的允许列表。

`vite.config.ts` 已固定 `server.host=127.0.0.1`、端口和 `strictPort`，但
Managed Runtime 的 Website 正向环境 allowlist 没有传递 Vite 官方变量
`__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS`。因此 systemd EnvironmentFile 即使
声明公共 hostname，Website child 也看不到它。

## 设计决策

继续使用 Vite 官方 `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS`，不在 TypeScript
中建立第二套 `RARDAR_PUBLIC_HOST` parser，也不设置 `allowedHosts: true`。
固定 Vite `8.1.4` 已通过真实 HTTP 测试证明支持该机制，所以
`vite.config.ts` 无需修改。

Nginx 的 `proxy_set_header Host $host` 是正确行为：它保留外部请求的真实
authority，让 Website 执行自身的 Host gate。把 Host 改写成 `127.0.0.1`
会绕过该安全边界，因此不属于修复方案。

## Exact FQDN 合同

变量缺失表示没有额外公共 Host，保留 loopback / tunnel 兼容。变量存在时：

- 接受 1–8 个逗号分隔的 canonical lowercase ASCII FQDN；
- 单个 hostname 最长 253 字符，DNS label 为 1–63 字符；
- 至少包含一个点，只允许字母、数字、点和 label 内部连字符；
- 不做 trim、lowercase、空项忽略或重复项去重；
- URL scheme、port、path、query、fragment、userinfo、IP、`localhost`、
  非 ASCII、leading-dot suffix、通配符和 `true` 全部 fail closed。

Runtime 在写入新的 status/control/log 或启动 Manager child 前验证该合同。
合法原始值不改写地进入 Manager，然后仅由 Website 正向 allowlist 暴露给
Vite；无关环境和 secrets 继续被过滤。Scheduler ownership、参数和环境行为
不变。

offline 和 online deployment checker 都复用同一 validator。变量存在时只在
结构化 `runtimeContract.websiteAllowedHosts` 中输出经过验证的 hostname；它
不是 `REQUIRED_RUNTIME_VARIABLES`，checker 仍是 read-only、fail-closed、
no-repair。

## 测试矩阵

- validator：单个/多个合法 FQDN、数字与内部连字符、变量 absent；
- validator 失败：`true`、通配符、leading dot、URL、port/path/query/
  fragment、IP、localhost、uppercase、空白、空项、重复、非 ASCII、长度与
  label 边界；
- Runtime：合法值精确进入 Website，非允许环境与 secret 继续过滤；非法值在
  status read/write、process cleanup 和 child spawn 前失败；
- deployment：变量 absent 兼容，合法值在离线报告中可见，offline/online 对
  wildcard 都返回 `runtime_configuration_invalid`；
- 真实 Vinext/Vite：随机 loopback port、临时 data/D1/Runtime 下，
  `rardar.cosflow.icu` 为 200；未知、兄弟、父域、嵌套子域和前缀欺骗 Host
  均为 403；HTTP 栈拒绝控制字符；直接 loopback 为 200；listener 只有
  `127.0.0.1`；同一个 Website PID 完成测试且无 restart；
- 完整 `npm run verify` 继续覆盖 Schema、Audit、build、安全审计、正式 data
  不变、Runtime 隔离和进程清理门禁。

## 验证结果

Windows 开发 worktree 使用自身 `.venv` 和 Node >=22.13 完成
`npm run verify`：Python 488 个测试通过（33 个平台条件跳过），Node 87 个
测试通过，Schema 与 Audit healthy，production build 通过，production
dependency audit 为 0 vulnerabilities；正式 `data/` 字节无变化，隔离 Runtime
已移除且没有残留 Vinext/workerd 进程。Linux 的 `systemd-analyze` 仍由 GitHub
Verify 执行，本地 Windows 按既有门禁明确跳过。

## 后续部署顺序

```text
Hotfix final review
→ Ready / Squash merge
→ main Verify
→ exact CI release artifact
→ 部署 exact release
→ 在 /etc/rardar/rardar.env 写入精确 FQDN
→ controlled Runtime restart
→ offline / online checks
→ 直接 Host-header 200/403 验收
→ 重新执行 PROD-DEPLOY-02
```

正式值为：

```text
__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=rardar.cosflow.icu
```

本开发任务不执行以上 Production 步骤。

## 回滚

若新合同或后续激活失败：保持/恢复 Public Edge inactive，停止新 Runtime，
保留诊断与 exact release，移除或恢复上一份非 secret EnvironmentFile 配置，
原子切回上一健康 exact code release，再执行 offline preflight、启动和 online
check。不要改写 Host 为 `127.0.0.1`，不要公开绑定 3000/3002，也不要修改
generation、D1、DNS、证书或认证材料来掩盖 Website Host 失败。

## 遗留边界

Nginx vhost 重新启用、Production EnvironmentFile 写入、Runtime restart、
公网 200/401、安全 headers、rate limit 和最终 Public Edge 验收都属于后续独立
部署任务。本迭代不开始 P2，也不改变 P1-6C2 deferred 状态。
