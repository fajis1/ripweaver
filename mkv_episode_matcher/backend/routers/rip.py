"""Read-only rip orchestration preview routes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from mkv_episode_matcher.backend.control_access import require_local_control
from mkv_episode_matcher.backend.dependencies import (
    get_disc_inventory_runner,
    get_drive_watcher,
    get_engine,
    get_ffprobe_inspector,
    get_handbrake_profile_store,
    get_orchestration_store,
    get_pipeline_contract_root,
    get_pipeline_queue_store,
    get_private_binding_store,
    get_rip_execution_registry,
    get_rip_queue_runner,
    get_special_feature_queue_runner,
)
from mkv_episode_matcher.backend.gemini_fallback import execute_gemini_fallback
from mkv_episode_matcher.backend.organization_authorization import (
    build_organization_authorization_plan,
)
from mkv_episode_matcher.backend.rip_runtime import RipExecutionRegistry
from mkv_episode_matcher.backend.transcode_authorization import (
    build_transcode_authorization_plan,
)
from mkv_episode_matcher.backend.unmatched_disc_analysis import (
    execute_unmatched_disc_analysis,
)
from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.disc.content_policy import infer_tv_context_from_disc_label
from mkv_episode_matcher.disc.drive_watcher import DriveWatcher
from mkv_episode_matcher.disc.eject import DiscEjectError, eject_optical_drive
from mkv_episode_matcher.disc.episode_release_catalog import (
    catalog_by_id,
    public_assignments,
)
from mkv_episode_matcher.disc.existing_rip_recovery import (
    discover_existing_rips,
    recovered_jobs,
)
from mkv_episode_matcher.disc.failed_rip_cleanup import (
    apply_failed_rip_cleanup,
    plan_failed_rip_cleanup,
)
from mkv_episode_matcher.disc.feature_catalog_registry import select_feature_catalog
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.preflight import (
    PreflightError,
    parse_disc_inventory,
    parse_drives,
    resolve_makemkv_path,
    write_inventory_report,
)
from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.disc.rip_dispatcher import RipDispatcher
from mkv_episode_matcher.disc.rip_execution_adapter import (
    ProductionRipExecutor,
    RipExecutionOptions,
)
from mkv_episode_matcher.disc.rip_manifest import MediaContext, build_rip_manifest
from mkv_episode_matcher.disc.rip_preview import RipPreview, build_rip_preview
from mkv_episode_matcher.disc.ripper import RipError, RipResult
from mkv_episode_matcher.disc.special_feature_binder import (
    SpecialFeatureBindError,
    load_bound_special_feature_manifest,
)
from mkv_episode_matcher.disc.special_feature_executor import (
    execute_bound_special_feature_manifest,
)
from mkv_episode_matcher.disc.special_feature_manifest import (
    build_diagnostic_special_feature_manifest,
)
from mkv_episode_matcher.disc.title_selector import load_title_plan
from mkv_episode_matcher.media.ffprobe_runner import FFprobeError, resolve_ffprobe_path
from mkv_episode_matcher.media.handbrake import HandBrakeProfile
from mkv_episode_matcher.media.handbrake_profiles import (
    HandBrakeProfileStore,
    HandBrakeProfileStoreError,
    StoredHandBrakeProfile,
)
from mkv_episode_matcher.media.organizer import (
    OrganizationPlanError,
    add_jellyfin_version_label,
    inspect_episode_destination,
    jellyfin_resolution_label,
)
from mkv_episode_matcher.pipeline_adapters import (
    OrganizeStageAdapter,
    TranscodeStageAdapter,
)
from mkv_episode_matcher.pipeline_queue import (
    DownstreamDispatcher,
    PipelineQueueError,
    PipelineQueueStore,
    PipelineReviewRequiredError,
    build_artifact,
    enqueue_verified_rip_results,
)

router = APIRouter(
    prefix="/rip",
    tags=["rip"],
    dependencies=[Depends(require_local_control)],
)


class MediaContextInput(BaseModel):
    series_name: str = Field(min_length=1, max_length=200)
    season: int | None = Field(default=None, ge=0, le=99)
    disc_number: int | None = Field(default=None, ge=1)
    volume_number: int | None = Field(default=None, ge=1)
    tmdb_id: int | None = Field(default=None, ge=1)
    content_hint: str | None = Field(default=None, pattern=r"^(tv|movie|extras|mixed)$")
    handbrake_profile_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9-]{1,47}$"
    )


class RipPreviewRequest(BaseModel):
    report_paths: list[str] = Field(min_length=1, max_length=16)
    media_contexts: dict[str, MediaContextInput]
    output_root: str | None = None


class RipPreviewDriveResponse(BaseModel):
    disc_id: str
    drive_index: int
    strategy: str
    title_count: int
    estimated_bytes: int
    minimum_length_seconds: int | None
    reason: str
    selection_mode: str = "episode"


class RipPreviewJobResponse(BaseModel):
    job_id: str
    drive_index: int
    title_index: int
    estimated_bytes: int | None
    staging_destination: str
    final_destination: str | None
    collision_status: str
    display_name: str | None = None
    extras_folder: str | None = None
    identification_status: str | None = None
    prior_outcome_name: str | None = None
    prior_library_relative: str | None = None
    prior_episode_id: str | None = None


class RipPreviewResponse(BaseModel):
    mode: str
    execution_authorized: bool
    plan_sha256: str
    drives: list[RipPreviewDriveResponse]
    jobs: list[RipPreviewJobResponse]
    skipped_discs: list[dict[str, object]]
    collision_count: int
    requires_review: bool
    limitations: list[str]


def _media_contexts_from_request(
    request: RipPreviewRequest,
) -> dict[str, MediaContext]:
    contexts: dict[str, MediaContext] = {}
    for disc_id, value in request.media_contexts.items():
        contexts[disc_id] = MediaContext(
            disc_id=disc_id,
            **value.model_dump(),
        )
    return contexts


def _build_preview_from_request(request: RipPreviewRequest) -> RipPreview:
    report_paths = [Path(value) for value in request.report_paths]
    if len({path.resolve() for path in report_paths}) != len(report_paths):
        raise HTTPException(status_code=400, detail="Report paths must be unique")
    if not all(path.is_file() for path in report_paths):
        raise HTTPException(
            status_code=400,
            detail="Every report must be an existing JSON file",
        )

    try:
        return build_rip_preview(
            report_paths,
            _media_contexts_from_request(request),
            output_root=(Path(request.output_root) if request.output_root else None),
        )
    except RipError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/preview", response_model=RipPreviewResponse)
def preview_rip(request: RipPreviewRequest) -> dict[str, object]:
    """Preview saved reports; never discover a disc or execute media work."""

    preview = _build_preview_from_request(request)
    return preview.to_dict()


class OrchestrationJobResponse(BaseModel):
    job_id: str
    plan_sha256: str
    state: str
    created_at: str
    updated_at: str
    authorization_sha256: str | None
    executor_attached: bool
    preview: RipPreviewResponse
    error_type: str | None = None
    error_category: str | None = None
    failed_drive_indexes: list[int] = []
    recommendations: list[str] = []
    rip_progress_percent: int | None = None
    rip_transfer_mib_s: float | None = None
    rip_progress_scope: str | None = None
    rip_progress_updated_at: str | None = None


class OrchestrationEventResponse(BaseModel):
    sequence: int
    created_at: str
    event_type: str
    from_state: str | None
    to_state: str
    details: dict[str, object]


class OrchestrationJobListResponse(BaseModel):
    automatic_processing_enabled: bool
    watcher_attached: bool
    jobs: list[OrchestrationJobResponse]


class DriveStatusResponse(BaseModel):
    watcher_attached: bool
    refresh_mode: str
    status: str
    refreshed_at: str | None
    error_type: str | None
    drives: list[dict[str, object]]


class RefreshDrivesRequest(BaseModel):
    confirm_read: bool = False
    timeout_seconds: int = Field(default=30, ge=5, le=120)


class EjectDriveRequest(BaseModel):
    confirm_eject: bool = False
    timeout_seconds: int = Field(default=30, ge=5, le=120)


class PrepareDrivePipelineRequest(BaseModel):
    drive_index: int = Field(ge=0, le=99)
    content_hint: str | None = Field(default=None, pattern="^(tv|movie|extras|mixed)$")
    handbrake_profile_id: str | None = Field(default=None, max_length=80)
    library_policy: str = Field(
        default="review-conflicts", pattern="^(review-conflicts|missing-only)$"
    )
    confirm_read: bool = False
    timeout_seconds: int = Field(default=300, ge=30, le=900)


class HandBrakeProfileInput(BaseModel):
    encoder: str
    encoder_preset: str
    quality: float = Field(ge=0, le=51)
    quality_480p: float = Field(default=26, ge=0, le=51)
    quality_720p: float = Field(default=25, ge=0, le=51)
    quality_1080p: float = Field(default=24, ge=0, le=51)
    quality_2160p: float = Field(default=22, ge=0, le=51)
    selective_decomb: bool = True
    content_kind: str = "unknown"
    nlmeans_preset: str | None = None
    nlmeans_tune: str = "none"
    audio_track: int = Field(default=1, ge=1)
    audio_preference: str = Field(
        default="default", pattern=r"^(default|stereo|2\.1|5\.1|7\.1|highest)$"
    )
    audio_primary_layout: str = Field(
        default="", pattern=r"^$|^(default|stereo|2\.1|5\.1|7\.1|highest)$"
    )
    audio_secondary_layout: str = Field(
        default="", pattern=r"^$|^(none|default|stereo|2\.1|5\.1|7\.1|highest)$"
    )
    audio_default_language: str = Field(
        default="default", pattern=r"^(default|[a-z]{3})$"
    )
    audio_language: str = Field(
        default="default", pattern=r"^(default|[a-z]{3}(?:,[a-z]{3})*)$"
    )
    audio_selection: str = Field(
        default="all_matching", pattern=r"^(all_matching|first_matching|all)$"
    )
    additional_audio: str = Field(
        default="selected_only", pattern=r"^(selected_only|all)$"
    )
    subtitle_language: str = Field(default="eng", pattern=r"^[a-z]{3}(?:,[a-z]{3})*$")
    subtitle_selection: str = Field(
        default="all_matching", pattern=r"^(all_matching|first_matching|all|none)$"
    )
    subtitle_default: str = Field(default="none", pattern=r"^(none|first)$")
    resolution_policy: str = Field(
        default="source", pattern=r"^(source|480p|720p|1080p|2160p)$"
    )
    frame_rate_policy: str = Field(
        default="source", pattern=r"^(source|vfr|23\.976|24|25|29\.97|30|50|59\.94|60)$"
    )
    compatibility_audio_bitrate: int = Field(default=256, ge=64, le=512)
    audio_bitrate_stereo: int = Field(default=256, ge=64, le=1024)
    audio_bitrate_2_1: int = Field(default=320, ge=64, le=1024)
    audio_bitrate_5_1: int = Field(default=512, ge=64, le=1024)
    audio_bitrate_7_1: int = Field(default=640, ge=64, le=1024)
    stereo_first: bool = True
    retain_subtitles: bool = True


class SaveHandBrakeProfileRequest(BaseModel):
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,47}$")
    display_name: str = Field(min_length=1, max_length=80)
    profile: HandBrakeProfileInput


class SetDefaultHandBrakeProfileRequest(BaseModel):
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,47}$")
    scope: str = Field(default="general", pattern=r"^(general|480p|720p|1080p|2160p)$")


def _profile_response(item: StoredHandBrakeProfile) -> dict[str, object]:
    return {
        "profile_id": item.profile_id,
        "display_name": item.display_name,
        "built_in": item.built_in,
        "profile": asdict(item.profile),
    }


@router.get("/handbrake/profiles")
def list_handbrake_profiles(
    store: Annotated[HandBrakeProfileStore, Depends(get_handbrake_profile_store)],
) -> dict[str, object]:
    try:
        return {"profiles": [_profile_response(item) for item in store.list()]}
    except HandBrakeProfileStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/handbrake/profiles")
def save_handbrake_profile(
    request: SaveHandBrakeProfileRequest,
    store: Annotated[HandBrakeProfileStore, Depends(get_handbrake_profile_store)],
) -> dict[str, object]:
    try:
        profile = HandBrakeProfile(**request.profile.model_dump())
        return _profile_response(
            store.save_custom(request.profile_id, request.display_name, profile)
        )
    except HandBrakeProfileStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/handbrake/profiles/default")
def set_default_handbrake_profile(
    request: SetDefaultHandBrakeProfileRequest,
    store: Annotated[HandBrakeProfileStore, Depends(get_handbrake_profile_store)],
) -> dict[str, object]:
    """Persist the profile used automatically for subsequently detected discs."""

    try:
        selected = next(
            (item for item in store.list() if item.profile_id == request.profile_id),
            None,
        )
        if selected is None:
            raise HandBrakeProfileStoreError("Selected HandBrake profile was not found")
        manager = get_config_manager()
        config = manager.load()
        field = {
            "general": "default_handbrake_profile",
            "480p": "default_handbrake_profile_480p",
            "720p": "default_handbrake_profile_720p",
            "1080p": "default_handbrake_profile_1080p",
            "2160p": "default_handbrake_profile_2160p",
        }[request.scope]
        manager.save(config.model_copy(update={field: selected.profile_id}))
        return {
            "status": "success",
            "profile_id": selected.profile_id,
            "display_name": selected.display_name,
            "scope": request.scope,
        }
    except (HandBrakeProfileStoreError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Default HandBrake profile was not saved ({type(exc).__name__})",
        ) from exc


def _drive_status_response(watcher: DriveWatcher) -> dict[str, object]:
    snapshot = watcher.snapshot()
    return {
        "watcher_attached": True,
        "refresh_mode": "startup-and-events",
        "status": snapshot.status,
        "refreshed_at": snapshot.refreshed_at,
        "error_type": snapshot.error_type,
        "drives": [asdict(drive) for drive in snapshot.drives],
    }


def _attach_prior_outcomes(
    preview: RipPreview, store: PipelineQueueStore
) -> RipPreview:
    store.rebuild_title_history()
    enriched = []
    for job in preview.jobs:
        parts = Path(job.staging_destination).parts
        fingerprint = next(
            (part for part in parts if re.fullmatch(r"[0-9a-f]{16}", part)), None
        )
        history = (
            store.title_history(fingerprint).get(job.title_index)
            if fingerprint
            else None
        )
        enriched.append(
            replace(
                job,
                prior_outcome_name=history.get("outcome_name") if history else None,
                prior_library_relative=(
                    history.get("library_relative") if history else None
                ),
                prior_episode_id=history.get("episode_id") if history else None,
            )
        )
    return replace(preview, jobs=tuple(enriched))


@router.get("/drives", response_model=DriveStatusResponse)
def get_drive_status(
    watcher: Annotated[DriveWatcher, Depends(get_drive_watcher)],
) -> dict[str, object]:
    """Return only cached redacted drive status; never access hardware."""

    return _drive_status_response(watcher)


@router.post("/drives/refresh", response_model=DriveStatusResponse)
def refresh_drive_status(
    request: RefreshDrivesRequest,
    watcher: Annotated[DriveWatcher, Depends(get_drive_watcher)],
) -> dict[str, object]:
    """Run one explicitly confirmed MakeMKV drive-enumeration info call."""

    if request.confirm_read is not True:
        raise HTTPException(
            status_code=400, detail="Read-only drive refresh confirmation is required"
        )
    try:
        config = get_config_manager().load()
        executable = resolve_makemkv_path(config.makemkv_path)
        watcher.refresh(executable, timeout_seconds=request.timeout_seconds)
    except PreflightError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Drive refresh failed safely ({type(exc).__name__})",
        ) from exc
    return _drive_status_response(watcher)


@router.post("/drives/{drive_index}/eject")
def eject_drive(
    drive_index: int,
    request: EjectDriveRequest,
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
    inventory_runner: Annotated[Callable, Depends(get_disc_inventory_runner)],
    watcher: Annotated[DriveWatcher, Depends(get_drive_watcher)],
    registry: Annotated[RipExecutionRegistry, Depends(get_rip_execution_registry)],
) -> dict[str, object]:
    """Eject one exact idle Windows optical drive after explicit confirmation."""

    if request.confirm_eject is not True:
        raise HTTPException(
            status_code=400, detail="Disc ejection confirmation is required"
        )
    if drive_index < 0:
        raise HTTPException(status_code=400, detail="Optical drive index is invalid")
    drive_jobs = [
        job
        for job in store.list_jobs()
        if any(
            drive.get("drive_index") == drive_index
            for drive in job.preview.get("drives", [])
            if isinstance(drive, dict)
        )
    ]
    active_jobs = [job for job in drive_jobs if registry.is_job_active(job.job_id)]
    if active_jobs:
        raise HTTPException(
            status_code=409,
            detail="MakeMKV is actively reading this disc; stop the physical rip before ejecting",
        )
    if any(job.state in {"running", "pause_requested"} for job in drive_jobs):
        store.reconcile_incomplete()
        drive_jobs = [store.get_job(job.job_id) for job in drive_jobs]
    for job in drive_jobs:
        if job.state in {"authorized", "queued"}:
            store.return_to_review(
                job.job_id,
                idempotency_key=f"eject-review-{drive_index}-{job.job_id}",
            )
    try:
        device_name = watcher.device_name(drive_index)
        if device_name is None:
            config = get_config_manager().load()
            executable = resolve_makemkv_path(config.makemkv_path)
            result = inventory_runner(
                executable,
                f"disc:{drive_index}",
                minimum_length=0,
                timeout_seconds=request.timeout_seconds,
            )
            drive = next(
                (
                    item
                    for item in parse_drives(result.stdout)
                    if item.index == drive_index
                ),
                None,
            )
            if drive is None or not drive.has_disc:
                raise DiscEjectError(
                    "The selected optical drive does not contain a disc"
                )
            device_name = drive.device_name
        eject_optical_drive(device_name)
    except (DiscEjectError, PreflightError, OSError) as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                str(exc)
                if isinstance(exc, DiscEjectError | PreflightError)
                else "Windows could not open the optical tray"
            ),
        ) from exc
    return {"status": "ejected", "drive_index": drive_index}


def _auto_eject_completed_job_drives(
    completed,
    store: OrchestrationStore,
    inventory_runner: Callable,
    executable: Path,
    timeout_seconds: int,
) -> None:
    """Best-effort eject completed drives without changing rip success state."""

    for drive in completed.preview.get("drives", []):
        drive_index = drive.get("drive_index") if isinstance(drive, dict) else None
        if not isinstance(drive_index, int):
            continue
        competing = any(
            other.job_id != completed.job_id
            and other.state in {"authorized", "queued", "running", "pause_requested"}
            and any(
                candidate.get("drive_index") == drive_index
                for candidate in other.preview.get("drives", [])
                if isinstance(candidate, dict)
            )
            for other in store.list_jobs()
        )
        if competing:
            logger.warning(
                "Automatic disc eject skipped because the drive still has queued work"
            )
            continue
        try:
            result = inventory_runner(
                executable,
                f"disc:{drive_index}",
                minimum_length=0,
                timeout_seconds=min(timeout_seconds, 120),
            )
            physical_drive = next(
                (
                    item
                    for item in parse_drives(result.stdout)
                    if item.index == drive_index
                ),
                None,
            )
            if physical_drive is None or not physical_drive.has_disc:
                raise DiscEjectError("Completed drive no longer contains a disc")
            eject_optical_drive(physical_drive.device_name)
            logger.info("Automatically ejected one successfully completed rip drive")
        except (DiscEjectError, PreflightError, OSError) as exc:
            logger.warning("Automatic disc eject failed safely: {}", type(exc).__name__)


@router.post(
    "/drives/prepare-pipeline",
    response_model=OrchestrationJobResponse,
)
def prepare_drive_pipeline(
    request: PrepareDrivePipelineRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    watcher: Annotated[DriveWatcher, Depends(get_drive_watcher)],
    public_store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
    private_store: Annotated[PrivateBindingStore, Depends(get_private_binding_store)],
    pipeline_store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    inventory_runner: Annotated[Callable, Depends(get_disc_inventory_runner)],
) -> dict[str, object]:
    """Inventory one selected loaded drive and create a non-authorized rip job."""

    if request.confirm_read is not True:
        raise HTTPException(
            status_code=400,
            detail="Preparing a disc pipeline requires read-only disc confirmation",
        )
    snapshot = watcher.snapshot()
    selected = next(
        (
            drive
            for drive in snapshot.drives
            if drive.drive_index == request.drive_index and drive.has_disc
        ),
        None,
    )
    if selected is None:
        raise HTTPException(
            status_code=409,
            detail="The selected drive does not currently contain a detected disc",
        )

    config = get_config_manager().load()
    if config.rip_output_root is None or not config.rip_output_root.is_dir():
        raise HTTPException(
            status_code=409,
            detail="Configure an existing MakeMKV rip staging root before preparing a pipeline",
        )
    try:
        executable = resolve_makemkv_path(config.makemkv_path)
        result = inventory_runner(
            executable,
            f"disc:{request.drive_index}",
            minimum_length=0,
            timeout_seconds=request.timeout_seconds,
        )
        matching_drive = next(
            (
                item
                for item in parse_drives(result.stdout)
                if item.index == request.drive_index
            ),
            None,
        )
        if matching_drive is None or not matching_drive.has_disc:
            raise PreflightError("Selected disc disappeared during inventory")
        inventory = parse_disc_inventory(result, matching_drive)
        if not inventory.titles:
            raise PreflightError("MakeMKV returned no titles for the selected disc")

        report_dir = config.cache_dir.parent / "preflight" / "web" / uuid4().hex
        report_path, _robot_path = write_inventory_report(
            report_dir, inventory, result, minimum_length_seconds=0
        )
        explicit_tv_context = infer_tv_context_from_disc_label(matching_drive.disc_name)
        disc_id = "disc-01"
        episode_plan = load_title_plan(report_path, report_id=disc_id)
        use_special_features = request.content_hint in {None, "extras", "mixed"}
        selected_title_indexes = None
        catalog_id = None
        release_id = None
        library_title = None
        library_year = None
        assignments: tuple[dict[str, object], ...] = ()
        if use_special_features:
            selection = select_feature_catalog(
                report_path,
                (
                    Path(__file__).resolve().parents[2] / "feature_catalogs",
                    config.cache_dir.parent / "feature-catalogs",
                ),
                report_id=disc_id,
            )
            if selection is not None:
                diagnostic = build_diagnostic_special_feature_manifest(selection.plan)
                feature_decisions = {
                    item.title.index: item for item in selection.plan.decisions
                }
                special_indexes = tuple(job.title_index for job in diagnostic.jobs)
                episode_indexes = tuple(
                    item.title.index for item in episode_plan.decisions if item.selected
                )
                selected_title_indexes = tuple(
                    sorted(
                        set(special_indexes)
                        | (
                            set(episode_indexes)
                            if request.content_hint == "mixed"
                            else set()
                        )
                    )
                )
                catalog_id = selection.plan.catalog_id
                release_id = selection.plan.release_id
                library_title = selection.plan.library_title
                library_year = selection.plan.library_year
                assignments = tuple(
                    {
                        "title_index": job.title_index,
                        "classification": job.classification,
                        "matched_title": feature_decisions[
                            job.title_index
                        ].matched_title,
                        "candidate_feature_ids": list(
                            feature_decisions[job.title_index].candidate_feature_ids
                        ),
                        "jellyfin_folder": feature_decisions[
                            job.title_index
                        ].jellyfin_folder,
                        "fallback_name_policy": job.fallback_name_policy,
                        "audio_policy": job.audio_policy,
                    }
                    for job in diagnostic.jobs
                )
        context = MediaContext(
            disc_id=disc_id,
            series_name=(
                explicit_tv_context[0] if explicit_tv_context else "Unmatched"
            ),
            season=explicit_tv_context[1] if explicit_tv_context else None,
            content_hint=request.content_hint
            or (
                "tv"
                if explicit_tv_context
                else (
                    "extras"
                    if catalog_id is not None
                    or not any(item.selected for item in episode_plan.decisions)
                    else None
                )
            ),
            handbrake_profile_id=request.handbrake_profile_id,
            staging_attempt=f"attempt-{uuid4().hex[:12]}",
            selected_title_indexes=selected_title_indexes,
            special_feature_catalog_id=catalog_id,
            special_feature_release_id=release_id,
            special_feature_library_title=library_title,
            special_feature_library_year=library_year,
            special_feature_assignments=assignments,
            episode_assignments=(),
            existing_output_policy=(
                "missing-only"
                if request.library_policy == "missing-only"
                else "preserve"
            ),
        )
        preview = build_rip_preview(
            [report_path],
            {disc_id: context},
            output_root=config.rip_output_root,
        )
        preview = _attach_prior_outcomes(preview, pipeline_store)
        job = public_store.create_job(preview, idempotency_key=idempotency_key)
        private_store.bind(
            job_id=job.job_id,
            plan_sha256=job.plan_sha256,
            report_paths=[report_path],
            output_root=config.rip_output_root,
            media_contexts={disc_id: context},
        )
        watcher.bind_current_job(request.drive_index, job.job_id)
        return _job_response(job, public_store)
    except (PreflightError, RipError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class AuthorizeJobRequest(BaseModel):
    expected_plan_sha256: str
    confirm_authorization: bool = False


class StartJobRequest(BaseModel):
    confirm_queue: bool = False


class ExecuteJobRequest(BaseModel):
    expected_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_job_count: int = Field(ge=1)
    makemkv_executable: str | None = None
    run_directory: str | None = None
    timeout_seconds: int = Field(default=7200, ge=60, le=86400)
    max_drives: int | None = Field(default=None, ge=1, le=16)
    confirm_execute: bool = False
    preserve_failed_partials: bool = True
    failed_cleanup_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirm_failed_cleanup: bool = False


class ControlJobRequest(BaseModel):
    confirm_control: bool = False


class ResolveRipCollisionsRequest(BaseModel):
    policy: str = Field(pattern="^(missing-only|rerip-all|replace-after-verification)$")
    confirm_resolution: bool = False


class DeleteStagedLibraryDuplicatesRequest(BaseModel):
    expected_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_file_count: int = Field(ge=1, le=999)
    confirm_delete: bool = False


class SelectRipTitlesRequest(BaseModel):
    title_indexes: list[int] = Field(min_length=1, max_length=999)
    confirm_selection: bool = False


def _resolution_title_indexes(source_jobs: object, policy: str) -> dict[str, set[int]]:
    if not isinstance(source_jobs, list):
        raise RipError("Saved rip preview jobs are invalid")
    selected_by_disc: dict[str, set[int]] = {}
    for item in source_jobs:
        if not isinstance(item, dict):
            raise RipError("Saved rip preview job is invalid")
        if policy == "missing-only" and item.get("collision_status") not in {
            "clear",
            "not-checked",
        }:
            continue
        saved_job_id = item.get("job_id")
        title_index = item.get("title_index")
        if not isinstance(saved_job_id, str) or not isinstance(title_index, int):
            raise RipError("Saved rip preview job is invalid")
        disc_id = saved_job_id.rsplit("-title-", 1)[0]
        selected_by_disc.setdefault(disc_id, set()).add(title_index)
    if not selected_by_disc:
        raise RipError("No missing titles remain in this review")
    return selected_by_disc


def _staged_library_duplicate_plan(
    job, binding, config
) -> tuple[str, list[tuple[Path, int]]]:
    """Bind exact staged finals to existing historical Jellyfin destinations."""

    entries: list[tuple[Path, int]] = []
    source_jobs = job.preview.get("jobs", [])
    if not isinstance(source_jobs, list):
        raise RipError("Saved rip preview jobs are invalid")
    output_root = binding.output_root.resolve()
    for item in source_jobs:
        if not isinstance(item, dict) or item.get("collision_status") != "final-exists":
            continue
        staged_relative = item.get("final_destination")
        library_relative = item.get("prior_library_relative")
        if not isinstance(staged_relative, str) or not isinstance(
            library_relative, str
        ):
            continue
        staged_parts = PurePosixPath(staged_relative)
        library_parts = PurePosixPath(library_relative)
        if (
            staged_parts.is_absolute()
            or library_parts.is_absolute()
            or ".." in staged_parts.parts
            or ".." in library_parts.parts
            or staged_parts.suffix.casefold() != ".mkv"
            or library_parts.suffix.casefold() != ".mkv"
        ):
            raise RipError("Saved staged or Jellyfin destination is unsafe")
        staged = (output_root / Path(*staged_parts.parts)).resolve()
        staged.relative_to(output_root)
        library_root = (
            config.jellyfin_tv_root
            if item.get("prior_episode_id")
            else config.jellyfin_movie_root
        )
        if library_root is None or not library_root.is_dir():
            continue
        resolved_library_root = library_root.resolve()
        library = (resolved_library_root / Path(*library_parts.parts)).resolve()
        library.relative_to(resolved_library_root)
        if not staged.is_file() or not library.is_file():
            continue
        size = staged.stat().st_size
        if size <= 0:
            continue
        entries.append((staged, size))
    identity = json.dumps(
        [(path.relative_to(output_root).as_posix(), size) for path, size in entries],
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(identity).hexdigest(), entries


class ExecuteSpecialFeatureRequest(BaseModel):
    bound_manifest: str = Field(min_length=1)
    fresh_inventory: str = Field(min_length=1)
    bound_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_job_count: int = Field(ge=1)
    makemkv_executable: str = Field(min_length=1)
    output_root: str = Field(min_length=1)
    run_directory: str = Field(min_length=1)
    timeout_seconds: int = Field(default=7200, ge=60, le=86400)
    confirm_execute: bool = False


class SpecialFeatureExecutionResponse(BaseModel):
    mode: str
    status: str
    manifest_sha256: str
    completed_count: int


class PipelineItemResponse(BaseModel):
    media_id: str
    artifact_sha256: str
    disc_fingerprint: str | None = None
    display_name: str | None = None
    location_label: str
    location_relative: str | None = None
    location_root_key: str | None = None
    output_size_bytes: int | None = None
    retained_source_available: bool = False
    retention_candidate_available: bool = False
    staged_source_available: bool = False
    provisional_match: bool = False
    gemini_confidence: float | None = None
    state: str
    stage: str
    created_at: str
    updated_at: str
    error_type: str | None
    review_code: str | None


class PipelineQueueResponse(BaseModel):
    paused: bool
    downstream_worker_limit: int
    items: list[PipelineItemResponse]


class PipelineControlRequest(BaseModel):
    confirm_control: bool = False


class DismissPipelineItemsRequest(BaseModel):
    media_ids: list[str] = Field(min_length=1, max_length=999)
    confirm_dismiss: bool = False


class CancelQueuedPipelineItemsRequest(BaseModel):
    media_ids: list[str] = Field(min_length=1, max_length=999)
    confirm_cancel: bool = False


class DeleteQueuedPipelineMediaRequest(BaseModel):
    confirm_delete: bool = False


class AmbiguityChoiceRequest(BaseModel):
    choice: str = Field(pattern=r"^(gemini|manual|hold)$")


class ApplyEpisodeReleaseRequest(BaseModel):
    disc_fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")
    catalog_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    confirm_apply: bool = False
    confirm_external_fallback: bool = False


class UnmatchedDiscAnalysisRequest(BaseModel):
    disc_fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")
    series_name: str = Field(min_length=1, max_length=160)
    season: int | None = Field(default=None, ge=0, le=99)
    confirm_media_read: bool = False
    confirm_provider_lookup: bool = False
    confirm_external_fallback: bool = False


class UnmatchedDiscClassificationRequest(BaseModel):
    disc_fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")
    classification: str = Field(pattern=r"^(extras)$")
    confirm_classification: bool = False


class GeminiFallbackExecutionRequest(BaseModel):
    media_ids: list[str] = Field(min_length=1, max_length=20)
    confirm_media_read: bool = False
    confirm_external_transmission: bool = False


class RestartExistingPipelineRequest(BaseModel):
    confirm_restart: bool = False
    title_indexes: list[int] | None = None


class VerifyExistingRipsRequest(BaseModel):
    expected_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_ids: list[Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] = Field(
        min_length=1, max_length=999
    )
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    confirm_media_read: bool = False


class PreviewExistingRipsRequest(BaseModel):
    confirm_search: bool = False


class AuthorizeTranscodeRequest(BaseModel):
    expected_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_item_count: int = Field(ge=1, le=999)
    profile_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,47}$")
    confirm_transcode: bool = False


class AuthorizeOrganizationRequest(BaseModel):
    expected_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_item_count: int = Field(ge=1, le=999)
    confirm_organize: bool = False


class RetainedSourceRequest(BaseModel):
    media_ids: list[str] = Field(min_length=1, max_length=999)


class DeleteRetainedSourceRequest(RetainedSourceRequest):
    expected_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_file_count: int = Field(ge=1, le=999)
    confirm_delete: bool = False


class RetainExistingSourceRequest(RetainedSourceRequest):
    expected_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_file_count: int = Field(ge=1, le=999)
    confirm_move: bool = False


class ReencodeRetainedSourceRequest(RetainedSourceRequest):
    profile_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,47}$")
    confirm_reencode: bool = False


class PlayPipelineItemRequest(BaseModel):
    confirm_play: bool = False


class RenameProvisionalItemRequest(BaseModel):
    new_name: str = Field(min_length=1, max_length=160)
    confirm_rename: bool = False


class ResolveLibraryCollisionRequest(BaseModel):
    action: str = Field(pattern=r"^(replace-library|delete-new)$")
    expected_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_resolution: bool = False


def _pipeline_item_display_name(item) -> str | None:
    """Read only a safe matched basename from the current private contract."""

    try:
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    relative = payload.get("library_relative")
    if not isinstance(relative, str) or not relative.strip():
        return None
    parts = tuple(part for part in relative.replace("\\", "/").split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    name = parts[-1]
    if name.casefold().endswith(".mkv"):
        name = name[:-4]
    return name.strip() or None


def _pipeline_item_location(item) -> tuple[str, str | None, str | None]:
    try:
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "Location unavailable", None, None
    relative = payload.get("library_relative")
    safe_relative = None
    if isinstance(relative, str):
        parts = tuple(part for part in relative.replace("\\", "/").split("/") if part)
        if parts and not any(part in {".", ".."} for part in parts):
            safe_relative = "/".join(parts)
    if item.state == "completed" and item.stage == "organize":
        root_key = None
        transcode_contract = item.artifact.contract_path.with_name(
            f"{item.media_id}.transcode.json"
        )
        try:
            transcode = json.loads(transcode_contract.read_text(encoding="utf-8"))
            root_key = (
                "jellyfin_tv_root"
                if transcode.get("episode_id")
                else "jellyfin_movie_root"
            )
        except (OSError, json.JSONDecodeError):
            root_key = None
        return "Jellyfin library", safe_relative, root_key
    if item.stage == "organize":
        return "Encoded staging (awaiting Jellyfin placement)", safe_relative, None
    if item.stage == "transcode":
        return "Rip staging (awaiting transcode)", safe_relative, None
    return "Rip staging", safe_relative, None


def _pipeline_item_response(item) -> dict[str, object]:
    location_label, location_relative, location_root_key = _pipeline_item_location(item)
    output_size_bytes = None
    retained_source_available = False
    retention_candidate_available = False
    staged_source_available = False
    provisional_match = False
    gemini_confidence = None
    disc_fingerprint = None
    try:
        rip_payload = json.loads(
            item.artifact.contract_path.with_name(
                f"{item.media_id}.verified-rip.json"
            ).read_text(encoding="utf-8")
        )
        fingerprint = rip_payload.get("disc_fingerprint")
        if isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{16}", fingerprint):
            disc_fingerprint = fingerprint
    except (OSError, json.JSONDecodeError):
        pass
    try:
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
        source_value = payload.get("source_path")
        source_size = payload.get("source_size_bytes")
        if isinstance(source_value, str) and isinstance(source_size, int):
            source = Path(source_value)
            staged_source_available = (
                source_size > 0
                and source.is_file()
                and source.stat().st_size == source_size
            )
        value = payload.get("output_size_bytes")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            output_size_bytes = value
        retained = payload.get("archived_source_path")
        retained_size = payload.get("archived_source_size_bytes")
        if isinstance(retained, str) and isinstance(retained_size, int):
            retained_path = Path(retained)
            retained_source_available = (
                retained_path.is_file()
                and retained_path.stat().st_size == retained_size
            )
        provisional_match = bool(payload.get("provisional_match"))
        confidence = payload.get("gemini_confidence")
        if isinstance(confidence, int | float) and not isinstance(confidence, bool):
            gemini_confidence = float(confidence)
    except (OSError, json.JSONDecodeError):
        pass
    if (
        item.state == "completed"
        and item.stage == "organize"
        and not retained_source_available
    ):
        try:
            rip_payload = json.loads(
                item.artifact.contract_path.with_name(
                    f"{item.media_id}.verified-rip.json"
                ).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            rip_payload = {}
        source = Path(str(rip_payload.get("source_path", "")))
        source_size = rip_payload.get("source_size_bytes")
        retention_candidate_available = (
            isinstance(source_size, int)
            and source_size > 0
            and source.is_file()
            and source.stat().st_size == source_size
        )
    return {
        "media_id": item.media_id,
        "artifact_sha256": item.artifact.contract_sha256,
        "disc_fingerprint": disc_fingerprint,
        "display_name": _pipeline_item_display_name(item),
        "location_label": location_label,
        "location_relative": location_relative,
        "location_root_key": location_root_key,
        "output_size_bytes": output_size_bytes,
        "retained_source_available": retained_source_available,
        "retention_candidate_available": retention_candidate_available,
        "staged_source_available": staged_source_available,
        "provisional_match": provisional_match,
        "gemini_confidence": gemini_confidence,
        "state": item.state,
        "stage": item.stage,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "error_type": item.error_type,
        "review_code": item.review_code,
    }


def _exact_queued_rip_source(item, rip_root: Path) -> Path:
    """Validate and resolve one verified source inside rip staging."""

    expected_modes = {
        "rip": "verified-rip-contract",
        "identify": "identified-episode-contract",
    }
    expected_mode = expected_modes.get(item.artifact.stage)
    if expected_mode is None:
        raise PipelineQueueError("Only a verified staged rip can be deleted here")
    try:
        contract_bytes = item.artifact.contract_path.read_bytes()
        payload = json.loads(contract_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineQueueError("The queued media contract is unavailable") from exc
    if hashlib.sha256(contract_bytes).hexdigest() != item.artifact.contract_sha256:
        raise PipelineQueueError("The queued media contract changed")
    if payload.get("mode") != expected_mode:
        raise PipelineQueueError("The queued media contract has an unexpected type")
    source_value = payload.get("source_path")
    source_size = payload.get("source_size_bytes")
    if not isinstance(source_value, str) or not isinstance(source_size, int):
        raise PipelineQueueError("The queued media source identity is incomplete")
    if isinstance(source_size, bool) or source_size <= 0:
        raise PipelineQueueError("The queued media source identity is incomplete")
    source = Path(source_value).resolve()
    try:
        source.relative_to(rip_root.resolve())
    except ValueError as exc:
        raise PipelineQueueError("The queued media source escapes rip staging") from exc
    if source.suffix.lower() != ".mkv" or not source.is_file():
        raise PipelineQueueError("The queued staged MKV is unavailable")
    if source.stat().st_size != source_size:
        raise PipelineQueueError("The queued staged MKV changed after verification")
    return source


def _delete_exact_queued_rip(item, rip_root: Path) -> None:
    try:
        _exact_queued_rip_source(item, rip_root).unlink()
    except OSError as exc:
        raise PipelineQueueError("The queued staged MKV could not be deleted") from exc


_RIP_RECOMMENDATIONS = {
    "timeout": [
        "Retry with a new run directory and a longer timeout.",
        "If the drive stopped responding, clean the disc or try another drive.",
    ],
    "io_error": [
        "Check the optical drive connection and staging-disk availability.",
        "Clean the disc and retry; if it repeats, try a different drive.",
    ],
    "collision": [
        "Review the existing output. It will not be overwritten.",
    ],
    "storage": [
        "Free staging space or select another staging location before retrying.",
    ],
    "interrupted": [
        "Review preserved partials, then retry into a new run directory.",
    ],
    "makemkv_failure": [
        "Review the MakeMKV diagnostics, clean the disc, and retry.",
        "If read failures repeat, try the disc in a different optical drive.",
    ],
    "unknown": [
        "Review the run log, drive connection, disc surface, and staging storage.",
    ],
}


def _job_response(job, store: OrchestrationStore | None = None) -> dict[str, object]:
    response = asdict(job)
    response.update({
        "error_type": None,
        "error_category": None,
        "failed_drive_indexes": [],
        "recommendations": [],
        "rip_progress_percent": None,
        "rip_transfer_mib_s": None,
        "rip_progress_scope": None,
        "rip_progress_updated_at": None,
    })
    if store is not None:
        events = store.list_events(job.job_id)
        samples = [event for event in events if event.event_type == "rip_progress"]
        if samples:
            latest = samples[-1]
            scope = str(latest.details.get("scope", "batch"))
            percent = int(latest.details.get("percent", 0))
            response.update({
                "rip_progress_percent": percent,
                "rip_progress_scope": scope,
            })
        throughput = next(
            (
                event
                for event in reversed(events)
                if event.event_type == "rip_throughput"
            ),
            None,
        )
        if throughput is not None:
            response["rip_transfer_mib_s"] = float(
                throughput.details.get("mib_per_second", 0)
            )
        activity = throughput or (samples[-1] if samples else None)
        if activity is not None:
            response["rip_progress_updated_at"] = activity.created_at
    if store is not None and job.state == "failed":
        failures = [
            event
            for event in store.list_events(job.job_id)
            if event.event_type == "job_failed"
        ]
        if failures:
            details = failures[-1].details
            category = str(details.get("error_category", "unknown"))
            response.update({
                "error_type": details.get("error_type"),
                "error_category": category,
                "failed_drive_indexes": details.get("failed_drive_indexes", []),
                "recommendations": _RIP_RECOMMENDATIONS.get(
                    category, _RIP_RECOMMENDATIONS["unknown"]
                ),
            })
    return response


def _store_error(error: RipError) -> HTTPException:
    detail = str(error)
    status_code = 404 if "not found" in detail.casefold() else 409
    return HTTPException(status_code=status_code, detail=detail)


@router.post("/jobs", response_model=OrchestrationJobResponse)
def create_rip_job(
    request: RipPreviewRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
    private_store: Annotated[
        PrivateBindingStore,
        Depends(get_private_binding_store),
    ],
) -> dict[str, object]:
    """Persist public review state plus an isolated private path binding."""

    if not request.output_root:
        raise HTTPException(
            status_code=400,
            detail="An existing output root is required for a durable job",
        )
    try:
        preview = _build_preview_from_request(request)
        job = store.create_job(
            preview,
            idempotency_key=idempotency_key,
        )
        private_store.bind(
            job_id=job.job_id,
            plan_sha256=job.plan_sha256,
            report_paths=[Path(value) for value in request.report_paths],
            output_root=Path(request.output_root),
            media_contexts=_media_contexts_from_request(request),
        )
    except RipError as exc:
        raise _store_error(exc) from exc
    return _job_response(job)


@router.get("/jobs/{job_id}", response_model=OrchestrationJobResponse)
def get_rip_job(
    job_id: str,
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
) -> dict[str, object]:
    try:
        return _job_response(store.get_job(job_id), store)
    except RipError as exc:
        raise _store_error(exc) from exc


@router.post(
    "/jobs/{job_id}/resolve-collisions",
    response_model=OrchestrationJobResponse,
)
def resolve_rip_collisions(
    job_id: str,
    request: ResolveRipCollisionsRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
    private_store: Annotated[
        PrivateBindingStore,
        Depends(get_private_binding_store),
    ],
) -> dict[str, object]:
    """Create a new immutable, collision-safe review from an existing review."""

    if request.confirm_resolution is not True:
        raise HTTPException(
            status_code=400, detail="Collision choice confirmation is required"
        )
    try:
        source_job = store.get_job(job_id)
        binding = private_store.get(job_id)
        selected_by_disc = _resolution_title_indexes(
            source_job.preview.get("jobs", []), request.policy
        )

        contexts = {
            disc_id: replace(
                context,
                staging_attempt=f"attempt-{uuid4().hex[:12]}",
                selected_title_indexes=tuple(sorted(selected_by_disc[disc_id])),
                existing_output_policy=(
                    "replace-after-verification"
                    if request.policy == "replace-after-verification"
                    else "preserve"
                ),
            )
            for disc_id, context in binding.media_contexts.items()
            if disc_id in selected_by_disc
        }
        if contexts.keys() != selected_by_disc.keys():
            raise RipError("Saved rip review is missing its private media context")
        preview = build_rip_preview(
            list(binding.report_paths),
            contexts,
            output_root=binding.output_root,
        )
        replacement = store.create_job(preview, idempotency_key=idempotency_key)
        private_store.bind(
            job_id=replacement.job_id,
            plan_sha256=replacement.plan_sha256,
            report_paths=list(binding.report_paths),
            output_root=binding.output_root,
            media_contexts=contexts,
        )
        authorized = store.authorize(
            replacement.job_id,
            expected_plan_sha256=replacement.plan_sha256,
            idempotency_key=f"{idempotency_key}-authorize",
        )
        queued = store.queue(
            authorized.job_id,
            idempotency_key=f"{idempotency_key}-queue",
        )
        return _job_response(queued, store)
    except RipError as exc:
        raise _store_error(exc) from exc


@router.post("/jobs/{job_id}/staged-library-duplicates/preview")
def preview_staged_library_duplicates(
    job_id: str,
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
    private_store: Annotated[PrivateBindingStore, Depends(get_private_binding_store)],
) -> dict[str, object]:
    """Preview exact staged MKVs whose historical Jellyfin output still exists."""

    try:
        job = store.get_job(job_id)
        binding = private_store.get(job_id)
        digest, entries = _staged_library_duplicate_plan(
            job, binding, get_config_manager().load()
        )
    except (RipError, OSError, ValueError) as exc:
        raise _store_error(RipError(str(exc))) from exc
    return {
        "plan_sha256": digest,
        "file_count": len(entries),
        "total_size_bytes": sum(size for _path, size in entries),
        "jellyfin_files_affected": 0,
    }


@router.post("/jobs/{job_id}/staged-library-duplicates/delete")
def delete_staged_library_duplicates(
    job_id: str,
    request: DeleteStagedLibraryDuplicatesRequest,
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
    private_store: Annotated[PrivateBindingStore, Depends(get_private_binding_store)],
) -> dict[str, object]:
    """Delete one exact reviewed set of redundant staged MKVs, never Jellyfin."""

    if request.confirm_delete is not True:
        raise HTTPException(
            status_code=400,
            detail="Exact staged-file deletion confirmation is required",
        )
    try:
        job = store.get_job(job_id)
        binding = private_store.get(job_id)
        digest, entries = _staged_library_duplicate_plan(
            job, binding, get_config_manager().load()
        )
        if not entries:
            raise RipError(
                "No verified staged files with existing Jellyfin outputs are available"
            )
        if (
            digest != request.expected_plan_sha256
            or len(entries) != request.authorized_file_count
        ):
            raise RipError("Staged-file deletion plan changed; review it again")
        for path, expected_size in entries:
            if not path.is_file() or path.stat().st_size != expected_size:
                raise RipError("A staged file changed; no further files were deleted")
        for path, _expected_size in entries:
            path.unlink()
    except RipError as exc:
        raise _store_error(exc) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=409, detail="A reviewed staged file could not be deleted"
        ) from exc
    return {
        "deleted_file_count": len(entries),
        "deleted_size_bytes": sum(size for _path, size in entries),
        "jellyfin_files_affected": 0,
    }


@router.post("/jobs/{job_id}/select-titles", response_model=OrchestrationJobResponse)
def select_rip_titles(
    job_id: str,
    request: SelectRipTitlesRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
    private_store: Annotated[
        PrivateBindingStore,
        Depends(get_private_binding_store),
    ],
) -> dict[str, object]:
    """Create a new immutable review containing the exact checked titles."""

    if request.confirm_selection is not True:
        raise HTTPException(
            status_code=400, detail="Title selection confirmation is required"
        )
    selected = set(request.title_indexes)
    if len(selected) != len(request.title_indexes) or any(
        index < 0 for index in selected
    ):
        raise HTTPException(
            status_code=400, detail="Selected title indexes are invalid"
        )
    try:
        source_job = store.get_job(job_id)
        binding = private_store.get(job_id)
        available = {
            item.get("title_index")
            for item in source_job.preview.get("jobs", [])
            if isinstance(item, dict) and isinstance(item.get("title_index"), int)
        }
        if not selected <= available:
            raise RipError("Selected titles are not part of this reviewed disc")
        contexts = {
            disc_id: replace(
                context,
                staging_attempt=f"attempt-{uuid4().hex[:12]}",
                selected_title_indexes=tuple(sorted(selected)),
            )
            for disc_id, context in binding.media_contexts.items()
        }
        preview = build_rip_preview(
            list(binding.report_paths), contexts, output_root=binding.output_root
        )
        selected_job = store.create_job(preview, idempotency_key=idempotency_key)
        private_store.bind(
            job_id=selected_job.job_id,
            plan_sha256=selected_job.plan_sha256,
            report_paths=list(binding.report_paths),
            output_root=binding.output_root,
            media_contexts=contexts,
        )
        return _job_response(selected_job, store)
    except RipError as exc:
        raise _store_error(exc) from exc


@router.get("/jobs", response_model=OrchestrationJobListResponse)
def list_rip_jobs(
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
) -> dict[str, object]:
    """Return recent redacted jobs; this endpoint never discovers drives."""

    from mkv_episode_matcher.core.config_manager import get_config_manager

    automatic = get_config_manager().load().automatic_processing_enabled
    return {
        "automatic_processing_enabled": automatic,
        "watcher_attached": False,
        "jobs": [_job_response(job, store) for job in store.list_jobs()],
    }


@router.get(
    "/jobs/{job_id}/events",
    response_model=list[OrchestrationEventResponse],
)
def get_rip_job_events(
    job_id: str,
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
) -> list[dict[str, object]]:
    try:
        return [asdict(event) for event in store.list_events(job_id)]
    except RipError as exc:
        raise _store_error(exc) from exc


@router.post(
    "/jobs/{job_id}/authorize",
    response_model=OrchestrationJobResponse,
)
def authorize_rip_job(
    job_id: str,
    request: AuthorizeJobRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
) -> dict[str, object]:
    if request.confirm_authorization is not True:
        raise HTTPException(
            status_code=400,
            detail="Exact rip authorization confirmation is required",
        )
    try:
        job = store.authorize(
            job_id,
            expected_plan_sha256=request.expected_plan_sha256,
            idempotency_key=idempotency_key,
        )
    except RipError as exc:
        raise _store_error(exc) from exc
    return _job_response(job)


@router.post("/jobs/{job_id}/start", response_model=OrchestrationJobResponse)
def start_rip_job(
    job_id: str,
    request: StartJobRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
) -> dict[str, object]:
    if request.confirm_queue is not True:
        raise HTTPException(
            status_code=400,
            detail="Explicit queue confirmation is required",
        )
    try:
        return _job_response(store.queue(job_id, idempotency_key=idempotency_key))
    except RipError as exc:
        raise _store_error(exc) from exc


@router.post("/jobs/{job_id}/return-to-review", response_model=OrchestrationJobResponse)
def return_rip_job_to_review(
    job_id: str,
    request: ControlJobRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
) -> dict[str, object]:
    if request.confirm_control is not True:
        raise HTTPException(
            status_code=400, detail="Return-to-review confirmation is required"
        )
    try:
        return _job_response(
            store.return_to_review(job_id, idempotency_key=idempotency_key), store
        )
    except RipError as exc:
        raise _store_error(exc) from exc


@router.post("/jobs/{job_id}/cancel", response_model=OrchestrationJobResponse)
def cancel_rip_job(
    job_id: str,
    request: ControlJobRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
    registry: Annotated[RipExecutionRegistry, Depends(get_rip_execution_registry)],
) -> dict[str, object]:
    """Cancel a non-running rip plan without changing staged media."""

    if request.confirm_control is not True:
        raise HTTPException(
            status_code=400, detail="Rip cancellation confirmation is required"
        )
    if registry.is_job_active(job_id):
        raise HTTPException(
            status_code=409,
            detail="MakeMKV is active for this disc; stop it safely before cancelling",
        )
    try:
        return _job_response(
            store.cancel(job_id, idempotency_key=idempotency_key), store
        )
    except RipError as exc:
        raise _store_error(exc) from exc


@router.get("/jobs/{job_id}/failed-attempts")
def preview_failed_rip_attempts(
    job_id: str,
    private_store: Annotated[
        PrivateBindingStore,
        Depends(get_private_binding_store),
    ],
) -> dict[str, object]:
    """Return a path-redacted exact cleanup preview; never remove media."""

    try:
        binding = private_store.get(job_id)
        manifest = build_rip_manifest(
            list(binding.report_paths), binding.media_contexts
        )
        return plan_failed_rip_cleanup(binding.output_root, manifest.jobs).public_dict()
    except RipError as exc:
        raise _store_error(exc) from exc


def _apply_requested_failed_cleanup(
    request: ExecuteJobRequest,
    job_id: str,
    registry: RipExecutionRegistry,
    private_store: PrivateBindingStore,
) -> None:
    if request.preserve_failed_partials:
        return
    if (
        request.confirm_failed_cleanup is not True
        or request.failed_cleanup_sha256 is None
    ):
        raise RipError("Exact failed-attempt cleanup confirmation is required")
    if registry.is_job_active(job_id):
        raise RipError("This rip job is active; its failed attempts cannot be cleaned")
    binding = private_store.get(job_id)
    manifest = build_rip_manifest(list(binding.report_paths), binding.media_contexts)
    apply_failed_rip_cleanup(
        binding.output_root,
        manifest.jobs,
        expected_plan_sha256=request.failed_cleanup_sha256,
    )


@router.post("/jobs/{job_id}/execute", response_model=OrchestrationJobResponse)
def execute_rip_job(
    job_id: str,
    request: ExecuteJobRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
    private_store: Annotated[
        PrivateBindingStore,
        Depends(get_private_binding_store),
    ],
    registry: Annotated[
        RipExecutionRegistry,
        Depends(get_rip_execution_registry),
    ],
    queue_runner: Annotated[Callable, Depends(get_rip_queue_runner)],
    pipeline_store: Annotated[
        PipelineQueueStore,
        Depends(get_pipeline_queue_store),
    ],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
    inventory_runner: Annotated[Callable, Depends(get_disc_inventory_runner)],
) -> dict[str, object]:
    """Synchronously dispatch one exactly authorized queued rip job."""

    if request.confirm_execute is not True:
        raise HTTPException(
            status_code=400,
            detail="Explicit physical rip confirmation is required",
        )
    try:
        job = store.get_job(job_id)
        if request.expected_plan_sha256 != job.plan_sha256:
            raise RipError("Execute request digest does not match the queued job")
        if request.authorized_job_count != len(job.preview.get("jobs", [])):
            raise RipError("Execute request job count does not match the queued job")
        _apply_requested_failed_cleanup(request, job_id, registry, private_store)

        def enqueue_results(bound, results) -> None:
            try:
                contract_root.mkdir(parents=True, exist_ok=True)
                enqueue_verified_rip_results(
                    pipeline_store,
                    jobs=bound.manifest.jobs,
                    results=results,
                    output_root=bound.output_root,
                    contract_root=contract_root,
                    media_contexts={
                        context.disc_id: context
                        for context in bound.manifest.media_contexts
                    },
                )
            except (OSError, PipelineQueueError) as exc:
                raise RipError(
                    f"Verified rip queue handoff failed: {type(exc).__name__}"
                ) from exc

        config = get_config_manager().load()
        executable = resolve_makemkv_path(
            Path(request.makemkv_executable)
            if request.makemkv_executable
            else config.makemkv_path
        )
        run_directory = (
            Path(request.run_directory)
            if request.run_directory
            else config.cache_dir.parent
            / "orchestration"
            / "rip-runs"
            / f"{job_id}-{uuid4().hex[:12]}"
        )
        executor = ProductionRipExecutor(
            RipExecutionOptions(
                makemkv_executable=executable,
                run_directory=run_directory,
                timeout_seconds=request.timeout_seconds,
                max_drives=request.max_drives,
            ),
            queue_runner=queue_runner,
            completion_sink=enqueue_results,
            progress_sink=lambda kind, message: store.record_progress(
                job_id, kind, message
            ),
        )
        registry.attach(job_id, run_directory)
        try:
            completed = RipDispatcher(store, private_store).dispatch(
                job_id,
                dispatch_key=idempotency_key,
                executor=executor,
            )
        finally:
            registry.detach(job_id)
        if config.automatic_eject_after_rip and completed.state == "completed":
            _auto_eject_completed_job_drives(
                completed,
                store,
                inventory_runner,
                executable,
                request.timeout_seconds,
            )
        return _job_response(completed, store)
    except RipError as exc:
        raise _store_error(exc) from exc


@router.post("/jobs/{job_id}/pause", response_model=OrchestrationJobResponse)
def pause_rip_job(
    job_id: str,
    request: ControlJobRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
    registry: Annotated[
        RipExecutionRegistry,
        Depends(get_rip_execution_registry),
    ],
) -> dict[str, object]:
    if request.confirm_control is not True:
        raise HTTPException(status_code=400, detail="Pause confirmation is required")
    try:
        if store.get_job(job_id).state == "running":
            registry.request_marker(job_id, "PAUSE")
        return _job_response(store.pause(job_id, idempotency_key=idempotency_key))
    except RipError as exc:
        raise _store_error(exc) from exc


@router.post("/jobs/{job_id}/stop", response_model=OrchestrationJobResponse)
def stop_rip_job(
    job_id: str,
    request: ControlJobRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
    registry: Annotated[
        RipExecutionRegistry,
        Depends(get_rip_execution_registry),
    ],
) -> dict[str, object]:
    if request.confirm_control is not True:
        raise HTTPException(status_code=400, detail="Stop confirmation is required")
    try:
        if store.get_job(job_id).state != "running":
            raise RipError("Stop requires a running physical rip job")
        registry.request_marker(job_id, "STOP")
        return _job_response(store.pause(job_id, idempotency_key=idempotency_key))
    except RipError as exc:
        raise _store_error(exc) from exc


@router.post(
    "/special-features/execute",
    response_model=SpecialFeatureExecutionResponse,
)
def execute_special_feature_job(
    request: ExecuteSpecialFeatureRequest,
    queue_runner: Annotated[
        Callable,
        Depends(get_special_feature_queue_runner),
    ],
) -> dict[str, object]:
    """Execute one exact, freshly rebound special-feature title set."""

    if request.confirm_execute is not True:
        raise HTTPException(
            status_code=400,
            detail="Explicit special-feature rip confirmation is required",
        )
    try:
        manifest = load_bound_special_feature_manifest(
            Path(request.bound_manifest),
            Path(request.fresh_inventory),
            expected_bound_sha256=request.bound_sha256,
        )
        results = execute_bound_special_feature_manifest(
            manifest,
            bound_manifest_sha256=request.bound_sha256,
            executable=Path(request.makemkv_executable),
            output_root=Path(request.output_root),
            run_dir=Path(request.run_directory),
            authorized_job_count=request.authorized_job_count,
            timeout_seconds=request.timeout_seconds,
            queue_runner=queue_runner,
        )
    except (RipError, SpecialFeatureBindError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "mode": "special-feature-rip-result",
        "status": "completed",
        "manifest_sha256": request.bound_sha256,
        "completed_count": len(results),
    }


@router.get("/pipeline/items", response_model=PipelineQueueResponse)
def get_pipeline_items(
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Return path-redacted downstream queue state."""

    return {
        "paused": store.is_paused(),
        "downstream_worker_limit": 1,
        "items": [_pipeline_item_response(item) for item in store.list_items()],
    }


