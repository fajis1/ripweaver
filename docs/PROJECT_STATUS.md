# Project Status

## 2026-08-01 — automatic sequence recovery and queue safety UX

- Automatic TV batches that ordinary identification cannot place now enter the
  serialized disc-sequence matcher without requiring a second UI click. The
  idle downstream worker also recovers already-held multi-title batches after
  restart.
- Active MakeMKV orchestration jobs are now shown above the downstream queue;
  the UI explains that a title appears downstream only after its rip verifies.
- Existing MakeMKV `PRGV` samples now flow into path-redacted orchestration
  events. The parser recognizes MakeMKV's progress reset between internal/title
  operations. Drive cards and the queue show the current percentage plus actual
  staged-MKV write throughput sampled every two seconds; this performs no extra
  disc read and exposes no staging path.
- Existing-rip discovery prefers one exact current ordinal/fingerprint/title
  basename over older ordinal copies, while multiple copies of the exact same
  basename remain an explicit ambiguity.
- Queued identified rips now offer both a non-destructive queue removal and a
  separately confirmed permanent staged-rip deletion. Deletion validates the
  immutable contract, exact size, MKV extension, and containment within the
  configured rip-staging root; it never changes Jellyfin.
- A non-running rip plan can be cancelled and its disc ejected from the review
  screen. Active MakeMKV execution is refused. Failed-attempt cleanup now blocks
  only the exact active rip job, not unrelated drives.
- Focused backend tests passed (39 tests), the API composition regression passed,
  focused Ruff checks passed, and frontend lint, TypeScript, and production build
  passed. No media operation was performed by these implementation tests.

## 2026-08-01: duplicate staged-rip recovery and disc-history isolation

- Existing-rip discovery now returns every collision-safe retained candidate when
  two or more attempts exist for the same fingerprint/title. The public response
  uses opaque candidate digests and does not expose staging paths.
- The web review requires an explicit one-per-title radio selection for ambiguous
  copies. Verification re-discovers the files, validates the unchanged plan, and
  binds FFprobe to the exact selected candidate; it still never overwrites or
  removes an earlier attempt.
- Removed the frontend's unsafe prior-name fallback through session-local
  `disc-01-title-NNN` queue IDs. Prior Jellyfin outcomes are now shown only from
  backend history keyed by the durable inventory fingerprint and title index, so
  one disc's titles cannot be displayed on an unrelated disc review.
- Synthetic recovery/API/queue tests passed. Ruff, frontend lint, TypeScript, and
  the packaged frontend build passed. No disc, MKV, credential, or external media
  executable was accessed.
- The Disc Dashboard now filters downstream records by the currently selected
  disc's verified inventory fingerprint. A separate Queue navigation page owns
  the complete cross-disc queue and its global start, pause, transcode, and
  organization controls.
- Reviewed cross-season episode assignments are now a production identification
  contract. The Faerie Tale Theatre Volume 4 aired-order catalogue maps its four
  title indexes across S03E05-S03E07 and S04E01, is attached automatically only
  when the normalized disc label and complete title set match, and bypasses the
  obsolete single-season requirement without running the matcher again.
- Existing held Volume 4 items can be repaired from the Disc Dashboard by an
  exact fingerprint/catalogue operation. It creates new immutable private input
  contracts and requeues identification; it does not read or modify media.
- Existing-rip recovery now derives a stable collision-safe queue ID from the
  exact selected candidate digest. Retained copies with the same MakeMKV
  basename no longer collide with an earlier queue contract, and neither the
  earlier contract nor either media copy is overwritten.
- Unknown discs without a season now stop as
  `unmatched_disc_analysis_required` instead of the misleading
  `missing_season_context`. The general live evidence-collection coordinator is
  still a separate remaining integration; the saved-data BM25/sequence planner
  itself is unchanged.

Last updated: 2026-07-30

## Current State

The repository remains an episode matcher with a Typer CLI, FastAPI backend,
React frontend, and Faster Whisper/subtitle matching engine. It now also has an
isolated read-only MakeMKV preflight capability and an environment-backed
credential boundary with safe CLI and Web replacement flows. Phase 1 now adds
a deterministic plan-only title selector with explicit ambiguity constraints.
Phase 2 now includes saved-FFprobe parsing, a constrained read-only live
metadata adapter, and data-only audio diagnostic plans. The live adapter has
only been exercised through mocks and synthetic files.

Phase 3 now has a controlled MakeMKV executor and redacted manifest format under
mocked test coverage. No physical ripping has occurred yet.

The first Phase 4 unmatched-identification slice is implemented. It can build
an aired-order TMDb episode catalogue, rank that catalogue locally from runtime
and transcript evidence, and prepare a path-free, schema-constrained Gemini
fallback request. The Gemini adapter produces review data only, validates all
returned episode IDs and one-to-one assignments locally, and has not been
called against the live service.

The second Phase 4 slice now adds a saved-data-only evidence collector. It
selects short non-duplicate excerpts from explicit multi-window transcript
JSON, creates the local catalogue shortlist, and writes both a private
transient Gemini bundle and a separate dialogue-free plan. No saved
multi-window transcript-text report was present in `.mkv-runs`, so this phase
defined and tested the input boundary but did not sample MKV files.

The third Phase 4 slice now adds the explicit-file CPU Whisper batch collector.
It consumes saved sanitized FFprobe metadata, loads one ASR provider, samples
three windows sequentially, uses a working default multichannel stream before
trying alternates, and separates private dialogue from durable metrics. It has
only been tested with fake extraction, synthetic WAVs, and fake ASR providers;
no real MKV was read by this command.

Live validation has now read one known-good Dragons control plus all twelve
staged Theatre files with explicit authorization. Every file collected from
the normal default 5.1 stream, so stereo fallback was not used. The Theatre
reports were merged under twelve redacted IDs, and a metadata-only TMDb request
for reviewed series ID 4603 saved 27 aired-order episodes. Offline local
ranking and the Gemini request preview completed; no Gemini request was made.

The standard three-window ranking led the reviewed answer for six of twelve
Theatre files. An authorized 60-second introduction pass collected three
usable supplements and raised the offline top-result agreement to eight of
twelve. The later authorized 120-second stream-2 pass collected usable local
transcripts for all four remaining targets and raised independent offline
top-result agreement to ten of twelve. Pied Piper and Snow White remain
lexically ambiguous; their expected aired-order entries are present in the
top-ten candidates. No Gemini request was made.

The saved-data-only disc-sequence planner now resolves the full reviewed
twelve-file set. Explicit chronological groups were supplied as disc 05, disc
04, then disc 03; title membership and order were not inferred. The selected
contiguous ranges are S03E01-S03E04, S03E05-S04E01, and S04E02-S04E05. The
global score is 0.818 with a 0.107 best-versus-next margin, and all three groups
are proposals. This is dialogue-free review output only; no media was changed
and no Gemini request was made.

The plan-only TV organizer subsequently generated twelve canonical Theatre
destinations directly beneath Season 03 and Season 04. Filename-only inspection
found zero exact or same-episode destination conflicts. The canonical series
folder does not yet exist, so both season directories are reported as missing;
neither was created. The saved report contains relative targets only. No media
content was read and no rename, move, overwrite, deletion, directory creation,
or transcode occurred.

The path-redacted Theatre HandBrake batch manifest covers all twelve staged
MKVs exactly once, retains only source basenames, and proposes unique
encoded-staging destinations. Live capability inspection confirmed AMD VCN
availability and sufficient space. Its original AMD `vce_h265` quality-26
profile is now superseded for execution because visual review found interlace
combing and the manifest predates the selective-decomb setting. It must be
regenerated after the replacement sample is visually approved. No full batch
transcode has occurred.

One explicitly authorized Theatre sample has now completed through the safe
adapter. It encoded a 120-second excerpt with AMD `vce_h265` at quality 26 into
a dedicated staging-sample directory. Adapter verification and an independent
FFprobe check found HEVC video, original AC-3 5.1 audio, AAC stereo compatibility
audio, no subtitle stream, a 120.021-second duration, and a 20,354,895-byte
output. The unique partial file was promoted only after verification and no
partial remains. No media-library destination was created or written.

HandBrake emitted two drive-qualified metadata fragments that were not the
source, destination, tool, or user-profile path. The generated process log was
sanitized and rechecked with zero drive-qualified or UNC paths remaining. The
adapter now removes any residual Windows or UNC path from process-log lines,
including paths originating in source metadata, and has regression coverage.

Visual review rejected the first Theatre sample because it retained obvious
interlace combing. The adapter profile now records `selective_decomb` and, when
enabled, passes both `--comb-detect` and `--decomb`; disabling it is separately
covered. A second explicitly authorized comparison encoded the same 120-second
window with AMD `vce_h265`, selective decomb, and CQ 24. Verification found
HEVC video at 718x480 and 30000/1001 fps, two audio streams, no subtitle stream,
a 120-second duration, and a 27,392,396-byte output. Its process log confirms
both filters ran, contains no drive-qualified or UNC paths, and no partial file
remains. Human visual and audio approval is still required.

The HandBrake profile now places the 256 kbps AAC Pro Logic II stereo track
first so it is the default playback track, with the original 5.1-capable source
track preserved second. The former surround-first order remains available only
as an explicit profile override and is covered separately.

NLMeans is now optional and plan-visible rather than a global default. The CLI
accepts validated `ultralight`, `light`, `medium`, or `strong` presets and the
installed HandBrake content tunes. Denoise is structurally limited to profiles
explicitly classified as `live_action`; `animation` and `unknown` profiles
reject NLMeans settings. This prevents cartoons from receiving denoise
accidentally.

The explicitly authorized low-quality Theatre comparison completed at CQ 26
with selective decomb and NLMeans `ultralight`/`film`. Independent FFprobe
verification found HEVC video at 718x480 and 30000/1001 fps, a 120.021-second
duration, and an 18,540,314-byte output. AAC stereo is the first/default audio
stream and the original AC-3 5.1 stream is second and non-default. The process
log confirms NLMeans, decomb, and comb detection ran; both logs contain zero
drive-qualified or UNC paths. No partial remains, both earlier samples are
preserved, and no batch or media-library operation occurred. The owner
subsequently visually approved this sample.

A new non-overwriting, path-redacted Theatre batch manifest now records the
approved profile. It exactly covers the same twelve reviewed sources and has
twelve unique relative encoded-staging destinations. The profile is AMD
`vce_h265` CQ 26, selective decomb, explicit `live_action`, NLMeans
`ultralight`/`film`, stereo-first audio, original surround second, and subtitle
retention. Capability, collision, and free-space checks passed. The manifest
contains no drive-qualified paths, UNC paths, or parent traversal. Its two
encoded-season staging directories remain absent and were not created; status
is `ready-after-directory-creation`. No full transcode was started.

The safe HandBrake batch executor is now implemented in
`mkv_episode_matcher/media/handbrake_batch_executor.py`, with the confirmed CLI
boundary `execute-handbrake-batch`. It strictly loads the current manifest
schema, hashes the raw manifest, revalidates source names and sizes, output
containment, capacity, and profile, and keeps its event directory outside both
media roots. The CLI defaults to a maximum of two concurrent jobs and two total
attempted jobs per invocation; a smaller canary or larger authorized slice can
be selected explicitly. Destination directories are created incrementally only
for jobs attempted in that invocation.

Each batch job delegates to the existing one-file partial/FFprobe/promote
adapter. Existing unknown finals or partials block only their own job, failures
are isolated by exception type without message/path retention, and unaffected
jobs continue. Path-free JSONL events support resume only when the manifest
digest matches and completed output sizes remain unchanged. `STOP` and `PAUSE`
prevent the next chunk from starting while allowing at most the active
two-job chunk to finish verification safely. All executor tests use synthetic
MKVs and mocked HandBrake/FFprobe behavior. No real batch execution, staging
directory creation, or full-episode transcode occurred during implementation.

CPU-only Faster Whisper with CTranslate2 INT8 is the supported transcription
baseline. AMD integrated-GPU experiments are deferred.

No ripping, renaming, moving, deletion, ejection, or transcoding was performed
during the initial integration work.

## Inspected

- CLI commands and configuration flow.
- FastAPI application, routers, dependencies, and WebSocket connection.
- React frontend API and WebSocket integration.
- current matching engine, audio extraction, ASR, subtitle providers, models,
  and file-renaming path;
- Python packaging, development dependencies, and pytest configuration;
- upstream Riplex documentation and source organization relevant to scanning,
  runtime matching, deduplication, organization, MakeMKV, and orchestration;
- three supplied PDF records covering prior deduplication, renaming/audio
  troubleshooting, and Riplex setup;
- fourteen relevant legacy Python scripts, parsed without importing or
  executing them.

Legacy credential values were not printed or inspected. Four recognized fields
were transferred opaquely from the old user JSON config into the local ignored
`.env`, and those fields were removed from the JSON.

## Files Changed

- `.gitignore`
  - ignores `.env`, other environment variants, `.mkv-preflight/`, and the
    local pytest `.test-tmp/` base directory;
  - retains `.env.example`.
- `.env.example`
  - documents credential and tool-path variable names with blank values.
- local ignored `.env`
  - stores the opaquely migrated credentials; values were never displayed.
- `mkv_episode_matcher/core/environment.py`
  - loads secrets and external-tool paths from environment variables or the
    local `.env`.
- `mkv_episode_matcher/core/config_manager.py`
  - overlays environment secrets;
  - excludes secret fields from future JSON saves.
- `mkv_episode_matcher/core/credentials.py`
  - defines safe credential metadata and provider links;
  - writes hidden user input to the ignored local `.env`;
  - classifies missing/rejected credentials separately from rate limits,
    outages, and other service failures;
  - allows an interactive client to replace a rejected credential and retry
    once;
  - provides a narrow, atomic legacy-JSON migration that never returns values.
- `mkv_episode_matcher/disc/__init__.py`
  - introduces the read-only disc package.
- `mkv_episode_matcher/disc/preflight.py`
  - validates MakeMKV `info` commands;
  - parses drive, disc, title, stream, message, channel, and default-track data;
  - writes ignored JSON and robot-output reports.
- `mkv_episode_matcher/disc/title_selector.py`
  - normalizes title duration, byte size, chapters, output name, and audio
    metadata from saved reports;
  - finds dominant episode-runtime clusters;
  - excludes aggregate titles when runtime matches a sum of selected
    individual titles;
  - classifies short titles as extras and ambiguous titles for review;
  - ranks English non-commentary stereo first for diagnostics and retains
    alternate streams;
  - supports optional expected episode-count, runtime, and tolerance hints
    without forcing titles that fail runtime evidence;
  - produces data-only plans with no execution command.
- `mkv_episode_matcher/disc/rip_manifest.py`
  - creates path-redacted manifests from fresh explicit inventories;
  - rejects duplicate-drive reports and excludes discs with review titles.
- `mkv_episode_matcher/disc/ripper.py`
  - permits one MakeMKV `mkv` title per subprocess;
  - supports parallel workers across physical drives while preserving
    sequential title order within each drive;
  - isolates drive-specific failures so unaffected drive workers finish;
  - creates unique staging directories and refuses overwrites;
  - assigns verified outputs a manifest-approved unique basename containing
    disc ID, inventory fingerprint, and title index for future runs;
  - streams redacted JSONL events with progress, warnings, cancellation,
    timeout, fatal-error detection, and output verification;
  - preserves partial output for diagnosis and pauses on the first failure.
- `mkv_episode_matcher/media/probe.py`
  - normalizes saved FFprobe duration, size, container, and audio streams;
  - recognizes language, default, channel layout, and commentary/descriptive
    metadata;
  - deliberately drops source filenames.
- `mkv_episode_matcher/media/ffprobe_runner.py`
  - accepts explicit existing `.mkv` files only;
  - constructs fixed FFprobe JSON-inspection arguments without a shell;
  - captures status and output with a timeout while redacting source paths;
  - saves only normalized, replayable metadata under ordinal media IDs.
- `mkv_episode_matcher/media/audio_diagnostics.py`
  - ranks primary and alternate streams;
  - plans three 30-second sample windows;
  - records dialogue-preserving downmix intent for multichannel streams;
  - requires loudness, peak, silence, transcript-word, and information-score
    measurements;
  - contains no external-process or media access.
