# Codex Repository Guide

This file applies to the entire repository. Read it before inspecting, testing,
or changing the project.

## Safety Rules

These rules are non-negotiable:

- Default to read-only and dry-run behavior.
- Never rip, rename, move, overwrite, delete, eject, or transcode media unless
  the user explicitly authorizes that specific operation.
- Never read, print, commit, or expose `.env` files, API keys, credentials,
  tokens, or personal data.
- Never execute legacy scripts until they have been reviewed for destructive
  behavior.
- Test MakeMKV parsers with saved or synthetic output before accessing physical
  discs.
- Process optical drives sequentially unless parallel operation is explicitly
  approved.
- The owner explicitly approved parallel operation on 2026-07-30. For approved
  rip manifests, parallel-across-drives is now the default: use one worker per
  physical drive, keep titles sequential within a drive, bound CPU-heavy stages,
  and isolate drive-specific failures so unaffected drive pipelines continue.
- TV staging must preserve matcher context:
  `TV Shows/<Canonical Series>/Season XX/<isolated disc/title staging>`.
  The matcher must search ancestor folders for season context. A box-set volume
  number must never be assumed to be a season number; mixed/unknown discs stage
  under the series `Unmatched` folder for reviewed identification.
- Newly ripped MKVs must receive a unique staging basename containing the
  ordinal disc ID, inventory fingerprint, and title index. Final Plex/Jellyfin
  organization must stop on an existing destination and route the conflict to
  deduplication/review; it must never overwrite.
- Preserve existing user files and unrelated working-tree changes.

Treat disc scans and media libraries as user data. Reports, logs, test fixtures,
and documentation must not contain drive serial numbers, personal paths,
credentials, or unnecessary media-library details. Do not assume that
`--dry-run` is safe without tracing the entire call path.

## Git Checkpoints

The owner authorizes repository agents to create and push safety checkpoints
only to the owner-controlled `origin/wip/test` and `origin/wip/main` branches by
running `scripts/checkpoint_worktree.ps1`. This standing authorization does not
permit pushes to `upstream`, force pushes, branch deletion, ordinary feature
commits, pull requests, merges, releases, or changes to `main`.

- Use `-Channel test` on every non-main development or test branch. Use
  `-Channel main` only when the current checked-out branch is actually `main`.
- Run a preview first, then push the checkpoint after each meaningful completed
  implementation milestone and at the end of every agent task that leaves
  meaningful uncommitted changes.
- Create a checkpoint before bulk rewrites, generated-build replacement,
  deletion, branch switching, or another risky operation whenever the current
  meaningful state is newer than the remote checkpoint.
- The checkpoint script must leave the current branch, real Git index, and
  worktree unchanged. If its secret, path, size, remote, or Git validation
  refuses a checkpoint, do not bypass the refusal; report it to the owner.
- Never treat `git stash`, reflog-only objects, ignored files, copied working
  directories, or the local `.git` directory as the sole backup of useful work.
- Never add `.env`, credentials, media, private transcripts, logs, pytest
  scratch directories, coverage output, personal paths, or ad-hoc local repair
  scripts to a public checkpoint. Sanitize useful project recovery notes before
  including them.
- Checkpoint history is recovery-only. Finished changes still use the normal
  reviewed feature-branch and pull-request workflow, normally squash-merging
  WIP history before `main`.

See `docs/GIT_CHECKPOINTS.md` for commands and recovery guidance.

## Repository Layout

- `mkv_episode_matcher/cli.py`: Typer CLI entry point.
- `mkv_episode_matcher/backend/`: FastAPI application and API/WebSocket routes.
- `mkv_episode_matcher/frontend/`: React/Vite frontend and packaged build.
- `mkv_episode_matcher/core/`: matching engine, configuration, providers, and
  shared models.
- `mkv_episode_matcher/disc/`: read-only disc discovery and MakeMKV parsing.
- `tests/`: pytest suite.
- `docs/`: user and project documentation.

The Python package supports Python 3.10 through 3.12. Prefer `pathlib.Path`,
typed models, explicit subprocess argument lists, and small modules with
separately testable parsers. Avoid `shell=True`. External-process adapters must
capture exit status, stdout, stderr, timeout, and structured context.

Separate planning from execution. A scanner or matcher should produce a
proposal; a different, explicitly authorized operation should apply it.

## Configuration

- `.env` is local and ignored by Git. Never open or display it.
- `.env.example` contains variable names and safe placeholders only.
- Secrets are loaded through `mkv_episode_matcher/core/environment.py`.
- Configuration JSON must not persist secret fields.
- Logs and exception messages must not include environment values or command
  environments.

Keys found in legacy source must be treated as exposed: rotate them at the
provider, then place only the replacement values in `.env`.

## Development on Windows

From PowerShell in the repository root:

