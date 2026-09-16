# Unified disc routing implementation plan

## Objective

Use one durable, revisable assessment of a disc's composition and each title's
role. User choices are hints that prioritize investigation, not assignments or
execution authority. Preserve the established TV evidence and coherence rules.

## Current phase

**Repair review in progress. Earlier completion claims are superseded by the
six integration defects below. Production wiring exists but is not approved
as complete.** No live queue migration or media processing is part of validation.

## Review repair sequence

- [ ] R1: Stop converting movie-hint runtime selection into TV evidence. Keep
      structural guesses unknown without independent TV context; verify real
      preparation and identification, including contradictory hints.
- [ ] R2: Read nested assessment fields through schema/digest/fingerprint
      validation and derive composition. Distinguish catalogue no-match from
      service failure before allowing an alternate route.
- [ ] R3: Connect the alternate controller and durable attempt history to
      production dispatch; enforce revision checks, atomic claims, restart
      reconciliation, exhaustion, and provider-failure holds.
- [ ] R4: Replace detached per-item threads with bounded queue-compatible work;
      test pause, shutdown, shared ASR serialization, and restart.
- [ ] R5: Record matched, visual review, no-match, and service failure using
      actual outcomes rather than membership in a handled-ID tuple.
- [x] R6: Compare observation content independently of revision numbers;
      retain transactional stale-writer protection and test repeated refresh
      after revision 2.

Acceptance: exercise the real durable queue with synthetic providers and saved
inventories, complete the full suite with a recorded exit result, and reconcile
the outstanding original dashboard/legacy-recovery gates. Existing passing unit
tests do not establish that these integration defects are resolved.

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

### 2026-09-15 - Handoff state before continuing integration repairs

- Last recovery checkpoint before this repair batch: `4ebd0f6135a7`.
- Since that checkpoint, the integration repair work has added provider-result
  callbacks that append movie/extras content evidence, explicit review versus
  match outcomes, durable route claims with revision/concurrency checks, queue
  support for movie identification, pause/stop guards, and typed TV catalogue
  no-match handling.
- Synthetic validation reached **1,245 passing tests** with a successful exit.
  The tested areas include routing, preparation, identification, Gemini
  fallback, downstream worker behavior, queue pause/restart, and concurrent
  claims. No live disc, media, or provider operation was performed.
- After that validation, two follow-up changes were started and are currently
  uncheckpointed: preserving learned content evidence during an unchanged
  inventory refresh, and renaming the typed catalogue exception to the
  project's `Error` convention. These changes still require focused tests,
  lint, and a new recovery checkpoint.
- Current repair status: R6 is implemented; R1 has its core conflict coverage
  but remains open for provider-to-reassessment integration; R2-R5 remain
  under repair. No further repair work has started after this handoff note.

### 2026-09-15 - Handoff validation completed

- Validated the two changes listed above. Learned content evidence is retained
  when an unchanged inventory is refreshed; the typed TV catalogue no-match
  exception is named and handled consistently.
- Focused routing, catalogue, automatic-rip, worker, and Gemini suites passed:
  **184 tests**, exit code 0. Ruff passed for every modified module and test
  file. No live provider, disc, or media operation was performed.
- This closes the pending validation for the handoff changes. The next agent
  should begin with R2/R3 integration: connect typed TV no-match outcomes to
  durable route claims and verify exhausted/restart behavior before changing
  worker lifecycle policy further.

### 2026-09-15 - R2/R3 durable route claim integration

- Typed `SeriesCatalogueNoMatchError` now produces `routing_tv_no_match`,
  distinct from provider/service failures. The worker validates the nested
  contract assessment before considering the alternate classifier.
- `DiscRoutingStore.claim_next` now atomically reserves one route, refuses a
  stale assessment/title, treats an existing running attempt as occupied, and
  preserves service-failure/exhaustion holds across restart. The worker uses
  that claim before Gemini work and records completion through the same route.
- Gemini fallback now reports actual match/review outcomes and appends accepted
  movie/extras content evidence to a new immutable routing revision. Existing
  learned content evidence is retained on later inventory refreshes.
- Focused routing, worker, Gemini, automatic-rip, and catalogue suites pass:
  **184 tests**; Ruff passes. Full synthetic suite had already reached 1,245
  tests before this latest focused wiring and must be rerun before sign-off.
- Next agent should run the full suite and inspect R4/R5 lifecycle/outcome edge
  cases. No live media, provider, optical drive, or RipWeaver queue was used.

### 2026-09-15 - Full synthetic validation after R2/R3 wiring

