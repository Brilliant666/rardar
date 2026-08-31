# Discover eligibility boundary correction

## Goal

Correct the product overlap boundary between Today and Discover. Today Explosion keeps every verified exact 24-hour fact, while the Today product publishes only rank 1 through 20. Discover must therefore exclude the published Top 20 rather than every exact project.

## Contract

- `todayExactSet`: every item in `exactRanked`;
- `todayPublishedSet`: numeric GitHub repository IDs at exact rank 1–20;
- `discoverEligibleSet`: latest Observation candidates minus the published set and invalid candidates;
- `exact_outside_published`: exact rank 21+; evaluated rather than automatically published;
- `pre_exact`: projects without a complete exact 24-hour fact;
- identity is always GitHub numeric repository ID.

`TrendingDiscoverArtifact v3` freezes `todayPublishedTopCount=20`, the published-set digest, eligibility counts, and a fourth `outside_today_momentum` stage. An outside project is published only when its real recent four-hour delta passes the absolute `+10` or relative `+1%` channel, both recent intervals are positive, and the recent delta is greater than the prior comparable four-hour delta. No score, prediction, extrapolation, model output, or repository exception participates.

## Safety and compatibility

- retained v1 and v2 generations remain strictly readable, recomputable, and rollback-capable;
- Audit rebuilds the published set, digest, eligibility class, windows, acceleration, stages, reasons, and ordering from cryptographically bound source evidence;
- v3 Observation provenance is descriptor-only and Audit reopens the canonical capture store by safe relative path plus SHA-256; v1/v2 retain their generation-local copies;
- Discover does not write D1, daily generations, Today artifacts, or Observation captures;
- scheduler order and fail-isolation remain unchanged;
- Production activation and retention changes are outside this iteration.

## Replay

The repository-external replay compares the former exact-set exclusion with the corrected Top-20 boundary and policies A/B/C. Policy C is selected: pre-exact retains the v2 total-window gate; exact rank 21+ uses a four-hour window, the same `+10 / +1%` channels, two positive intervals, and positive acceleration. Empty output remains valid after every eligible candidate is evaluated.

Evidence is stored outside the repository at `%TEMP%/rardar-discover-boundary-correction/20260831-173452/`: `root-cause.json` (`SHA-256 29a67362cf24cc4111fe9a0fa9b76a8797df52fcca0be2ed40e57efcccfcc4a3`) and `replay-report.json` (`SHA-256 77bd4f1037af692f874d67623820bcc245026e801cc6f76aba6723c0c461e634`). The source archive contains 55 continuous captures across 108 hours; 38 derive points across 74 hours have a contemporaneous exact Today source. The old boundary published 15 events from 2 repositories and was empty at 23 points. Corrected policy C published 1,681 events from 178 repositories across the replay; the baseline current point at 02:00 UTC publishes 31 outside-momentum projects after evaluating 454 valid exact-outside candidates. The later local 04:00 capture produces 73, which is intentionally reported for product-density review rather than hidden with an arbitrary cap.

The real local 04:00 v3 candidate is 3,861,093 bytes across four files and contains zero duplicated Observation bytes. At twelve two-hour derives per day that is roughly 46.3 MB/day, 1.39 GB/30 days, or 4.17 GB/90 days before filesystem compression or retention. Most remaining volume is the frozen Today source; changing retention or deduplicating that source is deliberately deferred to the observability/retention iteration.

## Rollback

Rollback the independent Discover pointer to a retained healthy v1/v2 generation. This does not change Today, daily generation data, Observation captures, D1, or scheduler ordering.