```powershell
uv sync --extra cpu --group dev
uv run mkv-match --help
uv run mkv-match credentials
uv run mkv-match serve --no-browser
```

Use `uv run mkv-match credentials <name>` to enter or replace one credential
through a hidden prompt. Supported names are shown by
`uv run mkv-match credentials`. The command writes only to the local ignored
`.env`; status output never displays values.

`uv run mkv-match credentials --migrate-legacy` is the one reviewed exception
for an old user JSON config: it transfers recognized credential fields into the
ignored `.env`, atomically rewrites the JSON without those fields, and reports
credential names only. Never generalize it to scan arbitrary files.

Use `--extra cu128` instead of `--extra cpu` only on an intentionally configured
CUDA system.

Frontend development:

```powershell
Set-Location mkv_episode_matcher\frontend
npm install
npm run dev
npm run lint
npm run build
```

Do not run a development server or open a browser unless it is relevant to the
requested task.

## Tests and Checks

Run focused tests first, then the full suite:

```powershell
uv run pytest tests\test_disc_preflight.py
uv run pytest
uv run ruff check mkv_episode_matcher tests
uv run ruff format --check mkv_episode_matcher tests
```

The repository currently contains some pre-existing lint findings in older
code. Do not mechanically rewrite unrelated files to clean them up. Check new
or modified modules directly and report unrelated failures separately.

Test runs update the tracked `.coverage` file. Do not discard that or any other
working-tree change unless it is known to have been produced by the current
work and cleanup is authorized.

## MakeMKV Work

`mkv-match preflight` is the only current disc command. It permits MakeMKV's
`info` action only and saves reports under the ignored `.mkv-preflight/`
directory.

`disc/makemkv_process_control.py` is the exclusive Windows MakeMKV process
boundary. Backend startup must acquire its named single-instance mutex before
touching MakeMKV, terminate every surviving `makemkvcon.exe` or
`makemkvcon64.exe`, verify two empty process snapshots, and abort startup if
cleanup cannot be proven. This intentionally includes CLI processes started
outside RipWeaver; the MakeMKV GUI executable itself is outside the kill scope.
Every MakeMKV info or rip child launched by RipWeaver must be assigned to the
process-wide Windows Job Object with kill-on-close enabled. Standalone CLI
launches must acquire the same machine-wide exclusivity gate lazily, so they
cannot compete with an active backend. Assignment failure must settle the child
and poison further launches until restart. Startup/process logs may report
counts and error types only, never PIDs, command lines, executable paths, drive
details, or environments. Tests must use injected synthetic process snapshots,
terminators, and child handles; implementation approval is not authorization to
terminate a real process or access a physical disc during validation.
The same process-control boundary must classify every ordinal `disc:N` child
before process creation. At most one live child may own a physical drive; an
`info disc:9999` child is an all-drive barrier and may overlap no per-drive
child. Different explicitly bound drives may run concurrently. The claim is
released only after process exit is proven, and an unscoped `dev:` physical
command is refused. Explicit per-drive info and rip commands use `--noscan` so
MakeMKV does not perform its normal media prescan against the other drives.
Failed automatic drive refreshes remain armed but must use the bounded
5-second, 15-second, 60-second, then 300-second retry backoff. A fast MakeMKV
failure must never become a tight discovery loop against the drives.

`mkv-match plan-titles <report.json> [<report.json> ...]` reads only saved JSON
reports. It classifies episode candidates, combined titles, extras, and review
items; ranks diagnostic audio streams; and produces no execution command.
`--json` output uses ordinal report IDs and does not expose source paths.

`mkv-match plan-special-features <saved-inventory.json>
<reviewed-catalogue.json>` is saved-data-only. The release-aware catalogue
distinguishes standalone, multi-audio, menu-bound, audio-only, still-gallery,
and unknown representations. Runtime ties remain explicit candidate sets;
menu-bound items do not appear as missing MKV titles. It performs no disc,
provider, or media access and has no execution command.

`mkv-match plan-special-feature-rip <saved-inventory.json>
<reviewed-catalogue.json> --manifest-out <new-safe.json>` writes only a
non-executable diagnostic-rip plan. It includes strong matches and plausible
unresolved titles, preserves ambiguity candidate IDs, requires all streams for
multi-audio titles, and excludes menu/play-all candidates by default. Its
staging names are derived from a digest of the validated saved-data plan. The
manifest has no drive binding, MakeMKV command, library destination, or
execution authority and must never be passed to `execute-rip`. A future
execution boundary must revalidate against a fresh preflight and requires
separate explicit rip authorization.

`mkv-match bind-special-feature-rip <diagnostic.json> <fresh-inventory.json>
--diagnostic-sha256 <reviewed-sha256> --manifest-out <new-safe.json>` is also
saved-data-only. It verifies the exact diagnostic file digest and compares the
complete title inventory signature plus every selected title's index, runtime,
size, and audio-stream count. A changed or substituted inventory stops binding.
The resulting `special-feature-rip-binding-plan` contains an exact drive/title
binding but remains `execution_authorized: false`; the episode `execute-rip`
loader must reject it. Running the binder is not authorization to access a
disc or rip media.