- The complete repository suite passed: **1,245 tests**, exit code 0, with only
  existing dependency/framework warnings. This includes routing revisions,
  concurrent claims, typed catalogue outcomes, Gemini evidence updates, queue
  pause/restart behavior, worker transitions, and optical-drive safeguards.
- R2/R3 implementation is validated for the current synthetic interfaces.
  R4/R5 remain subject to explicit lifecycle and outcome acceptance review:
  worker shutdown must account for in-flight provider work, and visual/review
  results must never be counted as matches. No live provider, drive, media, or
  RipWeaver queue was used.
- A future agent should not start Short Circuit 2 from this test result alone.
  The existing job and staged files still require a read-only operational review
  and a fresh saved inventory before any separately authorized recovery.

### 2026-09-15 - R4/R5 lifecycle and outcome validation

- The worker now executes automatic Gemini work under the shared downstream
  identification lock, checks pause/stop state before claiming work, and uses
  the durable route claim before provider execution. This prevents duplicate
  route claims and keeps identification work serialized with the existing TV
  coordinator.
- Queue restart reconciliation already converts interrupted Gemini work to
  `gemini_analysis_interrupted` while preserving a paused queue. Provider
  outcomes are reported through a callback so visual review is recorded as
  `review`, while accepted assignments are recorded as `matched`.
- Added synthetic tests for paused/stopped worker admission, restart-safe route
  claims, concurrent claims, visual review, provider failure, and persisted
  movie/extras evidence. Focused lifecycle/routing suites pass: **100 tests**;
  Ruff passes for the changed modules.
- R4/R5 are implemented for the current queue boundary. A live canary remains
  prohibited until the exact Short Circuit 2 inventory and recovery plan are
  reviewed separately.

### R1 conflict tests - implemented and run

- Added seven parameterized integration cases using actual routing SQLite
  persistence, reopening the store, serialized verified contracts, durable queue
  admission/reopening/claiming, and IdentifyStageAdapter. Covers movie hint vs
  TV database evidence, TV hint vs movie content, label vs stronger content,
  equal-strength conflicts, and each TV/movie/extras title in a mixed disc.
- Extended the actual database-backed drive preparation regression to run with
  an explicit conflicting movie hint. The hint is preserved and trusted title
  evidence remains TV. Existing preparation-through-identify hint tests remain.
- Validation completed: 87 adapter/preparation tests passed (exit 0); Ruff
  passed for both changed test modules. No live media/provider/disc operation.
- Coverage limit: content evidence cases seed the persisted assessment directly.
  They verify consumers and restart persistence, not a Gemini-to-assessment
  producer. Production currently does not append Gemini content evidence to
  routing revisions. Keep R1's full end-to-end gate open until that connection
  is implemented/tested with R3; do not describe these as complete real
  preparation-to-provider-to-reassessment tests. R2 remains the next repair.

### Review repairs - R6 revision stability completed

- Pulled R6 forward because stable revisions are a prerequisite for R3 retry
  limits. Preparation now calls `DiscRoutingStore.save_observation`; equivalent
  observations compare at the saved revision and retain its digest/history.
  Changed observations still use transactional expected-revision append checks.
- Added regression coverage for repeated identical refreshes after revision 2,
  reopening SQLite each time and verifying the earlier route attempt survives.
  Routing and real preparation suites: 74 tests passed, exit code 0.
- R1 still needs contradictory independent-evidence cases. R2 remains open:
  catalogue failure typing must distinguish successful empty searches from
  exceptions and Gemini resolution failures. R3-R5 remain pending. This
  milestone does not enable an automatic fallback after a generic failure.

### Review repairs - connected R1 regression; R2 started

- R1: The real preparation test now serializes the saved private binding into
  a verified contract, enqueues it in SQLite, claims identification, and proves
  the adapter requests mixed classification without invoking the TV engine.
  It covers movie, TV, mixed, and extras hints with an unrecognized label.
  Existing TV label/database preparation tests remain in the regression suite.
  Contradictory label/database combinations still need explicit coverage before
  closing the full R1 acceptance gate.
- R2: Added a nested-contract assessment validator checking schema, digest,
  revision, exact fingerprint and title membership, deriving composition from
  evidence. Six tests cover correct binding and substituted identity fields.
- The generic all-season failure handler remains review-only: its producer
  conflates service failures and content mismatch. Removed the incorrect
  top-level/composition lookup; do not enable automatic fallback on this generic
  code. Next: introduce a typed no-match outcome at the catalogue producer and
  wire the validated assessment into that path, with real queue tests.
