# Integration Plan

## Objective

Build one safe media-ingest application that combines:

- the existing `mkv-episode-matcher` CLI, FastAPI backend, React frontend, and
  transcript/subtitle matching engine;
- selected Riplex concepts for MKV scanning, runtime matching, deduplication,
  Plex-style organization, MakeMKV integration, and disc orchestration; and
- reviewed portions of the legacy MakeMKV, matcher, proxy, inventory, and
  staging scripts.

The combined system should support unattended work eventually, but unattended
operation is not an initial milestone. Every mutating action must be planned,
observable, interruptible, restartable, and explicitly enabled.

## Existing Components

### mkv-episode-matcher

Keep as the application foundation:

- Typer CLI in `mkv_episode_matcher/cli.py`;
- FastAPI routes and WebSockets in `mkv_episode_matcher/backend/`;
- React/Vite frontend in `mkv_episode_matcher/frontend/`;
- `MatchEngineV2`, subtitle providers, Faster Whisper integration, and matching
  models in `mkv_episode_matcher/core/`.

The matcher currently extracts multiple audio chunks, transcribes them, compares
them with subtitle windows, votes across chunks, and may rename matched files.
Renaming must be separated from identification before it joins disc automation.

### Selected Riplex Capabilities

Adopt or adapt concepts rather than placing a second orchestrator around the
application:

- FFprobe-based MKV inventory;
- runtime-based feature, episode, and extras classification;
- deterministic duplicate detection;
- Plex-compatible organization plans;
- a MakeMKV adapter;
- durable disc/job orchestration.

Confirm the upstream version, public API, CLI behavior, license, and data models
before importing code. Prefer a narrow adapter or a documented reimplementation
when direct reuse would create competing ownership of drives or output folders.

### Special-feature discs

Bonus discs require a separate plan from episode-cluster selection. Short
runtime is not synonymous with junk: deleted scenes, trailers, interviews,
featurettes, shorts, and clips can all be much shorter than a movie or episode.
The special-feature path therefore combines:

1. a saved MakeMKV title/stream inventory;
2. a reviewed, provider-neutral per-disc feature catalogue, initially populated
   from a selected DVDCompare release or manually;
3. global one-to-one runtime assignment;
4. explicit play-all/component relationships;
5. post-rip media fingerprints for actual duplicate confirmation;
6. a collision-safe Jellyfin/Plex organization proposal.

`mkv_episode_matcher/disc/special_features.py` and
`mkv-match plan-special-features` now implement the first plan-only portion.
The planner uses a minimum-cost global assignment so similar runtimes cannot
claim the same catalogue entry. It proposes Jellyfin-recognized folders such as
`behind the scenes`, `deleted scenes`, `interviews`, `scenes`, `shorts`,
`featurettes`, `clips`, `trailers`, and `other`. It recommends only
catalogue-matched individual features. Play-all/summed titles,
metadata-duplicate candidates, menu-length clips, and unmatched titles remain
held for review. Equal-best runtime assignments are also explicit ambiguities:
the planner never breaks them arbitrarily. Metadata equality never proves
duplicate content. Titles containing multiple audio streams carry a
`preserve-all` policy because a bonus-disc audio archive may store distinct
features as separate streams within one title.

The catalogue is an exact, path-free JSON contract with stable feature IDs,
display titles, feature types, runtimes, representation types, source
references, and optional explicit play-all component IDs. A feature can be a
standalone video title, multi-audio title, menu-bound item, audio-only item,
still gallery, or unknown. Only representations expected to exist as titles
participate in runtime matching or the missing-title report. Provider adapters
translate into this contract rather than letting DVDCompare-, Riplex-, or
review-site-specific structures enter the planner. Release selection remains
necessary because regions and editions can contain different bonus discs.

The identification ladder is deliberately provider-independent:

1. release catalogue plus global runtime assignment;
2. FFprobe structure and exact/perceptual content fingerprints;
3. representative thumbnails and contact sheets;
4. embedded text, subtitle OCR, and short transcription evidence;
5. an optional bounded external model only for the unresolved candidate set;
6. explicit human review.

A provider outage or lack of a catalogue must reduce confidence, not lose
content. After a separately authorized diagnostic rip, every plausible
non-menu title remains in isolated staging and receives a content fingerprint.
If all identification layers fail, the organization planner may propose
`Movie (Year)/extras/Unidentified Extra - <short-content-fingerprint>.mkv`.
That neutral name is collision-resistant and valid for Jellyfin, while the item
remains in the review queue for a later rename proposal. A filename must never
be generated from a path, drive identity, title index alone, or an unverified
model guess. Menu candidates and unverified duplicates remain held rather than
being sent through this fallback automatically.

