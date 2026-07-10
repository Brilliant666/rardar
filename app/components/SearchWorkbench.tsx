"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { projects, type Project } from "../data";

const capabilityRules = [
  { words: ["视频", "抖音", "短视频"], capabilities: ["视频采集", "内容拆解", "账号分析"] },
  { words: ["新闻", "热点", "研究", "信息"], capabilities: ["多源检索", "热点研究", "证据汇总"] },
  { words: ["agent", "智能体", "代理"], capabilities: ["Agent 工具", "编程 Agent", "Agent Memory"] },
  { words: ["文档", "office", "表格", "ppt"], capabilities: ["Office 自动化", "文档读写", "CLI"] },
  { words: ["github", "开源", "仓库", "项目"], capabilities: ["GitHub 分析", "Trending 抓取", "项目对比"] },
  { words: ["自动", "流程", "工作流"], capabilities: ["流程自动化", "工程工作流", "自动化"] },
];

function scoreProject(project: Project, query: string, capabilities: string[]) {
  const haystack = [
    project.repo,
    project.title,
    project.description,
    ...project.capabilities,
  ].join(" ").toLowerCase();
  const tokens = query.toLowerCase().split(/[\s，。,.、/]+/).filter((token) => token.length > 1);
  const tokenScore = tokens.reduce((score, token) => score + (haystack.includes(token) ? 4 : 0), 0);
  const capabilityScore = capabilities.reduce(
    (score, capability) => score + (project.capabilities.some((item) => item.includes(capability) || capability.includes(item)) ? 6 : 0),
    0,
  );
  return tokenScore + capabilityScore + project.reuseScore / 20;
}

export function SearchWorkbench({ compact = false }: { compact?: boolean }) {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");

  const capabilities = useMemo(() => {
    const matched = capabilityRules
      .filter((rule) => rule.words.some((word) => submitted.toLowerCase().includes(word)))
      .flatMap((rule) => rule.capabilities);
    return [...new Set(matched.length ? matched : ["需求理解", "相似项目", "可复用模块"])]
      .slice(0, compact ? 3 : 6);
  }, [compact, submitted]);

  const results = useMemo(() => {
    if (!submitted) return [];
    return projects
      .map((project) => ({ project, score: scoreProject(project, submitted, capabilities) }))
      .sort((a, b) => b.score - a.score)
      .slice(0, compact ? 3 : 5);
  }, [capabilities, compact, submitted]);

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
              {capabilities.map((capability) => <span key={capability}>{capability}</span>)}
            </div>
          </div>
          <div className="match-list">
            {results.map(({ project, score }, index) => (
              <Link href={`/projects/${project.slug}`} key={project.slug} className="match-row">
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div>
                  <strong>{project.repo}</strong>
                  <p>{project.fit}</p>
                </div>
                <b>{Math.min(96, Math.round(score * 4.2))}%</b>
              </Link>
            ))}
          </div>
          <p className="model-note">当前结果为规则匹配演示；接入 Codex 后会继续读取代码并生成组合复用方案。</p>
        </div>
      )}
    </div>
  );
}
