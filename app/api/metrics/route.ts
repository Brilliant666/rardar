import { env } from "cloudflare:workers";
import { ensureDecisionSchema } from "../../../db/ensure";
import {
  readStableFeedback,
  readStableWeeklyActionMetrics,
  readStableWeeklyFeedbackMetrics,
} from "../../../db/stable-project-decisions.mjs";
import {
  createProjectIdentityContext,
  projectIdentityErrorResponse,
  validateStoredProjectIdentity,
} from "../../project-identity.mjs";
import { loadPublishedData } from "../../server-data";

const values = ["有用", "无用", "复用", "待确定"] as const;

export async function GET(request: Request) {
  try {
    const published = await loadPublishedData();
    const identityContext = await createProjectIdentityContext(
      published.generationId,
      published.catalog,
      published.publishedAt,
    );
    const url = new URL(request.url);
    const deviceId = url.searchParams.get("deviceId")?.trim();
    if (!deviceId || deviceId.length > 200) {
      return Response.json({ error: "deviceId is required" }, { status: 400 });
    }
    if (
      url.searchParams.has("repository")
      || url.searchParams.has("repo")
      || url.searchParams.has("occurredAt")
    ) {
      return Response.json(
        { error: "client_project_evidence_not_allowed" },
        { status: 400, headers: { "cache-control": "no-store" } },
      );
    }

    await ensureDecisionSchema(identityContext.identityCatalog);
    const feedbackRows = (await readStableFeedback(env.DB, deviceId))
      .map((row) => validateStoredProjectIdentity(row));
    const current = Object.fromEntries(values.map((value) => [value, 0])) as Record<(typeof values)[number], number>;
    for (const row of feedbackRows) {
      if (values.includes(row.value as (typeof values)[number])) {
        current[row.value as (typeof values)[number]] += 1;
      }
    }

    const [feedbackWeek, actionWeek] = await Promise.all([
      readStableWeeklyFeedbackMetrics(env.DB, deviceId),
      readStableWeeklyActionMetrics(env.DB, deviceId),
    ]);

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
        feedbackDecisions: feedbackWeek.effectiveDecisions,
        feedbackReuseDecisions: feedbackWeek.reuseDecisions,
        feedbackChanges: feedbackWeek.feedbackChanges,
      },
      current: {
        useful: current["有用"],
        useless: current["无用"],
        reused: current["复用"],
        uncertain: current["待确定"],
        total: Object.values(current).reduce((sum, value) => sum + value, 0),
      },
    }, { headers: { "cache-control": "no-store" } });
  } catch (error) {
    const response = projectIdentityErrorResponse(error);
    if (response) return response;
    throw error;
  }
}
