export const feedbackEventName = "rardar:feedback";
export const projectActionEventName = "rardar:project-action";
export const generationStaleEventName = "rardar:generation-stale";

export type ProjectActionValue = "opened" | "saved" | "tried" | "cloned" | "reused";
export type ClientProjectIdentity = { projectIdVersion: 1; projectId: string };

const storageKey = "rardar-device-id";
const sharedActionRequests = new Map<string, Promise<{ recorded: boolean; generationId?: unknown }>>();
const sharedActionKeys = new Map<string, string>();

export function getDeviceId(create = true) {
  if (typeof window === "undefined") return null;
  try {
    const existing = window.localStorage.getItem(storageKey);
    if (existing || !create) return existing;
    const id = globalThis.crypto?.randomUUID?.() ?? `device-${Date.now()}`;
    window.localStorage.setItem(storageKey, id);
    return id;
  } catch {
    return null;
  }
}

export function createProjectActionIdempotencyKey() {
  return globalThis.crypto?.randomUUID?.()
    ?? `action-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export class ProjectActionRequestError extends Error {
  readonly retryable: boolean;

  constructor(message: string, retryable: boolean) {
    super(message);
    this.name = "ProjectActionRequestError";
    this.retryable = retryable;
  }
}

export function isRetryableProjectActionError(error: unknown) {
  return !(error instanceof ProjectActionRequestError) || error.retryable;
}

export async function recordProjectAction(
  project: ClientProjectIdentity,
  action: ProjectActionValue,
  idempotencyKey = createProjectActionIdempotencyKey(),
  expectedGenerationId?: string,
) {
  const deviceId = getDeviceId();
  if (!deviceId) throw new Error("device unavailable");
  let response: Response | null = null;
  let lastError: unknown = null;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      response = await fetch("/api/actions", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          deviceId,
          ...project,
          action,
          idempotencyKey,
          ...(expectedGenerationId ? { generationId: expectedGenerationId } : {}),
        }),
        keepalive: action === "opened",
      });
      if (response.ok || response.status < 500) break;
      lastError = new Error(`action save failed (${response.status})`);
    } catch (error) {
      lastError = error;
    }
  }
  if (!response?.ok) {
    if (response?.status === 409) {
      const payload = await response.clone().json().catch(() => null) as {
        error?: unknown;
        generationId?: unknown;
      } | null;
      if (payload?.error === "stale_generation") {
        window.dispatchEvent(new CustomEvent(generationStaleEventName, {
          detail: {
            expectedGenerationId,
            currentGenerationId: payload.generationId,
          },
        }));
      }
    }
    const retryable = !response || response.status >= 500;
    const message = lastError instanceof Error
      ? lastError.message
      : `action save failed${response ? ` (${response.status})` : ""}`;
    throw new ProjectActionRequestError(message, retryable);
  }
  const result = (await response.json()) as { recorded: boolean; generationId?: unknown };
  if (expectedGenerationId && result.generationId !== expectedGenerationId) {
    throw new ProjectActionRequestError("published generation changed", false);
  }
  window.dispatchEvent(new CustomEvent(projectActionEventName, {
    detail: { ...project, action, generationId: result.generationId },
  }));
  return result;
}

export function recordSharedProjectAction(
  scope: string,
  project: ClientProjectIdentity,
  action: ProjectActionValue,
  expectedGenerationId: string,
) {
  const current = sharedActionRequests.get(scope);
  if (current) return current;
  const idempotencyKey = sharedActionKeys.get(scope) ?? createProjectActionIdempotencyKey();
  sharedActionKeys.set(scope, idempotencyKey);
  const request = recordProjectAction(
    project,
    action,
    idempotencyKey,
    expectedGenerationId,
  ).finally(() => {
    if (sharedActionRequests.get(scope) === request) sharedActionRequests.delete(scope);
  });
  sharedActionRequests.set(scope, request);
  return request;
}