`mkv_episode_matcher/disc/special_feature_manifest.py` and
`mkv-match plan-special-feature-rip` implement the next saved-data-only
boundary. The command rebuilds the validated feature plan from one explicit
inventory and catalogue, then writes a digest-bound diagnostic manifest. It
includes matched, ambiguous, unmatched-review, and metadata-duplicate
candidates; excludes menu and play-all candidates by default; retains every
ambiguity candidate ID; and schedules FFprobe, cryptographic/content
fingerprinting, perceptual fingerprinting, and escalation evidence after a
future rip. Multi-audio titles also require a complete audio-stream inventory.

This diagnostic manifest is deliberately not executable. It contains no drive
index, MakeMKV arguments, source path, or library destination and declares
`execution_authorized: false`. Its isolated staging names derive from the
validated plan digest.

`mkv_episode_matcher/disc/special_feature_binder.py` and
`mkv-match bind-special-feature-rip` implement the fresh-preflight binding
boundary. The caller supplies the exact reviewed diagnostic SHA-256. The binder
then compares the full inventory signature and each selected title's index,
runtime, size, and audio-stream count. A stale, changed, or substituted
inventory stops safely. Its output uses the distinct
`special-feature-rip-binding-plan` mode, remains
`execution_authorized: false`, and is intentionally rejected by the episode
rip loader.

The next layers are:

- a cached DVDCompare adapter with release selection and saved/synthetic
  fixtures;
- adapters for other public/provider catalogues using the same normalized
  contract, with manual catalogue entry as the final metadata fallback;
- a separate special-feature executor that accepts only the bound manifest
  mode, rechecks its immutable digest and fresh metadata, and requires explicit
  exact-manifest rip authorization;
- post-rip FFprobe and perceptual/content fingerprint evidence;
- optional short sample, OCR, or transcription evidence for unresolved titles;
- preservation or reviewed splitting of multi-stream audio archive titles;
- HandBrake profile selection by content type;
- a final organization plan using Jellyfin's supported extras folders.

None of these layers should directly encode into or mutate the configured media
library. Current Jellyfin documentation confirms that movie extras can live in
typed subfolders under the movie folder, including `behind the scenes`,
`deleted scenes`, `interviews`, `scenes`, `shorts`, `featurettes`, `clips`,
`other`, `extras`, and `trailers`.

### Reviewed Legacy Work

Reuse small ideas, not whole scripts:

- MakeMKV drive discovery and robot-output parsing;
- title duration and playlist extraction;
- progress-message parsing;
- drive-letter to MakeMKV-index mapping;
- subprocess output streaming;
- basic show/season filename heuristics;
- source/library inventory separation;
- review manifests and dry-run staging;
- audio-track discovery that demonstrated default 5.1 plus alternate stereo
  layouts.

Do not reuse automatic target-folder cleanup, immediate rename/delete behavior,
daemon-thread orchestration, season-folder sweeping, or low-confidence AI
renaming.

## Target Workflow

```text
Discover
  -> read-only disc scan
  -> normalized title/stream inventory
  -> title classification and duplicate groups
  -> proposed rip plan
  -> explicit approval
  -> isolated per-disc staging rip
  -> output verification
  -> FFprobe inventory and audio diagnostics
  -> runtime candidate matching
  -> subtitle/transcript episode identification
  -> confidence gate and ambiguity review
  -> deterministic duplicate detection
  -> optional transcode
  -> post-transcode media verification
  -> proposed Plex/Jellyfin organization plan
  -> explicit approval
  -> transactional rename/copy/move
  -> quarantine or retention decision
```

Planning and execution are separate APIs and job states. No stage may infer
permission for the next destructive or expensive stage.

Each disc owns an independent pipeline state. When a disc completes a stage and
passes its gate, it may enter the next stage immediately while other discs
remain in earlier stages. Parallelism is bounded separately for optical I/O,
lightweight metadata work, ASR, and transcoding.

TV staging uses the matcher's native hierarchy while retaining collision
isolation:

```text
TV Shows/
  Canonical Series Name/
    Season 01/
      Disc 02 [disc-02]/
        <fingerprint>/
          title-000/
            source.mkv
```

The matcher searches ancestors for the `Season XX` directory, so per-disc and
per-title safety directories do not destroy context. If a box-set volume spans
seasons or the season is unknown, stage it under
`TV Shows/<Series>/Unmatched/<volume and staging ID>/`; do not convert a volume
number into a season number.

MakeMKV-assigned names are not unique across discs. After a single new output
passes size/count verification, the rip worker assigns a collision-resistant
staging basename:

```text
disc-02-031fa38fcc781d5c-title-003.mkv
```

