# Rardar

Rardar 是一个证据优先的开源情报与项目复用雷达。它将技术事件、GitHub 仓库、能力标签、静态代码证据和用户反馈组织在一起，帮助开发者回答两个问题：

1. 最近真正发生了什么？
2. 我想实现的功能是否已经有项目做过？

## 当前版本

- 今日重点与候选池
- 全球影响力、复用价值双评分
- 自然语言任务拆解和能力匹配演示
- 项目证据页与风险提示
- `有用 / 无用 / 复用 / 待确定` 持久化反馈
- 公共项目只读浅克隆与静态分析工具

当前网页使用一份带采集时间的演示快照。实时 GitHub 采集、新闻关联和 Codex 深度分析将在后续阶段接入。

## 开发

需要 Node.js 22.13 或更高版本。

```bash
npm install
npm run dev
npm run build
```

默认本地预览地址：<http://127.0.0.1:3000/>。项目默认不发布线上版本，除非用户明确提出部署要求。

静态分析工具只读取文件，不执行仓库代码或安装陌生依赖：

```bash
python -m pipeline.analyze_repository --path .
python -m pipeline.analyze_repository --repo owner/name
python -m pipeline.collect_github --out data/snapshots/latest.json
```

## 数据原则

- 事实与 AI 判断分开保存。
- 每条结论尽量附带来源、采集时间和置信度。
- 功能目标优先于编程语言。
- 全球影响力与个人复用价值独立评分。
- 陌生仓库默认只读分析，禁止自动执行代码。