- `mkv_episode_matcher/media/episode_catalog.py`
  - builds and validates an authoritative aired-order episode catalogue from
    supplied TMDb data;
  - ranks candidates locally using transcript/title/overview similarity and
    runtime without invoking an external AI service.
- `mkv_episode_matcher/media/evidence_bundle.py`
  - loads explicit saved multi-window transcript evidence and authoritative
    catalogue JSON;
  - rejects paths, invalid IDs, duplicate files, unsafe sizes, and malformed
    fields;
  - selects up to three informative non-duplicate excerpts per file;
  - joins local candidate rankings into a private transient bundle while
    retaining enough candidates for disc-wide assignment;
  - produces a separate dialogue-free score/count report and refuses output
    collisions before writing.
- `mkv_episode_matcher/media/transcript_batch.py`
  - validates explicit MKVs, redacted IDs, and saved FFprobe-derived media
    plans;
  - builds fixed no-shell FFmpeg sample commands using `-n`, mono 16-bit PCM,
    explicit stream maps, and bounded timeouts;
  - loads one CPU ASR provider and samples files sequentially;
  - prefers a normal default stream, keeps usable 5.1 audio, and falls back only
    for weak/silent/failed samples;
  - isolates per-file audio failures so later files continue;
  - removes temporary WAVs and separates private dialogue output from safe
    path-free metrics;
  - supports a caller-reviewed introduction start and preferred saved audio
    stream.
- `mkv_episode_matcher/media/evidence_bundle.py`
  - supports explicit, validated redacted-ID aliases during saved-report merge;
  - refuses absent mapping sources and retains duplicate-duration validation;
  - allowed the completed four-file evidence to be merged without rereading
    media after an ID-format mismatch was detected.
- `mkv_episode_matcher/media/sequence_matcher.py`
  - validates explicit chronological disc groups with exact evidence coverage;
  - scores every contiguous aired-order window for each group;
  - uses dynamic programming for the best and second-best ordered,
    non-overlapping global assignments;
  - retains independent lexical tops and local/global ambiguity margins in a
    dialogue- and path-free report;
  - never accesses media, providers, or mutation operations.
- `mkv_episode_matcher/media/organizer.py`
  - loads only fully proposed sequence assignments;
  - generates Windows-safe Plex/Jellyfin episode filenames directly under
    `Season XX`;
  - performs case-insensitive canonical and same-episode destination checks;
  - routes collisions to deduplication/review and refuses report overwrites;
  - records relative destinations only and performs no library mutation.
- `mkv_episode_matcher/media/handbrake_batch.py`
  - validates exact one-to-one source coverage using file metadata only;
  - requires one source root so the manifest can store basenames without
    absolute paths;
  - checks AMD VCN capabilities, profile validity, free space, output
    separation, and final/partial collisions;
  - produces relative encoded-staging jobs and reports missing directories
    without creating them.
- `mkv_episode_matcher/media/gemini_matcher.py`
  - constructs path-free, size-bounded Gemini structured-output requests from
    explicit evidence and an authoritative candidate catalogue;
  - accepts only supplied episode IDs, exact file coverage, and one-to-one
    assignments;
  - classifies authentication, quota, transient-service, and network failures,
    with bounded retries and the existing hidden credential replacement flow;
  - writes dialogue-free safe plan reports and never creates filenames or
    mutates media.
- `mkv_episode_matcher/cli.py`
  - adds `mkv-match preflight`;
  - adds `mkv-match credentials`;
  - prompts for missing or rejected TMDb/OpenSubtitles credentials without
    echoing values and supplies official management links;
  - adds `mkv-match plan-titles` with console and JSON-only plans;
  - adds `mkv-match plan-audio` for saved FFprobe JSON only.
  - adds `mkv-match probe-mkv` for sequential read-only inspection of explicit
    MKV files; it performs no discovery or media mutation.
  - adds `mkv-match plan-rip` and confirmation-gated `mkv-match execute-rip`;
  - adds `mkv-match plan-gemini-unmatched`, which validates an explicit
    transient evidence bundle and writes only a safe request plan; it performs
    no Gemini/TMDb request and no media access;
  - adds `mkv-match build-unmatched-bundle`, which reads explicit saved reports,
    performs local ranking, and writes collision-safe private/safe outputs
    without Whisper, provider, or media access;
  - adds confirmation-gated `mkv-match collect-transcripts` for explicit MKV
    lists, paired redacted IDs, and paired saved FFprobe reports;
  - adds private saved-report merging/enrichment and metadata-only
    `fetch-aired-catalog`.
  - defaults approved rip manifests to parallel-across-drives execution, with
    `--parallel-drives` as a concurrency cap and `--sequential` as an opt-out.
- `mkv_episode_matcher/tmdb_client.py`
  - no longer embeds API keys in URL strings;
  - handles missing, rejected, rate-limited, and unavailable service states
    separately;
  - exposes an aired-order catalogue fetch for the unmatched planner.
- `mkv_episode_matcher/core/providers/subtitles.py`
  - supports one-time interactive recovery for missing or rejected
    OpenSubtitles credentials;
  - avoids including raw provider exceptions in logs;
  - uses stable Windows-safe cache directories and filenames while retaining
    canonical punctuation in provider searches.
- `mkv_episode_matcher/core/engine.py`
  - accepts a canonical `--show-name` override for generic staging folders;
  - searches ancestor folders for `Season XX` context so collision-safe
    disc/title directories may live below the matcher-native TV hierarchy.
- `mkv_episode_matcher/backend/routers/system.py`
  - never returns credential values to the browser;
  - accepts replacements from Settings/onboarding and stores them in `.env`;
  - returns only configured status and official management links.
- `mkv_episode_matcher/backend/routers/match.py` and frontend source/build
  - return and display structured credential failures;
  - offer a Settings action and provider-management link.
- `tests/test_credentials.py` and `tests/test_system_credentials.py`
  - exercise hidden entry, safe storage/status, TMDb recovery, OpenSubtitles
    recovery, rate-limit classification, and browser-response redaction.
- `tests/test_title_selector.py`
  - covers the four-episode fixture, six-episode known-good control, combined
    titles, short extras, audio ranking, JSON path redaction, and absence of
    execution data.
- `tests/test_audio_diagnostic_plan.py`
  - covers saved FFprobe normalization, filename redaction, stereo/5.1/
    commentary ordering, multichannel downmix intent, invalid metadata, CLI
    JSON, and absence of execution data.
- `tests/test_ffprobe_runner.py`
  - covers fixed argument construction, `.mkv`-only validation, no-shell
    execution, timeout, nonzero exit, malformed JSON, path redaction,
    replayable sanitized reports, and CLI ordinal IDs.
- `tests/test_rip_manifest.py` and `tests/test_ripper.py`
  - cover manifest redaction, ambiguity and duplicate-drive handling, strict
    command construction, path containment, overwrite refusal, progress,
    hardware/path redaction, fatal errors, timeout, cancellation, and output
    verification using fake processes only.
- `tests/test_disc_preflight.py`
  - tests safe command construction and robot-output parsing.
- credential/configuration tests
  - now verify environment loading and non-persistence of secrets.
- `tests/test_episode_catalog.py`, `tests/test_gemini_matcher.py`, and
  `tests/test_gemini_cli.py`
  - cover catalogue construction and local ranking, request redaction and
    bounds, structured-response validation, credential recovery, paid-key
    fallback, bounded retries, plan-only CLI behavior, and all twelve reviewed
    *Faerie Tale Theatre* aired-order assignments.
- `tests/test_evidence_bundle.py` and `tests/test_evidence_bundle_cli.py`
  - cover saved-report loading, excerpt selection/deduplication, local ranking,
    disc-wide candidate retention, path rejection, output collision refusal,
    dialogue separation, and absence of live API requests.
- `tests/test_transcript_batch.py` and `tests/test_transcript_batch_cli.py`
  - cover fixed FFmpeg arguments, overwrite refusal, redacted failures, model
    reuse, temporary cleanup, usable 5.1 retention, alternate fallback,
    per-file failure isolation, dialogue separation, confirmation gating, and
    CLI output, including targeted introduction sampling.

The test run updated the tracked `.coverage` artifact. It was not discarded.
A pre-existing untracked nested frontend directory was preserved and not
inspected or modified as part of this work.

## Verification

Commands run:

```powershell
uv run pytest tests\test_disc_preflight.py tests\test_main.py
uv run pytest
uv run pytest tests\test_disc_preflight.py tests\test_main.py tests\test_config_special_characters.py
uv run ruff check mkv_episode_matcher\disc\preflight.py mkv_episode_matcher\core\environment.py mkv_episode_matcher\core\config_manager.py tests\test_disc_preflight.py tests\test_config_special_characters.py
uv run mkv-match preflight --help
uv run pytest tests\test_credentials.py tests\test_system_credentials.py tests\test_tmdb_id_feature.py tests\test_main.py tests\test_backend_singleton.py
uv run ruff check mkv_episode_matcher\core\credentials.py mkv_episode_matcher\core\environment.py mkv_episode_matcher\core\config_manager.py mkv_episode_matcher\tmdb_client.py tests\conftest.py tests\test_credentials.py tests\test_system_credentials.py mkv_episode_matcher\backend\routers\system.py --select E9,F,I,UP
uv run mkv-match credentials
uv run mkv-match credentials --migrate-legacy
npm ci
npm run build
uv run pytest tests\test_title_selector.py tests\test_disc_preflight.py
uv run ruff check mkv_episode_matcher\disc\title_selector.py tests\test_title_selector.py --select E9,F,I,UP
uv run ruff format --check mkv_episode_matcher\disc\title_selector.py tests\test_title_selector.py
uv run mkv-match plan-titles --help
uv run pytest tests\test_audio_diagnostic_plan.py tests\test_title_selector.py tests\test_disc_preflight.py
uv run ruff check mkv_episode_matcher\disc\title_selector.py mkv_episode_matcher\media tests\test_title_selector.py tests\test_audio_diagnostic_plan.py --select E9,F,I,UP
uv run ruff format --check mkv_episode_matcher\disc\title_selector.py mkv_episode_matcher\media tests\test_title_selector.py tests\test_audio_diagnostic_plan.py
uv run mkv-match plan-audio --help
uv run pytest tests\test_ffprobe_runner.py tests\test_audio_diagnostic_plan.py
uv run ruff check mkv_episode_matcher\media\ffprobe_runner.py mkv_episode_matcher\core\environment.py tests\test_ffprobe_runner.py --select E9,F,I,UP
uv run ruff check mkv_episode_matcher\cli.py --select E9
uv run mkv-match probe-mkv --help
.\.venv\Scripts\pytest.exe -p no:cacheprovider --basetemp .\.test-tmp\full-gemini-1 -q
.\.venv\Scripts\ruff.exe check mkv_episode_matcher\media\episode_catalog.py mkv_episode_matcher\media\gemini_matcher.py mkv_episode_matcher\tmdb_client.py tests\test_episode_catalog.py tests\test_gemini_matcher.py tests\test_gemini_cli.py --select E9,F,I,UP
.\.venv\Scripts\ruff.exe format --check mkv_episode_matcher\media\episode_catalog.py mkv_episode_matcher\media\gemini_matcher.py tests\test_episode_catalog.py tests\test_gemini_matcher.py tests\test_gemini_cli.py
uv run mkv-match plan-gemini-unmatched --help
.\.venv\Scripts\pytest.exe -p no:cacheprovider --basetemp .\.test-tmp\evidence-focused -q tests\test_evidence_bundle.py tests\test_evidence_bundle_cli.py tests\test_episode_catalog.py tests\test_gemini_matcher.py tests\test_gemini_cli.py
uv run mkv-match build-unmatched-bundle --help
.\.venv\Scripts\pytest.exe -p no:cacheprovider --basetemp .\.test-tmp\transcript-batch-focused-2 -q tests\test_transcript_batch.py tests\test_transcript_batch_cli.py tests\test_transcript_diagnostic.py tests\test_evidence_bundle.py tests\test_evidence_bundle_cli.py tests\test_audio_diagnostic_plan.py
uv run mkv-match collect-transcripts --help
uv run mkv-match merge-transcript-reports --help
uv run mkv-match fetch-aired-catalog --help
uv run pytest tests\test_transcript_batch.py tests\test_transcript_batch_cli.py tests\test_gemini_matcher.py
uv run ruff check mkv_episode_matcher\media\transcript_batch.py tests\test_transcript_batch.py tests\test_transcript_batch_cli.py tests\test_gemini_matcher.py
uv run ruff format --check mkv_episode_matcher\media\transcript_batch.py tests\test_transcript_batch.py tests\test_transcript_batch_cli.py tests\test_gemini_matcher.py
uv run pytest
uv run pytest tests\test_evidence_bundle.py tests\test_transcript_merge_cli.py tests\test_evidence_bundle_cli.py
uv run pytest
uv run pytest tests\test_sequence_matcher.py tests\test_sequence_matcher_cli.py
uv run ruff check mkv_episode_matcher\media\sequence_matcher.py tests\test_sequence_matcher.py tests\test_sequence_matcher_cli.py
uv run ruff format --check mkv_episode_matcher\media\sequence_matcher.py tests\test_sequence_matcher.py tests\test_sequence_matcher_cli.py
uv run pytest
uv run pytest tests\test_organizer.py tests\test_organizer_cli.py
uv run ruff check mkv_episode_matcher\media\organizer.py tests\test_organizer.py tests\test_organizer_cli.py
uv run ruff format --check mkv_episode_matcher\media\organizer.py tests\test_organizer.py tests\test_organizer_cli.py
uv run pytest
uv run pytest tests\test_handbrake_batch.py tests\test_handbrake_batch_cli.py tests\test_handbrake_adapter.py
uv run ruff check mkv_episode_matcher\media\handbrake.py mkv_episode_matcher\media\handbrake_batch.py tests\test_handbrake_batch.py tests\test_handbrake_batch_cli.py
uv run ruff format --check mkv_episode_matcher\media\handbrake.py mkv_episode_matcher\media\handbrake_batch.py tests\test_handbrake_batch.py tests\test_handbrake_batch_cli.py
uv run pytest tests\test_handbrake_adapter.py tests\test_handbrake_batch.py tests\test_handbrake_batch_cli.py
uv run pytest tests\test_handbrake_batch_executor.py tests\test_handbrake_adapter.py tests\test_handbrake_batch.py tests\test_handbrake_batch_cli.py
uv run ruff check mkv_episode_matcher\media\handbrake_batch_executor.py tests\test_handbrake_batch_executor.py
uv run ruff format --check mkv_episode_matcher\media\handbrake_batch_executor.py tests\test_handbrake_batch_executor.py
uv run pytest
```

Results:

- latest full suite: 340 passed with five existing third-party audio deprecation
  warnings;
- intro-start/preferred-stream focused suite: 25 passed;
- saved-report mapping focused suite: 15 passed;
- disc-sequence focused suite: 9 passed;
- TV-organization focused suite: 7 passed;
- HandBrake batch and adapter focused suite: 38 passed;
- transcript/audio/evidence focused suite: 28 passed;
- Gemini/catalogue focused suite: 35 passed;
- evidence/catalogue/Gemini focused suite: 26 passed;
- Gemini/catalogue focused Ruff and formatting checks: passed;
- `plan-gemini-unmatched --help`: passed;
- `build-unmatched-bundle --help`: passed;
- no live Gemini request was made and no transcript text was written to a
  durable plan report;
- final focused configuration suite: 51 passed;
- focused Ruff check and frontend TypeScript/Vite build: passed;
- legacy migration: moved the TMDb and OpenSubtitles fields without displaying
  values and sanitized the old JSON;
- credential status command: passed and confirms TMDb and all three
  OpenSubtitles fields are configured; Gemini fields are not configured;
- `npm ci` reported ten dependency audit findings (one low, one moderate, eight
  high); no automatic audit fix was run.
