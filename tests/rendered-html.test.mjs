import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const templateRoot = new URL("../", import.meta.url);

test("contains the complete Rardar home experience", async () => {
  const [page, data, build] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/data.ts", import.meta.url), "utf8"),
    access(new URL("../dist/server/index.js", import.meta.url)),
  ]);

  assert.match(page, /Rardar|<Nav \/>/);
  assert.match(page, /今天真正值得看的/);
  assert.match(page, /任务侦察/);
  assert.match(page, /Daily Five/);
  assert.match(data, /dailyProjects = projects\.slice\(0, 5\)/);
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
  await assert.rejects(access(new URL("../app/_sites-preview", import.meta.url)));
  await access(new URL("../public/og.png", import.meta.url));
  await access(new URL("../drizzle/0000_organic_the_professor.sql", import.meta.url));
  await access(templateRoot);
});
