const DEFAULT_SCHEDULE_AT = "08:00";
const DEFAULT_SCHEDULE_TIMEZONE = "Asia/Shanghai";
const DEFAULT_STALE_AFTER_HOURS = 36;
const MAX_STALE_AFTER_HOURS = 24 * 365;
const MAX_FUTURE_SKEW_SECONDS = 5 * 60;
const CLOCK_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
const POSITIVE_INTEGER = /^[1-9]\d*$/;
const RFC3339_WITH_TIMEZONE =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:Z|([+-])(\d{2}):(\d{2}))$/;
const GENERATION_ID_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$/;

function fail(message) {
  throw new Error(`runtime readiness configuration is invalid: ${message}`);
}

function validateTimezone(value) {
  if (typeof value !== "string" || !value || value.trim() !== value) {
    fail("RARDAR_SCHEDULE_TIMEZONE must be a valid IANA timezone");
  }
  try {
    new Intl.DateTimeFormat("en", { timeZone: value }).format(0);
  } catch {
    fail("RARDAR_SCHEDULE_TIMEZONE must be a valid IANA timezone");
  }
  return value;
}

function parseRfc3339Milliseconds(value) {
  const timestampMatch = typeof value === "string" ? RFC3339_WITH_TIMEZONE.exec(value) : null;
  if (!timestampMatch) return null;
  const [, yearText, monthText, dayText, hourText, minuteText, secondText, , , offsetHourText, offsetMinuteText] =
    timestampMatch;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const offsetHour = offsetHourText === undefined ? 0 : Number(offsetHourText);
  const offsetMinute = offsetMinuteText === undefined ? 0 : Number(offsetMinuteText);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (
    month < 1
    || month > 12
    || day < 1
    || day > daysInMonth[month - 1]
    || hour > 23
    || minute > 59
    || second > 59
    || offsetHour > 23
    || offsetMinute > 59
  ) return null;
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) ? milliseconds : null;
}

export function parseRfc3339Instant(value) {
  const milliseconds = parseRfc3339Milliseconds(value);
  if (milliseconds === null) {
    throw new Error("published generation is unavailable: timestamp is invalid");
  }
  return milliseconds;
}

export function runtimeReadinessConfig(environment = {}) {
  const scheduleAt = environment.RARDAR_SCHEDULE_AT ?? DEFAULT_SCHEDULE_AT;
  if (typeof scheduleAt !== "string" || !CLOCK_PATTERN.test(scheduleAt)) {
    fail("RARDAR_SCHEDULE_AT must use canonical HH:MM in 24-hour time");
  }
  const scheduleTimezone = validateTimezone(
    environment.RARDAR_SCHEDULE_TIMEZONE ?? DEFAULT_SCHEDULE_TIMEZONE,
  );
  const staleText = environment.RARDAR_STALE_AFTER_HOURS ?? String(DEFAULT_STALE_AFTER_HOURS);
  if (typeof staleText !== "string" || !POSITIVE_INTEGER.test(staleText)) {
    fail(`RARDAR_STALE_AFTER_HOURS must be an integer from 1 to ${MAX_STALE_AFTER_HOURS}`);
  }
  const staleAfterHours = Number(staleText);
  if (!Number.isSafeInteger(staleAfterHours) || staleAfterHours > MAX_STALE_AFTER_HOURS) {
    fail(`RARDAR_STALE_AFTER_HOURS must be an integer from 1 to ${MAX_STALE_AFTER_HOURS}`);
  }
  return Object.freeze({
    scheduleAt,
    scheduleTimezone,
    staleAfterHours,
    staleAfterSeconds: staleAfterHours * 60 * 60,
  });
}

export function evaluateDataFreshness(
  snapshotCapturedAt,
  staleAfterSeconds,
  nowMilliseconds = Date.now(),
) {
  let capturedMilliseconds;
  try {
    capturedMilliseconds = parseRfc3339Instant(snapshotCapturedAt);
  } catch {
    throw new Error("published generation is unavailable: snapshot capturedAt is invalid");
  }
  if (!Number.isSafeInteger(staleAfterSeconds) || staleAfterSeconds < 1) {
    fail("staleAfterSeconds must be a positive safe integer");
  }
  if (!Number.isFinite(nowMilliseconds)) {
    fail("current time is invalid");
  }
  const futureSeconds = (capturedMilliseconds - nowMilliseconds) / 1000;
  if (futureSeconds > MAX_FUTURE_SKEW_SECONDS) {
    throw new Error("published generation is unavailable: snapshot capturedAt is unexpectedly in the future");
  }
  const ageSeconds = Math.max(0, (nowMilliseconds - capturedMilliseconds) / 1000);
  return Object.freeze({
    freshness: ageSeconds <= staleAfterSeconds ? "fresh" : "stale",
    snapshotCapturedAt,
    ageSeconds,
    staleAfterSeconds,
  });
}

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