`mkv-match execute-special-feature-rip <bound.json> --fresh-inventory
<fresh.json> --bound-sha256 <authorized-sha256> --authorized-job-count <count>
--output-root <existing-staging-root> --run-dir <new-log-dir>
--confirm-special-feature-rip` is the only special-feature execution boundary.
It accepts only the distinct bound-manifest mode, revalidates the exact digest
and fresh inventory, requires the separately authorized exact job count,
preflights every staging collision and conservative free space before starting,
and processes titles sequentially. The run directory must be new and outside
the staging root; its `STOP` marker cancels the current job while preserving
partials. The executor must stop the queue on the first failure and may not
rename into a library, transcode, delete, or eject.

`mkv-match plan-special-feature-resume <original-bound.json>
--original-inventory <saved.json> --fresh-inventory <saved.json>
--events <events.jsonl> --bound-sha256 <digest> --manifest-out <new.json>` is
saved-data-only. It validates the original binding, requires a
metadata-identical fresh inventory, trusts only completed jobs recorded in the
append-only event log, excludes those jobs, and assigns every unfinished job a
new collision-refusing staging directory. It never accesses a disc or media.
The resulting bound manifest has no execution authority and requires a new
exact job-count authorization before execution.

`mkv_episode_matcher/disc/batch_ripper.py` is the synthetic-tested single-open
MakeMKV adapter. It may plan `mkv ... all` only when a complete saved inventory
proves one minimum-runtime cutoff selects exactly the authorized title set. It
must refuse arbitrary subsets, unsafe or duplicate MakeMKV output names,
collisions, and multiple drives. A separately authorized physical validation on
2026-07-30 confirmed that the installed MakeMKV version accepts `all`, opens
the drive once, and saves exactly the titles selected by an exact
`--minlength` cutoff. It also showed that MakeMKV renumbers selected output
suffixes contiguously (`_t00`, `_t01`, ...) rather than retaining original
title indexes. Batch planning must therefore require a strict `_tNN.mkv`
inventory suffix matching the original title index, sort selected titles by
index, derive contiguous batch output names, and refuse unrecognized or
ambiguous names. The adapter is wired only through the guarded strategy
selection described below; per-title execution remains the fallback.

`plan-rip` now stores a path-redacted per-drive inventory signature and whether
the reviewed title set is representable by one exact cutoff. It never stores
the private MakeMKV output names used to calculate that signature.
`execute-rip --fresh-inventory <report.json>` rebinds only explicit fresh saved
reports to those signatures and automatically selects single-open execution for
eligible drives. A changed inventory stops execution; an ineligible drive, an
older manifest, or a drive without an explicitly supplied fresh report retains
per-title execution. `disc/rip_orchestrator.py` keeps one strategy per drive,
preserves parallel-across-drives behavior, and never retries a failed or
partially written batch per title. Batch output is first verified as an exact
set in isolated staging; only then may the already-authorized manifest
finalization place unique files in the flat season folder. All final collisions
must be detected before MakeMKV starts.

Fresh Web UI drive preparation now selects every title in the complete
zero-minimum inventory by default. Except for explicit saved future-rip skips,
this makes one targeted `mkv disc:N all --minlength=0` child the normal physical
strategy. Classification and unwanted-title review happen after the verified
batch; nothing is deleted automatically. A reviewed exclusion or inventory that
cannot form one safe exact batch retains per-title recovery. Recovery titles
remain sequential on that drive, and the process-control claim must prove the
previous child exited before the next child can start. A failed batch is never
silently retried per title in the same run; preserved partials require review
and a newly authorized attempt. Whole-disc image acquisition remains an
available recovery path rather than a prerequisite for normal physical ripping.

Whole-disc acquisition scope and disc-aware episode-matching scope are distinct.
The former may contain every zero-minimum MakeMKV title; the latter must exclude
every durable or context-classified downstream skip. Non-episodes may be
preserved for review but must never block disc-level analysis, establish an
episode-range anchor, consume an episode candidate, or participate in residual
elimination. Older contracts carrying broad inventory expectations must be
narrowed by exact-fingerprint durable skip dispositions at coordination time.
Read-only preparation must persist the latest path-free classifier-derived
relevant index set separately from acquisition manifests; that exact-fingerprint
scope is authoritative for disc-coordinator readiness and range title counts,
and forgetting the disc must remove it.