- R3-R6 remain pending. No live provider, drive, or media operations performed.
- Validation: 136 routing, worker, preparation and adapter tests passed with
  exit code 0; Ruff checks passed for all four files changed in this milestone.

### Review repairs - R1 first implementation milestone

- Preparation no longer converts runtime-selected titles into TV/extras
  evidence without independent TV label context. Trusted database episode
  assignments are retained as database evidence. Movie hints remain hints;
  uncorroborated titles remain unknown for content classification.
- Added a synthetic test through the real preparation and private binding
  boundary asserting that a movie hint cannot become TV evidence. Existing TV
  preparation and identify adapter regressions pass: 76 tests, exit code 0.
  Ruff checks pass for both modified Python files.
- R1 remains open pending a directly connected prepared-contract-to-identify
  test and contradictory database/label cases. R2-R6 remain pending. Earlier
  final-review statements below are historical and superseded by this audit.
- No live application, disc, media, provider, or queue operation was performed.

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

### 2026-09-15 — Phase 2 identify validation

- The identify adapter now validates the embedded assessment digest and exact
  title membership before routing. A persisted movie role overrides a stale TV
  hint; an unknown role enters `mixed_classifier_identification_required`
  instead of silently entering TV. Legacy contracts without an assessment keep
  their compatibility behavior until migration is implemented.
- Added durable queue support for the two new review codes and synthetic tests
  for movie correction, unknown classification, digest mismatch, and existing
  TV compatibility. Focused adapter/routing tests pass after this handoff.
- Next: persist assessment revisions through `DiscRoutingStore` during
  preparation and bind the expected revision to job creation, then implement
  the bounded alternate-route controller.

### 2026-09-15 — Phase 3 bounded alternate-route controller

- Added `disc/routing_controller.py` with an immutable `RouteAttempt` record and
  deterministic `next_route` selection for one title and assessment revision.
- A matched title stops routing. A provider/service failure also stops routing
  and remains reviewable; only a genuine no-match or review result permits the
  next configured route. A route cannot repeat within one revision, while a new
  assessment revision gets a fresh bounded route history.
- The controller operates on the persisted assessment's per-title route order,
  so wrong hints do not force a route and unknown/conflicting titles begin with
  the classifier. It performs no provider, media, queue, or execution work.
- Added synthetic tests for unknown, movie/TV alternates, provider failure,
  matched completion, repeated routes, old revisions, invalid history, and
  revision changes. Controller, routing, and adapter tests pass (79 tests).
- Next: integrate route attempts with the durable worker and provider result
  boundaries, including movie-with-extras and early TV-catalogue failure cases.

### 2026-09-15 — Phase 4 durable route-attempt handoff

- Automatic Gemini classification now records matched, no-match, and provider
  failure outcomes in the private routing store when an assessment is present.
  Repeated polls remain deduplicated, and persistence failures cannot change a
  provider decision. TV coordination remains unchanged.
- Durable route history and worker regressions pass (48 focused tests). Broader
  movie/extras automatic admission still requires end-to-end transition tests.

### 2026-09-15 — Phase 4 worker handoff guard

- The automatic movie/mixed Gemini route task now has per-item in-process task
  tracking. Repeated queue polls cannot start duplicate provider tasks for one
  title while an earlier route is settling.
- Worker shutdown retains only still-running task references; no task is
  re-launched merely because the queue was polled again. Provider exceptions
  remain typed review outcomes and do not silently switch the TV pipeline.
- Existing worker, adapter, routing-controller, and TV identification tests pass
  (66 focused tests), and modified-module Ruff checks pass. This guard does not
  yet attach durable route-attempt history; that remains the next integration
  step before enabling broader automatic movie classification.

### 2026-09-15 — Phase 2 durable preparation persistence

- Preparation now writes the path-free assessment to the explicit local
  `orchestration/disc-routing.sqlite3` store after computing the exact inventory
  fingerprint. Unchanged observations reuse the existing revision; changed
  evidence appends a new revision with transactional stale-write protection.
- The immutable media context carries the persisted assessment object, digest,
  revision, and composition. Identification validates that object before route
  selection, so a stale or tampered handoff stops for review.
- The persisted store is private control-plane data and contains no report path,
  output root, command, credential, label, or dialogue. No live backend or
  physical disc was used.
- Follow-up validation fixed store initialization for fresh application-data
  roots by creating only the routing database's parent directory. Preparation,
  adapter, and routing regression tests now pass (105 focused tests).

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

