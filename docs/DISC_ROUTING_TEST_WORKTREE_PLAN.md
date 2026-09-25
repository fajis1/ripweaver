# Disc routing repair — active test worktree

> STOP: This plan applies only to the `ripweaver-test` source worktree launched by
> the desktop test shortcut. The installed EXE and the separate
> `mkv-episode-matcher` checkout are not this running test build. Confirm the
> resolved repository root and branch before editing or reporting a result.

## Status (2026-09-17)

> **September 22 continuation:** The live Short Circuit 2 trail now continues in
> `docs/MOVIE_EXTRAS_IDENTITY_PLAN.md`. Read the two September 22 progress entries
> at the end of this file for the observed queue-control repair and live matching
> outcome, then follow that newer plan. M0-M6 and the original M7 canary describe
> how this point was reached; they are not the current implementation backlog.
> The remaining problem is the coupling between an accepted movie/extra role and
> an exact final identity, not another disc scan or rerip.

Milestones M0-M6: **complete in synthetic validation; M7 pending**. Preparation
and identify now consume the queue-owned routing assessment, and the automatic
alternate-route worker is successfully routing failures, but no fresh Short Circuit 2 live rip has
been validated. Existing uncommitted test-worktree changes belong to the user
and must be preserved. The earlier routing plan in another checkout remains a
design/reference only; its completion and test claims do not apply here.

## Current agent handoff (2026-09-17, M6 complete, M7 pending)

- **Source and recovery:** work only in the `ripweaver-test` worktree on
  `codex/windows-drive-provisional-fallback`. The last approved `wip/test`
  recovery checkpoint before this resumed turn was `2a8ceab36ece`; the latest
  pushed recovery state is always `origin/wip/test` (resolve its hash when
  resuming). The checkout intentionally remains
  dirty and contains older user/agent work. Do not switch to the installed EXE
  or the separate `mkv-episode-matcher` checkout, reset the tree, or remove
  `.coverage`, staging files, or unrelated changes.
- **Completed:** M0–M3 passed synthetic gates. M4 has a saved-data route policy,
  revision-bound atomic claims with optional exact held-item review-code check,
  typed Gemini reports, terminal TV coordinator settlement, and startup
  interruption reconciliation. The latest full pytest run and modified-file
  Ruff checks passed. No physical disc, media, or live provider was accessed.
- **Current agent handoff (2026-09-17, M6 complete, M7 pending):**
  - **Source and recovery:** work only in the `ripweaver-test` worktree on `Codex/windows-drive-provisional-fallback`.
  - **M4 Completion:** A *true* TV title-level no-match followed by a viable movie search is now successfully produced by the TV coordinator and correctly survives the automatic rip boundary to trigger the movie alternate route. The full end-to-end handoff has been proven with a combined coordinator -> automatic handler -> routing-worker synthetic test (`test_m5_end_to_end_tv_no_match_to_movie_route`). All Ruff checks and synthetic tests pass perfectly.
  - **M5 Completion (2026-09-17):**
    - Accepted Gemini content roles are successfully appended as a new durable assessment revision within the worker.
    - Sibling-revision races have true end-to-end coverage. The settle/append race was resolved by appending the evidence in a bounded (10 retries) retry loop. The update is now fully idempotent: if a sibling appends the exact same role, the loop breaks immediately and avoids a duplicate-evidence `ValueError`. A local `settled` flag ensures a matched route is never erroneously resettled as `review` in the outer exception handler. The synthetic tests explicitly test this sibling revision occurring between those operations.
    - The manual Gemini endpoint (`execute_pipeline_gemini_fallback`) and Unmatched Disc Analysis are now serialized globally. The queue transition (`choose_review_path`) was moved **inside** the thread and under the `_downstream_lock`. This prevents any queue state race where a worker thread might claim an item before the manual thread actually acquires the lock. The HTTP routes check the state early for responsive UX, and the background thread re-checks the transition safely. Synthetic concurrency API tests exist for this logic.
    - A descriptive `tv_episode` suggestion correctly falls through to `review`, and `all_season_series_not_found` explicitly settles as `review` via the TV coordinator mapping (`_TV_TERMINAL_REVIEW_OUTCOMES`), preventing unintended negative TV identities.
    - Worker ruff import-order errors have been fixed. The full synthetic test suite (1263 tests) and ruff lints pass!
    - **Update (Later on 2026-09-17):** Discovered and fixed race conditions in the manual endpoints queue transition test (	est_manual_endpoints_queue_transition_lock) and 	est_gemini_retry_starts_only_the_exact_requested_item. The mock thread behaviors were corrected to allow deterministic queue transition assertions without executing unmocked functions (like loading the ASR model) or escaping the monkeypatch context. The full suite passes reliably.

