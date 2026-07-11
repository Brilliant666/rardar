# Rardar local workflow

- Treat this project as local-only by default.
- Start the site with `npm run dev` and use `http://127.0.0.1:3000/` as the primary preview and handoff URL.
- Do not publish, deploy, redeploy, or provide a hosted Sites URL unless the user explicitly asks for an online deployment in the current request.
- Keep the local development server running when the user is actively reviewing the site.
- The existing hosted project is legacy state and must not be used as the default preview target.

## Governance

Before planning or implementing repository evolution work, read and follow these documents:

- `CODEX_MASTER_INSTRUCTION.md` — operating instructions, scope, and delivery constraints.
- `docs/RARDAR_AUDIT_BASELINE.md` — current audit findings and priority baseline.
- `docs/RARDAR_NORTH_STAR.md` — mission, North Star metric, and non-negotiable principles.
- `docs/RARDAR_EVOLUTION_PROTOCOL.md` — one-goal iteration, testing, documentation, and PR protocol.
