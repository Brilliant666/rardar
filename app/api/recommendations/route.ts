import { eq } from "drizzle-orm";
import { getDb } from "../../../db";
import { ensureDecisionSchema } from "../../../db/ensure";
import { feedback } from "../../../db/schema";
import { projects } from "../../data";
import { rankProjects } from "../../personalization";

export async function GET(request: Request) {
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
