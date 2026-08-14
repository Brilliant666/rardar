# Launch Decision Flow v1

## 状态与边界

本轮基线为 `d3794cb4a36b14cf8e10613968ac17a90818717e`，开发分支为
`feat/launch-decision-flow`。唯一目标是让用户从进入 Rardar，到找到一个值得关注的项目、理解依据与风险，再完成 Watch、Action 或 Feedback，形成连续、低摩擦的决策闭环。

本轮是 presentation 与交互工程，不改变事实来源、排序、评分、D1 Schema、generation Schema、refresh 或 scheduler。交付只进入 Draft PR；在 `SERVER-NATURAL-RUN-01` 通过前不得 Ready、合并或部署。

产品心智固定为：

> Rardar 不是 GitHub 数据展示站；它帮助用户判断“现在有什么项目值得关注，以及下一步该做什么”。

## 修改前的用户路径

修改前的主要路径为：

```text
/
→ 今日五项 / PersonalizedDailyList
→ /project/v1/<projectId>
→ Why now / score explanations / capabilities / evidence / risk
→ ProjectActions / FeedbackButtons
→ /watchlist

/signals → 外部动态来源
/search → 客户端任务匹配 → canonical project detail
/projects/<slug> → 同一 current Catalog 中唯一匹配时 302 canonical URL
```

已有基础边界是正确的：canonical URL、React key、Action、Feedback、recommendation 与 watch 状态关联均使用 `projectIdVersion: 1` 和 `projectId`；SSR 页面一次只调用一次 `loadPublishedData()`；首页 recommendation 结果还会核对 `generationId`。Action 与 Feedback mutation 已具备 in-flight、防旧 GET 覆盖和幂等保护。

但修改前的页面仍要求用户自行把多个指标拼成决策：Home、Search 与 Signals 没有统一卡片心智；详情页的 Action/Feedback 早于证据与风险；已处理状态在项目列表不可见；Search 在没有明确命中时仍显示“优先匹配”；stale 只在首页披露。

## 产品路径审计

严重度定义：

- **P0 launch blocker**：会让核心决策闭环不成立、产生错误信任或混淆正式业务状态；
- **P1 launch friction**：用户仍能完成任务，但需要额外推理、重复操作或无法判断失败；
- **P2 later**：需要新的数据契约、独立治理轮次，或不应阻塞 v1 launch。