Do not apply that fresh-disc whole-inventory rule to an exact disc that already
has an executed per-title or single-open batch failure. Failed-disc preparation
is a recovery workflow: intersect the current classifier-selected relevant
titles with the durable acquisition scope of the exact fingerprint's failed
jobs, then
remove titles already verified in staging or present in Jellyfin. A newer
zero-minimum inventory or an older full-disc awaiting-review job must never
expand legacy recovery to tiny, menu/control, or otherwise inventory-only
titles. The Web UI must treat a recovery plan created before the latest failure
as stale: it may offer a read-only `Refresh failed-disc recovery`, but it must
not execute the stale missing-title set. After refresh, the newly prepared
relevant scope is authoritative; do not fall back to unfinished indexes from an
older failed job when those indexes are outside that scope. The refresh must
show visible in-progress and failure feedback because it may spend substantial
time in a read-only MakeMKV inventory. This recovery preparation is not rip
authorization.

Preserved outputs from a failed single-open batch must be offered for exact
read-only verification before any rerip. MakeMKV 0-byte menu and navigation
outputs are treated as graceful skips (with zero output bytes and no file
distribution) rather than batch errors. Inventory-only titles outside the
classifier-derived matching scope must not appear as missing recovery work. Once
every relevant title is verified in staging/Jellyfin or explicitly skipped, the
disc counts as acquisition-complete and the configured automatic-eject flow may
run while downstream identification and transcoding continue.
When recovery narrows an original whole-inventory batch, preserved MakeMKV
`_tNN` suffixes retain their original batch ordinals; they must not be
renumbered against the smaller relevant-title subset. The isolated batch
directory's first-title index may anchor this mapping only for the fresh
whole-inventory strategy, with the normal size/cohort verification retained.

`POST /rip/preview` and `disc/rip_preview.py` are the saved-report-only web
planning boundary. They read only the explicit preflight JSON paths submitted
by the caller, build the plan in memory, rebind those same reports, optionally
inspect an existing output root for collisions, and return only relative
destinations and path-redacted strategy data. The response must always state
`execution_authorized: false`. A preview digest excludes volatile creation
timestamps, so identical reports and contexts produce the same authorization
identity. Preview must never discover drives or media, write a manifest, create
a directory, or invoke MakeMKV.

`disc/orchestration_store.py` is the durable SQLite control-plane boundary.
The default database is local under the configured application data directory,
not the repository. It stores opaque job IDs, immutable path-redacted previews,
exact plan/authorization digests, validated states, idempotency keys, and
append-only path-free events. It must never persist report paths, output roots,
commands, environments, credentials, or disc labels. Job creation and every
control transition require a caller-stable idempotency key; retries must not
append duplicate events.

The `/rip/jobs` API can persist and inspect previews and record exact-digest
authorization, queue, execute, pause, stop, and resume transitions. `start`
still means only `authorized -> queued`. Physical work is available only at
`POST /rip/jobs/{job_id}/execute`, which requires the exact plan digest, exact
authorized job count, an explicit MakeMKV executable, a new run directory,
bounded execution options, and a separate confirmation. It constructs the
production adapter only inside that request; tests override the queue-runner
dependency. Creating a durable job requires an existing output root and writes
absolute report/output paths only to the separate local
`disc/private_bindings.py` database. Private paths must never appear in the
public database, routine API responses, or events.

All `/rip` routes currently require a loopback client and same-origin request.
Remote and phone control must remain disabled until secure pairing and
authentication are implemented. Broad application CORS settings are not
authorization to bypass this route guard.

`disc/rip_dispatcher.py` is the private queued-job handoff. It has no default
physical executor: tests inject fakes only. Before atomically claiming a job,
it reloads the private binding and revalidates the stable plan digest, exact
fresh reports, review state, and destination collisions. Dispatch keys are
idempotent, and retrying a terminal dispatch must not execute it twice.
Incomplete executor results become a redacted failure. On process restart,
`running` and `pause_requested` jobs reconcile to `paused` for output review.
A queued state must never be represented as running before the dispatcher
claims it.

Do not invoke the production MakeMKV executor until the exact live canary plan
digest, title/job limit, output root, new run directory, executable, timeout,
fresh inventory, and parallel-drive scope have been presented for separate user
authorization. The existence of the API route is never live authorization.

`disc/rip_execution_adapter.py` is the explicit production adapter. It is
API-wired only through the separately confirmed execute route and must never be
constructed by preview, save, authorize, or queue operations. It requires an exact
MakeMKV executable, a new dedicated run directory outside the media output
root, timeout, and optional drive bound. It delegates only to the existing
parallel auto-orchestrator and verifies that the returned job-ID set exactly
matches the authorized manifest. Tests must inject a fake queue runner; merely
having this adapter in the repository is not authorization to construct it
against MakeMKV or dispatch a live job.

`tests/test_rip_api_composition.py` exercises the complete in-process web
handoff with synthetic reports and a fake queue runner: FastAPI durable job
creation, isolated binding, exact authorization, queueing, the execute endpoint,
private dispatch, and adapter result validation. Passing this test does not
authorize physical media access.