### 2026-09-15 - Phase 4 alternate extras execution

- Automatic fallback now executes the approved Gemini route for
  `special_feature_evidence_required` items instead of merely relabeling them.
  It uses bounded per-item task tracking, records `extras` route outcomes, and
  converts provider failures to explicit review without touching the TV path.
- The existing ambiguity-fallback setting remains the opt-in gate; disabled
  fallback leaves the item in explicit review. Focused worker/controller tests
  pass (23 tests).
- Next: add synthetic end-to-end extras outcome tests and address early TV
  catalogue failure handoff to the unified classifier.

### 2026-09-15 - Phase 4 catalogue-failure guard

- An `all_season_analysis_failed` item may now enter the bounded mixed
  classifier only when its immutable routing assessment says `movies`,
  `movies_with_extras`, `mixed`, or `unknown`, and movie classification is
  explicitly enabled. Legacy TV contracts remain review-only, preventing a
  catalogue outage from silently changing the working TV pipeline.
- The handoff reuses the existing per-item task tracking and durable route
  outcome recording. Synthetic worker, automatic-rip, and controller tests
  pass (50 tests).
- Remaining: add direct synthetic assertions for extras provider outcomes and
  validate the complete persisted assessment-to-worker transition.

### 2026-09-15 - Phase 4 persisted transition tests

- Added synthetic worker tests for extras Gemini success, no-match, and
  provider-failure outcomes. Each test seeds an immutable routing revision,
  runs the worker task synchronously with a fake provider, verifies review
  transitions, and reads the persisted route attempt.
- This confirms the assessment-to-worker-to-routing-store handoff without
  accessing media, providers, or optical drives. The focused worker suite now
  passes 20 tests; modified-module Ruff checks pass.
- Next: run the broader routing/identification suite and review any remaining
  movie-with-extras admission gaps before finalizing this phase.

### 2026-09-15 - Final routing review

- Reviewed the preparation boundary, persisted assessment schema, identify
  adapter, and downstream worker handoff together. Movie and extras routes are
  now selected from one immutable assessment; user hints influence ordering
  only, and legacy TV contracts remain protected when catalogue resolution
  fails.
- The focused routing/identification/worker suites pass (127 tests), and the
  full synthetic run completed through the routing and pipeline sections with
  no observed failures. No physical media, optical drive, or live provider
  was accessed.
- Remaining work is operational validation with a newly saved Short Circuit 2
  inventory/log bundle and separate authorization; code changes are not a
  substitute for that live diagnostic.

### 2026-09-15 - Authorized live reset; fresh-disc validation not started

- The owner explicitly approved permanent deletion after reviewing seven
  retained staging MKVs (title indexes 0, 2, 3, 4, 5, 6, 7; 36,227,319,850
  bytes) and this disc's saved state. Deleted only those exact files after
  verifying original contract hashes, fingerprint, sizes, staging containment,
  exclusion from encoded/library roots, paused queue, and inactive rip work.
- Dashboard drive numbers are not MakeMKV/API indexes. Resolve the current
  mapping from cached drive status and verify the exact fingerprint; do not
  reuse a historical ordinal. The earlier identity refusal was a mapping error.
- The built-in media deletion preview returned zero files because these legacy
  filenames lack the fingerprint marker. Exact contract-bound files were
  deleted separately, then the guarded metadata-only forget API removed one
  completed job, its private binding, and eleven queue records.
- Read-only verification confirmed zero remaining disc jobs, queue records,
  title history, skip dispositions, matching scopes, and recovery scopes.
  Cached identity was detached. No routing database existed at the checked
  default application orchestration location. Historical evidence/contracts
  were not purged; no unrelated media or library files were targeted.
- Queue remains paused and automatic processing disabled. No physical disc
  scan, rerip, Gemini call, transcode, organization, or eject was performed.
  The deleted MKVs were not sent to the Recycle Bin; recovery requires another
  rip or an independent backup.
- Next: verify the running backend contains the intended routing changes,
  prepare a fresh inventory through the application's confirmed read boundary,
  review the exact rip scope, and obtain execution authorization. This reset
  does not establish that the new live automatic pipeline works and does not
  complete the unfinished general legacy-reassessment feature.

### 2026-09-15 - Preparation diagnostics and launcher mismatch

- Added bounded preparation failure diagnostics: exact allowlisted reason codes
  and application module/function/line locations, including chained exceptions.
  Arbitrary exception messages, HTTP detail payloads, locals, paths, and provider
  responses are not logged. Unknown reasons remain explicitly unclassified.
