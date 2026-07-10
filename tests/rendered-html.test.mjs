import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const templateRoot = new URL("../", import.meta.url);

test("contains the complete Rardar home experience", async () => {
  const [page, data, signals, signalsPage, runtimeStatus, metricsRoute, actionsRoute, recommendationsRoute, dailyList, projectActions, personalization, queue, schema, build] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/data.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/signals.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/signals/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/RuntimeStatus.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/api/metrics/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/actions/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/recommendations/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/components/PersonalizedDailyList.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/ProjectActions.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/personalization.ts", import.meta.url), "utf8"),
    readFile(new URL("../data/queues/codex.json", import.meta.url), "utf8"),
    readFile(new URL("../db/schema.ts", import.meta.url), "utf8"),
    access(new URL("../dist/server/index.js", import.meta.url)),
  ]);

  assert.match(page, /Rardar|<Nav \/>/);
  assert.match(page, /今天真正值得看的/);
  assert.match(page, /任务侦察/);
  assert.match(page, /Daily Five/);
  assert.match(page, /DecisionMetrics/);
  assert.match(page, /SignalDigest/);
  assert.match(page, /PersonalizedDailyList/);
  assert.match(data, /catalogJson/);
  assert.match(data, /taskTerms/);
  assert.match(data, /dailyProjects = projects\.slice\(0, 5\)/);
  assert.doesNotMatch(data, /starsToday/);
  assert.match(metricsRoute, /effective_decisions/);
  assert.match(metricsRoute, /project_actions/);
  assert.match(actionsRoute, /allowedActions/);
  assert.match(projectActions, /确认复用/);
  assert.match(recommendationsRoute, /rankProjects/);
  assert.match(dailyList, /rardar:feedback|feedbackEventName/);
  assert.match(personalization, /降低重复曝光/);
  assert.match(personalization, /globalScore \* 0\.58/);
  assert.match(schema, /decisionEvents/);
  assert.match(signals, /signalJson/);
  assert.doesNotMatch(signals, /schedulerJson/);
  assert.match(signalsPage, /sourceStatus/);
  assert.match(signalsPage, /RuntimeStatus/);
  assert.match(runtimeStatus, /runtime-status\.json/);
  assert.match(runtimeStatus, /heartbeatLimit/);
  assert.match(queue, /pendingCount/);
  assert.doesNotMatch(page, /codex-preview|react-loading-skeleton|Your site is taking shape/i);
  assert.equal(build, undefined);
});

test("removes starter-only assets and metadata", async () => {
  const [page, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(page, /<Nav \/>/);
  assert.match(layout, /开源情报与项目复用雷达/);
  assert.match(layout, /og\.png/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  assert.match(packageJson, /local:start/);
  await assert.rejects(access(new URL("../app/_sites-preview", import.meta.url)));
  await access(new URL("../public/og.png", import.meta.url));
  await access(new URL("../drizzle/0000_organic_the_professor.sql", import.meta.url));
  await access(templateRoot);
});
