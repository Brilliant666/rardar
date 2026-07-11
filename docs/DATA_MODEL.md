# Rardar Data Model

本文记录 Rardar 的 JSON 数据契约、generation 发布边界和兼容规则。它描述结构与发布协议，不替代 `pipeline.audit_data` 的跨文件语义审计。

## 核心产物

| 产物 | 路径 | Schema | 版本字段 |
| --- | --- | --- | --- |
| 当前 generation 指针 | `data/current.json` | `current-generation.schema.json` | `schemaVersion` |
| generation manifest | `data/generations/<id>/manifest.json` | `generation-manifest.schema.json` | `schemaVersion` |
| GitHub 事实快照 | `<generation>/snapshots/latest.json`、`history/*.json` | `github-snapshot.schema.json` | `schema_version` |
| 技术动态 | `<generation>/signals/latest.json` | `technical-signals.schema.json` | `schemaVersion` |
| 只读静态证据 | `<generation>/analysis/*.json` | `static-evidence.schema.json` | `schemaVersion` |
| 项目中文画像 | `<generation>/enrichment/*.json` | `project-enrichment.schema.json` | `schemaVersion` |
| 动态中文画像 | `<generation>/signals/enrichment.json` | `signal-enrichment.schema.json` | `schemaVersion` |
| 前端目录 | `<generation>/catalog/latest.json` | `catalog.schema.json` | `schemaVersion` |
| Codex 队列 | `<generation>/queues/codex.json` | `codex-queue.schema.json` | `schemaVersion` |

这里的 `<generation>` 表示 `data/current.json` 一次解析后得到的 `data/generations/<generationId>/`。页面、API、审计命令和下一轮增长基线不得分别重读指针或拼接 flat 路径。

Schema 使用 JSON Schema Draft 2020-12，并限制必填字段、对象额外字段、字段类型、数组成员、枚举、时间、HTTP(S) URL、`owner/name` 仓库身份、字符串长度和数值范围。Schema 只引用仓库内文件，验证过程不会联网获取契约。

## 验证顺序

```text
严格 JSON 解析
→ JSON Schema 校验
→ repository 与证据文件名核对
→ 完整候选 generation 的跨文件一致性审计
→ manifest 记录全部产物 SHA-256 与审计摘要
→ 候选目录原子重命名为不可变 generation
→ `current.json` 临时文件、fsync 与原子替换
```

`pipeline/schema_validation.py` 提供：

- `validate_payload`：返回包含 JSON Pointer 的全部结构错误；
- `require_valid`：失败时抛出 `ArtifactValidationError`；
- `load_validated_json`：严格解析并验证单个文件；
- `validate_data_tree`：直接验证给定 flat 树或候选 generation；
- `strict_json_loads`：拒绝重复键、`NaN` 和 `Infinity`；
- `strict_json_dumps`：禁止写出非标准数值；
- `atomic_write_validated_json`：校验产物类型与目标路径后，在同目录暂存并原子替换。

`npm run data:validate` 是独立结构验证命令：存在 current 指针时，它先验证指针、manifest、路径和哈希，再校验同一个 generation；不存在指针时才接受完整、合法的旧 flat 树用于迁移。`npm run data:audit` 对同一个已解析 generation 执行数量、时间、URL、增长、信源、历史和队列一致性检查。指针一旦存在，任何解析失败都直接失败，不回退到 flat 数据。

## Generation 发布协议

```text
读取并固定 base generation
→ 在 data/generations/.candidates/<id>/ 构建完整候选
→ Schema gate
→ 跨文件 audit gate
→ ready manifest + 全部产物哈希
→ 获取 data directory 跨进程锁
→ 精确比较 baseGenerationId（CAS）
→ 候选目录原子重命名
→ 原子替换 current.json
```

- `current.json` 是唯一可变发布状态，字段包含当前代、上一代、发布时间和 manifest 哈希；
- ready generation 不允许原地修改；读取时会再次核对 manifest 与全部产物哈希；
- Git 属性对 `data/current.json` 与 `data/generations/**` 禁用换行转换，保证不同平台 checkout 后仍保持 manifest 绑定的原始字节；
- `healthy` 或只有 warning 的 `degraded` 审计结果可以发布，`errorCount` 必须为 0；
- Schema、审计、临时写入、目录重命名、指针替换或并发 CAS 任一步失败，旧 current 和增长快照保持不变；
- 构建、Schema 或审计失败写入 failed manifest；发布冲突后的 ready candidate 与指针中断后的 orphan generation 保持不可变，错误码、candidate ID 和阶段由命令或 scheduler 状态记录；
- 中断后已重命名但尚未被指向的 orphan generation 可安全重试；
- `npm run data:generation:publish -- <generation-id>` 可重试 ready candidate 或 orphan 的同一套 CAS 发布协议；
- 回滚必须显式指定保留的 ready generation，重新执行完整验证后再原子改指针；
- `refresh` 必须产生晚于当前快照的新增长基线；`derive` 的快照和 history 哈希必须与 base generation 完全一致。

