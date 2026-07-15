import { env } from "cloudflare:workers";
import { ensureDecisionSchema } from "../../../db/ensure";
import { readStableFeedback } from "../../../db/stable-project-decisions.mjs";
import { rankProjects } from "../../personalization";
import {
  createProjectIdentityContext,
  projectIdentityErrorResponse,
  withCurrentProjectIdentityIfPresent,
} from "../../project-identity.mjs";
import { loadPublishedData } from "../../server-data";

export async function GET(request: Request) {
  try {
    const published = await loadPublishedData();
    const identityContext = await createProjectIdentityContext(
      published.generationId,
      published.catalog,
      published.publishedAt,
    );
    const url = new URL(request.url);
    const deviceId = url.searchParams.get("deviceId")?.trim();
    if (!deviceId || deviceId.length > 200) {
      return Response.json({ error: "deviceId is required" }, { status: 400 });
    }
    if (
      url.searchParams.has("repository")
      || url.searchParams.has("repo")
      || url.searchParams.has("occurredAt")
    ) {
      return Response.json(
        { error: "client_project_evidence_not_allowed" },
        { status: 400, headers: { "cache-control": "no-store" } },
      );
    }

    await ensureDecisionSchema(identityContext.identityCatalog);
    const rows = (await readStableFeedback(env.DB, deviceId))
      .map((row) => withCurrentProjectIdentityIfPresent(identityContext, row))
      .filter((row) => row !== null);
    const projects = identityContext.stableProjects(published.projects);
    return Response.json(rankProjects(projects, rows), {
      headers: { "cache-control": "no-store" },
    });
  } catch (error) {
    const response = projectIdentityErrorResponse(error);
    if (response) return response;
    throw error;
  }
}