The source-created directory is unique and pre-existing targets are refused.
When a later organizer proposes the canonical Plex/Jellyfin filename, an
existing destination is a deduplication/review event, never an overwrite.

## Proposed Architecture

### Disc adapter

Responsibilities:

- discover MakeMKV drive indices;
- run read-only `info`;
- parse `DRV`, `CINFO`, `TINFO`, `SINFO`, `MSG`, and progress records;
- later, execute one explicitly approved title rip;
- expose cancellation and retain exact process results.

The read-only implementation begins in `mkv_episode_matcher/disc/preflight.py`.
Ripping must be implemented in a separate module.

### Media probe

Use FFprobe to record:

- duration, size, codecs, resolution, frame rate, and chapters;
- all audio/subtitle streams, languages, flags, channels, and layouts;
- container and stream warnings;
- stable, deterministic fingerprints.

Raw probe data should be retained so parsers can be tested without accessing
media again.

### Title planner

Classify titles using deterministic evidence:

- episode and movie runtime ranges;
- near-identical and exact runtimes;
- segment maps, chapters, and output size;
- combined-title relationships;
- extras and short-title policy.

The planner produces reasons and confidence, never a rip command.

### Audio diagnostics

For every candidate audio stream:

1. rank language, commentary indicators, default status, codec, and channels;
2. extract a short sample with an explicit stream map;
3. use a documented, dialogue-preserving downmix;
4. measure peak, RMS/loudness, and silence;
5. transcribe and score transcript information content;
6. retry alternate streams when evidence is weak.

Do not modify default-track flags or replace source MKVs with proxies. A
multichannel layout alone is not evidence of silent audio.

### Episode matcher

Use a staged evidence cascade:

1. show/season constraints;
2. runtime candidates;
3. local or downloaded subtitles;
4. multi-sample ASR matching;
5. cross-sample agreement;
6. ambiguity/review queue.

Identification returns a proposed episode identity and evidence. A separate
authorized executor applies a collision-checked name.

#### Anthology and conflicting-order fallback

For sets such as *Faerie Tale Theatre*, normalize numbered volume labels to the
canonical series title before metadata lookup; the volume number must not be
treated as a season number. Attempt the normal runtime/subtitle/transcript path
first. If release-order conflicts or incorrect subtitle assignments prevent a
match:

1. sample several short windows, prioritizing the spoken introduction;
2. evaluate alternate audio streams when the primary is silent,
   low-information, or materially quieter;
3. retrieve candidates across every season with BM25/window search;
4. reject a subtitle cache when retrieval and fuzzy scores are weak or
   contradictory rather than forcing its labels;
5. compare names, characters, and distinctive plot language with a supplied
   authoritative episode-title/summary catalogue;
6. enforce one-to-one assignment within each physical-disc order; and
7. require an explicit aired-order versus DVD-order choice before creating a
   season-folder plan.

An LLM may rank only the supplied authoritative episode candidates and must
return structured evidence and confidence. It may not invent a filename or
rename automatically. Ambiguous results enter the review queue.

The Gemini adapter is a bounded fallback, not the primary matcher:

- send redacted file IDs, runtimes, short transcript excerpts, and an
  authoritative candidate catalogue; never send local paths or library
  inventories;
- request schema-validated JSON containing supplied episode IDs, evidence,
  rankings, and confidence;
- reject any episode ID not present in the request;
- use bounded retries with explicit authentication/rate-limit handling and the
  existing hidden credential-replacement flow;
- call once for a disc-level candidate set when practical, then apply local
  one-to-one assignment and confidence/margin thresholds;
- retain only score/evidence metadata in durable logs, not API keys, paths, or
  full transcripts; and
- produce a review plan only. Rename, move, and organization remain separate
  authorized operations.

Implemented foundation (2026-07-30):

- an aired-order TMDb catalogue builder and local runtime/text candidate ranker;
- a path-free, size-bounded Gemini Interactions request using structured JSON;
- local rejection of invented IDs, missing/duplicate files, duplicate episode
  assignments, malformed confidence, and path-bearing evidence;
- bounded authentication recovery, paid-key fallback, rate-limit/transient
  retries, and redacted errors;
- a plan-only `plan-gemini-unmatched` CLI whose durable output excludes
  transcript excerpts; and
- a twelve-file *Faerie Tale Theatre* aired-order regression fixture.

The transient evidence-bundle collector now joins saved multi-window Whisper
output to the catalogue and local shortlist. Producing those saved windows from
explicit MKV inputs remains a separate, authorization-gated media-reading
stage. A live Gemini request is also a later, separately approved external-data
transmission, not part of planning.

The transient evidence collector is now implemented as
`build-unmatched-bundle`. Its explicit saved-transcript input is:

```json
{
  "files": [
    {
      "file_id": "disc-01-title-000",
      "duration_seconds": 2940,
      "windows": [
        {
          "start_seconds": 300,
          "text": "Short saved transcript window"
        }
      ]
    }
  ]
}
```

The separate catalogue report contains an `episodes` array using
`episode_id`, `season`, `episode`, `title`, `overview`, and optional
`runtime_seconds`. The collector:

- reads only the two explicit reports and never discovers MKV files;
- selects at most three informative, non-duplicate excerpts per file;
- creates a deterministic local candidate ranking;
- retains enough catalogue candidates to support disc-wide one-to-one
  assignment even when `--top-k` is smaller than the number of files;
- refuses existing or identical output paths before writing;
- writes the excerpts only to the explicitly named private transient bundle;
  and
- writes scores, counts, redacted file IDs, and candidate episode IDs—but no
  dialogue or paths—to the durable safe report.

No saved multi-window transcript-text report existed in `.mkv-runs` when this
boundary was added. The next collection stage must produce this schema from
explicitly authorized media sampling without placing dialogue in normal logs.

That collection stage is now implemented as `collect-transcripts`:

- accepts explicit MKV paths only, with one redacted ID and one saved sanitized
  FFprobe report per file;
- requires `--confirm-read` and otherwise exits before loading the ASR model or
  accessing media;
- forces the configured ASR provider onto CPU and loads it once for the batch;
- processes files and windows sequentially to bound CPU and disk load;
- uses the existing three-window audio plan;
- prefers a normal default track, including usable 5.1 audio, and attempts
  stereo/other non-commentary streams only when the preferred track is weak,
  silent, or fails;
- runs constrained FFmpeg arguments without a shell, uses `-n` for temporary
  WAV collision refusal, and returns redacted error categories;
- continues later files when one file needs audio review;
- writes dialogue only to the explicit private transcript report and writes a
  separate path- and dialogue-free metrics report; and
- marks weak/failed files `review-audio`, which the downstream bundle loader
  refuses.

No real MKV has been read through this batch command yet. Its implementation
and tests use synthetic WAVs, fake extraction, and fake ASR providers.

Live validation on 2026-07-30 subsequently read one known-good Dragons control
and all twelve staged Theatre files with explicit authorization. Every file
collected from the normal default 5.1 stream; stereo fallback was unnecessary.
The CPU Faster Whisper `small` INT8 model loaded once per monitored batch.
Dialogue was retained only in ignored private reports, while safe reports kept
redacted IDs, stream IDs, word counts, and signal metrics.

The standard three-window local catalogue ranking correctly led six Theatre
files but left six weak or incorrect. This demonstrated that 25%/50%/75%
sampling misses the spoken story introductions. A tested `intro` mode now
collects one additional 30-second window at a caller-reviewed start time, and
can prioritize a reviewed stream index from saved FFprobe metadata.
`merge-transcript-reports --enrich-duplicates` can add those windows without
discarding existing evidence. Only locally low-confidence files should be
resampled.

The four monitored private reports were merged to twelve Theatre IDs. A
metadata-only TMDb request for reviewed series ID 4603 saved 27 aired episodes.
The offline bundle and Gemini request preview both validate twelve files
against those 27 candidates. No Gemini API request has occurred.

An authorized 60-second introduction pass collected usable dialogue for three
of six selected files and improved the offline top result from six to eight of
twelve reviewed identities. The other three samples were weak, while prior
path-free diagnostics identify four unresolved files with much stronger
dialogue at 120 seconds on stream 2. That targeted fallback must be separately
approved before those files are read again.

That four-file read was subsequently authorized and completed locally. All four
stream-2 samples were usable, and evidence enrichment improved independent
offline top-result agreement to ten of twelve. The remaining ambiguity is now
an assignment problem rather than an audio failure: per-file lexical ranking
does not yet account for the strong contiguous episode ordering within each
disc. Add an offline disc-sequence/global-assignment layer before considering
an external Gemini fallback.

The saved-data-only sequence layer is now implemented. It requires explicit
title membership/order inside each disc and explicit chronological order
between discs; it never infers either from a volume number. It enumerates
contiguous aired-order windows per disc, then uses dynamic programming to find
the best and second-best ordered, non-overlapping global assignments. The safe
report retains per-file independent lexical tops, selected lexical scores,
group alternatives, and local/global margins.

Applied to the twelve-file Theatre evidence, it recovered all reviewed aired
identities without Gemini: disc 05 maps to S03E01-S03E04, disc 04 to
S03E05-S04E01, and disc 03 to S04E02-S04E05. The global score was 0.818 and the
best-versus-next margin was 0.107, so all groups cleared the current proposal
thresholds. This remains a review plan; it did not rename or move media.