| Issue | Route / component | Severity | User impact | Recommended change | Scope |
| --- | --- | --- | --- | --- | --- |
| Home 与 Search 没有统一的决策摘要 | `/`、`/search`、`ProjectCard`、`SearchWorkbench` | P0 | 用户必须从分数、趋势和标签自行推断是否值得看 | 复用同一 Decision presentation，按 Why now → Evidence → Risk → Action 展示 | 本轮 |
| 详情页核心信息顺序错误 | `/project/v1/<projectId>` | P0 | Feedback/Action 和大量评分先于证据、风险，窄屏下风险尤其靠后 | 将 Identity、Why now、Evidence、Risk、Action 放在首要阅读流，评分与 metadata 下沉 | 本轮 |
| Watch、Action、Feedback 表达混用 | `ProjectActions`、`FeedbackButtons`、`WatchlistClient` | P0 | “待确定”反馈被当作 watch，反馈按钮比业务行动更突出 | UI 层分开三种语义；现有 `saved` 表示已关注，Feedback 只评价推荐质量 | 本轮 |
| 已行动项目仍像未处理项目 | Home、Search、detail、watchlist | P0 | 用户无法辨认 opened/tried/cloned/reused，重复判断同一项目 | 按 Stable ID 读取一次 collection state，并在卡片与详情显示可见状态 | 本轮 |
| Search 无明确命中时仍返回“优先匹配” | `SearchWorkbench` | P0 | 无关项目被包装成匹配结果，破坏 evidence-first 信任 | 显示明确无结果；候选回退必须单独标注为“待验证”，不能伪造匹配度 | 本轮 |
| stale 只在首页显示 | `/signals`、`/search`、project detail | P0 | 直接入口会把旧数据读成当前 48 小时或当前判断 | 复用共享 freshness notice，并从该页面同一次 published bundle 读取 | 本轮 |
| 空列表和读取失败没有区分 | Daily、Signals、Evidence、Watchlist、Action/Feedback GET | P1 | 空白区域或接口失败被误解为“没有记录” | 提供 loading、empty、error 与 retry 状态，不显示技术异常 | 本轮 |
| Action/Feedback/Watch client read 未核对页面 generation | client decision state | P1 | pointer switch 时旧页面可能吸收新一代 client state | collection 响应加法返回 generation ID，客户端只接受页面 generation | 本轮 |
| Mutation 状态反馈不够明确 | `ProjectActions`、`FeedbackButtons`、Watch | P1 | 初始状态闪烁、GET 失败静默、disabled 不易辨认 | 明确 loading/disabled/success/error，监听现有 projectId 事件同步同页组件 | 本轮 |
| 长 repository 与触控尺寸缺少窄屏门禁 | cards、search、detail、evidence | P1 | 375px 下可能截断长标识，Feedback 控件触控区域过小 | 增加安全换行、44px 左右核心触控目标并验证 DOM 顺序 | 本轮 |
| Signal 没有 generation-bound Stable Project ID | `/signals`、`SignalCard` | P2 | 无法在不猜测 repo/slug 的情况下链接项目 | 保持 signal-only；未来通过 audited generation 契约提供 projectId | Deferred — `BLOCKED_DATA_CONTRACT` |
| Legacy slug 的歧义浏览器体验仍是结构化错误 | `/projects/<slug>` | P2 | 歧义或缺失 URL 不是完整产品页 | 由 P1-6C2 独立处理；继续保持唯一 302、未知 404、歧义 409，绝不猜第一项 | Deferred |
| 真正的 unwatch 没有对应事实模型 | Watch | P2 | `saved` 是追加式事实，当前只能可靠表达“未关注 / 已关注”，不能撤销 | 新增独立、可审计的 watch event/state 前不得伪造取消状态 | Deferred — `BLOCKED_DATA_CONTRACT` |
| 已行动项目的隐藏或排序降权 | recommendations | P2 | v1 只能显示已处理视觉状态，不能完全消除重复曝光 | 另行定义 acted-project ranking semantics，不在 presentation PR 修改排序 | Deferred |

## Decision presentation contract

统一的项目决策摘要只消费同一 verified generation 中已有字段，不增加 `decisionScore`、`homeScore` 或新的排序公式。

### Identity

- 显示项目名与 repository；
- canonical link、React key、状态关联和 API selector 必须使用 Stable Project ID；
- `projectId` 无需直接暴露给用户；slug 和 repository 只作为可读展示或 legacy URL 输入。

### Why now

首要结论来自现有 `whyNow`。补充事实只能从现有 `evidence`、`scoreExplanations.*.facts`、当前分析状态、增长口径与已有 recommendation reason 中机械选择 1～3 条并去重。

不得生成或暗示不存在的“AI 判断”“爆发概率”“预测增长”或新的趋势百分比。没有事实时明确显示证据仍待补充，不能用分数替代事实。

### Evidence

卡片必须让用户知道当前判断有多少可核查事实，并提供 canonical 详情或具体来源入口。详情页优先呈现最关键的事实来源，再展示完整评分语义与 supporting metadata。

没有 evidence 时显示明确空状态；不能留下一个空白 Evidence 区，也不能把风险或代理误写成已验证事实。

### Risk

风险只使用现有 `risk`、license、analysis availability 和 evidence limitations。存在风险时在 next action 之前展示；没有额外风险字段时明确说明“暂无结构化风险说明”，不自动宣称“低风险”。

### Action

系统 recommendation 是建议下一步，不是用户已经行动的事实。用户状态来自 Action Event/State；Action UI 清楚区分打开、试用、浅克隆和确认复用，并在成功后立即同步同一 projectId 的页面组件。

## Watch、Action 与 Feedback

