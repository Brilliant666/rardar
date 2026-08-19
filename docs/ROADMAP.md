# Rardar Roadmap

> Last updated: **2026-08-18**
>
> 这是执行路线，不是承诺时间表。长期产品原则由 [`RARDAR_NORTH_STAR.md`](RARDAR_NORTH_STAR.md) 定义；当前完成度看 [`PROJECT_STATUS.md`](PROJECT_STATUS.md)。

## 路线原则

Rardar 当前不缺“更多指标”，缺的是把已有可信数据能力变成稳定、可持续、可行动的产品。

因此路线优先级固定为：

```text
可信数据
→ 稳定身份
→ 可解释判断
→ 真实用户行动
→ Always-on 运行
→ 产品决策流
→ 可重复、离线可激活的 exact release
→ 安全公网入口
→ 更强个性化 / Agent 能力
```

任何新功能如果要求放宽 generation、Audit、Stable ID、Action history 或 Runtime 安全边界，都必须拆成独立工程轮。

---

# Recently completed

## SERVER-NATURAL-RUN-02

状态：**PASS / VERIFIED**

- 2026-08-13 自然发布 generation `20260813T000002931860Z-111fffa574b0`；
- 2026-08-14 第二次连续自然发布 generation `20260814T000003142671Z-e14314b022b4`；
- 两轮均无人干预，single Scheduler、`restartCount = 0`；
- Schema、Audit、CAS、authoritative publication 与 historical snapshot byte-exact invariant 全部通过。

结论：Always-on unattended operation 已验证，Runtime 不再是当前产品开发 blocker。

## Launch Decision Flow v1

状态：**MERGED / completed**

- PR #18 已以 `4e9c0eadaf612fdda99d6e988a28720ff336953f` 合入 `main`；
- Home / Search / Project Detail 已统一 Why now → Evidence → Risk → Action；
- Action、Watch、Feedback 保持独立，客户端状态绑定 Stable ID 与页面 generation；
- 产品发布与 Public Edge 仍由独立任务决定，本次合并不代表已经部署。

## Signal → Project Audited Association v1

状态：**completed capability**

- PR #21 仅以 Signal 自身的 `repo` 作为关联权威；
- repository 在同一 verified generation 的 Catalog 中精确重算并验证 Stable Project ID 后，才产生 canonical `/project/v1/<projectId>` 入口；
- 无 repo 或当前 Catalog 无精确项目都是合法 signal-only 状态；
- title、slug、basename、中文 enrichment、模糊规则、LLM 与 source-provided projectId 都不能决定项目归属；
- 产品发布仍由独立 Runtime 任务决定，本项完成不代表已经部署到生产。

---

# Now — 发布准备隔离

---

## N1. RELEASE-ARTIFACT-01

状态：**实现完成 / 等待首个 main artifact Bootstrap 验收**

目标：在固定 Ubuntu 24.04 x86_64 GitHub runner 中，为成功通过 main `Verify` 的 exact SHA 构建完整 Node runtime、`dist`、Python 3.12 wheelhouse、manifest、archive checksum，并完成 fresh extraction / offline acceptance。

边界：

- Production 不再执行 `npm ci`、`npm install` 或 build；
- artifact 必须绑定成功 Verify 的完整 SHA，排除 `data/`、secrets 与不安全 symlink；
- 本任务不访问 Production、不部署、不调整 swap，也不改变 Runtime / Scheduler / data。

---

# Next — 上线与安全

## X1. PROD-PRODUCT-RELEASE-02

状态：**等待 RELEASE-ARTIFACT-01 合并及 main artifact SUCCESS**

目标：将包含 Launch Decision Flow 与 Signal → Project audited association 的 exact CI artifact 安全激活到现有 Server Primary。

边界：

- exact artifact download → checksum → extract → offline Python venv → preflight → backup → atomic switch → restart；
- 不访问 npm registry，不在服务器 install/build Node dependencies；
- 记录并验证下一次自然 08:00 refresh；
- 不与 Public Edge、DNS/TLS、SSH hardening 或 resource hardening 混合。

---

## X2. OPS-RESOURCE-HARDEN-01

状态：**独立评估**

基于生产正常 RSS 与 memory pressure 单独评估 swap、`MemoryHigh`、`MemoryMax` 与 OOM policy。该任务不能替代 CI artifact，也不能与发布协议变更合并。

