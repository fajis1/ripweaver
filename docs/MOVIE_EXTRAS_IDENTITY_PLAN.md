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

Status: pending.

- Record the current live queue state without changing it.
- Preserve the seven provisional Short Circuit 2 contracts and their redacted
  identification audit as the behavioral reference.
- Review untracked scratch/private files before creating a safe checkpoint;
  do not include manifests, media context, logs, databases, or ad-hoc scripts.
- Identify the exact functions that currently convert an accepted movie-bonus
  result into `provisional_content_identity_review_required`.

Acceptance gate: documented baseline and a safe recovery point, or an explicit
note explaining why the checkpoint safety rules refused it.

### P1 - Separate role and identity outcomes

Status: pending.

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

### P2 - Disc-level `movie_with_extras` settlement

Status: pending.

- Aggregate accepted title roles into one persisted disc assessment.
- Require exactly one viable main-movie candidate for automatic settlement.
- Permit multiple extras and durable short-title skips.
- Treat a second plausible feature-length movie as a mixed-disc or ambiguity
  review, not automatically as an extra.
- Keep TV evidence gates and TV route selection unchanged.

Acceptance gate: an 11-title synthetic disc matching the live canary settles as
one main movie, six extras, and four reviewed skips; conflicting TV and
second-movie cases remain held.

### P3 - Exact main-movie verification

Status: pending.

- Route only the `main_movie` title through canonical movie identification.
- Require an exact provider identity, canonical title, and compatible runtime
  evidence before the movie identity becomes verified.
- Distinguish no match, ambiguity, provider outage, and invalid response.
- Block extras from final organization until the main movie identity is
  verified, without discarding their completed descriptive evidence.

Acceptance gate: fake-provider tests cover exact match, ambiguous movies,
runtime mismatch, no match, and outage. Only the exact match unlocks dependent
extras.

### P4 - Descriptive extra acceptance

Status: pending.

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

### P5 - Worker handoff and restart safety

Status: pending.

- Update the downstream worker so role settlement, main-movie verification,
  and extra naming occur under the shared routing/provider lock.
- Re-evaluate dependent extras after the main movie becomes verified.
- Ensure a failed extra does not block the verified movie or sibling extras.
- Prevent unrelated triage recovery from starving physical-disc identification.
- Preserve pause, stop, stale revision, and concurrent-worker behavior.

Acceptance gate: real queue/fake provider tests cover restart between every
transition, sibling revision races, provider serialization, queue fairness, and
partial extra failure.

### P6 - UI and review visibility

Status: pending.

- Show the persisted disc composition and each title's accepted role separately
  from its identity status.
- Label descriptive extra names as accepted descriptions, not catalogue-verified
  official feature titles.
- Show why the main movie is verified, ambiguous, or held.
- Provide title-specific review only for unresolved identity/name decisions.
- Do not present a review hold as a need to rerip verified media.

Acceptance gate: frontend build and response tests verify the distinction
between role, exact movie identity, and descriptive extra identity.

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

- 2026-09-22: Plan created from the successful live Short Circuit 2 evidence
  review. The current system correctly inferred the feature film and six
  related extras but held all seven because role and exact identity remain
  coupled. No implementation began in this step. No disc, provider, transcode,
  organization, deletion, move, or eject operation was performed.

## Next Step

Begin P0: capture the current code/state baseline, locate the provisional
identity gate, and establish a safe checkpoint strategy without including the
existing private/scratch artifacts.
