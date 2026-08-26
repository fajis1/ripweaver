# Legacy Script Inventory

## Scope and Method

Fourteen relevant Python scripts were inspected read-only. They were syntax
parsed without importing or executing them. Environment files were not opened,
and credential values were redacted from inspection output.

The scripts are outside the active package and are not approved entry points.
They should be treated as design history and a source of small reusable ideas.

## MakeMKV Rippers

### `MakeMkVRipper.py`

Purpose:

- discover optical drives;
- scan discs using MakeMKV robot output;
- infer media identity using an external AI service and TMDb;
- choose titles using runtime heuristics;
- rip with progress logging and eject afterward.

Risks:

- deletes every MKV in an existing target folder before a fresh attempt;
- writes directly into final media-style folders;
- treats log strings as the primary success signal;
- has no durable job state, cancellation, or restart recovery;
- ejects in `finally`, including after failure;
- permits concurrent drive workers without isolated per-disc staging.

Recommendation: do not execute. Reuse only drive discovery, robot-output parsing,
duration extraction, and progress parsing after tests.

Duplication: a download-folder copy is byte-for-byte identical.

### `MakeMkVRipper_Gemini.py`

Purpose: earlier AI-assisted variant of the MakeMKV ripper.

Risks:

- short timeouts and small cache settings;
- target-folder MKV deletion;
- older drive mapping and identification behavior;
- the same coupled scan/identify/rip/eject workflow.

Recommendation: superseded. Retain only for historical comparison.

### `MakeMkVRipper_ENV.py`

Purpose: most developed direct-to-matcher branch.

Notable behavior:

- environment-backed settings;
- serialized hardware scans and parallel rip threads;
- richer metadata pool and TMDb ambiguity handling;
- movie and TV runtime selection;
- per-volume cleanup attempt;
- background multi-season matcher sweep.

Risks:

- the cleanup glob is not a transaction boundary;
- multiple discs can still share one show/season folder;
- matching moves unmatched files across season folders;
- matching runs in a daemon thread and may be lost on exit;
- drive state becomes `COMPLETED` regardless of detailed outcome;
- no durable process handle, pause, cancellation, or output verification;
- automatic ejection remains.

Recommendation: highest-value legacy source, but extract functions into tested
adapters rather than importing the script.

### `Riplex_MakeMkVRipper_ENV.py`

Purpose: hand off a detected disc to the Riplex CLI.

Useful ideas:

- translate drive letter to MakeMKV index;
- pass environment configuration to a subprocess;
- stream child output into the existing logger.

Risks:

- deletes all MKVs in the inferred target folder before handoff;
- creates competing orchestration ownership between the wrapper and Riplex;
- shared output directories are unsafe for multiple discs;
- documented CLI arguments may not match the current upstream release;
- always ejects in cleanup.

Recommendation: do not execute. Reuse only mapping and log-streaming concepts
after verifying current Riplex interfaces.

## Episode-Matcher Recovery Scripts

### `MKV_Matcher_retro_sweep.py`

Purpose:

- collapse legacy folders into a season folder;
- change default/forced audio-track flags;
- move unmatched files through multiple season directories;
- run the episode matcher repeatedly.

Risks:

- mutates original MKV metadata;
- moves source files before identity is known;
- assumes channel count identifies the correct dialogue track;
- sweeps seasons by manipulating folder context;
- has no collision journal or rollback.

Recommendation: do not execute. Preserve as evidence of the audio/season failure
mode only.

### `MKV_Matcher_retro_sweep_proxy.py`

Purpose:

- rename originals to `.ORIGINAL`;
- build stereo proxy MKVs that retain video;
- run the matcher at a very low confidence threshold;
- delete proxies and restore originals under matched names.

Risks:

- failure after renaming can strand the original;
- proxy deletion and restoration are not transactional;
- the low confidence threshold permits false matches;
- proxies bypass rather than diagnose stream-selection problems;
- season sweeping still moves files.

Recommendation: do not execute. Reuse only its MKVToolNix track-discovery logic
as a reference for FFprobe/MakeMKV audio models.

