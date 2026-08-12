# Rardar Roadmap

> Last updated: **2026-08-12**
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
→ 安全公网入口
→ 更强个性化 / Agent 能力
```

任何新功能如果要求放宽 generation、Audit、Stable ID、Action history 或 Runtime 安全边界，都必须拆成独立工程轮。

---

# Now — 当前主线

## N1. 完成 SERVER-NATURAL-RUN-02

状态：**进行中**

目标：

- 将 PR #19 historical snapshot byte-preserving hotfix 部署为 production exact release；
- 不手工 refresh、不追溯 publish 8/12 的三个 ready forensic candidates；
- 等待下一次 08:00 natural run；
- 验证：

```text
natural trigger
→ source
→ static analysis
→ signals
→ Schema
→ Audit
→ byte-exact history archive
→ ready generation
→ authoritative publication
→ lastSuccessfulRefreshAt
```

完成定义：

```text
Always-on unattended publication = VERIFIED
```

在该门禁前，不把 Public Edge 或产品 PR 合并与 Runtime 验证混在一起。

---

## N2. 集成 Launch Decision Flow

状态：**Draft PR #18**

目标：把现有数据页面收敛成真正的决策路径：

```text
Today / Daily Five
→ Why now
→ Evidence
→ Risk
→ Project Detail
→ Watch / Action / Feedback
```

已在 Draft 中完成：

- Home / Search / Project Detail 统一 Decision Summary；
- Action、Watch、Feedback 语义分离；
- same-project 状态同步；
- stale-generation 写前 409；
- empty / error / stale UX；
- 375px / 768px / desktop 响应式；
- accessibility 基础门禁。

下一步：

1. 等 Runtime 自然 publication 门禁通过；
2. 对齐最新 `main`；
3. 完整 Verify；
4. Ready / merge；
5. 再决定是否立即部署 UI release。

---

## N3. Signal → Project Audited Association v1

状态：**待开发**

目标：只有在同一 generation 中存在 authoritative repository identity 时，Signal 才能关联项目。

预期 contract：

```text
Signal source repository
→ strict repository normalization
→ recompute Stable Project ID v1
→ exact match in same-generation Catalog
→ canonical /project/v1/<projectId>
```

禁止：

- title matching；
- slug guessing；
- fuzzy repository matching；
- source 直接提供 projectId 并被无条件信任。

无充分证据时继续 signal-only。

---

# Next — 上线与身份收口

## X1. PROD-DEPLOY-02 Public Edge

状态：**未开始**

前置：

- Server Primary 稳定；
- 自然 publication 验证通过；
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

## X2. SEC-SSH-HARDEN-01

状态：**待执行**

当前 `rardar-deploy` 为部署与回滚保留了 bootstrap `NOPASSWD: ALL`。

目标：

- 明确日常发布所需 sudo surface；
- 保留可验证 rollback；
- 收紧或移除临时全量 NOPASSWD；
- 评估 deployment key rotation；
- 不让 runtime account `rardar` 获得 SSH / sudo 权限。

---

## X3. P1-6C2 Legacy Collision History

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