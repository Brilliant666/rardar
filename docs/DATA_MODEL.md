# Rardar Data Model

本文记录 Phase 0 的 JSON 数据契约、验证入口和兼容规则。它描述结构，不替代 `pipeline.audit_data` 的跨文件语义审计。

## 核心产物

| 产物 | 路径 | Schema | 版本字段 |
| --- | --- | --- | --- |
| GitHub 事实快照 | `data/snapshots/latest.json`、`history/*.json` | `github-snapshot.schema.json` | `schema_version` |
| 技术动态 | `data/signals/latest.json` | `technical-signals.schema.json` | `schemaVersion` |
| 只读静态证据 | `data/analysis/*.json` | `static-evidence.schema.json` | `schemaVersion` |
| 项目中文画像 | `data/enrichment/*.json` | `project-enrichment.schema.json` | `schemaVersion` |
| 动态中文画像 | `data/signals/enrichment.json` | `signal-enrichment.schema.json` | `schemaVersion` |
| 前端目录 | `data/catalog/latest.json` | `catalog.schema.json` | `schemaVersion` |
| Codex 队列 | `data/queues/codex.json` | `codex-queue.schema.json` | `schemaVersion` |

Schema 使用 JSON Schema Draft 2020-12，并限制必填字段、对象额外字段、字段类型、数组成员、枚举、时间、HTTP(S) URL、`owner/name` 仓库身份、字符串长度和数值范围。Schema 只引用仓库内文件，验证过程不会联网获取契约。

## 验证顺序

```text
严格 JSON 解析
→ JSON Schema 校验
→ repository 与证据文件名核对
→ 现有业务一致性审计
→ 严格 JSON 序列化
→ 临时文件与原子替换
```

`pipeline/schema_validation.py` 提供：

- `validate_payload`：返回包含 JSON Pointer 的全部结构错误；
- `require_valid`：失败时抛出 `ArtifactValidationError`；
- `load_validated_json`：严格解析并验证单个文件；
- `validate_data_tree`：验证完整 `data/` 树；
- `strict_json_loads`：拒绝重复键、`NaN` 和 `Infinity`；
- `strict_json_dumps`：禁止写出非标准数值；
- `atomic_write_validated_json`：校验产物类型与目标路径后，在同目录暂存并原子替换。

`npm run data:validate` 是独立结构验证命令。`npm run data:audit` 会先报告 `schema_validation_failed`，再执行数量、时间、URL、增长、信源、历史和队列一致性检查。

## 写入边界

以下入口在正式写入前复用同一契约：

- GitHub 与技术动态采集 CLI；
- 第三方仓库只读静态分析输出；
- catalog 与 Codex queue 独立 CLI；
- `data:refresh` 批量发布；
- `data:derive` 本地重建。

批量写入会先完成所有 payload 的验证和严格序列化，之后才创建临时文件。任一文件失败时，整批不会开始替换，因此旧的健康数据保持不变。

独立采集器和静态扫描器在共享锁外完成网络与磁盘扫描，并先验证候选 payload；锁内只重复边界验证、比较产物时间和执行原子替换。时间早于现有正式文件的候选会被拒绝，项目画像/静态证据也不能覆盖已属于另一个仓库的碰撞文件名，因此慢任务不会以旧结果回写，也不会长期占用数据锁。

Codex enrichment 采用显式草稿边界：先将结果写到 `data/` 之外，再运行 `python -m pipeline.ingest_enrichment --kind project|signal --input <draft>`。入口在共享数据锁内严格解析、校验、按仓库身份确定目标并原子替换；正式文件不能作为自己的输入草稿。队列中的 `outputPath` 表示最终归属，不授权直接覆盖该文件。

## 兼容与迁移

本轮只增加结构版本，不改变事实或评分含义：

1. Snapshot v1 保留既有 snake_case `schema_version`。早期 history 没有查询健康字段；Schema 接受该基础形状，latest 的查询覆盖继续由审计验证。
2. 五份带可信 `analyzed_at` 的静态证据迁移为 v1。两份缺少可信分析时间的历史证据标记为 v0；没有补造时间，v0 也不会被当作当前证据。
3. 现有项目画像机械增加 `schemaVersion: 1`；`model` 继续可选，其他内容未改写。
4. Signal enrichment v1 保留旧式条目的顶层 `generatedAt` 回退；新条目应保存逐条时间绑定。
5. Catalog 内项目级 `capturedAt` 是显示文本，顶层 `capturedAt` 才是 RFC3339 时间，两者不会混用。

未知版本或未版本化的新数据会失败。以后收紧字段或改变含义时应新增 Schema 版本和显式迁移，不得静默把旧数据解释为新版。

## 安全与回滚

Schema 验证不执行候选仓库代码、不安装其依赖、不读取用户 Git 配置，也不改变静态分析的资源上限。回滚本轮只需撤销验证代码、Schema、依赖声明和机械增加的版本字段；既有事实内容无需反向迁移。

Schema 不能解决跨文件一致性、历史增长、缓存新鲜度或 slug 碰撞。这些仍由 `audit_data` 处理，稳定项目 ID 与 generation 发布属于后续独立迭代。