- **Live boundary:** M7 remains separately unapproved. Do not start RipWeaver,
  read the Short Circuit 2 disc/MKVs, call Gemini, rip, transcode, eject, or
  alter library media during these synthetic gates.

## Findings verified in this worktree

- The active branch has no `disc/routing.py`, `disc/routing_store.py`, or
  `disc/routing_controller.py`, and no associated routing tests. The small
  `backend/identification_router.py` orders nominal branches from a hint, but
  no production caller was found for it.
- Preparation in `backend/routers/rip.py` passes `effective_content_hint` into
  title selection; explicit TV label context can set that value to `tv`. A
  separately chosen `content_hint` is stored in `MediaContext`, and downstream
  TV analysis skips contexts marked `movie`, `extras`, or `mixed`. Thus a hint
  still affects execution scope and eligibility, not merely search priority.
- `disc/title_selector.py` uses an episode-only selection for `tv`, bonus-only
  selection for `extras`, and their union otherwise. Fresh whole-inventory
  acquisition is a separate later step. These layers must be tested together
  before changing title selection.
- On a fresh disc with no staged candidates, `_auto_admit_staged_disc_if_complete`
  previously raised `NO CANDIDATES FOUND` instead of declining auto-admission.
  That prevented ordinary preparation and included a private output path in
  the error. A no-staging regression test now covers the repaired behavior.
- `pipeline_adapters.py` still chooses the first identify strategy from
  `content_hint`; a movie/extras-first route immediately enters review unless
  a separate reviewed assignment exists. This is not a unified fallback.
- The configuration schema has separate automatic processing, auto-eject, and
  automatic Gemini fallback flags. This audit has not read `.env` or printed
  the owner's local configuration values. The branch also contains unfinished
  triage/recovery edits; routing work must not overwrite them.
- The verified-rip contract carries `media_context`, fingerprint, title index,
  and expected title indexes. There is no assessment revision or digest in that
  contract today. The queue separately persists fingerprint-bound matching and
  recovery scopes, dispositions, and per-item review states.
- The current worker runs disc-level TV analysis and the newer automatic
  non-disc triage recovery under its downstream lock. Its extras fallback only
  changes a review code; it does not execute movie/extra classification. The
  existing Gemini fallback can classify descriptive movie/extras evidence when
  invoked, but its tuple of handled IDs is not a durable per-route outcome.

## Earlier-checkout compatibility verdict

