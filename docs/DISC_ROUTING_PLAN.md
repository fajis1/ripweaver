# Unified disc routing implementation plan

## Objective

Use one durable, revisable assessment of a disc's composition and each title's
role. User choices are hints that prioritize investigation, not assignments or
execution authority. Preserve the established TV evidence and coherence rules.

## Current phase

**Phase 2 in progress: preparation now carries the routing identity into the
immutable media context.**

Production routing remains unchanged until the handoff and worker phases pass
their integration tests. No live queue migration or media processing is part of
implementation validation.

## Invariants

- Bind every assessment to the exact inventory fingerprint and complete title
  index set. Store user hints separately from evidence-supported roles.
- Retain unknown and conflicting evidence explicitly. A failed provider request
  is not evidence of another content type.
- Classify individual titles as well as disc composition: movies, episodes,
  extras, and unresolved titles may coexist. Runtime and hints may prioritize
  searches but cannot establish an episode identity or final library name.
- Keep acquisition scope separate from downstream identification scope. Do not
  change MakeMKV commands, physical drive claims, batch validation, or recovery
  ordinals as part of routing work.
- Preserve TV confidence, independent-window, residual, range, and whole-disc
  coherence rules. Extras cannot enter episode counts or establish anchors.
- Append assessment revisions; require the caller's expected revision to prevent
  stale workers from overwriting newer decisions. Completed assignments are not
  silently revised, renamed, or reorganized.
- Persist only bounded, path-free codes and ordinal title indexes in routing
  records. Keep provider dialogue, paths, credentials, and raw responses out.
- External analysis still requires the configured authorization boundary. A
  route preference or queue admission never authorizes a new media operation.
- Preserve triage originals. No production backend launch, physical disc read,
  provider call, or live queue change during synthetic implementation tests.

## Phases and acceptance gates

### Phase 1 — Shared assessment and durable revision contract

- [x] Define disc composition, per-title evidence, user hint, and route priority.
- [x] Make evidence outrank a contradictory hint; no evidence remains unknown.
- [x] Reject conflicting title identities, malformed records, and unbounded data.
- [x] Persist exact-fingerprint, append-only revisions with stale-write refusal.
- [x] Test restart persistence, conflicts, unknowns, and representative disc types.

Deliverable: independently tested foundation; no new production routing yet.

### Phase 2 — Preparation and identification handoff

- [ ] Gather label, inventory, and database evidence through a single classifier.
- [ ] Bound metadata searches; retain unavailable/ambiguous outcomes explicitly.
- [ ] Preserve the user's original hint without treating it as a forced type.
- [ ] Persist the assessment and bind its revision/digest in the verified contract.
- [ ] Eliminate the lost `effective_content_hint` handoff and unknown-to-TV default
      for new assessments. Keep an explicit compatibility path for old contracts.
- [ ] Derive title routes and TV matching scope from the same assessment.
- [ ] Test preparation -> serialization -> restart -> identify with fake providers,
      including movie evidence contrary to a TV hint and the reverse.

### Phase 3 — Bounded alternate-route controller

- [ ] Record per-title route attempts and distinguish no-match from service failure.
- [ ] Route unresolved content to movie, TV, or descriptive extras analysis using
      evidence; an early missing TV catalogue must not block alternate analysis.
- [ ] Supply Gemini with the hint, evidence, and prior safe attempt summaries.
- [ ] Validate Gemini's proposal; preserve existing independent identity rules.
- [ ] Permit one attempt per route/evidence revision and prevent restart loops.
- [ ] Handle singleton episodes, multiple movies, ambiguous cuts, and mixed discs.
- [ ] Reconcile same-disc changes without revising completed assignments.

### Phase 4 — Worker and durable queue integration

- [ ] Replace the unfinished detached movie-classifier thread with bounded,
      tracked work compatible with queue pause, shutdown, and restart.
