# 2026-07-12 · Audited data generations

## 目标

把 Rardar 的完整数据树从“逐文件替换后再审计”改为“候选 generation 先通过 Schema 与跨文件审计，再原子切换 current 指针”，确保失败数据不能成为网页事实源或下一轮增长基线。

## 本分支实现

- 为 generation manifest 与 `data/current.json` 增加版本化 JSON Schema；
- 对 current 与 generation 数据禁用 Git 换行转换，确保跨平台 checkout 不改变 SHA-256 绑定字节；
- 在 `data/generations/.candidates/<id>/` 构建完整候选，并记录全部产物 SHA-256；
- 固定执行 Schema gate → cross-file audit gate → ready manifest → 原子发布顺序；
- 使用跨进程 data lock 与 `baseGenerationId` CAS 裁决并发发布；
- 页面、API、验证、审计、调度器和增长基线只读取一次解析出的同一 generation；
- 本地 Vite watcher 忽略 generation 内部目录，避免 Windows 开发服务器占用候选目录并阻断同盘原子重命名；
- `refresh` 发布新快照，`derive` 保持快照与 history 基线不变；
- 构建、Schema 和审计失败保留 failed manifest；发布冲突后的 ready candidate 与指针写入中断后的 orphan generation 保持不可变，并通过稳定错误码、candidate ID、scheduler 状态和显式回滚命令诊断；
- 将 Codex 队列证据路径绑定到不可变 generation，同时保留 flat enrichment 作为受控 staging；
- 拒绝路径逃逸、符号链接、危险 generation ID、manifest/产物哈希不一致和 stale flat staging 回写；
- 首次 generation 机械迁移既有正式数据，不补造采集、推送、分析或画像时间。

## 行为保证

以下失败均不得切换 `current.json`：

- 单文件 Schema 失败；
- 跨文件一致性审计失败；
- 候选写入或 current 指针替换中断；
- 两个发布者以同一 base generation 竞争；
- 旧快照、旧 base 或过期发布时间尝试发布；
- manifest、路径或任一产物被篡改。

已经加载的网页请求继续使用原 generation；后续请求只会在 current 原子替换后整体看到新一代。`derive` 不能修改当前快照或 history，因此不会推进或倒退真实增长基线。

## 兼容与迁移

`current.json` 不存在时，合法且完整的旧 flat 树可通过 `npm run data:generation:bootstrap` 一次迁移。迁移复制原始产物，只重建队列中指向 generation 的输入路径，并新增 manifest/current 发布元数据。指针一旦存在，解析失败会 fail closed，不再静默回退 flat 数据。

本仓库已机械迁移到首代 `20260711T183728486430Z-72ff8eefea0d`。逐文件 SHA-256 比对确认 19 份事实、静态证据和画像产物与 flat 源完全一致；唯一重建的 `queues/codex.json` 全部 input path 都绑定到该 generation，manifest 审计为 `healthy`。迁移没有修改或补造任何来源时间。

flat `analysis/`、`enrichment/` 和 `signals/enrichment.json` 继续作为本地静态分析与 Codex staging。创建候选时，只有目标缺失或 staging 中的真实版本时间严格更新，才允许覆盖当前 generation 的同名产物。

## 回滚

运行：

```bash
npm run data:generation:rollback -- <generation-id>
```

目标必须是仍保留的 ready generation；命令会重新核对 Schema、跨文件审计、manifest 与全部哈希，再原子更新 current 指针。失败时当前代保持不变。

若候选已 ready 但发布冲突，或目录已重命名而 current 指针写入中断，可运行 `npm run data:generation:publish -- <generation-id>` 重试原候选；它不会重建或改写该 generation。

## 验证结果

Node.js 22.13.1 与当前 Python 环境下：

- `npm run lint`：通过；
- Python：157 项测试通过，3 项真实 Windows 符号链接测试因当前用户权限不可用跳过，等价的真实路径/别名边界测试已通过；
- `npm run data:validate`：当前 generation 21 份产物通过，0 错误；
- `npm run data:audit`：`healthy`，0 错误、0 警告；
- `npm run build`：通过，所有网页数据路由为 Dynamic；
- `npm test`：build 通过，4 项 Node 行为/渲染测试通过；
- `npm run security:audit:prod`：0 个生产依赖漏洞。

本文件记录当前 Draft PR 分支的验证，不把尚未合并的实现标记为 main 已完成。

## 是否影响 North Star

不改变 Weekly Acted Projects 的定义或数值。它保证支撑推荐、行动入口与增长计算的数据来自一个经过审计、可回滚且不会混代的事实版本。

## 明确未处理

- 追加式行动事件；
- 评分语义；
- verify 与 GitHub Actions；
- 稳定项目 ID；
- UI、数据库、信源扩展、Agent 平台或部署。

## 已知后续风险

- 当前 Node consumer 为每个动态请求重新核对 manifest 中全部产物哈希；数据代数与 history 继续增长后，可增加以 pointer + manifest digest 为键的冻结缓存，但缓存失效仍必须服从 current 原子切换；
- 发布竞争行为测试覆盖同进程双发布者，跨进程锁另有独立测试；完整 publish CAS 的多进程端到端压力测试可在后续 verify/CI 迭代补充；
- 正式写入 API 会拒绝 final generation namespace，但本地用户仍可手工改文件；任何此类修改会因 manifest/hash 校验 fail closed，后续 verify/CI 应阻止其进入 main。

本轮 Draft PR 创建后停止，不自动开始下一工程目标。