三类语义必须独立：

- **Action**：用户实际发生的打开、试用、浅克隆或确认复用；Event 追加、State 投影，北极星仍只计算 tried/cloned/reused；
- **Watch**：v1 复用已有 `saved` Action 事实表达“关注 / 已关注”，不使用 localStorage 另造业务真相；
- **Feedback**：`有用 / 无用 / 复用 / 待确定` 只评价推荐或判断质量，可影响既有 personalization，但不能充当 Watch 或 Action；
- 同一页面内状态通过 Stable ID 与既有 `rardar:project-action` / `rardar:feedback` 事件同步；网络读取不得覆盖已成功 mutation；
- 同一页面重复出现的 Watch 控件共享 `generation + projectId + saved` in-flight 请求与幂等键，不能因双入口生成两条关注 Event；
- “待确定”不再自动代表“已关注”。

`saved` 是不可否认的历史事实，当前数据模型没有合法的撤销事件。真正的 unwatch 需要新的追加式 Event/State 契约与迁移，因此标记为 `BLOCKED_DATA_CONTRACT`；本轮不得通过删除 Event、篡改 State 或 localStorage 假状态实现。

## Stable identity boundary

- Project link、React key、selected state、Action、Feedback、Watch、recommendation association 都必须使用 `{ projectIdVersion: 1, projectId }`；
- 不得用 slug、repository display string、标题或数组位置作为 canonical selector；
- legacy `/projects/<slug>` 只在该请求的 current Catalog 中唯一匹配时跳转 canonical URL；
- 本轮不放宽 unresolved legacy collision publication gate，也不开始 P1-6C2；
- retired project 不得误映射到另一个 current project。

## Generation boundary

- 每个 SSR 页面只消费一次 `loadPublishedData()` 返回的 bundle；
- Decision presentation 不自行读取 `current.json`，也不从 flat data 回退；
- Home recommendation 继续要求 response generation 与页面 generation 相等；
- Action、Feedback 与 Watch collection state 只在 response generation 与页面 generation 一致时应用；
- mutation 把页面 generation 作为写前置条件提交；pointer 已切换时 API 在写 D1 前返回 `409 stale_generation`，UI 引导刷新页面而不是无限重试；
- 页面应暴露 generation marker，供真实 HTTP 测试验证 Home、Signals、Search 和 detail；
- pointer 原子切换后，下一请求可以看到新代，但单个响应不能混用两代；
- corrupt current 继续 fail closed，不因产品空状态而伪装成“没有推荐”。

## Empty、error 与 stale

产品状态必须区分：

| State | Product behavior |
| --- | --- |
| 没有今日推荐 | 说明当前没有通过验证的重点项目，并提供 Search/候选入口 |
| Search 无明确命中 | 明确说明未找到；可展示单独标注的待验证候选 |
| 没有 signals | 显示该窗口没有已验证动态，不渲染空列表 |
| 没有 evidence | 明确说明证据待补充，不把空白当成功 |
| recommendation API failure | 保留同 generation 的 evidence-base 排序，并披露个性化暂不可用 |
| Action/Feedback/Watch read failure | 不显示成“未处理”；提供可重试错误状态 |
| mutation failure | 保留上一个可信状态并允许重试，不使用阻塞式 `alert()` |
| published data stale | HTTP 200 degraded，所有核心入口都显示最近成功快照与数据年龄 |
| generation invalid/corrupt | 保持非 200 / fail closed，不降级到 flat 或伪造空状态 |

## Responsive 与 accessibility

本轮保持现有颜色、字体和 page shell，只调整层级、间距、卡片与响应式布局。交付前至少验证 375px、768px 和 desktop：

- Home → Search/Signals → canonical detail → Evidence/Risk → Watch/Action/Feedback 全程可键盘和触控完成；
- 页面不得横向溢出；长 repository、title、risk 和 evidence 文本允许安全换行；
- 核心按钮不能被 sticky header 或窄屏 grid 遮挡，触控目标清晰；
- Action/Watch/Feedback 使用真实 button，canonical navigation 与 evidence source 使用 link；
- pressed、disabled、loading 和 success/error 状态对辅助技术可读；
- 保留全局 `:focus-visible` 和 `prefers-reduced-motion`；
- heading 顺序反映 Why now → Evidence → Risk → Action，不只用视觉粗体表达结构；
- 关键信息不能只在 hover 出现。

