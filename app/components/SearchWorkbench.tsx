"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { projects, type Project } from "../data";

type IntentRule = {
  label: string;
  words: string[];
  capabilities: string[];
  projectTerms: string[];
};

const intentRules: IntentRule[] = [
  {
    label: "视频与账号分析",
    words: ["视频", "抖音", "短视频", "账号", "脚本", "流量", "字幕"],
    capabilities: ["视频采集", "内容拆解", "账号分析", "脚本生成", "流量预测"],
    projectTerms: ["video", "视频", "transcript", "douyin", "youtube", "creator", "content", "analytics"],
  },
  {
    label: "研究与证据",
    words: ["新闻", "热点", "研究", "信息", "证据", "文献", "报告"],
    capabilities: ["多源检索", "证据汇总", "研究工作流", "报告生成"],
    projectTerms: ["research", "science", "evidence", "news", "paper", "literature", "report", "研究"],
  },
  {
    label: "Agent 工程化",
    words: ["agent", "智能体", "代理", "codex", "claude", "mcp", "技能"],
    capabilities: ["Agent 工具", "技能编排", "模型协作", "会话记忆"],
    projectTerms: ["agent", "codex", "claude", "mcp", "skill", "memory", "hook", "智能体"],
  },
  {
    label: "文档自动化",
    words: ["文档", "office", "表格", "excel", "ppt", "word", "公众号"],
    capabilities: ["文档读写", "格式转换", "内容生成", "自动排版"],
    projectTerms: ["document", "office", "excel", "spreadsheet", "ppt", "word", "markdown", "公众号"],
  },
  {
    label: "GitHub 项目情报",
    words: ["github", "开源仓库", "代码仓库", "star", "趋势排行"],
    capabilities: ["GitHub 分析", "趋势跟踪", "项目对比", "代码检查"],
    projectTerms: ["github", "repository", "developer", "code", "open source", "trend", "analysis"],
  },
  {
    label: "流程自动化",
    words: ["自动", "流程", "工作流", "批量", "任务", "定时"],
    capabilities: ["流程编排", "批量执行", "任务自动化", "运行监控"],
    projectTerms: ["automation", "workflow", "pipeline", "scheduler", "task", "自动化", "工作流"],
  },
  {
    label: "第三方服务集成",
    words: ["oauth", "登录", "gmail", "notion", "slack", "api", "第三方", "连接器"],
    capabilities: ["OAuth", "凭据管理", "API 集成", "连接器网关"],
    projectTerms: ["oauth", "connector", "api gateway", "credential", "integration", "saas", "openapi"],
  },
  {
    label: "知识与能力图谱",
    words: ["知识库", "知识图谱", "能力图谱", "学习路径", "依赖关系", "课程"],
    capabilities: ["知识图谱", "能力拆解", "依赖关系", "证据标准"],
    projectTerms: ["taxonomy", "knowledge graph", "prerequisite", "curriculum", "graph", "知识图谱"],
  },
];

function normalize(value: string) {
  return value.toLowerCase().replace(/[-_/]+/g, " ");
}

function queryTokens(query: string) {
  const latin = query.toLowerCase().match(/[a-z0-9+#.]{2,}/g) ?? [];
  const chunks = query
    .toLowerCase()
    .split(/[\s，。,.、/：:；;（）()]+/)
    .filter((token) => token.length > 1);
  return [...new Set([...latin, ...chunks])];
}

function analyzeIntent(query: string) {
  const normalized = normalize(query);
  const rules = intentRules.filter((rule) => rule.words.some((word) => normalized.includes(word)));
  const capabilities = [...new Set(rules.flatMap((rule) => rule.capabilities))];
  return {
    rules,
    capabilities: (capabilities.length ? capabilities : ["需求理解", "相似项目", "可复用模块"]).slice(0, 6),
    tokens: queryTokens(query),
  };
}

function matchProject(project: Project, query: string, rules: IntentRule[], tokens: string[]) {
  const haystack = normalize([
    project.repo,
    project.title,
    project.description,
    project.category,
    project.fit,
    project.reusePlan,
    ...project.capabilities,
    ...project.taskTerms,
  ].join(" "));

  const ruleMatches = rules
    .map((rule) => ({
      rule,
      terms: rule.projectTerms.filter((term) => haystack.includes(normalize(term))),
    }))
    .filter((match) => match.terms.length > 0);
  const matchedTokens = tokens.filter((token) => haystack.includes(normalize(token)));
  const semanticScore = Math.min(
    54,
    ruleMatches.reduce((score, match) => score + 10 + Math.min(12, (match.terms.length - 1) * 4), 0),
  );
  const tokenScore = Math.min(20, matchedTokens.length * 5);
  const evidenceScore = project.analysisState === "深度分析" ? 10 : project.analysisState === "静态分析" ? 6 : 2;
  const reuseScore = Math.round(project.reuseScore * 0.12);
  const score = Math.min(96, semanticScore + tokenScore + evidenceScore + reuseScore);
  const reasons = [
    ...ruleMatches.map((match) => `${match.rule.label}：${match.terms.slice(0, 2).join("、")}`),
    ...(matchedTokens.length ? [`直接命中：${matchedTokens.slice(0, 2).join("、")}`] : []),
    project.analysisState,
  ];

  return {
    project,
    score,
    strongMatch: ruleMatches.length > 0 || matchedTokens.length > 0,
    reasons: [...new Set(reasons)].slice(0, 3),
  };
}

export function SearchWorkbench({ compact = false }: { compact?: boolean }) {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");

  const intent = useMemo(() => analyzeIntent(submitted), [submitted]);

  const results = useMemo(() => {
    if (!submitted) return [];
    const ranked = projects
      .map((project) => matchProject(project, submitted, intent.rules, intent.tokens))
      .sort((a, b) => b.score - a.score || b.project.reuseScore - a.project.reuseScore);
    const strong = ranked.filter((result) => result.strongMatch);
    return (strong.length ? strong : ranked).slice(0, compact ? 3 : 5);
  }, [compact, intent, submitted]);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (query.trim()) setSubmitted(query.trim());
  }

  return (
    <div className={`search-workbench ${compact ? "compact-search" : ""}`}>
      <form onSubmit={submit}>
        <label htmlFor={compact ? "home-task" : "search-task"}>描述你想实现的功能</label>
        <div className="search-row">
          <input
            id={compact ? "home-task" : "search-task"}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="例如：找到能扫描账号、拆解视频并预测流量的项目"
          />
          <button type="submit">开始侦察</button>
        </div>
      </form>

      {submitted && (
        <div className="search-results" aria-live="polite">
          <div className="capability-breakdown">
            <span className="section-label">目标拆解</span>
            <div className="capability-list">
              {intent.capabilities.map((capability) => <span key={capability}>{capability}</span>)}
            </div>
          </div>
          <div className="match-list">
            {results.map(({ project, score, reasons }, index) => (
              <Link href={`/projects/${project.slug}`} key={project.slug} className="match-row">
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div>
                  <strong>{project.repo}</strong>
                  <p>{reasons.join(" · ")}</p>
                  <small>{project.reusePlan}</small>
                </div>
                <b>{score}%</b>
              </Link>
            ))}
          </div>
          <p className="model-note">匹配依据来自本地 Codex 能力画像、仓库事实与可解释任务规则；没有明确命中时不会伪造高匹配度。</p>
        </div>
      )}
    </div>
  );
}
