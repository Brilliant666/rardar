import { env } from "cloudflare:workers";
import { ensureDecisionSchema } from "../../../db/ensure";
import {
  ACTION_VALUES,
  LEGACY_IDEMPOTENCY_PREFIX,
  appendProjectActionEvent,
  readProjectActionState,
  stateToActionProjection,
} from "../../../db/project-actions.mjs";
import { loadPublishedData } from "../../server-data";
import { readJsonObject, trimmedString } from "../validation";

const allowedActions = ACTION_VALUES;
const idempotencyKeyPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$/;
const noStoreHeaders = { "cache-control": "no-store" };

export async function GET(request: Request) {
  const { projects } = await loadPublishedData();
  const projectSlugs = new Set(projects.map((project) => project.slug));
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
  const states = await readProjectActionState(env.DB, deviceId, projectSlug);
  return Response.json(
    { states, actions: stateToActionProjection(states) },
    { headers: noStoreHeaders },
  );
}

export async function POST(request: Request) {
  const { projects } = await loadPublishedData();
  const projectSlugs = new Set(projects.map((project) => project.slug));
  const payload = await readJsonObject(request);
  if (!payload) {
    return Response.json({ error: "invalid project action" }, { status: 400 });
  }
  const deviceId = trimmedString(payload, "deviceId");
  const projectSlug = trimmedString(payload, "projectSlug");
  const action = trimmedString(payload, "action");
  const idempotencyKey = trimmedString(payload, "idempotencyKey");
  if (
    !deviceId ||
    deviceId.length > 200 ||
    !projectSlug ||
    !projectSlugs.has(projectSlug) ||
    !action ||
    !allowedActions.includes(action as (typeof allowedActions)[number]) ||
    !idempotencyKey ||
    !idempotencyKeyPattern.test(idempotencyKey) ||
    idempotencyKey.startsWith(LEGACY_IDEMPOTENCY_PREFIX) ||
    "occurredAt" in payload
  ) {
    return Response.json({ error: "invalid project action" }, { status: 400 });
  }

  await ensureDecisionSchema();
  const result = await appendProjectActionEvent(env.DB, {
    deviceId,
    projectSlug,
    action: action as (typeof allowedActions)[number],
    idempotencyKey,
  });
  if (result.status === "conflict") {
    return Response.json(
      { error: "idempotency key is already bound to another project action" },
      { status: 409, headers: noStoreHeaders },
    );
  }
  return Response.json(
    {
      ok: true,
      action,
      recorded: result.recorded,
      idempotentReplay: result.status === "replayed",
      event: result.event,
    },
    { headers: noStoreHeaders },
  );
}