`pipeline.py` is the restartable four-stage checkpoint engine. It requires the
exact `rip`, `identify`, `transcode`, and `organize` runners, passes one hashed
JSON contract to the next stage, validates every completed contract on resume,
and records only a redacted exception type on failure. A pipeline checkpoint is
private because it contains contract paths. The engine has no implicit media
runner and its existence is not authorization for any stage.

`pipeline_queue.py` is the private durable item scheduler. Verified MakeMKV
results enter `identify`; successful items then advance to `transcode` and
`organize`. SQLite claiming permits only one running item per downstream stage.
Different stages may overlap, so organization must not wait for an unrelated
HandBrake transcode, while identification, transcode, and organization each
remain serialized within their own stage. Per-item stage order and all
collision/refusal checks remain mandatory. MakeMKV's separately bounded
one-worker-per-drive parallelism is unchanged. Failed and review-required items
release their stage claim so unrelated items continue. Restart reconciliation
returns a running downstream item to the beginning of its current stage. Public
queue responses and events must never expose private artifact or media paths.
An already-paused durable downstream queue must remain paused across backend
restart and require an explicit user resume. The startup grace-period timer may
arm only when the durable queue was active before startup.

All-season identification also keeps a private append-only per-title audit
under the identification-evidence root. Each analysis retry has a fresh opaque
run ID. The audit records workflow boundaries, concise provider attempts, every
OpenSubtitles episode candidate that was selected or rejected (including
unavailable references), the bounded Gemini candidate set and returned choice,
and the final per-title outcome. Audit records must contain no transcript text,
reviewer scene description, media path, credential, or provider secret. Normal
`/pipeline/items` polling remains bounded to its concise recent attempt summary;
the complete trace is returned only by the explicit path- and dialogue-redacted
per-disc identification-audit route. Audit write failure must be surfaced in
the application log but must never change or stop an identification decision.
Gemini's safe trace may report the candidates supplied, returned selection,
confidence, two-pass consistency, runtime check, and pipeline rejection rule;
it must not claim an unreturned per-candidate score or hidden provider rationale.

`pipeline_adapters.py` contains explicit identify, transcode, and organization
adapters. Identification must call the legacy engine with `dry_run=True` and
must create an immutable contract rather than rename. Transcode is one verified
HandBrake job and preserves collision partials. Organization refuses an
existing destination and requires an explicitly confirmed adapter. These
adapters are not attached to an automatic background worker yet: the current
rip authorization covers MakeMKV only. Before attachment, add a combined exact
pipeline authorization that binds the HandBrake profile, encoded root,
destination library root, item/destination set, tool paths, and move/copy
policy. Queue admission is not transcode or organization authorization.

Post-transcode FFprobe verification must retain the encoded width, height, and
field order. Final Jellyfin television placement appends the verified resolution
as a version suffix using exactly ` - <height>p` or ` - <height>i` before the
extension, for example `Series - S01E01 - Title - 1080p.mkv`. The suffix is
derived from the encoded output, never a requested profile or source filename.
An existing exact version destination remains a review conflict and must never
be overwritten automatically. Jellyfin 12 adds episode-version grouping; older
servers may display version-suffixed episodes separately, so version coexistence
must remain an explicit conflict decision rather than an assumed safe merge.

The Web UI configuration persists only non-secret pipeline settings: rip and
encoded staging roots, Jellyfin TV/movie roots, external tool paths, the default
HandBrake profile name, and whether unattended processing is requested. Gemini,
TMDb, and OpenSubtitles values must continue to use the ignored `.env`; API
responses expose configuration status and management links only. The monitoring
dashboard may list path-redacted durable jobs and serialized downstream stages.
It must report `watcher_attached: false` until a real background watcher with a
combined immutable authorization is installed. Merely enabling the preference
must never falsely claim that a disc is being scanned or processed.

`POST /rip/special-features/execute` is the web execution boundary for an
already reviewed special-feature binding. It repeats the exact bound-manifest
digest and fresh saved-inventory validation, exact job-count confirmation,
collision/free-space checks, sequential queue behavior, and isolated run-log
requirements of the CLI executor. Tests inject a fake queue runner. Planning or
binding remains non-authorizing, and the endpoint must not be invoked against a
disc without separate exact authorization.

`disc/batch_validation.py` and `disc/batch_validation_executor.py` provide a
saved-data plan and synthetic-tested binding/execution boundary for that future
physical validation. They are not CLI-wired. Any live use must present the
exact immutable manifest digest, fresh metadata-identical inventory, exact
title count, cutoff, estimated size, collision-safe output root, and new run
directory, then obtain separate authorization. The executor must retain
redacted logs, enforce STOP/timeout, conservative free space, and preserve
every partial or unexpected output. It must never promote, transcode, delete,
or eject validation output.