## `BLOCKED_DATA_CONTRACT`

以下两项不能在本 PR 内安全实现：

1. **Signal → Project association**：当前 `TechnicalSignal` 只有可选 `repo`，没有 generation-bound `projectIdVersion/projectId`。UI 按 repo、slug 或 display string 关联会重新引入猜测归属。信号必须保持 signal-only，直到 audited generation 明确发布 Stable ID 关系。
2. **真正的 unwatch**：当前 `saved` 是追加式 Action 事实，没有撤销 Event 或独立 Watch State。删除历史或维护 localStorage 开关都会与正式事实冲突。需要单独的数据模型设计、migration、幂等和回滚协议。

发现这两项不会扩大本轮 scope，也不能成为修改 generation Schema 或 D1 的理由。

## Deferred work

本轮明确不实现：

- P1-6C2 legacy slug collision gate 与旧 URL 生命周期；
- Signal Stable ID association 与真正的 unwatch；
- Research Profile；
- Momentum Lifecycle；
- Alerts / Digest；
- MCP；
- acted-project hide/demote 或新的 ranking semantics；
- advanced personalization；
- TrendRadar/P2、新信源、新评分算法和 LLM 自动分析；
- failed candidate cleanup、Public Edge、DNS/TLS 或部署。

## 验证门禁

交付前必须使用当前开发 worktree 自己的 `.venv`，并在临时 data、临时 D1、随机回环端口中完成：

```powershell
$env:RARDAR_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
npm run verify
git diff --check
git diff -- data
git status --short --untracked-files=all
```

行为验证至少覆盖：

- Home decision card、Why-now facts、Evidence、Risk conditional、canonical link；
- Action、Watch、Feedback 的初始状态、mutation、失败重试和同 projectId 同步；
- 两个显示相似的项目不会串状态；
- Search 无明确命中、Daily/Signals/Evidence 空状态；
- stale warning 出现在所有核心入口；
- Home、Signals、Search、detail 的真实 Vinext HTTP 与 generation marker；
- pointer A → B 后新请求使用 B，单个 response 不混代；
- 375px、768px 和 desktop 的截图或等价布局检查；
- Python、Node、Schema、Audit、build 与 production security audit 全部通过；
- 测试结束没有残留进程、临时监听或对 3000/3002 的占用。

实际验证结果只记录真实执行结果，不由本文预先声称通过。

## 回滚

本轮没有 D1 migration、generation migration 或正式数据写入。若需要回滚：

1. 停止该开发分支的隔离测试进程；
2. revert 本轮 presentation/API additive commit；
3. 重新运行 `npm run verify`；
4. Action、Feedback 与 saved 历史事实保持原样，不删除或改写；
5. 不需要 rollback generation、恢复 D1 或修改 `data/current.json`。

若回滚期间发现业务事实与 UI 状态不一致，应停止并诊断，不能通过清空 localStorage、删除 Event 或修改 State 掩盖问题。

## 生产隔离与不触碰项

本轮不得访问 Server Primary、Windows Primary、生产 data/D1、生产凭据、3000/3002、systemd、EnvironmentFiles、scheduler 或 `/opt/rardar/current`。测试不得手工 refresh、publish generation、修改 pointer、部署 feature branch或清理 failed candidates。

与核心 Decision UI 无关的以下范围必须保持不变：

```text
pipeline/scheduler.py
pipeline/refresh*
generation schemas
drizzle migrations
deployment / systemd
runtime settings
formal data
```

本轮完成后仅创建 Draft PR，并等待 `SERVER-NATURAL-RUN-01` 与人工审查；不 Ready、不合并、不部署，也不自动开始 P1-6C2 或 TrendRadar/P2。
