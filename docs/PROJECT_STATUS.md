# Rardar Project Status

> Last updated: **2026-08-14**
>
> 本文记录“Rardar 现在做到哪”。长期使命和不变量看 [`RARDAR_NORTH_STAR.md`](RARDAR_NORTH_STAR.md)，未来路线看 [`ROADMAP.md`](ROADMAP.md)，具体工程证据看 [`iterations/`](iterations/)。

## 一句话状态

Rardar 已经完成从“本地项目雷达原型”到“有审计的数据发布系统 + Stable Project ID + 用户行动闭环 + Linux Always-on Runtime”的主干工程；连续两次自然 Scheduler refresh 已验证无人值守运行，当前产品主线是 Launch Decision Flow 与 Signal → Project audited association。

---

## Repository 状态

本文档分支所基于的 `main`：

```text
8436834f49eb5d90f4b52dfc58ca02c483183286
Preserve historical snapshot bytes during refresh (#19)
```

PR #19 已合并，修复 2026-08-12 首次 Server Primary 自然刷新暴露的 historical snapshot byte-exact publication producer bug。

GitHub Actions 的最终状态以仓库 `Verify` workflow 为准；`main` 与所有目标为 `main` 的 PR 都必须经过统一 `npm run verify`。

当前主要开放产品 PR：

- **PR #18 — Make Rardar's decision flow launch-ready**
  - Branch: `feat/launch-decision-flow`
  - 状态：Draft
  - 已完成 6 个 P0 + 6 个高价值 P1 launch 问题修复；
  - 引入 Why now → Evidence → Risk → Action 的统一决策呈现；
  - Action / Watch / Feedback 分离；
  - generation-bound client state 与 stale-generation 409 写前门禁；
  - 由于 `main` 已在 PR #18 创建后继续前进，正式集成前需要重新对齐最新 main 并完整 Verify。

---

## Production Runtime 状态

已确认的生产拓扑：

```text
Ubuntu Server Primary
└─ systemd
   └─ Rardar Manager
      ├─ Website
      └─ Scheduler

Windows Primary = STOPPED
```

已完成：

- Ubuntu 24.04 x86_64 Server Primary cutover；
- exact release + atomic `/opt/rardar/current`；
- systemd 单 Manager ownership；
- Website `127.0.0.1:3000`；
- Runtime status `127.0.0.1:3002`；
- deployment offline / online checker；
- restart rehearsal；
- AF_NETLINK Runtime compatibility；
- SSH deployment alias 与独立 deployment user；
- 生产数据、Vinext/D1 与 release 分离。

### 2026-08-12 首次自然运行

第一次 Server Primary 无人值守 08:00 refresh 已证明：

```text
Natural trigger       PASS
Source collection     PASS
Analysis              PASS
Signals               PASS
Schema                PASS
Audit                 PASS
Ready candidate       PASS (3 attempts)
Authoritative publish FAIL
```

publication failure：

```text
refresh_base_snapshot_not_archived
```

根因不是 Scheduler、systemd、采集或 Audit，而是 refresh producer 把 published base snapshot 从 CRLF JSON 重新序列化成 LF JSON，语义相同但 bytes 不同，因此被正确的 byte-exact history invariant 拒绝。

PR #19 已在 `main` 修复：

- cloned base snapshot 使用共享 `stable_read`；
- 从同一份稳定原始 bytes 解析语义与创建 history；
- 不再重新序列化历史快照；
- create-only / no-replace archive；
- 冲突、symlink、reparse、损坏和竞态继续 fail closed。

### 2026-08-13 / 2026-08-14 连续自然运行验证

`SERVER-NATURAL-RUN-02 = PASS`，`Always-on unattended operation = VERIFIED`。

- 2026-08-13 08:00（Asia/Shanghai）无人干预运行发布 generation `20260813T000002931860Z-111fffa574b0`；historical snapshot base/archive 字节数和 SHA-256 精确一致，Schema、Audit、publication 与 CAS 全部通过。
- 2026-08-14 08:00（Asia/Shanghai）第二次连续自然运行发布 generation `20260814T000003142671Z-e14314b022b4`，于 08:00:52 完成并返回 `SUCCESS`。
- 两轮均为 `humanTriggered: false`；全过程保持 single Scheduler、`restartCount = 0` 与可信 telemetry。

因此 Runtime 主线不再是当前产品开发 blocker。Public Edge 和 SSH hardening 仍属于后续独立授权任务，不因 Always-on VERIFIED 自动完成。

---