The organization planner is now implemented as a separate saved-plan boundary.
It accepts only the fully proposed sequence report, joins episode titles from
the saved authoritative catalogue, sanitizes Windows filename components, and
produces direct season-folder targets without any disc/title subfolders. It
inspects only direct destination filenames. Exact case-insensitive canonical
collisions and alternate filenames containing the same episode ID are routed
to deduplication/review.

A filename-only inspection of the configured TV library found no existing
canonical Theatre series folder and therefore no episode conflicts. All twelve
items are proposed; the canonical Season 03 and Season 04 directories are
reported as missing but were not created. The safe proposal contains only
relative destinations and does not expose the library root.

The HandBrake batch-planning boundary is now implemented. It joins the
conflict-free organization plan to exact caller-supplied staged MKVs using
redacted media IDs, requires exact one-to-one source coverage from one source
root, and writes only source basenames plus relative encoded-staging targets.
It checks nonempty MKV metadata, final/partial output collisions, source/output
separation, conservative free space, the reviewed AMD VCN profile, and live
`HandBrakeCLI --help` capabilities. It does not create missing staging folders
or invoke a transcode.

The Theatre batch contains twelve jobs using `vce_h265`, quality 26. Total
source size is 13,122,380,591 bytes; the conservative requirement with a
10-GiB reserve is 23,859,798,831 bytes, well below available space. No final or
partial output collision was found. Two encoded season directories remain to
be created under the staging prefix, so manifest status is
`ready-after-directory-creation`. The configured HandBrake path was stale;
the previously validated explicit executable succeeded.

The confirmed batch-execution boundary is also implemented but has only been
exercised with synthetic MKVs and mocked HandBrake/FFprobe calls. It consumes
the strict path-redacted manifest schema, hashes the raw manifest for safe
resume, rechecks source basenames/sizes and capacity, and delegates every job
to the existing verified partial/promote adapter. Concurrency is capped at two,
while a separate total-job limit defaults the CLI to one two-job chunk.
Destination directories are created incrementally only for jobs attempted in
that invocation. Unknown collisions and individual failures are isolated;
unaffected jobs continue. Path-free JSONL events support digest-checked resume,
and `STOP`/`PAUSE` markers prevent a new chunk while allowing the active chunk
to finish verification. The event directory is prohibited inside either media
root. No real batch has been authorized or executed.

### Deduplication

Do not use an LLM or filename equality as deletion proof. Build levels:

- exact content hash where practical;
- sampled content fingerprint;
- duration, size, and stream-signature comparison;
- normalized media identity and edition;
- title/segment relationship for disc duplicates.

Dedup results create a review manifest. Initial handling is quarantine or
retention, not permanent deletion.

### Organizer

Generate Plex/Jellyfin-compatible destination proposals:

- movies: title, year, edition, and extras;
- TV: show, season, `SxxExx`, episode title, and multi-episode handling;
- collision and existing-library reports.

The organizer must not mutate the library while scanning or planning.

### Durable orchestration

Use a durable store, likely SQLite initially, with:

- job, disc, drive, title, attempt, and artifact identifiers;
- explicit states and timestamps;
- structured events and retained external-process output;
- cancellation and graceful pause after the current safe unit;
- restart recovery;
- per-drive locks plus separate scan, rip, ASR, and transcode limits.

Parallel-across-drives operation was explicitly approved on 2026-07-30 and is
the default for approved manifests. Each physical drive has one sequential title
worker. Every MakeMKV CLI process is pinned to an explicit `disc:<index>` target;
the coordinator never uses MakeMKV all-drive mode. A completed drive may advance
to read-only validation and matching while other drives continue ripping.
Drive-specific failures pause only that drive; shared failures such as
destination-storage errors may pause the whole batch. CPU-heavy ASR and
transcode stages use separate bounded worker pools.

### Web orchestration control plane

Extend the existing FastAPI/React server instead of creating a second web
application. The server becomes a control plane over the reviewed planners and
executors; API routes must not duplicate subprocess construction or bypass the
CLI safety boundaries.

The normal unattended policy is:

1. an explicitly enabled drive monitor notices media insertion;
2. read-only disc discovery and title planning run automatically;
3. a saved default profile is snapshotted into the disc job;
4. unambiguous jobs progress through rip, verification, matching, and
   encoded-staging without requiring a web selection;
5. a web choice, if made before the applicable stage is dispatched, replaces
   the default for that disc only;
6. ambiguity, collisions, failed verification, unavailable tools/encoders, and
   exhausted credentials pause only the affected pipeline for review.