## 写入边界

以下入口在候选或 staging 写入前复用同一契约：

- GitHub 与技术动态采集 CLI；
- 第三方仓库只读静态分析输出；
- catalog 与 Codex queue 独立调试 CLI；
- `data:refresh` 完整候选生成与发布；
- `data:derive` 从当前事实和 flat enrichment staging 本地重建并发布。

候选内部的关联文件仍使用批量写入，先完成所有 payload 的验证和严格序列化再替换；对读者可见的边界则只有最后一次 `current.json` 切换。候选内部任一失败不会修改当前已发布 generation。

独立采集器和静态扫描器在共享锁外完成网络与磁盘扫描，并先验证候选 payload；锁内只重复边界验证、比较产物时间和执行原子替换。时间早于现有正式文件的候选会被拒绝，项目画像/静态证据也不能覆盖已属于另一个仓库的碰撞文件名，因此慢任务不会以旧结果回写，也不会长期占用数据锁。

Codex enrichment 采用显式草稿和 staging 边界：先将结果写到 `data/` 之外，再运行 `python -m pipeline.ingest_enrichment --kind project|signal --input <draft>`。入口会先解析 `..` 与符号链接并拒绝整个 `data/` 树内的草稿，再在共享数据锁内严格解析、校验、按仓库身份确定 flat staging 目标并原子替换。队列中的 `outputPath` 表示 staging 归属，不授权直接覆盖；只有后续 `data:derive` 通过 generation gates 后才会成为页面数据。

Project enrichment v1 还绑定两项来源版本：`sourcePushedAt` 必须与当前 catalog 项目的同名字段字符串完全相同，`sourceAnalysisAt` 必须与当前静态证据的 `analyzed_at` 字符串完全相同。Codex 只能从队列原样复制；repository、任一来源版本不一致、`analyzedAt` 无有效时区或画像时间早于来源静态证据时，catalog 和 queue 都把画像判为过期，generation audit 不允许它作为当前画像发布。ingest 负责 Schema、草稿边界和时间先后校验，不把进入 flat staging 等同于正式发布。

## 兼容与迁移

契约与 generation 迁移不改变事实或评分含义：

1. Snapshot v1 保留既有 snake_case `schema_version`。早期 history 没有查询健康字段；Schema 接受该基础形状，latest 的查询覆盖继续由审计验证。
2. 五份带可信 `analyzed_at` 的静态证据迁移为 v1。两份缺少可信分析时间的历史证据标记为 v0；没有补造时间，v0 也不会被当作当前证据。
3. 四份能与可信当前静态证据形成真实时间顺序的项目画像，从 catalog 和 analysis 文件机械补入 `sourcePushedAt`、`sourceAnalysisAt`。两份静态证据没有可信 `analyzed_at`，另有一份画像早于当前静态证据；三者均保留为 legacy v0，不补造时间且永远不视为当前画像。
4. Signal enrichment v1 保留旧式条目的顶层 `generatedAt` 回退；新条目应保存逐条时间绑定。
5. Catalog 内项目级 `capturedAt` 是显示文本，顶层 `capturedAt` 才是 RFC3339 时间，两者不会混用。
6. 首个 generation 从合法旧 flat 树机械复制产物，不修改快照、history、静态证据、画像或评分；只把 Codex 队列的输入证据路径重建为该不可变 generation，并生成 manifest/current 的发布元数据。
7. `data/current.json` 缺失时，完整旧 flat 树仍可通过 `data:generation:bootstrap` 一次迁移；current 存在后，flat 的 snapshot、catalog、signals 和 queue 不再是网页或增长基线。
8. flat `analysis/`、`enrichment/` 和 `signals/enrichment.json` 继续作为静态分析/Codex staging。创建新候选时，只有目标缺失或 staging 的真实来源时间严格更新，才允许覆盖 base generation，避免旧 flat 文件回写新 generation。

未知版本或未版本化的新数据会失败。以后收紧字段或改变含义时应新增 Schema 版本和显式迁移，不得静默把旧数据解释为新版。

## 安全与回滚

Schema 与 generation 验证不执行候选仓库代码、不安装其依赖、不读取用户 Git 配置，也不改变静态分析的资源上限。generation ID、manifest 产物路径与符号链接都经过逃逸检查；路径必须留在当前 data/generations 根内。

正常回滚使用 `npm run data:generation:rollback -- <generation-id>`，它只会指向重新验证通过的保留代。代码回滚时可以恢复上一版消费者并继续使用保留的 flat 兼容数据；无需反向改写既有事实，也不得从缺失时间补造历史。

本地管理器在启动任何子服务前检查声明的 Python 运行依赖。缺失时只输出 `python -m pip install -r requirements.txt` 并退出，不自动安装，也不启动会持续失败重启的 scheduler。

Schema 不能单独解决跨文件一致性、历史增长、缓存新鲜度或 slug 碰撞。前两项由 generation audit gate 处理；稳定项目 ID、追加式行动事件、评分语义和 verify/CI 仍属于后续独立迭代。