- title-selector/preflight focused suite: 15 passed;
- selector Ruff and formatting checks: passed;
- combined preflight/title/audio planning suite: 24 passed;
- media-planning Ruff and formatting checks: passed;
- FFprobe runner focused tests, Ruff checks, formatting check, and CLI help:
  passed;
- parallel rip-worker tests confirm concurrent drives, sequential titles within
  each drive, manifest-order results, and drive-failure isolation;
- saved-report regression, reported by ordinal only:
  - the known-good control selected six episode titles;
  - the four-title episodic report selected four;
  - three episodic reports selected four or three individual titles and
    excluded one aggregate title each;
  - one mixed-content report selected three candidates, excluded six short
    extras, and retained two items for review.

## Read-Only Disc Preflight

With explicit authorization, five loaded optical discs were inspected
sequentially using MakeMKV's `info` action. Parsed and raw reports are stored in
the ignored `.mkv-preflight/` directory.

The scan found:

- 31 titles and 85 total streams;
- a clean episodic-disc pattern with four episode-length titles;
- combined-title candidates on other episodic discs that would create
  duplicates if ripped alongside individual titles;
- default English Dolby Digital 5.1 plus secondary English Dolby Digital stereo
  on the relevant episode-length titles;
- MakeMKV structural warnings on the episodic discs that must be retained and
  evaluated during any future read or rip.

The metadata supports explicit audio-stream selection. It does not establish
that the 5.1 stream is silent.

A subsequent read-only scan inspected a known-good episode-matcher control disc.
It exposed six uniform episode-length titles, each with one default English
Dolby Digital 5.1 track, and MakeMKV reported no warnings. Because this disc is
known to match successfully, multichannel audio alone cannot explain the
earlier matching failures. Future audio diagnostics must distinguish stream
selection, channel layout, signal level, dialogue content, transcription
quality, and match-scoring failures.

## Current Limitations

- Title planning is heuristic and currently uses runtime relationships plus
  available stream metadata. Explicit expected-count/runtime constraints are
  supported, but segment maps and playlist identity are not yet used.
- Ambiguous runtime clusters can still contain feature-length extras or
  alternate cuts; `review` and the absence of any executor are deliberate.
- The first authorized MakeMKV run completed all 18 planned titles with no fatal
  event or queue pause. The output contains 18 non-empty MKVs totaling 16.27
  GiB.
- The constrained FFprobe runner has been exercised against completed staging
  outputs. Sanitized reports contain runtimes and stream metadata without
  source filenames.
- The known-good Dragons control completed a matcher dry run with explicit show
  and season context: six matches, zero failures, S01E08 through S01E13, with
  confidence from approximately 0.729 to 0.869. No media was renamed.
- The current rip-manifest builder still emits ordinal top-level staging
  folders. A reviewed media-context plan must be added before future manifests
  emit `TV Shows/<Show>/Season XX/...` paths.
- Future manifests now include unique staging basenames. This completed
  18-title batch retains its original MakeMKV filenames because no retroactive
  rename was authorized.
- Audio planning remains separate from execution. A bounded, explicit
  `diagnose-transcript` command now supports an absolute FFprobe audio-stream
  index, extracts a temporary 16 kHz mono WAV, measures mean/peak signal,
  transcribes at most 60 seconds, saves dialogue-free metrics, and removes the
  WAV.
- Episode identification and renaming are still coupled in parts of the current
  application.
- No durable job store, cancellation API, pause state, or restart recovery
  exists.
- No deterministic content fingerprint or safe dedup executor exists.
- No Plex/Jellyfin organization planner exists.
- A legacy HandBrake automation script was located and reviewed. It must not be
  executed: it recursively discovers files, deletes suspiciously small
  outputs, reuses a shared temporary filename, retries Gemini indefinitely, and
  performs weak post-encode verification.
- A safe HandBrake adapter now exists in
  `mkv_episode_matcher/media/handbrake.py`. It accepts one explicit MKV and one
  explicit staging destination, defaults to plan-only operation, requires a
  separate `--confirm-transcode` execution boundary, refuses existing output
  and partial files, and never discovers media recursively.
- The adapter uses an explicit AMD VCN encoder rather than trusting GUI preset
  names. Inspection showed that several existing presets containing “AMD” or
  “VCE” currently resolve to the CPU `x264` encoder. The default adapter profile
  is `vce_h265`, hardware quality preset, CQ 26, source-audio passthrough plus a
  256 kbps AAC Pro Logic II compatibility track, retained subtitles, chapters,
  and metadata.
- HandBrake output is written to a unique partial MKV, checked with FFprobe for
  positive size/duration, one expected video stream/codec, and audio, and only
  then promoted to the reviewed staging filename. Failed or unverified partial
  output is preserved for diagnosis. Redacted process and JSONL event logs are
  retained.
- `plan-handbrake` and `execute-handbrake` expose the planning and confirmed
  execution boundaries. One explicitly authorized 60-second Dragons sample
  completed through the adapter using AMD VCE H.265. FFprobe verified HEVC
  video, original AC-3 5.1 audio, AAC stereo compatibility audio, one VobSub
  subtitle stream, and a valid duration. The source remained present and
  unchanged in size. The output display aspect ratio is standard 16:9; a human
  visual/audio spot-check is still required before any full-episode encode.
- The legacy Gemini history documents a *Faerie Tale Theatre* workaround:
  normalize numbered disc labels to the base series title for Riplex/DVDCompare,
  then use an introduction-focused Whisper/LLM fallback when conflicting
  subtitle episode orders defeat ordinary matching. This remains a plan, not an
  authorized rename executor.
- Twelve *Faerie Tale Theatre* files are staged in the series `Unmatched`
  folder. Each has AC-3 5.1 and AC-3 stereo audio and no embedded subtitles, so
  OCR is not available for these files. A same-window 60-second pilot produced
  the same clear 26-word Whisper transcript from both tracks. Stereo measured
  about 7 dB louder (`-31.5 dBFS` mean versus `-38.4 dBFS`) and is therefore the
  preferred diagnostic track; 5.1 remains a usable fallback.
- Cross-season BM25/window retrieval was implemented and tested. Applying it to
  6,422 windows from 124 cached SRT files produced weak, conflicting results;
  fuzzy partial scores also clustered around 51%. The cache is therefore not
  trusted as identity evidence for this set. Introduction-focused stereo
  Whisper samples, distinctive dialogue/plot evidence, and two independent
  aired-order episode guides produced the following path-redacted review plan:

| File ID | Proposed aired-order identity | Evidence |
| --- | --- | --- |
| disc-03-title-000 | S04E02 — The Snow Queen | evil-goblin introduction and episode synopsis |
| disc-03-title-001 | S04E03 — The Pied Piper of Hamelin | exact Willie/holiday dialogue reference |
| disc-03-title-002 | S04E04 — Cinderella | story and protagonist named in transcript |
| disc-03-title-003 | S04E05 — Puss in Boots | exact wealth-and-station dialogue reference |
| disc-04-title-000 | S03E05 — Snow White and the Seven Dwarfs | queen and sewing introduction |
| disc-04-title-001 | S03E06 — Beauty and the Beast | Beauty and her sisters named in dialogue |
| disc-04-title-002 | S03E07 — The Boy Who Left Home to Find Out About the Shivers | Transylvania/fearless-son introduction |
| disc-04-title-003 | S04E01 — The Three Little Pigs | three brothers and oboe dialogue |
| disc-05-title-000 | S03E01 — Goldilocks and the Three Bears | story named in transcript |
| disc-05-title-001 | S03E02 — The Princess and the Pea | pea museum introduction |
| disc-05-title-002 | S03E03 — Pinocchio | marionette and carved-wood introduction |
| disc-05-title-003 | S03E04 — Thumbelina | magic seed, tulip, and tiny girl dialogue |

  These are proposals only; no media was renamed, moved, overwritten, or
  transcoded by the Theatre diagnostics. The authoritative metadata catalogue
  also contains S04E06 `The Emperor's New Clothes` and S04E07 `Grimm Party`;
  neither is assigned to this reviewed disc set.
- The owner selected aired order on 2026-07-30. The reviewed Theatre proposal
  therefore targets `Season 03` and `Season 04`; TVDB DVD order will not be used
  for this set.
- Legacy scripts still contain unsafe behavior and must not be executed.
- No literal active API-key assignments were found in the repository or the
  previously reviewed legacy-script set. The recognized values discovered in
  the old application JSON were migrated opaquely and are now configured
  through `.env`.
- The migrated credentials have not been tested against live provider APIs in
  this change. Runtime authentication errors will trigger the new replacement
  flow.
- Gemini keys have safe storage metadata and the bounded adapter supports
  automatic recovery. Evidence collection and offline request planning have
  been exercised, but no live Gemini request has been authorized or made.
- Zero Gemini API calls have been made. The only provider call in this phase
  sent reviewed show ID 4603 to TMDb and returned 27 aired episodes.
- The first sandbox-blocked TMDb catalogue attempt escaped the redacted error
  boundary and Rich rendered configuration locals in console diagnostics. No
  values are recorded here. `_tmdb_get_json` now converts request failures to a
  redacted `ApiServiceError`, with regression coverage. Because configured
  credentials appeared in that console traceback, they must be treated as
  exposed and rotated at their providers.
- The frontend dependency audit has ten findings that need a separate review;
  dependency versions were not force-upgraded in this change.
- One explicitly authorized full-episode Theatre canary completed through the
  safe batch executor on 2026-07-30. It used the reviewed AMD VCN H.265 CQ 26,
  selective-decomb, NLMeans ultralight/film, stereo-first plus original
  surround profile. FFprobe verified a 2,873.109-second HEVC MKV with two audio
  streams, zero subtitle streams, and a final size of 536,278,719 bytes before
  promotion into encoded staging. The source remained unchanged. The invocation
  was capped at one job/one worker; eleven manifest jobs remain pending and were
  not started.
- The existing FastAPI/React application was inspected for the proposed
  automatic disc control plane. It currently has folder scan/match/system
  routes, a single broadcast WebSocket, an in-memory matching-job dictionary,
  and a manual folder-oriented frontend. It does not yet have durable jobs,
  drive monitoring, disc orchestration routes, HandBrake profile management,
  restart recovery, or safe non-loopback authentication.
- The existing Settings page already edits cache/matcher/provider settings and
  accepts write-only replacements for TMDb and OpenSubtitles credentials. Its
  GET response exposes only blank secret fields plus configured status and
  official management links. The update endpoint still accepts a free-form
  dictionary and the application has no typed settings for rip staging,
  encoded staging, Jellyfin TV/movies, external tools, automation defaults,
  Gemini status, or path-role conflict validation.
- Web orchestration requirements now specify a valid saved default that
  progresses automatically when no UI choice is made. The UI is an optional
  per-disc override and monitoring surface. The planned profile builder covers
  AMD VCN/VCE, NVIDIA NVENC, Intel Quick Sync, and CPU encoders using detected
  HandBrakeCLI capabilities, immutable profile versions, selectable
  quality/filter/subtitle settings, and four guarded audio-layout policies.
- Windows packaging now requires an installer-time, opt-in desktop shortcut.
  The shortcut will start the installed local web server and open its dashboard,
  while a repeated launch will reuse an already healthy server. It will not arm
  disc automation merely by launching; when the owner has already enabled a
  persisted automatic policy, server startup will activate monitoring under
  that policy. It will contain no repository, virtual-environment, credential,
  or media paths. The current repository has a PyInstaller build path and
  executable entry point but no reviewed installer implementation yet.
- A plan-only special-feature classifier was added for bonus discs. It consumes
  an explicit saved MakeMKV inventory and a reviewed provider-neutral feature
  catalogue; it performs global one-to-one runtime assignment and proposes
  Jellyfin-recognized extras folders. Catalogue-matched individual features are
  recommended for a future reviewed rip manifest. Play-all/summed titles,
  metadata-duplicate candidates, menu-length clips, and unmatched titles are
  held for review. Equal-best runtime assignments are now held as explicit
  ambiguities rather than being assigned arbitrarily. Multi-audio titles carry
  a preserve-all-source-streams policy. It has no provider call, subprocess,
  disc access, media discovery, or execution command.
- `mkv-match plan-special-features` exposes that path-redacted planning boundary.
  Synthetic coverage includes a mixed bonus disc, play-all filtering, Jellyfin
  folder mapping, metadata duplicates, malformed catalogues, CLI redaction, and
  a global-assignment case where greedy nearest-runtime matching fails.
- The explicitly authorized read-only Parent Trap bonus-disc preflight completed
  successfully. It found 14 titles and 31 streams with no scan warnings and
  saved its sanitized inventory under the ignored preflight area. No title was
  ripped and no media or disc state was changed.
- The saved inventory demonstrated why the generic episode selector is unsafe
  for bonus discs: it labeled three similarly timed documentaries as episodes.
  It also contains two distinct 99-second titles that runtime evidence cannot
  distinguish and one 144-second title with four stereo audio streams.
- The catalogue contract is now release-aware and source-referenced. It
  distinguishes standalone, multi-audio, menu-bound, audio-only, still-gallery,
  and unknown feature representations, so interactive DVD material is not
  falsely reported as a missing MKV title.
- A reviewed 2005 Region 1 2-Movie Collection fixture was added using public
  review and library-record evidence. Against the saved scan it proposes eleven
  strong matches, retains both 99-second titles as a two-candidate ambiguity,
  identifies the 144-second four-stream Sound Studio title with preserve-all
  audio, holds the seven-second item as a menu candidate, and reports the other
  documented Sound Studio scene as missing.
- Unidentified plausible extras now carry a plan-only Jellyfin fallback policy:
  after a future authorized diagnostic rip and content fingerprint, they may be
  proposed under `extras` with a neutral fingerprint-derived name. This path
  does not depend on Riplex, a web catalogue, Gemini, OCR, or transcription.
- `mkv-match plan-special-feature-rip` now creates a digest-bound,
  path-redacted, non-executable diagnostic manifest from one saved inventory
  and reviewed catalogue. It includes plausible unresolved titles, preserves
  candidate IDs and multi-audio policy, excludes menu/play-all candidates by
  default, and schedules post-rip evidence collection. It has no drive binding,
  MakeMKV command, media-library destination, or execution authority.
- The Parent Trap saved-data diagnostic manifest contains 13 candidates,
  including two ambiguous titles and one preserve-all multi-audio title. The
  seven-second menu candidate is excluded. The manifest is retained only under
  the ignored preflight area; no disc or media operation occurred.
- `mkv-match bind-special-feature-rip` now verifies the reviewed diagnostic
  SHA-256, complete title-inventory signature, and every selected title's
  runtime, size, and audio-stream count. It emits a distinct bound-manifest mode
  that remains unauthorized and is rejected by the episode rip loader.
- A newly authorized sequential read-only Parent Trap preflight completed with
  14 titles, 31 streams, and no warnings. The first background launch failed
  argument parsing before disc access; the corrected launch completed and
  retained fresh diagnostics. The fresh inventory matched the saved diagnostic
  source exactly.
- The immutable bound plan contains 13 titles totaling 5,520,734,208 estimated
  bytes. It retains the two runtime ambiguities and the four-stream
  preserve-all job, and excludes the menu candidate. Its SHA-256 is
  `753fe298f2d5eaa1c8de7e3383fe469db21f181ac034850d909213969775a4e0`.
  No rip or other media mutation occurred.
- Focused special-feature/title tests pass 22 tests, and the six new diagnostic
  manifest tests also pass. Coverage includes equal-runtime ambiguity, release
  provenance, representation types, unmatched fallback, multi-audio
  preservation, menu/play-all exclusion, collision refusal, CLI behavior, and
  absence of execution fields. Four binder tests cover digest, substituted-disc
  rejection, drive/title binding, and episode-loader isolation. The full
  362-test suite
  passes after these safeguards. Five third-party audio
  deprecation/fallback warnings are pre-existing.

## Unresolved Decisions

- Directly reuse Riplex modules or reimplement selected behavior behind local
  interfaces after license/API review.
- Define runtime thresholds separately for movies, standard episodes, long-form
  episodes, combined titles, and extras.
- Decide whether combined titles are always excluded when their component
  titles are present.
