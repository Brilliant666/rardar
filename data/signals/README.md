# 技术动态中文简报

`latest.json` 由公开信源采集器生成，保存原始标题、来源、时间、评分和信源健康状态。`enrichment.json` 由本地 Codex 阅读原文后补充中文标题、要点和影响判断。

每条中文深读还应保存 `analyzedAt` 与 `sourcePublishedAt`：前者记录 Codex 实际分析时间，后者绑定当时读取的原始事件版本。相同 URL 若出现更新的发布时间，会重新进入队列，旧结论不会直接覆盖新事件。

`enrichment.json` 使用 `schemaVersion: 1`，顶层必须包含 `generatedAt`、`model` 和以 HTTP(S) 原始链接为键的 `items`。早期条目可能没有逐条时间字段，此时只允许使用顶层 `generatedAt` 作为兼容回退；所有新条目仍应同时写入 `analyzedAt` 与 `sourcePublishedAt`。

不要直接覆盖正式 `enrichment.json`。先写 `data/` 之外的草稿，再执行：

```bash
python -m pipeline.ingest_enrichment --kind signal --input tmp/signal-draft.json
npm run data:derive
```

验证或原子写入失败时，已有正式简报保持不变。

两层数据必须分开：定时采集可以自动运行，但没有被 Codex 阅读过的动态只能显示原始标题与来源摘要，不能伪装成深度中文分析。

当前默认来源：OpenAI News、GitHub Changelog、Hugging Face Blog、AI News Radar、OpenGithubs Daily Rank 和 HelloGitHub Releases。
