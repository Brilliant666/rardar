import { and, eq, sql } from "drizzle-orm";
import { getDb } from "../../../db";
import { ensureDecisionSchema } from "../../../db/ensure";
import { decisionEvents, feedback } from "../../../db/schema";

const allowedValues = new Set(["有用", "无用", "复用", "待确定"]);

export async function GET(request: Request) {
  await ensureDecisionSchema();
  const url = new URL(request.url);
  const deviceId = url.searchParams.get("deviceId")?.trim();
  const projectSlug = url.searchParams.get("projectSlug")?.trim();
  if (!deviceId) return Response.json({ error: "deviceId is required" }, { status: 400 });

  const db = getDb();
  if (projectSlug) {
    const [row] = await db.select().from(feedback).where(and(eq(feedback.deviceId, deviceId), eq(feedback.projectSlug, projectSlug))).limit(1);
    return Response.json({ feedback: row ?? null });
  }

  const rows = await db.select().from(feedback).where(eq(feedback.deviceId, deviceId));
  return Response.json({ feedback: rows });
}

export async function POST(request: Request) {
  await ensureDecisionSchema();
  const payload = (await request.json()) as { deviceId?: string; projectSlug?: string; value?: string };
  const deviceId = payload.deviceId?.trim();
  const projectSlug = payload.projectSlug?.trim();
  const value = payload.value?.trim();

  if (!deviceId || !projectSlug || !value || !allowedValues.has(value)) {
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