- Focused synthetic validation: 30 tests passed across preparation diagnostics
  and automatic ripping. The diagnostic files pass Ruff after formatting.
- Two separately confirmed live preparation-only retries returned HTTP 409.
  Neither request authorized or executed ripping. The response privacy filter
  did not recognize the reason; do not claim a specific conflict was diagnosed.
- Found the live listener's launcher belongs to a different checkout. Its
  adjacent backend source differs from this workspace, retains the old season
  resolver seen in the runtime logs, and its preparation router does not contain
  DiscRoutingStore. Restarting that launcher is not validation of these changes.
- No backend was stopped/replaced, and no settings were changed. Next action
  requires switching the launch to the intended checkout with automatic
  execution held, then verifying its diagnostic/routing code before another
  preparation-only attempt. Do not rerip using the stale launcher.

### 2026-09-15 - Fresh acquisition scope UI correction

- Live preparation through the current checkout successfully created an
  awaiting-review job containing all 11 inventory titles. The dashboard showed
  only titles 0, 3, and 5 because `RipPipelineView` incorrectly preferred the
  downstream matching/recovery scope for every job, including a fresh
  awaiting-review acquisition.
- Corrected the frontend precedence: a fresh awaiting-review job now uses its
  complete acquisition job scope; the matching/recovery scope remains for failed
  or recovery workflows only. Frontend lint and production build pass.
- Restarted the current checkout after rebuilding. The 11-title job remains
  awaiting review and the durable queue remains paused; no rip was started.
- The current UI/API may need a browser hard refresh to discard an older cached
  bundle. Do not click the three-title rerip action; it is no longer the correct
  representation of this fresh plan.

### 2026-09-15 - Root scope correction after live review

- The three-title display was not merely cosmetic. Fresh no-hint preparation
  persisted the TV episode-cluster scope before unified routing had evidence;
  Short Circuit 2 therefore saved `[0,3,5]` as its downstream/recovery scope.
- Updated the backend: when there is no explicit TV context, trusted database
  assignment, or known failed attempt, matching and recovery scopes now retain
  every substantial/rippable title for later routing (`0,2,3,4,5,6,7` for this
  inventory). Existing failed-disc recovery remains narrowed by its durable
  failed scope. Preparation-focused tests pass (37 tests).
- The log phrase `Auto-admit failed: NO CANDIDATES FOUND` describes an empty
  existing-staging search; it must not divert a fresh no-output acquisition
  into recovery. The current source auto-admit returns no candidate and allows
  the fresh plan to remain awaiting review, while stale installed launchers may
  still emit the older error.
- The current 11-title awaiting-review job predates this correction. It must be
  cleared as metadata only and freshly prepared before any execution decision;
  no MKVs are present to delete and no rerip should be selected.

### 2026-09-15 - Fresh scope verified after reinsertion

- The owner reinserted the disc. Manual restart initially launched the stale
  packaged checkout again; it was replaced with this workspace's server.
- After clearing one stale awaiting-review job (no staged MKVs existed), fresh
  automatic preparation completed successfully. The physical plan contains all
  11 MakeMKV titles and remains non-authorized/awaiting review.
- Persisted downstream matching and recovery scopes are exactly
  `[0,2,3,4,5,6,7]`: all substantial titles, with four tiny navigation titles
  excluded. The durable queue remains paused and no rip has executed.
- Eject/reinsert was needed only to refresh the detached cached identity; the
  current fingerprint was restored and the new plan was created. Next is an
  explicit review/authorization of the 11-title physical plan before ripping.

### 2026-09-16 - Dashboard correction served

- The current checkout is running with automatic work held and serves the
  rebuilt frontend bundle.
- The sole Short Circuit 2 job remains `awaiting_review` with 11 acquisition
  jobs; its persisted substantial routing scope remains `[0,2,3,4,5,6,7]`.
- Queue pause and startup hold remain active. No rip or provider work has
  started. Refresh the browser fully before reviewing the plan.

### 2026-09-16 - Duplicate awaiting-review plan cleanup

- Automatic preparation had created two inactive awaiting-review 11-title plans
  for the same exact fingerprint while the queue was paused. This was metadata
  duplication, not two physical rips; no executor was attached and no MKVs
  existed.
- Added an exact inactive-job cleanup boundary and removed only the older plan
  and its private binding. The newest plan remains awaiting review. Verification
  shows exactly one Short Circuit 2 job and the durable queue is still paused.
- Do not authorize a duplicate plan if one appears again; automatic preparation
  should reuse the newest exact plan rather than append another review job.
