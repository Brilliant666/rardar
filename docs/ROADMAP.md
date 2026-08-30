# Rardar Roadmap

> Last updated: **2026-08-26**
>
> 这是执行路线，不是承诺时间表。长期产品原则由 [`RARDAR_NORTH_STAR.md`](RARDAR_NORTH_STAR.md) 定义；当前完成度看 [`PROJECT_STATUS.md`](PROJECT_STATUS.md)。候选项的出现顺序不代表已经确定产品优先级。

## 路线原则

Rardar 当前不缺“更多指标”，缺的是把已有可信数据能力变成稳定、可持续、可行动的产品。

```text
可信数据
→ 稳定身份
→ 可解释判断
→ 真实用户行动
→ Always-on 运行
→ 产品决策流
→ 可重复、离线可激活的 exact release
→ 安全、认证的公网入口
→ 人工确认下一产品阶段
```

任何新功能如果要求放宽 generation、Audit、Stable ID、Action history、Runtime 或网络安全边界，都必须拆成独立工程轮。

Repository `main` 与 Production release 可以合法不同。只有完成 exact artifact、独立部署门禁和生产验收的 SHA 才能称为 Production release；文档合并或未部署的产品提交不会自动改变它。

---

# Recently completed

## Product and data foundations

- **Launch Decision Flow v1 — MERGED**：PR #18 将 Home / Search / Project Detail 收敛为 Why now → Evidence → Risk → Action，并保持 Watch、Action 与 Feedback 分离。
- **Signal → Project Audited Association v1 — COMPLETED**：PR #21 只以同 generation 中可精确验证的 `signal.repo` 建立 canonical Stable ID 入口；证据不足时保持 signal-only。
- **Stable identity / audited generation / action history — COMPLETED MAINLINE**：Schema、atomic generation、Stable Project ID、append-only Event + State 与 Verify CI 已形成可信主链；P1-6C2 历史 collision 生命周期仍单独 deferred。

## CI-built Exact Release Artifact v1

状态：**PASS / VERIFIED**

- PR #22 建立由成功 main Verify exact SHA 触发的 release artifact；
- manifest、archive checksum、fresh extraction、offline Python wheelhouse install 与 runnable acceptance 已通过；
- Production 不再执行 co-located `npm ci`、`npm install` 或 build。

## PROD-PRODUCT-RELEASE-02

状态：**PASS / FULLY VERIFIED**

- exact artifact 经过 checksum/manifest 校验、离线安装、preflight、backup 与 atomic switch；
- Production release 已更新为 `29a844504376b8432dfa01202f2817ac376cd490`；
- controlled restart 与 online checks 通过。

## SERVER-NATURAL-RUN-03

状态：**PASS / VERIFIED**

- 截至 2026-08-23，Server Primary 已独立完成 08:00（Asia/Shanghai）自然刷新；
- generation `20260823T000005118713Z-e5cfd5b8c5c9` 的 natural trigger、Schema、Audit 与 publication 通过；
- 这是带日期的验收快照，不将 generation 或 `nextRunAt` 写成永久 current 状态。

## OPS-RESOURCE-HARDEN-01

状态：**PASS**

- 2 GiB swap；
- `vm.swappiness=10`；
- systemd `MemoryHigh=2304M`；
- systemd `MemoryMax=infinity`；
- 资源门禁已解除，但 Production build 禁令保持不变。

## PUBLIC-HOST-ALLOWLIST-01 / 02 / 03

状态：**PASS / DEPLOYED**

- PR #23 的 Vite exact FQDN Host contract 已合并并随 exact artifact 激活；
- Website 保持 loopback-only，Nginx 保留 `Host: $host`；
- 受信 Host 通过，未知、兄弟和嵌套子域继续 fail closed；
- wildcard、leading-dot suffix、`allowedHosts=true`、Host rewrite 和 public bind 仍被禁止。

## PROD-DEPLOY-02 Public Edge

状态：**PASS / ACTIVE**