- Define the unique staging root and free-space policy.
- Select Plex, Jellyfin, or dual-target organization rules.
- Decide the final per-resolution quality policy after short AMD VCN sample
  encodes; the source remains authoritative until a verified staged encode is
  separately approved for organization.
- Define quarantine duration and the future approval model for deletion.
- Define structured log retention and redaction rules.
- Choose SQLite schema and job-state transitions before orchestration work.
- Choose the initial conservative built-in HandBrake defaults for NVIDIA,
  Intel, and CPU after capability discovery and short samples on hardware that
  is actually available. Do not infer equivalent quality values across encoder
  families.
- Define how long an inserted disc waits for an optional per-disc override
  before the saved default is snapshotted and ripping begins. The default must
  remain fully automatic when no choice is made.
- Define authentication and TLS/network exposure before allowing phones or
  other non-loopback clients to control executors.
- Finalize safe directory-role rules for initial browsing, rip staging, encoded
  staging, and Jellyfin TV/movie roots. Merely configuring a library path must
  not authorize encoding or organization into it.
- Choose the exact Parent Trap edition/region in DVDCompare before treating its
  listed extras as authoritative. The current evidence supports the 2005
  Region 1 2-Movie Collection, but catalogue adapters still need a general
  scored release-selection workflow rather than title-specific rules.
- Select the Windows installer technology and stable installed-launcher
  behavior. Uninstall must preserve user configuration, logs, manifests, and
  media, and automatic launch at Windows sign-in is out of scope unless
  separately requested.

## Exact Recommended Next Step

The first Parent Trap special-feature run completed and verified titles 0-2,
then stopped at title 3 after a hardware-level medium read error. The disc was
relocated and a targeted read-only inventory completed with 14 titles, 31
streams, and no warnings. No media process remains active.

A generic saved-data-only resume planner is now implemented and focused safety
tests pass. It validates the original bound digest, original inventory, fresh
metadata-identical inventory, and prior append-only event log; excludes only
explicitly completed jobs; and routes every unfinished job to a new
collision-refusing staging directory. Future MakeMKV error messages also redact
embedded hardware identity. Existing evidence logs and all completed or partial
outputs remain untouched.

The exact 10-title resume was authorized and completed successfully. Titles
3-12 produced 3.53 GiB of verified staged output with no fatal or paused event.
Together with the first run, all reviewed titles 0-12 have now completed;
excluded title 13 remains unripped. The earlier title-3 partial and completed
outputs remain untouched, and nothing was renamed, moved to a library, deleted,
ejected, or transcoded.

Performance observation: the current safety executor launches one
`makemkvcon` process per title. Each invocation reopens and rescans the disc,
causing roughly two to five minutes of overhead per title even for short
features. This is materially slower than the MakeMKV GUI, which opens the disc
once and writes multiple selected titles. The next ripping-engine step is to
research and synthetic-test a single-open selected-title batch adapter (or a
strictly bounded all-eligible-titles strategy) while retaining the immutable
selection manifest, collision-refusing per-file finalization, verification,
append-only events, STOP behavior, and resumability. Do not experiment against
a physical disc until command construction and saved-output parsing are tested.

A single-open batch foundation now exists in
`disc/batch_ripper.py` but is deliberately not exposed through the CLI. The
installed MakeMKV help documents `mkv <source> <title-id> <destination>` and
does not document arbitrary multi-title selection. The experimental adapter
uses the conventional `all` selector only when a complete saved inventory
proves one `--minlength` cutoff selects exactly the authorized title set; all
other subsets fall back to refusal and the existing per-title executor. It
also refuses collisions, unsafe or duplicate source output names, multiple
drives, and library destinations; verifies the exact output-name set and
sizes; and preserves partial or unexpected files on failure. It supports
redacted events, STOP, timeout, fatal-error, and keyboard-interrupt handling.
Eight focused batch tests pass, focused batch/ripper checks pass 24 tests, and
the full suite passes 377 tests with five pre-existing audio warnings. No disc
or real media was accessed while initially developing this adapter.

The owner then authorized read-only FFprobe inspection of the exact 13 verified
Parent Trap MKVs. Manifest-derived paths were checked first; all were distinct,
present, and nonempty, and the successful resume copy of title 3 was selected
instead of the preserved failed partial. Sequential FFprobe inspection
completed for titles 0-12 with sanitized, path-free reports. Titles 0-11 each
contain one audio stream; title 12 contains four stereo audio streams.

Saved-runtime comparison retains strong catalogue matches for titles 0-3 and
5-10. Titles 4 and 11 are both approximately 102 seconds and remain ambiguous
between the short song and production-gallery entries. Title 12 is
approximately 145 seconds with four audio streams and remains a multi-audio
Sound Studio representation requiring content evidence before final naming.
The next special-feature evidence step should be narrowly limited to titles 4,
11, and 12: plan collision-refusing contact sheets/OCR for all three and
audio-stream-specific sampling for title 12. That requires new explicit
authorization to read those exact MKVs and create derived evidence; the
FFprobe authorization does not authorize extraction or transcription.

The owner subsequently authorized concurrent derived-evidence collection for
titles 4, 11, and 12. A generic explicit-file evidence adapter was implemented
and synthetic-tested. It preflights all collisions, runs at most three
independent items concurrently, creates 3x2 contact sheets, stores OCR text
privately, extracts only authorized audio streams to short WAV samples, and
returns path/dialogue-free metrics. The first run completed titles 4 and 11
but isolated a Windows text-decoding failure on title 12 after its contact
sheet. UTF-8 replacement decoding was added and regression-tested; title 12
then completed in a new evidence directory with four distinct audio samples.
The failed directory was preserved and nothing was overwritten.

Visual review resolves title 4 as `Production Gallery` and title 11 as
`Let's Get Together`. Title 12 is `Sound Studio: The Girlfriend`; all four
audio samples are distinct, consistent with the reviewed disc description's
dialogue-only, music-only, effects-only, and composite options. The catalogued
`Sound Studio: Twin's Revenge` remains absent from the MakeMKV title inventory
and must be recorded as missing/menu-or-PGC-addressed evidence rather than
forced onto another title. No Gemini call was needed.

A saved-data-only minimal single-open physical-validation plan was also
generated. It proposes `selector=all`, `--minlength=1056`, and exactly titles
0 and 6, estimated at 1,753,133,056 bytes (about 1.63 GiB), under isolated
`batch-validation/ac04b9bd198758b3` staging. The manifest remains
`execution_authorized: false`. A synthetic binder/executor now enforces the
exact digest and title count, fresh inventory identity, collisions, free
space, STOP/timeout, and redacted logs before calling the experimental batch
adapter.

The owner authorized a fresh read-only scan followed by that exact two-title
physical validation. The scan remained metadata-identical with 14 titles, 31
streams, and no warnings. One MakeMKV process opened the drive once and
reported `Copy complete. 2 titles saved.` with return code zero. The initial
post-run verifier deliberately refused promotion because MakeMKV renamed
original title 6 from the inventory suffix `_t06` to selected-output ordinal
`_t01`. Both outputs were preserved without rename, move, overwrite, deletion,
or transcode.

The generic adapter was then hardened to require a strict inventory `_tNN.mkv`
suffix matching each original title index, sort selected titles by original
index, and predict contiguous batch ordinals. A new public read-only verifier
confirmed the preserved output set and sizes exactly: title 0 mapped
`C6_t00.mkv` to `C6_t00.mkv`, while title 6 mapped `D2_t06.mkv` to
`D2_t01.mkv`. The append-only run log retains the original refusal followed by
a path-free post-validation success event. The physical single-open behavior
is therefore validated without a rerip. The preserved validation outputs must
not be moved or deleted without separate authorization.

The single-open adapter is now integrated into the regular `plan-rip` and
`execute-rip` boundary. New manifests contain only a path-redacted inventory
signature, selected title indexes, eligibility, and the exact cutoff; private
MakeMKV output names are not serialized. `execute-rip --fresh-inventory
<report.json>` binds an explicit metadata-identical fresh report and selects
single-open execution only for eligible drives. Changed reports stop before
execution. Ineligible drives, older manifests, and drives without an explicitly
supplied fresh report retain per-title execution.

The new per-drive orchestrator preserves parallel-across-drives operation and
isolates drive failures. It never retries a failed single-open operation by
title because that could duplicate partially written media. Exact verified
batch outputs may follow the already-authorized manifest finalization into a
flat `Season XX` folder only after all final collisions are checked before
MakeMKV starts. Synthetic tests cover CLI routing, redacted proof persistence,
fresh-report mismatch refusal, eligibility fallback, selected-output ordinal
renumbering, flat-season finalization, collision refusal, no retry after batch
failure, and unaffected-drive continuation. No disc or media operation ran
during this integration work. The full suite now passes 413 tests with five
pre-existing audio warnings.

The first server handoff is now implemented as a saved-report-only preview.
`POST /rip/preview` accepts explicit preflight JSON paths, reviewed media
contexts, and an optional existing output root. It builds the rip manifest in
memory, rebinds those same reports, chooses single-open or per-title strategy
per drive, checks staging and final-name collisions, and returns only redacted
strategy data and relative destinations. It does not write a manifest, discover
or access a disc, invoke MakeMKV, create directories, or expose any execution
route. Every response states `execution_authorized: false`.

The web frontend now contains a preview-only `Disc Pipeline` screen. It accepts
explicit saved report paths, a canonical series/season, and an optional output
root, then displays selected strategy, exact runtime cutoff, title count,
estimated size, relative destination, collision state, excluded discs, and the
non-authorizing preview digest. It intentionally has no start or confirmation
button. Frontend lint and the production TypeScript/Vite build pass.
The full Python suite now passes 418 tests with the same five pre-existing
audio warnings.

The durable orchestration state boundary is now implemented with SQLite.
It persists opaque job IDs, immutable path-redacted previews, exact plan and
authorization digests, idempotency keys, and append-only events. It does not
persist report paths, output roots, commands, environments, credentials, or
disc labels. The tested states are `awaiting_review`, `authorized`, `queued`,
`running`, `pause_requested`, `paused`, `failed`, and `completed`. Invalid
transitions stop transactionally; retries with the same idempotency key do not
create another job or event; concurrent creation retries produce one job; and
reopening the database recovers the job and event history.

The API now exposes durable create/status/events plus exact-digest authorize,
start, pause, and resume controls. `start` currently performs only
`authorized -> queued`, and responses explicitly report
`executor_attached: false`; it does not invoke MakeMKV. The Disc Pipeline UI can
save a preview as a restart-safe review job and display its state, but it does
not expose physical authorization or execution controls. Frontend lint and the
production build pass.
The full Python suite now passes 424 tests with the same five pre-existing
audio warnings.

The private execution-binding and fake-dispatch boundary is now implemented.
Absolute saved-report paths, the output root, and media contexts are stored in
a separate local SQLite database and never returned in routine API responses
or public events. Durable job creation requires an output root and binds those
private values to the stable preview digest. The preview identity excludes its
volatile creation timestamp, so an unchanged saved plan can be revalidated
immediately before dispatch.

The dispatcher has no default physical executor. It accepts only an explicitly
injected executor, revalidates the private binding, fresh reports, exact plan
digest, review state, and destination collisions before atomically claiming a
queued job. Dispatch retries are idempotent, incomplete executor results fail
closed, and a process restart reconciles `running` or `pause_requested` work to
`paused` for output review. Tests use fake executors only.

All `/rip` routes now reject non-loopback clients and cross-origin/cross-site
requests. This is an interim local-control boundary, not phone authentication;
remote control remains disabled until a secure pairing/token design exists.
The web UI can preview and save a durable review job but still cannot authorize
or start a physical rip. API status continues to report
`executor_attached: false`.

Frontend lint and the production TypeScript/Vite build pass. The full Python
suite now passes 439 tests with the same five pre-existing audio warnings.

The thin production adapter from `BoundRipDispatch` to the existing guarded
parallel auto-orchestrator is now implemented and tested with an injected fake
queue runner. It requires an explicit MakeMKV executable, a new dedicated run
directory outside the media output root, a timeout, and an optional drive
bound. It preserves the existing per-drive strategy and refuses a result set
that does not exactly match every authorized job ID. It is not connected to
FastAPI, the web UI, or a default dispatcher executor, and no external program
was launched during its tests.

The synthetic end-to-end composition test now creates a durable job through
the real FastAPI router, records exact authorization, queues it, and hands it
through the private dispatcher and production adapter with a fake queue
runner. It verifies that routine HTTP responses contain neither saved-report
paths nor the absolute output root. The API still exposes no physical execute
route.

The exact recommended next step is the first live composition canary decision.
Before adding or invoking any physical execution composition root, inspect a
fresh saved inventory and present the exact immutable manifest digest,
physical drive/title set, title count and cutoff, output root, new run
directory, timeout, and maximum parallel-drive/job scope. Obtain explicit
authorization for that exact canary. If live execution is not yet desired,
continue instead with synthetic drive-monitor and automatic-policy state
transitions. Do not infer live authorization from prior implementation or test
approval.
Separately replace the stale configured HandBrake path through a safe
non-secret tool-path setting; the explicit validated path currently works.

Separately, rotate every configured credential that appeared in the
sandbox-blocked TMDb traceback. Store replacements only through the hidden
credential command; never paste them into chat or documentation.

Then generate a collision-checked Theatre aired-order rename/move plan that
places the reviewed files directly under `Season 03` and `Season 04`. Do not
execute that plan until it has been reviewed separately.

Separately, visually and audibly inspect the completed Dragons HandBrake sample
before planning a full-episode encode. Do not move encoded output into a media
library or discard any source yet.

## 2026-07-31 Reboot Recovery

A Windows reboot interrupted the active Theatre HandBrake batch. The durable
event log shows four verified completions, two active jobs terminated with the
same Windows `DBG_TERMINATE_PROCESS` code, and two later jobs that were
dispatched only after that systemic failure. Six previously completed Dragons
encodes also remain staged. No completed output was overwritten. Two Theatre
diagnostic partials remain preserved, and no media file was deleted, renamed,
moved, or transcoded during the recovery audit.

The HandBrake adapter now raises a typed process error containing a path-free
interruption classification. The batch executor stops dispatching new chunks
after Windows `DBG_TERMINATE_PROCESS`/`STATUS_CONTROL_C_EXIT` or POSIX
HUP/INT/TERM, while ordinary one-file failures remain isolated. Resume derives
attempt numbers only from the exact append-only event log, preserves every old
partial, and chooses a new collision-refusing `.retry-NNN.partial.mkv` path.
Unknown unlogged partials still block execution. A later restart accepts a
verified recorded final even when an older diagnostic partial remains beside
it; no partial is automatically removed.

Synthetic regression tests cover systemic interruption, suppression of the
next chunk, collision-safe retry, preservation of the prior partial, and
verified-final resume. The focused HandBrake suite passes 40 tests. The full
Python suite passes 442 tests with the same five pre-existing audio warnings.
Focused Ruff lint and format checks pass.

The exact recommended next step is a separately confirmed resume of only the
four unfinished Theatre jobs from the unchanged reviewed manifest and existing
run log, with two workers and no library placement. The executor must first
revalidate the manifest digest, all four recorded completed output sizes, all
source sizes, free space, and destination collisions. It must retain both
existing interrupted partials and create new retry partials. After all four
outputs verify, create a distinct collision-inspection plan before any move to
the media library. Parent Trap recovery remains a separate fresh-inventory
operation and must not be combined with this HandBrake resume.

## 2026-07-31 Pipeline Composition

The HandBrake-to-organization contract is repaired. Newly planned manifests
carry schema version 2, retain the original input basename in `source_name`,
and identify the encoded output with `destination_relative`. Version-1 batch
manifests remain accepted by the executor. TV organization now requires the
append-only HandBrake event log and encoded-output root, verifies the exact
manifest digest and every recorded completed output size, and refuses an
incomplete, missing, or changed encode. It no longer maps organization IDs to
the raw HandBrake inputs.

