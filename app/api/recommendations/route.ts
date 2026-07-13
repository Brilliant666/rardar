import { eq } from "drizzle-orm";
import { getDb } from "../../../db";
import { ensureDecisionSchema } from "../../../db/ensure";
import { feedback } from "../../../db/schema";
import { rankProjects } from "../../personalization";
import { loadPublishedData } from "../../server-data";

export async function GET(request: Request) {
  const { projects } = await loadPublishedData();
  const url = new URL(request.url);
  const deviceId = url.searchParams.get("deviceId")?.trim();
  if (!deviceId || deviceId.length > 200) {
    return Response.json({ error: "deviceId is required" }, { status: 400 });
  }

  await ensureDecisionSchema();
  const db = getDb();
  const rows = await db
    .select({ projectSlug: feedback.projectSlug, value: feedback.value })
    .from(feedback)
    .where(eq(feedback.deviceId, deviceId));

  return Response.json(rankProjects(projects, rows), {
    headers: { "cache-control": "no-store" },
  });
}