`media/special_feature_evidence.py` reads only explicitly authorized MKVs. It
preflights every input, executable, duration, audio-stream index, and output
collision before creating private per-item evidence. It may run at most three
independent items concurrently, creates collision-refusing contact sheets,
private OCR text, and only the authorized short audio samples, and returns a
path/dialogue-free metrics report. Codex must state the exact MKVs and derived
evidence and obtain explicit authorization before a real run. Failed evidence
directories are preserved and retries must use a new output root.

`mkv-match plan-audio <saved.ffprobe.json> [<saved.ffprobe.json> ...]` reads
saved FFprobe JSON only. It ranks audio streams and plans sample windows,
downmix intent, measurements, and fallback order. It must not invoke FFprobe,
FFmpeg, transcription, or any media operation.

`mkv-match build-unmatched-bundle <saved-transcripts.json>
<episode-catalog.json> --bundle-out <new-private.json> --report-out
<new-safe.json>` reads only the two explicit JSON reports. It selects up to
three short, non-duplicate transcript excerpts per file, ranks the authoritative
catalogue locally, and writes a private transient bundle plus a dialogue-free
audit report. It must not discover media, invoke Whisper/TMDb/Gemini, or mutate
media. Treat the transient bundle as private because it contains dialogue; keep
it out of Git and logs. Both outputs must be new, distinct paths.

`mkv-match collect-transcripts <explicit.mkv>... --media-id <id>...
--probe-report <saved.ffprobe.json>... --report-out <new-private.json>
--metrics-out <new-safe.json> --confirm-read` is the only batch Whisper
collection boundary. It requires exactly one redacted ID and saved sanitized
FFprobe report per explicit MKV, processes files sequentially on CPU, loads one
ASR provider for the batch, and uses three planned sample windows. It tries the
normal default audio stream first—including usable multichannel audio—and
falls back through other non-commentary streams only for weak, silent, or
failed samples. Temporary mono WAVs must use collision-refusing FFmpeg output
and be removed when the batch ends. The private report contains dialogue; the
metrics report must contain no dialogue or source paths. A file requiring audio
review must prevent downstream bundle generation without stopping collection
of later files.

`collect-transcripts --sampling-mode intro` is a targeted supplement for
low-confidence anthology results. It reads one 30-second window beginning at
the caller-reviewed `--intro-start` time instead of the standard three windows.
`--preferred-audio-stream` may prioritize one stream index from the saved
FFprobe report when prior safe diagnostics justify it. Both controls require
the same exact file approval and `--confirm-read`; they do not broaden media
discovery.

`mkv-match merge-transcript-reports <private.json>... --report-out
<new-private.json>` merges only explicit private reports. `--file-id-prefix`
can exclude a control item, and `--enrich-duplicates` combines additional
windows for the same redacted file ID only when durations agree. It performs no
media or provider access and refuses overwrites. `--map-file-id SOURCE=TARGET`
may reconcile explicitly reviewed redacted-ID aliases; every source must exist
in the input reports, mappings must be unique, and duration validation still
applies before enrichment.

`mkv-match fetch-aired-catalog <reviewed-tmdb-id> --report-out <new.json>`
sends only the numeric show ID to TMDb and saves a path-free aired catalogue.
Network failures must be converted to redacted service errors before reaching
Typer; provider exceptions must never render configuration or credential
locals.

`mkv-match plan-disc-sequences <private-transcripts.json>
<aired-catalog.json> --group GROUP=FILE,FILE... --report-out <new-safe.json>`
is saved-data-only. Every evidence file must appear exactly once in an explicit
group. File order within a group is title order; repeated `--group` options are
in reviewed chronological catalogue order. Titles must map to contiguous
episodes within each group, groups may have catalogue gaps, and the dynamic
program enforces ordered one-to-one use. The report retains independent
lexical scores, selected sequence scores, and best-versus-next margins but no
dialogue or paths. It must route weak margins to review and must never infer
disc groups or chronology from filenames.

Sequence matching is advisory only and must never skip, short-circuit, replace,
or satisfy the independent episode-identification process. A sequence plan may
act as a map for choosing the next candidates to test, bounding provider work,
or displaying reviewed suggestions, but sequence position, contiguity, disc
title order, or a sequence score must never be the reason an episode receives
its canonical name. Every automatically named episode still requires the normal
independent evidence boundary (for example, qualifying Whisper/OpenSubtitles
dialogue matches or a separately confirmed Gemini decision) and the configured
confidence rules. Evidence-based elimination may remove episode candidates
already assigned confidently on that same disc and may identify the only
remaining plausible candidate when its own evidence meets the residual-match
rules; this must not assume that MakeMKV title indexes are chronological.