The local FastAPI control plane now has an explicit physical execute boundary
at `/rip/jobs/{job_id}/execute`. Preview, save, authorize, and queue remain
non-executing. Execute repeats the exact plan digest and authorized job count,
constructs a fresh production adapter from the explicit executable/new run
directory/timeout/parallel bound, revalidates private reports and collisions in
the dispatcher, and exposes no private paths in its response. The frontend now
supports review, exact authorization, queue, execute, status polling, PAUSE,
and STOP controls. Tests inject a fake queue runner; no external media program
or physical disc was accessed during this work.

The restartable four-stage pipeline engine now links `rip -> identify ->
transcode -> organize` with an immutable hashed JSON contract at every
checkpoint. Resume validates all completed contracts and begins at the first
unfinished stage. Failures retain only the exception type in the private
checkpoint. Concrete stage runners remain explicit; the engine never discovers
or launches media work by itself.

Special-feature execution is now also exposed through a guarded local API
boundary. It accepts only an exact bound manifest plus fresh saved inventory,
revalidates the digest and title metadata, requires the exact job count and a
separate confirmation, and delegates to the existing sequential,
collision-refusing executor. Tests continue to inject fake queue runners.

Focused Python tests pass for the pipeline, rip API composition, special-feature
executor, HandBrake batch/recovery, and organization handoff. Frontend ESLint
and TypeScript compilation pass. Vite packaging could not be rerun in the
current restricted sandbox because esbuild was denied its parent-directory
scan; the failure occurred while loading `vite.config.ts`, before application
bundling.

The next step is regression hardening: add direct API tests for execute refusal,
active PAUSE/STOP marker behavior, and special-feature API composition; then
run the full Python suite and package the frontend in a normal workspace. Only
after those checks should an exact live canary be presented for separate media
authorization.

## 2026-07-31 Serialized Downstream Queue

The pipeline now has a private durable item-level SQLite queue. Ripping retains
the approved one-worker-per-drive parallel model. All non-rip work shares one
global downstream claim: identification, any transcription/OCR/provider
fallback performed by identification, HandBrake, and organization cannot run
at the same time. Verified titles can enter identification as soon as their rip
finishes; they do not wait for other drives. Failed or review-required items
release the worker and do not block unrelated items. Pause prevents new claims,
retry returns only the selected item to its retained stage, and reboot recovery
requeues an interrupted item at the beginning of that stage.

The production rip adapter now has an exact completion sink. After result-set
validation it verifies each deterministic finalized MKV and size, writes a
private immutable verified-rip contract outside the media root, and admits it
to identification. The FastAPI rip execute composition installs this sink.
Routine job responses and queue events remain path-redacted.

Explicit stage adapters now exist. Identification invokes the existing matcher
in dry-run mode and produces a canonical episode contract without renaming.
Transcode consumes that contract, creates one collision-safe HandBrake job, and
produces a verified encoded-output contract. Organization consumes only that
verified output, refuses existing destinations, and verifies the final size.
Synthetic tests cover the complete adapter handoff, collision-safe queue
claims, global one-worker enforcement, review isolation, retry, pause, reboot
recovery, and verified-rip admission.

The web UI now polls and displays downstream queue state and exposes pause,
resume, and per-item retry. It states the one-global-worker resource policy.
Frontend ESLint and TypeScript compilation pass.

The downstream adapters are deliberately not started by a background worker
yet. Existing exact authorization covers MakeMKV only; it does not authorize a
particular HandBrake profile or library move. The exact next step is a combined
pipeline-authorization plan that binds the reviewed title/media IDs, HandBrake
profile and tool paths, encoded staging root, collision-checked library
destinations, move/copy policy, and job count. Once that immutable plan is
authorized, the server can attach the serialized worker and resume it safely
after restart without asking for each ordinary stage transition.
## Jellyfin resolution-version naming

- Post-transcode FFprobe verification now retains encoded width, height, and
  field order in the verified HandBrake result.
- The downstream transcode contract carries those verified fields to the
  organizer.
- Final television placement appends Jellyfin's required version delimiter and
  resolution label, such as ` - 1080p` or ` - 576i`, immediately before `.mkv`.
- The label describes the encoded output rather than the source or requested
  HandBrake profile.
- Existing exact resolution destinations still stop for review and are never
  overwritten automatically.
- A different version of the same episode also stops by default. Organization
  may place both resolution-labelled files only when the reviewed decision is
  passed explicitly as `allow_version_coexistence`; the encoded staging file is
  preserved while the item is held.
- Jellyfin's stable documentation explicitly describes this convention for
  movie versions. Jellyfin 12 adds episode-version support, while older servers
  may show resolution-suffixed episodes separately; the future conflict-review
  UI must therefore present version coexistence as an explicit user decision.

Focused verification:

- `uv run pytest tests/test_organizer.py tests/test_pipeline_adapters.py
  tests/test_handbrake_adapter.py tests/test_handbrake_batch_executor.py`
- `uv run ruff check` on the modified organizer, HandBrake, pipeline adapter,
  and focused test modules.

## Web settings and monitoring dashboard

- The Web settings page now exposes separate rip staging, encoded staging,
  Jellyfin television, and Jellyfin movie roots.
- It also exposes MakeMKV, HandBrakeCLI, FFmpeg, and FFprobe paths plus the
  default HandBrake profile name.
- Gemini primary and fallback credentials are now accepted by the existing
  secret-storage boundary and returned to the browser only as configured/not
  configured status with the provider management link.
- Riplex requires TMDb; its dvdcompare.net integration does not require another
  user API key.
- The pipeline page now polls recent durable rip jobs and serialized downstream
  queue items, showing disc and rip/identify/transcode/organize stage icons.
- The API explicitly reports whether automatic processing is requested and
  whether a background watcher is actually attached. It currently reports the
  watcher as unattached, so the UI cannot imply that an inserted disc is being
  processed.

Current limitation and exact next step:

- Implement the separately tested background disc watcher and combined pipeline
  authorization. It must bind tool paths, staging/library roots, HandBrake
  profile, disc-context fallback, conflict policy, and parallel-drive limit.
  Only then may `watcher_attached` become true and the automatic preference
  initiate read-only inventory scans and authorized collision-safe rips.

## Disc dashboard landing view

- The web application now opens on the Disc Dashboard instead of the legacy
  library-folder scan.
- Navigation labels the legacy matcher as Library Scan and the primary workflow
  as Disc Dashboard.
- The dashboard provides per-disc draft choices for TV episodes, movie, extras,
  or mixed main-title/extras content, plus the configured default HandBrake
  profile or a future custom profile.
- A synthetic-tested read-only drive watcher is now attached. `GET /rip/drives`
  returns only its cached, redacted slot state and never accesses hardware.
  `POST /rip/drives/refresh` requires an explicit confirmation and runs one
  MakeMKV `info disc:9999` discovery call; it never inventories titles or rips.
  The dashboard shows every returned drive as empty or loaded and performs no
  automatic physical polling merely because a browser is open.
- MakeMKV's invisible disabled placeholder records are filtered from the
  dashboard; visible physical drives remain listed even when their tray is
  empty.
- Some MakeMKV versions report additional enabled placeholder records with no
  drive description or device identifier. Those records are also filtered;
  a real empty optical drive remains visible because its hardware and device
  fields are present even when its disc label is blank.
- These choices remain UI drafts and are not execution authority. The next step
  is to add durable HandBrake profiles and bind each per-disc content/profile
  choice into the combined immutable pipeline authorization.
- Both per-disc controls now default to an explicit blank Automatic choice.
  Content hints are intended to change identification priority rather than act
  as hard filters: TV prefers episodic matching before movies; movie does the
  inverse; extras prefers special-feature classification; mixed evaluates main
  titles and extras; every preference may fall back only after its preferred
  strategy fails. A blank profile uses the configured default.

## HandBrake profile library and content hints

- A durable non-secret HandBrake profile library now exposes built-in AMD VCN,
  NVIDIA NVENC, Intel Quick Sync, and CPU x265 HEVC profiles. Custom profiles
  are validated and atomically stored outside the repository under application
  data; built-ins cannot be overwritten.
- The custom profile editor exposes every field accepted by the current safe
  HandBrake adapter: encoder family and compatible preset, constant quality,
  selective comb detection/decomb, reviewed content type, NLMeans preset and
  tune, a source audio layout preference, compatibility bitrate, stereo-first
  audio ordering, and subtitle retention. Preferences include disc default,
  stereo, 2.1, 5.1, 7.1, and highest channel count. Immediately before an
  authorized encode, a fixed FFprobe metadata query resolves the preference to
  the best available one-based source track for that individual file. It does
  not accept arbitrary CLI arguments that would bypass adapter validation.
  Audio profiles also accept ISO-639-2 language preferences and can retain all
  matching-language tracks, only the first matching track, or all languages.
  Language selection is resolved against the source metadata before the
  explicit HandBrake command is built.
  Profiles may retain only that preferred source stream or append every other
  source audio stream after the preferred stereo/original pair. Additional
  streams use codec copy where supported with the existing safe AAC fallback;
  no source stream can displace the reviewed preferred stream as the default.
  The profile editor also makes subtitle handling explicit: by default it
  retains all English (`eng`) subtitle/closed-caption tracks, with options for
  the first matching track, all languages, or none. The first selected track
  may be marked as the playback default. Burned-in text remains part of the
  video and is unaffected.
  Profiles also expose maximum resolution (source, 480p, 720p, 1080p, or
  2160p) and independent frame-rate policy. Source timing is the default;
  explicit VFR and fixed-rate PFR choices are separate. Built-in profiles can be customized into a new profile, and existing
  custom profiles can be edited or duplicated without changing the built-ins.
  Quality values can be set independently for 480p, 720p, 1080p, and 2160p;
  when resolution is left at source, a fixed FFprobe video-height query maps
  the source into the matching CQ bucket automatically. The general quality
  value remains a compatibility fallback if that metadata is unavailable.
  Selecting a profile for a disc also persists that profile ID as the local
  user's default for future newly detected discs. Choosing the automatic option
  leaves the saved default unchanged; this setting contains no secrets.
- The local `/rip/handbrake/profiles` API lists profiles and creates/replaces
  validated custom profiles. The settings page includes a profile creator and
  the Disc Dashboard selector lists the resulting library.
- HandBrake capability parsing and validation now recognize the four encoder
  families. Actual availability remains an execution-time requirement; no
  encoder or physical media was invoked while implementing this change.
- Optional `content_hint` and `handbrake_profile_id` fields are accepted in
  reviewed media contexts and preserved through private rip, identification,
  and transcode contracts. A selected profile is frozen by ID in the identify
  contract and an unavailable profile routes the item to review.
- The deterministic content policy establishes preferred-first fallback order
  for automatic, TV, movie, extras, and mixed choices. The existing TV adapter
  honors TV-first operation. Movie/extras/mixed-first items stop with a typed
  review code because dedicated movie and mixed/special-feature runtime
  identifiers are not attached yet; they are never silently sent through the
  TV matcher first.

Current limitation and exact next step:

- The drive-card choices cannot yet be safely attached by drive number because
  a disc can be swapped between refresh and execution. Implement automatic
  read-only inventory/job creation, bind the choices to the resulting complete
  inventory signature, and include that preference/profile binding in the
  combined pipeline authorization digest. Then attach dedicated movie and
  mixed/extras identification adapters before enabling no-touch execution.

## Windows optical-drive events

- FastAPI startup now attaches a hidden-window `WM_DEVICECHANGE` listener on
  Windows. Volume arrival/removal events are debounced and request exactly one
  cached MakeMKV drive-slot refresh; browser polling continues to read only the
  in-memory cache.
- The refresh coordinator never launches MakeMKV while a rip executor is
  attached. An event remains pending and one refresh runs after all active rips
  detach, preventing discovery/rip contention.
- FastAPI shutdown stops both the native message loop and refresh worker.
- Loaded-drive cards may display a sanitized MakeMKV disc volume label after a
  refresh. Hardware names and drive letters remain excluded, and labels are not
  written to orchestration jobs, events, or logs.
- A server started with discs already inserted still uses the existing manual
  read-only refresh for its initial snapshot. Subsequent Windows insertion or
  removal events refresh automatically.

## Spark handoff: current edge-case audit baseline (2026-08-01)

The latest safe troubleshooting pass was synthetic-only. It did not start
MakeMKV, HandBrakeCLI, FFmpeg, FFprobe, Whisper, OCR, or access physical discs
or media files. The focused command was run with a writable temporary root
because the default Windows pytest temporary directory is access-restricted:

```text
uv run --no-cache pytest tests/test_handbrake_adapter.py tests/test_handbrake_profiles.py tests/test_batch_ripper.py tests/test_rip_manifest.py --basetemp "G:\\CodexProject_MKV\\.pytest-troubleshooter-current"
```

Result: **78 passed**. Pytest emitted only two cache-path permission warnings
for the repository `.pytest_cache`; these did not affect test results.

The tested behavior includes encoder-family validation, CQ/resolution mapping,
audio default-language versus retained-language policy, missing-language
handling, subtitle selection policies, collision-safe MakeMKV staging, and
path-redacted planning. The broader queue/pipeline and HandBrake recovery
tests should be included in Spark's next synthetic audit.

Known follow-up: subtitle retention language selection is explicit, but a
separate language-aware subtitle *default-track* field and execution mapping
are not yet complete. Do not claim that choosing a subtitle language makes it
the playback default until that contract is implemented and tested.

Spark may run synthetic tests and static checks using a writable `--basetemp`
under `G:\\CodexProject_MKV`. Any live edge-case audit must first enumerate
the current drives, state the exact drives and read-only operation, and obtain
approval for that specific MakeMKV information scan. No live scan is implied
by this documentation or by passing tests.

## 2026-08-01 Synthetic Verification Cycle (post-merge, non-destructive)

Ran a broad synthetic verification pass with no media I/O.

- Command:

```text
uv run --no-cache pytest tests/test_handbrake_profiles.py tests/test_handbrake_adapter.py tests/test_handbrake_batch.py tests/test_handbrake_batch_executor.py tests/test_batch_ripper.py tests/test_pipeline_adapters.py tests/test_pipeline_queue.py tests/test_rip_dispatcher.py tests/test_content_policy.py tests/test_rip_api_composition.py tests/test_system_credentials.py tests/test_handbrake_batch_cli.py tests/test_organizer.py tests/test_organizer_cli.py tests/test_rip_manifest.py tests/test_rip_orchestrator.py tests/test_rip_preview.py tests/test_rip_execution_adapter.py tests/test_batch_validation.py tests/test_batch_validation_executor.py --basetemp "G:\CodexProject_MKV\.pytest-spark-audit"
```

Result:

- `182 passed`, `0 failed`.
- `pytest` cache warnings persist for `C:\Users\Owner\mkv-episode-matcher\.pytest_cache` path write restrictions.

- Static and frontend checks:
  - `uv run ruff check` (touched Python modules/tests): passed.
  - `uv run ruff format --check` (touched Python modules/tests): passed.
  - `Set-Location mkv_episode_matcher/frontend; npm.cmd run lint; npx.cmd tsc -b; npm.cmd run build`: passed.

No external process touched discs/media.

No synthetic defect findings were introduced by this cycle. Next recommended
step remains the combined live authorization and pipeline attachment work under an
explicit approved immutable batch.

## 2026-08-01 Drive-failure recovery and dashboard status

Implemented a synthetic-only, path-redacted recovery boundary for parallel rip
failures. The parallel orchestrator now retains verified results from unaffected
drives and the failed drive indexes without retaining media paths. Verified
results are admitted to the downstream queue even when another drive fails.

The durable failure event records an error category and opaque completed job
IDs. A confirmed retry revalidates the original private binding and authorized
plan, accepts only collisions belonging to those recorded completed outputs,
and dispatches only unfinished titles. A partially completed single-open drive
falls back to per-title execution. Unfinished titles restart from the beginning
in a new run directory; existing finals, partials, and unknown collisions are
never overwritten or removed.

The Disc Dashboard now shows the failed drive, categorized status, preserved-
partial warning, recommended actions, and a confirmed **Retry unfinished
titles** control. Recommendations cover timeout, I/O/connection, collision,
storage, interruption, and MakeMKV failures.

