import { count, eq } from "drizzle-orm";
import { env } from "cloudflare:workers";
import { getDb } from "../../../db";
import { ensureDecisionSchema } from "../../../db/ensure";
import { feedback } from "../../../db/schema";

type WeeklyMetricRow = {
  effective_decisions: number | string | null;
  reuse_decisions: number | string | null;
  feedback_changes: number | string | null;
};

type WeeklyActionRow = {
  acted_projects: number | string | null;
  opened_projects: number | string | null;
  saved_projects: number | string | null;
  tried_projects: number | string | null;
  cloned_projects: number | string | null;
  reused_projects: number | string | null;
};

const values = ["有用", "无用", "复用", "待确定"] as const;

export async function GET(request: Request) {
  await ensureDecisionSchema();
  const deviceId = new URL(request.url).searchParams.get("deviceId")?.trim();
  if (!deviceId) return Response.json({ error: "deviceId is required" }, { status: 400 });

  const currentRows = await getDb()
    .select({ value: feedback.value, count: count() })
    .from(feedback)
    .where(eq(feedback.deviceId, deviceId))
    .groupBy(feedback.value);
  const current = Object.fromEntries(values.map((value) => [value, 0])) as Record<(typeof values)[number], number>;
  for (const row of currentRows) {
    if (values.includes(row.value as (typeof values)[number])) {
      current[row.value as (typeof values)[number]] = Number(row.count);
    }
  }

  const weekly = await env.DB.prepare(`
    SELECT
      COUNT(DISTINCT CASE WHEN value IN ('有用', '复用') THEN project_slug END) AS effective_decisions,
      COUNT(DISTINCT CASE WHEN value = '复用' THEN project_slug END) AS reuse_decisions,
      COUNT(*) AS feedback_changes
    FROM decision_events
    WHERE device_id = ? AND created_at >= datetime('now', '-7 days')
  `).bind(deviceId).first<WeeklyMetricRow>();

  const actionWeek = await env.DB.prepare(`
    SELECT
      COUNT(DISTINCT CASE WHEN action IN ('tried', 'cloned', 'reused') THEN project_slug END) AS acted_projects,
      COUNT(DISTINCT CASE WHEN action = 'opened' THEN project_slug END) AS opened_projects,
      COUNT(DISTINCT CASE WHEN action = 'saved' THEN project_slug END) AS saved_projects,
      COUNT(DISTINCT CASE WHEN action = 'tried' THEN project_slug END) AS tried_projects,
      COUNT(DISTINCT CASE WHEN action = 'cloned' THEN project_slug END) AS cloned_projects,
      COUNT(DISTINCT CASE WHEN action = 'reused' THEN project_slug END) AS reused_projects
    FROM project_actions
    WHERE device_id = ? AND created_at >= datetime('now', '-7 days')
  `).bind(deviceId).first<WeeklyActionRow>();

  return Response.json({
    northStar: {
      label: "近 7 天已行动项目",
      value: Number(actionWeek?.acted_projects ?? 0),
    },
    week: {
      openedProjects: Number(actionWeek?.opened_projects ?? 0),
      savedProjects: Number(actionWeek?.saved_projects ?? 0),
      triedProjects: Number(actionWeek?.tried_projects ?? 0),
      clonedProjects: Number(actionWeek?.cloned_projects ?? 0),
      reusedProjects: Number(actionWeek?.reused_projects ?? 0),
      feedbackDecisions: Number(weekly?.effective_decisions ?? 0),
      feedbackReuseDecisions: Number(weekly?.reuse_decisions ?? 0),
      feedbackChanges: Number(weekly?.feedback_changes ?? 0),
    },
    current: {
      useful: current["有用"],
      useless: current["无用"],
      reused: current["复用"],
      uncertain: current["待确定"],
      total: Object.values(current).reduce((sum, value) => sum + value, 0),
    },
  });
}
