# Signal → Project Audited Association v1

## Status and scope

This iteration starts from `main` at
`4e9c0eadaf612fdda99d6e988a28720ff336953f`, after Launch Decision Flow v1 was
merged. The implementation lives on `feat/signal-project-association`; until
its Draft PR is reviewed and merged, this capability is not complete on
`main`.

The single goal is to connect a technical Signal to the existing canonical
Project Decision Flow only when the association can be proven from the same
published generation. This is a read-side and cross-artifact audit change. It
does not change collectors, persisted JSON versions, D1, scoring, ranking,
recommendations, Runtime, Scheduler, deployment, or production data.

## Problem

Launch Decision Flow now provides:

```text
Project
→ Why now
→ Evidence
→ Risk
→ Action / Watch / Feedback
```

Signals previously stopped at their source article. Linking by title, slug,
display name, enrichment text, or a similar repository basename would recreate
an ambiguous identity boundary. The product therefore remained deliberately
signal-only until an audited bridge could be defined.

> Absence of association is a valid state, not an error.

## Association authority

The only permitted input is the optional raw Signal field `signal.repo` in
strict GitHub `owner/repository` form. The association code does not inspect:

- title, translated title, summary, takeaway, or other Codex enrichment;
- Signal URL text, source name, legacy slug, or display name;
- substring, token, basename, fuzzy, search, or LLM similarity;
- a source-supplied `projectId`, `projectIdVersion`, or association object.

Codex may explain a Signal. It cannot assign Project identity.

## Stable ID reuse

Both Python Audit and the Worker-side derived helper call the existing Stable
Project Identity v1 primitives. They do not maintain a second lowercase,
slugification, or hashing implementation.

The deterministic sequence is:

```text
signal.repo exists
→ strict identity-v1 repository validation and canonicalization
→ recompute projectId v1
→ exact lookup in the request's verified Catalog identity context
→ verify Catalog repository, projectIdVersion=1, and projectId
→ return github_repository association
```

Any failure to prove the sequence returns no association. Repositories that
differ only by ASCII case follow identity v1 casing semantics. Repositories
with the same basename but different owners remain different identities.

## Same-generation boundary

`loadPublishedData()` obtains one fully verified immutable bundle and creates
the request-scoped Catalog identity context from that bundle. Signal
associations are derived before that function returns, using the Signal
snapshot and identity context already in memory. There is no second pointer or
Catalog read.

The complete `signals` collection is the authority for derived association.
`topSignals` receives the derived result only by exact Signal ID projection; a
top item absent from the complete collection remains signal-only. This prevents
the home digest from independently constructing a less-audited identity.

## Cross-artifact Audit

The semantic Audit now builds an ephemeral canonical repository → identity map
from the current generation's Catalog:

- Catalog v1/v2 identities are mechanically derived with identity v1;
- Catalog v3 carried identities are recomputed and verified;
- duplicate canonical repositories, project ID collisions, wrong versions,
  and forged IDs continue to fail closed;
- every present `signal.repo` is checked with the strict identity primitive,
  including values accepted by the older, broader repository Schema format;
- no repo is valid and produces no warning;
- a valid repo missing from Catalog is valid and remains signal-only;
- an exact Catalog match must resolve to version 1 and the recomputed ID.

The persisted `technical-signals` Schema remains v1. No association or guessed
identity is written into a generation.

## UI behavior

An associated Signal keeps its original source link and explanation, then adds
a small “关联项目” block and a canonical
`/project/v1/<validatedProjectId>` action built by the existing route helper.
It does not embed another Project Card or duplicate Decision Summary.

An unassociated Signal retains the existing
`data-signal-association="signal-only"` behavior and plainly says that Rardar
will not guess ownership. Malformed or unsafe identity input never reaches an
`href`.

## Historical compatibility

Retained Catalog v1/v2 generations continue to derive Stable ID v1 from their
verified repository fields. Catalog v3 continues to verify its carried ID.
Signals may or may not contain `repo`; no historical backfill or retained
generation rewrite is performed. A historical value that cannot satisfy the
strict identity contract remains signal-only at read time and fails a newly
built generation's Audit rather than being normalized heuristically.

## Pointer switching

The isolated Vinext acceptance fixture publishes three independent, audited
generations:

```text
A: Signal repo and exact Catalog project → associated
B: same Signal, project absent from Catalog → signal-only
C: verified Catalog project restored → associated
```

The pointer changes atomically while the same Vinext process remains alive.
Each response exposes its own generation marker, and no response may retain the
previous generation's association.

## Test matrix

- exact match, no repo, and valid repo absent from Catalog;
- same basename with different owners and identity-v1 casing variants;
- malformed repository, forged ID, wrong version, duplicate canonical repo,
  and unsafe path-shaped values;
- source-supplied identity and contradictory enrichment are ignored;
- associated and signal-only UI hooks, canonical helper use, and unchanged
  source links;
- audited A → B → C pointer switching without restart;
- 375px, 768px, and 1440px layout, focus, 44px touch target, and no horizontal
  overflow;
- full Python, Node, Schema, Audit, build, security, data-isolation, and process
  cleanup gates.

Actual pass counts and CI results belong in the Draft PR, not in this design
record before they run.

## Non-goals and rollback

This iteration does not implement fuzzy matching, LLM identity assignment,
new external sources, Search changes, scoring/ranking changes, D1 writes or
migrations, P1-6C2, Public Edge, Runtime/Scheduler work, or TrendRadar/P2.

Rollback is a normal application revert: associations disappear and Signals
return to signal-only. Because no persisted artifact or database contract is
changed, rollback does not require a generation rollback, data migration, or
fact repair.
