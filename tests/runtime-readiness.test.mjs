import assert from "node:assert/strict";
import test from "node:test";

import {
  evaluateDataFreshness,
  formatSnapshotAge,
  normalizeRuntimeStatusSnapshot,
  parseRfc3339Instant,
  runtimeReadinessConfig,
} from "../app/runtime-readiness.mjs";

const now = Date.parse("2026-08-10T00:00:00Z");
const threshold = 36 * 60 * 60;

test("runtime readiness defaults and explicit configuration are deterministic", () => {
  assert.deepEqual(runtimeReadinessConfig(), {
    scheduleAt: "08:00",
    scheduleTimezone: "Asia/Shanghai",
    staleAfterHours: 36,
    staleAfterSeconds: 129600,
  });
  assert.deepEqual(
    runtimeReadinessConfig({
      RARDAR_SCHEDULE_AT: "06:45",
      RARDAR_SCHEDULE_TIMEZONE: "Europe/Berlin",
      RARDAR_STALE_AFTER_HOURS: "48",
    }),
    {
      scheduleAt: "06:45",
      scheduleTimezone: "Europe/Berlin",
      staleAfterHours: 48,
      staleAfterSeconds: 172800,
    },
  );
  for (const environment of [
    { RARDAR_SCHEDULE_AT: "8:00" },
    { RARDAR_SCHEDULE_AT: "24:00" },
    { RARDAR_SCHEDULE_TIMEZONE: "Not/A_Zone" },
    { RARDAR_STALE_AFTER_HOURS: "0" },
    { RARDAR_STALE_AFTER_HOURS: "36.5" },
  ]) {
    assert.throws(() => runtimeReadinessConfig(environment), /configuration is invalid/);
  }
});

test("snapshot freshness uses the inclusive threshold and a bounded future skew", () => {
  assert.equal(
    parseRfc3339Instant("2026-08-10T00:00:00Z"),
    parseRfc3339Instant("2026-08-10T08:00:00+08:00"),
  );
  const beforeBoundary = evaluateDataFreshness("2026-08-08T12:00:01Z", threshold, now);
  assert.equal(beforeBoundary.freshness, "fresh");
  const boundary = evaluateDataFreshness("2026-08-08T12:00:00Z", threshold, now);
  assert.equal(boundary.freshness, "fresh");
  assert.equal(boundary.ageSeconds, threshold);
  const afterBoundary = evaluateDataFreshness("2026-08-08T11:59:59.999Z", threshold, now);
  assert.equal(afterBoundary.freshness, "stale");

  const toleratedFuture = evaluateDataFreshness("2026-08-10T00:05:00Z", threshold, now);
  assert.equal(toleratedFuture.freshness, "fresh");
  assert.equal(toleratedFuture.ageSeconds, 0);
  assert.throws(
    () => evaluateDataFreshness("2026-08-10T00:05:00.001Z", threshold, now),
    /unexpectedly in the future/,
  );
});

test("invalid snapshot timestamps fail closed and age text stays lightweight", () => {
  for (const capturedAt of [
    undefined,
    null,
    "",
    "2026-08-10",
    "not-a-time",
    "2026-02-29T00:00:00Z",
    "2026-04-31T00:00:00Z",
    "2026-08-10T24:00:00Z",
    "2026-08-10T00:60:00Z",
    "2026-08-10T00:00:60Z",
    "2026-08-10T00:00:00+24:00",
  ]) {
    assert.throws(
      () => evaluateDataFreshness(capturedAt, threshold, now),
      /snapshot capturedAt is invalid/,
    );
  }
  assert.equal(formatSnapshotAge(49 * 60 * 60), "2 天");
  assert.equal(formatSnapshotAge(90 * 60), "1 小时");
});

test("runtime status normalization rejects malformed and future telemetry", () => {
  const snapshot = {
    schemaVersion: 1,
    state: "healthy",
    checkedAt: "2026-08-10T00:00:00Z",
    message: "ok",
    services: {
      website: { state: "healthy", pid: 10 },
      scheduler: { state: "healthy", pid: 11 },
    },
    data: {
      freshness: "fresh",
      currentGenerationId: "generation-a",
      snapshotCapturedAt: "2026-08-09T23:00:00Z",
      snapshotAgeSeconds: 3600,
      staleAfterSeconds: threshold,
      lastSuccessfulRefreshAt: "2026-08-09T23:05:00Z",
      lastSuccessfulSnapshotAt: "2026-08-09T23:00:00Z",
    },
    schedule: {
      at: "08:00",
      timezone: "Asia/Shanghai",
      nextRunAt: "2026-08-11T00:00:00Z",
    },
  };
  assert.equal(normalizeRuntimeStatusSnapshot(snapshot, now).state, "healthy");
  const staleAndBlocked = {
    ...snapshot,
    state: "degraded",
    services: {
      ...snapshot.services,
      scheduler: { state: "blocked", pid: null },
    },
    data: {
      ...snapshot.data,
      freshness: "stale",
      snapshotAgeSeconds: threshold + 1,
    },
  };
  assert.equal(normalizeRuntimeStatusSnapshot(staleAndBlocked, now).state, "degraded");
  const stopped = {
    ...snapshot,
    state: "stopped",
    services: {
      website: { state: "stopped", pid: null },
      scheduler: { state: "stopped", pid: null },
    },
    data: {
      freshness: "invalid",
      currentGenerationId: null,
      snapshotCapturedAt: null,
      snapshotAgeSeconds: null,
      staleAfterSeconds: threshold,
      lastSuccessfulRefreshAt: null,
      lastSuccessfulSnapshotAt: null,
    },
    schedule: { ...snapshot.schedule, nextRunAt: null },
  };
  assert.equal(normalizeRuntimeStatusSnapshot(stopped, now).state, "stopped");
  assert.equal(
    normalizeRuntimeStatusSnapshot(
      { ...snapshot, checkedAt: "2026-08-09T23:59:20Z" },
      now,
    ).state,
    "stale",
  );
  for (const invalid of [
    null,
    {},
    { ...snapshot, services: {} },
    { ...snapshot, data: undefined },
    { ...snapshot, schedule: undefined },
    { ...snapshot, checkedAt: "2026-08-10T00:05:00.001Z" },
    { ...snapshot, checkedAt: "2026-02-29T00:00:00Z" },
    { ...snapshot, data: { ...snapshot.data, freshness: "unknown" } },
    { ...snapshot, data: { ...snapshot.data, currentGenerationId: "../escape" } },
    { ...snapshot, data: { ...snapshot.data, freshness: "stale" } },
  ]) {
    assert.throws(
      () => normalizeRuntimeStatusSnapshot(invalid, now),
      /runtime status contract is invalid/,
    );
  }
});
