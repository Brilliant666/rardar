# Runtime observability and bounded retention

> Iteration: `RARDAR-RUNTIME-OBSERVABILITY-AND-RETENTION-01`
>
> Repository implementation record. Production activation remains a separate exact-release cutover and must not be inferred merely from this document being present on `main`.

## Goal

Give the existing single Manager → Website + Scheduler topology enough evidence and bounded storage behavior for long-running Production operation, without adding a monitor platform, daemon, cron, timer, D1 migration, or Discover product change.

## Decisions

- Runtime events use one versioned JSON line format and systemd journal as the sole Production application log backend. Local non-systemd development keeps bounded compatibility files.
- The journal is persistent, compressed, retained for 14 days, bounded to 3 GiB with 128 MiB files, and preserves at least 8 GiB free. The 3 GiB host-wide limit avoids immediately discarding the approximately 2.6 GiB of pre-existing multi-service evidence.
- `RARDAR_TRENDING_DISCOVER_ENABLED` is an independent strict boolean, defaults to disabled, and never disables Observation, Refresh, or Explosion.
- Retention defaults are Capture 90 days, unified/Discover generation 30 days, failed/ready candidate 7 days plus newest 10 per state, and inactive temporary entry 24 hours. Release directories and Operator backups are audit-only.
- Plan/apply/audit are separate operations. Apply requires the exact deterministic plan digest, rebuilds the protected-set guard, rechecks every target identity and digest, and uses transactional rename plus a durable receipt.
- Current and previous healthy generations, the newest three ready generations per artifact type, active candidates, referenced canonical captures, and operator keep/protected markers are non-negotiable protected evidence.
- At 85% disk use, storage becomes warning and a non-daily Discover phase prioritizes one daily retention attempt. At 90% or less than 8 GiB free, only a new Discover candidate is blocked with `discover_storage_guard`; core facts continue through existing atomic fail-closed writes.

## Security boundary

Structured logs bound field length, collection cardinality and total event bytes. Sensitive keys and credential-shaped values, prompt/model/upstream bodies, README content, database URLs and absolute runtime paths are redacted before output. Normal repository metadata failures are aggregated rather than emitted per repository.

Retention never follows symlink, junction or reparse entries and rejects hard links, path traversal, changing targets and malformed/rehashed plans. It does not read or write D1, change Nginx/Public Edge, delete releases/backups, or activate Discover.

## Scheduler behavior

```text
Discover enabled:  Observation → Refresh → Explosion → Discover → Retention
Discover disabled: Observation → Refresh → Explosion → Retention
```

The daily maintenance remains inside the one Scheduler, is idempotent per local day, and failure degrades telemetry without rolling back successful facts or exiting Scheduler. A restart catch-up can run at most the latest due maintenance.

## Verification

The iteration adds deterministic and failure-injection coverage for logging/redaction, strict feature flags, retention protected-set construction, digest-bound apply, transaction rollback/retry, path/link safety, scheduler order/isolation/idempotency, storage warning/hard guards, systemd journal configuration and reviewed telemetry projection. The authoritative final evidence is the exact-head GitHub `Verify` run recorded in the PR.

## Rollback

Application rollback uses the previous exact release and environment/unit backups. Retention deletion is intentionally not reconstructed by code rollback; the protected set and first-apply operator review prevent removal of rollback-critical artifacts, while normal data/backup restore remains the disaster-recovery boundary. Discover stays disabled throughout this iteration.
