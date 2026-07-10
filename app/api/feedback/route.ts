import { and, eq, sql } from "drizzle-orm";
import { env } from "cloudflare:workers";
import { getDb } from "../../../db";
import { feedback } from "../../../db/schema";

const allowedValues = new Set(["有用", "无用", "复用", "待确定"]);

async function ensureSchema() {
  await env.DB.batch([
    env.DB.prepare(`
      CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        project_slug TEXT NOT NULL,
        value TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      )
    `),
    env.DB.prepare(`
      CREATE UNIQUE INDEX IF NOT EXISTS feedback_device_project_idx
      ON feedback (device_id, project_slug)
    `),
  ]);
}

export async function GET(request: Request) {
  await ensureSchema();
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
  await ensureSchema();
  const payload = (await request.json()) as { deviceId?: string; projectSlug?: string; value?: string };
  const deviceId = payload.deviceId?.trim();
  const projectSlug = payload.projectSlug?.trim();
  const value = payload.value?.trim();

  if (!deviceId || !projectSlug || !value || !allowedValues.has(value)) {
    return Response.json({ error: "invalid feedback" }, { status: 400 });
  }

  const db = getDb();
  await db
    .insert(feedback)
    .values({ deviceId, projectSlug, value })
    .onConflictDoUpdate({
      target: [feedback.deviceId, feedback.projectSlug],
      set: { value, updatedAt: sql`CURRENT_TIMESTAMP` },
    });

  return Response.json({ ok: true, value });
}
