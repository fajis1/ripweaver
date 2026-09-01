"""Saved-report-only orchestration previews for the server and web UI."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
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
from mkv_episode_matcher.disc.title_selector import load_title_plan


def _has_episode_selection(report_paths: list[Path], drive_index: int) -> bool:
    """Recheck the saved report only to label an automatic fallback preview."""

    for ordinal, report_path in enumerate(report_paths, start=1):
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            if payload.get("drive", {}).get("index") != drive_index:
                continue
            plan = load_title_plan(report_path, report_id=f"disc-{ordinal:02d}")
        except (OSError, ValueError, TypeError):
            return True
        return any(decision.selected for decision in plan.decisions)
    return True


def _selection_mode(
    report_paths: list[Path],
    media_contexts: dict[str, MediaContext],
    *,
    disc_id: str,
    drive_index: int,
) -> str:
    context = media_contexts.get(disc_id)
    if context is not None and context.special_feature_catalog_id is not None:
        return "reviewed-special-features"
    if context is not None and context.selected_title_indexes is not None:
        return "reviewed-title-selection"
    return "all-plausible-media"


@dataclass(frozen=True)
class RipPreviewDrive:
    disc_id: str
    drive_index: int
    strategy: str
    title_count: int
    estimated_bytes: int
    minimum_length_seconds: int | None
    reason: str
    selection_mode: str
    metadata_source: str | None = None
    metadata_status: str | None = None
    metadata_matched_title_count: int = 0


@dataclass(frozen=True)
class RipPreviewJob:
    job_id: str
    drive_index: int
    title_index: int
    estimated_bytes: int | None
    duration_seconds: int | None
    staging_destination: str
    final_destination: str | None
    collision_status: str
    display_name: str | None = None
    extras_folder: str | None = None
    identification_status: str | None = None
    prior_outcome_name: str | None = None
    prior_library_relative: str | None = None
    prior_episode_id: str | None = None
    prior_library_status: str | None = None


@dataclass(frozen=True)
class RipPreview:
    mode: str
    execution_authorized: bool
    plan_sha256: str
    drives: tuple[RipPreviewDrive, ...]
    jobs: tuple[RipPreviewJob, ...]
    skipped_discs: tuple[dict[str, object], ...]
    skipped_titles: tuple[dict[str, object], ...]
    collision_count: int
    requires_review: bool
    limitations: tuple[str, ...]
    held_titles: tuple[dict[str, object], ...] = ()

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


def compatible_manifest_sha256s(manifest: RipManifest) -> frozenset[str]:
    """Return current and known-safe legacy digests for one exact manifest."""

    identity = manifest.to_dict()
    identity.pop("created_at", None)
    identities = [identity]
    jobs = identity.get("jobs", [])
    if all(
        isinstance(job, dict) and job.get("duration_seconds") is None for job in jobs
    ):
        legacy_jobs_identity = deepcopy(identity)
        for job in legacy_jobs_identity.get("jobs", []):
            job.pop("duration_seconds", None)
        identities.append(legacy_jobs_identity)
    legacy_defaults = {
        "selected_title_indexes": None,
        "downstream_skip_title_indexes": (),
        "special_feature_catalog_id": None,
        "special_feature_release_id": None,
        "special_feature_library_title": None,
        "special_feature_library_year": None,
        "special_feature_assignments": (),
        "episode_assignments": (),
        "disc_metadata_source": None,
        "disc_metadata_status": None,
        "disc_metadata_matched_title_count": 0,
        "existing_output_policy": "preserve",
    }
    contexts = identity.get("media_contexts", [])
    if not all(
        isinstance(context, dict)
        and all(context.get(key) == default for key, default in legacy_defaults.items())
        for context in contexts
    ):
        return frozenset(
            hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            for value in identities
        )
    for value in tuple(identities):
        legacy_context_identity = deepcopy(value)
        for context in legacy_context_identity.get("media_contexts", []):
            for key in legacy_defaults:
                context.pop(key, None)
        identities.append(legacy_context_identity)
    return frozenset(
        hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        for value in identities
    )


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


def _episode_assignment_display(assignment: object) -> str | None:
    if not isinstance(assignment, dict):
        return None
    try:
        season = int(assignment["season"])
        episode = int(assignment["episode"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= season <= 99 or not 1 <= episode <= 999:
        return None
    title = assignment.get("title")
    suffix = f" - {title}" if isinstance(title, str) and title.strip() else ""
    return f"S{season:02d}E{episode:02d}{suffix}"


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
        disc_id = drive_jobs[0].job_id.rsplit("-title-", 1)[0]
        context = media_contexts.get(disc_id)
        drives.append(
            RipPreviewDrive(
                disc_id=disc_id,
                drive_index=drive_index,
                strategy=strategy,
                title_count=len(drive_jobs),
                estimated_bytes=sum(job.estimated_bytes or 0 for job in drive_jobs),
                minimum_length_seconds=cutoff,
                reason=reason,
                selection_mode=_selection_mode(
                    report_paths,
                    media_contexts,
                    disc_id=disc_id,
                    drive_index=drive_index,
                ),
                metadata_source=(context.disc_metadata_source if context else None),
                metadata_status=(context.disc_metadata_status if context else None),
                metadata_matched_title_count=(
                    context.disc_metadata_matched_title_count if context else 0
                ),
            )
        )

    jobs: list[RipPreviewJob] = []
    collision_count = 0
    for job in manifest.jobs:
        disc_id = job.job_id.rsplit("-title-", 1)[0]
        context = media_contexts.get(disc_id)
        feature_assignment = next(
            (
                item
                for item in (context.special_feature_assignments if context else ())
                if item.get("title_index") == job.title_index
            ),
            None,
        )
        episode_assignment = next(
            (
                item
                for item in (context.episode_assignments if context else ())
                if item.get("title_index") == job.title_index
            ),
            None,
        )
        episode_display = _episode_assignment_display(episode_assignment)
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
                duration_seconds=job.duration_seconds,
                staging_destination=job.relative_output_dir,
                final_destination=(
                    f"{job.final_relative_dir}/{job.output_basename}"
                    if job.final_relative_dir is not None
                    else None
                ),
                collision_status=collision,
                display_name=(
                    str(feature_assignment["matched_title"])
                    if feature_assignment and feature_assignment.get("matched_title")
                    else episode_display
                ),
                extras_folder=(
                    str(feature_assignment["jellyfin_folder"])
                    if feature_assignment and feature_assignment.get("jellyfin_folder")
                    else None
                ),
                identification_status=(
                    "catalogue-match"
                    if feature_assignment
                    and feature_assignment.get("classification") == "matched-feature"
                    and feature_assignment.get("fallback_name_policy") == "none"
                    else "disc-database-match"
                    if episode_display
                    and episode_assignment.get("identification_source") == "thediscdb"
                    else "evidence-required"
                    if feature_assignment
                    else None
                ),
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
        skipped_titles=(),
        collision_count=collision_count,
        requires_review=bool(
            collision_count
            or skipped
            or any(
                context.disc_metadata_status == "support-required"
                for context in media_contexts.values()
            )
        ),
        limitations=(
            "This preview reads only the explicit saved JSON reports.",
            "No disc discovery, MakeMKV execution, ripping, rename, move, "
            "transcode, deletion, directory creation, or ejection is permitted.",
            "A preview SHA-256 is not execution authority.",
            "Physical execution requires a separately reviewed manifest and "
            "explicit authorization.",
        ),
        held_titles=(),
    )