Verification was synthetic only. Focused Python tests passed (`22 passed`),
focused Ruff checks passed, and frontend ESLint, TypeScript, and production
build checks passed. No MakeMKV, HandBrake, FFmpeg, optical disc, or media file
was accessed. A future live retry still requires a new run directory and the
existing explicit execute confirmation.

## 2026-08-01 Manual startup-disc pipeline preparation

The Disc Dashboard now offers **Start pipeline for this disc** for a loaded
drive even when the disc was inserted before the HTTP server started. A
completed or failed historical job on the same physical drive does not hide
the option for a newly inserted disc.

The action requires a clear read-only confirmation, inventories only the
selected drive with MakeMKV `info`, saves private sanitized evidence, and
creates the normal durable `awaiting_review` job. Optional content and
HandBrake-profile hints are bound into that job. With no canonical identity
yet, the plan stages under `Unmatched`; identification remains responsible for
finding the final series or movie identity. The action does not itself rip,
transcode, rename, organize, delete, or eject media. The resulting exact title
plan must still pass the existing review, authorization, queue, and execute
controls.

Synthetic verification passed (`14 passed`) together with focused Ruff,
frontend ESLint, TypeScript, and production-build checks. No physical drive or
real media was accessed during implementation.

## 2026-08-01 Multi-drive preparation queue and stale-staging recovery

The dashboard preparation controls now accept multiple loaded drives into a
visible client queue. The user may queue every drive immediately; MakeMKV
information scans remain sequential so preparation does not contend for drive
resources. Each drive displays either `Queued for preparation` or the active
read status.

New web-prepared plans bind a random, validated staging-attempt ID into the
immutable media context. Re-preparing the same physical disc therefore uses a
new isolated staging directory and never overwrites or deletes an earlier
partial attempt. Stable final basenames retain the disc fingerprint and title
index, so a real finalized-output collision still requires deduplication
review.

The review UI now distinguishes staging-only collisions, explains that the
earlier attempt is preserved, hides authorization while review is unresolved,
and offers **Prepare fresh isolated attempt** on the corresponding drive. A
web-prepared job is labeled as automatically saved rather than presenting a
misleading save-only action.

Synthetic verification passed (`23 passed`) using the configured writable
pytest base directory. Focused Ruff, frontend ESLint, TypeScript, and
production-build checks passed. No real disc or media operation was performed.

Queue isolation was also hardened: both review-required and media-specific
failed downstream items now release the single global downstream worker and
the dispatcher continues with unrelated queued media. Queue-database failures
still propagate because scheduler integrity would then be unknown. Regression
coverage confirms a review item and a failed item can remain held while a
healthy item proceeds through identify, transcode, and organize to completion.

The dashboard review display is now explicitly drive-selected. Completing a
later disc preparation no longer replaces the plan currently being reviewed.
Every drive whose newest job is `awaiting_review` is rendered red with a
**Review this disc** action; failed drives are also red with their recovery
action. Selecting a drive loads that exact durable job and preview, labels the
review with the optical-drive number, and scrolls to its available decisions.
Other drive cards and queued work remain independent.

## 2026-08-01 Automatic bonus-disc fallback

The normal web-preparation path now makes the optional content hint effective
during title selection. Explicit **Extras / bonus disc** mode proposes a
conservative set of plausible bonus titles, and **Automatic** uses the same
fallback only when no episode cluster can be selected. **Mixed** combines the
episode and plausible-bonus sets.

The fallback is saved-inventory-only. It holds titles shorter than three
minutes as menu/navigation candidates and holds a longer title when its runtime
matches the sum of multiple plausible individual features. These rules create
only an `awaiting_review` proposal; they do not authorize execution and do not
replace the reviewed-catalogue special-feature workflow for final naming.
Preview drive cards identify when automatic bonus fallback was used so the user
does not mistake the proposal for an episode match.

Synthetic verification passed (`27 passed`) for manifest, preview, and drive-
preparation coverage. Focused Ruff checks and formatting checks passed, along
with frontend ESLint, TypeScript, and production build. No physical disc,
MakeMKV, FFmpeg, HandBrake, or real media file was accessed.

## 2026-08-01 Special-feature dashboard and downstream attachment

The Disc Dashboard preparation boundary now evaluates reviewed, provider-neutral
special-feature catalogues for Automatic, Extras, and Mixed discs. Catalogue
selection is saved-data-only and requires a uniquely best release with multiple
strong global runtime assignments; a weak or tied catalogue is never forced.
The proven Parent Trap release catalogue is packaged as the first built-in
catalogue. Additional reviewed catalogues can be installed as JSON files in the
local application-data `feature-catalogs` directory without changing planner
code.

When a catalogue is selected, the exact title indexes, catalog/release IDs,
library identity, feature assignments, audio policy, and evidence requirement
are stored in the immutable private media context. Rebinding and restart resume
therefore rebuild the same selection from the same fresh inventory. The public
preview labels this as `reviewed-special-features` but does not expose catalogue
paths or private media paths.

Verified strong feature matches now enter the serialized downstream queue with
Jellyfin-compatible movie extras destinations. The episode matcher is not
called for those items. Ambiguous, duplicate, or fingerprint-required items
stop at `special_feature_evidence_required`, release the global worker, and
allow unrelated items to continue; they are never assigned a guessed name.
Organization remains collision-refusing and appends the normal resolution
version label after a verified staged transcode.

Synthetic verification passed 80 focused catalogue, special-feature, rip API,
dispatcher, queue, and downstream-adapter tests. Focused Ruff checks and format
checks passed, as did frontend ESLint, TypeScript, and the production build. No
physical disc, real MKV, MakeMKV, FFmpeg, HandBrake, provider request, or
credential was accessed.

The awaiting-review dashboard language now describes outcomes rather than the
internal authorization transition. A collision-free review offers one primary
**Save these titles and add to rip queue** action and states explicitly that it
does not start MakeMKV, overwrite files, remove earlier attempts, or eject the
disc. Separate actions inspect the selected title list, keep the durable review
on hold, or perform a newly confirmed read-only inventory for a fresh review.
Authorization and queue admission are combined in the UI while the final
physical-rip confirmation remains separate.

The reviewed special-feature preview now keeps raw MakeMKV outputs in isolated
collision-safe staging instead of presenting a misleading
`TV Shows/Unmatched/Unmatched` final destination. Each planned title displays
its own reviewed feature name, Jellyfin extras category, and whether the match
is catalogue-backed or still needs evidence. The single-open cutoff is labelled
as MakeMKV's minimum title length and explained as a menu-title exclusion
boundary. Existing durable reviews remain immutable; these presentation and
staging corrections apply after creating a fresh review.

Regression coverage caught and repaired a stale per-title catalogue lookup
that initially repeated the last feature name for every title. Verification
passed 30 focused manifest, preview, and drive-preparation tests, focused Ruff
checks and format checks, frontend ESLint, TypeScript, and production build. No
physical disc, real media, or external media program was accessed.

## 2026-08-01 Collision choices and prior title outcomes

Collision review now offers two real plan operations. **Rip only missing
titles** derives a new immutable review containing only jobs whose planned
output and staging locations are absent. **Rip all titles again as replacement
copies** derives a new attempt for the full selection using a fresh isolated
staging namespace. Neither operation overwrites or deletes a completed file or
partial. Replacement of an older completed file remains a later, separately
confirmed organization decision after the new output verifies.

The downstream SQLite store now retains a path-redacted title outcome keyed by
the stable disc-inventory fingerprint and MakeMKV title index. A completed
identification records the episode/feature name, episode ID when applicable,
and relative Jellyfin name. Fresh disc reviews project those prior outcomes
back onto the matching title cards. Existing private pipeline contracts are
scanned lazily to backfill older outcomes when their verified-rip basename still
contains the inventory fingerprint. No media content is opened for this
backfill; missing or malformed contracts are ignored.

Verification passed 22 focused queue, API-composition, drive-preparation, and
preview tests, focused Ruff checks and formatting checks, frontend ESLint,
TypeScript, and a production build. No physical disc, media file, MakeMKV,
FFmpeg, HandBrake, provider, or credential was accessed.

Collision review now also distinguishes a deliberate replacement intent from a
non-destructive replacement copy. **Deliberately replace existing completed
files** records `replace-after-verification` in the private media context and
propagates that policy through identify and transcode contracts. It is not
deletion authority: the new output must verify before the UI presents the exact
old/new destination confirmation. Partials remain ineligible for overwrite.

**Use verified existing rips and restart matching** avoids unnecessary disc
work. It resets only items with an unchanged durable verified-rip contract to
the serialized identify queue. Titles without such proof are reported as
requiring verification and are not trusted merely because a staging path
exists. The action performs no optical-disc access and no media mutation.

Verification passed 37 focused queue, adapters, API composition, and manifest
tests, focused Ruff checks, frontend ESLint, TypeScript, and production build.
No physical disc, media file, or external media program was accessed.

The verified-existing-rip recovery action is now shown for every
`awaiting_review` job, including reviews whose newly isolated staging paths are
collision-free. Recovery validates that each durable verified-rip contract has
the same inventory fingerprint and MakeMKV title index as the current review;
a generic `disc-01-title-NNN` ID alone is never sufficient to reuse media.

Queued jobs now offer **Remove from rip queue and return to review**. This
changes only durable control state from `queued` to `awaiting_review`, preserves
the exact plan and authorization history, and performs no media or disc access.
Verification passed 19 focused orchestration, queue, and API tests plus Ruff,
frontend ESLint, TypeScript, and production build.

Title inspection is now actionable rather than a scroll-only control. It opens
checkboxes for every planned MakeMKV title, with select-all and clear controls.
The checked set can create a new immutable rip review or restart identification
for only matching durable verified-rip records. The selection endpoint rejects
duplicates, empty sets, negative indexes, and indexes outside the saved review;
it binds a fresh collision-safe staging attempt without changing the original
durable job. Verification passed 19 focused API, queue, and orchestration tests,
Ruff, frontend ESLint, TypeScript, and production build.

Existing staging files that predate durable verified-rip records now have a
safe recovery path. The UI first searches only the job's privately bound rip
staging root for each exact collision-safe planned basename and presents a
path-redacted candidate list. Missing, empty, or duplicate candidates remain
held. A separate explicit confirmation runs read-only FFprobe verification on
only the checked candidates, rechecks their sizes, and admits successful files
to the serialized identification queue without reripping, renaming, moving,
overwriting, deleting, transcoding, or reading the optical disc. The recovery
plan is digest-bound and is recomputed immediately before verification.

Verification passed 13 focused recovery, API composition, and pipeline queue
tests; focused Ruff; frontend ESLint; TypeScript; and the production frontend
build. No physical disc, real media file, MakeMKV, HandBrake, FFmpeg, or
FFprobe process was accessed during implementation testing. An already-renamed
file that no longer retains its unique staging basename is intentionally not
auto-discovered and still requires a future reviewed canonical-file recovery
flow.

Recovery also treats the ordinal `disc-XX` filename prefix as session-local.
An older staging basename may be reused when its 16-character inventory
fingerprint and three-digit title index exactly match the current reviewed
disc, even if its earlier disc ordinal differs. The recovered job is rebound to
that exact basename before verification. Different fingerprints or title
indexes remain ineligible, and two matching candidates remain an ambiguity
rather than being chosen automatically. Fifteen focused recovery, API, and
queue tests pass after this compatibility correction.

Legacy special-feature execution output is now recoverable without trusting a
filename alone. Recovery groups `special-<token>-title-NNN.mkv` files by their
reviewed-plan token and accepts a cohort only when exactly one token contains
the complete current title-index set and every file size equals the saved disc
inventory size. Incomplete earlier attempts are ignored; multiple complete
cohorts remain ambiguous. The zero-candidate UI now explicitly explains that
verification is disabled instead of silently returning from a button click.
Seventeen focused recovery, API, and queue tests, Ruff, frontend ESLint,
TypeScript, and the production build pass. Diagnosis inspected filenames and
sizes only; it did not open media content or invoke an external media tool.

The initial special-cohort recovery check was too strict because MakeMKV's
saved inventory size is an estimate: the observed completed filesystem sizes
were consistently about two to three percent smaller. Recovery now permits a
bounded five-percent estimate variance (with a one-MiB minimum allowance) only
inside the already strict unique-complete-cohort rule. It still requires the
exact complete title-index set and a single plan token; zero-byte files,
larger size discrepancies, incomplete cohorts, and competing complete cohorts
are rejected. The active 13-title saved review now resolves through metadata
to 13 candidates, zero missing, and zero ambiguous. Nineteen focused tests and
Ruff pass. No MKV content or external media process was accessed.

Automatic mode now starts a durable identification-only background consumer
after the matching engine finishes warming. It claims only `identify` items,
one globally at a time, and continues past media-specific review/failure states
so unrelated items are not blocked. Successful identification advances an item
to `transcode · queued`; the worker deliberately cannot claim transcode or
organize because no combined HandBrake/library authorization is yet bound to
these queue items. Shutdown signals and joins the worker. Twenty-two focused
queue and application tests plus Ruff pass. No real identification, external
media process, transcode, or organization operation ran during testing.

The downstream dashboard now always shows separate **Start / resume authorized
work** and **Pause downstream queue** controls. Start/resume clears the durable
pause flag and lets the background worker claim permitted work; it never
bypasses a per-item review state or grants a missing stage authorization. When
items are `transcode · queued`, the UI explicitly states that they are ready
but held until the HandBrake profile, encoded staging root, and downstream
authorization are reviewed. Frontend ESLint, TypeScript, and the production
build pass.

Queued transcodes now have a concrete two-step web authorization flow. **Review
and authorize transcoding** builds a path-redacted SHA-256 identity over the
exact currently queued media IDs, immutable identification-contract hashes,
saved default HandBrake profile settings, configured HandBrake/FFprobe tools,
and encoded staging root. The review displays the exact item count, IDs,
profile, and digest without exposing private paths. **Start this exact
transcode batch** recomputes that identity, refuses any changed item set or
configuration, and starts a daemon batch restricted to those exact IDs and the
`transcode` stage. Queue claiming remains globally serialized. The existing
one-file adapter preserves collision partials and verifies with FFprobe;
organization remains unauthorized and cannot run through this batch.

Fifteen focused authorization, queue, and API tests, Ruff, frontend ESLint,
TypeScript, and the production build pass. No HandBrake, FFprobe, real media,
or library operation ran during implementation testing.

The transcode review now presents the full saved HandBrake profile list and
binds the user's selected profile into both the preview digest and execution
request. Changing the selector invalidates the prior preview. Tool failures are
reported separately for HandBrakeCLI and FFprobe, and the warning provides a
direct **Open settings to fix tools** action. Current public configuration
diagnostics showed that FFprobe exists and the custom default profile exists,
but the saved HandBrakeCLI path no longer points to a file; no configuration
value was changed automatically. Backend profile tests, Ruff, frontend ESLint,
TypeScript, and the production build pass.

Windows tool discovery now searches for an exact `HandBrakeCLI.exe` in PATH,
the standard installed location, and bounded download-folder roots (maximum
three directory levels and 2,000 directories, without following symlinks).
This supports portable HandBrakeCLI bundles without indexing arbitrary media
or launching the executable. The currently available portable CLI was found
under the user's downloads area; the UI still requires **Save Configuration**
before replacing a stale saved path. Five focused discovery/configuration tests
and Ruff pass.

Web-saved non-secret executable paths now remain authoritative after reload.
Previously, a stale `.env` or process-level tool path was applied after JSON on
every load, making a successful Settings save appear to revert. Environment
values remain authoritative for credentials, but MakeMKV, HandBrakeCLI,
FFmpeg, and FFprobe environment paths now act only as defaults when the JSON
configuration has no saved value. Eleven focused configuration and
special-character tests plus Ruff pass. No `.env` file was opened or changed.

