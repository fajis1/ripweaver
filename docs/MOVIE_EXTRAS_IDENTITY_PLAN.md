# Movie With Extras Identity Plan

> **ACTIVE WORKTREE: the `ripweaver-test` worktree**
>
> All implementation, tests, builds, notes, and checkpoints for this plan must
> be made in the test worktree above. Do not modify the installed executable or
> use another checkout as the source of truth.

## Fast Handoff for the Next Agent

Start here for any follow-up to the September 22 Short Circuit 2 live run. Do
not restart from the older unified-routing investigation or assume the live run
failed to classify the disc.

- The authoritative source is this `ripweaver-test` worktree. The similarly
  named plan in the separate main checkout is an older reference from another
  worktree.
- Read the September 22 entries at the end of
  `DISC_ROUTING_TEST_WORKTREE_PLAN.md`, then use this document as the active
  implementation plan.
- The live evidence already established the useful behavioral baseline: title
  0 was recognized as the feature film; titles 2 through 7 were recognized as
  related extras; titles 1, 8, 9, and 10 retained short-title review
  dispositions. Preserve the existing verified MKVs and provisional contracts.
- The current blocker is not acquisition, missing media, or failure to infer
  roles. All seven substantial titles were held at
  `provisional_content_identity_review_required` because accepted content role
  and exact canonical identity are coupled at the safety gate.
- A separate September 22 repair already split inserted-disc/autorip control
  from downstream identification control. Do not re-diagnose the earlier
  seven-item queue stall as the remaining identity problem.
- Do not rerip, rescan the disc, call a provider, retry matching, transcode,
  organize, delete, move, or eject as part of orientation. P0 begins with the
  existing path-redacted state and source inspection only.
- Before editing, inspect the dirty worktree and preserve unrelated changes.
  Do not add `m7_manifest.json`, `media_context.json`, preview output, databases,
  logs, private traces, or ad-hoc scripts to a checkpoint.

The shortest productive path is therefore: confirm this worktree, read the two
September 22 routing-plan entries, locate the code path that emits
`provisional_content_identity_review_required`, and execute P0 below. Do not
resume M7 or design another general routing system before addressing the
role-versus-identity boundary described here.

## Objective

Allow RipWeaver to complete a movie-with-extras disc automatically when the
main movie can be identified exactly and the related extras can be described
accurately, without requiring every extra to exist in a reviewed
special-feature catalogue.

For the current Short Circuit 2 canary, the intended result is:

- title 0: main movie; exact canonical movie identity required;
- titles 2 through 7: related extras; unique descriptive names are sufficient;
- titles 1, 8, 9, and 10: retain their existing reviewed short-title
  dispositions;
- no title may reach organization with a provisional main-movie identity;
- no existing destination may be overwritten.

## Problem Statement

The live identification pass correctly recognized one feature film and six
related bonus features. RipWeaver nevertheless held all seven substantial
titles at `provisional_content_identity_review_required` because the current
safety gate treats these two questions as one decision:

1. What role does this title have: main movie, extra, television episode, or
   unknown?
2. What is its exact canonical identity and final name?

That coupling is too strict for extras and too permissive to weaken globally.
The main movie needs an exact verified identity. An extra needs strong evidence
that it belongs to the movie plus a useful collision-safe descriptive name.

## Non-Negotiable Safety Rules

- Preserve the existing TV identification and all-season coordination behavior.
- User selections remain hints, never forced media-type decisions.
- A Gemini description alone must not establish the exact main-movie identity.
- Provider/catalogue outages are not negative identity evidence.
- Extras may use descriptive identities only after the disc's main movie is
  verified and their relationship to that movie is supported.
- Generated extra names must be sanitized, non-empty, distinct within the disc,
  and traceable to the disc fingerprint and title index.
- Existing library destinations always stop for conflict review; never
  overwrite.
- No implementation or synthetic validation authorizes a live provider call,
  transcode, move, deletion, disc read, or eject.