- [ ] Test movie/mixed/extras review transitions through the actual durable store.
- [ ] Ensure opted-in extras/movie analysis executes rather than just marking
      evidence as needed; keep provider and media permissions authoritative.
- [ ] Preserve TV coordinator readiness, exact-fingerprint scopes, and serialization.
- [ ] Record concise redacted errors and route changes in the audit.

### Phase 5 — Dashboard and reviewed legacy recovery

- [ ] Show hint, assessed composition, per-title roles, current route, and reasons.
- [ ] Explain contradictory evidence and exhausted routes without presenting
      model guesses as verified identity.
- [ ] Provide metadata-only reassessment for eligible unresolved legacy items.
- [ ] Keep live retry, evidence access, and placement behind existing confirmations.
- [ ] Read `FRONTEND_RECOVERY_GUIDE.md` before changing frontend source or builds.

### Phase 6 — Verification and handoff

- [ ] Synthetic matrix: ordinary TV, TV with extras, movie with extras, double
      feature, TV release with a movie, mixed seasons, unknown label, conflicting
      metadata, provider outage, wrong hint, restart, concurrent revision, and
      exhausted fallback.
- [ ] Run focused suites, then the full suite with coverage disabled to preserve
      the user's existing coverage file; check modified modules with Ruff.
- [ ] Update project status and this progress log after each completed milestone.
- [ ] Preview and push the approved recovery checkpoint after each milestone.
- [ ] Any live canary is a separate, exact-input authorization; passing tests is
      not permission to read or alter the user's discs or staged files.

## Progress log

### 2026-09-15 — Plan created; Phase 1 started

- Reviewed existing preparation, content policy, immutable context, identify
  adapter, and downstream worker interfaces.
- Confirmed the foundation must separate preferences from evidence and avoid
  silently activating the existing incomplete movie-worker branch.
- Earlier prerequisite repairs passed all 1,176 tests and were checkpointed as
  `57afa2cc3348` on `origin/wip/main`.
- Implementing the routing record and persistence contract before wiring any
  changed production behavior.

### 2026-09-15 — Phase 1 implementation and focused verification

- Added `disc/routing.py`: immutable, schema-versioned assessments, per-title
  evidence, preserved user hints, deterministic serialization/digests, and
  derived composition/search priorities. Unknown stays unknown; equal-strength
  contradictory evidence requires classification. Database/content evidence
  outranks structural inventory/label clues. These are routing proposals, never
  final episode or movie assignments.
- Added `disc/routing_store.py`: explicit-path SQLite persistence with immutable
  revisions, exact-retry idempotency, inventory continuity, digest validation,
  and transactional stale-write refusal. No production store or worker is
  constructed by this foundation.
- Added 31 routing tests, including two competing writers, restart/retry,
  tampered records, mixed movie/TV/extra roles, multiple movies, wrong hints,
  unknown evidence, and inventory substitution. With existing content-policy
  tests, all 54 focused tests pass; focused Ruff lint/format checks pass.
- Next: wire one assessment through preparation and the immutable identification
  handoff, with synthetic integration tests before changing live behavior.

### 2026-09-15 — Phase 2 handoff milestone

- Added routing identity fields to `MediaContext`: assessment digest, revision,
  and derived composition. Preparation now saves the effective inferred hint
  instead of recomputing and losing a movie result when constructing the
  downstream contract.
- Preparation creates a path-free assessment from the saved inventory and
  classifier roles. This is an initial proposal only; later content evidence can
  revise it. Existing MakeMKV acquisition selection and TV evidence policy are
  unchanged.
- Focused routing, preparation, adapter, and content-policy checks pass (72
  tests). The complete synthetic suite passes (1,206 tests). No provider,
  optical drive, or real media was accessed.
- The remaining Phase 2 work is to persist the assessment in the explicit
  routing store and make the identify adapter consume its revision before any
  route starts. That handoff is deliberately not enabled yet.
