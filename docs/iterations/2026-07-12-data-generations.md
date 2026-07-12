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
- 为本地 Vinext Worker 增加 token 保护的同端口 Vite host 数据桥，Worker 不再直接读取宿主文件；
- 增加实际加载 published generation 的 `/api/health`，管理器以 HTTP 契约而不是 TCP 端口判断网站健康；
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

`current.json` 不存在时，合法且完整的旧 flat 树可通过 `npm run data:generation:bootstrap` 一次迁移。迁移复制原始产物，只重建队列中指向 generation 的输入路径，并新增 manifest/current 发布元数据。指针一旦存在，普通页面、调度、validate、audit 和正常 publish 的解析失败会 fail closed，不再静默回退 flat 数据；显式 rollback 是唯一受控灾难恢复入口。

本仓库已机械迁移到首代 `20260711T183728486430Z-72ff8eefea0d`。逐文件 SHA-256 比对确认 19 份事实、静态证据和画像产物与 flat 源完全一致；唯一重建的 `queues/codex.json` 全部 input path 都绑定到该 generation，manifest 审计为 `healthy`。迁移没有修改或补造任何来源时间。

flat `analysis/`、`enrichment/` 和 `signals/enrichment.json` 继续作为本地静态分析与 Codex staging。创建候选时，只有目标缺失或 staging 中的真实版本时间严格更新，才允许覆盖当前 generation 的同名产物。

## 回滚

运行：

```bash
npm run data:generation:rollback -- <generation-id>
```

目标必须是仍保留的 ready generation。本轮审查修正将 rollback 调整为：在同一个 canonical data lock 内，先核对目标路径、ready manifest、manifest digest、全部 artifact hash、Schema 与跨文件 audit，全部通过后才读取 current 并原子更新 pointer。目标失败时原 pointer 字节保持不变并返回结构化错误。

current 健康时继续执行原有严格逻辑；current pointer、manifest、generation 目录或 artifact 已损坏时，显式 rollback 可以继续恢复，但只独立验证旧 pointer 中安全的 `generationId` 与有限可信的 `publishedAt`。旧时间只有在不晚于恢复时当前 UTC 加五分钟时才用于保持新 `publishedAt` 严格递增；超过窗口的异常未来值、非法时间或递增溢出均降级使用当前 UTC，不能永久阻止恢复。该五分钟窗口只属于损坏 current 的 recovery，健康 current 的时间冲突规则保持不变。旧 manifest/hash/audit 不受信任，也不回退 flat 数据；symlink 或 junction pointer 的目标不会被读取为可信元数据，安全原子替换失败时返回结构化 `pointer_write_failed`，链接目标保持不变。

若候选已 ready 但发布冲突，或目录已重命名而 current 指针写入中断，可运行 `npm run data:generation:publish -- <generation-id>` 重试原候选；它不会重建或改写该 generation。

## 本地 Vinext 消费与健康

默认 `vinext dev` 使用请求级 Vite host bridge：Node host 每次调用现有 `loadPublishedBundle` 完整核对 current、manifest、ready 状态、artifact 清单与哈希，并一次性返回同一 generation 的网页 bundle；Cloudflare RSC Worker 只访问可信 Vinext 配置固定的 `127.0.0.1` bridge origin，并携带当前进程随机 token。入站 `Host` 不参与桥目标选择，不能把 token 导向其他回环端口。该方案不缓存旧代、不依赖 HMR，pointer 切换下一请求立即生效，损坏 current 时返回 503 且不回退 flat。

`/api/health` 只有在同一通道成功加载 generation 时才返回 200、`status: healthy` 与 `generationId`。本地管理器把非 200 或非法健康响应标为 degraded 并保存有界诊断，不因数据错误重启仍存活的 Vinext 进程；rollback 恢复后原进程自动恢复 healthy。Cloudflare plugin 与 D1 binding 保持原运行方式。此桥是本地 `vinext dev` 通道，不声称部署或 `vinext start` 能读取宿主数据。

## 验证结果

Node.js 22.13.1 与当前 Python 环境下：

- `npm run lint`：通过；
- Python：174 项测试通过，4 项真实 Windows 符号链接测试因当前用户权限不可用跳过，等价的可移植链接/真实路径边界测试已通过；
- `npm run data:validate`：本地每日刷新 generation `20260712T000000282772Z-0de7461784f8` 的 24 份产物通过，0 错误；该运行时 generation 不属于本轮修正提交；
- `npm run data:audit`：同一 generation 为 `healthy`，0 错误、0 警告；
- `npm run build`：通过，所有网页数据路由为 Dynamic；
- `npm test`：build 通过，5 项 Node 行为/渲染/真实 Vinext HTTP 测试通过；
- `npm run security:audit:prod`：0 个生产依赖漏洞。

真实 Vinext 子进程使用隔离的临时 generation、Cloudflare/D1 状态和随机回环端口完成验收：`GET /`、`GET /api/health`、`GET /signals`、`GET /search` 与 D1 `/api/actions` 均为 200；同一进程从 `http-generation-a` 切换到 `http-generation-b` 后立即返回新一代；损坏 current 时健康端点为 503、首页为 500 且完整 flat 诱饵没有被读取；显式 rollback 后原进程的健康端点和首页恢复 200。伪造入站 `Host` 指向另一回环监听器时，健康端点仍从固定 bridge origin 返回当前 generation，诱饵监听器收到 0 个请求，因此 token 不会随 Host 泄露。

新增行为测试覆盖 current manifest digest 不匹配、manifest 缺失、generation 目录缺失、artifact 被篡改、非法 JSON、链接型 pointer、损坏回滚目标保持 pointer 字节不变、目标验证后的再次篡改、与正常 publisher 的锁串行化，以及恢复后的 Python resolver、真实 validate/audit CLI 和 Node published-data loader。

新增时间边界测试覆盖 recovery 对五分钟内未来时间保持严格单调、超过信任窗口的异常未来值降级当前 UTC、递增溢出不阻止恢复、异常未来时间恢复后可立即发布新 derive generation，以及健康 current 继续执行原有 stale publication 规则。

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