- Triage items remain excluded from this physical-disc workflow.

## Persisted Decision Model

Store role confidence separately from identity confidence for every title.

### Role decision

- `main_movie`
- `extra`
- `tv_episode`
- `skip`
- `unknown`

The disc composition may settle as `movie_with_extras` when evidence supports
exactly one main movie and one or more related extras.

### Identity decision

- Main movie: canonical title and provider ID must be verified by the movie
  identification route. Runtime and disc evidence must remain consistent.
- Extra: a reviewed catalogue identity is preferred but not required. A
  descriptive identity may be accepted when it is evidence-derived and linked
  to the verified main movie.
- TV: retain the existing episode and season evidence requirements unchanged.

Role acceptance must not falsely mark a provisional main-movie identity as
verified. Identity acceptance must not rewrite the already persisted role
without a new assessment revision.

## Descriptive Extra Naming Policy

A descriptive extra name is acceptable when all of the following are true:

- the persisted disc composition is `movie_with_extras`;
- the main movie has an exact verified identity;
- the title is supported as related bonus material rather than a second movie,
  episode, menu, or unrelated title;
- the description is based on bounded OCR, transcript, catalogue, or Gemini
  evidence already accepted by the identification policy;
- the name is concise and safe for the filesystem;
- duplicate descriptions receive a deterministic distinguishing suffix;
- the immutable contract retains the source title index and evidence summary.

If these conditions are not met, hold only that extra for naming or relevance
review. Do not block unrelated verified titles.

## Implementation Phases

### P0 - Baseline and recovery checkpoint

Status: complete (2026-09-22).

- Record the current live queue state without changing it.
- Preserve the seven provisional Short Circuit 2 contracts and their redacted
  identification audit as the behavioral reference.
- Review untracked scratch/private files before creating a safe checkpoint;
  do not include manifests, media context, logs, databases, or ad-hoc scripts.
- Identify the exact functions that currently convert an accepted movie-bonus
  result into `provisional_content_identity_review_required`.

Acceptance gate: documented baseline and a safe recovery point, or an explicit
note explaining why the checkpoint safety rules refused it.

Baseline source map:

- `backend/gemini_fallback.py` writes a `matched-feature` assignment for an
  accepted descriptive result. It sets `provisional_match` whenever the title
  lacks an independent related-movie subtitle match; this currently applies to
  both `movie` and `extra` roles.
- `backend/downstream_worker.py::_verified_gemini_route_assignment()` accepts
  both roles only when that assignment has `provisional_match is False`. The
  automatic route therefore changes an otherwise matched descriptive extra to
  `provisional_content_identity_review_required` before appending its accepted
  role evidence.
- `pipeline_adapters.py::IdentifyStageAdapter` independently applies the same
  blanket provisional-assignment hold whenever a routing assessment is bound.
  The older compatibility path without routing permits provisional descriptive
  extras, which is why existing adapter coverage did not reproduce the live
  routed-disc result.
- The existing strict test for an assessed provisional movie is correct and
  must remain. The missing regression is a routed `movie_with_extras` disc where
  the main movie remains held until exact verification but evidence-derived
  extras can persist accepted roles and later receive descriptive identities.

P0 inspected source, tests, the path-redacted plans, and Git state only. It did
not open private manifests, media context, logs, databases, provider traces, or
media; it performed no live operation.

### P1 - Separate role and identity outcomes

Status: complete (2026-09-22, synthetic validation).

- Add explicit typed outcomes for accepted content role versus accepted exact
  identity.
- Persist an assessment revision when coherent evidence establishes
  `main_movie` or `extra`, even if the final name is not yet verified.
- Keep revision, claim, sibling-worker, and restart protections from the disc
  routing controller.
- Ensure weak evidence, service failure, and catalogue unavailability remain
  review/service holds rather than affirmative roles.

Acceptance gate: synthetic tests prove role evidence survives restart without
promoting a provisional main movie to an exact identity.