An unresolved TV title held at `gemini_descriptive_review_required` remains an
automatic disc-analysis candidate. The coordinator must retry it when the set
of identified same-disc title indexes changes, because new independent sibling
assignments may make residual elimination conclusive. Retry suppression must be
keyed by the exact fingerprint plus that resolved-index set, not by fingerprint
alone, so unchanged evidence cannot form a loop and ordinary transcode or
organization state changes cannot retrigger matching.

Automatic all-season TV assignments use identification-policy version 4. A
normal OpenSubtitles assignment requires two qualifying independent windows at
the configured threshold with a hard 70-percent floor, plus the normal margin
and runtime checks. Residual
elimination is available only after confident same-disc assignments reduce the
pool and still requires the unresolved title's own qualifying evidence and
residual margin. The only automatic assignment sources accepted by the identify
adapter are `opensubtitles-two-window`,
`opensubtitles-residual-elimination`, and `gemini-two-pass`; a sequence plan is
never an assignment source. Older sequence-only contracts must return to
identification or explicit repair review before further automatic processing.

Ambiguous residual and Gemini candidates must pass an evidence-anchored
same-disc episode-range fence before automatic assignment. The fence requires
at least two independently assigned episodes from one season that fit within
the disc's known title count. It admits every contiguous episode window capable
of containing those anchors, so it may remove an impossible out-of-range
candidate but must not infer episode identity or MakeMKV title order. Direct
two-window subtitle matches establish current-run anchors; restart anchors must
retain current-policy OpenSubtitles provenance or an explicitly reviewed or
deterministic source. Gemini outcomes and legacy sequence history must never
establish the fence. Every applied fence and rejected provider candidate must
remain visible in the private identification audit.

Immediately before any episode assignment advances from completed-disc
analysis, RipWeaver must run a provider-independent whole-disc coherence gate.
The gate combines the current run's proposals with durable episode outcomes
for other relevant title indexes on the exact fingerprint. The combined set
must contain unique episodes from one season and fit within an episode span no
wider than the disc's known relevant-title count, even when saved season
context is absent. Failure must withhold every current proposal under an
explicit review code; coherence may reject assignments but must never supply
episode identity evidence.

A held organize-stage wrong-episode collision may be corrected only through
the metadata-only correction boundary. The replacement must be an already
displayed OpenSubtitles candidate, be the sole unassigned candidate inside a
range established by at least two independently matched same-disc siblings,
and preserve every verified encoded-media field exactly. The corrected item
must remain held until a separate explicit Jellyfin-placement confirmation.

`mkv-match plan-tv-organization <sequence-plan.json> <aired-catalog.json>
--library-root <existing-tv-root> --series-name <canonical-name> --report-out
<new-safe.json>` is destination-name inspection only. It accepts only a fully
proposed sequence plan, generates canonical
`Series/Season XX/Series - SXXEYY - Title.mkv` relative targets, and never
creates directories. An exact case-insensitive filename or any existing file
containing the same episode ID is a deduplication/review conflict. The report
must not contain the absolute library root. This command is not authorization
to rename, move, overwrite, delete, or transcode anything.

`mkv-match plan-handbrake-batch <organization-plan.json> --source
MEDIA_ID=MKV... --output-root <existing-root> --manifest-out <new-safe.json>`
is file-metadata and capability inspection only. Source mappings must exactly
cover the conflict-free organization plan, use one source directory, and refer
to distinct nonempty MKVs. The planner invokes only `HandBrakeCLI --help` to
confirm the requested AMD VCN encoder, checks destination/partial collisions
and conservative free space, and stores source basenames plus relative encoded
destinations without either absolute root. Missing staging directories are
reported, never created. This command is not transcode authorization.

`mkv-match execute-handbrake-batch <reviewed-manifest.json> --source-root
<existing-source-root> --output-root <existing-staging-root> --run-dir
<dedicated-log-dir> --confirm-transcode` is the only batch-transcode boundary.
Codex must present the exact immutable manifest and obtain explicit
authorization for the exact job limit before invoking it. The executor hashes
the manifest, revalidates every source basename/size, destination, free-space
requirement, and collision, and refuses a run-log directory inside either
media root. It defaults to at most two concurrent jobs and at most two total
jobs per invocation; raising `--max-jobs` is a separate scope decision. It
creates only destination directories required by jobs attempted in that
invocation. Every job still uses the one-file partial/FFprobe/promote boundary.
Unknown final or partial outputs block only that job and are never overwritten;
other jobs continue. A failed job is recorded by type without a path-bearing
exception message, while unaffected jobs continue.

Batch events are append-only, path-free JSONL. Resume is permitted only when
the manifest SHA-256 matches and every previously completed output still has
the recorded size; changed or missing completed output stops the resume. A
`STOP` or `PAUSE` marker in the run directory prevents new chunks from
starting. Up to the currently active two-job chunk is allowed to finish and
verify; markers do not terminate or corrupt an in-flight encode. Remove
`PAUSE` only after review before a separately confirmed resume. Batch staging
is never a media-library destination, and completion is not authorization to
move, replace, or delete source or library files.