The web UI is therefore an optional override and monitoring surface, not a
required wizard. Automation must be deliberately armed by the repository owner
with configured drive and staging roots. It never implies eject, deletion,
source replacement, or direct media-library writes. A profile or policy change
does not mutate a job already dispatched; each job retains the version and
digest of the policy snapshot it received.

Add a durable SQLite-backed state machine before adding automatic execution.
Recommended states are `detected`, `scanning`, `planned`, `ripping`,
`verifying-rip`, `matching`, `review-needed`, `transcode-queued`,
`transcoding`, `verifying-transcode`, `staged`, `paused`, `failed`, and
`complete`. Store opaque disc/job/artifact IDs and path-redacted events. Keep
private paths in a separate local store that is never returned in routine API
responses. On restart, reconcile running attempts against retained executor
events and files before dispatching any new work.

The first UI views should be:

- a drive/pipeline dashboard with current stage, progress, retained warnings,
  and per-drive pause/resume controls;
- an automation page that selects the saved default HandBrake profile and audio
  policy;
- an operating-defaults page for staging roots, encoded output, media-library
  destinations, external-tool locations, and concurrency/retention policies;
- a per-disc override panel with an explicit deadline/status showing whether
  the override can still be applied;
- a review queue for unknown series/season, ambiguous episode matches,
  collisions, and failed verification;
- a profile builder backed by live, read-only HandBrake capability discovery.

### Operating defaults and credentials

The web UI is the primary configuration surface for an installed application.
Organize settings into clear sections rather than one untyped configuration
form:

- **Folders:** initial/browse directory, isolated rip-staging root,
  encoded-staging root, Jellyfin TV root, Jellyfin movie root, and local
  cache/log location;
- **Tools:** MakeMKV, HandBrakeCLI, FFmpeg, FFprobe, OCR, and optional
  transcription executable/model selections;
- **Automation:** enabled/disabled state, default HandBrake profile, default
  audio policy, optional override delay, per-drive rip concurrency, bounded ASR
  and transcode workers, and stop/pause behavior;
- **Matching:** metadata order, minimum confidence, provider preference, and
  ambiguity thresholds;
- **Credentials:** TMDb, OpenSubtitles, Gemini, and future integration
  credentials registered through the central credential registry.

Configuring a Jellyfin directory is destination planning information only. It
does not authorize direct encoding into that directory or automatic moves.
Transcodes always finish and verify in encoded staging. A separate organization
stage inventories the configured library, checks episode/movie identity and
collisions, and applies only the owner's configured organization policy. It
never overwrites an existing library file.

Directory settings must be absolute, normalized, and validated for role
separation. Reject dangerous broad roots, identical source/destination roots,
encoded staging nested inside a media library, a library nested inside
temporary rip staging, and any layout that would make recursive discovery feed
outputs back into inputs. Saving a path must not scan media, create missing
trees, or test it by writing a probe file. Offer an explicit folder picker and
separate validation result. Creating a missing directory, if supported later,
is a distinct user action showing the exact target.

Credentials remain write-only:

- configuration GET responses return only configured/missing/invalid status
  and an official provider-management link;
- replacement fields are empty password inputs and never receive the stored
  value;
- submitted replacements go directly through the central credential store into
  the ignored local `.env`;
- credential values never enter configuration JSON, WebSocket events, logs,
  validation errors, or browser persistence;
- a provider test returns only classified status such as valid,
  authentication-rejected, rate-limited, unavailable, or network-error;
- clearing a credential is a separate confirmed action, not an empty form
  submission.

The existing Settings route already implements the core non-disclosure pattern
for TMDb and OpenSubtitles: secret fields are blank on reads and replacements
are stored through the credential layer. Extend that registry and replace the
current free-form `dict` update endpoint with versioned Pydantic request models,
field allowlists, role-aware path validation, and atomic configuration writes.

### Windows installation and launcher

The Windows installer must ask whether to create a desktop shortcut. This is an
opt-in convenience choice, not a prerequisite for installation. The shortcut
launches the installed application entry point in server mode, opens the local
web dashboard, and gives the user a normal way to stop the server. Its target
must not contain a repository path, Python virtual-environment path, API key,
credential, or media-library path.

The shortcut starts the server only; it does not arm disc automation by itself.
The dashboard must clearly show whether automatic processing is armed. If a
user has explicitly enabled a persisted automatic-processing policy, starting
the server activates monitoring under that policy, so the shortcut becomes the
normal “ready to rip” launcher. If a
second shortcut launch finds the same healthy local server already running, it
should open that dashboard instead of starting a duplicate server. Startup
failures should produce a local diagnostic with a safe link or message rather
than leaving a hidden orphan process.

The initial packaging target is a per-user Windows installer with:

- an installer checkbox such as `Create a desktop shortcut`;
- a Start Menu entry regardless of the desktop choice;
- a stable installed launcher independent of the source checkout;
- uninstall cleanup for shortcuts and installed program files without removing
  user configuration, logs, manifests, or media;
- no automatic server launch at Windows sign-in unless a separate, explicit
  option is added later.

Shortcut creation belongs to the installer rather than runtime application
code. Development installs continue to use `uv run mkv-match serve
--no-browser` or `uv run mkv-match serve`; they must not modify the desktop.

The profile builder supports AMD VCN/VCE, NVIDIA NVENC, Intel Quick Sync, and
CPU encoders. It must populate encoder and preset choices only from the
installed HandBrakeCLI capabilities, record the hardware/tool version used for
validation, and refuse to dispatch a profile whose encoder is no longer
available. Profiles may select quality, encoder preset, selective decomb,
content kind, conditional denoise, subtitle retention, and one of these audio
policies:

- stereo compatibility first plus original surround;
- original surround first plus stereo compatibility;
- original audio only;
- stereo compatibility only.

Animation profiles reject denoise. Unsupported passthrough formats must resolve
through an explicit fallback rule rather than silently dropping audio. Built-in
profiles should include a conservative automatic default, higher-quality,
space-saving, and source-preserving choices; user-created profiles are immutable
versions, and changing a profile creates a new version.

Use one event schema for HTTP status and WebSocket updates. Replace the current
in-memory matching job dictionary with the durable store, authenticate
non-loopback access before treating the UI as a phone-accessible controller,
restrict CORS, and avoid returning absolute paths or raw external-process logs.
The existing `/ws` manager can carry progress initially, but it needs
disconnect cleanup, bounded per-client delivery, event sequence IDs, and
replay-from-sequence support.

## Phases and Gates

### Phase 0: Safety and fixtures

- maintain `AGENTS.md` and credential boundaries;
- retain ignored preflight reports;
- create sanitized/synthetic parser fixtures;
- keep all operations read-only.

Gate: parser and safety tests pass without physical media.

### Phase 1: Plan-only title selection

- normalize title and stream metadata;
- detect episode-length titles, combined titles, and extras;
- present reasons and exclusions;
- add CLI JSON output without rip commands.

Gate: the saved episodic-disc fixture produces a reviewable, deterministic
four-title plan and excludes its duplicate/extra patterns.

Status as of 2026-07-30: implemented and verified. The selector uses dominant
runtime clusters, subset-sum combined-title detection, a conservative short
extra threshold, and explicit review classification. Diagnostic audio ranking
prefers English non-commentary stereo while retaining multichannel alternates.
When stereo is absent, the available 5.1 stream remains the diagnostic choice.
The implementation contains no disc or media executor.

### Phase 2: Media and audio diagnostics

- add FFprobe models and fixtures;
- implement explicit audio selection and loudness/silence diagnostics;
- verify 5.1 and stereo behavior using non-destructive samples.

Gate: diagnostics distinguish extraction failure, low level, no speech, short
transcript, and matching failure.

Status as of 2026-07-30: read-only inventory foundation implemented and tested.
Saved FFprobe JSON is normalized without retaining source filenames. A
constrained runner accepts explicit MKV files only, uses fixed metadata flags
without a shell, enforces a timeout, and saves replayable sanitized reports
under ordinal IDs. A separate plan-only command ranks audio streams, selects
sample windows, declares dialogue-preserving downmix intent, lists
loudness/silence/transcript measurements, and defines fallback to alternate
streams. A bounded transcript diagnostic now extracts an explicit audio stream
to a temporary mono WAV, measures mean/peak signal, runs CPU Faster Whisper, and
removes the WAV. It has been exercised on real staging media with path-redacted
reports. It is diagnostic-only and has no rename or move behavior.

### Phase 3: Controlled single-title rip

- separate rip adapter from preflight;
- require an approved plan and unique staging directory;
- support cancellation, timeout, verification, and retained logs;
- never eject automatically.

Gate: one authorized title can be ripped, verified, and resumed safely.

### Phase 4: Episode identification plan

- combine runtime and transcript/subtitle evidence;
- return proposed identities without renaming;
- add an ambiguity queue.
- build an authoritative aired-order catalogue and deterministic local
  shortlist before any LLM fallback;
- permit a bounded, schema-constrained Gemini ranker only for unresolved sets,
  with local semantic validation and score-only durable output.

Gate: known fixtures identify correctly without source mutation.

The cross-kind retry design now uses a private per-title evidence dossier.
Classifier branches receive safe summaries of earlier results, reuse exact-source
transcripts across restarts, and execute through a bounded route that cannot
repeat a branch within one cycle. Public state remains dialogue-free. Provider
episode assignments require two agreeing structured responses and must satisfy
the configured confidence threshold plus a local runtime guard. Confident
partial results release the worker while unresolved titles remain held.
Jellyfin-present/missing state is a tie-break annotation over the complete
canonical catalogue, never a candidate filter.

