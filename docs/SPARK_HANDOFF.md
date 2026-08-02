# Spark Handoff: Edge-Case Audit

This document is the starting point for a follow-up Spark session. Read
`AGENTS.md` and the newest section of `docs/PROJECT_STATUS.md` first.

## Current baseline

On 2026-08-01, the safe synthetic troubleshooting pass completed with **78
tests passed**. It used a writable pytest temporary directory under
`G:\\CodexProject_MKV` after the default Windows pytest temp location returned
access denied. No physical disc, media file, MakeMKV, HandBrakeCLI, FFmpeg,
FFprobe, Whisper, or OCR process was accessed.

## Spark's next audit

1. Run focused synthetic tests for HandBrake profiles, recovery, MakeMKV batch
   planning, manifests, pipeline contracts, adapters, and queue serialization.
2. Run Ruff check and format checks on only the touched modules and tests.
3. Exercise fake external-process runners for encoder selection, audio and
   subtitle language policies, missing-language fallback, collisions,
   interruption codes, retries, partial preservation, pause/stop, and resume.
4. Report exact commands, counts, and substantiated findings. Do not modify
   files unless a minimal fix is separately described and justified.

Use this pytest form on Windows:

```powershell
uv run --no-cache pytest <focused tests> --basetemp `
  "G:\\CodexProject_MKV\\.pytest-spark-audit"
```

## Live-media boundary

Synthetic passing tests do not authorize live work. If a read-only edge-case
scan is needed, Spark must first enumerate the current optical drives, state
the exact drives and operation, and obtain approval for that exact MakeMKV
information scan. No ripping, renaming, moving, deleting, ejecting,
transcoding, or media inspection is included.

Spark may also supervise an authorized live pipeline, but it must use these
stages and approval boundaries:

1. **Read-only inventory:** identify the exact drive letters/slots and run
   MakeMKV `info` only. Save sanitized reports and verify the title set.
2. **Rip authorization:** present the immutable manifest digest, exact drive
   set, title/job count, staging root, new run directory, executable, timeout,
   and parallel-drive limit. Only after approval may the rip executor run.
3. **Identify and review:** consume verified staged outputs through the
   serialized downstream queue. Matching is proposal-only; conflicts and
   unmatched items pause for review.
4. **Transcode authorization:** present the exact HandBrake profile, source
   IDs, encoded staging root, job limit, and run directory. Approval is
   separate from rip approval. Use fake/synthetic validation first whenever
   possible.
5. **Organization authorization:** present exact encoded outputs and relative
   Jellyfin destinations, resolution-version policy, collision decisions, and
   move/copy policy. Existing destinations are never overwritten implicitly.

During an authorized run Spark should monitor redacted logs and state. It may
pause or stop only under the user's stated policy (for example, stop on any
unexpected output, tool failure, collision, or drive error). It must preserve
partials and run logs and must never eject or delete media automatically.

Suggested Spark live-run prompt:

```text
After completing the synthetic audit, prepare a live pipeline run. First
enumerate drives and show me the exact read-only MakeMKV information operation.
Do not start it until I approve that exact drive set. Then create and show the
immutable rip manifest and digest. Before each destructive-capability boundary
(rip, transcode, organization), state the exact inputs, outputs, job limit,
profile, run directory, collision policy, and whether operation is parallel or
serialized, and obtain approval for that boundary. Monitor redacted logs,
preserve every partial, stop on unexpected failures, and never eject, delete,
overwrite, or expose credentials. Do not treat a successful dry run as live
authorization.
```

## Known limitation

Subtitle language retention is explicit in the profile UI, but a separate
language-aware subtitle default-track field and execution mapping remain a
follow-up. Do not report that a selected subtitle language becomes the
playback default until it has a tested backend contract.
