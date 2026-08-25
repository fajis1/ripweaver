"""Execute one explicitly authorized bound special-feature manifest."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from mkv_episode_matcher.disc.ripper import (
    JsonlRipLog,
    RipError,
    RipJob,
    RipResult,
    resolve_job_output,
    run_rip_queue,
)
from mkv_episode_matcher.disc.special_feature_binder import (
    BoundSpecialFeatureManifest,
)

_FREE_SPACE_RESERVE = 512 * 1024**2


def execute_bound_special_feature_manifest(  # noqa: C901
    manifest: BoundSpecialFeatureManifest,
    *,
    bound_manifest_sha256: str,
    executable: Path,
    output_root: Path,
    run_dir: Path,
    authorized_job_count: int,
    timeout_seconds: int = 7200,
    on_event: Callable[[str, str], None] | None = None,
    queue_runner: Callable[..., list[RipResult]] = run_rip_queue,
) -> list[RipResult]:
    """Run the exact bound jobs sequentially with collision refusal."""

    if manifest.execution_authorized is not False:
        raise RipError("Bound special-feature authority flag is invalid")
    if authorized_job_count != len(manifest.jobs):
        raise RipError("Authorized job count does not match the bound manifest")
    if timeout_seconds <= 0:
        raise RipError("Rip timeout must be positive")
    if not executable.is_file():
        raise RipError("MakeMKV executable was not found")
    if not output_root.is_dir():
        raise RipError("Authorized output root does not exist")
    if run_dir.exists():
        raise RipError("Dedicated run directory already exists; refusing reuse")

    resolved_output = output_root.resolve()
    resolved_run = run_dir.resolve()
    try:
        resolved_run.relative_to(resolved_output)
    except ValueError:
        pass
    else:
        raise RipError("Run logs must not be stored inside the media staging root")

    rip_jobs = tuple(
        RipJob(
            job_id=job.job_id,
            drive_index=job.drive_index,
            title_index=job.title_index,
            relative_output_dir=job.relative_output_dir,
            estimated_bytes=job.estimated_bytes,
            output_basename=job.output_basename,
        )
        for job in manifest.jobs
    )
    for job in rip_jobs:
        destination = resolve_job_output(output_root, job)
        if destination.exists():
            raise RipError(f"Staging collision for {job.job_id}; no title was started")

    total_estimated = sum(job.estimated_bytes or 0 for job in rip_jobs)
    available = shutil.disk_usage(output_root).free
    if available < total_estimated + _FREE_SPACE_RESERVE:
        raise RipError("Insufficient conservative free space for authorized rip")

    run_dir.mkdir(parents=True)
    cancel_file = run_dir / "STOP"
    audit_path = run_dir / "authorization.jsonl"
    with JsonlRipLog(audit_path) as audit:
        audit.write(
            "special_feature_execution_authorized",
            bound_manifest_sha256=bound_manifest_sha256,
            authorized_job_count=authorized_job_count,
            total_estimated_bytes=total_estimated,
            sequential=True,
        )

    results = queue_runner(
        executable,
        output_root,
        rip_jobs,
        run_dir / "events.jsonl",
        timeout_seconds=timeout_seconds,
        cancel_file=cancel_file,
        on_event=on_event,
    )
    if len(results) != len(rip_jobs):
        raise RipError("Special-feature queue returned an incomplete result set")
    return results