Settings now validates every nonempty executable path before saving. The path
must be an existing file with the expected MakeMKV, HandBrakeCLI, FFmpeg, or
FFprobe basename. A failed check returns field-specific errors, highlights the
affected input, and does not save configuration or submitted credential
updates. Validation never launches the executable. Ambiguous special-feature
queue items no longer offer a misleading retry button: the dashboard explains
that contact-sheet/OCR/audio evidence or a reviewed manual assignment is
required. New disc reviews now retain candidate feature IDs for that future
resolution action. Nine focused save/review tests, Ruff, frontend ESLint,
TypeScript, and the production build pass.

The queue action is now explicitly **Assign a HandBrake profile to this queued
batch**, separate from the tool-path repair action. A reviewed batch profile
has execution precedence over any older per-disc profile frozen in an
identification contract, and the verified transcode contract records the
actual override profile ID. This prevents a newly selected queue profile from
being silently ignored. Ten focused pipeline-adapter and authorization tests,
Ruff, frontend ESLint, TypeScript, and the production build pass.

Ambiguous bonus-feature items now expose three durable review choices in the
disc dashboard: use Gemini after local evidence, choose a name manually, or
leave the item on hold. The saved non-secret setting **Use Gemini as the final
fallback** automatically records the Gemini path when local identification
ends in `special_feature_evidence_required`; it does not contact Gemini. The
item remains at identify/review and therefore cannot reach HandBrake with an
unknown name, while unrelated queue items continue. The UI presents an
external-data warning and the queue event log records only the path-free review
code. The next implementation step is the separately guarded local-evidence
collector and schema-constrained provider execution boundary; no live Gemini
request was made by this change.

The downstream dashboard now displays the matched title from the private
identify/transcode contract while retaining the stable `disc-XX-title-NNN`
recovery ID as technical information. Queued organization items explain that
their encode is verified in staging and has not yet been moved. A new
path-redacted organization preview and exact authorization endpoint binds the
queued contract digests, configured TV/movie roots, resolution-version target,
and collision set. The UI exposes **Review placement into Jellyfin** and refuses
the batch when an exact destination already exists. A separate saved setting
may enable automatic organization of collision-free verified encodes on server
startup; collisions still stop for review and overwrite is never automatic.

The Gemini ambiguity action now queues real bounded work instead of only
changing a review label. One confirmed batch reads only the exact held MKVs,
runs FFprobe plus the existing sequential CPU Whisper/FFmpeg sampling fallback,
and sends Gemini only up to three short excerpts per item together with the
remaining runtime-plausible reviewed-catalogue candidates. A structured answer
must use an allowed candidate and reach the confidence threshold before a new
immutable verified-rip contract is written and identification is requeued.
Failures remain held and retryable. Completed organization cards now state the
local completion time as **Moved into Jellyfin**, and saved disc reviews show a
known prior matched name ahead of the technical collision-safe staging path.

The web navigation now includes **Recently Finished**. It groups durable queue
results by local calendar day and shows the matched name, completion/review
time, stage outcome, stop reason, and a safe Jellyfin- or staging-relative
location after the optical disc is removed. Absolute private roots remain out
of the public response. New production queue admissions use the existing
collision-safe basename (including the inventory fingerprint) as the durable
media ID, preventing a later disc's generic `disc-01-title-NNN` identifiers
from replacing an earlier disc's history.

Recently Finished now groups records as day → expandable disc. The disc summary
uses the best known library title and shows completed versus attention-required
counts. Expanding it exposes title-level destinations and reasons. A held or
failed Gemini ambiguity can launch the confirmed local-evidence/Gemini workflow
directly from history, including after the physical disc has been ejected.
Full Windows Jellyfin paths are assembled only in the loopback browser from the
configured root plus the validated relative contract; absolute roots are not
added to queue events or durable public history records.
# 2026-08-01 — Recoverable source retention and finished-size reporting

- Added a non-secret `deletion_staging_root` setting and folder-browser field.
- New transcodes retain the verified original-source identity in the private
  transcode contract. After a Jellyfin destination has been collision-checked,
  moved, and size-verified, organization may move that original into a unique
  per-media directory beneath the configured deletion-staging root. Existing
  archive paths are never overwritten. If the root is unset, or an older
  transcode contract has no retained-source fields, the original remains in its
  existing staging location.
- Organized contracts record the verified Jellyfin output size and retained
  source identity privately. Public pipeline responses expose only the output
  byte count and whether a retained source is currently available; they do not
  expose the private retained-source path.
- The Disc Dashboard queue and Recently Finished view now show the completed
  Jellyfin file size. Recently Finished provides per-disc `Re-encode this disc`
  and `Delete retained originals` actions when retained originals are present.
  Re-encode reuses saved identification and enters at the serialized transcode
  stage. Delete requires a digest-bound preview, exact file count, and a second
  confirmation; it affects only recorded files beneath the configured
  deletion-staging root and never Jellyfin files.
- Focused synthetic validation: 32 pytest tests passed across pipeline adapters,
  queue, API composition, and configuration; focused Ruff check and format
  checks passed; frontend ESLint, TypeScript, and production build passed. No
  disc, real MKV, MakeMKV, HandBrake, FFmpeg, or FFprobe operation was run.
- Fixed the Settings folder chooser so it renders as a modal over the user's
  current scroll position. Previously it rendered inline at the top of the long
  Settings page, making a successful click appear to do nothing when the user
  was viewing Media Pipeline Locations farther down the page. Frontend lint,
  TypeScript, and production build checks passed after the repair.
- Added left-navigation `Logs` and `System Cleanup` views. Logs reads the
  durable path-redacted pipeline event stream rather than exposing the raw
  application log. New Gemini failures retain a safe specific review code for
  insufficient audio evidence, unavailable catalogue data, or provider
  failure. System Cleanup groups retained originals by disc, requires a valid
  HandBrake profile for requeue, reuses saved identification, and provides an
  exact-preview deletion action that cannot affect Jellyfin files.
- Weak or silent local audio no longer prevents a confirmed Gemini fallback.
  The request may contain runtime plus the constrained remaining catalogue
  names with zero transcript excerpts and asks Gemini for a provisional
  one-to-one best choice. Low-evidence/low-confidence results are carried
  through identify, transcode, and organization as explicitly provisional.
  Disc Dashboard and Recently Finished show the provisional marker and
  confidence, offer an exact `Play for review` action through the Windows
  default media player, and provide a filename-stem box. A reviewed rename
  preserves `.mkv`, refuses invalid Windows names and destination collisions,
  and appends a new immutable organization contract/event.
- System Cleanup now also lists legacy completed items whose durable verified
  rip contract still points to an unchanged original in rip staging. A
  digest-bound, exact-count confirmation can move those originals into the
  configured cleanup root without touching Jellyfin, after which normal
  requeue/delete controls become available. Recently Finished now detects
  queued transcodes and provides prominent navigation to the Disc Dashboard's
  exact HandBrake-profile review and start controls.
- Core Settings now persists a validated, non-secret Gemini model ID. The UI
  provides a suggested-model chooser with custom model-ID entry, and the live
  ambiguity fallback uses this saved configuration. An existing `GEMINI_MODEL`
  environment value remains a migration/default source only when no model has
  yet been persisted; API keys remain environment-only.
- Queue views now show only actionable downstream work. Completed items remain
  in durable history and are presented under Recently Finished instead of
  continuing to occupy the global or selected-disc queue. When recovery creates
  a newer durable record for the same disc fingerprint and title index, queue
  views show only that newest record. Reviewed release repair likewise requeues
  only the newest held copy of each title, preventing duplicate downstream work.
- Fresh seasonless TV discs no longer receive an automatic hard-coded release
  assignment, including known Faerie Tale Theatre test media. Their selected-
  disc queue instead requests a canonical series name and offers the general
  all-season analysis path: inspect each newest held MKV, transcribe bounded
  samples once, fetch the complete aired TMDb catalogue, and run the existing
  ordered BM25/runtime sequence model across every season. Confident sequence
  assignments are written into new private contracts and requeued; ambiguous
  or failed analysis remains held with a typed review reason. The legacy
  reviewed-release endpoint remains diagnostic-only and is no longer offered
  as the normal dashboard solution.
- Planned Titles now overlays the newest durable identification result by disc
  fingerprint and title index. The immutable collision-safe rip destination is
  still shown as the original rip target, while a newly matched pretty name
  refreshes globally even when recovery changed the internal media ID.
- The selected-disc queue on Disc Dashboard now exposes the same global
  serialized worker start/resume and pause controls as the Queue page. Resume
  still cannot bypass HandBrake or organization authorization.
- Pipeline item stages now use a consistent visual state cycle in both queue
  views: completed stages are green, the actively running stage is animated
  blue, a queued current stage is amber, review is orange, failures are red,
  and future stages remain muted. Human-readable status text distinguishes
  waiting from active ripping, matching, transcoding, and Jellyfin transfer.
- The production transcode adapter now receives the configured TV and movie
  library roots and performs a read-only destination/episode-ID collision check
  before creating an encoded destination, run directory, or HandBrake job.
  Existing Jellyfin episodes therefore stop at `library_collision` before
  encoding; the organization-stage check remains as defense in depth. The Disc
  Dashboard now exposes transcode and organization review controls for its
  selected disc and reports when Start/Resume merely unpaused a queue whose
  remaining items are all held for review.
- Library collision inspection now also runs as part of the successful
  identify-to-transcode transition. On a mixed disc, only titles whose episode
  ID or exact destination already exists are held; unrelated missing episodes
  remain queued for HandBrake. Each held item exposes two exact-digest actions:
  discard only the new pipeline media, or replace from a verified encode after
  first moving the old Jellyfin file into collision-refusing deletion staging.
  Raw rips are retained when an already-encoded copy is discarded or replaces
  a library file. Synthetic tests cover both destructive decisions; no real
  media was used during implementation.
- Recently Finished now supports browser-persistent read and cleared state,
  including mark-all-read, clear-read, clear-all, and restore-cleared controls.
  These presentation controls never delete durable history or media.
- Distribution attribution was hardened: `THIRD_PARTY_NOTICES.md` retains the
  MKV Episode Matcher and Riplex MIT notices, `docs/ATTRIBUTIONS.md` documents
  external services and tools, Help contains TMDB's required notice and an
  approved TMDB logo, and the PyInstaller build copies these files plus
  recursive installed-package metadata into binary distributions.
## Failed rip replacement authorization (2026-08-01)

- The web rip confirmation now treats failed-partial preservation as an explicit
  opt-in. With it unchecked, starting a reviewed rip previews the exact isolated
  incomplete-attempt set and shows its file count and total size before removal.
- Cleanup is bound to the reviewed disc fingerprint, title indexes, and an
  immutable metadata digest. A changed plan, active physical rip, symlink,
  non-MKV/unknown file, path escape, or verified final output prevents cleanup.
- Older API clients remain preservation-safe by default. Destructive cleanup
  requires both the exact preview digest and a dedicated confirmation field.
- No physical disc or real media was accessed while implementing this boundary.

## Per-disc missing-items policy (2026-08-01)

- The disc dashboard offers an optional `Add missing items from this disc`
  policy. The choice is stored in the private media context and propagated to
  every verified-rip contract.
- Known prior results can be shown during preparation. Every successfully
  identified item is checked against the configured Jellyfin library before
  HandBrake; collisions are held per item while unrelated missing items keep
  moving through the global downstream worker.
- A same-named movie is not sufficient to skip an ambiguous edition or
  commentary-bearing title before its streams are inspected.
## Legacy queued-plan digest compatibility (2026-08-01)

- Queued jobs created before the special-feature, selected-title, episode-map,
  and existing-output-policy context fields were added can now be rebound when
  every omitted field still has its original safe default.
- Compatibility never removes a non-default policy or assignment from the
  authorization identity. Inventory proofs, selected jobs, destinations, disc
  bindings, and private/public binding agreement remain mandatory.
- The previously blocked six-title queued job passed a read-only dispatcher
  binding validation after this repair. No MakeMKV process was started.

## Sticky HandBrake profile default (2026-08-01)

- Each loaded disc retains its own HandBrake profile choice for the current
  dashboard session. Choosing a profile on one drive does not change the
  displayed or effective profile of the other currently loaded discs.
- The most recently selected explicit profile becomes the automatic default
  for discs detected later and is persisted for the next server session.
- Profile-default persistence now uses a dedicated validated API boundary. It
  refuses unknown profile IDs and reports save failures instead of silently
  changing only the browser state.
- Focused profile tests, Ruff checks, frontend lint, TypeScript compilation,
  and the production frontend build passed.
- No optical disc, media file, encoder, or external media tool was accessed.

## Reduced review-to-queue interaction and unmatched fallback (2026-08-01)

- Choosing a rip-collision policy now creates, approves, and queues the new
  immutable collision-safe plan in one action. Physical MakeMKV execution still
  retains its final exact title-count warning and confirmation.
- Restarting verified-rip identification and starting all-season analysis now
  resume the downstream scheduler automatically instead of requiring a second
  generic queue-resume click.
- Suggested series names remove complete `Season N` and disc/DVD suffixes. For
  example, `Dragons Race to the Edge Season 1 DVD2` now suggests
  `Dragons Race to the Edge`.
- If local all-season sequence matching remains ambiguous, the user has enabled
  automatic Gemini fallback, and the analysis confirmation permits external
  fallback, bounded transcript excerpts can be ranked only against the fetched
  TMDb episode catalogue. Results are one-to-one and recorded as provisional
  with confidence. Missing catalogue or evidence still stops safely.
- Eleven focused backend tests, Ruff, frontend lint, TypeScript, and the
  production frontend build passed. No live media or provider was accessed.

## Explicit season context and automatic restart analysis (2026-08-01)

- Disc preparation now recognizes an explicit `Season N` phrase in a MakeMKV
  volume label. `Dragons Race to the Edge Season 1 DVD2` becomes series
  `Dragons Race to the Edge`, season `1`, and TV-first identification. Generic
  `DVD2`, `Disc 2`, and `Volume 2` labels are never treated as season numbers.
- New jobs with reliable label context therefore enter ordinary season matching
  and do not stop at the general all-season analysis prompt.
- For older verified-rip records already held without season context, choosing
  restart matching now launches all-season analysis automatically using the
  cleaned series label. The separate Analyze button is no longer required for
  that recovery path.
- Fifteen focused tests, Ruff, frontend lint, TypeScript, and the production
  frontend build passed.

## Non-destructive queue clearing (2026-08-01)

- The global and selected-disc queue views now offer `Clear held items`, and
  ordinary failed/review cards offer `Clear from queue` for one record.
- Clearing changes only the durable queue state to `discarded`. Original MKVs,
  partials, encoded files, contracts, event history, logs, and library media are
  not renamed, moved, or deleted.
- Bulk clearing is atomic and accepts only failed or review-held records. If an
  item becomes active before confirmation, the entire selection is refused.
- Twenty-nine focused queue/content-policy tests, Ruff, frontend lint,
  TypeScript, and the production build passed. No live media was accessed.

## Existing-rip review and optical-drive ejection (2026-08-01)

- After verified existing MKVs are accepted for identification, the disc review
  now says they are processing instead of continuing to present `final-exists`
  as an unresolved rip collision. Planned titles retain the accepted status and
  current durable match information.
- Inserted-drive cards now have an explicitly confirmed manual `Eject disc`
  action. It resolves only the selected MakeMKV drive and refuses ejection while
  that drive has authorized, queued, running, or pause-requested rip work.
- Settings now include an opt-in `Automatically eject after a successful rip`
  preference. Automatic ejection occurs only after the complete reviewed job is
  recorded completed and every selected output has passed the existing rip
  verification boundary. Failure, timeout, pause, stop, a missing disc, or
  competing work for that drive prevents ejection. An eject failure is logged
  without changing a successful rip into a failed rip.
- Ejection uses the Windows storage-eject control against the exact resolved
  optical drive, retries compatible storage access modes, and falls back to the
  exact-drive Windows CD-audio tray command for optical drivers that reject the
  storage control. The existing-rip review now also includes a directly visible
  `Start / resume matching queue` action, and eject failures are shown in an
  immediate dialog instead of only in the page-level status area. Focused tests
  inject fake inventory and eject adapters; no physical disc was read or
  ejected while implementing this change.