Implementation note: the downstream worker now produces a typed assignment
decision that separates accepted role from `exact_verified`, `exact_pending`,
and `descriptive_pending` identity states. A valid provisional movie or extra
settles its content route as matched and appends immutable content-role evidence
to a new assessment revision, while the queue remains held at
`provisional_content_identity_review_required`. Reopening the queue store
preserves both the role revision and identity hold. Invalid or unapplied results
still append no role evidence, and an exact identity remains required before a
movie can advance.

### P2 - Disc-level `movie_with_extras` settlement

Status: complete (2026-09-22, synthetic validation).

- Aggregate accepted title roles into one persisted disc assessment.
- Require exactly one viable main-movie candidate for automatic settlement.
- Permit multiple extras and durable short-title skips.
- Treat a second plausible feature-length movie as a mixed-disc or ambiguity
  review, not automatically as an extra.
- Keep TV evidence gates and TV route selection unchanged.

Acceptance gate: an 11-title synthetic disc matching the live canary settles as
one main movie, six extras, and four reviewed skips; conflicting TV and
second-movie cases remain held.

Implementation note: routing assessments now support an explicit `skip` role
with `review` provenance. Skips remain part of the exact inventory-bound
assessment but do not contribute to content composition and can claim no
content route. Exactly one accepted movie plus extras derives
`movie_with_extras`; multiple accepted movies remain the distinct
`movies_with_extras` composition with separate movie routes, so a second
feature is never silently reclassified as bonus material. The 11-title
synthetic assessment persists and reloads with one movie, six extras, and four
skips while retaining a contradictory TV hint only as a hint.

### P3 - Exact main-movie verification

Status: complete (2026-09-22, synthetic validation).

- Route only the `main_movie` title through canonical movie identification.
- Require an exact provider identity, canonical title, and compatible runtime
  evidence before the movie identity becomes verified.
- Distinguish no match, ambiguity, provider outage, and invalid response.
- Block extras from final organization until the main movie identity is
  verified, without discarding their completed descriptive evidence.

Acceptance gate: fake-provider tests cover exact match, ambiguous movies,
runtime mismatch, no match, and outage. Only the exact match unlocks dependent
extras.

Implementation note: assessment-designated movie titles on `movie` and
`movie_with_extras` discs now use the existing bounded TMDb/runtime/subtitle
verifier rather than bypassing it as a TV-only feature. Only the main-movie
evidence is supplied to that verifier. An exact result writes the canonical
title, positive TMDb movie ID, `exact_verified` status, compatible runtime
evidence, and the approved movie-subtitle identification method. The worker
rejects a bare `provisional_match: false` flag without those exact fields.
No-match, runtime-incompatible, and ambiguous results retain their diagnostic
status and remain provisional; provider outages and invalid responses return a
typed service failure and cannot fall through to a provisional movie contract.

### P4 - Descriptive extra acceptance

Status: complete (2026-09-23, synthetic validation).

- Convert accepted evidence summaries into concise descriptive names.
- Prefer a reviewed catalogue name when available; otherwise use the bounded
  descriptive result.
- Sanitize names and resolve same-disc duplicates deterministically.
- Preserve title index, role evidence, and relationship to the canonical movie
  in the immutable identify contract.
- Do not require an external special-feature catalogue for descriptive extras.

Acceptance gate: synthetic tests reproduce the six live descriptions, verify
stable names across restart, reject empty/unsafe/unrelated descriptions, and
prove collision handling never overwrites.

