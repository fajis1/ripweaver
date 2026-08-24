"""Sequential title extraction exclusively from a verified local disc image."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from mkv_episode_matcher.disc.image_acquisition import VerifiedLocalSource
from mkv_episode_matcher.disc.ripper import (
    JsonlRipLog,
    RipError,
    RipJob,
    RipResult,
    run_rip_job,
)


def run_local_image_titles(
    executable: Path,
    output_root: Path,
    source: VerifiedLocalSource,
    jobs: Sequence[RipJob],
    run_directory: Path,
    *,
    timeout_seconds: int = 7200,
    title_runner: Callable[..., RipResult] = run_rip_job,
) -> list[RipResult]:
    """Extract reviewed titles sequentially without any physical-drive source."""

    if not source.source_specifier.startswith(("iso:", "file:")):
        raise RipError("Verified local source is not an ISO or file backup")
    if not source.source.exists():
        raise RipError("Verified local source no longer exists")
    if run_directory.exists():
        raise RipError("Local-image run directory already exists")
    if len({job.job_id for job in jobs}) != len(jobs):
        raise RipError("Local-image jobs must have unique IDs")
    run_directory.mkdir(parents=True)
    results = []
    with JsonlRipLog(run_directory / "events.jsonl") as event_log:
        for job in jobs:
            results.append(
                title_runner(
                    executable,
                    output_root,
                    job,
                    event_log,
                    timeout_seconds=timeout_seconds,
                    cancel_file=run_directory / "STOP",
                    source_specifier=source.source_specifier,
                )
            )
    return results
