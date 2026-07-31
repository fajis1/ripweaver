# Project Status

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
