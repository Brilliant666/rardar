import { and, desc, eq } from "drizzle-orm";
import { getDb } from "../../../db";
import { ensureDecisionSchema } from "../../../db/ensure";
import { projectActions } from "../../../db/schema";
import { projects } from "../../data";
import { readJsonObject, trimmedString } from "../validation";

const allowedActions = ["opened", "saved", "tried", "cloned", "reused"] as const;
const projectSlugs = new Set(projects.map((project) => project.slug));

export async function GET(request: Request) {
  const url = new URL(request.url);
  const deviceId = url.searchParams.get("deviceId")?.trim();
  const projectSlug = url.searchParams.get("projectSlug")?.trim();
  if (!deviceId || deviceId.length > 200) {
    return Response.json({ error: "deviceId is required" }, { status: 400 });
  }
  if (projectSlug && !projectSlugs.has(projectSlug)) {
    return Response.json({ error: "unknown project" }, { status: 404 });
  }

  await ensureDecisionSchema();
  const db = getDb();
  const rows = projectSlug
    ? await db
        .select()
        .from(projectActions)
        .where(and(eq(projectActions.deviceId, deviceId), eq(projectActions.projectSlug, projectSlug)))
        .orderBy(desc(projectActions.createdAt))
    : await db
        .select()
        .from(projectActions)
        .where(eq(projectActions.deviceId, deviceId))
        .orderBy(desc(projectActions.createdAt));
  return Response.json({ actions: rows }, { headers: { "cache-control": "no-store" } });
}

export async function POST(request: Request) {
  const payload = await readJsonObject(request);
  if (!payload) {
    return Response.json({ error: "invalid project action" }, { status: 400 });
  }
  const deviceId = trimmedString(payload, "deviceId");
  const projectSlug = trimmedString(payload, "projectSlug");
  const action = trimmedString(payload, "action");
  if (
    !deviceId ||
    deviceId.length > 200 ||
    !projectSlug ||
    !projectSlugs.has(projectSlug) ||
    !action ||
    !allowedActions.includes(action as (typeof allowedActions)[number])
  ) {
    return Response.json({ error: "invalid project action" }, { status: 400 });
  }

  await ensureDecisionSchema();
  const db = getDb();
  const [existing] = await db
    .select({ id: projectActions.id })
    .from(projectActions)
    .where(
      and(
        eq(projectActions.deviceId, deviceId),
        eq(projectActions.projectSlug, projectSlug),
        eq(projectActions.action, action),
      ),
    )
    .limit(1);
  if (!existing) {
    await db.insert(projectActions).values({ deviceId, projectSlug, action }).onConflictDoNothing();
  }
  return Response.json({ ok: true, action, recorded: !existing });
}
