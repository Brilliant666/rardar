# Rardar

Rardar 是一个证据优先的开源情报与项目复用雷达。它将技术事件、GitHub 仓库、能力标签、静态代码证据和用户反馈组织在一起，帮助开发者回答两个问题：

1. 最近真正发生了什么？
2. 我想实现的功能是否已经有项目做过？

## 当前版本

- 今日重点与候选池
- 过去 48 小时 AI/技术动态中文简报与信源健康状态
- 全球影响力、复用价值双评分
- 自然语言任务拆解、中文能力画像和可解释匹配
- 项目证据页与风险提示
- `有用 / 无用 / 复用 / 待确定` 持久化反馈与偏好重排
- 打开、收藏、试用、浅克隆和确认复用的真实行动记录
- 公共项目只读浅克隆与静态分析工具
- 每日生成的本地 Codex 中文深读队列

当前网页读取 `data/catalog/latest.json` 中的真实 GitHub API 快照。首次采集只展示明确标注的“创建以来速度代理”；第二次刷新起会自动归档旧快照并计算真实观测区间增长。刷新流程还会对前五名执行隔离用户 Git 配置的只读浅克隆，静态检查代码、测试、文档与许可证，不执行仓库代码。技术动态来自官方 RSS 与可归因的社区补充源，先去重、标注信源健康，再由本地 Codex 为前五条生成中文要点。

本地 Codex 生成的中文能力画像保存在 `data/enrichment/`。画像只覆盖已核对 README 和静态证据的项目，并与 GitHub 事实分层保存；刷新目录时不会把没有深度画像的新项目伪装成已分析项目。

每日刷新还会生成 `data/queues/codex.json`：它只收录重点范围内尚未完成中文画像的项目与动态，并为每项列出输入证据、目标输出、必填字段和安全边界，便于本地 Codex 按优先级继续阅读。

首页推荐默认沿用事实热度与复用价值排序。浏览器产生反馈后，`/api/recommendations` 会生成匿名设备偏好画像：标记“无用”的项目和相近特征会降低曝光，“有用 / 复用”会提高相似未处理项目的机会，已处理项目本身会减少重复推荐；个性化只做有限调整，不覆盖证据评分主干。

## 开发

需要 Node.js 22.13 或更高版本。

```bash
npm install
npm run data:refresh
npm run data:audit
npm run dev
npm run build
npm run security:audit:prod
```

`data:audit` 只读核对快照、目录、动态、历史和 Codex 队列的时间、数量、唯一性与 URL 安全边界；`security:audit:prod` 使用 npm 官方漏洞库检查会进入运行环境的依赖。本地构建工具仍应结合完整 `npm audit` 与实际暴露面单独复核。

Windows 上可以直接双击项目根目录的 `打开 Rardar.cmd`。它会启动一个隐藏的本地管理器，同时看护网站和每日刷新任务，并打开本地首页。管理器会在任一子服务异常退出后自动重启它。运行心跳、PID 和日志保存在 Windows 本地应用数据目录，不会因频繁写入而触发网站热更新；每份日志超过 5 MB 后滚动，并保留最近两份历史。

也可以使用命令管理：

```bash
npm run local:start
npm run local:status
npm run local:stop
```

如需单独调试每日刷新守护进程：

```bash
npm run data:schedule
```

默认在 `Asia/Shanghai` 每天 08:00 刷新 GitHub 快照、前五静态检查和技术动态。刷新期间调度器会持续写入运行心跳；若进程中途退出，管理器重启后会在 12 小时窗口内补跑，网络等临时故障则每 5 分钟重试，单轮最多 3 次。守护进程不会部署网站，也不会执行候选仓库代码。

“动态”页面从本地管理器的实时心跳读取运行状态；旧状态超过 35 秒就会显示需要重新启动，不再把过期的 `scheduled` 文件误报为正在运行。

默认本地预览地址：<http://127.0.0.1:3000/>。项目默认不发布线上版本，除非用户明确提出部署要求。

静态分析工具只读取文件，不执行仓库代码或安装陌生依赖：

```bash
python -m pipeline.analyze_repository --path .
python -m pipeline.analyze_repository --repo owner/name
python -m pipeline.collect_github --out data/snapshots/latest.json
python -m pipeline.collect_signals --out data/signals/latest.json
python -m pipeline.build_catalog --snapshot data/snapshots/latest.json --analysis-dir data/analysis --enrichment-dir data/enrichment --out data/catalog/latest.json
```

## 数据原则

- 事实与 AI 判断分开保存。
- 每条结论尽量附带来源、采集时间和置信度。
- 功能目标优先于编程语言。
- 全球影响力与个人复用价值独立评分。
- 陌生仓库默认只读分析，禁止自动执行代码。
- 北极星指标按近 7 天发生“试用 / 浅克隆 / 确认复用”的不同项目数计算；反馈只用于学习排序，不再冒充实际结果。
- 官方 RSS 优先；AI News Radar、OpenGithubs 和 HelloGitHub 只作为可归因的补充信号，第三方榜单增长必须由 Rardar 自有快照验证。
