# Production Public Edge Closeout

## 1. 初始目标

本阶段最初要解决的问题是：把 Rardar 从依赖本地 Windows 电脑在线的开发服务，迁移为云端 Linux Server Primary 上的 24 小时 Managed Runtime，同时让本地长期 worktree 继续承担独立的产品开发、测试和 Draft PR。

阶段收口任务只整理已经由独立工程与生产操作验收的事实；它不访问 Production，不部署 release，不读取 data/D1 或凭据，也不修改产品代码、Runtime 或数据合同。

## 2. 最终架构

```text
Local development worktree
→ Draft PR / review
→ GitHub main Verify
→ CI-built exact release artifact
→ manifest + checksum + offline acceptance
→ Server Primary offline activation
→ systemd Manager
   ├─ Website 127.0.0.1:3000
   └─ Scheduler
→ Nginx 80/443
→ TLS + whole-site Basic Auth
→ https://rardar.cosflow.icu

Runtime status 127.0.0.1:3002
└─ server-internal only; never proxied
```

2026-08-23 的收口基线中，repository `main` 与 Production release 均为 `29a844504376b8432dfa01202f2817ac376cd490`。Server Primary 为 ACTIVE，Windows Primary 为 STOPPED。

## 3. 已完成能力

- **Always-on Server Runtime — VERIFIED**：systemd 只看护一个 Manager，由 Manager 唯一拥有 Website 与 Scheduler。
- **Natural refresh — VERIFIED**：截至 2026-08-23 08:00（Asia/Shanghai），自然任务独立发布 generation `20260823T000005118713Z-e5cfd5b8c5c9`；natural trigger、Schema、Audit 与 publication 均通过。该 generation 只是验收快照，不是永久 current 声明。
- **CI-built Exact Release Artifact — VERIFIED**：成功 main Verify exact SHA 绑定 manifest/checksum，完成 fresh extraction、offline wheelhouse install 与 runnable acceptance。
- **Offline Production deployment — FULLY VERIFIED**：Production 只下载、校验、解包、离线安装、preflight、备份、原子切换并受控重启，不在服务器运行 dependency install 或 build。
- **OPS-RESOURCE-HARDEN-01 — PASS**：2 GiB swap、`vm.swappiness=10`、`MemoryHigh=2304M`、`MemoryMax=infinity` 已验收。
- **Exact Host allowlist — DEPLOYED**：Vite 只接受受审查的 exact FQDN；Nginx 保留外部 Host，未知 Host 继续 fail closed。
- **PROD-DEPLOY-02 Public Edge — PASS / ACTIVE**：HTTPS + 整站 Basic Auth 已形成私有认证公网入口；它不是匿名公开产品。

## 4. Production 边界

- Website 只监听 `127.0.0.1:3000`；
- Runtime status 只监听 `127.0.0.1:3002`，不进入 Nginx；
- Nginx 在 80/443 终止 TLS、执行 Basic Auth，并只代理到 3000；
- `Authorization` 不传给 Website upstream；
- Production 不运行 `npm ci`、`npm install`、build 或 active-release `git pull`；
- release 与 data、Vinext/D1、runtime、cache、logs、locks 和 backups 分离；
- data generation 和 D1 不被 release artifact 覆盖；
- 凭据、认证 hash、private key 和完整 EnvironmentFile 不进入 Git、日志或本文件。

这些边界意味着 Public Edge ACTIVE 并不等于扩大 Runtime 权限，也不允许绕过 exact Host gate 或直接公开 3000/3002。

## 5. 当前产品状态

Rardar 的私有公网 MVP 已上线，当前产品工作流保持：

```text
Home / Daily Five
→ Why now
→ Evidence
→ Risk
→ Project Detail
→ Watch / Action / Feedback
→ subsequent recommendations
```

下一产品阶段尚未确定。`PRODUCT-NEXT-PHASE-DISCOVERY` 只授权人工讨论，不授权创建产品分支、设计或实现 Research Profile、Momentum、Alerts、MCP 或其他候选能力。

## 6. 未完成维护项

以下是保留的维护工作，不构成当前产品开发 blocker，也没有在本次收口中重新排序：

- `SEC-SSH-HARDEN-01`：收紧 bootstrap deployment sudo surface并评估 key rotation；
- `clash-sub.service`：作为独立主机服务维护，不与 Rardar Runtime 合并；
- `P1-6C2 Legacy Collision History`：保留现有 ambiguity gate，后续单独处理历史 collision 生命周期；
- bootstrap credential 的明文副本应在操作者确认已安全保存后删除；
- reboot persistence 尚未通过一次真实服务器重启进行独立验收。

## 7. 非目标

本收口不：

- 设计、选择或实现下一产品阶段；
- 修改应用、pipeline、测试、workflow、systemd unit、依赖或正式 data；
- 访问 Production、SSH、D1、credentials、Nginx、DNS、TLS 或 Basic Auth；
- 部署、重启、手工 refresh、执行 Scheduler `--once` 或改变 Runtime；
- 创建 Release Artifact 部署任务；
- 把维护项包装成产品 blocker。

结果是一个可审查的生产里程碑文档基线，而不是新的运行状态变更。