New HandBrake manifests use `schema_version: 2`; their
`destination_relative` is the encoded-output contract while `source_name`
remains the original input basename. Version-1 manifests remain executable for
resume compatibility. `execute-tv-organization` must use the encoded root and
append-only HandBrake event log, verify the exact manifest digest and every
completed output size, and refuse incomplete or changed batches. It must never
organize the original `source_name` as though it were the encoded output.

HandBrake process exits that represent an operating-system interruption are
typed separately from ordinary one-file failures. Windows
`DBG_TERMINATE_PROCESS`/`STATUS_CONTROL_C_EXIT` and POSIX HUP/INT/TERM must stop
the executor from dispatching another chunk while allowing the already-active
chunk to settle. A later separately confirmed resume may retry only jobs whose
prior dispatch is present in the exact append-only event log. It must preserve
every earlier partial and select a new collision-refusing
`.retry-NNN.partial.mkv` path. An unlogged partial remains an unknown output and
blocks that job. A verified final may coexist with preserved diagnostic
partials; later resume validation trusts only the recorded final size and never
deletes those partials.

Codex must state the exact MKVs that will be read and obtain explicit approval
for that set before invoking `collect-transcripts --confirm-read`. Approval to
implement or test the collector is not approval to read real media. Tests must
use fake extractors, synthetic WAVs, and fake ASR providers.

`mkv-match plan-gemini-unmatched <private-bundle.json> --report-out
<new-safe.json>` validates a future schema-constrained Gemini request and writes
only a dialogue-free request plan. It must not contact Gemini. Any future live
Gemini command requires explicit approval because it transmits selected
transcript excerpts to an external provider.

`mkv-match probe-mkv <explicit.mkv> [<explicit.mkv> ...]` is the only current
live-media inspection command. It runs fixed FFprobe metadata flags
sequentially, saves only sanitized reports under `.mkv-preflight/ffprobe/`, and
does not retain source filenames. Codex must not run it against real media
without stating that it will read the file and obtaining explicit approval for
the specific test input. It must never accept a directory or discover media
implicitly.

The Library Scan episode-repair channel is separate from normal identification.
Its discovery step may recursively inspect filenames, sizes, timestamps, and
private RipWeaver provenance under the configured Jellyfin TV root, but it must
not open media or contact a provider. The default scope contains only current
Jellyfin episode IDs whose retained dossier has a matched `tv-local` sequence
attempt. Before Whisper or subtitle lookup, the UI must present the exact MKVs
and bind confirmation to the inventory digest. Each file may be compared only
to the episode already claimed by its filename; this channel must never assign
another episode, use disc/title order, or treat sequence output as evidence.
Confirmed names remain unchanged. A mismatch may receive a generic, non-SxxExx
name in the same season folder only through a separate exact-result-digest and
exact-file confirmation; inconclusive results remain unselected by default.
The apply boundary must revalidate source size and timestamp, preflight every
destination before changing any name, and refuse overwrite. A generic rename
is intended to make the file eligible for a later standard Season 0X scan; it
is not itself an episode identification.

`mkv-match plan-rip <fresh-report.json>... --manifest-out <new.json>` creates a
path-redacted manifest for every nonempty inventory title; classification may
remain unresolved until after ripping.
It does not access discs or media. `mkv-match execute-rip <manifest.json>
--output-root <existing-directory> --confirm-rip` is the only authorized rip
boundary. It runs one title at a time, creates a new unique staging directory,
refuses overwrites, writes redacted JSONL events under `.mkv-runs/`, supports a
`STOP` marker, and pauses the queue on the first failure. Codex must present the
exact manifest and obtain explicit authorization before invoking it.

Before physical-disc access:

1. Test command construction and robot-output parsing using synthetic data.
2. Test against saved, sanitized robot output when available.
3. Confirm no ripper, transcoder, or competing MakeMKV process is active.
4. State that the access will spin/read the disc and obtain explicit approval.
5. Scan sequentially and preserve raw output for diagnosis.

Do not add `mkv`, `backup`, eject, or filesystem mutation behavior to the
preflight module. Ripping must remain behind the separate manifest,
confirmation, staging, and logging boundary in `disc/ripper.py`. Backup and
eject operations remain prohibited.

## Change Discipline

- Inspect `git status` before and after work.
- Keep generated reports, media, logs, `.env`, and caches out of commits.
- Preserve pre-existing untracked files.
- Use tests for parser edge cases, failed commands, timeouts, malformed robot
  records, path safety, and credential non-persistence.
- Update `docs/PROJECT_STATUS.md` when a phase, safety boundary, or recommended
  next step changes.
