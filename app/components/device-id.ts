export const feedbackEventName = "rardar:feedback";
export const projectActionEventName = "rardar:project-action";

export type ProjectActionValue = "opened" | "saved" | "tried" | "cloned" | "reused";

const storageKey = "rardar-device-id";

export function getDeviceId(create = true) {
  if (typeof window === "undefined") return null;
  const existing = window.localStorage.getItem(storageKey);
  if (existing || !create) return existing;
  const id = globalThis.crypto?.randomUUID?.() ?? `device-${Date.now()}`;
  window.localStorage.setItem(storageKey, id);
  return id;
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
  projectSlug: string,
  action: ProjectActionValue,
  idempotencyKey = createProjectActionIdempotencyKey(),
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
        body: JSON.stringify({ deviceId, projectSlug, action, idempotencyKey }),
        keepalive: action === "opened",
      });
      if (response.ok || response.status < 500) break;
      lastError = new Error(`action save failed (${response.status})`);
    } catch (error) {
      lastError = error;
    }
  }
  if (!response?.ok) {
    const retryable = !response || response.status >= 500;
    const message = lastError instanceof Error
      ? lastError.message
      : `action save failed${response ? ` (${response.status})` : ""}`;
    throw new ProjectActionRequestError(message, retryable);
  }
  const result = (await response.json()) as { recorded: boolean };
  window.dispatchEvent(new CustomEvent(projectActionEventName, { detail: { projectSlug, action } }));
  return result;
}