| Earlier work | Verdict for this test branch | Reason / required adaptation |
| --- | --- | --- |
| Path-free fingerprint-bound assessment, explicit unknown/conflicts, hint separate from evidence, digest/revision checks (R1/R2/R6) | **Keep the design; adapt implementation** | No equivalent persisted assessment exists here. Reuse the invariants and tests, but bind to this branch's actual `MediaContext`, queue contract, and exact matching/recovery scopes. Do not let runtime or hint become TV evidence. |
| Separate acquisition and downstream title scope | **Already present; preserve** | Fresh preparation acquires the full zero-minimum inventory; the queue stores classifier-derived matching/recovery scope. Rework downstream scope for mixed discs without changing MakeMKV acquisition or failed-disc recovery rules. |
| Old checkout's preparation/worker/adapter file edits | **Do not copy** | These files have extensive uncommitted triage/season-recovery work here. The old worker's post-item path omits this branch's automatic triage call. Merge behavior deliberately with targeted tests. |
| Durable route claims and per-route outcomes (R3/R5) | **Needed, but redesign integration** | The current queue has review states but no revision-bound route attempt record. Old claim/attempt logic is useful as a starting point; outcome classification must come from actual provider/identity results, not a handled-ID tuple. Restart and stale-claim behavior need end-to-end tests. |
| Bounded Gemini/movie/extras work (R4) | **Needed, but preserve worker discipline** | Current extras automation only changes a review code. Any new work must share pause/stop and ASR serialization, avoid detached tasks, and retain the triage and TV paths already operating in this branch. |
| Dashboard and legacy repair UI | **Defer until backend contracts are stable** | The old routing dashboard is not deployed here. Read `FRONTEND_RECOVERY_GUIDE.md` before any frontend edits and preserve existing recovery UI. |
| Old synthetic test results and live claims | **Do not carry over** | They ran against another checkout. Repeat relevant tests here; a new exact-input live canary is still separate authorization. |

The older six repair themes remain relevant as acceptance questions, not as
completed work or files ready to transplant. In particular, Short Circuit 2
needs a movie-with-extras assessment that can retain short extras for review
without sending them into TV episode matching. A user TV/movie/extras choice
may order investigation but must not force a content role or skip other routes.

## Proposed decision contract

One fingerprint-bound assessment represents the **whole disc composition** and
each inventory title's **provisional role** (`tv`, `movie`, `extra`, `unknown`,
or `conflicting`). It stores the complete sorted title-index set, original user
hint, path-free evidence source/status, monotonic revision, schema version, and
digest. It is a routing proposal, never a verified episode/movie identity or
permission to read, rip, contact Gemini, transcode, eject, or organize media.

Use the existing `PipelineQueueStore` database as the authoritative routing
control plane, with append-only assessment revisions and per-title route
attempts. A separate routing database would make the assessment, queue item,
and route claim non-atomic. Reuse the earlier checkout's *validation ideas*,
not its files or separate-store integration. Any change to queue schema must
have a migration test against an older database and preserve existing records.

Evidence hierarchy is explicit: independently validated content identity or
trusted title-specific database match outranks structural label/inventory
clues; user hints only order searches. A runtime cluster is not TV evidence.
Unavailable provider results add no type evidence. Equal-strength conflicting
claims stay conflicting and require a bounded classifier or review. Assessment
revision changes must compare evidence content, not just revision numbers;
stale writers are rejected. Successful sibling assignments may add evidence
but cannot silently rename or reroute completed titles.

Acquisition and identification stay separate: fresh zero-minimum whole-disc
MakeMKV selection remains unchanged, as do failed-disc recovery ordinals and
durable skips. The assessment determines only which preserved titles can enter
TV episode matching versus movie/extras classification or review. Unknown
substantial titles must not disappear because they are outside the TV cohort;
tiny/menu titles remain preserved and reviewable under existing policy, never
automatically treated as episodes or deleted.

## Implementation milestones and acceptance gates

### M0 — Test-worktree baseline (complete)

- [x] Verify launcher/worktree, dirty branch, configuration **schema**, current
  preparation, contract, worker, Gemini, TV, and triage boundaries.
- [x] Record keep/adapt/do-not-copy verdict above. Do not read `.env` or copy
  the other checkout's claimed completion state.

### M1 — Fresh preparation prerequisite (complete, synthetic only)

- [x] Empty or incomplete staged candidates decline auto-admission without
  path-bearing errors. Retain complete-staging recovery.
- [x] Focused preparation tests, full pytest suite, and modified-Python Ruff
  checks passed in this worktree. No live scan was performed for this gate.

### M2 — Assessment model, queue persistence, and migration (complete, synthetic only)

- [x] Specify strict path-free schema, source reliability, role/composition
  derivation, digests, bounds, and duplicate/conflict validation.
- [x] Add queue-owned assessment revisions and route-attempt tables with
  transactional compare-and-swap, idempotent retries, and exact-fingerprint
  forget cleanup; leave existing scopes and dispositions intact.