@router.get("/pipeline/events")
def get_pipeline_events(
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Return the durable path-free downstream event stream."""

    events = store.list_events()
    return {
        "events": [asdict(event) for event in events[-1000:]],
        "path_redacted": True,
    }


@router.post("/pipeline/items/dismiss", response_model=PipelineQueueResponse)
def dismiss_pipeline_items(
    request: DismissPipelineItemsRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Remove held records from active presentation without changing media."""

    if request.confirm_dismiss is not True:
        raise HTTPException(
            status_code=400, detail="Queue clear confirmation is required"
        )
    try:
        store.dismiss_items(tuple(request.media_ids))
    except PipelineQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return get_pipeline_items(store)


@router.post("/pipeline/items/cancel-queued", response_model=PipelineQueueResponse)
def cancel_queued_pipeline_items(
    request: CancelQueuedPipelineItemsRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Remove waiting work from the queue while preserving all media."""

    if request.confirm_cancel is not True:
        raise HTTPException(
            status_code=400, detail="Queued-work cancellation confirmation is required"
        )
    try:
        store.cancel_queued_items(tuple(request.media_ids))
    except PipelineQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return get_pipeline_items(store)


@router.post(
    "/pipeline/items/{media_id}/delete-staged-source",
    response_model=PipelineQueueResponse,
)
def delete_queued_pipeline_media(
    media_id: str,
    request: DeleteQueuedPipelineMediaRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Delete one exact inactive rip source after explicit destructive consent."""

    if request.confirm_delete is not True:
        raise HTTPException(
            status_code=400,
            detail="Permanent staged-rip deletion confirmation is required",
        )

    root_value = get_config_manager().load().rip_output_root
    if root_value is None:
        raise HTTPException(
            status_code=409, detail="The rip staging root is not configured"
        )
    try:
        store.delete_queued_item_media(
            media_id, lambda item: _delete_exact_queued_rip(item, root_value)
        )
    except PipelineQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return get_pipeline_items(store)


@router.post("/pipeline/apply-episode-release")
def apply_episode_release(  # noqa: C901
    request: ApplyEpisodeReleaseRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict[str, object]:
    """Apply one reviewed, path-free release catalogue to held rip contracts."""

    if request.confirm_apply is not True:
        raise HTTPException(
            status_code=400,
            detail="Reviewed episode catalogue confirmation is required",
        )
    catalog = catalog_by_id(request.catalog_id)
    if catalog is None:
        raise HTTPException(
            status_code=404, detail="Episode release catalogue was not found"
        )
    expected = {item.title_index for item in catalog.assignments}
    selected_by_title: dict[int, tuple[object, dict[str, object], int]] = {}
    try:
        for item in store.list_items():
            if item.state != "review_required" or item.stage != "identify":
                continue
            artifact = store.rip_artifact(item.media_id)
            payload = json.loads(artifact.contract_path.read_text(encoding="utf-8"))
            if payload.get("disc_fingerprint") != request.disc_fingerprint:
                continue
            title_index = payload.get("title_index")
            if isinstance(title_index, int) and not isinstance(title_index, bool):
                previous = selected_by_title.get(title_index)
                if previous is None or item.updated_at >= previous[0].updated_at:
                    selected_by_title[title_index] = (item, payload, title_index)
        selected = list(selected_by_title.values())
        if {title_index for _item, _payload, title_index in selected} != expected:
            raise PipelineQueueError(
                "Held title set does not exactly match the reviewed release catalogue"
            )
        for item, payload, _title_index in selected:
            context = payload.get("media_context")
            if not isinstance(context, dict):
                raise PipelineQueueError("Verified rip media context is invalid")
            revised = dict(payload)
            revised["media_context"] = {
                **context,
                "series_name": catalog.series_name,
                "season": None,
                "episode_assignments": list(public_assignments(catalog)),
            }
            path = contract_root / (
                f"{item.media_id}.reviewed-{catalog.catalog_id}-{uuid4().hex[:8]}.json"
            )
            path.write_text(
                json.dumps(revised, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            store.apply_reviewed_identification_input(
                item.media_id, build_artifact("rip", path)
            )
    except (OSError, json.JSONDecodeError, PipelineQueueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="Reviewed episode catalogue could not be applied safely",
        ) from exc
    return {
        "catalog_id": catalog.catalog_id,
        "queued_item_count": len(selected),
        "disc_fingerprint": request.disc_fingerprint,
    }


@router.post("/pipeline/analyze-unmatched-disc")
def analyze_unmatched_disc(  # noqa: C901 - guarded asynchronous disc workflow
    request: UnmatchedDiscAnalysisRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict[str, object]:
    """Start bounded local evidence collection and all-season sequence matching."""

    if not request.confirm_media_read or not request.confirm_provider_lookup:
        raise HTTPException(
            status_code=400,
            detail="Media-read and episode-catalogue lookup confirmations are required",
        )
    series_name = " ".join(request.series_name.split())
    selected_by_title = {}
    for item in store.list_items():
        title_match = re.search(r"-title-(\d{3})(?:-|$)", item.media_id)
        if (
            _pipeline_item_response(item)["disc_fingerprint"]
            != request.disc_fingerprint
            or title_match is None
            or item.stage != "identify"
            or item.state != "review_required"
            or item.review_code
            not in {
                "missing_season_context",
                "unmatched_disc_analysis_required",
                "all_season_analysis_failed",
                "all_season_sequence_review_required",
            }
        ):
            continue
        title_index = int(title_match.group(1))
        previous = selected_by_title.get(title_index)
        if previous is None or item.updated_at >= previous.updated_at:
            selected_by_title[title_index] = item
    selected = tuple(
        selected_by_title[index].media_id for index in sorted(selected_by_title)
    )
    if not selected:
        raise HTTPException(
            status_code=409, detail="No unmatched disc titles are ready"
        )
    for media_id in selected:
        store.choose_review_path(media_id, "all_season_analysis_running")
    store.set_paused(False)

    def run() -> None:
        try:
            config = get_config_manager().load()
            execute_unmatched_disc_analysis(
                store,
                request.disc_fingerprint,
                series_name,
                config,
                get_engine().asr,
                contract_root,
                season=request.season,
                allow_gemini=request.confirm_external_fallback,
            )
        except Exception as exc:
            logger.error(
                "All-season disc analysis failed safely: {}", type(exc).__name__
            )
            code = (
                "all_season_sequence_review_required"
                if str(exc) == "All-season sequence result requires review"
                else "gemini_provider_failed"
                if str(exc) == "Automatic Gemini all-season fallback failed"
                else "all_season_analysis_failed"
            )
            for media_id in selected:
                try:
                    if store.get(media_id).state == "review_required":
                        store.choose_review_path(media_id, code)
                except PipelineQueueError:
                    pass

    threading.Thread(target=run, name="all-season-disc-analysis", daemon=True).start()
    return {"started": True, "item_count": len(selected)}


@router.post("/pipeline/classify-unmatched-disc")
def classify_unmatched_disc(
    request: UnmatchedDiscClassificationRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict[str, object]:
    """Apply a reviewed non-TV route to the newest held titles for one disc."""

    if request.confirm_classification is not True:
        raise HTTPException(status_code=400, detail="Disc classification is required")
    selected_by_title = {}
    for item in store.list_items():
        title_match = re.search(r"-title-(\d{3})(?:-|$)", item.media_id)
        if (
            _pipeline_item_response(item)["disc_fingerprint"]
            != request.disc_fingerprint
            or title_match is None
            or item.stage != "identify"
            or item.state != "review_required"
            or item.review_code
            not in {
                "missing_season_context",
                "unmatched_disc_analysis_required",
                "all_season_analysis_failed",
                "all_season_sequence_review_required",
            }
        ):
            continue
        title_index = int(title_match.group(1))
        previous = selected_by_title.get(title_index)
        if previous is None or item.updated_at >= previous.updated_at:
            selected_by_title[title_index] = item
    selected = tuple(selected_by_title[index] for index in sorted(selected_by_title))
    if not selected:
        raise HTTPException(status_code=409, detail="No held disc titles are ready")
    try:
        contract_root.mkdir(parents=True, exist_ok=True)
        for item in selected:
            payload = json.loads(
                item.artifact.contract_path.read_text(encoding="utf-8")
            )
            context = payload.get("media_context")
            if not isinstance(context, dict):
                raise PipelineQueueError("Held media context is unavailable")
            revised = dict(payload)
            revised["media_context"] = {
                **context,
                "content_hint": request.classification,
            }
            path = contract_root / f"{item.media_id}.classified-{uuid4().hex[:8]}.json"
            path.write_text(
                json.dumps(revised, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            store.apply_reviewed_identification_input(
                item.media_id, build_artifact("rip", path)
            )
        store.set_paused(False)
    except (OSError, json.JSONDecodeError, PipelineQueueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="The reviewed disc classification could not be applied safely",
        ) from exc
    return {
        "classification": request.classification,
        "queued_item_count": len(selected),
        "media_ids": [item.media_id for item in selected],
    }


def _review_media_path(store: PipelineQueueStore, media_id: str) -> tuple[Path, int]:
    item = store.get(media_id)
    try:
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
        archived = payload.get("archived_source_path")
        archived_size = payload.get("archived_source_size_bytes")
        if isinstance(archived, str) and isinstance(archived_size, int):
            candidate = Path(archived).resolve()
            if candidate.is_file() and candidate.stat().st_size == archived_size:
                return candidate, archived_size
        rip = store.rip_artifact(media_id)
        rip_payload = json.loads(rip.contract_path.read_text(encoding="utf-8"))
        candidate = Path(str(rip_payload["source_path"])).resolve()
        size = int(rip_payload["source_size_bytes"])
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise PipelineQueueError("Review media is unavailable") from exc
    if not candidate.is_file() or size <= 0 or candidate.stat().st_size != size:
        raise PipelineQueueError("Review media is missing or changed")
    return candidate, size


@router.post("/pipeline/items/{media_id}/play-review")
def play_pipeline_item_for_review(
    media_id: str,
    request: PlayPipelineItemRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Open one exact recorded MKV in the server user's default media player."""

    if not request.confirm_play:
        raise HTTPException(status_code=400, detail="Playback confirmation is required")
    try:
        source, _size = _review_media_path(store, media_id)
        if os.name != "nt":
            raise PipelineQueueError(
                "Default-player review is available only on Windows"
            )
        os.startfile(source)  # type: ignore[attr-defined]  # noqa: S606
    except (PipelineQueueError, OSError) as exc:
        raise HTTPException(
            status_code=409, detail="Review playback could not start"
        ) from exc
    return {"started": True, "media_id": media_id}


@router.post("/pipeline/items/{media_id}/rename-provisional")
def rename_provisional_pipeline_item(
    media_id: str,
    request: RenameProvisionalItemRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict[str, object]:
    """Apply one collision-refusing user-reviewed Jellyfin basename."""

    if not request.confirm_rename:
        raise HTTPException(
            status_code=400, detail="Exact rename confirmation is required"
        )
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", request.new_name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned.casefold().endswith(".mkv"):
        raise HTTPException(
            status_code=400,
            detail="Enter a filename without an extension; .mkv is preserved automatically",
        )
    try:
        item = store.get(media_id)
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
        if not payload.get("provisional_match"):
            raise PipelineQueueError(
                "Only a provisional match may use this rename action"
            )
        relative = Path(str(payload["library_relative"]))
        transcode = json.loads(
            (contract_root / f"{media_id}.transcode.json").read_text(encoding="utf-8")
        )
        config = get_config_manager().load()
        root = (
            config.jellyfin_tv_root
            if transcode.get("episode_id")
            else config.jellyfin_movie_root
        )
        if root is None or not root.is_dir():
            raise PipelineQueueError("Jellyfin root is unavailable")
        source = (root / relative).resolve()
        source.relative_to(root.resolve())
        expected_size = int(payload["output_size_bytes"])
        if not source.is_file() or source.stat().st_size != expected_size:
            raise PipelineQueueError("Jellyfin output is missing or changed")
        destination = source.with_name(f"{cleaned}.mkv")
        if destination.exists():
            raise PipelineReviewRequiredError("library_collision")
        source.rename(destination)
        revised = dict(payload)
        revised.update(
            library_relative=(relative.parent / destination.name).as_posix(),
            provisional_match=False,
            user_reviewed_name=True,
        )
        contract = contract_root / f"{media_id}.organize.rename-{uuid4().hex[:12]}.json"
        contract.write_text(
            json.dumps(revised, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        updated = store.revise_completed_organization(
            media_id, build_artifact("organize", contract)
        )
    except (
        PipelineQueueError,
        PipelineReviewRequiredError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _pipeline_item_response(updated)


def _retained_source_plan(
    store: PipelineQueueStore, media_ids: list[str], deletion_root: Path | None
) -> tuple[str, list[tuple[str, Path, int]]]:
    if deletion_root is None or not deletion_root.is_dir():
        raise PipelineQueueError("Configure an existing staging-for-deletion root")
    root = deletion_root.resolve()
    if len(set(media_ids)) != len(media_ids):
        raise PipelineQueueError("Retained-source selection contains duplicates")
    entries = []
    for media_id in sorted(media_ids):
        item = store.get(media_id)
        if item.state != "completed" or item.stage != "organize":
            raise PipelineQueueError("Only completed organized items may be selected")
        try:
            payload = json.loads(
                item.artifact.contract_path.read_text(encoding="utf-8")
            )
            path = Path(str(payload["archived_source_path"])).resolve()
            size = int(payload["archived_source_size_bytes"])
            path.relative_to(root)
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise PipelineQueueError("Retained source contract is unavailable") from exc
        if not path.is_file() or size <= 0 or path.stat().st_size != size:
            raise PipelineQueueError("Retained source is missing or changed")
        entries.append((media_id, path, size))
    identity = json.dumps(
        [
            (media_id, path.relative_to(root).as_posix(), size)
            for media_id, path, size in entries
        ],
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(identity).hexdigest(), entries


def _existing_source_retention_plan(
    store: PipelineQueueStore, media_ids: list[str], deletion_root: Path | None
) -> tuple[str, list[tuple[str, Path, Path, int]]]:
    if deletion_root is None or not deletion_root.is_dir():
        raise PipelineQueueError("Configure an existing staging-for-deletion root")
    root = deletion_root.resolve()
    if len(set(media_ids)) != len(media_ids):
        raise PipelineQueueError("Source-retention selection contains duplicates")
    entries = []
    for media_id in sorted(media_ids):
        item = store.get(media_id)
        if item.state != "completed" or item.stage != "organize":
            raise PipelineQueueError("Only completed organized items may be retained")
        rip_payload = json.loads(
            store.rip_artifact(media_id).contract_path.read_text(encoding="utf-8")
        )
        source = Path(str(rip_payload["source_path"])).resolve()
        size = int(rip_payload["source_size_bytes"])
        if not source.is_file() or size <= 0 or source.stat().st_size != size:
            raise PipelineQueueError("Verified original is missing or changed")
        destination = (root / media_id / source.name).resolve()
        destination.relative_to(root)
        if destination.exists() or destination.parent.exists():
            raise PipelineReviewRequiredError("deletion_staging_collision")
        entries.append((media_id, source, destination, size))
    identity = json.dumps(
        [
            (media_id, destination.relative_to(root).as_posix(), size)
            for media_id, _source, destination, size in entries
        ],
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(identity).hexdigest(), entries


@router.post("/pipeline/retained-sources/preview-retain-existing")
def preview_existing_source_retention(
    request: RetainedSourceRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Preview moving exact verified legacy originals into cleanup staging."""

    try:
        digest, entries = _existing_source_retention_plan(
            store, request.media_ids, get_config_manager().load().deletion_staging_root
        )
    except (
        PipelineQueueError,
        PipelineReviewRequiredError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "plan_sha256": digest,
        "file_count": len(entries),
        "total_size_bytes": sum(entry[3] for entry in entries),
        "jellyfin_files_affected": 0,
    }


@router.post("/pipeline/retained-sources/retain-existing")
def retain_existing_sources(
    request: RetainExistingSourceRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict[str, object]:
    """Move a reviewed exact set of legacy originals into cleanup staging."""

    if not request.confirm_move:
        raise HTTPException(
            status_code=400, detail="Exact retention confirmation is required"
        )
    try:
        digest, entries = _existing_source_retention_plan(
            store, request.media_ids, get_config_manager().load().deletion_staging_root
        )
        if (
            digest != request.expected_plan_sha256
            or len(entries) != request.authorized_file_count
        ):
            raise PipelineQueueError("Source-retention plan changed")
        completed = 0
        for media_id, source, destination, size in entries:
            item = store.get(media_id)
            payload = json.loads(
                item.artifact.contract_path.read_text(encoding="utf-8")
            )
            revised = dict(payload)
            revised.update(
                archived_source_path=str(destination), archived_source_size_bytes=size
            )
            contract = (
                contract_root / f"{media_id}.organize.retain-{uuid4().hex[:12]}.json"
            )
            contract.write_text(
                json.dumps(revised, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            destination.parent.mkdir(parents=True, exist_ok=False)
            shutil.move(str(source), str(destination))
            if not destination.is_file() or destination.stat().st_size != size:
                raise PipelineQueueError("Retained original verification failed")
            store.revise_completed_organization(
                media_id, build_artifact("organize", contract)
            )
            completed += 1
    except (
        PipelineQueueError,
        PipelineReviewRequiredError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"retained_file_count": completed, "jellyfin_files_affected": 0}


@router.post("/pipeline/retained-sources/preview-delete")
def preview_retained_source_deletion(
    request: RetainedSourceRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Preview an exact deletion batch without exposing private source paths."""

    try:
        digest, entries = _retained_source_plan(
            store, request.media_ids, get_config_manager().load().deletion_staging_root
        )
    except PipelineQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "plan_sha256": digest,
        "file_count": len(entries),
        "total_size_bytes": sum(entry[2] for entry in entries),
        "jellyfin_files_affected": 0,
    }


@router.post("/pipeline/retained-sources/delete")
def delete_retained_sources(
    request: DeleteRetainedSourceRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Delete only an exact reviewed set of retained originals."""

    if not request.confirm_delete:
        raise HTTPException(
            status_code=400, detail="Exact deletion confirmation is required"
        )
    try:
        digest, entries = _retained_source_plan(
            store, request.media_ids, get_config_manager().load().deletion_staging_root
        )
        if (
            digest != request.expected_plan_sha256
            or len(entries) != request.authorized_file_count
        ):
            raise PipelineQueueError("Retained-source deletion plan changed")
        for _media_id, path, _size in entries:
            path.unlink()
    except PipelineQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=409, detail="A retained source could not be deleted"
        ) from exc
    return {"deleted_file_count": len(entries), "jellyfin_files_affected": 0}


@router.post("/pipeline/retained-sources/reencode")
def reencode_retained_sources(
    request: ReencodeRetainedSourceRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
    profile_store: Annotated[
        HandBrakeProfileStore, Depends(get_handbrake_profile_store)
    ],
) -> dict[str, object]:
    """Queue retained originals at transcode while preserving matched identity."""

    if not request.confirm_reencode:
        raise HTTPException(
            status_code=400, detail="Re-encode confirmation is required"
        )
    try:
        if request.profile_id is not None and request.profile_id not in {
            profile.profile_id for profile in profile_store.list()
        }:
            raise PipelineQueueError("Selected HandBrake profile is unavailable")
        _digest, entries = _retained_source_plan(
            store, request.media_ids, get_config_manager().load().deletion_staging_root
        )
        queued = []
        for media_id, retained, size in entries:
            identify_path = contract_root / f"{media_id}.identify.json"
            identified = json.loads(identify_path.read_text(encoding="utf-8"))
            if identified.get("mode") != "identified-episode-contract":
                raise PipelineQueueError("Saved matched-name contract is unavailable")
            attempt = 1
            while True:
                new_id = f"{media_id}-reencode-{attempt:03d}"
                new_path = contract_root / f"{new_id}.identify.json"
                if not new_path.exists():
                    break
                attempt += 1
            identified.update(
                media_id=new_id,
                source_path=str(retained),
                source_size_bytes=size,
            )
            if request.profile_id is not None:
                identified["handbrake_profile_id"] = request.profile_id
            new_path.write_text(
                json.dumps(identified, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            store.enqueue_reencode(
                new_id,
                build_artifact("identify", new_path),
                store.rip_artifact(media_id),
            )
            queued.append(new_id)
    except (
        PipelineQueueError,
        HandBrakeProfileStoreError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        raise HTTPException(
            status_code=409, detail="Retained sources could not be queued safely"
        ) from exc
    return {"queued_item_count": len(queued)}


@router.post("/jobs/{job_id}/existing-rips/preview")
def preview_existing_rip_candidates(
    job_id: str,
    request: PreviewExistingRipsRequest,
    private_store: Annotated[PrivateBindingStore, Depends(get_private_binding_store)],
) -> dict[str, object]:
    """Search only the bound staging root for exact planned basenames."""

    if request.confirm_search is not True:
        raise HTTPException(
            status_code=400, detail="Existing-rip search confirmation is required"
        )
    try:
        binding = private_store.get(job_id)
        manifest = build_rip_manifest(
            list(binding.report_paths), binding.media_contexts
        )
        return discover_existing_rips(binding.output_root, manifest.jobs).public_dict()
    except RipError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/existing-rips/verify")
def verify_existing_rip_candidates(
    job_id: str,
    request: VerifyExistingRipsRequest,
    private_store: Annotated[PrivateBindingStore, Depends(get_private_binding_store)],
    pipeline_store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
    inspector: Annotated[Callable, Depends(get_ffprobe_inspector)],
) -> dict[str, object]:
    """FFprobe an exact recovery plan, then queue verified files at identify."""

    if request.confirm_media_read is not True:
        raise HTTPException(
            status_code=400, detail="Exact MKV verification confirmation is required"
        )
    selected_ids = set(request.candidate_ids)
    if not selected_ids or len(selected_ids) != len(request.candidate_ids):
        raise HTTPException(
            status_code=400, detail="Recovery title selection is invalid"
        )
    try:
        binding = private_store.get(job_id)
        manifest = build_rip_manifest(
            list(binding.report_paths), binding.media_contexts
        )
        plan = discover_existing_rips(binding.output_root, manifest.jobs)
        if plan.plan_sha256 != request.expected_plan_sha256:
            raise RipError("Existing-rip recovery plan changed; preview it again")
        candidates = tuple(
            item for item in plan.candidates if item.candidate_id in selected_ids
        )
        if len(candidates) != len(selected_ids):
            raise RipError(
                "One or more selected existing rips are unavailable or ambiguous"
            )
        selected = {item.title_index for item in candidates}
        if len(selected) != len(candidates):
            raise RipError("Select exactly one existing rip for each title")
        jobs = recovered_jobs(
            tuple(job for job in manifest.jobs if job.title_index in selected),
            replace(plan, candidates=candidates),
        )
        executable = resolve_ffprobe_path(get_config_manager().load().ffprobe_path)
        results = []
        now = datetime.now(UTC).isoformat()
        for job, candidate in zip(jobs, candidates, strict=True):
            source = (
                binding.output_root / candidate.relative_parent / candidate.basename
            )
            inspection = inspector(
                executable, source, timeout_seconds=request.timeout_seconds
            )
            if (
                inspection.media.duration_seconds <= 0
                or source.stat().st_size != candidate.size_bytes
            ):
                raise RipError(
                    "Existing rip verification returned inconsistent metadata"
                )
            results.append(
                RipResult(
                    job_id=job.job_id,
                    return_code=0,
                    output_count=1,
                    output_bytes=candidate.size_bytes,
                    warning_count=0,
                    started_at=now,
                    finished_at=datetime.now(UTC).isoformat(),
                )
            )
        contract_root.mkdir(parents=True, exist_ok=True)
        queued = enqueue_verified_rip_results(
            pipeline_store,
            jobs=jobs,
            results=results,
            output_root=binding.output_root,
            contract_root=contract_root,
            media_contexts=binding.media_contexts,
            media_id_overrides={
                job.job_id: (
                    f"{Path(candidate.basename).stem}-recovery-"
                    f"{candidate.candidate_id[:12]}"
                )
                for job, candidate in zip(jobs, candidates, strict=True)
            },
        )
        return {"verified_count": len(queued), "queued_for_identification": True}
    except (RipError, PipelineQueueError, FFprobeError, OSError) as exc:
        logger.warning(
            "Existing-rip verification stopped safely: {}", type(exc).__name__
        )
        detail = (
            "The selected MKV verified, but its recovery record conflicts with an "
            "earlier queue contract. Refresh the recovery choices and try again."
            if isinstance(exc, PipelineQueueError)
            else f"Existing-rip verification failed safely ({type(exc).__name__})"
        )
        raise HTTPException(
            status_code=409,
            detail=detail,
        ) from exc


@router.post("/jobs/{job_id}/restart-existing-pipeline")
def restart_existing_rip_pipeline(
    job_id: str,
    request: RestartExistingPipelineRequest,
    orchestration_store: Annotated[
        OrchestrationStore, Depends(get_orchestration_store)
    ],
    pipeline_store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Queue recorded verified rips at identification; never access a disc."""

    if request.confirm_restart is not True:
        raise HTTPException(
            status_code=400,
            detail="Existing-rip pipeline restart confirmation is required",
        )
    try:
        job = orchestration_store.get_job(job_id)
        jobs = job.preview.get("jobs", [])
        if not isinstance(jobs, list):
            raise PipelineQueueError("Saved rip review jobs are invalid")
        requested_indexes = (
            set(request.title_indexes) if request.title_indexes is not None else None
        )
        available_indexes = {
            item.get("title_index")
            for item in jobs
            if isinstance(item, dict) and isinstance(item.get("title_index"), int)
        }
        if requested_indexes is not None and (
            not requested_indexes
            or len(requested_indexes) != len(request.title_indexes or [])
            or not requested_indexes <= available_indexes
        ):
            raise PipelineQueueError("Selected existing-rip titles are invalid")
        restarted = []
        unavailable = []
        for item in jobs:
            media_id = item.get("job_id") if isinstance(item, dict) else None
            staging = (
                item.get("staging_destination") if isinstance(item, dict) else None
            )
            title_index = item.get("title_index") if isinstance(item, dict) else None
            if requested_indexes is not None and title_index not in requested_indexes:
                continue
            fingerprint = (
                next(
                    (
                        part
                        for part in Path(staging).parts
                        if re.fullmatch(r"[0-9a-f]{16}", part)
                    ),
                    None,
                )
                if isinstance(staging, str)
                else None
            )
            if (
                not isinstance(media_id, str)
                or not isinstance(title_index, int)
                or fingerprint is None
            ):
                raise PipelineQueueError("Saved rip review job is invalid")
            try:
                pipeline_store.restart_identification(
                    media_id,
                    expected_disc_fingerprint=fingerprint,
                    expected_title_index=title_index,
                )
                restarted.append(media_id)
            except PipelineQueueError:
                unavailable.append(media_id)
        if not restarted:
            raise PipelineQueueError(
                "No durable verified rips are available; existing files require verification"
            )
        pipeline_store.set_paused(False)
        return {
            "restarted_count": len(restarted),
            "verification_required_count": len(unavailable),
        }
    except (RipError, PipelineQueueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/pipeline/pause", response_model=PipelineQueueResponse)
def pause_pipeline(
    request: PipelineControlRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    if request.confirm_control is not True:
        raise HTTPException(
            status_code=400, detail="Pipeline pause confirmation is required"
        )
    store.set_paused(True)
    return get_pipeline_items(store)


@router.get("/pipeline/transcode/preview")
def preview_transcode_authorization(
    profile_id: str | None,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    profiles: Annotated[HandBrakeProfileStore, Depends(get_handbrake_profile_store)],
) -> dict[str, object]:
    """Review the exact currently queued transcode set without starting tools."""

    try:
        plan = build_transcode_authorization_plan(
            store,
            profiles,
            get_config_manager().load(),
            profile_id=profile_id,
        )
        return plan.public_dict()
    except (PipelineQueueError, HandBrakeProfileStoreError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _authorization_blocker(_item):
    raise PipelineReviewRequiredError("stage_not_authorized_by_this_batch")


def _run_authorized_transcode_batch(
    store: PipelineQueueStore,
    adapter: TranscodeStageAdapter,
    media_ids: tuple[str, ...],
) -> None:
    dispatcher = DownstreamDispatcher(
        store,
        {
            "identify": _authorization_blocker,
            "transcode": adapter,
            "organize": _authorization_blocker,
        },
    )
    while True:
        item = dispatcher.run_one(
            allowed_stages=("transcode",), allowed_media_ids=media_ids
        )
        if item is not None:
            continue
        selected = [store.get(media_id) for media_id in media_ids]
        if any(
            item.stage == "transcode" and item.state in {"queued", "running"}
            for item in selected
        ):
            time.sleep(1)
            continue
        return


def _run_authorized_organization_batch(
    store: PipelineQueueStore,
    config,
    contract_root: Path,
    media_ids: tuple[str, ...],
) -> None:
    def organize(item):
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
        library_root = (
            config.jellyfin_tv_root
            if payload.get("episode_id")
            else config.jellyfin_movie_root
        )
        if library_root is None:
            raise PipelineQueueError("Jellyfin library root is unavailable")
        return OrganizeStageAdapter(
            library_root=library_root,
            contract_root=contract_root,
            confirm_organize=True,
            deletion_staging_root=config.deletion_staging_root,
        )(item)

    dispatcher = DownstreamDispatcher(
        store,
        {
            "identify": _authorization_blocker,
            "transcode": _authorization_blocker,
            "organize": organize,
        },
    )
    while (
        dispatcher.run_one(allowed_stages=("organize",), allowed_media_ids=media_ids)
        is not None
    ):
        pass


@router.post("/pipeline/transcode/authorize")
def authorize_transcode_batch(
    request: AuthorizeTranscodeRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    profiles: Annotated[HandBrakeProfileStore, Depends(get_handbrake_profile_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict[str, object]:
    """Start exactly one reviewed serialized HandBrake batch into staging."""

    if request.confirm_transcode is not True:
        raise HTTPException(
            status_code=400, detail="Exact transcode confirmation is required"
        )
    try:
        config = get_config_manager().load()
        plan = build_transcode_authorization_plan(
            store, profiles, config, profile_id=request.profile_id
        )
        if (
            plan.plan_sha256 != request.expected_plan_sha256
            or len(plan.media_ids) != request.authorized_item_count
        ):
            raise PipelineQueueError(
                "Queued transcode set or configuration changed; review it again"
            )
        available = {item.profile_id: item.profile for item in profiles.list()}
        default_profile = available[plan.default_profile_id]
        run_root = config.cache_dir.parent / "orchestration" / "handbrake-runs"
        run_root.mkdir(parents=True, exist_ok=True)
        contract_root.mkdir(parents=True, exist_ok=True)
        adapter = TranscodeStageAdapter(
            handbrake=plan.handbrake,
            ffprobe=plan.ffprobe,
            output_root=plan.output_root,
            run_root=run_root,
            contract_root=contract_root,
            profile=default_profile,
            profiles=available,
            profile_override_id=plan.profile_override_id,
            resolution_profile_ids=plan.resolution_profile_ids,
            tv_library_root=config.jellyfin_tv_root,
            movie_library_root=config.jellyfin_movie_root,
        )
        threading.Thread(
            target=_run_authorized_transcode_batch,
            args=(store, adapter, plan.media_ids),
            name=f"transcode-batch-{plan.plan_sha256[:8]}",
            daemon=True,
        ).start()
        return {
            "started": True,
            "authorized_item_count": len(plan.media_ids),
            "plan_sha256": plan.plan_sha256,
            "organization_authorized": False,
        }
    except (PipelineQueueError, HandBrakeProfileStoreError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/pipeline/organization/preview")
def preview_organization_authorization(
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Review exact collision-free Jellyfin placements without moving files."""

    try:
        return build_organization_authorization_plan(
            store, get_config_manager().load()
        ).public_dict()
    except PipelineQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/pipeline/organization/authorize")
def authorize_organization_batch(
    request: AuthorizeOrganizationRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict[str, object]:
    """Move one exact reviewed collision-free batch into configured libraries."""

    if request.confirm_organize is not True:
        raise HTTPException(
            status_code=400, detail="Organization confirmation is required"
        )
    try:
        config = get_config_manager().load()
        plan = build_organization_authorization_plan(store, config)
        if request.expected_plan_sha256 != plan.plan_sha256:
            raise PipelineQueueError("Organization plan changed; review it again")
        if request.authorized_item_count != len(plan.media_ids):
            raise PipelineQueueError(
                "Authorized organization item count does not match"
            )
        if plan.collision_media_ids:
            raise PipelineQueueError(
                "Existing Jellyfin destinations require per-item collision review"
            )
        threading.Thread(
            target=_run_authorized_organization_batch,
            args=(store, config, contract_root, plan.media_ids),
            name=f"organization-batch-{plan.plan_sha256[:8]}",
            daemon=True,
        ).start()
        return {
            "started": True,
            "authorized_item_count": len(plan.media_ids),
            "plan_sha256": plan.plan_sha256,
            "overwrite_authorized": False,
        }
    except (PipelineQueueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/pipeline/resume", response_model=PipelineQueueResponse)
def resume_pipeline(
    request: PipelineControlRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    if request.confirm_control is not True:
        raise HTTPException(
            status_code=400, detail="Pipeline resume confirmation is required"
        )
    store.set_paused(False)
    return get_pipeline_items(store)


@router.post(
    "/pipeline/items/{media_id}/retry",
    response_model=PipelineItemResponse,
)
def retry_pipeline_item(
    media_id: str,
    request: PipelineControlRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    if request.confirm_control is not True:
        raise HTTPException(
            status_code=400, detail="Pipeline retry confirmation is required"
        )
    try:
        return _pipeline_item_response(store.retry(media_id))
    except PipelineQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _collision_destination(payload: dict[str, object], config) -> tuple[Path, Path]:
    relative = Path(str(payload.get("library_relative", "")))
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.lower() != ".mkv"
    ):
        raise PipelineQueueError("Library destination contract is unsafe")
    root = (
        config.jellyfin_tv_root
        if payload.get("episode_id")
        else config.jellyfin_movie_root
    )
    if root is None or not root.is_dir():
        raise PipelineQueueError("Configured Jellyfin library root is unavailable")
    root = root.resolve()
    try:
        relative = add_jellyfin_version_label(
            relative,
            jellyfin_resolution_label(
                payload.get("encoded_height"), payload.get("encoded_field_order")
            ),
        )
    except OrganizationPlanError as exc:
        raise PipelineQueueError("Encoded resolution contract is invalid") from exc
    planned = (root / relative).resolve()
    try:
        planned.relative_to(root)
    except ValueError as exc:
        raise PipelineQueueError("Library destination escapes its root") from exc
    return root, planned


def _dismiss_duplicate_title_lineage(
    store: PipelineQueueStore,
    *,
    resolved_media_id: str,
    disc_fingerprint: object,
    title_index: object,
) -> int:
    """Retire other held copies of one disc title without changing their media."""

    if not isinstance(disc_fingerprint, str) or not isinstance(title_index, int):
        return 0
    siblings = []
    for candidate in store.list_items():
        if candidate.media_id == resolved_media_id or candidate.state not in {
            "failed",
            "review_required",
        }:
            continue
        fingerprint_match = re.search(r"-([0-9a-f]{16})-title-", candidate.media_id)
        title_match = re.search(r"-title-(\d{3})(?:-|$)", candidate.media_id)
        if (
            fingerprint_match is not None
            and fingerprint_match.group(1) == disc_fingerprint
            and title_match is not None
            and int(title_match.group(1)) == title_index
        ):
            siblings.append(candidate.media_id)
    if siblings:
        store.dismiss_items(tuple(siblings))
    return len(siblings)


@router.post(
    "/pipeline/items/{media_id}/resolve-library-collision",
    response_model=PipelineItemResponse,
)
def resolve_pipeline_library_collision(  # noqa: C901
    media_id: str,
    request: ResolveLibraryCollisionRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict[str, object]:
    """Apply one exact destructive choice to one held library collision."""

    if not request.confirm_resolution:
        raise HTTPException(
            status_code=400, detail="Exact collision confirmation is required"
        )
    try:
        item = store.get(media_id)
        if (
            item.state != "review_required"
            or item.review_code != "library_collision"
            or item.artifact.contract_sha256 != request.expected_artifact_sha256
        ):
            raise PipelineQueueError("Held collision changed; review it again")
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
        if request.action == "delete-new":
            if item.stage == "organize":
                source_key, size_key = "encoded_path", "encoded_size_bytes"
            elif item.stage == "transcode":
                source_key, size_key = "source_path", "source_size_bytes"
            else:
                raise PipelineQueueError("This collision has no removable new media")
            source = Path(str(payload[source_key])).resolve()
            expected_size = int(payload[size_key])
            if not source.is_file() or source.stat().st_size != expected_size:
                raise PipelineQueueError("New pipeline media is missing or changed")
            source.unlink()
            updated = store.resolve_review_terminal(
                media_id,
                state="discarded",
                event_type="new_pipeline_media_discarded",
            )
            _dismiss_duplicate_title_lineage(
                store,
                resolved_media_id=media_id,
                disc_fingerprint=payload.get("disc_fingerprint"),
                title_index=payload.get("title_index"),
            )
            return _pipeline_item_response(updated)

        if item.stage == "transcode":
            revised = dict(payload)
            revised["existing_output_policy"] = "replace-after-verification"
            contract = (
                contract_root / f"{media_id}.identify.replace-{uuid4().hex[:12]}.json"
            )
            contract.write_text(
                json.dumps(revised, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            updated = store.resolve_review_with_artifact(
                media_id, build_artifact("identify", contract)
            )
            return _pipeline_item_response(updated)
        if item.stage != "organize":
            raise PipelineQueueError("A verified encode is required for replacement")

        encoded = Path(str(payload["encoded_path"])).resolve()
        encoded_size = int(payload["encoded_size_bytes"])
        if not encoded.is_file() or encoded.stat().st_size != encoded_size:
            raise PipelineQueueError("Verified encode is missing or changed")
        config = get_config_manager().load()
        root, planned = _collision_destination(payload, config)
        episode_id = payload.get("episode_id")
        if isinstance(episode_id, str):
            status, names = inspect_episode_destination(
                planned.parent, planned.name, episode_id
            )
            if status not in {"review-existing-destination", "review-existing-episode"}:
                raise PipelineQueueError(
                    "The reviewed Jellyfin collision no longer exists"
                )
            if len(names) != 1:
                raise PipelineQueueError(
                    "Multiple Jellyfin episode files require manual review"
                )
            existing = (planned.parent / names[0]).resolve()
        else:
            existing = planned
        existing.relative_to(root)
        if not existing.is_file():
            raise PipelineQueueError("The reviewed Jellyfin file no longer exists")
        deletion_root = config.deletion_staging_root
        if deletion_root is None or not deletion_root.is_dir():
            raise PipelineQueueError("Configure an existing staging-for-deletion root")
        backup = (
            deletion_root.resolve() / "library-replacements" / media_id / existing.name
        ).resolve()
        backup.relative_to(deletion_root.resolve())
        if backup.exists() or backup.parent.exists():
            raise PipelineQueueError("Replacement backup destination already exists")
        old_size = existing.stat().st_size
        backup.parent.mkdir(parents=True, exist_ok=False)
        shutil.move(str(existing), str(backup))
        try:
            shutil.move(str(encoded), str(existing))
        except OSError:
            if not existing.exists() and backup.is_file():
                shutil.move(str(backup), str(existing))
            raise
        if not existing.is_file() or existing.stat().st_size != encoded_size:
            raise PipelineQueueError("Replacement verification failed")
        revised = dict(payload)
        revised.update(
            mode="organized-media-contract",
            library_relative=existing.relative_to(root).as_posix(),
            output_size_bytes=encoded_size,
            replaced_library_backup_path=str(backup),
            replaced_library_backup_size_bytes=old_size,
        )
        contract = (
            contract_root / f"{media_id}.organize.replace-{uuid4().hex[:12]}.json"
        )
        contract.write_text(
            json.dumps(revised, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        updated = store.resolve_review_terminal(
            media_id,
            state="completed",
            event_type="library_replacement_completed",
            artifact=build_artifact("organize", contract),
        )
        return _pipeline_item_response(updated)
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        json.JSONDecodeError,
        OrganizationPlanError,
        PipelineQueueError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/pipeline/items/{media_id}/ambiguity-choice",
    response_model=PipelineItemResponse,
)
def choose_pipeline_ambiguity_resolution(
    media_id: str,
    request: AmbiguityChoiceRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Record how one ambiguous title should be resolved; never call a provider."""

    if request.choice == "gemini" and request.confirm_external_fallback is not True:
        raise HTTPException(
            status_code=400,
            detail=(
                "Confirm that selected evidence may be sent to Gemini after local "
                "analysis has been exhausted"
            ),
        )
    code = {
        "gemini": "gemini_evidence_required",
        "manual": "special_feature_manual_assignment_required",
        "hold": "special_feature_evidence_required",
    }[request.choice]
    try:
        return _pipeline_item_response(store.choose_review_path(media_id, code))
    except PipelineQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/pipeline/gemini/execute")
def execute_pipeline_gemini_fallback(  # noqa: C901
    request: GeminiFallbackExecutionRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict[str, object]:
    """Queue bounded local evidence and one catalogue-constrained Gemini batch."""

    if not request.confirm_media_read or not request.confirm_external_transmission:
        raise HTTPException(
            status_code=400,
            detail="Media-read and external-evidence confirmations are required",
        )
    requested = tuple(dict.fromkeys(request.media_ids))
    if len(requested) != len(request.media_ids):
        raise HTTPException(status_code=400, detail="Gemini media IDs must be unique")
    eligible_codes = {
        "gemini_evidence_required",
        "gemini_analysis_failed",
        "gemini_audio_evidence_insufficient",
        "gemini_catalog_unavailable",
        "gemini_provider_failed",
    }
    selected_items = []
    try:
        selected_items = [store.get(media_id) for media_id in requested]
    except PipelineQueueError as exc:
        raise HTTPException(
            status_code=404, detail="Gemini-held item was not found"
        ) from exc
    if any(
        item.state != "review_required"
        or item.stage != "identify"
        or item.review_code not in eligible_codes
        for item in selected_items
    ):
        raise HTTPException(
            status_code=409,
            detail="Selected Gemini item is no longer awaiting a retry; refresh the queue",
        )
    selected = tuple(item.media_id for item in selected_items)
    try:
        for media_id in selected:
            store.choose_review_path(media_id, "gemini_analysis_running")
    except PipelineQueueError as exc:
        raise HTTPException(
            status_code=409,
            detail="Gemini-held queue state changed; refresh and retry the review",
        ) from exc

    def run() -> None:
        try:
            applied = set(
                execute_gemini_fallback(
                    store,
                    selected,
                    get_config_manager().load(),
                    get_engine().asr,
                    contract_root,
                )
            )
            for media_id in set(selected) - applied:
                store.choose_review_path(media_id, "gemini_descriptive_review_required")
        except Exception as exc:
            logger.error("Gemini fallback batch failed safely: {}", type(exc).__name__)
            safe_code = "gemini_provider_failed"
            if isinstance(exc, PipelineQueueError):
                safe_code = {
                    "Local audio evidence was insufficient for Gemini": (
                        "gemini_audio_evidence_insufficient"
                    ),
                    "Reviewed special-feature catalogue is unavailable": (
                        "gemini_catalog_unavailable"
                    ),
                    "Special-feature catalogue ID is unavailable": (
                        "gemini_catalog_unavailable"
                    ),
                }.get(str(exc), "gemini_analysis_failed")
            for media_id in selected:
                try:
                    if store.get(media_id).state == "review_required":
                        store.choose_review_path(media_id, safe_code)
                except PipelineQueueError:
                    pass

    threading.Thread(
        target=run,
        name="gemini-special-feature-fallback",
        daemon=True,
    ).start()
    return {"started": True, "item_count": len(selected)}


@router.post("/jobs/{job_id}/resume", response_model=OrchestrationJobResponse)
def resume_rip_job(
    job_id: str,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
) -> dict[str, object]:
    try:
        return _job_response(store.resume(job_id, idempotency_key=idempotency_key))
    except RipError as exc:
        raise _store_error(exc) from exc


@router.post("/jobs/{job_id}/retry", response_model=OrchestrationJobResponse)
def retry_failed_rip_job(
    job_id: str,
    request: ControlJobRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
) -> dict[str, object]:
    """Queue unfinished titles; a later execute call must use a new run directory."""

    if request.confirm_control is not True:
        raise HTTPException(
            status_code=400, detail="Rip retry confirmation is required"
        )
    try:
        return _job_response(
            store.retry_failed(job_id, idempotency_key=idempotency_key), store
        )
    except RipError as exc:
        raise _store_error(exc) from exc
