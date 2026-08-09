import { env } from "cloudflare:workers";
import { ensureDecisionSchema } from "../../../db/ensure";
import {
  readStableFeedback,
  upsertStableFeedback,
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

const feedbackValues = ["有用", "无用", "复用", "待确定"] as const;
type FeedbackValue = (typeof feedbackValues)[number];
const allowedValues = new Set<string>(feedbackValues);
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
    const rows = (await readStableFeedback(env.DB, deviceId, project?.projectId ?? null))
      .map((row) => withCurrentProjectIdentityIfPresent(identityContext, row))
      .filter((row) => row !== null);
    if (project) {
      return Response.json({ feedback: rows[0] ?? null }, { headers: noStoreHeaders });
    }
    return Response.json({ feedback: rows }, { headers: noStoreHeaders });
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
      return Response.json({ error: "invalid feedback" }, { status: 400 });
    }
    rejectClientEvidence(payload);
    const project = resolveProjectSelector(identityContext, selectorFromRecord(payload));
    const deviceId = trimmedString(payload, "deviceId");
    const value = trimmedString(payload, "value");
    if (!deviceId || deviceId.length > 200 || !value || !allowedValues.has(value)) {
      return Response.json({ error: "invalid feedback" }, { status: 400 });
    }

    await ensureDecisionSchema(identityContext.identityCatalog);
    const result = await upsertStableFeedback(env.DB, {
      deviceId,
      projectIdVersion: 1,
      projectId: project.projectId,
      projectSlug: project.projectSlug,
      catalogGenerationId: published.generationId,
      value: value as FeedbackValue,
    });
    const feedback = withCurrentProjectIdentity(identityContext, result.feedback);
    return Response.json({
      ok: true,
      projectIdVersion: 1,
      projectId: project.projectId,
      projectSlug: project.projectSlug,
      value,
      changed: result.changed,
      feedback,
    }, { headers: noStoreHeaders });
  } catch (error) {
    const response = projectIdentityErrorResponse(error);
    if (response) return response;
    throw error;
  }
}