- [`https://rardar.cosflow.icu`](https://rardar.cosflow.icu) 已作为 HTTPS + 整站 Basic Auth 的私有认证生产 MVP；
- Nginx 80/443 只反向代理到 `127.0.0.1:3000`；
- Runtime status `127.0.0.1:3002` 不代理、不公开；
- Public Edge 激活未放宽 Production secret、release、data 或 Runtime 边界。

---

# Now — Near-real-time Discover fact contract

状态：**RARDAR-V2-RFC ACCEPTED / RARDAR-DISCOVER-REALTIME-01 IN DEVELOPMENT**

- P0：今日爆发榜 v2，以 Rardar 自有连续 observation 形成可审计的精确 24h Star 增量榜；
- P0：找项目 v2，以自然语言需求、可选公开 GitHub URL、动态召回、静态证据和同任务横向比较形成复用决策；
- AI v1：通过自托管 Sub2API 调用 `gpt-5.6-sol`，由 Rardar 自有 durable queue 与独立 Worker 负责异步、幂等、重试和熔断；
- 高价值资产库完整产品建设继续 Deferred，只积累最低限度历史事实；
- `TRENDING-OBSERVATIONS-01` 已由 PR #26 合并；PR #27 已解决 Linux same-slot create-only settlement 竞态，observation Schema、immutable capture bundle、九查询召回、26 小时 carry-forward、numeric repository ID 连续性、create-only store、单实例锁和只读 audit 均已进入 `main`。
- `TRENDING-EXPLOSION-ARTIFACT-01` 已由 PR #28 合并；eligible endpoint 的 24h exact/pending/conflict、byte-exact generation source copies、Schema/Audit、幂等和 CAS publication 已进入 `main`。
- Production 已自然运行既有两小时 Observation 与每日 Explosion。本工程轮为尚未形成完整 24 小时事实的项目增加独立 `TrendingDiscoverArtifact v1`：最新 eligible capture + 最多 26 小时 source copies + 当前 Today exact 排除、确定性三阶段、manifest/hash/digest、Audit 与原子 pointer。
- 同一唯一 Scheduler 在普通相位按 Observation → Discover，在 08:00 按 Observation → Refresh → Explosion → Discover；Discover 失败隔离，不新增 service/daemon，不修改 D1，也不让 AI 参与候选、阶段、增量或排序。
- Repository 实现不等于 Production Discover ACTIVE。Rardar 合并后由 TopicEye vendoring 最终合同并完成本地真实数据产品闭环；Production 激活只能由后续 `RARDAR-DISCOVER-RUNTIME-ACTIVATION-01` 完成。
- Today Artifact、精确排名、Stable Project ID、Action/Feedback、AI Provider 与 Find Project 均冻结；本轮不部署 Production。

推荐实现顺序：observation contract/store（已完成）→ audited 24h artifact（已完成）→ Production producer（已激活）→ audited near-real-time Discover artifact（当前）→ TopicEye safe adapter/static serving/UI（同一跨仓产品任务）→ 独立 Production Discover activation。其他产品切片仍必须分别经过独立、可审计、可回滚的 PR。

---

# Next / Deferred — 未排序的维护项与候选方向

以下条目被保留，但**没有在本次收口中重新排序，也不构成当前授权**。

## Maintenance

### SEC-SSH-HARDEN-01

收紧 bootstrap deployment sudo surface，在保留可验证 rollback 的同时评估 deployment key rotation；Runtime account 不获得 SSH / sudo 权限。

### `clash-sub.service` maintenance

作为与 Rardar 独立的主机服务维护，不与产品路线或 Rardar Runtime 权限合并。

### P1-6C2 Legacy Collision History

解决同一个 legacy slug 在不同 Stable Project ID / generation 中出现时的历史生命周期，不修改已合并 migration `0004`，不放宽 ambiguity publication gate，也不改写 append-only 事实。

## Product candidates

### Research Profile

候选目标：表达研究方向、技术栈、任务类型与约束。Profile 只能影响排序和解释，不能覆盖事实与风险。

### Momentum Lifecycle

候选目标：以充分历史 observation 建立 emerging / accelerating / sustained / cooling / revived 生命周期，不能由单次 snapshot 伪造。

### Alerts / Digest

候选目标：基于已发布 generation 提供 Daily digest、关注项目重大变化、新证据与风险变化提醒。

### MCP / Read-only Agent Interface

候选目标：为外部 Agent 提供 verified project、Evidence、Signal、Decision Summary 与推荐的只读接口，不绕过 Action API 或部署权限。

### Advanced Personalization

候选目标：研究 Profile、Action history、冷启动和可解释个性化；不得把个人偏好描述成全球趋势。

### Watch Lifecycle

候选目标：设计 unwatch/unsave event、Watch history、Watch reason 与 Watch → Action conversion；不能通过删除历史 `saved` Event 实现取消关注。

---

# 不进入当前路线的事项

- 为了“功能多”继续堆更多通用评分；
- 默认执行第三方仓库代码；
- 用 AI confidence 替代可验证事实；
- 在没有数据历史时伪造趋势预测；
- 为兼容旧 slug 放宽 Stable ID collision gate；
- 让多个 Scheduler / cron / systemd timer 同时拥有 refresh；
- 绕过 Basic Auth，或直接公开 3000 / 3002；
- 把 ready/unpublished forensic candidate 追溯发布成正式数据。

---

# 路线检查点

每个阶段完成后至少回答：

1. 它改善了哪个真实用户决策？
2. 它使用的是事实、判断，还是用户状态？三者是否分离？
3. 是否仍绑定一个 authoritative generation？
4. Project identity 是否仍使用 Stable ID？
5. 是否改变了 D1 append-only history？
6. 是否扩大 Runtime / network / deployment 权限？
7. 是否有 deterministic test 和 rollback 路径？
8. README / PROJECT_STATUS / ROADMAP 是否需要同步更新？

项目说明必须和工程进度一起迭代；产品方向必须由人工讨论确认，不能由文档收口任务自动选定。
