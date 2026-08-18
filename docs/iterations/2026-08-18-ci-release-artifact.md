# CI-built Exact Release Artifact v1

## Status and scope

This iteration starts from `main` at
`9b6399fde527eb9775898b41a3f9371952ce066f`. Its single goal is to move release
preparation out of Server Primary and make one Linux release artifact traceable
to one successful main `Verify` commit.

This is repository, CI, packaging, deployment-checker, test, and documentation
work. It does not access or change Production, Windows Primary Runtime, data,
D1, Scheduler, refresh, generation publication, DNS, Public Edge, swap, SSH, or
application product behavior.

## Incident and problem

On 2026-08-17/18 the old production preparation path ran `npm ci` beside the
live service on a 3.8 GiB RAM host with no swap. A registry `ECONNRESET`, an
approximately 13 hour 50 minute failed install, and production workerd memory
pressure culminated in a kernel OOM kill. The service restarted, the natural
08:00 run was missed, and Scheduler catch-up later restored a healthy
generation.

Production availability recovered, but installation safety was degraded. The
architectural defect was co-locating network dependency resolution and build
work with the active Runtime. Adding swap may be evaluated separately under
`OPS-RESOURCE-HARDEN-01`; it is not a substitute for removing release
preparation from Production.

## Trust boundary

```text
main Verify SUCCESS for exact SHA
→ isolated Ubuntu 24.04 x86_64 builder
→ npm ci + full Verify + build
→ Python 3.12 wheelhouse
→ exact git-archive staging
→ manifest + checksum + fresh extraction acceptance
→ GitHub Actions artifact

================ trust boundary ================

operator selects exact SHA artifact
→ Production verifies checksum before extraction
→ read-only artifact verification
→ offline Python venv installation
→ deployment preflight
→ atomic code switch and restart
```

Production no longer resolves Node dependencies, contacts the npm registry, or
runs a production build.

## Exact identity

The release workflow is triggered only by the completed `Verify` workflow and
admits only a successful same-repository `push` on `main`. Checkout, manifest,
archive name, GitHub artifact name, and the successful Verify head are all
bound to the same full 40-character SHA. A branch name, short SHA, `latest`, or
the state of main at a later time is never release authority.

The manifest records:

- repository and schema version;
- commit and successful Verify run/head identity;
- Ubuntu 24.04, Linux x86_64, Node 22.13.1, actual npm version, and Python 3.12
  wheel target;
- SHA-256 of the packaged `package-lock.json` and `requirements.lock`;
- timezone-aware build time.

## Contents and exclusions

Staging begins with `git archive <exact-sha>`, not a tar of the mutable Actions
workspace. CI then adds the complete installed `node_modules`, final `dist`,
and `wheelhouse`.

The artifact deliberately excludes:

- repository `data/` and every mutable generation;
- `.git`, builder virtual environments, Wrangler/Miniflare state, and Vite/npm
  caches;
- `.env*`, `.dev.vars*`, `rardar.secret`, credentials, and token-like paths.

The complete Node tree is required because the current Managed Runtime uses
the validated Vite/Vinext compatibility entry. Shipping production-only npm
dependencies would omit runtime-required tools.

## Link and extraction safety

Node `.bin` entries may be relative symlinks. The verifier accepts them only
when the complete link chain resolves inside the release root and does not
target `data`. Absolute, missing, escaping, system-path, and special-file links
fail closed with stable release artifact errors.

Archive acceptance verifies the archive SHA-256 before creating an extraction
directory. It rejects absolute/traversing names, hard links, unsafe symlinks,
special files, duplicate members, and members nested through a symlink before
extracting anything.

## Offline activation contract

CI accepts a fresh extraction without any npm command. It creates a new Python
venv and installs all locked requirements with `--no-index --find-links
wheelhouse`, runs `pip check`, and proves the packaged Vite and Vinext CLIs are
runnable.

The raw artifact must not contain `.venv`. Production creates `.venv` only
after raw artifact verification; deployment preflight then permits that one
real top-level directory while continuing to bind `RARDAR_PYTHON` to it and
verify the immutable release payload.

## Deployment checker

The existing read-only preflight now requires `release-manifest.json`, both
lock files, the verifier, wheelhouse, `dist`, Vite, and Vinext. The resolved
release directory must be named by the same full SHA. Manifest identity, fixed
platform contract, lock hashes, forbidden content, wheel coverage, and links
are checked before any Runtime activation. The checker does not download or
repair an artifact.

## Validation matrix

- valid, wrong, and short exact SHA;
- wrong platform/architecture and malformed manifest;
- package and Python lock mismatch;
- missing Vite, Vinext, `dist`, or wheelhouse dependency;
- tracked `data`, environment, credential, and cache exclusion;
- safe relative, absolute, escape, missing, and archive symlink behavior;
- deterministic archive bytes for unchanged stage input;
- checksum-first fresh extraction;
- real offline fixture wheel install and `pip check`;
- deployment preflight manifest/lock binding and post-install `.venv` mode;
- workflow trigger, exact checkout, fixed builder/toolchains, pinned actions,
  and no npm install/build in acceptance.

The complete repository `npm run verify` remains mandatory. The first actual
release artifact is not considered proven until this workflow exists on the
default branch, a main push `Verify` succeeds for an exact SHA, and the
corresponding Release Artifact workflow and uploaded artifact succeed.

## Rollback and non-goals

This iteration does not activate an artifact. A future
`PROD-PRODUCT-RELEASE-02` must preserve the current release and stopped-state
data/D1 backup, then atomically switch only after offline checks. Code rollback
switches back to the previous exact release; data and D1 remain separate unless
their own explicit recovery contract is invoked.

This iteration does not implement server memory/swap hardening, Public Edge,
SSH hardening, P1-6C2, P2, new data contracts, or Scheduler behavior.
