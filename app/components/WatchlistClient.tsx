"use client";

import Link from "next/link";
import { canonicalProjectPath } from "../client-project-identity.mjs";
import { decisionStatusForProject } from "../decision-flow.mjs";
import type { StableProject } from "../data";
import { useDecisionState, useDecisionStateCollection } from "./DecisionStateProvider";

function WatchedProjectCard({ project }: { project: StableProject }) {
  const { state } = useDecisionState(project.projectId);
  const status = decisionStatusForProject(state);
  if (!status.watched) return null;
  return (
    <Link href={canonicalProjectPath(project)} className="watch-card">
      <span>已关注 · {status.stageLabel} · {project.category}</span>
      <strong>{project.repo}</strong>
      <p>{project.whyNow}</p>
    </Link>
  );
}

export function WatchlistClient({ projects }: { projects: StableProject[] }) {
  const {
    stateByProjectId,
    loading,
    error,
    staleGeneration,
    retry,
    reload,
  } = useDecisionStateCollection();
  const watched = projects.filter((project) => (
    decisionStatusForProject(stateByProjectId.get(project.projectId)).watched
  ));

  if (loading) return <div className="empty-state" role="status">正在读取观察列表…</div>;
  if (error) {
    return (
      <div className="empty-state" role="alert">
        <span>!</span>
        <h2>观察列表暂时无法读取</h2>
        <p>{error}</p>
        <button className="primary-link" type="button" onClick={staleGeneration ? reload : retry}>
          {staleGeneration ? "刷新页面" : "重新读取"}
        </button>
      </div>
    );
  }
  if (!watched.length) {
    return (
      <div className="empty-state">
        <span>0</span>
        <h2>还没有关注的项目</h2>
        <p>在项目卡片或详情页选择“关注”，它就会出现在这里。“待确定”只代表推荐质量反馈，不会自动加入观察列表。</p>
        <Link className="primary-link" href="/discover">去发现项目</Link>
      </div>
    );
  }

  return <div className="watch-grid">{watched.map((project) => (
    <WatchedProjectCard key={project.projectId} project={project} />
  ))}</div>;
}
