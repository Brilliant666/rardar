"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  associateProjectsById,
  resultForGeneration,
} from "../client-project-identity.mjs";
import type { StableProject } from "../data";
import type { PersonalizationResult } from "../personalization";
import { feedbackEventName, getDeviceId } from "./device-id";
import { ProjectCard } from "./ProjectCard";

export function PersonalizedDailyList({
  generationId,
  dailyProjects,
  projects,
}: {
  generationId: string;
  dailyProjects: StableProject[];
  projects: StableProject[];
}) {
  const [resultState, setResultState] = useState<{
    generationId: string;
    result: PersonalizationResult;
  } | null>(null);
  const [failedGeneration, setFailedGeneration] = useState<string | null>(null);
  const requestVersion = useRef(0);
  const result = resultState?.generationId === generationId ? resultState.result : null;
  const failed = failedGeneration === generationId;

  const refresh = useCallback(async () => {
    const currentRequestVersion = ++requestVersion.current;
    const deviceId = getDeviceId();
    if (!deviceId) return;

    try {
      const response = await fetch(`/api/recommendations?deviceId=${encodeURIComponent(deviceId)}`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error("recommendations unavailable");
      const responseResult = (await response.json()) as PersonalizationResult & {
        generationId?: unknown;
      };
      if (currentRequestVersion !== requestVersion.current) return;
      const nextResult = resultForGeneration(generationId, responseResult);
      if (!nextResult) {
        setFailedGeneration(generationId);
        return;
      }
      setResultState({ generationId: nextResult.generationId, result: nextResult });
      setFailedGeneration(null);
    } catch {
      if (currentRequestVersion !== requestVersion.current) return;
      setFailedGeneration(generationId);
    }
  }, [generationId]);

  useEffect(() => {
    const initialRefresh = window.setTimeout(() => void refresh(), 0);
    const handleFeedback = () => void refresh();
    window.addEventListener(feedbackEventName, handleFeedback);
    return () => {
      requestVersion.current += 1;
      window.clearTimeout(initialRefresh);
      window.removeEventListener(feedbackEventName, handleFeedback);
    };
  }, [generationId, refresh]);

  const rankedProjects = useMemo(() => {
    if (!result) return dailyProjects.map((project) => ({ project, reason: "" }));
    return associateProjectsById(projects, result.recommendations)
      .map(({ project, record: recommendation }) => ({
        project,
        reason: result.personalized ? recommendation.reasons[0] ?? "" : "",
      }))
      .slice(0, 5);
  }, [dailyProjects, projects, result]);

  return (
    <>
      <div className="personalization-status" aria-live="polite">
        <span>{result?.personalized ? "已开启偏好重排" : "当前为证据基础排序"}</span>
        <p>
          {result?.personalized
            ? `已根据 ${result.feedbackCount} 条反馈调整；关注优先级与可用工程证据仍占主干，已处理项目会减少重复曝光。`
            : "点击“有用 / 无用 / 复用 / 待确定”后，下一次推荐会学习你的目标。"}
          {failed ? " 个性化接口暂时不可用，已保留证据基础排序。" : ""}
        </p>
      </div>
      {rankedProjects.length ? <div className="daily-list">
        {rankedProjects.map(({ project, reason }, index) => (
          <ProjectCard
            key={project.projectId}
            project={project}
            index={index}
            rankingReason={reason}
          />
        ))}
      </div> : (
        <div className="empty-state compact-empty">
          <span>0</span>
          <h2>今天还没有可用推荐</h2>
          <p>当前已验证 generation 没有 Daily Five 项目。Rardar 不会用未审计候选补位。</p>
        </div>
      )}
    </>
  );
}
