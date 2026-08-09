import { env } from "cloudflare:workers";
import { ensureDecisionSchema } from "../../../db/ensure";
import {
  ACTION_VALUES,
  LEGACY_IDEMPOTENCY_PREFIX,
} from "../../../db/project-actions.mjs";
import {
  appendStableProjectActionEvent,
  readStableProjectActionState,
  stableStateToActionProjection,
} from "../../../db/stable-project-decisions.mjs";
import {
  ProjectIdentityError,
  createProjectIdentityContext,
  projectIdentityErrorResponse,
  resolveProjectSelector,
  selectorFromRecord,
  selectorFromSearchParams,
  withCurrentProjectIdentity,
  withCurrentProjectIdentityIfPresent,
} from "../../project-identity.mjs";
import { loadPublishedData } from "../../server-data";
import { readJsonObject, trimmedString } from "../validation";

const allowedActions = ACTION_VALUES;
const idempotencyKeyPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$/;
const noStoreHeaders = { "cache-control": "no-store" };

function rejectClientEvidence(value: URLSearchParams | Record<string, unknown>) {
  const has = value instanceof URLSearchParams
    ? (key: string) => value.has(key)
    : (key: string) => Object.hasOwn(value, key);
  if (has("repository") || has("repo") || has("occurredAt")) {
    throw new ProjectIdentityError(
      "client_project_evidence_not_allowed",
      "repository and occurredAt are server-owned evidence",
      400,
    );
  }
}

export async function GET(request: Request) {
  try {
    const published = await loadPublishedData();
    const identityContext = await createProjectIdentityContext(
      published.generationId,
      published.catalog,
      published.publishedAt,
    );
    const url = new URL(request.url);
    rejectClientEvidence(url.searchParams);
    const deviceId = url.searchParams.get("deviceId")?.trim();
    if (!deviceId || deviceId.length > 200) {
      return Response.json({ error: "deviceId is required" }, { status: 400 });
    }
    const project = resolveProjectSelector(
      identityContext,
      selectorFromSearchParams(url.searchParams),
      { required: false },
    );

    await ensureDecisionSchema(identityContext.identityCatalog);
    const rawStates = await readStableProjectActionState(env.DB, deviceId, project?.projectId ?? null);
    const states = rawStates
      .map((state) => withCurrentProjectIdentityIfPresent(identityContext, state))
      .filter((state) => state !== null);
    const actions = stableStateToActionProjection(states)
      .map((action) => withCurrentProjectIdentity(identityContext, action));
    return Response.json({ states, actions }, { headers: noStoreHeaders });
  } catch (error) {
    const response = projectIdentityErrorResponse(error);
    if (response) return response;
    throw error;
  }
}

export async function POST(request: Request) {
  try {
    const published = await loadPublishedData();
    const identityContext = await createProjectIdentityContext(
      published.generationId,
      published.catalog,
      published.publishedAt,
    );
    const payload = await readJsonObject(request);
    if (!payload) {
      return Response.json({ error: "invalid project action" }, { status: 400 });
    }
    rejectClientEvidence(payload);
    const project = resolveProjectSelector(identityContext, selectorFromRecord(payload));
    const deviceId = trimmedString(payload, "deviceId");
    const action = trimmedString(payload, "action");
    const idempotencyKey = trimmedString(payload, "idempotencyKey");
    if (
      !deviceId
      || deviceId.length > 200
      || !action
      || !allowedActions.includes(action as (typeof allowedActions)[number])
      || !idempotencyKey
      || !idempotencyKeyPattern.test(idempotencyKey)
      || idempotencyKey.startsWith(LEGACY_IDEMPOTENCY_PREFIX)
    ) {
      return Response.json({ error: "invalid project action" }, { status: 400 });
    }

    await ensureDecisionSchema(identityContext.identityCatalog);
    const result = await appendStableProjectActionEvent(env.DB, {
      deviceId,
      projectIdVersion: 1,
      projectId: project.projectId,
      projectSlug: project.projectSlug,
      catalogGenerationId: published.generationId,
      action: action as (typeof allowedActions)[number],
      idempotencyKey,
    });
    if (result.status === "conflict") {
      return Response.json(
        { error: "idempotency key is already bound to another project action" },
        { status: 409, headers: noStoreHeaders },
      );
    }
    const event = withCurrentProjectIdentity(identityContext, result.event);
    return Response.json(
      {
        ok: true,
        projectIdVersion: 1,
        projectId: project.projectId,
        projectSlug: project.projectSlug,
        action,
        recorded: result.recorded,
        idempotentReplay: result.status === "replayed",
        event,
      },
      { headers: noStoreHeaders },
    );
  } catch (error) {
    const response = projectIdentityErrorResponse(error);
    if (response) return response;
    throw error;
  }
}
