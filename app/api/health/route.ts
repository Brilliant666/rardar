import { loadPublishedData } from "../../server-data";

const noStoreHeaders = { "cache-control": "no-store" };

function shortError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/\s+/g, " ").trim().slice(0, 240) || "published generation is unavailable";
}

export async function GET() {
  try {
    const { generationId, dataFreshness, runtimeReadiness } = await loadPublishedData();
    const stale = dataFreshness.freshness === "stale";
    return Response.json(
      {
        schemaVersion: 1,
        status: stale ? "degraded" : "healthy",
        ...(stale ? { reason: "published_data_stale" } : {}),
        generationId,
        data: dataFreshness,
        schedule: {
          at: runtimeReadiness.scheduleAt,
          timezone: runtimeReadiness.scheduleTimezone,
        },
      },
      { status: 200, headers: noStoreHeaders },
    );
  } catch (error) {
    return Response.json(
      {
        schemaVersion: 1,
        status: "degraded",
        reason: "published_generation_unavailable",
        error: shortError(error),
      },
      { status: 503, headers: noStoreHeaders },
    );
  }
}
