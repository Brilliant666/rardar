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

export async function recordProjectAction(projectSlug: string, action: ProjectActionValue) {
  const deviceId = getDeviceId();
  if (!deviceId) throw new Error("device unavailable");
  const response = await fetch("/api/actions", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ deviceId, projectSlug, action }),
    keepalive: action === "opened",
  });
  if (!response.ok) throw new Error("action save failed");
  const result = (await response.json()) as { recorded: boolean };
  window.dispatchEvent(new CustomEvent(projectActionEventName, { detail: { projectSlug, action } }));
  return result;
}