- [x] Define a safe migration for pre-routing queue databases and read-only
  legacy contracts. Never infer an old item's content role from its hint alone.
- [x] Test TV, movie-with-extras, mixed disc, unknown, conflicting metadata,
  changed inventory, concurrent writes, revision >2, restart, and forgetting.
  Gate: no provider, media, or physical-disc access is needed.

### M3 — Preparation and immutable-contract handoff (complete, synthetic only)

- [x] Feed inventory, explicit label structure, and trusted title-specific
  database outcomes into one assessment builder. Record unavailable/ambiguous
  lookups as such; do not turn a movie hint or dominant runtime into TV fact.
- [x] Persist the assessment during preparation, then carry exact revision and
  digest through `MediaContext` into each verified-rip contract. Validate the
  contract's fingerprint, title index, digest, schema, and saved revision when
  identify/worker reads it. Old contracts remain on explicit compatibility
  review paths, not an implicit unknown-to-TV default.
- [x] Derive downstream per-title scope from that assessment without changing
  fresh whole-disc acquisition or failed-disc recovery. Keep extras out of TV
  anchors/range/coherence counts; preserve relevant unknown content for an
  alternate route. Verify both contradictory hint directions.
- [x] Synthetic preparation-to-contract-to-restart-to-identify tests must use
  saved inventories and fake lookups, including an 11-title movie-plus-extras
  shape. Gate: no title is silently discarded or promoted to TV by hint.

### M4 — Bounded alternate-route policy and real outcomes

- [x] Route per title using assessed evidence, with hints changing **priority
  only**. Record `matched`, `no_match`, `review`, `service_failed`, and
  `interrupted` distinctly. Provider failure holds; it is not evidence to
  switch type. A genuine no-match may advance to a different eligible route.
- [x] Claim at most one route per title/evidence revision atomically with queue
  state. Settle each claim from an actual result; reconcile interrupted claims
  on restart and prevent same-evidence retry loops. Bound attempts/exhaustion.
- [x] Gemini receives only permitted, bounded evidence and prior safe attempt
  summaries. Its classification proposal cannot bypass the existing TV
  independent-window, residual, range, and whole-disc coherence gates, or
  automatically place a provisional movie/extras identity into the library.
- [x] Test a failed TV catalogue followed by a viable movie search, a true TV
  no-match, Gemini no-match, visual review, provider outage, conflicting
  metadata, multiple movies, and a movie with extras. Distinguish an absent
  catalogue from an unavailable service.

### M5 — Worker, Gemini, and existing-TV/triage integration

- [x] Replace the review-code-only extras fallback with bounded, tracked work
  on the current downstream worker. Preserve pause/stop, its shared ASR lock,
  serialized identify stage, and no detached per-item thread.
- [x] Keep `_apply_automatic_triage_analysis()` and disc-level TV analysis in
  both idle and post-item paths. Preserve triage originals and the established
  TV evidence/coherence rules. Integrate Gemini accepted content roles into
  the same durable assessment **after** actual result validation.
- [x] End-to-end fake-provider/queue tests cover matched, no-match, review,
  service failure, pause, shutdown, concurrent claim, restart, and sibling
  evidence revisions. Gate: existing TV and triage suites still pass.

### M6 — Visibility, legacy review, and broad synthetic verification

- [x] Display the user's hint separately from assessed composition, per-title
  role, current route, evidence status, and exhausted/held reason. Do not show
  a model guess as verified identity. Read `FRONTEND_RECOVERY_GUIDE.md` fully
  before frontend source or build edits.
- [x] Offer a metadata-only reassessment for eligible legacy unresolved discs;
  keep live retries, provider/media reads, and final placement behind existing
  authorization. Do not silently alter completed assignments.
- [x] Run focused tests first, then full pytest with coverage disabled, Ruff
  on modified modules, frontend checks if touched, and a synthetic matrix for
  ordinary TV, TV+extras, movie+extras, double feature, mixed TV/movie, wrong
  hints, unknown labels, ties, provider outage, restart, and recovery.

