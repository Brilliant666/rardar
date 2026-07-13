import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { access, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { loadPublishedBundle } from "../app/published-data-loader.mjs";

const templateRoot = new URL("../", import.meta.url);

test("contains the complete Rardar home experience", async () => {
  const [
    page,
    data,
    serverData,
    publishedLoader,
    publishedClient,
    healthRoute,
    publishedBridge,
    signals,
    signalsPage,
    searchPage,
    searchWorkbench,
    nav,
    globalCss,
    runtimeStatus,
    feedbackRoute,
    metricsRoute,
    actionsRoute,
    validation,
    recommendationsRoute,
    dailyList,
    projectCard,
    projectActions,
    projectPage,
    candidatesPage,
    watchlist,
    personalization,
    scoreSemantics,
    schema,
    ensure,
    actionStore,
    actionMigration,
    build,
    viteConfig,
  ] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/data.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/server-data.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/published-data-loader.mjs", import.meta.url), "utf8"),
    readFile(new URL("../app/published-data-client.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/health/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../build/published-data-bridge.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/signals.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/signals/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/search/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/SearchWorkbench.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/Nav.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
    readFile(new URL("../app/components/RuntimeStatus.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/api/feedback/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/metrics/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/actions/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/validation.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/recommendations/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/components/PersonalizedDailyList.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/ProjectCard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/ProjectActions.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/projects/[slug]/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/candidates/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/WatchlistClient.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/personalization.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/score-semantics.mjs", import.meta.url), "utf8"),
    readFile(new URL("../db/schema.ts", import.meta.url), "utf8"),
    readFile(new URL("../db/ensure.ts", import.meta.url), "utf8"),
    readFile(new URL("../db/project-actions.mjs", import.meta.url), "utf8"),
    readFile(new URL("../drizzle/0003_flaky_spacker_dave.sql", import.meta.url), "utf8"),
    access(new URL("../dist/server/index.js", import.meta.url)),
    readFile(new URL("../vite.config.ts", import.meta.url), "utf8"),
  ]);

  assert.match(page, /Rardar|<Nav/);
  assert.match(page, /今天真正值得看的/);
  assert.match(page, /任务侦察/);
  assert.match(page, /Daily Five/);
  assert.match(page, /长期高热/);
  assert.match(page, /DecisionMetrics/);
  assert.match(page, /SignalDigest/);
  assert.match(page, /PersonalizedDailyList/);
  assert.match(data, /taskTerms/);
  assert.match(data, /enduranceScore/);
  assert.match(data, /attentionScore/);
  assert.match(data, /engineeringReadiness/);
  assert.match(data, /reuseFitScore/);
  assert.match(data, /evidenceCompleteness/);
  assert.match(data, /scoreExplanations/);
  assert.match(data, /fitHypothesis/);
  assert.doesNotMatch(data, /globalScore|reuseScore|\bfit:/);
  assert.match(data, /dailyTrackCounts/);
  assert.doesNotMatch(data, /data\/catalog\/latest\.json|catalogJson/);
  assert.doesNotMatch(data, /starsToday/);
  assert.match(serverData, /await loadPublishedBundleFromBridge/);
  assert.match(serverData, /normalizeCatalogSnapshot\(bundle\.catalog\)/);
  assert.match(serverData, /projects\.slice\(0, 5\)/);
  assert.match(publishedLoader, /current\.json/);
  assert.match(publishedLoader, /manifestSha256/);
  assert.match(publishedLoader, /artifact hash mismatch/);
  assert.match(publishedClient, /RARDAR_DATA_BRIDGE_ORIGIN/);
  assert.doesNotMatch(publishedClient, /await headers\(\)|get\("host"\)/);
  assert.match(publishedClient, /X-Rardar-Data-Token/);
  assert.match(publishedClient, /cache: "no-store"/);
  assert.match(healthRoute, /status: "healthy", generationId/);
  assert.match(healthRoute, /status: "degraded"/);
  assert.match(publishedBridge, /loadPublishedBundle\(dataDirectory\)/);
  assert.match(publishedBridge, /PUBLISHED_DATA_BRIDGE_PATH/);
  assert.match(publishedBridge, /Cache-Control", "no-store"/);
  assert.match(metricsRoute, /effective_decisions/);
  assert.match(metricsRoute, /cache-control.*no-store/s);
  assert.match(feedbackRoute, /projectSlugs/);
  assert.match(feedbackRoute, /noStoreHeaders/);
  assert.match(feedbackRoute, /setWhere: ne\(feedback\.value, value\)/);
  assert.match(feedbackRoute, /changedRows\.length === 1/);
  assert.doesNotMatch(feedbackRoute, /insert\(decisionEvents\)/);
  assert.match(metricsRoute, /readWeeklyActionMetrics/);
  assert.doesNotMatch(metricsRoute, /FROM project_actions/);
  assert.match(actionsRoute, /allowedActions/);
  assert.match(actionsRoute, /unknown project/);
  assert.match(actionsRoute, /appendProjectActionEvent/);
  assert.match(actionsRoute, /idempotencyKey/);
  assert.match(actionsRoute, /status === "conflict"/);
  assert.match(actionsRoute, /readProjectActionState/);
  assert.match(validation, /request\.json\(\)/);
  assert.match(validation, /typeof value === "string"/);
  assert.match(projectActions, /确认复用/);
  assert.match(projectActions, /successfulMutationVersion/);
  assert.match(projectActions, /mutationVersionAtStart/);
  assert.match(projectActions, /successfulMutationVersion\.current \+= 1/);
  assert.match(projectActions, /inFlightActions/);
  assert.match(projectActions, /retryKeys/);
  assert.match(projectActions, /createProjectActionIdempotencyKey/);
  assert.match(projectPage, /<ProjectActions key=\{project\.slug\}/);
  assert.doesNotMatch(projectActions, /if \(selected\.has\(action\)\) return/);
  assert.match(watchlist, /item\.action !== "saved"/);
  assert.match(watchlist, /已收藏/);
  assert.match(recommendationsRoute, /rankProjects/);
  assert.match(dailyList, /rardar:feedback|feedbackEventName/);
  assert.match(dailyList, /currentRequestVersion = \+\+requestVersion\.current/);
  assert.match(dailyList, /currentRequestVersion !== requestVersion\.current/);
  assert.match(personalization, /降低重复曝光/);
  assert.match(personalization, /evidenceBaseScore\(project\)/);
  assert.doesNotMatch(personalization, /globalScore|reuseScore/);
  assert.match(personalization, /balanceHeatTracks/);
  assert.match(scoreSemantics, /schemaVersion === 1/);
  assert.match(scoreSemantics, /schemaVersion === 2/);
  assert.match(scoreSemantics, /engineeringReadiness: null/);
  assert.match(scoreSemantics, /reuseFitScore: null/);
  assert.match(scoreSemantics, /evidenceCompleteness: null/);
  assert.match(scoreSemantics, /attentionScore \* 0\.58 \+ engineeringReadiness \* 0\.42/);
  assert.match(schema, /decisionEvents/);
  assert.match(schema, /projectActionEvents/);
  assert.match(schema, /projectActionState/);
  assert.match(ensure, /schemaReady/);
  assert.match(ensure, /CREATE TRIGGER IF NOT EXISTS feedback_insert_decision_event/);
  assert.match(ensure, /CREATE TRIGGER IF NOT EXISTS feedback_update_decision_event/);
  assert.match(ensure, /WHEN OLD\.value <> NEW\.value/);
  assert.match(ensure, /prepareProjectActionSchema/);
  assert.match(actionStore, /project_action_events/);
  assert.match(actionStore, /project_action_state/);
  assert.match(actionStore, /project_action_events_reject_update/);
  assert.match(actionStore, /project_action_events_reject_identity_replacement/);
  assert.match(actionStore, /legacy-project-actions:/);
  assert.match(actionMigration, /project_action_events_sync_state/);
  assert.match(actionMigration, /project_action_events_reject_delete/);
  assert.match(viteConfig, /ignored: \["\*\*\/data\/generations\/\*\*"\]/);
  assert.match(viteConfig, /publishedDataBridge/);
  assert.match(viteConfig, /RARDAR_DATA_BRIDGE_TOKEN/);
  assert.match(viteConfig, /RARDAR_DATA_BRIDGE_ORIGIN/);
  assert.doesNotMatch(signals, /data\/signals\/latest\.json|signalJson|codexQueueJson/);
  assert.match(signals, /applySignalEnrichments/);
  assert.match(signals, /isCurrentEnrichment/);
  assert.match(signals, /sourcePublishedAt/);
  assert.doesNotMatch(signals, /schedulerJson/);
  assert.match(signalsPage, /sourceStatus/);
  assert.match(signalsPage, /RuntimeStatus/);
  assert.match(searchPage, /search-page/);
  assert.match(searchPage, /radar-field/);
  assert.doesNotMatch(searchPage, /dark-page/);
  assert.match(searchWorkbench, /search-presets/);
  assert.match(searchWorkbench, /search-overview/);
  assert.match(searchWorkbench, /match-row-rich/);
  assert.match(searchWorkbench, /任务匹配/);
  assert.match(searchWorkbench, /\/100/);
  assert.doesNotMatch(searchWorkbench, /reuseScore|复用价值|\{score\}%/);
  assert.match(projectCard, /关注优先级/);
  assert.match(projectCard, /静态工程就绪度/);
  assert.match(projectPage, /持久热度/);
  assert.match(projectPage, /证据完整度|SCORE_DIMENSION_LABELS/);
  assert.match(projectPage, /适用场景假设/);
  assert.match(projectPage, />事实</);
  assert.match(projectPage, />代理</);
  assert.match(projectPage, />未知</);
  assert.match(projectPage, />升级条件</);
  assert.match(candidatesPage, /关注/);
  assert.match(candidatesPage, /静态就绪/);
  assert.doesNotMatch(
    [page, projectCard, projectPage, candidatesPage, searchPage, searchWorkbench, dailyList].join("\n"),
    /全球影响|全局影响|复用价值/,
  );
  assert.match(nav, /usePathname/);
  assert.match(nav, /aria-current/);
  assert.match(globalCss, /Rardar fusion visual system/);
  assert.match(globalCss, /--cobalt: #315cff/);
  assert.match(globalCss, /--cyan: #39bdf2/);
  assert.doesNotMatch(globalCss, /--acid: #caff59/);
  assert.match(runtimeStatus, /127\.0\.0\.1:3002\/status/);
  assert.match(runtimeStatus, /heartbeatLimit/);
  assert.match(runtimeStatus, /dataAuditStatus/);
  assert.match(runtimeStatus, /observedNetStarChange/);
  assert.match(runtimeStatus, /净 Star/);
  assert.match(runtimeStatus, /数据需复核/);
  assert.match(runtimeStatus, /等待重试/);
  assert.match(runtimeStatus, /刷新失败/);
  assert.match(serverData, /codexQueue/);
  assert.doesNotMatch(page, /codex-preview|react-loading-skeleton|Your site is taking shape/i);
  assert.equal(build, undefined);
});

test("removes starter-only assets and metadata", async () => {
  const [page, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(page, /<Nav/);
  assert.match(layout, /开源情报与项目复用雷达/);
  assert.match(layout, /og\.png/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  assert.match(packageJson, /local:start/);
  assert.match(packageJson, /data:audit/);
  assert.match(packageJson, /security:audit:prod/);
  await assert.rejects(access(new URL("../app/_sites-preview", import.meta.url)));
  await access(new URL("../public/og.png", import.meta.url));
  await access(new URL("../drizzle/0000_organic_the_professor.sql", import.meta.url));
  await access(templateRoot);
});

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function writeGeneration(dataDirectory, generationId, marker, baseGenerationId = null) {
  const generationDirectory = join(dataDirectory, "generations", generationId);
  const payloads = {
    "snapshots/latest.json": { schema_version: 1, marker: `snapshot-${marker}` },
    "catalog/latest.json": { schemaVersion: 1, marker: `catalog-${marker}`, projects: [] },
    "signals/latest.json": { schemaVersion: 1, marker: `signals-${marker}`, signals: [], topSignals: [] },
    "signals/enrichment.json": { schemaVersion: 1, marker: `enrichment-${marker}`, generatedAt: "2026-07-12T00:00:00Z", items: {} },
    "queues/codex.json": { schemaVersion: 1, marker: `queue-${marker}`, pendingCount: 0 },
  };
  const hashes = {};
  for (const [relativePath, payload] of Object.entries(payloads)) {
    const serialized = `${JSON.stringify(payload)}\n`;
    const target = join(generationDirectory, ...relativePath.split("/"));
    await mkdir(join(target, ".."), { recursive: true });
    await writeFile(target, serialized, "utf8");
    hashes[relativePath] = digest(serialized);
  }
  const manifest = {
    schemaVersion: 1,
    generationId,
    createdAt: "2026-07-12T00:00:01Z",
    baseGenerationId,
    operation: baseGenerationId ? "refresh" : "bootstrap",
    state: "ready",
    failureStage: null,
    error: null,
    artifacts: Object.keys(payloads).sort(),
    hashes,
    audit: {
      status: "healthy",
      errorCount: 0,
      warningCount: 0,
      validatedCount: Object.keys(payloads).length,
    },
  };
  const manifestText = `${JSON.stringify(manifest)}\n`;
  await writeFile(join(generationDirectory, "manifest.json"), manifestText, "utf8");
  return {
    schemaVersion: 1,
    generationId,
    publishedAt: "2026-07-12T00:00:02Z",
    previousGenerationId: baseGenerationId,
    manifestSha256: digest(manifestText),
  };
}

test("loads every web artifact from one current generation and switches atomically", async () => {
  const dataDirectory = await mkdtemp(join(tmpdir(), "rardar-published-data-"));
  try {
    await mkdir(join(dataDirectory, "generations"), { recursive: true });
    const firstId = "generation-001";
    const secondId = "generation-002";
    const firstPointer = await writeGeneration(dataDirectory, firstId, "one");
    const secondPointer = await writeGeneration(dataDirectory, secondId, "two", firstId);

    // A conflicting legacy tree must never become the page source once a
    // current pointer exists.
    await mkdir(join(dataDirectory, "catalog"), { recursive: true });
    await writeFile(
      join(dataDirectory, "catalog", "latest.json"),
      JSON.stringify({ marker: "legacy-flat-data" }),
      "utf8",
    );

    await writeFile(join(dataDirectory, "current.json"), JSON.stringify(firstPointer), "utf8");
    const first = loadPublishedBundle(dataDirectory);
    assert.equal(first.generationId, firstId);
    assert.equal(first.catalog.marker, "catalog-one");
    assert.equal(first.signals.marker, "signals-one");
    assert.equal(first.signalEnrichment.marker, "enrichment-one");
    assert.equal(first.codexQueue.marker, "queue-one");

    await writeFile(join(dataDirectory, "current.json"), JSON.stringify(secondPointer), "utf8");
    const second = loadPublishedBundle(dataDirectory);
    assert.equal(second.generationId, secondId);
    assert.equal(second.catalog.marker, "catalog-two");
    assert.equal(second.signals.marker, "signals-two");
    assert.equal(second.signalEnrichment.marker, "enrichment-two");
    assert.equal(second.codexQueue.marker, "queue-two");

    // The already-loaded request remains an internally consistent immutable
    // view even after a later request observes the new pointer.
    assert.equal(first.catalog.marker, "catalog-one");
    assert.equal(first.signals.marker, "signals-one");
    assert.ok(Object.isFrozen(first));
    assert.ok(Object.isFrozen(first.catalog));
  } finally {
    await rm(dataDirectory, { recursive: true, force: true });
  }
});

test("rejects tampered artifacts and unsafe generation pointers", async () => {
  const dataDirectory = await mkdtemp(join(tmpdir(), "rardar-published-data-"));
  try {
    await mkdir(join(dataDirectory, "generations"), { recursive: true });
    const generationId = "generation-safe";
    const pointer = await writeGeneration(dataDirectory, generationId, "safe");
    await writeFile(join(dataDirectory, "current.json"), JSON.stringify(pointer), "utf8");
    await writeFile(
      join(dataDirectory, "generations", generationId, "catalog", "latest.json"),
      JSON.stringify({ marker: "tampered" }),
      "utf8",
    );
    assert.throws(() => loadPublishedBundle(dataDirectory), /artifact hash mismatch/);

    const unsafePointer = {
      ...pointer,
      generationId: "../escape",
    };
    await writeFile(join(dataDirectory, "current.json"), JSON.stringify(unsafePointer), "utf8");
    assert.throws(() => loadPublishedBundle(dataDirectory), /not a safe generation id/);
  } finally {
    await rm(dataDirectory, { recursive: true, force: true });
  }
});
