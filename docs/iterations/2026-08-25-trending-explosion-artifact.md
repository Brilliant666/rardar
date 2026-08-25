# TRENDING-EXPLOSION-ARTIFACT-01 — Audited 24-hour GitHub Explosion Facts

日期：2026-08-25
状态：Draft 实现；只有对应 PR 合并到 `main` 后才算完成

## 唯一目标

把已经合并的 append-only `TrendingCaptureBundle v1` 机械派生为一个版本化、自包含、可审计的 24 小时 GitHub Explosion Artifact，供下一阶段 TopicEye/Rardar Intelligence Adapter 从单个已验证 generation 读取。

本轮不实现 TopicEye Adapter、UI、AI/Sub2API、找项目、D1、Scheduler/systemd 或 Production 部署，也不修改 Stable Project ID、Catalog 排序、Action/Feedback 历史或原始 observation。

## Observation → Explosion

原始两小时 capture 继续位于 generation 外的追加式账本，并计划保留 90 天。正式 Artifact 位于：

```text
data/generations/<generationId>/trending/explosion.json
```

derive 对指定 `08:00 Asia/Shanghai` 窗口端点执行 stable-read、strict JSON、capture Schema、payload digest、captureId、scheduled path 与 `windowEligible` 校验。当前端点必须可信；T-24h 端点存在且 eligible 才能形成 exact 窗口。

Artifact 的 `schemaVersion=1`，`policyVersion=trending-explosion-v1`。`window.state` 区分：

- `exact`：current 与 T-24h baseline 都存在且 eligible；
- `warming_up`：账本尚未运行满 24 小时；
- `baseline_missing`：更早 observation 证明历史已经覆盖目标基线，但该 slot 缺失或不 eligible。

## 自包含来源

候选 generation 保存用于完整重算的每一份源 capture 原始字节：

- `trending/sources/current.json`；
- baseline 存在时的 `trending/sources/baseline.json`；
- pending partial 计算实际消费的 `partial-XX.json`；
- baseline slot 缺失但需证明历史覆盖时的 `coverage-witness.json`。

source reference 同时保存 generation-relative path、原始 ledger path、captureId、scheduled/captured time、coverage、payload digest 和 raw file SHA-256。复制不重新序列化，全部文件进入 manifest artifact/hash 清单。原始 observation store 删除后，retained generation 仍能独立重算并通过 Schema、Audit、manifest integrity 与 rollback target verification。

## Exact、Pending 与 Conflict

身份连续性只使用 GitHub numeric repository ID；现有 Rardar Stable Project ID 不变。

`exactRanked` 对 current/baseline ID 交集计算：

```text
observedStarDelta = totalStars(current) - totalStars(baseline)
```

排序唯一为 `observedStarDelta DESC`、`totalStars DESC`、`repository ASC`，rank 从 1 连续递增。Artifact 保存全部 eligible 项（上限 500），不只保存 UI Top 20。rename/transfer 继续同一 numeric ID 并保存 previous repository；archived、fork、mirror 保持独立事实。

`pendingRanked` 收纳没有合法 exact baseline 的 current 项。最多扫描窗口内 11 个中间两小时 phase，计算真实 partial hours/delta；单观察点保持 null，不外推 24h，不占 exact rank。待验证排序先放有实际 partial delta 的项目，再按 partial delta、totalStars、repository 排序，上限 500。

Star 下降、current disabled 和跨端点 repository name→numeric ID 冲突进入 `conflicts`。三组 numeric ID 互斥；外部 reported delta、Attention、AI value、Feedback 或 Trending rank 都不能修复冲突或改变名次。

## Coverage 与事实/AI 边界

coverage 记录 current/baseline query、metadata failure 与 exact/pending/conflict 数量。端点 degraded、query incomplete/failure、metadata failure、warming up 或 baseline missing 都不会伪装成完整全站覆盖。

Artifact 只允许 GitHub observation、机械窗口、来源、覆盖和冲突。中文简介、能力、AI 爆发原因判断、confidence、model/reasoning effort、复用建议与工程成熟度一律拒绝，留给后续独立 AI Profile。

## Derive、幂等与 CAS

CLI：

```powershell
python -m pipeline.derive_trending_explosion `
  --data-dir <isolated-data> `
  --window-end 2026-08-24T08:00:00+08:00 `
  --dry-run
```

正式执行在 generation lock 外准备可信来源，再创建 `operation=derive` candidate，byte-exact 写入 source、生成 Artifact、验证既有业务事实未变化、执行 Schema/Audit，最后复用现有 base-generation CAS 原子切换 current。`--dry-run` 不创建 candidate、不改 pointer。

相同窗口与端点 payload/file digest 返回 `already_derived`；比 current Artifact 更旧的请求返回 `stale_explosion_window`；同窗口出现不同端点字节返回 `explosion_source_conflict`。若并发 refresh/derive 先发布，loser 返回 `stale_base_generation`，不覆盖 winner，ready candidate 保留诊断。

无关 generation 会机械重绑定并保留最新 Explosion 的 generationId，source bytes、窗口和事实不变，再完整审计；新 Explosion derive 才替换该 namespace。历史 generation 没有 `trending/` 继续合法。

## Audit

`pipeline.audit_data` 对出现 Explosion namespace 的 generation 追加只读审计：

- Artifact Schema、generationId 与 generation 目录；
- source path/inventory、no-follow 类型、file SHA、capture Schema/digest/identity；
- 24h 端点、eligible 与 window state；
- exact 完整交集、delta、全候选排序与连续 rank；
- pending partial 窗口、排序、无外推与连续 rank；
- conflict 完整性与三分区唯一性；
- coverage 计数与 source coverage；
- AI 字段不存在；
- 完整重算结果逐字段等于已发布 Artifact。

Audit 不读取已经过期的 raw ledger、不修复或改写产物。任何 source/ranking tamper 都返回 failed。

## 行为测试

隔离 fixture 包含至少 25 个 exact、5 个 pending 和 3 个 conflict，并覆盖：Schema/时间/rank/负数/重复身份/AI 字段，稳定 tie-break 与 500 上限，2h/12h/single-point pending，warming up、baseline missing/ineligible、rename/transfer、fork/mirror/archive，损坏 JSON/digest/identity、symlink/junction，byte-exact source copy、manifest inventory、业务事实不变、dry-run、same-window 幂等、stale window/source conflict、CAS loser，以及 raw store 删除后的 retained Audit/rollback 和 source/ranking tamper 负向控制。

测试只使用临时 data、合法 generation fixture 和本地 capture bytes；不调用 GitHub、不读取 Production、不写 D1，不占用 3000/3002。

## 回滚与下一步

应用代码回滚不删除 observation 或 retained generation。包含 Explosion 的 retained generation 自包含完整审计来源，可由本版本继续作为明确 rollback target；回滚到不认识新 artifact inventory 的旧代码前，必须先显式切换到一个经本版本完整验证、且不含 `trending/` 的历史健康 generation，再停止 Runtime、回滚应用并重新验证，不能假设旧代码会忽略新 namespace。无关 generation 对 latest Explosion 的 carry-forward 只修改 enclosing generation identity，不修改事实窗口。

只有本 PR 合并且最新 `main` Verify 成功后，才能开始独立 `RARDAR-INTELLIGENCE-ADAPTER-01`。Adapter 必须消费一次请求内同一个已验证 generation，不得自行重算 24h delta、读取 raw ledger、使用旧 Daily Five 或引入 POC fixture。