### M7 — Separately authorized live canary (not yet approved)

- [ ] Before requesting a live Short Circuit 2 test, present the exact disc
  identity/fingerprint, drive, title set, plan digest, output/run roots, tool
  paths, timeout, provider/media operations, and whether eject/auto-processing
  is enabled. Confirm no competing MakeMKV child and obtain **separate exact
  authorization** for each media-changing or provider operation required.
- [ ] Capture path-redacted preparation, assessment revisions, route attempts,
  and per-title outcomes. Success means the feature film and extras are
  considered without TV episode misrouting; no relevant title disappears,
  rerips are not requested without evidence, and existing TV behavior remains
  intact. A synthetic pass or this plan alone does not authorize the canary.

After each completed milestone, update this status/progress log, run its gate,
preview then push the approved `wip/test` checkpoint. Never mark a milestone
complete solely because another checkout passed tests.

## Progress log

- 2026-09-19: Diagnosed the sidebar's "Failed to load resources" report in the
  active test build. `/system/status` returned the valid `idle` state while
  automatic processing was held, and `/system/health` returned 200/ready.
  The sidebar now describes idle ASR as available on demand, and a manual
  library scan may initialize it. Rebuilt the frontend; TypeScript/Vite and
  modified-file ESLint pass. Repository-wide ESLint still reports six existing
  findings in MediaTriageView and RipPipelineView. No disc or media operation
  occurred; M7 remains pending.
- 2026-09-19: Fixed the system-health tool discovery failure caused by an
  unavailable `F:\\downloads` probe. Health/support checks now pass configured
  executable paths directly and do not scan portable download roots when those
  paths are supplied; disconnected roots are also skipped safely. Added two
  regression tests. Focused system-health tests (5) and Ruff checks pass. No
  disc or media operation occurred.
- 2026-09-18: Diagnosed the live test-worktree "Media context structure is
  invalid" failure. Existing private execution bindings contained the legacy
  derived `routing_composition` field, which is not part of `MediaContext` and
  caused restart decoding to fail. The parser now ignores that derived field;
  new serialization never writes it. Added manifest and private-binding
  regression tests (23 focused tests pass; no disc or provider access). M7
  remains pending and no live retry is authorized.
- 2026-09-18: With explicit user authorization, cleared only the exact
  Short Circuit 2 fingerprint `6f9afc599cf192c8` from orchestration,
  private-binding, routing, recovery-scope, and queue metadata. Two inactive
  jobs and two private bindings were removed; no staged MKVs were found or
  deleted, and unrelated records were preserved. Verification shows no jobs,
  scopes, routing revision, or route attempts remain for the fingerprint.
  M7 still requires a fresh read-only inventory and separate exact rip
  authorization.
- 2026-09-19: Diagnosed the repeated "rerip 7 missing titles" card after the
  metadata forget. The test launcher and live process both use `ripweaver-test`.
  The fresh preparation created an 11-title awaiting-review job, while the
  frontend incorrectly treated any awaiting-review inventory with missing
  classifier-relevant titles as failed-rip recovery. Guarded that recovery
  presentation on an actual failed job or persisted recovery scope. Rebuilt
  the frontend; M7 physical ripping remains pending.
- 2026-09-19: Diagnosed the subsequent false "11 previously seen titles are
  missing from inventory" card. The live 11-title awaiting-review preview was
  complete, but the UI compared it with an empty identification matching scope
  and treated that scope as physical inventory. The drive card now compares
  acquisition titles with the current preview; the frontend build passes.
  This was a presentation bug, not a failed MakeMKV inventory. No rip started.
- 2026-09-19: Removed the remaining fresh-plan warning that listed every
  title as "not safely present" before acquisition. That status is now shown
  only for an actual recovery/rerip plan; a new awaiting-review plan correctly
  proceeds to exact review and authorization. Frontend build passes.
