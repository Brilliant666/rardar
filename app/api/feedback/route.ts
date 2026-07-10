import { and, eq, sql } from "drizzle-orm";
import { getDb } from "../../../db";
import { ensureDecisionSchema } from "../../../db/ensure";
import { decisionEvents, feedback } from "../../../db/schema";
import { projects } from "../../data";

const allowedValues = new Set(["有用", "无用", "复用", "待确定"]);
const projectSlugs = new Set(projects.map((project) => project.slug));
const noStoreHeaders = { "cache-control": "no-store" };

export async function GET(request: Request) {
  await ensureDecisionSchema();
  const url = new URL(request.url);
  const deviceId = url.searchParams.get("deviceId")?.trim();
  const projectSlug = url.searchParams.get("projectSlug")?.trim();
  if (!deviceId || deviceId.length > 200) {
    return Response.json({ error: "deviceId is required" }, { status: 400 });
  }
  if (projectSlug && !projectSlugs.has(projectSlug)) {
    return Response.json({ error: "unknown project" }, { status: 404 });
  }

  const db = getDb();
  if (projectSlug) {
    const [row] = await db.select().from(feedback).where(and(eq(feedback.deviceId, deviceId), eq(feedback.projectSlug, projectSlug))).limit(1);
    return Response.json({ feedback: row ?? null }, { headers: noStoreHeaders });
  }

  const rows = await db.select().from(feedback).where(eq(feedback.deviceId, deviceId));
  return Response.json({ feedback: rows }, { headers: noStoreHeaders });
}

export async function POST(request: Request) {
  await ensureDecisionSchema();
  const payload = (await request.json()) as { deviceId?: string; projectSlug?: string; value?: string };
  const deviceId = payload.deviceId?.trim();
  const projectSlug = payload.projectSlug?.trim();
  const value = payload.value?.trim();

  if (
    !deviceId ||
    deviceId.length > 200 ||
    !projectSlug ||
    !projectSlugs.has(projectSlug) ||
    !value ||
    !allowedValues.has(value)
  ) {
    return Response.json({ error: "invalid feedback" }, { status: 400 });
  }

  const db = getDb();
  const [previous] = await db
    .select({ value: feedback.value })
    .from(feedback)
    .where(and(eq(feedback.deviceId, deviceId), eq(feedback.projectSlug, projectSlug)))
    .limit(1);
  await db
    .insert(feedback)
    .values({ deviceId, projectSlug, value })
    .onConflictDoUpdate({
      target: [feedback.deviceId, feedback.projectSlug],
      set: { value, updatedAt: sql`CURRENT_TIMESTAMP` },
    });

  const changed = previous?.value !== value;
  if (changed) {
    await db.insert(decisionEvents).values({ deviceId, projectSlug, value });
  }

  return Response.json({ ok: true, value, changed });
}
