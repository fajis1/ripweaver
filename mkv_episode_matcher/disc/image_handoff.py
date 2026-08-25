"""Revalidate a completed acquisition before any local MakeMKV operation."""

from __future__ import annotations

from pathlib import Path

from mkv_episode_matcher.disc.image_acquisition import (
    DiscImagePlan,
    VerifiedLocalSource,
    verify_bluray_backup,
    verify_dvd_iso,
)
from mkv_episode_matcher.disc.image_acquisition_bindings import (
    PrivateAcquisitionBinding,
)
from mkv_episode_matcher.disc.image_acquisition_store import AcquisitionJob
from mkv_episode_matcher.disc.ripper import RipError


def load_verified_local_source(
    job: AcquisitionJob, binding: PrivateAcquisitionBinding
) -> VerifiedLocalSource:
    """Rebuild a private source only from an exact verified job and binding."""

    if job.state != "verified":
        raise RipError("Acquisition is not verified for local handoff")
    if binding.job_id != job.job_id or binding.plan_sha256 != job.plan_sha256:
        raise RipError("Acquisition private binding does not match")
    plan = DiscImagePlan(**job.plan)
    root = binding.image_root.resolve()
    destination = (root / plan.relative_destination).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise RipError("Acquisition handoff escaped its image root") from exc
    if plan.media_kind == "bluray":
        return verify_bluray_backup(plan, destination)
    return verify_dvd_iso(plan, destination)


def local_source_path(source: VerifiedLocalSource) -> Path:
    """Return the private local path after rejecting physical source schemes."""

    if not source.source_specifier.startswith(("iso:", "file:")):
        raise RipError("Acquisition handoff is not a local source")
    return source.source