- 2026-09-19: At the owner's request, reverted the frontend-only suppression
  of the missing-title and recovery messages. Both diagnostic UI elements are
  visible again. The live backend still has two 11-title `awaiting_review`
  plans and no recorded execute failure; the next investigation must capture
  the exact API/UI error when the owner clicks the control that fails. Do not
  infer a physical-disc failure or delete further metadata from these labels.
- 2026-09-19: Compared the live API with the screenshot. The current-drive
  preview contains titles 0–10, has no collision and no required review, while
  the identification matching scope is empty. The dashboard had incorrectly
  used that matching scope as the physical inventory and called all 11 titles
  absent. Corrected the comparison to use the current acquisition preview;
  kept both diagnostic panels, and labeled unacquired titles as awaiting their
  first rip. A fresh awaiting-review plan no longer creates a false rerip
  action. Frontend build and fake-runner rip API composition tests pass. M7
  physical execution has not begun; the owner must still confirm the exact
  current plan through the normal queued-job execute control.
- 2026-09-19: Corrected manual-start workflow in the test frontend. With
  automatic processing disabled, the drive's manual action now inventories
  the disc and, when the plan is clean, authorizes and queues the exact plan
  before scrolling to the final physical-rip confirmation. The confirmation
  shows drive, title indexes, estimated size, and plan digest. Plans with
  collisions or required review still open the review. This does not perform
  a physical rip until the owner confirms the displayed exact plan. Frontend
  TypeScript and production build pass; live execution remains M7 pending.
- 2026-09-19: Refined that manual flow following owner feedback: the drive
  button now opens the detected title list with checkboxes and estimated
  sizes before queueing. `Rip checked titles` creates an exact subset plan
  when needed, queues only a clean reviewed selection, and opens the final
  physical-rip confirmation. The previous automatic queue-after-inventory
  behavior was removed. Frontend TypeScript and production build pass; no
  physical rip or media mutation was performed.
- 2026-09-16: Confirmed the desktop test launcher targets `ripweaver-test` and
  that the other checkout's routing modules are absent here. Added a prominent
  location warning to this worktree's `AGENTS.md`. Began read-only audit of the
  test configuration schema and existing preparation/worker behavior. No media
  operation or local application configuration change was performed.
- 2026-09-16: Reproduced the empty-staging failure with a new synthetic test,
  changed auto-admission to decline absent/incomplete candidates, and ran
  `tests/test_rip_drive_prepare.py` successfully (33 tests). No physical disc,
  provider, or media access occurred. Phase 0 audit continues; Phase 2 above is
  a narrow prerequisite, not proof that routing or the live pipeline works.
- 2026-09-16: The full repository pytest suite passed with coverage disabled;
  Ruff passed for the modified Python files. The approved `wip/test` checkpoint
  was previewed and pushed after the initial milestone. Existing unrelated
  working-tree changes remain in place. Next: complete the test-branch routing
  and configuration-schema comparison before porting any unified assessment.
- 2026-09-16: Completed the test-branch comparison and recorded the
  keep/adapt/do-not-copy verdict above. No other-checkout code was ported.
  M2 (persisted assessment design for this branch) is next. No live disc,
  provider, or media operation occurred during this comparison.
- 2026-09-16: Expanded the repair plan into M0–M7 with queue-owned assessment
  persistence, precise route-outcome/restart gates, explicit TV/triage
  preservation, synthetic acceptance tests, and a separate live-canary approval
  boundary. Planning/documentation only; M2–M7 remain unimplemented.
- 2026-09-16: Started M2 in the active test worktree. Added the saved-data-only
  assessment model and 18 passing synthetic tests for hints, evidence priority,
  unknown/conflicting roles, digest round-trip, and malformed inputs. No queue
  persistence or production consumer is connected yet; M2 remains in progress.
- 2026-09-16: Completed M2's queue-owned foundation. Added append-only routing
  revisions, bounded per-revision route claims/outcomes, interruption marking,
  exact-fingerprint forget cleanup, and a strict optional contract reader for
  legacy compatibility. Concurrent/stale writes, migration, restart,
  idempotency, conflict, movie+extras, mixed-disc, and provider-failure cases
  pass in synthetic tests. The full repository pytest suite and modified-file
  Ruff checks pass. No production preparation, identification, Gemini, or
  worker path consumes this foundation yet; that is M3–M5 work. No live disc,
  media, or provider access occurred.