const RUNTIME_STATES = new Set(["healthy", "degraded", "starting", "stopped", "stale"]);
const SERVICE_STATES = new Set([
  "healthy",
  "degraded",
  "starting",
  "stopped",
  "stale",
  "restarting",
  "blocked",
  "unknown",
]);

function validService(value) {
  return isRecord(value)
    && SERVICE_STATES.has(value.state)
    && (value.pid === null || (Number.isSafeInteger(value.pid) && value.pid > 0));
}

export function normalizeRuntimeStatusSnapshot(
  value,
  nowMilliseconds = Date.now(),
  heartbeatLimitMilliseconds = 35_000,
) {
  if (
    !isRecord(value)
    || value.schemaVersion !== 1
    || !RUNTIME_STATES.has(value.state)
    || typeof value.message !== "string"
    || !isRecord(value.services)
    || !validService(value.services.website)
    || !validService(value.services.scheduler)
    || !isRecord(value.data)
    || !isRecord(value.schedule)
    || !Number.isFinite(nowMilliseconds)
    || !Number.isSafeInteger(heartbeatLimitMilliseconds)
    || heartbeatLimitMilliseconds < 1
  ) {
    throw new Error("runtime status contract is invalid");
  }
  const checkedAt = parseRfc3339Milliseconds(value.checkedAt);
  if (checkedAt === null || checkedAt - nowMilliseconds > MAX_FUTURE_SKEW_SECONDS * 1000) {
    throw new Error("runtime status contract is invalid");
  }
  const data = value.data;
  const schedule = value.schedule;
  const freshnessValid = new Set(["fresh", "stale", "invalid"]).has(data.freshness);
  const generationValid =
    typeof data.currentGenerationId === "string"
    && GENERATION_ID_PATTERN.test(data.currentGenerationId);
  const snapshotValid = parseRfc3339Milliseconds(data.snapshotCapturedAt) !== null;
  const ageValid = Number.isFinite(data.snapshotAgeSeconds) && data.snapshotAgeSeconds >= 0;
  const thresholdValid =
    Number.isSafeInteger(data.staleAfterSeconds) && data.staleAfterSeconds >= 1;
  const lastRefreshValid =
    data.lastSuccessfulRefreshAt === null
    || parseRfc3339Milliseconds(data.lastSuccessfulRefreshAt) !== null;
  const lastSnapshotValid =
    data.lastSuccessfulSnapshotAt === null
    || parseRfc3339Milliseconds(data.lastSuccessfulSnapshotAt) !== null;
  const activeDataValid =
    data.freshness !== "invalid"
    || (
      data.currentGenerationId === null
      && data.snapshotCapturedAt === null
      && data.snapshotAgeSeconds === null
    );
  if (
    !freshnessValid
    || !thresholdValid
    || !lastRefreshValid
    || !lastSnapshotValid
    || (
      data.freshness !== "invalid"
      && (!generationValid || !snapshotValid || !ageValid)
    )
    || (data.freshness === "fresh" && data.snapshotAgeSeconds > data.staleAfterSeconds)
    || (data.freshness === "stale" && data.snapshotAgeSeconds <= data.staleAfterSeconds)
    || !activeDataValid
    || typeof schedule.at !== "string"
    || !CLOCK_PATTERN.test(schedule.at)
    || typeof schedule.timezone !== "string"
  ) throw new Error("runtime status contract is invalid");
  validateTimezone(schedule.timezone);
  if (
    schedule.nextRunAt !== null
    && parseRfc3339Milliseconds(schedule.nextRunAt) === null
  ) throw new Error("runtime status contract is invalid");
  const servicesHealthy =
    value.services.website.state === "healthy"
    && value.services.scheduler.state === "healthy";
  if (
    (value.state === "healthy" && (!servicesHealthy || data.freshness !== "fresh"))
    || (data.freshness === "stale" && value.state !== "degraded")
  ) throw new Error("runtime status contract is invalid");
  if (nowMilliseconds - checkedAt > heartbeatLimitMilliseconds) {
    return Object.freeze({
      ...value,
      state: "stale",
      message: "运行心跳已过期，请重新启动本地管理器",
    });
  }
  return Object.freeze(value);
}

export function formatSnapshotAge(ageSeconds) {
  if (!Number.isFinite(ageSeconds) || ageSeconds < 0) return "未知";
  if (ageSeconds < 60 * 60) return `${Math.max(1, Math.floor(ageSeconds / 60))} 分钟`;
  const hours = ageSeconds / (60 * 60);
  return hours < 48 ? `${Math.floor(hours)} 小时` : `${Math.floor(hours / 24)} 天`;
}

export {
  DEFAULT_SCHEDULE_AT,
  DEFAULT_SCHEDULE_TIMEZONE,
  DEFAULT_STALE_AFTER_HOURS,
  MAX_FUTURE_SKEW_SECONDS,
};