## 能力完成度

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| GitHub facts collection | ✅ | 真实 API 快照、历史归档、增长区间 |
| 技术 Signal | ✅ | 48h Signal 与信源健康；项目关联仍保持谨慎 |
| Immutable generation | ✅ | candidate → Schema → Audit → atomic pointer |
| Historical rollback | ✅ | retained generation 显式验证与 rollback |
| Cross-file Audit | ✅ | 发布前跨产物一致性检查 |
| Explainable scoring | ✅ | 多维评分，不用单一热度替代任务适配 |
| Static repository analysis | ✅ | 有界 clone/archive，不执行陌生代码 |
| Stable Project ID | ✅ 主链 | Catalog v3、D1、API、route、client 主链已迁移 |
| Legacy collision lifecycle | ⏸ Deferred | P1-6C2 尚未收口历史 collision |
| Action Event / State | ✅ | append-only event + canonical state |
| Feedback | ✅ | 与真实工程 Action 分离 |
| Personalized recommendation | ✅ v1 | 有限重排，不覆盖事实与风险 |
| Codex queue / enrichment | ✅ | staging → derive → validated generation |
| Runtime freshness | ✅ | fresh / stale / invalid 语义明确 |
| Verify CI | ✅ | Ubuntu Node 22.13.1 + Python 3.10 |
| Local Managed Runtime | ✅ | Manager + Website + Scheduler |
| Linux Always-on deployment | ✅ | Server Primary 已建立 |
| Natural unattended publish | ✅ VERIFIED | 8/13 与 8/14 连续自然发布成功；Schema/Audit、CAS 与 byte-exact history invariant 通过 |
| Launch Decision Flow | 🚧 Draft | PR #18 已实现，未进入 main |
| Public Edge | ⏳ | 未配置公网 reverse proxy / TLS / DNS |
| Signal → Project association | ⏳ | 等 audited Stable ID association contract |
| Research Profile | 🧭 Backlog | P2 |
| Momentum Lifecycle | 🧭 Backlog | P2 |
| Alerts / Digest | 🧭 Backlog | P2 |
| MCP | 🧭 Backlog | P2 |

---

## 已完成的重要工程阶段

### Phase A — 数据可信边界

- PR #2：Data contracts
- PR #4：Generation / atomic publication
- PR #6：Scoring semantics
- PR #7：Repeatable Verify + GitHub CI

结果：Rardar 不再是“修改 JSON 然后网页直接读”，而是有正式 generation、Schema、Audit 和 rollback 边界的数据产品。

### Phase B — 用户行动闭环

- PR #5：Action events
- D1 Event / State / Feedback / Decision history
- Weekly Acted Projects 北极星指标
- 幂等 Action 写入与推荐反馈

结果：Rardar 开始衡量用户是否真的尝试、克隆和复用项目，而不是只统计页面浏览。

### Phase C — Stable Project Identity

- PR #8：Stable Project ID contract
- PR #9：D1 Action / Feedback identity migration
- PR #13：Client / route stable identity

结果：项目主链不再依赖可碰撞 slug。canonical route 使用 `/project/v1/<projectId>`。

仍未完成：P1-6C2 historical legacy collision lifecycle。

### Phase D — Runtime / Deployment

- PR #14：Runtime readiness、schedule、freshness
- PR #15：Always-on Linux deployment
- PR #16：Linux stable read integrity
- PR #17：systemd AF_NETLINK compatibility
- PROD-DEPLOY-01：Server Primary cutover
- PR #19：Historical snapshot byte-preserving refresh

结果：Rardar 已有真实 Linux Server Primary 和每日 Scheduler，不再依赖 Windows 笔记本持续在线；8/13 与 8/14 连续自然运行进一步验证 Always-on unattended operation。

### Phase E — Productization（进行中）

PR #18 已在 Draft 中完成：

```text
Home
→ Why now
→ Evidence
→ Risk
→ Detail
→ Watch / Action / Feedback
```

这轮目标是把已有的数据能力组织成用户能直接理解和采取行动的产品，而不是继续堆指标面板。

---

## 已知边界 / 技术债

### 1. P1-6C2 Legacy collision history

Stable ID 当前主链可以正常运行，但跨 generation 的 legacy slug collision 生命周期仍未最终解决。现有 collision gate 不应为了方便而放宽。

### 2. Signal 暂不猜测 Project

当前 Signal 没有 authoritative Stable Project association 时保持 signal-only。下一步应建立：

```text
Signal repository fact
→ canonical repository normalization
→ recompute projectId v1
→ verify project in same generation Catalog
→ canonical project link
```

不能使用 title / slug / fuzzy repo name 猜测。

### 3. Public Edge 尚未开放

Server Primary 已可长期运行，但：

- 3000 / 3002 只允许 loopback；
- DNS / TLS / reverse proxy 尚未进入 production；
- Public Edge 必须单独审计访问控制、headers、rate limit 和 health 暴露范围。

### 4. Deployment SSH 暂时使用宽 sudo

`rardar-deploy` 当前保留 bootstrap 阶段的 `NOPASSWD: ALL` 作为回滚通路。Server Primary 和 Public Edge 稳定后需要执行独立 `SEC-SSH-HARDEN-01`。

### 5. Vinext production compatibility

当前 build 是硬门禁，但正式 Manager 使用已验证的 Vite/Vinext compatibility entry，而不是把未验证的 `vinext start` 当成 production target。这一选择需要在 Vinext upstream 能力稳定后重新评估。

---

## 当前最重要的工作流

### 1 — Launch Decision Flow integration

`SERVER-NATURAL-RUN-02` 已通过。当前应重新集成并审查 PR #18，使产品主页、搜索、详情和 Action/Watch/Feedback 形成统一决策体验。

### 2 — Signal → Project Audited Association

在同一 verified generation 中建立 authoritative repository → Stable Project ID 关联；证据不足时继续 signal-only，不按 title、slug 或模糊 repository 猜测。

### 3 — Public Edge（独立上线主线）

产品主路径稳定后，再以 `PROD-DEPLOY-02` 独立处理正式域名、TLS、reverse proxy 与公开 API 安全边界。

---

## 如何维护本文

重要 PR 或生产门禁完成后，只更新以下内容：

1. Repository main / active PR；
2. Production Runtime；
3. 能力完成度；
4. 三个当前门禁；
5. 已知边界。

具体实现细节继续进入 `docs/iterations/`，不要重新把完整工程历史堆进 README。