- 2026-09-16: Started M3 in the active test worktree. Current work is the
  preparation-to-immutable-contract handoff and title-scope separation. No
  live disc or provider access is authorized by this milestone.
- 2026-09-16: Completed M3 synthetic handoff. Preparation now saves one
  assessment and embeds its exact digest/revision in verified-rip contracts;
  identify validates the binding and saved revision. The user's selection is
  stored only as an advisory assessment hint. Unknown/movie/extra roles no
  longer fall through to TV matching, while trusted TV evidence still uses the
  existing TV engine. Fresh acquisition remains whole-inventory; TV scope is
  role-filtered and failed-disc rerip work stays bounded by the exact failed
  acquisition scope. Synthetic tests cover contradictory hints and
  preparation-to-contract-to-restart-to-identify. Focused tests, full pytest,
  modified-file Ruff and formatting checks passed. No live disc/media/provider
  access occurred. M4 alternate-route execution is not yet wired.
- 2026-09-16: Started M4 with a saved-data route controller and atomic
  queue-owned `claim_next` reservation. Synthetic tests cover contradictory
  hints, no-match progression, provider-failure/review holds, concurrent
  claims, and restart persistence. Provider outcome wiring is still in progress.
- 2026-09-16: M4 route policy and typed Gemini result reporting now pass
  synthetic tests. A movie assessment no longer enters the legacy TV-related
  Gemini branch merely because its label looks like a series; validated TV
  fallback matches retain their TV role. Added per-title tests for absent
  catalogue versus service outage, multiple movies, extras, and conflicting
  metadata. Full pytest and modified-file Ruff pass. M4 is **not complete**:
  the downstream worker still does not claim and settle those actual outcomes
  transactionally, and the automatic fallback remains review-code-only.
  No live provider, media, or disc operation occurred.
- 2026-09-16: Continued M4 in the same test worktree. A held identify item can
  now be claimed atomically with its exact queue review code, and the worker
  records terminal TV coordinator outcomes against the bound assessment.
  `all_season_series_not_found` remains review because an absent series
  catalogue is not title-level non-TV evidence; catalogue/evidence or analysis
  failures hold as service failures, while coherence/independent evidence
  holds remain review. Pending TV codes are not settled early. Synthetic tests
  verify paused queues, no-match policy progression, outage hold,
  repeated worker calls, and queue-state mismatch. Gemini classification
  outcomes and alternate-route execution are still not connected to the
  worker, so M4 remains in progress. No live operation occurred.
- 2026-09-16: Audited the meaning of provider results before using them as
  route evidence. A Gemini descriptive `tv_episode` proposal without an
  independently validated TV assignment is now review, not a negative
  movie/classification result. Only an explicit null episode choice within a
  bounded reviewed catalogue is typed as a catalogue no-match. This prevents
  a provisional type guess from automatically advancing to another route.
- 2026-09-16: Connected startup to durable route-claim reconciliation. A
  crashed `running` claim becomes `interrupted` and is not silently retried;
  the startup log reports counts only. Focused synthetic restart tests pass.
  This still does not authorize provider or media work and does not complete
  M4's Gemini-to-worker result settlement.
- 2026-09-16: Resumed M4 after writing the current-agent handoff above. The
  existing worker now runs a single assessment-bound Gemini fallback on its
  own thread under `_downstream_lock`, with both automatic flags required, an
  exact held-item route claim, typed outcome settlement, and no detached
  per-item task. Synthetic real-queue/fake-provider tests cover matched,
  no-match, visual/review hold, provider outage, pause/stop, unapplied match,
  and failure after a revised contract is attached. Assessed provisional
  movie identities are held before identify can produce a transcode contract.
  Focused tests pass; full-suite and broader M4 matrix are next. No live
  disc, media, or provider was accessed.
