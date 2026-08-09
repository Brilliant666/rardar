import {
  canonicalProjectPath,
} from "../../client-project-identity.mjs";
import {
  projectIdentityErrorResponse,
  resolveProjectSelector,
} from "../../project-identity.mjs";
import { loadPublishedData } from "../../server-data";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ slug: string }> },
) {
  try {
    const { slug } = await params;
    const { generationId, identityContext } = await loadPublishedData();
    const project = resolveProjectSelector(identityContext, { projectSlug: slug });
    if (!project) throw new Error("legacy project selector unexpectedly resolved to null");
    return new Response(null, {
      status: 302,
      headers: {
        "cache-control": "no-store",
        location: canonicalProjectPath(project),
        "x-rardar-generation": generationId,
      },
    });
  } catch (error) {
    const response = projectIdentityErrorResponse(error);
    if (response) return response;
    throw error;
  }
}
