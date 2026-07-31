"""Saved-report-only orchestration previews for the server and web UI."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from mkv_episode_matcher.disc.rip_manifest import (
    MediaContext,
    RipManifest,
    bind_fresh_batch_plans,
    build_rip_manifest,
)
from mkv_episode_matcher.disc.ripper import (
    RipError,
    resolve_final_output,
    resolve_job_output,
)


@dataclass(frozen=True)
class RipPreviewDrive:
    disc_id: str
    drive_index: int
    strategy: str
    title_count: int
    estimated_bytes: int
    minimum_length_seconds: int | None
    reason: str


@dataclass(frozen=True)
class RipPreviewJob:
    job_id: str
    drive_index: int
    title_index: int
    estimated_bytes: int | None
    staging_destination: str
    final_destination: str | None
    collision_status: str


@dataclass(frozen=True)
class RipPreview:
    mode: str
    execution_authorized: bool
    plan_sha256: str
    drives: tuple[RipPreviewDrive, ...]
    jobs: tuple[RipPreviewJob, ...]
    skipped_discs: tuple[dict[str, object], ...]
    collision_count: int
    requires_review: bool
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _manifest_sha256(manifest: RipManifest) -> str:
    identity = manifest.to_dict()
    identity.pop("created_at", None)
    payload = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _collision_status(
    output_root: Path | None,
    *,
    staging_destination: Path,
    final_destination: Path | None,
) -> str:
    if output_root is None:
        return "not-checked"
    if staging_destination.exists():
        return "staging-exists"
    if final_destination is not None and final_destination.exists():
        return "final-exists"
    return "clear"


def build_rip_preview(
    report_paths: list[Path],
    media_contexts: dict[str, MediaContext],
    *,
    output_root: Path | None = None,
) -> RipPreview:
    """Plan and rebind explicit reports without writing or accessing a disc."""

    if output_root is not None and not output_root.is_dir():
        raise RipError("Preview output root must be an existing directory")

    manifest = build_rip_manifest(report_paths, media_contexts)
    batch_plans = bind_fresh_batch_plans(manifest, report_paths)
    proofs = {proof.drive_index: proof for proof in manifest.disc_proofs}

    jobs_by_drive: dict[int, list] = {}
    for job in manifest.jobs:
        jobs_by_drive.setdefault(job.drive_index, []).append(job)

    drives: list[RipPreviewDrive] = []
    for drive_index, drive_jobs in jobs_by_drive.items():
        proof = proofs.get(drive_index)
        plan = batch_plans.get(drive_index)
        if plan is not None:
            strategy = "single-open"
            cutoff = plan.minimum_length_seconds
            reason = "exact-runtime-cutoff"
        elif proof is None:
            strategy = "per-title"
            cutoff = None
            reason = "incomplete-inventory-proof"
        else:
            strategy = "per-title"
            cutoff = None
            reason = "no-exact-runtime-cutoff"
        drives.append(
            RipPreviewDrive(
                disc_id=drive_jobs[0].job_id.rsplit("-title-", 1)[0],
                drive_index=drive_index,
                strategy=strategy,
                title_count=len(drive_jobs),
                estimated_bytes=sum(job.estimated_bytes or 0 for job in drive_jobs),
                minimum_length_seconds=cutoff,
                reason=reason,
            )
        )

    jobs: list[RipPreviewJob] = []
    collision_count = 0
    for job in manifest.jobs:
        if output_root is None:
            staging = Path(job.relative_output_dir)
            final = (
                Path(job.final_relative_dir) / str(job.output_basename)
                if job.final_relative_dir is not None
                else None
            )
        else:
            staging = resolve_job_output(output_root, job)
            final = resolve_final_output(output_root, job)
        collision = _collision_status(
            output_root,
            staging_destination=staging,
            final_destination=final,
        )
        if collision not in {"clear", "not-checked"}:
            collision_count += 1
        jobs.append(
            RipPreviewJob(
                job_id=job.job_id,
                drive_index=job.drive_index,
                title_index=job.title_index,
                estimated_bytes=job.estimated_bytes,
                staging_destination=job.relative_output_dir,
                final_destination=(
                    f"{job.final_relative_dir}/{job.output_basename}"
                    if job.final_relative_dir is not None
                    else None
                ),
                collision_status=collision,
            )
        )

    skipped = tuple(
        {
            "disc_id": item.disc_id,
            "drive_index": item.drive_index,
            "reasons": list(item.reasons),
        }
        for item in manifest.skipped_discs
    )
    return RipPreview(
        mode="saved-report-rip-preview",
        execution_authorized=False,
        plan_sha256=_manifest_sha256(manifest),
        drives=tuple(drives),
        jobs=tuple(jobs),
        skipped_discs=skipped,
        collision_count=collision_count,
        requires_review=bool(collision_count or skipped),
        limitations=(
            "This preview reads only the explicit saved JSON reports.",
            "No disc discovery, MakeMKV execution, ripping, rename, move, "
            "transcode, deletion, directory creation, or ejection is permitted.",
            "A preview SHA-256 is not execution authority.",
            "Physical execution requires a separately reviewed manifest and "
            "explicit authorization.",
        ),
    )
