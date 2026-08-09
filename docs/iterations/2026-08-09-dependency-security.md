# 2026-08-09 — Production dependency security

## 目标

在不放宽 `npm run verify` 或 GitHub 分支保护的前提下，修复 2026-08-09 已知的四项生产依赖高危漏洞，使独立依赖安全 PR 可以恢复严格的 Production dependency security audit。

本轮以 `main` 提交 `d41033fcb918ffa7fb36b00d3940515ae215c279` 为基线，只处理依赖声明、锁文件和本迭代记录。它不修改 staging artifact conflict resolver、远程静态分析、数据契约、Runtime、正式数据或产品行为。

## 基线与根因

Draft PR #10 的功能门禁已经通过 lint、299 项 Python 测试（其中 4 项平台权限型 skip）、Schema、跨文件 Audit、production build、22 项 Node/真实 Vinext HTTP 与 D1 测试；唯一失败项是 `npm audit --omit=dev`。

在未修改依赖的基线上，生产审计报告 4 个 high severity 依赖：

- `next@16.2.6`；
- `postcss@8.5.14`；
- `nanoid@3.3.12`；
- `sharp@0.34.5`。

这些漏洞公告晚于此前通过的 Verify，并非 resolver hotfix 引入。由于 `main` 要求完整 `Verify` 且禁止绕过安全审计，依赖升级必须作为独立目标交付，而不能混入 PR #10。

## 依赖变更

- `next`：`16.2.6` → `16.3.0`；
- `eslint-config-next`：`16.2.6` → `16.3.0`，保持框架和 lint 规则版本一致；
- `postcss` override：`8.5.14` → `8.5.23`，与 Next.js 16.3.0 的精确运行时依赖一致；
- 锁文件中的生产 `nanoid`：`3.3.12` → `3.3.18`；
- Next.js 的生产可选 `sharp`：`0.34.5` → `0.35.3`。

完整锁文件仍包含开发工具链 Miniflare 使用的 `sharp@0.34.5`，但该节点标记为 dev-only，不进入 `npm audit --omit=dev` 或生产安装树。因此本轮只声称 production audit 清零，不声称包含 dev dependencies 的全量 audit 清零。没有将 `nanoid` 升至不兼容的 v6，也没有添加新的直接依赖。

## 安全与兼容边界

- 不降低审计等级，不使用 `continue-on-error`，不修改 required check；
- `package.json` 与 `package-lock.json` 同步更新，确保 `npm ci` 可重复安装；
- Next.js 16.3.0 是 minor 升级，主要兼容风险在 Vinext/RSC 适配；
- PostCSS 升级同时影响 Next.js、Tailwind 和 Vite 构建路径；
- `sharp@0.35.3` 使用新的原生二进制与 libvips，因此必须由干净安装、production build、真实 Vinext HTTP 和 GitHub Ubuntu Verify 共同验证；
- React 19.2.6 与 Node.js 22.13.1/24.x 满足新依赖的 peer 和 engine 范围。

## 验证

本地验证使用独立 worktree、独立 `.venv`、官方 npm registry 和一次性 Verify Runtime；隔离 Verify 不以 Primary Runtime 为数据源且没有写入，只在执行前后只读采集其指纹和健康状态。

- 基线 `npm audit --omit=dev`：失败，4 个 high；
- 修改后干净 `npm ci`：通过；
- 生产依赖树：`next@16.3.0`、`postcss@8.5.23`、`nanoid@3.3.18`、`sharp@0.35.3`；
- 修改后 `npm audit --omit=dev`：通过，0 vulnerabilities；
- 完整 `npm run verify`：通过，7 个门禁和全部隔离守卫成功；
- GitHub Actions Verify：仅在 Draft PR 创建后由远端结果确认，不在本地提前声称通过。

完整 Verify 使用 Node.js 24.14.0 和 worktree 自有 Python 3.10.9 `.venv`，耗时约 9 分 15 秒：

- Lint：通过；
- Python：共运行 250 项，240 项通过，10 项因当前 Windows 用户没有创建符号链接或 junction 所需权限而安全跳过；
- Schema：当前 generation 的 21 份 artifact 通过，0 error；
- 跨文件 Audit：`healthy`，0 error、0 warning；
- Production build：通过；
- Node：22/22 通过，真实 Vinext HTTP 使用随机回环端口和临时 data/D1；首页、health、signals、search、pointer switch、fail-closed、rollback 和 D1 API 均通过；
- Production dependency security audit：0 vulnerabilities；
- Verify guards：repository data unchanged、Git-visible file contents unchanged、no Git-visible artifacts、isolated Runtime state removed 全部通过。

Primary 前后只读指纹完全一致：

- HEAD：`d41033fcb918ffa7fb36b00d3940515ae215c279`；
- `data/`：939 个文件、31,874,130 bytes、聚合 SHA-256 `44b392528ab9a2b5f6978f15c571f278b2be387925866aef7ee99eca9eb955d7`；
- `data/current.json` SHA-256：`e249460ce5ec538e20ba80ccd948a3943424a16cbd83dab9341d1c82d7d7c284`；
- current generation：`20260716T000001945465Z-d7223e00847a`；
- Primary Git status：77 项且全部位于 `data/`，非 data 变更为 0；
- Manager PID 30632、Website PID 41700、Scheduler PID 38880 均未由验证重启；
- `127.0.0.1:3000` 始终由 PID 41700 监听，`/api/health` 为 200/`healthy` 且 generation 未变。

长期 Scheduler 在本轮 Verify 基线记录之前已自动执行过一次既有冲突下的 fail-closed 尝试，因此这里的 939 文件是本轮真实执行前基线，不沿用更早的 825 文件历史快照。Verify 期间该指纹没有变化。外层一次性验证目录及其 Node compile cache 已在确认无引用进程和 reparse point 后删除。

## 回滚

回滚时必须将本轮依赖提交整体 revert，使 `package.json` 与 `package-lock.json` 一起恢复。没有数据迁移、generation、数据库或 Runtime 状态需要反向处理。

## 非目标与后续顺序

本轮不处理：

- PR #10 的 staging artifact conflict resolver；
- Windows Git 子进程超时清理与 n8n 大仓 archive fallback；
- Primary 正式 refresh 或数据修复；
- PR #9；
- 部署、合并、评分语义、Stable IDs 或 UI。

本 PR 保持 Draft，等待人工审查和合并。只有它合入并同步最新 `main` 后，才在另一独立分支处理远程静态分析的有界完成问题；随后再同步 PR #10 并重新运行严格演练与 Verify。
