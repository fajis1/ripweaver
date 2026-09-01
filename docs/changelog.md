# Changelog

- Fixed completed-disc automatic eject after a stale or early dashboard settings read by synchronizing the saved eject preference through the recurring job dashboard response.

All notable changes to MKV Episode Matcher are documented here.

> [!TIP]
> For the complete changelog history, see [CHANGELOG.md](https://github.com/Jsakkos/mkv-episode-matcher/blob/main/CHANGELOG.md) in the repository.

## Unreleased - 2026-08-11

- Updated the desktop catalogue handshake for the server's fail-closed schema-4
  contract. The client now requires strict pending quarantine, refuses any
  publication-eligible submission receipt, and no longer expects a new upload
  to create consensus or contribution credit.
- Updated Settings to distinguish quarantined submissions from historical
  read-only reviewed or independently confirmed catalogue results.
- Added a local **Support & Bug Reports** page that downloads a bounded,
  privacy-redacted diagnostic ZIP and opens a prefilled GitHub issue or email
  draft without uploading anything automatically.
- Added ordered Gemini model fallbacks in Settings. RipWeaver now tries both
  configured keys for a model, then advances through up to two backup models
  after HTTP 429 capacity exhaustion, sustained HTTP 503 overload, or a
  definite unavailable-model response.
- Made every credential-status card an action button that jumps to and focuses
  its local replacement field. OpenSubtitles fields are revealed automatically,
  while the generated RipWeaver Catalogue credential jumps to its connection
  controls.
- Fixed source test worktrees so credential reads and replacements both honor
  `MKV_MATCH_ENV_FILE`; test code can reuse an existing ignored credential file
  without copying or exposing it.
- Expanded the Gemini model panel with clearly numbered primary, first-fallback,
  and second-fallback selectors plus the labeled model presets offered by the
  OpenReader-style setup.

- Added bounded alternate-release subtitle failover for confidently resolved TV
  series and seasons. When usable Whisper evidence fails against every normal
  reference, RipWeaver now searches previously untested Superfan, extended,
  uncut, unrated, supercut, and director's-cut release aliases and retries only
  the unresolved titles.
- Alternate-release matching retains at most two new references per episode,
  runs at most once per season in an analysis, and keeps the normal two-window,
  confidence, margin, runtime, one-to-one, residual, and whole-disc coherence
  requirements. Normal references for the entire season scope are always tried
  before the failover can run.
- Added TV-related movie detection before generic bonus-feature naming.
  Feature-length TV-disc titles now compare bounded, runtime-compatible TMDb
  movie candidates against ordinary OpenSubtitles movie dialogue. Confident
  films are routed to the movie library; unmatched items retain the existing
  provisional Gemini/manual review path.
- Fixed TV-disc bonus analysis incorrectly discarding a valid Gemini `movie`
  classification merely because the parent disc had television-series context.
- Fixed legacy unmatched-disc recovery with a whole-disc retry directly on its
  Needs Attention cards. The reviewed or inferred canonical series name now
  overrides stale packaging text, and labels such as Superfan `S1` and `S2-D1`
  automatically select the correct season.
- Changed automatic TV-series resolution so a lone inexact TMDb search result
  is no longer trusted before Gemini. With automatic Gemini fallback enabled,
  every inexact or packaging-style disc label is reviewed by Gemini and its
  proposed canonical name is validated through a fresh TMDb search before any
  user-name prompt appears.
- Added a configurable retained-original TTL with a 30-day default. The
  retained-source notices in the dashboard and cleanup views now show the live
  expiry window from Settings.
- TTL expiration now opens a guarded cleanup prompt showing the exact file
  count and size. Users can approve deletion of only those retained originals
  or durably postpone the prompt for 1, 7, or 30 days; Jellyfin is unaffected.
- Added extended-edition subtitle matching: every Whisper excerpt now receives
  a bounded whole-subtitle anchor search, so regular episode subtitles can
  identify reconstructed cuts even after inserted scenes shift the timeline.
  Unmatched added-scene excerpts remain neutral once multiple independent
  regular-dialogue anchors agree.
- Fixed catalogue-less packaging-label matches so the confirmed Gemini fallback
  can resolve a canonical series name, validate it through TMDb, and continue
  into local/OpenSubtitles episode matching.
- Restored `Play staged rip for review` on Gemini and manual-identification
  review cards whenever the verified staged source is still available.
- Fixed automatic HandBrake and Jellyfin placement cards so unattended,
  collision-free work continues without contradictory manual approval prompts.
- Added resolution-aware episode coexistence: different versions such as 480p
  and 1080p can coexist, while exact and same-resolution collisions remain held.
- Fixed the final local OCR fallback's missing evidence-directory bug, made OCR
  success/failure visible in the identification trail, and hold likely warning
  screens or disc menus outside episode matching without deleting them.
- Fixed organization incorrectly requiring an old ripped source after its
  verified encode was ready. A missing old source now skips optional archival;
  an existing but changed source still stops safely.
- Added a schema-v3 catalogue compatibility handshake and a Settings connection
  panel showing registration, lookup allowance, contribution outbox, and quorum.
- Changed matched-layout sharing to cumulative title-level updates. Only durable
  matches are included, unresolved titles wait locally, and a newer update
  supersedes older unsent retries for the same disc.
- Added an explicit review action for single-upload community candidates.
  Accepted candidates remain `server_assisted`, so they cannot reinforce their
  own consensus vote or earn contribution credit.
- Added opt-in, path-free matched-disc contributions with a private retryable
  outbox and durable match-provenance tracking.
- Added piecewise community consensus: two independent matching uploads with a
  strict lead confirm each title, while conflicts hold only affected titles.
- Added non-authorizing single-upload candidate help after local matching is
  unable to resolve a title; server-assisted results cannot vote or earn credit.
- Added partial catalogue lookup handling so confirmed titles can continue while
  disputed bonus items remain in local review.
- Added stable hashed Windows optical-device mapping so temporary MakeMKV slot
  renumbering cannot redirect work to another physical drive.
- Added a first-run/change-detection wizard with one-click approval for all
  currently detected intentional drives and exact-snapshot batch saving.
- Added fail-closed USB identity-change handling, safe similar-device warnings,
  and retirement of absent old trusted identities.
- Added a MakeMKV-confirmation guard: a Windows-only provisional slot cannot
  prepare or rip a disc.
- Added separately confirmed continuation for loaded discs after drive setup
  when automatic processing is enabled.
- Documented exact-device Windows restart, USB/SATA power-cycle recovery, and
  the remaining loaded-but-Windows-unreadable state distinction in
  [Windows optical-drive mapping and recovery](WINDOWS_OPTICAL_DRIVE_RECOVERY.md).

## [1.1.0] - 2026-01-11 - Polish Release ✨

### 🖥️ UI/UX Improvements
- **Complete Redesign**: New glassmorphism-inspired UI with modern color palette and improved aesthetics
- **Enhanced Workflow**: Clearer 4-step process (Folder Selection → Scan → Review → Match)
- **Component Refactoring**: Cleaner code structure with explicit Sidebar and Layout components
- **System Status Indicator**: Prominent indicator for backend system and model loading status

### ⚡ Backend Optimizations
- **Singleton Model Loading**: Fixed issue where Parakeet model was loaded multiple times
- **Background Loading**: Model initialization now happens in background on startup
- **Status Endpoint**: New `/system/status` endpoint for frontend health checks
- **Performance**: Significant reduction in resource usage during repeated scans

### 🛠️ Fixes & Updates
- **Dependency Updates**: Relaxed Python version constraints
- **CLI improvements**: Better error handling and help output
- **Documentation**: Updated CLI and README documentation

---

## [1.0.0] - Major Release

### 🖥️ Desktop GUI
- **Complete Flet-based desktop application** with cross-platform support
- **Theme-adaptive interface** that follows system light/dark mode
- **Real-time progress tracking** with "Processing file X of Y" indicators
- **Background model loading** with status indicators to prevent UI freezing
- **Built-in configuration dialog** accessible via settings icon
- **Dry run preview mode** allowing users to preview rename operations
- **Visual folder picker** for easy directory selection
- **Color-coded results display** with detailed match information and confidence scores

### 🤖 Enhanced ASR and Matching Engine
- **Complete rewrite of matching engine (V2)** with improved architecture
- **NVIDIA Parakeet ASR integration** replacing OpenAI Whisper for better accuracy
- **Multi-segment analysis with fallback strategies** to handle empty transcription segments
- **Enhanced caching system** for performance optimization
- **Intelligent checkpoint selection** with primary and fallback locations
- **Improved confidence scoring and voting logic**

### 📊 Improved Core Processing
- **Automatic series and season detection** from directory structure
- **Enhanced subtitle provider system** with local caching and OpenSubtitles integration
- **Optimized file processing workflow** with progress callbacks
- **Smart skip logic** for already processed files with S##E## patterns
- **Comprehensive error handling** with user-friendly messages

### ⚡ Performance Optimizations
- **Model singleton pattern** to avoid repeated ASR model loading
- **Memory caching** for subtitle content and metadata
- **Background task processing** for non-blocking operations
- **Efficient file scanning** with recursive directory support
- **LRU caching** for video duration and metadata

> [!IMPORTANT]
> First run takes **~60 seconds** to download and initialize the NVIDIA Parakeet ASR model.

---

## Previous Versions

### [0.9.3] - 2025-07-07
- Onboarding flag (`--onboard`) and interactive onboarding sequence for first-time setup
- Improved configuration experience for new and returning users

### [0.9.0] - 2025-06-01
- Replaced all `os.path` calls with `pathlib.Path` for improved path handling
- Fixed issues with trailing slashes in directory paths

### [0.7.0] - 2025-03-05
- Rich UI with color-coded output and progress indicators
- Interactive season selection interface
- GPU support check command

### [0.6.0] - 2025-03-02
- Comprehensive documentation including installation, configuration, and CLI guides
- Quick start guide with common usage examples

### [0.5.0] - 2025-02-23
- Progressive matching in 30s intervals (was 300s)

---

[View full changelog on GitHub](https://github.com/Jsakkos/mkv-episode-matcher/releases)