- 2026-09-16: Completed this turn's broader synthetic check. The full pytest
  suite passed with coverage disabled; focused TV/triage/routing/Gemini tests
  and modified-file Ruff passed. Added an 11-title movie-plus-extras worker
  matrix with a contradictory TV hint; all titles retained a reviewable route.
  Gemini results are accepted as `matched` only when a revised contract has a
  non-provisional assignment of the claimed role. Provisional identities and
  post-apply failures hold for review before transcode or placement. M4 still
  needs the explicit TV no-match alternate and sibling/concurrent integration
  gate described in the handoff above. No live operation occurred.
- 2026-09-16: Completed M4's explicit TV no-match alternate route integration.
  Synthetic real-queue/fake-provider tests confirm that a TV route that ends in
  a confident, whole-series explicit TV no-match is settled and the item transitions
  to the alternate movie route. Weak evidence, unavailable catalogues, and provider
  failures do not trigger the alternate route and are correctly held for review or
  service-failed. Confirmed pause/stop interrupts the fallback, and that concurrent
  claims or a concurrently advanced database revision safely reject the retry attempt.
  The full pytest suite and modified-file Ruff passed. M4 acceptance gate has passed.
  No live operation occurred. Next: M5.
- 2026-09-17: Fixed the M4 Ruff formatting and test-quality gaps. Corrected the TV coordinator test to properly assert the genuine 	v_title_no_match state transition. Removed leftover ad-hoc test scripts. The full pytest suite and modified-file Ruff checks now genuinely pass. Checkpoint pushed. Ready for M5. No live disc, media, or provider operations occurred.
- 2026-09-17: Completed M6's visibility and user fallback requirements.
  Extended `PipelineItemResponse` to export `user_hint`, `assessed_composition`,
  `assessed_role`, `current_route`, `evidence_status`, and `exhausted_reason`.
  Updated `RipPipelineView` to display these cleanly without misrepresenting
  model guesses as verified identity. Implemented `/pipeline/discs/{fingerprint}/reassess`
  for metadata-only legacy disc reassessment that safely appends a revision without
  re-reading physical media or altering completed assignments.
  Frontend compilation, Ruff checks, and pytest suite passed cleanly. No live
  operations occurred. Next: Hand-off/Completion.
- 2026-09-22: Live Short Circuit 2 follow-up exposed a post-rip control
  coupling: disabling unattended disc processing also prevented the
  identification worker from starting, leaving seven verified substantial
  titles queued while four short titles correctly entered review. Split the
  controls by adding a default-enabled `downstream_processing_enabled`
  preference. `automatic_processing_enabled` now remains the inserted-disc /
  autorip and automatic-transcode control; the new preference starts and
  governs identification and routing for already verified items independently.
  The CLI `--hold-automatic-rips` lifetime hold no longer suppresses approved
  downstream identification. Settings and queue status expose the distinction.
  Focused backend tests, modified-file Ruff, and the frontend production build
  pass. No disc, provider, transcode, organization, deletion, or eject operation
  was performed by this repair.
- 2026-09-22: Added `docs/MOVIE_EXTRAS_IDENTITY_PLAN.md` for the next repair.
  It separates verified content roles from exact identities: the main movie
  requires a canonical provider-verified identity, while related extras may use
  stable evidence-derived descriptive names. The plan preserves the TV path,
  adds queue-fairness and restart gates, and requires synthetic validation
  before a separately authorized continuation of the existing live MKVs.
- 2026-09-25: Added a default-off `automatic_ai_extra_titles` preference.
  The existing Gemini ambiguity fallback no longer implies permission to create
  or automatically accept descriptive extras filenames. With this preference
  disabled, the extra route remains in manual naming review; enabling it permits
  the bounded Gemini descriptive-evidence path. Settings now also track the
  last server-saved configuration and warn before in-app navigation, reload, or
  window close would discard edits. Focused downstream/config tests, modified-file
  Ruff, and the frontend production build pass. The repository-wide frontend
  lint still reports six unrelated pre-existing findings in `MediaTriageView`
  and `RipPipelineView`.