- Drive refresh now privately caches the exact MakeMKV-index-to-Windows-letter
  mapping. Dashboard eject uses that mapping immediately and performs the slow
  MakeMKV lookup only when the server has not yet been primed. Eject requests
  have per-drive UI state, so one tray operation no longer disables eject on
  other idle drives.
- Selected-disc record collapsing no longer lets a newer discarded recovery
  attempt hide an older active review. Existing MKVs held for
  `unmatched_disc_analysis_required` are described accurately and the review
  action launches all-season analysis instead of a queue resume that cannot
  bypass the hold.
- Recovery analysis now carries an explicit `Season N` parsed from the current
  disc label into its request, filters the authoritative episode catalogue to
  that season, and writes the season back into the revised identification
  contract. Only genuinely seasonless/mixed discs use the all-season scope.
- Queue review summaries now enumerate their hold codes, explain the available
  resolution, and link to the affected cards. Clear, collision resolution, and
  transcode-plan failures are shown immediately. Deleting a collision copy also
  retires other held/failed queue records for the same disc fingerprint and
  title index without deleting their media, preventing an older recovery
  lineage from resurfacing as the same collision.

## Automatic inserted-disc pipeline (2026-08-01)

- The Windows volume watcher now passes each refreshed drive snapshot to a
  process-local automatic-rip coordinator when automatic processing is enabled.
  A newly loaded drive is admitted once; repeated refresh/poll events cannot
  duplicate the attempt. Removing the disc rearms that drive for its next
  insertion, and discs already present at server startup are admitted by the
  startup refresh.
- Each newly loaded drive receives its own worker. Collision-free, unambiguous
  plans are prepared, authorized, queued, and executed with the configured
  MakeMKV path and isolated recovery directory. Multiple inserted drives may rip
  in parallel while titles within each drive remain sequential. A plan that
  actually requires review remains held and is never forced through.
- Verified outputs proceed through identification. Confident, collision-free
  items are automatically transcoded with the configured default HandBrake
  profile and organized into the configured Jellyfin library; review-held items
  release the global downstream worker so unrelated items continue. Automatic
  eject remains conditional on complete verified rip success and its separate
  setting.
- Settings now expose `Remember the last selected profile for future discs`.
  When enabled, a per-disc profile selection is saved as the default for later
  insertions and server restarts; existing disc overrides remain unchanged.
- Every HandBrake profile card now has a default-profile radio control. The
  selected card is highlighted and labeled `Current default`; changing it uses
  the validated persisted profile endpoint and reports success or failure.
- Fourteen focused automatic-dispatch, API-composition, profile, and collision
  tests passed with fake drive/executor boundaries. Ruff, frontend lint,
  TypeScript, and the production frontend build passed. No physical disc or
  media was accessed while implementing this phase.

## HandBrake audio-profile clarity (2026-08-01)

- The profile editor presents the source-layout preference as six always-visible
  radio choices: disc default, stereo, 2.1, 5.1, 7.1, or highest channel count.
- The UI now explains the existing adapter behavior: the preferred original
  source-layout track is retained and a stereo AAC compatibility track is also
  created. `Put stereo compatibility track first` controls only their order.
- A live output-audio summary reflects the selected order and additional-track
  retention policy. Frontend lint, TypeScript, and the production build passed.
  No HandBrake, FFmpeg, disc, or media operation was run.
- Stereo, 2.1, 5.1, and 7.1 now each store an independent bitrate preference.
  An explicitly selected layout is emitted as an AAC track using that layout's
  bitrate, alongside the separately configured stereo compatibility track.
  Disc-default and highest-available remain source passthrough choices. Older
  saved profiles and manifests receive backward-compatible bitrate defaults.
  Fifty-four focused adapter/profile/authorization tests, Ruff, frontend lint,
  TypeScript, and the production build passed; the new layouts have not yet
  been validated against real media.
- Audio profiles now have explicit ordered primary and secondary outputs. This
  supports stereo-first plus 5.1-second, highest-available passthrough first
  plus stereo second, any other distinct pair, or no secondary output. The
  adapter selects a source track with enough channels for both outputs.
  Language retention remains independent: `all specified languages` with
  `eng` retains other English tracks such as commentary and alternate mixes
  after the primary/secondary pair. Duplicate output layouts are rejected.
- Settings and HandBrake-profile save results now appear as fixed, dismissible
  success/error notifications that remain visible at any scroll position.
  The configuration button changes to `Configuration saved` after a successful
  response. Frontend lint, TypeScript, and the production build passed.
- HandBrake profile validation now also appears inline beside `Save custom
  profile`; Profile ID and Display name are visibly required and invalid values
  receive specific guidance.
- Each HandBrake profile card can assign that profile as the general, 480p,
  720p, 1080p, or 4K default. Automatic transcode authorization binds the full
  resolution mapping, FFprobe selects the source-height bucket at execution,
  and an explicit disc/queue profile still overrides it. Missing resolution
  assignments fall back to the general profile.
- Queued but not-running downstream items can be removed individually or in a
  batch. Cancellation is atomic, records a durable event, and preserves every
  staged MKV, encode, partial, contract, and log. Running work remains
  uncancellable through this control.
- Thirty-nine final focused profile, authorization, adapter, and queue tests
  passed, as did Ruff, frontend lint, TypeScript, and the production build. No
  disc or media tool was run.
- Rip-review collision handling now offers a separately reviewed deletion of
  raw staged MKVs only when both the exact planned staging file and its prior
  matched Jellyfin destination still exist. The server returns a path-redacted
  digest/count/size preview, revalidates it before deletion, and never changes
  Jellyfin. Files with ambiguous identity, missing history, or a missing
  Jellyfin destination are excluded.
- Optical tray requests are serialized in the web UI. Later requests display
  `Queued to eject` or `Queued to open`, then run after the active tray command.
  Empty detected drives remain visible and expose `Open tray`. Windows eject
  now sends both the native storage control and independent MCI door-open
  command because some drivers acknowledge one method without opening the
  tray. Cancelling and ejecting a reviewed disc also cancels restart-stale,
  non-active plans for that same drive; a genuinely attached MakeMKV executor
  remains protected.
- Twenty-six focused orchestration, eject, drive-watcher, and staged-cleanup
  tests passed. Ruff, frontend lint, TypeScript, and the production build also
  passed. No optical drive, disc, MKV, or external media program was accessed.
- Gemini retry is now an exact per-item operation. The request names one or
  more explicit recovery IDs, validates that every item is still held in the
  identify stage, and durably records `gemini_analysis_running` before the API
  reports that work started. It no longer relies on a two-request UI transition
  or silently includes every Gemini-held item in the global queue. Both the
  disc queue and Recently Finished display an immediate item-local submitting,
  running, or request-error message. Focused regression tests, Ruff, frontend
  lint, TypeScript, and the production build passed; no MKV, physical disc, or
  Gemini provider was accessed.
- Disc Dashboard drive cards and the selected-disc review now consistently use
  the newest durable orchestration job for that physical drive. When a tray is
  reused, an older review for the prior disc is replaced in the open view by
  the newest saved review, preventing prior matched-title history from being
  displayed beneath the newly detected disc label. Frontend lint, TypeScript,
  and the production build passed; no disc or media was accessed.
- Automatic bonus-title fallback is now persisted as `extras` content instead
  of an unclassified TV context. The automatic downstream worker also refuses
  to send explicit movie, extras, or mixed batches through all-season episode
  matching. This prevents movie bonus discs such as the Parent Trap test case
  from displaying or running a TV-season analysis. Three focused tests and
  Ruff checks passed; no disc, MKV, or external provider was accessed.
- New rip manifests now prefix the collision-safe staging basename with a
  sanitized, title-cased disc label. For example, a disc label resembling
  `PARENT_TRAP_1961_PARENT_TRAP_II` produces a basename beginning
  `Parent-Trap-1961-Parent-Trap-II--` while retaining the ordinal disc ID,
  inventory fingerprint, and title index required for recovery and
  deduplication. Legacy opaque basenames remain supported. Fifty-three focused
  manifest, recovery, cleanup, and queue tests passed with Ruff checks; no
  existing media was renamed or accessed.
- Failed and review-held identification cards now offer both `Remove from
  queue — keep staged rip` and `Delete staged rip permanently`. Permanent
  deletion requires a separate confirmation, resolves and size-checks the
  exact verified rip beneath the configured staging root, refuses active
  Gemini evidence analysis, leaves Jellyfin unchanged, and discards the queue
  record only after deletion succeeds. Twenty-three focused queue/API tests,
  Ruff, frontend lint, TypeScript, and the production build passed; no real
  media was deleted or accessed.
- The staged-rip deletion validator now accepts both the original verified-rip
  contract used by failed identification and the later identified contract.
  A record first removed from the active queue may subsequently delete its
  preserved exact staged rip from Recently Finished. Discarded records expose
  that action only while the recorded source still exists with its verified
  size. Movie/extras/mixed plans also remain in isolated staging until their
  destination is identified, and readable labels preserve Roman numerals such
  as `II`. Forty-five focused tests, Ruff, frontend lint, TypeScript, and the
  production build passed; no real media was changed.
- Disc cards now aggregate downstream identification state and older active rip
  records for the same tray. A completed MakeMKV job is displayed as `rip
  completed`, not whole-pipeline completion; unresolved identification turns
  the card red with a review explanation. If automatic eject is blocked by an
  earlier job still marked active, the card states that reason and opens the
  earlier job's controls. The live Parent Trap test confirmed automatic eject
  was enabled but safely skipped for exactly that competing-job condition.
- Extras classification is now evaluated before the TV season requirement in
  the identify adapter. An extras item without a season routes to bonus-feature
  evidence and the configured Gemini fallback instead of
  `unmatched_disc_analysis_required`. Thirty-eight focused adapter, queue, and
  API tests plus Ruff, frontend lint, TypeScript, and the production build
  passed; no disc or media operation was performed.
- Startup drive reconciliation no longer automatically rerips a disc whose
  inventory fingerprint already has durable rip work. The initial startup scan
  still launches genuinely new inserted discs, while a known disc is held for
  review so verified output, partials, and unresolved identification can be
  reused safely. The dashboard's unmatched-disc recovery now offers separate
  TV-series and movie/TV-movie bonus-feature routes; the latter requeues an
  immutable `extras` context into the special-feature evidence path. Nineteen
  focused tests, Ruff, frontend lint, TypeScript, and the production build
  passed. The already active physical rip was not interrupted or changed.
- The movie/TV-movie bonus-feature recovery action now continues into bounded
  local evidence and the confirmed Gemini batch when automatic Gemini fallback
  is enabled, rather than stopping at `gemini_evidence_required`. Already-held
  titles also have one batch action to start evidence and Gemini for the whole
  selected disc. The confirmation explains the bounded external transmission;
  no MKV, path, credential, or full transcript is sent. Focused Python tests,
  Ruff, frontend lint, TypeScript, and the production build passed without
  accessing real media or contacting Gemini.
- Catalogue-free Gemini recovery now handles mixed and previously unknown
  discs instead of stopping when no reviewed Riplex catalogue matches. After
  bounded local FFprobe/transcript evidence is collected, one schema-constrained
  batch classifies each title as a movie, TV episode, bonus feature, menu, or
  unknown and proposes a filesystem-safe provisional title. Confident movie
  and bonus-feature results re-enter the normal identify, transcode, collision,
  and organize pipeline; uncertain or unsupported results remain in an explicit
  manual-review state. Recovery-suffixed media IDs retain their original title
  index. Synthetic matcher, fallback, adapter, queue, and API tests passed; no
  disc, MKV, credential, or external provider was accessed.
- Automatic processing now watches the durable downstream queue for newly
  identified transcode work, including items that arrive after delayed Gemini
  review or a server restart. It builds the same validated authorization plan
  with no profile override, so each source uses its configured 480p, 720p,
  1080p, or 2160p profile and falls back to the general default only when that
  resolution has no mapping. One shared lock prevents the original rip
  continuation and the background worker from authorizing the same batch, and
  HandBrake remains globally serialized. Twenty-three focused tests and Ruff
  checks passed; HandBrake and real media were not accessed.
- Server startup now reconciles interrupted `running` and `pause_requested`
  physical-rip records to paused review state before drive processing begins.
  These stale records therefore no longer prevent the existing post-rip
  automatic eject path; a drive can be released once verified ripping is done,
  while identification, transcode, organization, and review continue from
  staging. The frontend adds a dedicated `Needs Attention` navigation view
  backed by the same durable queue records and action handlers as the dashboard,
  including Gemini/manual review, collision choices, retry, hold/clear, and
  staged-source cleanup. Twenty-two focused backend tests plus frontend lint,
  TypeScript, and production build checks passed; no disc or media operation
  was performed.

## RipWeaver publishing and deployment status (2026-08-02)

- The project and GitHub origin now use the RipWeaver name. The active remote
  is `fajis1/ripweaver`; the original upstream remains configured separately
  for attribution and future upstream comparison.
- The web application title and navigation identify the application as
  RipWeaver. Generated frontend assets are committed so the packaged FastAPI
  server serves the same interface that passed the frontend build checks.
- Third-party attribution and licensing notes are maintained in
  `THIRD_PARTY_NOTICES.md` and `docs/ATTRIBUTIONS.md`. Local credentials remain
  outside Git in the ignored `.env` and Codex/Cloudflare authentication state
  remains outside the repository.
- The owner registered the RipWeaver domain family. The intended public
  architecture places DNS and a narrowly scoped catalogue API behind
  Cloudflare; PostgreSQL, Jellyfin, media roots, and the local RipWeaver control
  API remain private. The existing `/rip` routes remain loopback/same-origin
  only until secure pairing and authentication are implemented.
- The local Windows Codex installation now has Cloudflare account, Workers
  Bindings, Builds, Documentation, and Observability MCP endpoints enabled.
  OAuth completed locally so the callback reached the same machine running
  Codex. No OAuth URL, account identifier, token, or credential is stored in
  this repository. A new Codex session is required before those tools appear
  as callable capabilities.
- Manual tray ejection now distinguishes a genuinely attached process-local
  MakeMKV executor from stale durable `running` records left by an earlier
  server process. An explicit eject still refuses a live executor, but safely
  reconciles stale claims and returns inactive authorized/queued work to review
  before opening the tray. Drive refreshes also merge partial MakeMKV results
  with previously discovered slots, so empty trays no longer disappear from
  the dashboard after a partial refresh. Nineteen focused drive, eject, and API
  tests plus Ruff and formatting checks passed; no disc was read or ejected.
- Windows drive discovery now supplements MakeMKV's partial inventory with the
  operating system's complete optical-drive list. This keeps every empty tray
  visible after a server restart without testing media readiness or spinning a
  disc. The dashboard also binds a saved rip job to the currently observed disc
  instead of choosing history by reusable drive index; changing the disc label
  clears that process-local binding, while durable matching identity remains
  the inventory fingerprint plus title index. Focused drive, eject, preparation,
  and API tests plus frontend lint, TypeScript, and production build checks
  passed; no physical disc or media file was accessed.
- Disc identity is never derived from an optical-drive index. The index is only
  a temporary hardware address for a MakeMKV command. Durable identity is the
  full inventory fingerprint, which already includes the disc label and every
  title's index and size (therefore the title count and aggregate disc-title
  size), with runtime retained as an additional discriminator. Windows volume
  changes now invalidate all process-local dashboard bindings—even when a new
  disc reuses the same label—and only a new full inventory may bind a job again.
  On 2026-08-02 the owner requested a clean identity baseline; the three local
  control databases and prior pipeline contracts were moved to a recoverable,
  credential-free application-data backup. No media, Jellyfin output, settings,
  profiles, credentials, or logs were removed.
- Automatic rip planning no longer requires episode classification before
  MakeMKV. Every nonempty title of at least eight seconds is placed in the rip
  manifest regardless of whether metadata calls it an episode, movie, extra,
  play-all title, or review item; classification begins only after the whole
  selected disc has completed and verified. A partial failure on one disc does
  not enqueue that disc for identification, while another fully completed drive
  may continue independently. The dashboard labels its manual action as a retry
  when automatic processing is enabled rather than presenting manual start as
  the normal workflow.