Implementation note: a saved-contract policy now accepts descriptive extras
only when the same exact-fingerprint assessment is `movie_with_extras`, the
title has accepted `extra` role evidence, and a separately supplied main-movie
contract has an exact canonical title and positive TMDb ID. Accepted names are
sanitized and bounded, retain the evidence summary and source title index, link
to the verified movie ID, and are explicitly marked `descriptive_accepted`
rather than catalogue-verified. Case-insensitive duplicate names receive stable
`Title NNN` suffixes. Empty, unsafe-only, unrelated-disc, non-extra, and
evidence-incomplete inputs are refused. The identify adapter validates the
linked descriptive fields before producing a collision-preserving movie
`Extras` destination. Synthetic coverage exercises the six-extra live shape;
automatic dependent-item re-evaluation remains P5 work.

### P5 - Worker handoff and restart safety

Status: complete (2026-09-23, synthetic validation).

- Update the downstream worker so role settlement, main-movie verification,
  and extra naming occur under the shared routing/provider lock.
- Re-evaluate dependent extras after the main movie becomes verified.
- Ensure a failed extra does not block the verified movie or sibling extras.
- Prevent unrelated triage recovery from starving physical-disc identification.
- Preserve pause, stop, stale revision, and concurrent-worker behavior.

Acceptance gate: real queue/fake provider tests cover restart between every
transition, sibling revision races, provider serialization, queue fairness, and
partial extra failure.

Implementation note: the worker now performs dependency reconciliation before
normal queue dispatch, so a restarted worker cannot consume the exact main-movie
contract before releasing its dependent extras and unrelated triage recovery
cannot starve that reconciliation. The handoff uses the shared downstream lock,
requires the latest matching assessment scope, writes a new immutable contract
for each accepted extra, and leaves invalid extras independently held. Existing
routing/controller coverage supplies the revision-race and provider-serialization
checks; the added real-SQLite queue tests cover restart persistence, duplicate
descriptions, pause/stop behavior, and partial extra failure.

### P6 - UI and review visibility

Status: complete (2026-09-23, synthetic validation).

- Show the persisted disc composition and each title's accepted role separately
  from its identity status.
- Label descriptive extra names as accepted descriptions, not catalogue-verified
  official feature titles.
- Show why the main movie is verified, ambiguous, or held.
- Provide title-specific review only for unresolved identity/name decisions.
- Do not present a review hold as a need to rerip verified media.

Acceptance gate: frontend build and response tests verify the distinction
between role, exact movie identity, and descriptive extra identity.

Implementation note: path-free pipeline responses now expose identity status
and method separately from the persisted disc composition and accepted title
role. Identity-only holds explicitly state that verified staged media does not
need to be ripped again. The queue renders exact movie verification,
description-pending, and descriptively accepted extras as distinct states, and
labels accepted extra descriptions as non-catalogue identities.

### P7 - Synthetic regression gate

Status: pending.

- Run focused routing, worker, pipeline adapter, and API tests.
- Run the full synthetic pytest suite.
- Run Ruff lint and formatting checks on modified Python files.
- Run the frontend TypeScript/Vite production build.
- Confirm no physical disc, provider, transcode, organization, deletion, or
  eject operation occurred during validation.

Acceptance gate: all relevant checks pass and the worktree contains no new
scratch databases, private traces, manifests, media, or ad-hoc repair scripts.

### P8 - Controlled live continuation

Status: pending and requires separate authorization.

- Reuse the already verified Short Circuit 2 MKVs; do not rerip.
- Present any live provider operation and exact affected titles before running
  it if new provider access is required.
- Apply the new routing logic to the existing seven provisional items.
- Verify title 0 obtains an exact canonical movie identity.
- Verify titles 2 through 7 obtain stable descriptive extra names.
- Stop before transcode or organization unless separately authorized.

Acceptance gate: the live queue shows one exactly identified movie and six
descriptively identified extras, with the four short titles unchanged and no
media mutation.

## Required Regression Matrix

- Movie plus extras with no user hint.
- Movie plus extras with a contradictory TV hint.
- Movie-only disc.
- Two-feature movie collection.
- TV disc with bonus material.
- Mixed/unknown disc.
- Extra with no useful description.
- Duplicate extra descriptions.
- Provider outage after role settlement.
- Main-movie ambiguity or runtime mismatch.
- Restart after role settlement but before identity settlement.
- Concurrent assessment revision.
- Old triage records present while a physical disc is queued.
- Existing movie or extra destination collision.

