# Rardar Project Status

> Last updated: **2026-08-26**
>
> 本文记录“Rardar 现在做到哪”。长期使命和不变量看 [`RARDAR_NORTH_STAR.md`](RARDAR_NORTH_STAR.md)，未来路线看 [`ROADMAP.md`](ROADMAP.md)，具体工程证据看 [`iterations/`](iterations/)。generation、snapshot 和 `nextRunAt` 均是带日期的验收快照，不应被解释为永久 current 状态。

## 一句话状态

Rardar 的 **Private authenticated production MVP 已 ACTIVE**：2026-08-23 生产里程碑的 code baseline 与当前 Production release 均为 `29a844504376b8432dfa01202f2817ac376cd490`，Server Primary 长期运行，并通过 [`https://rardar.cosflow.icu`](https://rardar.cosflow.icu) 提供 HTTPS + 整站 Basic Auth 访问。repository `main` 可以因纯文档或后续开发提交继续前进；只有通过独立 exact artifact deployment 门禁的 SHA 才是 Production release。CI exact artifact、离线生产激活、自然刷新、资源加固、exact Host allowlist 和 Public Edge 已形成完整受审计闭环。

---

## Repository 状态

当前代码与产品能力基线：

- 2026-08-23 milestone code baseline：`29a844504376b8432dfa01202f2817ac376cd490`；
- Production release：`29a844504376b8432dfa01202f2817ac376cd490`；
- Repository `main`：以 GitHub 默认分支为准；纯文档或尚未部署的开发提交不会自动改变 Production release；
- PR #18：Launch Decision Flow；
- PR #21：Signal → Project Audited Association v1；
- PR #22：CI-built Exact Release Artifact v1；
- PR #23：Vite Exact Public Host Contract；
- PR #26 / #27：append-only GitHub Trending Observation v1 与 create-only 并发 settlement；
- PR #28：audited 24-hour Trending Explosion Artifact v1。

GitHub Actions 的最终门禁仍是统一的 `npm run verify`。`Release Artifact` 不替代 Verify：它只接受同仓库 `main` push 的成功 Verify exact SHA，在固定 builder 上生成绑定 commit、manifest 和 checksum 的 artifact，并完成 fresh extraction、offline Python install 与 runnable acceptance。

Signal → Project 的长期合同保持不变：

- 唯一关联权威是 Signal 自身的 `repo`；
- 只允许在同一 verified generation 中重算并核对 Stable Project ID；
- 无 repo、Catalog 无精确项目或 identity 无法证明时继续 signal-only；
- 不按标题、slug、basename、中文 enrichment、模糊规则或 LLM 猜测归属。

---

## Production Runtime

### 当前拓扑

```text
Internet
→ Nginx :80 / :443
→ TLS + whole-site Basic Auth
→ 127.0.0.1:3000 Rardar Website

Ubuntu Server Primary = ACTIVE
└─ systemd
   └─ Rardar Manager
      ├─ Website  127.0.0.1:3000
      └─ Scheduler

Runtime status 127.0.0.1:3002 = server-internal only
Windows Primary = STOPPED
```

Production 的 release、data、Vinext/D1、runtime、cache、logs 与 backups 相互分离。Manager 是 Website 和 Scheduler 的唯一 owner；3000 与 3002 均只监听 loopback。

### CI artifact 到 Production 的闭环

以下链路已经完整验收：

```text
main Verify
→ exact Release Artifact
→ manifest + archive checksum
→ fresh extraction + offline wheelhouse install
→ Production preflight + backup
→ atomic current release switch
→ controlled restart
→ online checks
→ natural Scheduler refresh
→ authenticated Public Edge
```

Production 不运行 `npm ci`、`npm install` 或 build，也不在 active release 内 `git pull`。CI-built exact artifact 的离线激活已 **FULLY VERIFIED**。

### 自然运行验收

Always-on unattended operation 已验证。2026-08-13 与 2026-08-14 的连续自然发布完成后，`SERVER-NATURAL-RUN-03` 又在 **2026-08-23 08:00（Asia/Shanghai）**独立验收：

```text
generation:      20260823T000005118713Z-e5cfd5b8c5c9
natural trigger: PASS
Schema:          healthy
Audit:           healthy
publication:     PASS
nextRunAt:       2026-08-24 08:00 Asia/Shanghai
```

这是截至 2026-08-23 的验收证据，不声称上述 generation 或 `nextRunAt` 在后续调度后仍是 current。

### 资源加固

`OPS-RESOURCE-HARDEN-01 = PASS`。当前基础防线：

| Guardrail | Accepted value |
| --- | --- |
| Swap | 2 GiB |
| `vm.swappiness` | `10` |
| systemd `MemoryHigh` | `2304M` |
| systemd `MemoryMax` | `infinity` |

资源门禁已解除，但它不改变“Production 不进行依赖安装或构建”的 release 隔离合同。

### Public Edge

`PROD-DEPLOY-02 = PASS`，Public Edge 为 **ACTIVE**。当前入口是 [`https://rardar.cosflow.icu`](https://rardar.cosflow.icu)，访问模式为 HTTPS + 整站 Basic Auth 的私有认证生产 MVP，并非匿名公开产品。

边界：

- Nginx 只把认证后的流量代理到 `127.0.0.1:3000`；
- Nginx 保留外部 `Host`，Website 使用已部署的 exact FQDN allowlist 再次校验；
- `Authorization` 不转发给 upstream；
- `127.0.0.1:3002` 不进入 Nginx，也不暴露公网；
- Basic Auth 凭据、hash、TLS private key 与 Production secret 不存储在仓库；
- Public Edge 激活未影响原有站点。

---

## 能力完成度

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| GitHub facts / Signal collection | ✅ | 真实 API 快照、历史归档、信源健康与同代 audited association |
| Immutable generation / Audit | ✅ | candidate → Schema → Audit → atomic pointer；retained generation 可验证回滚 |
| Explainable scoring / static analysis | ✅ | 多维可解释评分；有界只读分析且不执行陌生代码 |
| Stable Project ID | ✅ 主链 | Catalog v3、D1、API、route、client 使用 Stable ID |
| Action / Feedback / Recommendation | ✅ | append-only Event + State、幂等写入、有限个性化重排 |
| Verify CI | ✅ | PR / main 统一 `npm run verify` |
| Launch Decision Flow | ✅ | Home / Daily Five → Why now → Evidence → Risk → Detail → Action |
| Signal → Project association | ✅ | 只接受同 generation 可精确证明的 repository identity |
| Linux Always-on Server Primary | ✅ VERIFIED | Manager、Website、Scheduler 由 systemd 长期看护 |
| Natural unattended publish | ✅ VERIFIED | 截至 2026-08-23 已独立验证自然触发、Schema/Audit 和发布 |
| CI exact release artifact | ✅ VERIFIED | exact commit、manifest/checksum、fresh extraction 与 offline acceptance |
| CI artifact Production release | ✅ FULLY VERIFIED | exact artifact 已离线激活到 Server Primary |
| Resource hardening | ✅ PASS | swap、swappiness 与 systemd memory guardrails 已验收 |
| Vite exact public Host | ✅ DEPLOYED | 精确 FQDN allowlist 与 Host 200/403 边界已验收 |
| Public Edge | ✅ ACTIVE | HTTPS + 整站 Basic Auth；3000/3002 继续 loopback-only |
| Trending Observation v1 | ✅ MERGED | 固定两小时事实 capture、numeric GitHub identity、26h carry-forward、create-only store 与只读 Audit |
| Trending Explosion Artifact v1 | ✅ MERGED | 08:00 exact/pending/conflict、byte-exact generation sources、Schema/Audit、幂等与 CAS publication |
| Trending Producer Runtime | 🚧 IMPLEMENTED / NOT DEPLOYED | 单 Scheduler 集成在独立工程轮审查；flag 默认 `false`，须经 exact artifact Production 验收后才能称为 ACTIVE |
| Legacy collision lifecycle | ⏸ Deferred | P1-6C2 尚未收口，不阻塞当前 Stable ID 主链 |

---

## 已完成的重要工程阶段

### Phase A — 数据可信边界

PR #2、#4、#6 与 #7 建立了数据 Schema、audited generation、评分语义和统一 Verify。Rardar 不再直接改 flat JSON，而是在 Schema + Audit 通过后原子发布 immutable generation。

### Phase B — 用户行动与稳定身份

PR #5、#8、#9 与 #13 建立 append-only Action Event、State 投影、Weekly Acted Projects 和 collision-safe Stable Project ID；canonical route 为 `/project/v1/<projectId>`。

### Phase C — Always-on Runtime

PR #14 至 #17、PR #19 与 Server Primary cutover 建立 systemd 单 Manager ownership、Runtime freshness、Linux stable-read 完整性和可持续自然刷新。

### Phase D — 产品决策流

PR #18 与 #21 将已有证据组织为：

```text
Home / Daily Five
→ Why now
→ Evidence
→ Risk
→ Project Detail
→ Watch / Action / Feedback
→ subsequent recommendations
```

Signal 只有在同 generation 可精确验证 repository identity 时进入项目详情。

### Phase E — Exact release 与 Public Edge

PR #22、PR #23、`PROD-PRODUCT-RELEASE-02`、`SERVER-NATURAL-RUN-03`、`OPS-RESOURCE-HARDEN-01` 和 `PROD-DEPLOY-02` 已完成：CI 构建 exact artifact，Production 离线激活，资源门禁通过，Website exact Host 合同已部署，私有认证 Public Edge 已 ACTIVE。

### Phase F — Rardar v2 fact producer

PR #26、#27 与 #28 已把固定两小时 GitHub Observation 和 audited 24-hour Explosion Artifact 合入 Repository `main`，但合并的数据能力不等于 Production producer 已启用。`RARDAR-PRODUCER-RUNTIME-INTEGRATION-01` 只在唯一 Managed Scheduler 内编排 Observation、原有 08:00 Refresh 和 Explosion，并以默认关闭的 feature flag、Scheduler-only `GITHUB_TOKEN`、失败隔离与嵌套 telemetry 保持现有 Runtime 边界。只有新 exact main artifact 完成离线部署、一次受控 restart 和首个自然两小时 Observation 验收后，才能把该能力标为 Production ACTIVE。

---

## 保留的维护项与边界

以下事项仍未完成，但不应被描述为当前产品开发 blocker，也不代表已经选定下一产品方向：

- `SEC-SSH-HARDEN-01`：收紧 bootstrap deployment sudo surface，并在保留可验证 rollback 的前提下评估 key rotation；
- `clash-sub.service`：作为与 Rardar 独立的主机维护项处理；
- `P1-6C2 Legacy Collision History`：保持现有 ambiguity gate，不改写 append-only 历史；
- bootstrap credential 的明文副本：由操作者确认已安全保存后删除；
- reboot persistence：尚未通过真实重启单独验收；
- Vinext production compatibility：继续使用已验证的 Vite/Vinext compatibility entry，等待 upstream 能力稳定后再评估。

---

## 当前产品决策状态

Rardar v2 RFC 已获批准，当前授权工程轮是 `RARDAR-PRODUCER-RUNTIME-INTEGRATION-01`：把已合并的 Observation 与 Explosion 接入唯一 Managed Scheduler，并通过 exact artifact 和自然运行完成 Production 验收。它不授权 TopicEye、Sub2API/AI Runtime、Find Project、P1-6C2 或其他候选方向。

Research Profile、Momentum Lifecycle、Alerts / Digest、MCP、Advanced Personalization 与 Watch Lifecycle 仍是候选方向；本状态文档不替用户选择顺序，也不把尚未通过 Production 门禁的 Producer 写成已部署。

---

## 如何维护本文

重要 PR 或生产门禁完成后，只更新 Repository 基线、Production Runtime、能力完成度、当前决策状态和已知边界。动态 generation 与 schedule 必须带验收日期；具体实现和历史事实继续进入 `docs/iterations/`，不得通过重写历史记录来制造当前状态。