### `MKV_Matcher_Trigger.py`

Purpose: scan a staging tree and invoke the matcher for folders containing MKVs.

Risks:

- uses outdated executable/argument syntax;
- captures all output only after completion;
- has no job identity, cancellation, or duplicate-work protection.

Recommendation: superseded by the active CLI and future durable worker.

### `Smart_AI_MKV_Fallback.py`

Purpose:

- extract one audio sample;
- transcribe it with Faster Whisper;
- ask a local language model for a media-server filename;
- rename immediately.

Risks:

- hardcoded to one show;
- no explicit audio-stream map;
- one shared temporary WAV;
- no catalog validation, collision protection, or confidence evidence;
- trusts any `.mkv` filename returned by the model.

Recommendation: do not execute. Reuse only the local-ASR fallback concept, with
authoritative validation and plan-only output.

## Inventory, Deduplication, and Staging

### `JellyFinScan.py`

Purpose: inventory existing library MKVs and large source MKVs, then classify
exact filename matches as duplicates.

Risks:

- filename equality is not content identity;
- dictionaries keyed by filename silently replace same-name entries;
- scans all available drives;
- does not use hashes, duration, size, or stream signatures.

Recommendation: reuse the separate source/library inventory concept. Replace
matching with deterministic fingerprints.

### `JellyFinScanGemini.py`

Purpose: extend filename comparison by sending source paths and the library
inventory to an external AI model.

Risks:

- appears to contain hardcoded credential assignments;
- transmits local path inventories externally;
- treats model confidence as duplicate evidence;
- can poll indefinitely during service errors;
- still has filename-key collisions;
- produces a manifest later consumed by a deletion script.

Recommendation: do not execute. Rotate any real embedded keys. Do not reuse the
LLM deduplication decision.

### `JellyFinMoveAfterCompare_MKV_MatcherPRep.py`

Purpose:

- read the unique-file CSV;
- infer show and season from filenames/folders;
- move files into matcher staging;
- write staged and skipped manifests.

Risks:

- live moving is enabled by default;
- heuristic guesses can create wrong show/season folders;
- collision and rollback handling are absent;
- CSV state can diverge from the filesystem.

Recommendation: reuse selected parsing heuristics and the staged/skipped
manifest concept. Future behavior should be plan-only by default.

### `JellyFinDeleteAfterCompare.py`

Purpose: delete `source_file` entries listed in the duplicate CSV.

Risks:

- live deletion is enabled by default;
- trusts a CSV generated using filename or AI matching;
- does not revalidate content identity or constrain allowed roots;
- performs permanent deletion without quarantine.

Recommendation: never execute. Do not port this implementation. Future duplicate
handling should use review plus quarantine.

### `Jellyfin_Check_Pipeline_State.py`

Purpose: count files waiting in staging and transcode-output folders and suggest
the next manual script.

Risks and limitations:

- transcode output remains a placeholder;
- counts files but has no durable job state;
- references a HandBrake automation script that was not located.

Recommendation: replace with queries against the future job database.

## Hardware Utility

### `testeject.py`

Purpose: try several Windows optical-drive eject mechanisms.

Risks:

- includes aggressive dismount behavior;
- does not verify media operations are complete;
- unnecessary for the read-only preflight phase.

Recommendation: do not execute. Future ejection must be a separately authorized
adapter invoked only after verified completion.

## Reuse Priority

1. `MakeMkVRipper_ENV.py`: robot parsing, progress parsing, and hardware mapping.
2. `Riplex_MakeMkVRipper_ENV.py`: drive-index translation and child-log streaming.
3. `MKV_Matcher_retro_sweep_proxy.py`: audio-layout test cases and track metadata.
4. `JellyFinScan.py`: separate source and library inventory models.
5. `JellyFinMoveAfterCompare_MKV_MatcherPRep.py`: planning manifests and limited
   show/season parsing.

Every reused behavior must be rewritten or extracted behind tests. No legacy
script is currently approved for execution.
