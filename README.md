# Rardar

> 证据优先的开源软件情报、能力发现与项目复用决策系统。
>
> **Rardar helps surface projects worth attention now and turns evidence into a next action.**

[![Verify](https://github.com/Brilliant666/rardar/actions/workflows/verify.yml/badge.svg?branch=main)](https://github.com/Brilliant666/rardar/actions/workflows/verify.yml)
![Node](https://img.shields.io/badge/Node-%3E%3D22.13-339933?logo=node.js&logoColor=white)
![Python](https://img.shields.io/badge/Python-%3E%3D3.10-3776AB?logo=python&logoColor=white)
![Status](https://img.shields.io/badge/status-active%20development-orange)

Rardar 面向个人开发者和小型工程团队，不只回答“最近什么项目很热”，而是尝试把公开技术生态里的事实、静态证据、可解释评分、能力画像、用户反馈和真实工程行动连成一个决策闭环：

```text
公开技术生态
→ 事实采集
→ 静态证据
→ 可解释排序
→ 能力画像 / 深读队列
→ 任务匹配
→ 项目判断
→ 用户行动
→ 反馈与后续推荐
```

它最终想帮助用户回答：

- 最近真正发生了什么？
- 哪些项目现在值得优先关注？
- 某项能力是否已经有成熟实现？
- 这个项目为什么值得看，有什么证据和风险？
- 应该继续观察、打开研究、隔离试用、浅克隆，还是确认复用？

---

## 快速导航

- [当前项目进度](#当前项目进度)
- [核心能力](#核心能力)
- [产品工作流](#产品工作流)
- [系统架构](#系统架构)
- [快速开始](#快速开始)
- [数据与安全原则](#数据与安全原则)
- [开发与验证](#开发与验证)
- [部署状态](#部署状态)
- [项目文档](#项目文档)
- [路线图](#路线图)

---

## 当前项目进度

> 生产里程碑快照：**2026-08-23**。更细的完成项、进行中事项和生产状态见 [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md)。

Rardar 已经从“本地数据面板原型”推进到具备完整数据发布边界、稳定项目身份、用户行动状态、自动调度和 Linux Always-on Runtime 的工程阶段。

| 领域 | 状态 | 当前结论 |
| --- | --- | --- |
| 数据采集与 generation 发布 | ✅ 已建立 | Schema → Audit → immutable generation → atomic `current.json` |
| GitHub 项目评分与证据 | ✅ 已建立 | Attention / Endurance / Engineering Readiness / Evidence Completeness 等可解释维度 |
| 第三方仓库静态分析 | ✅ 已建立 | 只读、资源有界、不执行陌生代码 |
| Stable Project ID | ✅ 主链完成 | Catalog、D1、API、canonical route、客户端交互均使用 `projectIdVersion: 1` |
| Action / Feedback / Recommendation | ✅ 已建立 | append-only Event + State、幂等写入、个性化有限重排 |
| Verify / CI | ✅ 已建立 | Node 22.13.1 + Python 3.10，统一 `npm run verify` |
| CI exact release artifact | ✅ VERIFIED | exact main Verify SHA、manifest/checksum、fresh extraction、offline Python install 与 runnable acceptance 已通过 |
| CI artifact Production release | ✅ FULLY VERIFIED | release `29a844504376b8432dfa01202f2817ac376cd490` 已通过离线激活进入 Server Primary |
| Managed Runtime | ✅ 已建立 | Manager 唯一拥有 Website + Scheduler，默认每日 08:00 Asia/Shanghai |
| Linux Always-on 部署 | ✅ VERIFIED | Server Primary ACTIVE；Windows Primary STOPPED |
| 无人值守自然刷新 | ✅ VERIFIED | 截至 2026-08-23 已独立验证自然 Scheduler refresh、publication 与 Schema/Audit；运行快照不作为永久 current 声明 |
| Resource hardening | ✅ PASS | swap、swappiness 与 systemd memory guardrails 已完成基础验收 |
| Launch Decision Flow | ✅ 已合并 | PR #18 已把 Why now → Evidence → Risk → Action 产品流合入 `main`（`4e9c0ea`） |
| Public Edge | ✅ ACTIVE | [`https://rardar.cosflow.icu`](https://rardar.cosflow.icu)；HTTPS + 整站 Basic Auth 的私有公网 MVP，不是匿名公开产品 |
| Signal → Project audited association | ✅ 已实现 | 仅以同一 generation 的 `signal.repo` 精确验证 Stable ID；证据不足时继续 signal-only |
| Trending fact foundation | ✅ 已合并 | 两小时 append-only Observation 与 audited 24h Explosion Artifact 已进入 `main` |
| Trending Producer Runtime | ✅ ACTIVE | Production 已自然运行两小时 Observation 与每日 Explosion；feature flag 与 Scheduler-only token 边界保持不变 |
| Near-real-time Discover Artifact | 🚧 本轮实现 / 未部署 | 独立 immutable store、Today exact 排除、确定性阶段和只读 Audit；Production 激活另行验收 |
| P1-6C2 legacy collision history | ⏸ Deferred | 不影响当前 Stable ID 主链，但历史 collision 生命周期尚未收口 |
| TrendRadar/P2 能力 | 🧭 Backlog | Research Profile、Momentum Lifecycle、Alerts/Digest、MCP 等尚未进入当前主线 |

### 最近里程碑

- **PR #8**：引入 collision-safe Stable Project ID。
- **PR #9 / #13**：把真实行动、反馈、推荐、路由和客户端交互迁移到稳定项目身份。
- **PR #14**：Runtime schedule、freshness 与健康状态进入可观测合同。
- **PR #15**：Always-on Linux 部署工程、systemd、preflight / online checker 完成。
- **PR #16**：修复 Linux stable-read 对同长度原地改写的完整性缺口。
- **PR #17**：补齐 systemd `AF_NETLINK` Runtime 契约并完成 Server Primary cutover。
- **PR #19**：修复 daily rollover 历史快照重新序列化导致的 byte-exact publication 拒绝。
- **SERVER-NATURAL-RUN-02**：8/13 与 8/14 连续两次无人干预自然发布成功，Always-on unattended operation 已验证。
- **PR #18**：Launch Decision Flow 已以 `4e9c0ea` 合入 `main`，统一 Why now → Evidence → Risk → Action / Watch / Feedback 决策路径。
- **PR #21**：Signal → Project audited association 已完成；关联只来自同一 verified generation 中可精确验证的 `signal.repo`。
- **PROD-PRODUCT-RELEASE-01 事故**：Server Primary 上的长期 `npm ci` 遇到 registry 失败并与 live workerd 争用 3.8 GiB、无 swap 的主机内存，触发 OOM；服务与 catch-up 已恢复，但旧的 co-located release preparation 不再受支持。
- **PR #22 / RELEASE-ARTIFACT-01**：CI-built Exact Release Artifact v1 已通过真实 main artifact、离线安装与 runnable acceptance。
- **PR #23 / PUBLIC-HOST-ALLOWLIST-01/02/03**：Vite exact FQDN Host 合同已合并、随 exact artifact 部署并完成正反向 Host 验收。
- **PROD-PRODUCT-RELEASE-02**：`29a844504376b8432dfa01202f2817ac376cd490` 已通过 CI artifact 离线激活到 Server Primary。
- **SERVER-NATURAL-RUN-03**：截至 2026-08-23，08:00（Asia/Shanghai）自然刷新独立发布 generation `20260823T000005118713Z-e5cfd5b8c5c9`，Schema/Audit 与 publication 通过。
- **OPS-RESOURCE-HARDEN-01**：基础资源门禁已通过。
- **PROD-DEPLOY-02**：HTTPS + 整站 Basic Auth 的 Public Edge 已验收并保持 ACTIVE。
- **PR #26 / #27 / #28**：append-only Trending Observation、Linux create-only settlement 与 audited 24-hour Explosion Artifact 已合并并已进入 Production 自然运行。

---

## 核心能力

### 1. 项目发现与事实采集

Rardar 以 GitHub 项目事实为主干，并接入技术动态 / Signal 数据。当前正式刷新流程会：

- 按查询规则发现候选仓库；
- 保存真实 GitHub API 快照；
- 记录 Star、Fork、Issue、仓库年龄、更新与 push 时间等事实；
- 归档历史快照，计算真实观测区间增长；
- 维护信源健康状态；
- 把一次刷新中的全部结果绑定到同一个 generation。

### 2. 可解释评分，而不是一个神秘总分

Rardar 把不同问题拆开回答：

- **Attention**：现在是否值得优先看？
- **Endurance**：是否存在持续热度 / 长期生命力线索？
- **Engineering Readiness**：静态工程证据是否完整？
- **Evidence Completeness**：当前证据覆盖到什么程度？
- **Reuse Fit**：只有拿到具体任务和约束后才有资格判断，不会用通用热度冒充任务适配度。

评分会公开事实、代理指标、限制和升级条件；没有当前证据时不会伪造“深度分析”或“可直接复用”。

### 3. 有界的第三方仓库静态分析

默认流水线对候选项目执行只读静态检查：

- 不安装依赖；
- 不运行仓库脚本、测试或构建；
- 不加载用户 Git 配置和凭据；
- clone / archive 有时间、大小、文件数和解压上限；
- 进程树清理无法确认时 fail closed；
- symlink、junction、reparse point 和路径逃逸有独立安全边界。

### 4. Stable Project ID

新 Catalog 使用：

```text
projectIdVersion: 1
projectId: <readable-prefix>--<repository-sha256-prefix>
```

Stable Project ID 由规范化 GitHub `owner/repo` 机械计算，避免旧 slug 规则产生碰撞。canonical 项目 URL 为：

```text
/project/v1/<projectId>
```

旧 `/projects/<slug>` 只作为兼容入口：唯一匹配时 302 到 canonical route，未知返回 404，歧义返回 409。

### 5. 用户真实行动、反馈与推荐

Rardar 不把“觉得有用”当成真正工程行动。

当前 D1 模型明确分离：

- **Action Event**：发生过什么，append-only；
- **Action State**：现在处于什么阶段；
- **Feedback**：推荐质量反馈；
- **Decision history / metrics**：用于周指标与后续推荐。

核心北极星指标是：

> **Weekly Acted Projects / 近 7 天已行动项目数**

即近 7 天真正发生“尝试 / 克隆 / 复用”等工程行动的不同 Stable Project ID 数量。

### 6. Codex 深读队列与能力画像

每次 generation 可以生成 `queues/codex.json`，把尚未完成中文能力画像的重点项目与 Signal 按优先级排队。画像写入 staging 后通过 `data:derive` 重新校验并发布，不直接覆盖当前数据。

### 7. Always-on Runtime

Rardar 已具备 Linux 单机长期运行工程：

```text
systemd
└─ Rardar Manager
   ├─ Website
   └─ Scheduler
```

核心边界：

- systemd 只管理一个 foreground Manager；
- Manager 是 Website 与 Scheduler 的唯一 owner；
- Producer 启用后仍由同一个 Scheduler 串行运行固定两小时 Observation → Discover，并在 08:00 按 Observation → Refresh → Explosion → Discover 排序；不新增 cron、timer 或 service；
- Website 与 Runtime status 只监听 loopback；
- exact release 与 data / D1 / runtime / cache / logs / backups 分离；
- 启动前做只读 deployment preflight；
- 服务运行后可执行 online deployment check；
- 数据 stale 与结构损坏使用不同健康语义。

---

## 产品工作流

当前 `main` 已经把项目发现、评分、详情、行动和反馈收敛为以下用户路径：

```text
Home / Daily Five
→ 为什么现在值得看
→ Evidence
→ Risk
→ Project Detail
→ Watch / Action / Feedback
→ 后续个性化推荐
```

Signal 只有在 `signal.repo` 能被同一 verified generation 的 Catalog 精确重算并验证 Stable ID 时，才提供 canonical 项目入口；否则继续 signal-only。Rardar 不会通过仓库名片段、slug、标题或中文 enrichment 做模糊猜测。

---

## 系统架构

```text
GitHub API / 技术信源
        │
        ▼
  Python Pipeline
  ├─ Collect
  ├─ Static Analysis
  ├─ Signals
  ├─ Scoring
  ├─ Schema Validation
  └─ Cross-file Audit
        │
        ▼
 Immutable Generation
        │
        ├── data/current.json ── atomic pointer
        │
        ▼
  Vinext / React App
  ├─ Home / Search / Signals
  ├─ Project Detail
  ├─ Recommendations
  └─ Health / Runtime status
        │
        ▼
       D1
  ├─ Action Events
  ├─ Action State
  ├─ Feedback
  └─ Decision History

systemd → Manager → Website + Scheduler
```

技术栈：

- **Node.js 22.13+**
- **Python 3.10+**
- **React 19 / Next.js 16 / Vinext / Vite**
- **Cloudflare-compatible runtime / D1 / Drizzle ORM**
- **systemd**（Always-on Linux profile）

---

## 快速开始

### 环境要求

- Node.js `>= 22.13`
- Python `>= 3.10`
- npm
- Git

### 安装

```bash
npm ci
python -m venv .venv
```

macOS / Linux：

```bash
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip check
export RARDAR_PYTHON="$PWD/.venv/bin/python"
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip check
$env:RARDAR_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
```

### 完整验证

```bash
npm run verify
```

`verify` 是推荐的统一门禁，覆盖：

- lint；
- Python tests；
- Schema validation；
- cross-file Audit；
- production build；
- Node / HTTP 行为测试；
- production dependency audit；
- data / Runtime isolation guards。

### 启动开发站点

```bash
npm run dev
```

默认仅监听 loopback。

### Managed Runtime

```bash
npm run local:start
npm run local:status
npm run local:stop
```

默认调度：

```text
08:00 Asia/Shanghai
```

`RARDAR_TRENDING_PRODUCER_ENABLED` 默认且未配置时为 `false`，因此保持上面的 daily-refresh-only 行为。经过独立部署授权设为严格小写 `true` 后，唯一 Scheduler 才会增加 Asia/Shanghai 偶数整点 Observation → Discover 与每日 08:00 Explosion derive；`GITHUB_TOKEN` 只进入 Scheduler child，不进入 Website、状态 JSON、日志或浏览器。Discover Repository 能力合并不表示 Production Discover 已启用。

Discover `trending-discover-v2` 只发布两类可解释信号：最近 4 小时首次进入候选池，或在实际观察窗口内达到 `+10 Star / +1%` 双通道之一且至少有 2 个连续正增长区间。观察满 20 小时的项目只有先通过同一质量门禁，才进入后端 `near_validation`（产品文案“待日榜验证”）。门禁不使用 AI、预测或综合评分，页面允许诚实空态；Artifact 同时冻结发布原因和聚合抑制原因供 Audit 重算。

### 常用数据命令

```bash
npm run data:generation:status
npm run data:validate
npm run data:audit
npm run data:refresh
npm run data:derive
npm run data:discover
npm run data:discover:status
npm run data:discover:audit
```

正式数据发布不是“直接改 JSON”，而是先生成 candidate，经过 Schema + Audit，再原子发布 generation。

---

## 数据与安全原则

Rardar 的几条硬边界：

1. **事实与判断分离**：AI / 中文画像不能覆盖 GitHub 与信源事实。
2. **证据先于结论**：没有当前证据时不输出确定性能力判断。
3. **陌生代码默认不执行**：默认雷达流水线只做受限静态分析。
4. **Audit 通过后才发布**：失败 candidate 不得替换上一代健康数据。
5. **一次请求只读一个 generation**：避免页面混合不同代数据。
6. **Stable ID 优先**：新业务状态不再以可碰撞 slug 作为身份。
7. **Event 与 State 分离**：指标来自事件，UI 当前状态来自 State。
8. **个性化不能覆盖事实主干**：偏好只做有限重排。
9. **Always-on 不等于扩大权限**：公网入口、DNS、TLS 和生产 secret 都是独立边界。

长期原则见 [`docs/RARDAR_NORTH_STAR.md`](docs/RARDAR_NORTH_STAR.md)。

---

## 开发与验证

GitHub Actions 会在：

- Pull Request → `main`
- push → `main`

运行同一个 `npm run verify`。

CI 当前使用：

```text
Node 22.13.1
Python 3.10
Ubuntu latest
```

独立 `Release Artifact` workflow 不替代 Verify。它只在同仓库 `main` push 的 Verify SUCCESS 后运行，并固定使用 Ubuntu 24.04 x86_64、Node 22.13.1 与 Python 3.12 wheel target，生成包含完整 `node_modules`、`dist`、`wheelhouse`、manifest 与 SHA-256 的 exact release artifact。

当前主线开发采用小步、可审计 PR。Runtime、数据迁移、用户状态、产品 UI 和 Public Edge 尽量拆成独立变更，避免一个 PR 同时改变多个信任边界。

---

## 部署状态

Rardar 已完成 Linux Always-on Server Primary cutover、exact CI artifact 离线发布与 Public Edge 激活。2026-08-23 的 milestone code baseline 与 Production release 均为 `29a844504376b8432dfa01202f2817ac376cd490`；公网入口为 [`https://rardar.cosflow.icu`](https://rardar.cosflow.icu)。纯文档 `main` 提交或尚未部署的开发提交不会改变 Production release。当前访问模式是 HTTPS + 整站 Basic Auth 的**私有认证生产 MVP**，不是匿名公开产品，认证凭据不存储在仓库。

Production 仍采用 exact release + atomic `current` symlink：`npm ci`、Verify、build 和 Python wheel preparation 只在 GitHub CI 完成；服务器只执行 artifact checksum/manifest 验证、解包、offline wheelhouse 安装、preflight、原子切换与受控 restart，不在 active release 内 `git pull` 或在线构建。

网络边界没有因 Public Edge 放宽：Website 与 Runtime status 分别只监听 `127.0.0.1:3000` 和 `127.0.0.1:3002`；Nginx 在 80/443 完成 TLS 与 Basic Auth 后只反向代理到 3000，3002 不进入代理也不暴露公网。

部署与回滚协议见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。

---

## 项目文档

| 文档 | 用途 |
| --- | --- |
| [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) | 当前完成度、活跃 PR、生产状态、近期里程碑 |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Now / Next / Later 路线图 |
| [`docs/RARDAR_NORTH_STAR.md`](docs/RARDAR_NORTH_STAR.md) | 长期使命、原则、北极星指标 |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | 数据契约、generation、Stable ID、D1 模型 |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Always-on Linux 部署、路径、preflight、rollback |
| [`docs/RARDAR_AUDIT_BASELINE.md`](docs/RARDAR_AUDIT_BASELINE.md) | 审计基线与已确认风险 |
| [`docs/RARDAR_EVOLUTION_PROTOCOL.md`](docs/RARDAR_EVOLUTION_PROTOCOL.md) | 迭代与兼容演进协议 |
| [`docs/iterations/`](docs/iterations/) | 每轮重要工程决策与验证证据 |

---

## 路线图

当前方向不是继续堆“排行榜功能”，而是让 Rardar 从数据雷达走向可用的工程决策系统。

当前唯一的 `Now` 是 `PRODUCT-NEXT-PHASE-DISCOVERY`：等待人工讨论真实使用痛点与下一阶段价值方向，尚未授权实现，也尚未创建产品 branch / PR。Research Profile、Momentum Lifecycle、Alerts / Digest、MCP 等仍只是候选方向；本次里程碑收口不替用户选择优先级。

完整路线与门禁见 [`docs/ROADMAP.md`](docs/ROADMAP.md)。

---

## 项目定位与 TrendRadar 的区别

Rardar 会参考 TrendRadar 等优秀开源雷达项目在**项目主页、快速导航、部署体验和用户信息架构**上的做法，但产品目标不同：

- TrendRadar 更偏向热点资讯聚合、筛选、推送和 AI 分析；
- Rardar 更聚焦**开源软件项目发现、证据验证、工程复用决策和真实行动闭环**。

因此 Rardar 不追求把所有热点源都接进来，而优先保证：

```text
事实可信
→ 证据可解释
→ 项目身份稳定
→ 推荐不会越过证据
→ 行动可以被记录和验证
```

---

## 当前阶段

Rardar 仍处于 **Active Development**，但私有认证生产 MVP 已上线。

它已经具备真实数据流水线、原子发布、Stable ID、D1 用户状态、Verify CI、Launch Decision Flow、Signal → Project audited association、CI-built exact release、经过独立自然运行验证的 Always-on Server Runtime，以及受 Basic Auth 保护的 HTTPS Public Edge。下一产品阶段尚未确定，必须先进行人工产品讨论。

如果你是在评估代码，请优先从 `docs/PROJECT_STATUS.md`、`docs/RARDAR_NORTH_STAR.md` 和最近的 `docs/iterations/` 开始。
