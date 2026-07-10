export type Evidence = {
  label: string;
  detail: string;
  href: string;
};

export type Project = {
  slug: string;
  repo: string;
  title: string;
  description: string;
  category: string;
  language: string;
  license: string;
  stars: number;
  starsToday: number;
  globalScore: number;
  reuseScore: number;
  trend: string;
  analysisState: "基础分析" | "深度分析";
  whyNow: string;
  recommendation: "了解" | "收藏" | "试用" | "复用" | "观望";
  fit: string;
  risk: string;
  capabilities: string[];
  evidence: Evidence[];
  capturedAt: string;
};

export const projects: Project[] = [
  {
    slug: "officecli",
    repo: "iOfficeAI/OfficeCLI",
    title: "让 AI Agent 直接操作 Office 文件",
    description:
      "面向 Agent 的 Office 命令行工具，可读取、编辑并自动化 Word、Excel 与 PowerPoint。",
    category: "生产力",
    language: "C#",
    license: "待核验",
    stars: 12582,
    starsToday: 1717,
    globalScore: 94,
    reuseScore: 88,
    trend: "+15.8% / 24h",
    analysisState: "基础分析",
    whyNow: "今日进入 GitHub Trending 前列，单日新增关注显著高于同类 Office 自动化项目。",
    recommendation: "试用",
    fit: "适合需要让 Agent 处理报告、表格和演示文稿的自动化工作流。",
    risk: "当前只完成仓库级基础核验，兼容范围与复杂文档保真度仍需实际测试。",
    capabilities: ["Office 自动化", "Agent 工具", "CLI", "文档读写"],
    evidence: [
      {
        label: "GitHub Trending",
        detail: "快照显示 1,717 stars today。",
        href: "https://github.com/trending",
      },
      {
        label: "项目仓库",
        detail: "仓库说明覆盖 Word、Excel、PowerPoint 的读取、编辑与自动化。",
        href: "https://github.com/iOfficeAI/OfficeCLI",
      },
    ],
    capturedAt: "2026-07-10 18:00 CST",
  },
  {
    slug: "last30days-skill",
    repo: "mvanhorn/last30days-skill",
    title: "把最近 30 天的多平台信息变成可追溯研究",
    description:
      "面向 AI Agent 的研究技能，聚合 Reddit、X、YouTube、Hacker News、Polymarket 与网页信息。",
    category: "信息研究",
    language: "Python",
    license: "待核验",
    stars: 51009,
    starsToday: 352,
    globalScore: 86,
    reuseScore: 95,
    trend: "+352 / 24h",
    analysisState: "基础分析",
    whyNow: "持续获得高增长，且其多源检索与证据汇总方式直接对应开源情报产品。",
    recommendation: "复用",
    fit: "可重点参考数据源编排、时间范围过滤和有依据的综合报告结构。",
    risk: "外部平台访问能力、费用和数据授权边界需要逐个确认。",
    capabilities: ["多源检索", "热点研究", "证据汇总", "Agent Skill"],
    evidence: [
      {
        label: "GitHub Trending",
        detail: "快照显示 352 stars today。",
        href: "https://github.com/trending",
      },
      {
        label: "项目仓库",
        detail: "README 将目标定义为最近 30 天的跨平台研究与有依据总结。",
        href: "https://github.com/mvanhorn/last30days-skill",
      },
    ],
    capturedAt: "2026-07-10 18:00 CST",
  },
  {
    slug: "agent-skills",
    repo: "addyosmani/agent-skills",
    title: "生产级工程技能正在成为 Agent 的新复用单元",
    description:
      "为 AI 编程 Agent 整理可复用、可组合的工程技能，强调真实生产工作流。",
    category: "开发工具",
    language: "JavaScript",
    license: "待核验",
    stars: 75027,
    starsToday: 1297,
    globalScore: 96,
    reuseScore: 83,
    trend: "+1,297 / 24h",
    analysisState: "基础分析",
    whyNow: "高基数仓库仍保持四位数单日增长，反映 Agent 工作流从提示词转向可维护技能。",
    recommendation: "收藏",
    fit: "适合参考技能的目录规范、任务边界、验证方式和可组合设计。",
    risk: "技能质量可能不均，需要逐项检查适用环境和验证证据。",
    capabilities: ["编程 Agent", "工程工作流", "技能库", "自动化"],
    evidence: [
      {
        label: "GitHub Trending",
        detail: "快照显示 1,297 stars today。",
        href: "https://github.com/trending",
      },
      {
        label: "项目仓库",
        detail: "仓库定位为面向 AI coding agents 的生产级工程技能。",
        href: "https://github.com/addyosmani/agent-skills",
      },
    ],
    capturedAt: "2026-07-10 18:00 CST",
  },
  {
    slug: "ai-job-search",
    repo: "MadsLorentzen/ai-job-search",
    title: "把复杂个人流程封装成可 Fork 的 AI 工作台",
    description:
      "基于 Claude Code 的求职自动化框架，覆盖岗位评估、简历调整、求职信和面试准备。",
    category: "垂直应用",
    language: "TypeScript",
    license: "待核验",
    stars: 16322,
    starsToday: 5079,
    globalScore: 99,
    reuseScore: 79,
    trend: "+45.2% / 24h",
    analysisState: "基础分析",
    whyNow: "单日新增 5,079 Star，是当前快照中最明显的爆发项目之一。",
    recommendation: "了解",
    fit: "虽然场景是求职，但其“用户资料 + Agent 流程 + 可定制模板”结构值得产品型项目参考。",
    risk: "爆发速度远高于项目年龄所能证明的稳定性，建议观察实际使用反馈。",
    capabilities: ["垂直 Agent", "流程自动化", "模板生成", "个人知识"],
    evidence: [
      {
        label: "GitHub Trending",
        detail: "快照显示 5,079 stars today。",
        href: "https://github.com/trending",
      },
      {
        label: "项目仓库",
        detail: "仓库说明覆盖岗位分析、材料生成和面试准备完整流程。",
        href: "https://github.com/MadsLorentzen/ai-job-search",
      },
    ],
    capturedAt: "2026-07-10 18:00 CST",
  },
  {
    slug: "tencentdb-agent-memory",
    repo: "TencentCloud/TencentDB-Agent-Memory",
    title: "Agent 长期记忆开始强调本地运行和分层存储",
    description:
      "面向 AI Agent 的本地长期记忆方案，以四层渐进式管线组织不同类型的记忆。",
    category: "AI 基础设施",
    language: "TypeScript",
    license: "待核验",
    stars: 7865,
    starsToday: 318,
    globalScore: 84,
    reuseScore: 86,
    trend: "+318 / 24h",
    analysisState: "基础分析",
    whyNow: "本地优先的 Agent 记忆方案持续升温，适合知识库和个性化推荐系统参考。",
    recommendation: "试用",
    fit: "可研究公共知识、用户上下文与长期记忆如何分层，避免把个性化信息混入事实层。",
    risk: "存储规模、召回准确率和迁移成本尚未完成独立验证。",
    capabilities: ["Agent Memory", "本地优先", "知识库", "分层存储"],
    evidence: [
      {
        label: "GitHub Trending",
        detail: "快照显示 318 stars today。",
        href: "https://github.com/trending",
      },
      {
        label: "项目仓库",
        detail: "仓库描述强调本地运行、长期记忆与四层渐进管线。",
        href: "https://github.com/TencentCloud/TencentDB-Agent-Memory",
      },
    ],
    capturedAt: "2026-07-10 18:00 CST",
  },
  {
    slug: "ossinsight",
    repo: "pingcap/ossinsight",
    title: "用事件数据理解开源生态，而不只看 Star 总数",
    description:
      "围绕 GitHub 事件数据提供趋势、排行、仓库分析、开发者分析与项目对比。",
    category: "数据平台",
    language: "TypeScript",
    license: "Apache-2.0",
    stars: 2500,
    starsToday: 18,
    globalScore: 72,
    reuseScore: 93,
    trend: "稳定",
    analysisState: "深度分析",
    whyNow: "不是今日爆发项目，但其数据模型和趋势维度对 Rardar 的实现具有直接参考价值。",
    recommendation: "复用",
    fit: "适合参考 GitHub 事件采集、项目比较和主题集合设计，不建议直接复制其重型架构。",
    risk: "数据规模和基础设施复杂度明显高于首版需求。",
    capabilities: ["GitHub 分析", "趋势排行", "数据探索", "项目对比"],
    evidence: [
      {
        label: "项目 README",
        detail: "公开说明包含趋势、仓库分析、开发者分析、比较和数据探索能力。",
        href: "https://github.com/pingcap/ossinsight",
      },
      {
        label: "许可证",
        detail: "仓库公开标注 Apache-2.0。",
        href: "https://github.com/pingcap/ossinsight/blob/main/LICENSE",
      },
    ],
    capturedAt: "2026-07-10 18:00 CST",
  },
  {
    slug: "github-trending-archive",
    repo: "antonkomarev/github-trending-archive",
    title: "轻量保存 GitHub Trending 历史快照",
    description:
      "使用 GitHub Actions 定时抓取多语言 Trending，并以紧凑 JSON 结构保存历史记录。",
    category: "数据采集",
    language: "TypeScript",
    license: "MIT",
    stars: 44,
    starsToday: 1,
    globalScore: 41,
    reuseScore: 91,
    trend: "稳定",
    analysisState: "深度分析",
    whyNow: "热度不高，但直接解决 Trending 无官方历史 API 的问题，是典型的低热度高复用价值项目。",
    recommendation: "复用",
    fit: "适合作为首版趋势历史数据源或采集器兜底。",
    risk: "只保存趋势排名，仓库元数据和 Star 历史仍需通过其他来源补充。",
    capabilities: ["Trending 抓取", "历史归档", "GitHub Actions", "JSON 数据"],
    evidence: [
      {
        label: "项目 README",
        detail: "项目说明记录每日多语言 Trending，并以 JSON 保存。",
        href: "https://github.com/antonkomarev/github-trending-archive",
      },
      {
        label: "许可证",
        detail: "仓库公开标注 MIT。",
        href: "https://github.com/antonkomarev/github-trending-archive/blob/master/LICENSE",
      },
    ],
    capturedAt: "2026-07-10 18:00 CST",
  },
];

export const dailyProjects = projects.slice(0, 5);
export const candidateProjects = projects.slice(5);

export const categories = [
  "全部",
  "生产力",
  "信息研究",
  "开发工具",
  "垂直应用",
  "AI 基础设施",
  "数据平台",
  "数据采集",
];

export const snapshotNotice =
  "当前为首版演示快照。页面中的趋势数据记录于 2026-07-10，实时采集与自动静态检查将在下一阶段接入。";

export function formatNumber(value: number) {
  return new Intl.NumberFormat("zh-CN", { notation: "compact" }).format(value);
}

export function getProject(slug: string) {
  return projects.find((project) => project.slug === slug);
}
