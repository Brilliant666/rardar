import { loadPublishedData } from "../../server-data";

const noStoreHeaders = { "cache-control": "no-store" };

function shortError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/\s+/g, " ").trim().slice(0, 240) || "published generation is unavailable";
}

export async function GET() {
  try {
    const { generationId } = await loadPublishedData();
    return Response.json(
      { status: "healthy", generationId },
      { status: 200, headers: noStoreHeaders },
    );
  } catch (error) {
    return Response.json(
      { status: "degraded", error: shortError(error) },
      { status: 503, headers: noStoreHeaders },
    );
  }
}
