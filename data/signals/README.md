# 技术动态中文简报

`latest.json` 由公开信源采集器生成，保存原始标题、来源、时间、评分和信源健康状态。`enrichment.json` 由本地 Codex 阅读原文后补充中文标题、要点和影响判断。

两层数据必须分开：定时采集可以自动运行，但没有被 Codex 阅读过的动态只能显示原始标题与来源摘要，不能伪装成深度中文分析。

当前默认来源：OpenAI News、GitHub Changelog、Hugging Face Blog、AI News Radar、OpenGithubs Daily Rank 和 HelloGitHub Releases。
