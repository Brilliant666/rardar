"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Project } from "../data";
import type { PersonalizationResult } from "../personalization";
import { feedbackEventName, getDeviceId } from "./device-id";
import { ProjectCard } from "./ProjectCard";

export function PersonalizedDailyList({
  dailyProjects,
  projects,
}: {
  dailyProjects: Project[];
  projects: Project[];
}) {
  const [result, setResult] = useState<PersonalizationResult | null>(null);
  const [failed, setFailed] = useState(false);
  const requestVersion = useRef(0);

  const refresh = useCallback(async () => {
    const currentRequestVersion = ++requestVersion.current;
    const deviceId = getDeviceId();
    if (!deviceId) return;

    try {
      const response = await fetch(`/api/recommendations?deviceId=${encodeURIComponent(deviceId)}`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error("recommendations unavailable");
      const nextResult = (await response.json()) as PersonalizationResult;
      if (currentRequestVersion !== requestVersion.current) return;
      setResult(nextResult);
      setFailed(false);
    } catch {
      if (currentRequestVersion !== requestVersion.current) return;
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    const initialRefresh = window.setTimeout(() => void refresh(), 0);
    const handleFeedback = () => void refresh();
    window.addEventListener(feedbackEventName, handleFeedback);
    return () => {
      requestVersion.current += 1;
      window.clearTimeout(initialRefresh);
      window.removeEventListener(feedbackEventName, handleFeedback);
    };
  }, [refresh]);

  const rankedProjects = useMemo(() => {
    if (!result) return dailyProjects.map((project) => ({ project, reason: "" }));
    const projectBySlug = new Map(projects.map((project) => [project.slug, project]));
    return result.recommendations
      .map((recommendation) => ({
        project: projectBySlug.get(recommendation.slug),
        reason: result.personalized ? recommendation.reasons[0] ?? "" : "",
      }))
      .filter((item): item is { project: Project; reason: string } => Boolean(item.project))
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
      <div className="daily-list">
        {rankedProjects.map(({ project, reason }, index) => (
          <ProjectCard
            key={project.slug}
            project={project}
            index={index}
            rankingReason={reason}
          />
        ))}
      </div>
    </>
  );
}
