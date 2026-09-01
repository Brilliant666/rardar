# Main Verify budget and Retention right-sizing

## Objective

This maintenance iteration restores a complete, trustworthy `main` Verify without deleting or weakening any gate, and narrows high-frequency artifact retention to the first MVP operating window. It does not change the fixed two-hour Observation cadence, collection queries, ranking, Discover eligibility/sorting, D1, UI, model routing, or Production state.

## Verify baseline and timing contract

The cancelled `main` run `33470218136` used the same `ubuntu-latest` job, dependency caches, and `npm run verify` steps as pull requests. Its only complete-gate timeout was the job-level `timeout-minutes: 30`. Lint took about 6.3 seconds; the full Python suite completed successfully (`637` tests, `13` platform skips) in `1572.603` seconds; Schema, data Audit, Retention Audit, and build then completed; Node reached test 73 of 87 before the workflow was cancelled. Security never ran. This is a time-budget failure after a successful Python gate, not a test assertion failure or a `main`-only code path.

The complete Verify job now has a bounded 45-minute budget. It still runs every gate serially and fail-closed. Python runs once through pytest's unittest-compatible collector with `--durations=50`, `--durations-min=1.0`, JUnit XML, and a local plugin that records aggregate setup/call/teardown duration for the 50 slowest items. `verify-timing.json` records repository/workflow identity, runner, Python, total duration, every gate result/duration, and pytest counts/top items; GitHub uploads it with JUnit and writes the useful summary to the job summary. Test-only pytest dependencies remain separate from the Production `requirements.lock` and release wheelhouse.

No test is removed, newly skipped, marked `continue-on-error`, sharded, or made concurrent. A small Retention planner cache avoids re-hashing and re-auditing the same immutable retained generation multiple times during one locked plan; all original hashes, Schema semantics, Audit results, and fail-closed checks remain required. On the same Windows host and Python 3.10 environment, the identical 11 pre-existing Retention tests improved from 81.51 seconds at `e437378` to 48.25 seconds with the cache (10 passed, one unchanged platform skip), a 40.8% reduction without altering assertions or filesystem fixtures.

## Retention policy v2

| Store | Default | Eligibility and protection |
| --- | ---: | --- |
| New Observation Capture | 45 days | Both configured age and embedded `retainUntil` must expire; any retained generation reference wins |
| Historical Capture | original 90 days | Existing immutable `retentionDays=90` / `retainUntil` remains valid and cannot be shortened |
| Refresh generation | 30 days | current, previous healthy, newest 3 per type, and operator keep remain protected |
| Explosion generation | 30 days | same retained-generation protections |
| Discover generation | 14 days | deterministic high-frequency derivative; independent current/previous/newest 3 remain protected |
| Failed candidate | 3 days | latest 10 failed candidates and active/unknown states remain protected |
| Ready-unpublished candidate | 7 days | latest 10 ready candidates remain protected |
| Temporary/partial | 24 hours | only bounded namespaces under the data lock; active candidate/observer boundaries remain protected |
| Release, deployment backup, operator artifact | audit only | never emitted as automatic deletion targets |

Capture is deliberately longer than the longest retained Refresh/Explosion window: 30 audit days plus 15 days for cleanup scheduling, timezone boundaries, and delayed rollback. Configuration is validated before child startup: every age is positive, Capture is strictly greater than both generation windows, Discover does not exceed Capture, candidate latest count is at least one, and temporary hours is positive.

The plan format is versioned as `rardar-retention-v2`. Plan/apply remains exact-digest bound, read-only until explicit apply, transactional, retry-safe, no-follow, and protected-set guarded. This iteration never applies retention to Production.

## Journal boundary

The checked-in journald drop-in is host-global. It therefore retains the safe 14-day / 3 GiB host cap and 8 GiB keep-free instead of reducing all services to 1 GiB. Rardar's own expected 14-day share is bounded through the unit rate limit and structured event field/size limits, and is checked from read-only per-unit journal volume. Exact Production measurements and the resulting capacity projection belong to the PR/final execution evidence.

## Read-only capacity evidence

The 2026-09-01 read-only inventory observed an 82,086,711,296-byte filesystem with 64,548,298,752 bytes used (78.63%) and 17,521,635,328 bytes free. Production data occupied 585,244,627 bytes. The 69 captures occupied 138,909,998 bytes (2,013,188-byte mean, 24,158,261 bytes/day at 12 fixed slots); all were immutable historical 90-day bundles. Retained Refresh generations occupied 217,615,954 bytes (8,369,844-byte mean), Explosion generations 193,821,414 bytes (32,303,569-byte mean), candidates 33,201,403 bytes, and Discover zero bytes because Production Discover remained disabled. Releases (12,689,218,607 bytes), deployment backups (356,799,211 bytes), and operator/runtime artifacts were audited but held constant because they are not automatic Runtime retention targets.

The host journal directory occupied 1,719,664,640 bytes while the active journal reported 58.1 MiB. Rardar itself contributed 101,871 export bytes over the available 14-day window and 47,288 bytes in the latest 24 hours; even projecting that latest daily rate for 14 days is under 1 MiB, far below the 1 GiB Rardar share target. The conservative capacity model nevertheless reserves the complete 3 GiB host-global journal cap.

The formula is `projected used = current used - current data - current journal + modeled retained data + 3 GiB journal cap`. Refresh and Explosion use one scheduled generation/day at their measured mean sizes; Capture uses the measured 12-slot daily rate; Discover contributes zero while separately disabled; 17,322,548 bytes of old unprotected candidate/generation evidence is modeled as eligible, but no Production apply occurred. Under the old policy, the 30-day projection is 82.31% used with 13.52 GiB free, and the 90-day/steady projection is 83.91% with 12.30 GiB free. The new policy's transitional 30-day projection is also 82.31%/13.52 GiB because existing 90-day bundles keep their promise; after convergence, the 45-day Capture steady/90-day projection is 82.58% with 13.32 GiB free. The active policy therefore remains below 85% at steady state and passes both the `<88%` and `>=8 GiB free` gates. If Retention were never activated, the measured 61.83 MiB/day automatic data rate would instead reach 85% around 2026-11-20 and 90% around 2027-01-23; activation remains a separately authorized Production operation.

The read-only Production-equivalent model scanned 127 capture/generation/candidate items: 87 paths were protected, 29 were age-eligible, 14 unprotected items (17,322,548 bytes) would enter a plan, and current/reference/path violations were all zero. Repository behavior tests independently exercise the same boundaries and prove dry-run zero writes and deterministic plan digests.

## Verification and deployment boundary

The required completion gates are local complete Verify, exact-head PR Verify, exact-main Verify, and the automatically produced exact release artifact. The release artifact must bind the final merge SHA and include the versioned five-minute systemd startup timeout. Production may be inspected read-only for capacity and health, but this iteration performs zero Production writes, retention applies, data jobs, restarts, cutovers, Discover activation, or deployments.
