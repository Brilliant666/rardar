import { count, eq } from "drizzle-orm";
import { env } from "cloudflare:workers";
import { getDb } from "../../../db";
import { ensureDecisionSchema } from "../../../db/ensure";
import { feedback } from "../../../db/schema";
import { readWeeklyActionMetrics } from "../../../db/project-actions.mjs";

type WeeklyMetricRow = {
  effective_decisions: number | string | null;
  reuse_decisions: number | string | null;
  feedback_changes: number | string | null;
};

const values = ["有用", "无用", "复用", "待确定"] as const;

export async function GET(request: Request) {
  await ensureDecisionSchema();
  const deviceId = new URL(request.url).searchParams.get("deviceId")?.trim();
  if (!deviceId || deviceId.length > 200) {
    return Response.json({ error: "deviceId is required" }, { status: 400 });
  }

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

  const actionWeek = await readWeeklyActionMetrics(env.DB, deviceId);

  return Response.json({
    northStar: {
      label: "近 7 天已行动项目",
      value: actionWeek.actedProjects,
    },
    week: {
      openedProjects: actionWeek.openedProjects,
      savedProjects: actionWeek.savedProjects,
      triedProjects: actionWeek.triedProjects,
      clonedProjects: actionWeek.clonedProjects,
      reusedProjects: actionWeek.reusedProjects,
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
  }, { headers: { "cache-control": "no-store" } });
}