---

## X3. PROD-DEPLOY-02 Public Edge

状态：**未开始**

前置：

- Server Primary 稳定；
- Server Primary 的自然 publication 已验证；
- 目标 UI release 完成审查。

需要独立设计：

- production domain；
- reverse proxy；
- TLS；
- Cloudflare / DNS；
- security headers；
- rate limiting；
- API / health 暴露范围；
- 是否增加访问认证；
- 3000 / 3002 始终不直接公网暴露。

Public Edge 不是简单“加一个 Nginx 配置”，它会改变真实攻击面，因此必须独立 PR / 独立部署门禁。

---

## X4. SEC-SSH-HARDEN-01

状态：**待执行**

当前 `rardar-deploy` 为部署与回滚保留了 bootstrap `NOPASSWD: ALL`。

目标：

- 明确日常发布所需 sudo surface；
- 保留可验证 rollback；
- 收紧或移除临时全量 NOPASSWD；
- 评估 deployment key rotation；
- 不让 runtime account `rardar` 获得 SSH / sudo 权限。

---

# Deferred — 身份历史与后续产品能力

## D1. P1-6C2 Legacy Collision History

状态：**Deferred，但仍未完成**

目标：解决同一个 legacy slug 在不同 Stable Project ID / generation 中出现时的历史生命周期，而不改写 append-only 事实。

边界：

- 不修改已合并 migration `0004`；
- 新 D1 变化必须新 migration；
- 不放宽当前 legacy ambiguity publication gate；
- retained generation、rollback 和历史 Action/Feedback 必须继续可审计。

它不阻塞当前产品主线，但不能被误写成“P1-6 已全部完成”。

---

# Later — P2 产品能力

以下能力应建立在稳定 Decision Flow 和 Public Edge 之后，不作为当前上线 blocker。

## L1. Research Profile

目标：让用户能描述自己的研究方向、技术栈、任务类型和约束，使推荐不再只依赖通用偏好。

可能包含：

- 技术主题；
- 语言 / 框架；
- 项目阶段；
- 许可证偏好；
- 风险容忍度；
- 复用目标。

原则：Profile 只能影响排序和解释，不能覆盖事实与风险。

---

## L2. Momentum Lifecycle

目标：从“一个时间点的热度”升级为可解释的生命周期：

```text
emerging
→ accelerating
→ sustained
→ cooling
→ revived
```

前置：足够历史 observation，禁止用一次 snapshot 伪造生命周期。

---

## L3. Alerts / Digest

目标：从“用户主动打开雷达”扩展到：

- Daily digest；
- 关注项目重大变化；
- 新证据出现；
- 项目从观察进入值得行动；
- 已关注项目风险恶化。

通知必须基于已发布 generation，而不是 candidate / transient state。

---

## L4. MCP / Read-only Agent Interface

目标：让外部 Agent 能读取：

- 当前已验证项目；
- Evidence；
- Signal；
- Decision Summary；
- Research Profile 下的推荐。

第一阶段保持 read-only，不允许 MCP 绕过 Action API、generation boundary 或 production deployment 权限。

---

## L5. Advanced Personalization

可能包括：

- acted-project ranking semantics；
- 更长期的偏好学习；
- Research Profile + Action history 联合排序；
- 冷启动解释；
- “为什么这条是为你推荐”的可解释性。

禁止把用户偏好描述成全球趋势。

---

## L6. Watch Lifecycle

当前 `saved` 是单向记录。

未来可以独立设计：

- unwatch / unsave event；
- Watch history；
- Watch reason；
- Watch → Action conversion。

不能直接删除历史 `saved` Event 来实现取消关注。

---

# 不进入当前路线的事项

以下内容不是当前优先级：

- 为了“功能多”继续堆更多通用评分；
- 默认执行第三方仓库代码；
- 用 AI confidence 替代可验证事实；
- 在没有数据历史时伪造趋势预测；
- 为了兼容旧 slug 放宽 Stable ID collision gate；
- 让多个 Scheduler / cron / systemd timer 同时拥有 refresh；
- 为了快速公网访问直接暴露 3000 / 3002；
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

最后一条是这版文档新增的维护要求：**项目说明必须和工程进度一起迭代，不再等到 README 明显落后后集中补写。**