The separately approved ten-file staged-media gate completed without injecting
a known episode layout. It advanced seven stable provisional episode matches,
held one inconsistent episode-length title, and rejected both play-all titles
from one-episode assignment. No live queue or media mutation was involved. The
next identification gate is synthetic/API composition coverage proving the
same partial/hold behavior through the web worker boundary.

### Phase 5: Deduplication and organization plan

- add media fingerprints and existing-library inventory;
- generate Plex/Jellyfin destinations and collision reports;
- provide no deletion path.

Gate: duplicate and organization decisions are reproducible from stored
evidence.

### Phase 6: Reviewed execution

- add explicit plan approval;
- transaction journal, collision checks, and rollback/quarantine;
- controlled rename/copy/move executors.

Gate: interrupted operations can be explained and safely resumed.

### Phase 7: Durable disc orchestration

- persistent queue and state machine;
- monitored logs and stop policies;
- one-drive operation first, then explicitly approved concurrency.

Gate: restart recovery and pause-after-current-title are tested.

### Phase 8: Optional HandBrake transcode

- separate resource-limited worker;
- explicit AMD VCN encoder selection and capability verification rather than
  trusting mutable GUI preset names;
- plan-only command separated from `--confirm-transcode` execution;
- one explicit input and one collision-free encoded-staging destination;
- source-audio passthrough plus a compatibility audio track;
- unique partial output with preserved failure artifacts;
- redacted process/event logs;
- FFprobe validation of codec, duration, size, video, audio, and subtitles
  before atomic promotion to the reviewed staging filename;
- short sample validation before a full-episode job.
- immutable path-redacted batch manifests with digest-checked resume;
- separate two-worker concurrency and total-job limits;
- incremental reviewed staging-directory creation;
- path-free coordinator events and `STOP`/`PAUSE` chunk boundaries;
- failure/collision isolation without overwrite or media-library writes.

Gate: source remains intact until the transcoded artifact passes verification
and the user approves subsequent handling.

### Phase 9: Automatic web orchestration

- retain the implemented durable public job/event store, stable preview
  identity, isolated private path-binding store, and restart reconciliation;
- retain loopback/same-origin control until secure remote pairing exists;
- retain the implemented, API-disconnected production rip adapter, which has
  fake-runner tests and requires an explicit executable, new external run
  directory, timeout, and drive bound; the dispatcher still has no default
  physical executor;
- add an explicit live-execution composition root only after a separately
  authorized physical canary;
- add read-only drive-monitor simulation with synthetic insertion/removal
  events;
- add typed operating-default models and role-aware directory validation;
- extend write-only credential status/replacement handling to every supported
  provider;
- add capability discovery and immutable HandBrake profile CRUD;
- add automatic-default and per-disc-override policy resolution;
- expose path-redacted pipeline status and replayable events through FastAPI;
- build the drive dashboard, profile builder, override panel, and review queue;
- test restart recovery, late overrides, unavailable encoders, pause/stop,
  drive-local failures, and bounded worker pools before accessing a real disc.

Gate: a synthetic disc can traverse the complete state machine using fake
executors, with no prompt when the default policy is valid, while an override
is applied only to the intended disc and every unsafe/ambiguous transition
pauses deterministically.

Current gate status: the rip-only path now traverses FastAPI job creation,
private binding, exact authorization, queueing, dispatch, and the production
adapter with a fake queue runner. Drive-monitor simulation, automatic policy,
matching/transcode continuation, and the complete-disc state machine remain.

### Phase 10: Windows installer and desktop launcher

Current status: the branded portable foundation is implemented. Release CI
builds and archives the complete `RipWeaver` onedir application and verifies an
extracted copy from a path containing spaces and non-ASCII characters with a
minimal loopback-only frozen smoke boundary. While the frontend source recovery
remains open, CI packages the reviewed tracked frontend bundle and refuses one
missing its existing-rip recovery marker instead of rebuilding incomplete
source. Manual workflow dispatch produces artifacts without publishing; `v*`
tags remain the only release trigger. The installer-specific work below remains
open.

- select and configure a reproducible Windows installer around the packaged
  executable;
- add the optional desktop-shortcut prompt and Start Menu launcher;
- make repeated launches reuse an existing healthy local server;
- test paths containing spaces and non-ASCII characters;
- verify uninstall preserves configuration, logs, manifests, and all media;
- document local-only and authenticated network-access modes.

Gate: clean-install, upgrade, repeated-launch, and uninstall tests pass in a
Windows test environment without exposing secrets or touching media.
