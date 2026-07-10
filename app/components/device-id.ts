export const feedbackEventName = "rardar:feedback";

const storageKey = "rardar-device-id";

export function getDeviceId(create = true) {
  if (typeof window === "undefined") return null;
  const existing = window.localStorage.getItem(storageKey);
  if (existing || !create) return existing;
  const id = globalThis.crypto?.randomUUID?.() ?? `device-${Date.now()}`;
  window.localStorage.setItem(storageKey, id);
  return id;
}
