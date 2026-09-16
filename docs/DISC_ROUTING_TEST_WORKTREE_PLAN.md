# Disc routing repair — active test worktree

> STOP: This plan applies only to the `ripweaver-test` source worktree launched by
> the desktop test shortcut. The installed EXE and the separate
> `mkv-episode-matcher` checkout are not this running test build. Confirm the
> resolved repository root and branch before editing or reporting a result.

## Status (2026-09-16)

Phase 0, test-worktree baseline audit: **complete**. The narrow fresh-scan
auto-admission blocker has been repaired, but no unified routing repair has
been ported and no fresh Short Circuit 2 live rip has been validated. Existing
uncommitted test-worktree changes belong to the user and must be preserved.
The earlier routing plan in another checkout is a design/reference only; its
completion and test claims do not apply here.

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

## Repair sequence and gates

1. [x] Finish baseline review: map test-worktree preparation, saved contracts,
   worker, movie/extra identify, queue, and relevant config **schema**; compare
   each proposed repair from the other checkout against current behavior.
2. [x] Fix fresh preparation's no-staged-candidate auto-admit path and add a
   synthetic regression test. Incomplete staged sets also decline auto-admit
   without a path-bearing error. The focused drive-preparation test file passes.
3. [ ] Design a single persisted, fingerprint-bound disc/title assessment for
   this branch. Keep user hints advisory, preserve unknown/conflicting evidence,
   and separate whole-disc acquisition from per-title identification scope.
   Port only compatible pieces after review; do not copy entire modules blindly.
4. [ ] Integrate preparation, immutable contract, identify, Gemini outcomes,
   alternate attempts, and durable worker/restart controls. Preserve existing
   TV independent-evidence, range, and coherence gates, and triage originals.
5. [ ] Review dashboard and legacy recovery; then run focused and full synthetic
   tests with recorded results. Update this plan after every milestone and
   checkpoint the test worktree under the approved `wip/test` procedure.
6. [ ] Propose an exact-input live Short Circuit 2 canary separately. Do not
   erase metadata, rip, eject, transcode, or call a provider based on this plan.

## Progress log

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
  Phase 3 (persisted assessment design for this branch) is next. No live disc,
  provider, or media operation occurred during this comparison.