## Current Progress Log

- 2026-09-23: Completed P6's API and dashboard visibility. Pipeline item
  responses now separate disc composition, accepted title role, identity
  verification status, and identification method. The UI explains exact movie
  verification, labels descriptive extra identities accurately, and states
  that identity/name review does not require reripping verified staged media.
  The broader API/queue/worker/adapter matrix passes 216 tests and the frontend
  production build passes. Frontend lint retains six pre-existing findings
  outside the P6 additions. No live provider, disc, media, transcode,
  organization, or eject operation occurred.
- 2026-09-23: Completed P5's restart-safe worker handoff. Exact main-movie
  contracts now trigger pre-dispatch dependent-extra reconciliation under the
  shared downstream lock. Valid descriptive extras receive separate immutable
  contracts and return to the identify queue; an invalid extra remains held
  without blocking its sibling or the movie. Added real SQLite queue coverage
  for restart persistence, duplicate-name stability, pause/stop refusal, and
  partial failure. The broader worker/queue/routing/adapter/Gemini matrix and
  modified-file Ruff/format checks pass. No live provider, disc, media,
  transcode, organization, or eject operation occurred.
- 2026-09-23: Completed P4's saved-contract descriptive-extra policy and
  identify-adapter boundary. Added safe bounded naming, deterministic duplicate
  handling, exact main-movie linkage, six-extra shape coverage, input
  immutability checks, and refusal tests for incomplete or unrelated evidence.
  The relevant routing/worker/adapter/Gemini policy matrix passes 174 tests;
  modified-file Ruff and formatting checks pass. No live provider, disc, media,
  transcode, organization, or eject operation occurred.
- 2026-09-22: Completed P3's exact main-movie verification path. Added
  movie-disc use of the bounded provider/runtime/subtitle verifier, exact-field
  enforcement at the worker boundary, path-free no-match/ambiguity diagnostics,
  and typed outage/invalid-response handling. The broader routing, worker,
  adapter, Gemini, and movie-verifier regression set passes 165 tests; Ruff and
  formatting checks pass. No live provider, disc, or media operation occurred.
- 2026-09-22: Completed P2's durable composition model and synthetic canary.
  Added reviewed-skip role provenance, no-route behavior for skips, singular
  `movie_with_extras` settlement, restart persistence, and multi-feature safety
  coverage. The combined routing/controller/worker/adapter suite passes 142
  tests; modified-file Ruff checks pass. No live state was changed and no disc,
  provider, or media operation occurred.
- 2026-09-22: Completed P1's worker/model slice with synthetic-only changes.
  Added explicit role/identity assignment decisions and restart coverage for
  provisional movie and descriptive-extra results. Focused worker tests and
  modified-file Ruff checks pass. The identify adapter still holds routed
  provisional assignments; changing extra acceptance belongs to later phases.
  No live provider, media, disc, transcode, organization, or eject operation
  occurred.
- 2026-09-22: Completed P0 in the active test worktree. Located the two blanket
  provisional-identity gates and the descriptive-assignment producer described
  above. Confirmed that the live result is a routed-disc policy gap rather than
  failed acquisition or failed role inference. The pre-P0 recovery checkpoint
  is `584f76f273e5`; a post-P0 checkpoint is required after this documentation
  update. No private state or live operation was used.
- 2026-09-22: Plan created from the successful live Short Circuit 2 evidence
  review. The current system correctly inferred the feature film and six
  related extras but held all seven because role and exact identity remain
  coupled. No implementation began in this step. No disc, provider, transcode,
  organization, deletion, move, or eject operation was performed.

## Next Step

Begin P7: run the full synthetic regression gate, modified-file checks, and
frontend production validation; confirm the worktree gained no private or live
artifacts.
