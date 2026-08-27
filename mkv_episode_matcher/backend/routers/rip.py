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
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from mkv_episode_matcher.backend.control_access import require_local_control
from mkv_episode_matcher.backend.dependencies import (
    get_catalogue_contribution_store,
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
    request_windows_drive_refresh,
    resume_windows_drive_automatic_discovery,
    windows_drive_automatic_discovery_status,
    windows_drive_refresh_deferred,
)
from mkv_episode_matcher.backend.gemini_fallback import execute_gemini_fallback
from mkv_episode_matcher.backend.identification_dossier import (
    IdentificationDossierStore,
)
from mkv_episode_matcher.backend.legacy_context_repair import (
    upgrade_legacy_disc_context,
)
from mkv_episode_matcher.backend.organization_authorization import (
    build_organization_authorization_plan,
)
from mkv_episode_matcher.backend.rip_runtime import (
    AllDriveDiscoveryDeferredError,
    AllDriveDiscoveryInProgressError,
    OpticalWorkLease,
    RipExecutionRegistry,
)
from mkv_episode_matcher.backend.startup_queue_resume import (
    cancel_startup_queue_resume,
    startup_queue_resume_seconds,
)
from mkv_episode_matcher.backend.transcode_authorization import (
    build_transcode_authorization_plan,
)
from mkv_episode_matcher.backend.unmatched_disc_analysis import (
    GeminiAnalysisError,
    execute_unmatched_disc_analysis,
)
from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.core.credentials import store_credential
from mkv_episode_matcher.core.datetime_compat import UTC
from mkv_episode_matcher.core.environment import load_environment_settings
from mkv_episode_matcher.core.tv_identification_policy import (
    AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION,
    TV_DISC_ANALYSIS_REVIEW_CODES,
)
from mkv_episode_matcher.disc.catalogue_contributions import (
    CatalogueContributionError,
    snapshot_from_inventory,
)
from mkv_episode_matcher.disc.content_policy import (
    infer_release_name_from_disc_label,
    infer_tv_context_from_disc_label,
    parse_tv_disc_label_context,
)
from mkv_episode_matcher.disc.drive_mapping import DriveMappingError
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
from mkv_episode_matcher.disc.orchestration_store import (
    OrchestrationEvent,
    OrchestrationJob,
    OrchestrationStore,
)
from mkv_episode_matcher.disc.preflight import (
    PreflightError,
    parse_disc_inventory,
    parse_drives,
    resolve_makemkv_path,
    targeted_inventory_drive,
    write_inventory_report,
)
from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.disc.rip_dispatcher import BoundRipDispatch, RipDispatcher
from mkv_episode_matcher.disc.rip_execution_adapter import (
    ProductionRipExecutor,
    RipExecutionOptions,
)
from mkv_episode_matcher.disc.rip_manifest import (
    MediaContext,
    build_rip_manifest,
    inventory_fingerprint_from_report,
)
from mkv_episode_matcher.disc.rip_preview import RipPreview, build_rip_preview
from mkv_episode_matcher.disc.ripper import RipError, RipJob, RipResult
from mkv_episode_matcher.disc.ripweaver_catalogue import (
    RipWeaverCatalogueAuthenticationError,
    RipWeaverCatalogueClient,
    RipWeaverCatalogueError,
    RipWeaverCatalogueSupportRequiredError,
)
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
from mkv_episode_matcher.disc.thediscdb import (
    TheDiscDbError,
    TheDiscDbResolution,
    disc_root_from_device_name,
    inferred_content_hint,
    lookup_disc_metadata,
    read_disc_filesystem_identity,
    unique_assignment_season,
)
from mkv_episode_matcher.disc.title_selector import (
    load_title_plan,
    select_pipeline_titles,
    select_recovery_titles,
)
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
    omit_placeholder_episode_title,
)
from mkv_episode_matcher.media.silent_video_review import (
    collect_silent_video_review,
    resolve_tesseract_path,
)
from mkv_episode_matcher.media.special_feature_evidence import (
    SpecialFeatureEvidenceError,
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

_prepared_disc_identity_lock = threading.Lock()

_DASHBOARD_ACTIVE_JOB_STATES = frozenset({
    "authorized",
    "queued",
    "running",
    "pause_requested",
    "paused",
})
_DASHBOARD_ACTIVE_PIPELINE_STATES = frozenset({
    "queued",
    "running",
    "review_required",
    "failed",
})
_DASHBOARD_ATTENTION_PIPELINE_STATES = frozenset({"review_required", "failed"})


def _parse_dashboard_disc_fingerprints(value: str | None) -> tuple[str, ...]:
    """Validate the bounded, path-free disc scope used by routine UI polling."""

    if value is None or not value.strip():
        return ()
    fingerprints = tuple(dict.fromkeys(part.strip() for part in value.split(",")))
    if len(fingerprints) > 16 or any(
        re.fullmatch(r"[0-9a-f]{16}", fingerprint) is None
        for fingerprint in fingerprints
    ):
        raise HTTPException(status_code=422, detail="Dashboard disc scope is invalid")
    return fingerprints


def _job_has_dashboard_disc(job, disc_fingerprints: tuple[str, ...]) -> bool:
    if not disc_fingerprints:
        return False
    preview_jobs = job.preview.get("jobs", [])
    if not isinstance(preview_jobs, list):
        return False
    return any(
        isinstance(item, dict)
        and isinstance(item.get("staging_destination"), str)
        and any(
            fingerprint in PurePosixPath(item["staging_destination"]).parts
            for fingerprint in disc_fingerprints
        )
        for item in preview_jobs
    )


def _filter_dashboard_jobs(
    jobs: tuple,
    *,
    scope: Literal["all", "active", "dashboard"],
    disc_fingerprints: tuple[str, ...],
) -> tuple:
    if scope == "all":
        return jobs
    if scope == "active":
        return tuple(job for job in jobs if job.state in _DASHBOARD_ACTIVE_JOB_STATES)
    return tuple(
        job
        for job in jobs
        if job.state in _DASHBOARD_ACTIVE_JOB_STATES
        or _job_has_dashboard_disc(job, disc_fingerprints)
    )


def _filter_dashboard_pipeline_items(
    items: tuple,
    *,
    scope: Literal["all", "active", "attention", "dashboard"],
    disc_fingerprints: tuple[str, ...],
) -> tuple:
    if scope == "all":
        return items
    if scope == "active":
        return tuple(
            item for item in items if item.state in _DASHBOARD_ACTIVE_PIPELINE_STATES
        )
    if scope == "attention":
        return tuple(
            item for item in items if item.state in _DASHBOARD_ATTENTION_PIPELINE_STATES
        )
    if not disc_fingerprints:
        return tuple(
            item for item in items if item.state in _DASHBOARD_ACTIVE_PIPELINE_STATES
        )
    return tuple(
        item
        for item in items
        if (
            any(fingerprint in item.media_id for fingerprint in disc_fingerprints)
            or _pipeline_item_saved_disc_fingerprint(item) in disc_fingerprints
        )
    )


def _pipeline_item_saved_rip_payload(item) -> dict[str, object] | None:
    """Read the first valid saved rip identity contract for one queue item."""

    artifact = getattr(item, "artifact", None)
    contract_path = getattr(artifact, "contract_path", None)
    if not isinstance(contract_path, Path):
        return None
    candidates = (
        contract_path.with_name(f"{item.media_id}.verified-rip.json"),
        contract_path,
    )
    for candidate in dict.fromkeys(candidates):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        fingerprint = payload.get("disc_fingerprint")
        if isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{16}", fingerprint):
            return payload
    return None


def _pipeline_item_saved_disc_fingerprint(item) -> str | None:
    """Read only the saved path-free disc identity needed for dashboard scope."""

    payload = _pipeline_item_saved_rip_payload(item)
    return str(payload["disc_fingerprint"]) if payload is not None else None


def _payload_has_verified_media_file(payload: dict[str, object]) -> bool:
    """Verify one concrete pipeline media path without trusting stage labels."""

    for path_key, size_key in (
        ("source_path", "source_size_bytes"),
        ("encoded_path", "encoded_size_bytes"),
        ("original_source_path", "original_source_size_bytes"),
        ("archived_source_path", "archived_source_size_bytes"),
    ):
        source_value = payload.get(path_key)
        source_size = payload.get(size_key)
        if not isinstance(source_value, str) or not isinstance(source_size, int):
            continue
        try:
            source = Path(source_value)
            if (
                source_size > 0
                and source.is_file()
                and source.stat().st_size == source_size
            ):
                return True
        except OSError:
            continue
    return False


def _safely_present_pipeline_title_indexes(
    store: PipelineQueueStore,
    disc_fingerprint: str,
) -> frozenset[int]:
    """Return concrete staged, encoded, retained, or organized disc titles."""

    present: set[int] = set()
    for item in store.list_items():
        rip_payload = _pipeline_item_saved_rip_payload(item)
        if (
            rip_payload is None
            or rip_payload.get("disc_fingerprint") != disc_fingerprint
        ):
            continue
        title_index = rip_payload.get("title_index")
        if not isinstance(title_index, int) or isinstance(title_index, bool):
            continue
        if item.stage == "organize" and item.state == "completed":
            present.add(title_index)
            continue
        payloads = [rip_payload]
        try:
            current_payload = json.loads(
                item.artifact.contract_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            current_payload = None
        if isinstance(current_payload, dict) and current_payload != rip_payload:
            payloads.append(current_payload)
        if any(_payload_has_verified_media_file(payload) for payload in payloads):
            present.add(title_index)
    return frozenset(present)


def _dashboard_disc_matching_scopes(
    store: PipelineQueueStore,
    disc_fingerprints: tuple[str, ...],
) -> list[dict[str, object]]:
    """Return the latest path-free classifier scope for visible exact discs."""

    scopes: list[dict[str, object]] = []
    for fingerprint in disc_fingerprints:
        title_indexes = store.disc_matching_scope(fingerprint)
        if title_indexes is None:
            continue
        scopes.append({
            "disc_fingerprint": fingerprint,
            "relevant_title_indexes": list(title_indexes),
        })
    return scopes


def _dashboard_disc_recovery_scopes(
    store: PipelineQueueStore,
    disc_fingerprints: tuple[str, ...],
) -> list[dict[str, object]]:
    """Return substantial-content obligations for visible exact discs."""

    scopes: list[dict[str, object]] = []
    for fingerprint in disc_fingerprints:
        title_indexes = store.disc_recovery_scope(fingerprint)
        if title_indexes is None:
            continue
        scopes.append({
            "disc_fingerprint": fingerprint,
            "required_title_indexes": list(title_indexes),
        })
    return scopes


def _claim_drive_preparation(
    drive_index: int,
    *,
    operation: str = "disc preparation scan",
) -> OpticalWorkLease:
    """Refuse concurrent inventory/planning work for one physical drive."""

    try:
        return get_rip_execution_registry().claim_drive_preparation(
            drive_index,
            operation=operation,
        )
    except RipError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc


def _preview_disc_fingerprint(preview: RipPreview | dict[str, object]) -> str | None:
    jobs = preview.jobs if isinstance(preview, RipPreview) else preview.get("jobs", [])
    for preview_job in jobs:
        destination = (
            preview_job.staging_destination
            if hasattr(preview_job, "staging_destination")
            else preview_job.get("staging_destination")
            if isinstance(preview_job, dict)
            else None
        )
        if not isinstance(destination, str):
            continue
        for part in PurePosixPath(destination.replace("\\", "/")).parts:
            if re.fullmatch(r"[0-9a-f]{16}", part):
                return part
    return None


def _preview_title_indexes(preview: dict[str, object]) -> frozenset[int]:
    jobs = preview.get("jobs", [])
    if not isinstance(jobs, list | tuple):
        return frozenset()
    return frozenset(
        item["title_index"]
        for item in jobs
        if isinstance(item, dict) and isinstance(item.get("title_index"), int)
    )


def _failed_rip_title_indexes(
    store: OrchestrationStore,
    disc_fingerprint: str,
) -> frozenset[int]:
    """Return the acquisition scope of this exact disc's failed attempts.

    Fresh acquisition intentionally selects the complete zero-minimum inventory
    so MakeMKV can stay open for one ``all`` operation.  A known per-title
    failure is a recovery workflow instead: re-running the title classifier
    must retain only the current relevant intersection rather than expanding
    recovery work to every tiny title in a failed whole-disc batch.

    Titles whose estimated size is zero are menu or navigation tracks that
    MakeMKV produces no usable output for.  They are excluded from recovery
    scope so they never appear as missing content requiring a rerip.
    """

    selected: set[int] = set()
    for candidate in store.list_jobs(limit=200):
        if (
            candidate.state != "failed"
            or _preview_disc_fingerprint(candidate.preview) != disc_fingerprint
        ):
            continue
        for item in candidate.preview.get("jobs", []):
            if not isinstance(item, dict):
                continue
            title_index = item.get("title_index")
            if not isinstance(title_index, int):
                continue
            # Exclude 0-byte inventory titles (menu/navigation tracks that
            # MakeMKV never produces usable content for).
            estimated = item.get("estimated_bytes")
            if isinstance(estimated, int | float) and estimated <= 0:
                continue
            selected.add(title_index)
    return frozenset(selected)


def _newer_completed_job_covering(
    candidate: OrchestrationJob,
    jobs: tuple[OrchestrationJob, ...],
    safely_present_title_indexes: frozenset[int],
) -> OrchestrationJob | None:
    """Find a later completed rip that already covers every candidate title."""

    candidate_titles = _preview_title_indexes(candidate.preview)
    if not candidate_titles:
        return None
    return next(
        (
            completed
            for completed in jobs
            if completed.state == "completed"
            and completed.created_at > candidate.created_at
            and candidate_titles <= _preview_title_indexes(completed.preview)
            and candidate_titles <= safely_present_title_indexes
        ),
        None,
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
    metadata_source: str | None = None
    metadata_status: str | None = None
    metadata_matched_title_count: int = 0


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
    prior_library_status: str | None = None


class RipPreviewResponse(BaseModel):
    mode: str
    execution_authorized: bool
    plan_sha256: str
    drives: list[RipPreviewDriveResponse]
    jobs: list[RipPreviewJobResponse]
    skipped_discs: list[dict[str, object]]
    skipped_titles: list[dict[str, object]] = Field(default_factory=list)
    held_titles: list[dict[str, object]] = Field(default_factory=list)
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
    rip_title_summary: dict[str, object] | None = None
    rip_progress_percent: int | None = None
    rip_transfer_mib_s: float | None = None
    rip_progress_scope: str | None = None
    rip_progress_updated_at: str | None = None
    rip_overall_progress_percent: int | None = None
    rip_completed_title_count: int | None = None
    rip_total_title_count: int | None = None
    rip_activity_status: str
    rip_activity_age_seconds: int | None = None
    rip_possibly_stalled: bool = False
    rip_stall_after_seconds: int | None = None
    pipeline_handoff_status: str = "not_configured"
    pipeline_queued_title_count: int | None = None
    pipeline_handoff_pending_title_count: int = 0


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
    error_code: str | None = None
    refresh_in_progress: bool = False
    refresh_deferred: bool = False
    automatic_discovery_paused: bool = False
    automatic_discovery_pause_reason: str | None = None
    automatic_discovery_timeout_count: int = 0
    busy_drive_indexes: list[int] = Field(default_factory=list)
    physical_drive_operations: dict[int, str] = Field(default_factory=dict)
    mapping_plan_sha256: str | None = None
    mapping_summary: dict[str, int] = Field(default_factory=dict)
    retired_mapping_count: int = 0
    automatic_processing_requested: bool = False
    drives: list[dict[str, object]]


class RefreshDrivesRequest(BaseModel):
    confirm_read: bool = False
    timeout_seconds: int = Field(default=300, ge=5, le=300)


class DriveMappingRequest(BaseModel):
    status: str = Field(pattern=r"^(trusted|ignored)$")
    confirm_mapping: bool = False


class DriveMappingDecision(BaseModel):
    mapping_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    status: str = Field(pattern=r"^(trusted|ignored)$")


class DriveMappingPlanRequest(BaseModel):
    expected_mapping_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mappings: list[DriveMappingDecision] = Field(min_length=1, max_length=32)
    retire_absent_trusted: bool = True
    continue_automatic_processing: bool = False
    confirm_mapping: bool = False
    confirm_automatic_processing: bool = False


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
    catalogue_lookup_mode: str = Field(
        default="automatic", pattern="^(automatic|manual)$"
    )
    support_prompt_version: str | None = Field(default=None, max_length=32)
    confirm_read: bool = False
    timeout_seconds: int = Field(default=300, ge=30, le=900)


class ForgetDiscIdentityRequest(BaseModel):
    expected_disc_fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")
    confirm_forget: bool = False
    delete_staged_media: bool = False
    expected_media_plan_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    authorized_media_file_count: int | None = Field(default=None, ge=1, le=9999)
    confirm_delete_staged_media: bool = False


class ForgetDiscMediaPreviewRequest(BaseModel):
    expected_disc_fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")


class PipelineSettingsRequest(BaseModel):
    content_hint: str | None = Field(default=None, pattern="^(tv|movie|extras|mixed)$")
    handbrake_profile_id: str | None = Field(default=None, max_length=80)


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


def _drive_status_response(
    watcher: DriveWatcher, registry: RipExecutionRegistry | None = None
) -> dict[str, object]:
    from mkv_episode_matcher.backend.automatic_rip import automatic_rip_coordinator

    snapshot = watcher.snapshot()
    registry = registry or get_rip_execution_registry()
    busy_drive_indexes = sorted(
        set(automatic_rip_coordinator.active_drive_indexes())
        | set(registry.busy_drive_indexes())
    )
    summary = {
        status: sum(drive.mapping_status == status for drive in snapshot.drives)
        for status in ("trusted", "ignored", "unmapped")
    }
    automatic_discovery = windows_drive_automatic_discovery_status()
    return {
        "watcher_attached": True,
        "refresh_mode": "startup-and-events",
        "status": snapshot.status,
        "refreshed_at": snapshot.refreshed_at,
        "error_type": snapshot.error_type,
        "error_code": snapshot.error_code,
        "refresh_in_progress": watcher.refresh_in_progress(),
        "refresh_deferred": windows_drive_refresh_deferred(),
        "automatic_discovery_paused": automatic_discovery["paused"],
        "automatic_discovery_pause_reason": automatic_discovery["pause_reason"],
        "automatic_discovery_timeout_count": automatic_discovery[
            "consecutive_timeout_count"
        ],
        "busy_drive_indexes": busy_drive_indexes,
        "physical_drive_operations": registry.physical_drive_operations(),
        "mapping_plan_sha256": watcher.mapping_plan_sha256(),
        "mapping_summary": summary,
        "drives": [asdict(drive) for drive in snapshot.drives],
    }


def _safe_drive_refresh_error(exc: PreflightError) -> str:
    message = str(exc).casefold()
    if "executable not found" in message:
        return (
            "MakeMKV could not be found. Repair the MakeMKVCLI path in Settings, "
            "then retry the read-only refresh."
        )
    if "timed out" in message:
        return (
            "MakeMKV drive discovery timed out. The configured CLI starts, but an "
            "optical drive, enclosure, or Windows device query may not be responding. "
            "Close MakeMKV, power-cycle external optical drives, reconnect them one "
            "at a time, then retry the five-minute refresh."
        )
    if "already in progress" in message:
        return (
            "A read-only MakeMKV drive discovery is already running. Wait for the "
            "current refresh to finish; another refresh was not queued."
        )
    if "no optical-drive records" in message:
        return (
            "MakeMKV did not report any optical drives. Confirm the drives appear "
            "in Windows, close any separate MakeMKV process, check the configured "
            "MakeMKVCLI path, then retry."
        )
    return (
        "MakeMKV drive discovery failed. Wait for active disc work to finish, "
        "confirm the MakeMKVCLI path in Settings, then retry."
    )


def _versioned_movie_destination_exists(destination: Path) -> bool:
    if not destination.parent.exists():
        return False
    if not destination.parent.is_dir():
        raise OrganizationPlanError("A planned movie destination is not a directory")
    version_pattern = re.compile(
        rf"(?i)^{re.escape(destination.stem)} - \d{{3,4}}[pi]\.mkv$"
    )
    return any(
        item.is_file() and version_pattern.fullmatch(item.name)
        for item in destination.parent.iterdir()
    )


def _prior_library_status(
    history: dict[str, str | None],
    *,
    tv_library_root: Path | None,
    movie_library_root: Path | None,
) -> str:
    """Inspect names only and classify a durable prior destination."""

    relative = history.get("library_relative")
    episode_id = history.get("episode_id")
    if not isinstance(relative, str) or not relative.strip():
        return "unavailable"
    root = tv_library_root if isinstance(episode_id, str) else movie_library_root
    if root is None:
        return "unavailable"
    relative_parts = PurePosixPath(relative.replace("\\", "/"))
    if (
        relative_parts.is_absolute()
        or ".." in relative_parts.parts
        or relative_parts.suffix.casefold() != ".mkv"
    ):
        return "unavailable"
    try:
        resolved_root = root.resolve()
        if not resolved_root.is_dir():
            return "unavailable"
        destination = (resolved_root / Path(*relative_parts.parts)).resolve()
        destination.relative_to(resolved_root)
        if destination.is_file():
            return "present"
        if destination.exists():
            return "unavailable"
        if isinstance(episode_id, str):
            status, _conflicts = inspect_episode_destination(
                destination.parent, destination.name, episode_id
            )
            return "present" if status != "proposed" else "missing"
        return (
            "present" if _versioned_movie_destination_exists(destination) else "missing"
        )
    except (OSError, ValueError, OrganizationPlanError):
        return "unavailable"


def _attach_prior_outcomes(
    preview: RipPreview,
    store: PipelineQueueStore,
    *,
    inspect_library: bool = False,
    tv_library_root: Path | None = None,
    movie_library_root: Path | None = None,
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
        library_status = (
            _prior_library_status(
                history,
                tv_library_root=tv_library_root,
                movie_library_root=movie_library_root,
            )
            if history and inspect_library
            else None
        )
        collision_status = job.collision_status
        if library_status == "present":
            collision_status = "library-exists"
        elif library_status == "unavailable":
            collision_status = "library-check-unavailable"
        enriched.append(
            replace(
                job,
                collision_status=collision_status,
                prior_outcome_name=history.get("outcome_name") if history else None,
                prior_library_relative=(
                    history.get("library_relative") if history else None
                ),
                prior_episode_id=history.get("episode_id") if history else None,
                prior_library_status=library_status,
            )
        )
    collision_count = sum(
        item.collision_status not in {"clear", "not-checked"} for item in enriched
    )
    return replace(
        preview,
        jobs=tuple(enriched),
        collision_count=collision_count,
        requires_review=bool(preview.requires_review or collision_count),
    )


def _build_prepared_preview(
    report_path: Path,
    context: MediaContext,
    *,
    output_root: Path,
    pipeline_store: PipelineQueueStore,
    tv_library_root: Path | None,
    movie_library_root: Path | None,
    disc_fingerprint: str,
    skipped_titles: tuple[dict[str, object], ...],
) -> tuple[RipPreview, MediaContext]:
    """Build one review and exclude currently present Jellyfin outcomes when asked."""

    def build(selected_context: MediaContext) -> RipPreview:
        candidate = build_rip_preview(
            [report_path],
            {selected_context.disc_id: selected_context},
            output_root=output_root,
        )
        return _attach_prior_outcomes(
            candidate,
            pipeline_store,
            inspect_library=True,
            tv_library_root=tv_library_root,
            movie_library_root=movie_library_root,
        )

    preview = build(context)
    held_titles = tuple(
        {
            "disc_fingerprint": disc_fingerprint,
            "title_index": job.title_index,
            "reason": "existing_jellyfin_destination",
            "outcome_name": job.prior_outcome_name,
            "library_relative": job.prior_library_relative,
            "episode_id": job.prior_episode_id,
        }
        for job in preview.jobs
        if job.prior_library_status == "present"
    )
    if (
        context.existing_output_policy == "missing-only"
        and held_titles
        and len(held_titles) < len(preview.jobs)
    ):
        context = replace(
            context,
            selected_title_indexes=tuple(
                job.title_index
                for job in preview.jobs
                if job.prior_library_status != "present"
            ),
        )
        preview = build(context)
    return (
        replace(
            preview,
            skipped_titles=skipped_titles,
            held_titles=held_titles,
        ),
        context,
    )


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
    registry: Annotated[RipExecutionRegistry, Depends(get_rip_execution_registry)],
) -> dict[str, object]:
    """Run one explicitly confirmed MakeMKV drive-enumeration info call."""

    if request.confirm_read is not True:
        raise HTTPException(
            status_code=400, detail="Read-only drive refresh confirmation is required"
        )
    try:
        discovery_lease = registry.claim_all_drive_discovery()
    except AllDriveDiscoveryDeferredError:
        if not request_windows_drive_refresh():
            raise HTTPException(
                status_code=409,
                detail=(
                    "Active optical work blocked all-drive discovery; no MakeMKV "
                    "refresh was started"
                ),
            ) from None
        response = _drive_status_response(watcher, registry)
        response["refresh_deferred"] = True
        return response
    except AllDriveDiscoveryInProgressError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "A read-only MakeMKV drive discovery is already running. Wait for "
                "the current refresh to finish; another refresh was not queued."
            ),
        ) from exc
    try:
        config = get_config_manager().load()
        executable = resolve_makemkv_path(config.makemkv_path)
        resume_windows_drive_automatic_discovery()
        watcher.refresh(executable, timeout_seconds=request.timeout_seconds)
    except PreflightError as exc:
        logger.warning("Read-only drive refresh failed safely: {}", type(exc).__name__)
        raise HTTPException(
            status_code=409,
            detail=_safe_drive_refresh_error(exc),
        ) from exc
    finally:
        discovery_lease.release()
    return _drive_status_response(watcher, registry)


@router.put("/drives/mappings/{mapping_id}", response_model=DriveStatusResponse)
def update_drive_mapping(
    mapping_id: str,
    request: DriveMappingRequest,
    watcher: Annotated[DriveWatcher, Depends(get_drive_watcher)],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
) -> dict[str, object]:
    """Trust or ignore one hashed Windows device without reading its media."""

    if request.confirm_mapping is not True:
        raise HTTPException(
            status_code=400,
            detail="Explicit optical-drive mapping confirmation is required",
        )
    if not re.fullmatch(r"[0-9a-f]{16}", mapping_id):
        raise HTTPException(
            status_code=400, detail="Optical-drive mapping ID is invalid"
        )
    selected = next(
        (
            drive
            for drive in watcher.snapshot().drives
            if drive.mapping_id == mapping_id
        ),
        None,
    )
    if selected is None:
        raise HTTPException(
            status_code=409, detail="The optical device is no longer present"
        )
    if request.status == "ignored":
        active_jobs = [
            job
            for job in store.list_jobs()
            if job.state in {"authorized", "queued", "running", "pause_requested"}
            and any(
                drive.get("drive_index") == selected.drive_index
                for drive in job.preview.get("drives", [])
                if isinstance(drive, dict)
            )
        ]
        if active_jobs:
            raise HTTPException(
                status_code=409,
                detail="Finish or return this drive's active rip work to review before ignoring it",
            )
    try:
        watcher.set_mapping_status(mapping_id, request.status)
    except DriveMappingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _drive_status_response(watcher)


@router.put("/drives/mappings", response_model=DriveStatusResponse)
def save_drive_mapping_plan(
    request: DriveMappingPlanRequest,
    watcher: Annotated[DriveWatcher, Depends(get_drive_watcher)],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
) -> dict[str, object]:
    """Apply one exact reviewed device set and optionally resume automation."""

    if request.confirm_mapping is not True:
        raise HTTPException(
            status_code=400,
            detail="Explicit optical-drive mapping confirmation is required",
        )
    mapping_ids = [item.mapping_id for item in request.mappings]
    if len(set(mapping_ids)) != len(mapping_ids):
        raise HTTPException(
            status_code=400,
            detail="Each optical-device mapping ID must appear exactly once",
        )
    decisions = {item.mapping_id: item.status for item in request.mappings}
    ignored_indexes = {
        drive.drive_index
        for drive in watcher.snapshot().drives
        if drive.mapping_id in decisions and decisions[drive.mapping_id] == "ignored"
    }
    if ignored_indexes:
        active_jobs = [
            job
            for job in store.list_jobs()
            if job.state in {"authorized", "queued", "running", "pause_requested"}
            and any(
                drive.get("drive_index") in ignored_indexes
                for drive in job.preview.get("drives", [])
                if isinstance(drive, dict)
            )
        ]
        if active_jobs:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Finish or return active rip work to review before ignoring "
                    "one of its optical drives"
                ),
            )
    if request.continue_automatic_processing:
        config = get_config_manager().load()
        if not config.automatic_processing_enabled:
            raise HTTPException(
                status_code=409,
                detail="Enable automatic processing before continuing loaded discs",
            )
        if request.confirm_automatic_processing is not True:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Explicit confirmation is required before loaded discs enter "
                    "automatic preparation and ripping"
                ),
            )
    try:
        snapshot, retired_count = watcher.apply_mapping_plan(
            request.expected_mapping_plan_sha256,
            decisions,
            retire_absent_trusted=request.retire_absent_trusted,
        )
    except DriveMappingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    automatic_requested = False
    if request.continue_automatic_processing:
        from mkv_episode_matcher.backend.automatic_rip import observe_automatic_drives

        observe_automatic_drives(snapshot, enabled=True)
        automatic_requested = True
    logger.info(
        "Saved optical-drive map: {} trusted, {} ignored, {} retired; "
        "automatic continuation={}",
        sum(status == "trusted" for status in decisions.values()),
        sum(status == "ignored" for status in decisions.values()),
        retired_count,
        automatic_requested,
    )
    response = _drive_status_response(watcher)
    response["retired_mapping_count"] = retired_count
    response["automatic_processing_requested"] = automatic_requested
    return response


@router.post("/drives/{drive_index}/eject")
def eject_drive(  # noqa: C901
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
    selected_drive = next(
        (
            drive
            for drive in watcher.snapshot().drives
            if drive.drive_index == drive_index
        ),
        None,
    )
    if watcher.mapping_required() and (
        selected_drive is None or selected_drive.mapping_status != "trusted"
    ):
        raise HTTPException(
            status_code=409,
            detail="Map this Windows optical device as trusted before controlling its tray",
        )
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
        with registry.claim_drive_preparation(
            drive_index,
            operation="manual eject check",
        ):
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
            watcher.record_successful_eject(drive_index)
    except RipError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    registry: RipExecutionRegistry,
    watcher: DriveWatcher,
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
            with registry.claim_drive_preparation(
                drive_index,
                operation="automatic eject check",
            ):
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
                watcher.record_successful_eject(drive_index)
            logger.info("Automatically ejected one successfully completed rip drive")
        except (DiscEjectError, PreflightError, RipError, OSError) as exc:
            logger.warning("Automatic disc eject failed safely: {}", type(exc).__name__)


def _existing_rip_recovery_media_id(candidate) -> str:
    """Return a stable queue-safe ID without changing the recovered basename."""

    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(candidate.basename).stem)
    stem = stem.strip("._-")[:96] or "recovered-mkv"
    return f"{stem}-recovery-{candidate.candidate_id[:16]}"


def _auto_admit_staged_disc_if_complete(  # noqa: C901 - auto-admit verification isolation
    manifest_jobs,
    disc_fingerprint: str,
    output_root: Path,
    recovery_scope: frozenset[int],
    safely_present_title_indexes: frozenset[int],
    preview: RipPreview,
    report_path: Path,
    disc_id: str,
    context: MediaContext,
    public_store: OrchestrationStore,
    private_store: PrivateBindingStore,
    pipeline_store: PipelineQueueStore,
    contract_root: Path,
    idempotency_key: str,
) -> OrchestrationJob | None:
    """Auto-admit verified staging files when all relevant titles are already ripped."""

    if not output_root.is_dir():
        return None
    staged_plan = discover_existing_rips(output_root, manifest_jobs)
    if not staged_plan.candidates:
        return None
    required_title_indexes = (
        recovery_scope
        if recovery_scope
        else frozenset(job.title_index for job in manifest_jobs)
    )
    library_title_indexes = frozenset(
        item.title_index
        for item in preview.jobs
        if item.prior_library_status == "present"
    )
    skipped_title_indexes = frozenset(
        item["title_index"]
        for item in preview.skipped_titles
        if isinstance(item.get("title_index"), int)
    )
    needed_title_indexes = required_title_indexes - (
        library_title_indexes | skipped_title_indexes | safely_present_title_indexes
    )
    if not needed_title_indexes:
        return None
    candidate_by_title = {c.title_index: c for c in staged_plan.candidates}
    if not needed_title_indexes <= set(candidate_by_title):
        return None
    selected_candidates = tuple(
        candidate_by_title[title_index] for title_index in sorted(needed_title_indexes)
    )
    recovered = recovered_jobs(
        tuple(job for job in manifest_jobs if job.title_index in needed_title_indexes),
        replace(staged_plan, candidates=selected_candidates),
    )
    inspector = get_ffprobe_inspector()
    config = get_config_manager().load()
    if not config.ffprobe_path:
        return None
    executable = resolve_ffprobe_path(config.ffprobe_path)
    verified_jobs = []
    verified_candidates = []
    results = []
    now = datetime.now(UTC).isoformat()
    candidates_by_job = {c.job_id: c for c in selected_candidates}
    for job in recovered:
        candidate = candidates_by_job.get(job.job_id)
        if candidate is None:
            return None
        source = output_root / candidate.relative_parent / candidate.basename
        try:
            inspection = inspector(executable, source, timeout_seconds=30)
            valid = (
                inspection.media.duration_seconds > 0
                and source.stat().st_size == candidate.size_bytes
            )
        except (FFprobeError, OSError):
            valid = False
        if not valid:
            return None
        verified_jobs.append(job)
        verified_candidates.append(candidate)
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
    if len(verified_jobs) != len(needed_title_indexes):
        return None
    contract_root.mkdir(parents=True, exist_ok=True)
    enqueue_verified_rip_results(
        pipeline_store,
        jobs=tuple(verified_jobs),
        results=results,
        output_root=output_root,
        contract_root=contract_root,
        media_contexts={disc_id: context},
        identity_overrides={
            job.job_id: (disc_fingerprint, job.title_index) for job in verified_jobs
        },
        repair_recovered_identity=True,
        media_id_overrides={
            job.job_id: _existing_rip_recovery_media_id(candidate)
            for job, candidate in zip(verified_jobs, verified_candidates, strict=True)
        },
    )
    admit_preview = (
        replace(preview, requires_review=False) if preview.requires_review else preview
    )
    job = public_store.create_job(admit_preview, idempotency_key=idempotency_key)
    public_store.authorize(
        job.job_id,
        expected_plan_sha256=job.plan_sha256,
        idempotency_key=f"auth-{idempotency_key}",
    )
    public_store.queue(job.job_id, idempotency_key=f"queue-{idempotency_key}")
    public_store.claim_for_dispatch(
        job.job_id, idempotency_key=f"claim-{idempotency_key}"
    )
    completed_job = public_store.complete(
        job.job_id,
        idempotency_key=f"comp-{idempotency_key}",
        completed_count=len(verified_jobs),
        pipeline_queued_count=len(verified_jobs),
    )
    private_store.bind(
        job_id=job.job_id,
        plan_sha256=job.plan_sha256,
        report_paths=[report_path],
        output_root=output_root,
        media_contexts={disc_id: context},
    )
    logger.info(
        "Automatically admitted {} verified staged MKVs for completed disc {}",
        len(verified_jobs),
        disc_fingerprint,
    )
    return completed_job


@router.post(
    "/drives/prepare-pipeline",
    response_model=OrchestrationJobResponse,
)
def prepare_drive_pipeline(  # noqa: C901
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
    if request.catalogue_lookup_mode == "manual":
        if not request.support_prompt_version:
            raise HTTPException(
                status_code=400,
                detail="Manual catalogue lookup requires the displayed support prompt",
            )
    elif request.support_prompt_version is not None:
        raise HTTPException(
            status_code=400,
            detail="Automatic catalogue lookup cannot acknowledge a support prompt",
        )
    snapshot = watcher.snapshot()
    selected = next(
        (
            drive
            for drive in snapshot.drives
            if drive.drive_index == request.drive_index
            and drive.has_disc
            and drive.mapping_status == "trusted"
            and drive.makemkv_confirmed
        ),
        None,
    )
    if selected is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "The selected device is not a trusted mapped, MakeMKV-confirmed "
                "optical drive with a detected disc"
            ),
        )

    config = get_config_manager().load()
    if config.rip_output_root is None or not config.rip_output_root.is_dir():
        raise HTTPException(
            status_code=409,
            detail="Configure an existing MakeMKV rip staging root before preparing a pipeline",
        )
    preparation_lock = _claim_drive_preparation(request.drive_index)
    try:
        executable = resolve_makemkv_path(config.makemkv_path)
        result = inventory_runner(
            executable,
            f"disc:{request.drive_index}",
            minimum_length=0,
            timeout_seconds=request.timeout_seconds,
        )
        matching_drive = targeted_inventory_drive(
            result.stdout,
            requested_index=request.drive_index,
            cached_device_name=watcher.device_name(request.drive_index),
            cached_drive_name=selected.display_name,
            cached_disc_name=selected.disc_label,
        )
        if matching_drive is None or not matching_drive.has_disc:
            raise PreflightError("Selected disc disappeared during inventory")
        inventory = parse_disc_inventory(result, matching_drive)
        if not inventory.titles:
            raise PreflightError("MakeMKV returned no titles for the selected disc")

        catalogue_enabled = bool(getattr(config, "ripweaver_catalogue_enabled", False))
        thediscdb_enabled = bool(getattr(config, "thediscdb_lookup_enabled", False))
        disc_resolution = TheDiscDbResolution(status="disabled")
        catalogue_help_assignments: tuple[dict[str, object], ...] = ()
        metadata_source: str | None = None
        catalogue_identity = None
        disc_root = disc_root_from_device_name(matching_drive.device_name)
        if catalogue_enabled:
            metadata_source = "ripweaver-catalogue"
            try:
                identity = read_disc_filesystem_identity(disc_root)
                catalogue_identity = identity
                catalogue_client = RipWeaverCatalogueClient(
                    base_url=config.ripweaver_catalogue_url
                )
                catalogue_token = load_environment_settings().ripweaver_catalogue_token
                if not catalogue_token:
                    registration = catalogue_client.register()
                    store_credential("ripweaver-catalogue", registration.access_token)
                    catalogue_token = registration.access_token
                elif not catalogue_client.capabilities().compatible:
                    raise RipWeaverCatalogueError(
                        "RipWeaver Catalogue protocol is not compatible with this desktop"
                    )
                catalogue_lookup = catalogue_client.lookup(
                    identity.content_hash,
                    inventory,
                    token=catalogue_token,
                    idempotency_key=(
                        "disc-lookup-"
                        + hashlib.sha256(
                            f"{idempotency_key}:{identity.content_hash}".encode()
                        ).hexdigest()
                    ),
                    mode=request.catalogue_lookup_mode,
                    support_prompt_version=request.support_prompt_version,
                )
                disc_resolution = (
                    catalogue_lookup.resolution
                    if catalogue_lookup is not None
                    else TheDiscDbResolution(status="not-found")
                )
                if catalogue_lookup is None or (
                    catalogue_lookup.resolution.unmatched_title_indexes
                ):
                    try:
                        help_resolution = catalogue_client.help(
                            identity.content_hash,
                            inventory,
                            token=catalogue_token,
                        )
                        if help_resolution is not None:
                            catalogue_help_assignments = (
                                help_resolution.episode_assignments
                            )
                    except RipWeaverCatalogueError as exc:
                        logger.warning(
                            "Catalogue candidate help skipped safely: {}",
                            type(exc).__name__,
                        )
            except RipWeaverCatalogueSupportRequiredError:
                disc_resolution = TheDiscDbResolution(status="support-required")
            except RipWeaverCatalogueAuthenticationError as exc:
                logger.warning(
                    "RipWeaver Catalogue authentication skipped safely: {}",
                    type(exc).__name__,
                )
                disc_resolution = TheDiscDbResolution(status="unavailable")
            except (RipWeaverCatalogueError, OSError) as exc:
                logger.warning(
                    "RipWeaver Catalogue enrichment skipped safely: {}",
                    type(exc).__name__,
                )
                disc_resolution = TheDiscDbResolution(status="unavailable")

        use_thediscdb_fallback = thediscdb_enabled and (
            not catalogue_enabled
            or disc_resolution.status in {"not-found", "unavailable"}
        )
        if use_thediscdb_fallback:
            metadata_source = "thediscdb"
            try:
                disc_resolution = lookup_disc_metadata(
                    disc_root,
                    inventory,
                    timeout_seconds=min(request.timeout_seconds, 20),
                )
            except TheDiscDbError as exc:
                logger.warning(
                    "TheDiscDB disc enrichment skipped safely: {}", type(exc).__name__
                )
                disc_resolution = TheDiscDbResolution(status="unavailable")

        report_dir = config.cache_dir.parent / "preflight" / "web" / uuid4().hex
        report_path, _robot_path = write_inventory_report(
            report_dir, inventory, result, minimum_length_seconds=0
        )
        structured_tv_context = parse_tv_disc_label_context(matching_drive.disc_name)
        explicit_tv_context = (
            (structured_tv_context.series_hint, structured_tv_context.season)
            if structured_tv_context
            else infer_tv_context_from_disc_label(matching_drive.disc_name)
        )
        release_name = infer_release_name_from_disc_label(matching_drive.disc_name)
        disc_id = "disc-01"
        episode_plan = load_title_plan(report_path, report_id=disc_id)
        trusted_discdb_match = disc_resolution.status == "matched"
        discdb_episode_assignments = (
            disc_resolution.episode_assignments if trusted_discdb_match else ()
        )
        use_special_features = request.content_hint in {"extras", "mixed"} or (
            request.content_hint is None and not discdb_episode_assignments
        )
        effective_content_hint = (
            "tv"
            if explicit_tv_context and request.content_hint is None
            else request.content_hint
        )
        planned_titles = select_pipeline_titles(
            episode_plan,
            effective_content_hint,
        )
        selected_title_indexes = tuple(
            decision.title.index for decision in planned_titles
        )
        recovery_title_indexes = tuple(
            decision.title.index
            for decision in select_recovery_titles(
                episode_plan,
                effective_content_hint,
            )
        )
        downstream_skip_title_indexes = (
            tuple(
                sorted(
                    decision.title.index
                    for decision in episode_plan.decisions
                    if decision.classification in {"extra", "combined"}
                )
            )
            if explicit_tv_context is not None
            and request.content_hint not in {"movie", "extras", "mixed"}
            else ()
        )
        if request.content_hint not in {"movie", "extras"}:
            selected_title_indexes = tuple(
                sorted(
                    set(selected_title_indexes)
                    | {
                        int(assignment["title_index"])
                        for assignment in discdb_episode_assignments
                    }
                )
            )
            recovery_title_indexes = tuple(
                sorted(
                    set(recovery_title_indexes)
                    | {
                        int(assignment["title_index"])
                        for assignment in discdb_episode_assignments
                    }
                )
            )
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
                    item.title.index
                    for item in select_pipeline_titles(episode_plan, "tv")
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
                recovery_title_indexes = selected_title_indexes
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
        disc_fingerprint = inventory_fingerprint_from_report(report_path)
        # Acquisition may expand to every zero-minimum MakeMKV title below,
        # but disc-aware episode reasoning must retain the classifier-derived
        # relevant scope calculated above.
        pipeline_store.remember_disc_matching_scope(
            disc_fingerprint, tuple(sorted(set(selected_title_indexes)))
        )
        pipeline_store.remember_disc_recovery_scope(
            disc_fingerprint, tuple(sorted(set(recovery_title_indexes)))
        )
        failed_title_indexes = _failed_rip_title_indexes(
            public_store,
            disc_fingerprint,
        )
        # Normal fresh acquisition mirrors MakeMKV's per-drive GUI mode:
        # authorize every title in the zero-minimum inventory so the executor
        # can keep one ``mkv ... all`` process open for this disc.  A known
        # failed attempt is different: preserve the classifier-selected
        # relevant set calculated above so neither legacy per-title recovery
        # nor a partial whole-disc batch is expanded to tiny/non-content titles.
        if failed_title_indexes:
            selected_title_indexes = tuple(
                title_index
                for title_index in recovery_title_indexes
                if title_index in failed_title_indexes
            )
            if not selected_title_indexes:
                raise RipError(
                    "The failed rip no longer contains a verifiable relevant title"
                )
        else:
            selected_title_indexes = tuple(
                sorted(title.index for title in inventory.titles)
            )
        dispositions = pipeline_store.title_dispositions(disc_fingerprint)
        skipped_title_dispositions = tuple(
            {
                "disc_fingerprint": disc_fingerprint,
                "title_index": title_index,
                "reason": dispositions[title_index]["reason"],
            }
            for title_index in selected_title_indexes
            if dispositions.get(title_index, {}).get("disposition") == "skip"
        )
        selected_title_indexes = tuple(
            title_index
            for title_index in selected_title_indexes
            if dispositions.get(title_index, {}).get("disposition") != "skip"
        )
        if not selected_title_indexes:
            raise RipError(
                "Every selected title is saved as a future-rip skip. Restore a "
                "title or forget this disc's saved identity before preparing it again."
            )
        if (
            getattr(config, "ripweaver_catalogue_contributions_enabled", False)
            and catalogue_identity is not None
        ):
            try:
                get_catalogue_contribution_store().record_snapshot(
                    snapshot_from_inventory(
                        content_hash=catalogue_identity.content_hash,
                        disc_fingerprint=disc_fingerprint,
                        media_type=(
                            "dvd"
                            if catalogue_identity.format.casefold() == "dvd"
                            else "bluray"
                        ),
                        release_name=release_name,
                        inventory=inventory,
                        selected_title_indexes=selected_title_indexes,
                    )
                )
            except CatalogueContributionError as exc:
                logger.warning(
                    "Catalogue contribution snapshot skipped safely: {}",
                    type(exc).__name__,
                )
        context = MediaContext(
            disc_id=disc_id,
            series_name=(
                disc_resolution.media_title
                if trusted_discdb_match and disc_resolution.media_title
                else explicit_tv_context[0]
                if explicit_tv_context
                else release_name or "Unmatched"
            ),
            season=(
                unique_assignment_season(discdb_episode_assignments)
                if discdb_episode_assignments
                else explicit_tv_context[1]
                if explicit_tv_context
                else None
            ),
            disc_number=(
                structured_tv_context.disc_number
                if structured_tv_context is not None
                else None
            ),
            tmdb_id=(disc_resolution.tmdb_id if trusted_discdb_match else None),
            content_hint=request.content_hint
            or (
                inferred_content_hint(disc_resolution.media_type)
                if trusted_discdb_match
                else None
            )
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
            downstream_skip_title_indexes=downstream_skip_title_indexes,
            special_feature_catalog_id=catalog_id,
            special_feature_release_id=release_id,
            special_feature_library_title=library_title,
            special_feature_library_year=library_year,
            special_feature_assignments=assignments,
            episode_assignments=discdb_episode_assignments,
            catalogue_help_assignments=catalogue_help_assignments,
            disc_metadata_source=metadata_source,
            disc_metadata_status=(disc_resolution.status if metadata_source else None),
            disc_metadata_matched_title_count=(
                len(disc_resolution.matched_title_indexes)
                if trusted_discdb_match
                else 0
            ),
            existing_output_policy=(
                "missing-only"
                if request.library_policy == "missing-only"
                else "preserve"
            ),
        )
        preview, context = _build_prepared_preview(
            report_path,
            context,
            output_root=config.rip_output_root,
            pipeline_store=pipeline_store,
            tv_library_root=getattr(config, "jellyfin_tv_root", None),
            movie_library_root=getattr(config, "jellyfin_movie_root", None),
            disc_fingerprint=disc_fingerprint,
            skipped_titles=skipped_title_dispositions,
        )
        preview_fingerprint = _preview_disc_fingerprint(preview)
        if preview_fingerprint != disc_fingerprint:
            raise RipError("Prepared disc identity is unavailable")
        try:
            repaired_context_items = upgrade_legacy_disc_context(
                pipeline_store,
                disc_fingerprint=disc_fingerprint,
                fresh_context=context,
                contract_root=get_pipeline_contract_root(),
            )
            if repaired_context_items:
                logger.info(
                    "Repaired legacy season context for {} verified pipeline titles",
                    len(repaired_context_items),
                )
        except PipelineQueueError as exc:
            logger.warning(
                "Legacy season-context repair stopped safely: {}",
                type(exc).__name__,
            )
        with _prepared_disc_identity_lock:
            reusable_states = {
                "awaiting_review",
                "authorized",
                "queued",
                "running",
                "pause_requested",
                "paused",
                "completed",
            }
            matching_jobs = tuple(
                candidate
                for candidate in public_store.list_jobs(limit=200)
                if candidate.state in reusable_states
                and _preview_disc_fingerprint(candidate.preview) == disc_fingerprint
            )
            drive_matching_jobs = tuple(
                candidate
                for candidate in matching_jobs
                if any(
                    isinstance(item, dict)
                    and item.get("drive_index") == request.drive_index
                    for item in candidate.preview.get("drives", [])
                )
            )
            prepared_title_indexes = _preview_title_indexes(asdict(preview))
            prepared_library_title_indexes = frozenset(
                item.title_index
                for item in preview.jobs
                if item.prior_library_status == "present"
            )
            skipped_title_indexes = frozenset(
                item["title_index"]
                for item in skipped_title_dispositions
                if isinstance(item.get("title_index"), int)
            )
            matching_scope = frozenset(
                pipeline_store.disc_matching_scope(disc_fingerprint) or ()
            )
            recovery_scope = frozenset(
                pipeline_store.disc_recovery_scope(disc_fingerprint) or matching_scope
            )
            safely_present_title_indexes = _safely_present_pipeline_title_indexes(
                pipeline_store,
                disc_fingerprint,
            )
            completion_required_title_indexes = (
                recovery_scope if recovery_scope else prepared_title_indexes
            )
            # A completed orchestration preview is only a plan.  It may contain
            # titles omitted from a narrowed recovery handoff, so completion
            # requires concrete staged/organized/library evidence instead.
            completed_history_covers_prepared = completion_required_title_indexes <= (
                prepared_library_title_indexes
                | skipped_title_indexes
                | safely_present_title_indexes
            )
            superseded_job_ids: set[str] = set()
            for candidate in drive_matching_jobs:
                if (
                    candidate.state not in {"authorized", "queued", "paused"}
                    or candidate.executor_attached
                ):
                    continue
                completed = _newer_completed_job_covering(
                    candidate,
                    drive_matching_jobs,
                    safely_present_title_indexes,
                )
                if completed is None:
                    continue
                public_store.cancel(
                    candidate.job_id,
                    idempotency_key=f"superseded-{candidate.job_id}",
                )
                superseded_job_ids.add(candidate.job_id)
            drive_matching_jobs = tuple(
                candidate
                for candidate in drive_matching_jobs
                if candidate.job_id not in superseded_job_ids
            )
            # A newer completed rip can safely retire an older duplicate queue
            # entry. A genuinely newer queued recovery still wins over older
            # completed history so startup observation can attach its executor.
            existing = next(
                (
                    candidate
                    for state in (
                        "running",
                        "pause_requested",
                        "queued",
                        "authorized",
                        "paused",
                        "completed",
                    )
                    for candidate in drive_matching_jobs
                    if candidate.state == state
                    and (state != "completed" or completed_history_covers_prepared)
                ),
                None,
            )
            if existing is not None:
                watcher.bind_current_job(
                    request.drive_index, existing.job_id, disc_fingerprint
                )
                return _job_response(existing, public_store)
            manifest = build_rip_manifest([report_path], {disc_id: context})
            contract_root = get_pipeline_contract_root()
            auto_admitted = _auto_admit_staged_disc_if_complete(
                manifest_jobs=manifest.jobs,
                disc_fingerprint=disc_fingerprint,
                output_root=config.rip_output_root,
                recovery_scope=recovery_scope,
                safely_present_title_indexes=safely_present_title_indexes,
                preview=preview,
                report_path=report_path,
                disc_id=disc_id,
                context=context,
                public_store=public_store,
                private_store=private_store,
                pipeline_store=pipeline_store,
                contract_root=contract_root,
                idempotency_key=idempotency_key,
            )
            if auto_admitted is not None:
                watcher.bind_current_job(
                    request.drive_index, auto_admitted.job_id, disc_fingerprint
                )
                return _job_response(auto_admitted, public_store)
            stale_review = next(
                (
                    candidate
                    for candidate in matching_jobs
                    if candidate.state == "awaiting_review"
                    and (
                        not failed_title_indexes
                        or _preview_title_indexes(candidate.preview)
                        == prepared_title_indexes
                    )
                ),
                None,
            )
            if stale_review is not None:
                stale_binding = private_store.get(stale_review.job_id)
                if len(stale_binding.media_contexts) != 1:
                    raise RipError("The stale disc review cannot be rebound safely")
                stale_context = next(iter(stale_binding.media_contexts.values()))
                stale_drive_indexes = {
                    item.get("drive_index")
                    for item in stale_review.preview.get("drives", [])
                    if isinstance(item, dict)
                }
                source_context = (
                    context
                    if request.drive_index in stale_drive_indexes
                    else stale_context
                )
                selected_indexes = source_context.selected_title_indexes
                available_indexes = {title.index for title in inventory.titles}
                if (
                    selected_indexes is not None
                    and not set(selected_indexes) <= available_indexes
                ):
                    raise RipError(
                        "The fresh disc inventory no longer contains every continuation title"
                    )
                rebound_context = replace(
                    source_context,
                    disc_id=disc_id,
                    staging_attempt=f"attempt-{uuid4().hex[:12]}",
                )
                preview, rebound_context = _build_prepared_preview(
                    report_path,
                    rebound_context,
                    output_root=stale_binding.output_root,
                    pipeline_store=pipeline_store,
                    tv_library_root=getattr(config, "jellyfin_tv_root", None),
                    movie_library_root=getattr(config, "jellyfin_movie_root", None),
                    disc_fingerprint=disc_fingerprint,
                    skipped_titles=tuple(
                        item
                        for item in skipped_title_dispositions
                        if selected_indexes is None
                        or item["title_index"] in selected_indexes
                    ),
                )
                if _preview_disc_fingerprint(preview) != disc_fingerprint:
                    raise RipError("Rebound disc identity changed unexpectedly")
                job = public_store.create_job(preview, idempotency_key=idempotency_key)
                private_store.bind(
                    job_id=job.job_id,
                    plan_sha256=job.plan_sha256,
                    report_paths=[report_path],
                    output_root=stale_binding.output_root,
                    media_contexts={disc_id: rebound_context},
                )
                watcher.bind_current_job(
                    request.drive_index, job.job_id, disc_fingerprint
                )
                return _job_response(job, public_store)
            job = public_store.create_job(preview, idempotency_key=idempotency_key)
            private_store.bind(
                job_id=job.job_id,
                plan_sha256=job.plan_sha256,
                report_paths=[report_path],
                output_root=config.rip_output_root,
                media_contexts={disc_id: context},
            )
            watcher.bind_current_job(request.drive_index, job.job_id, disc_fingerprint)
        return _job_response(job, public_store)
    except (PreflightError, RipError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        preparation_lock.release()


def _disc_staged_mkv_plan(
    rip_root: Path, disc_fingerprint: str
) -> tuple[str, tuple[tuple[Path, int, int, str], ...]]:
    """Plan exact fingerprint-named MKVs under the configured rip root."""

    root = rip_root.resolve()
    marker = re.compile(
        rf"(?:^|--)disc-[0-9]+-{re.escape(disc_fingerprint)}-title-[0-9]{{3}}(?:-[^.]+)?\.mkv$",
        re.IGNORECASE,
    )
    candidates: list[tuple[Path, int, int, str]] = []
    for discovered in root.rglob("*.mkv"):
        if not marker.search(discovered.name):
            continue
        resolved = discovered.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            continue
        if not resolved.is_file():
            continue
        stat = resolved.stat()
        candidates.append((resolved, stat.st_size, stat.st_mtime_ns, relative))
    candidates.sort(key=lambda item: item[3].casefold())
    digest = hashlib.sha256(
        json.dumps(
            [
                {"relative_path": relative, "size_bytes": size, "mtime_ns": mtime}
                for _path, size, mtime, relative in candidates
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return digest, tuple(candidates)


def _current_forgettable_disc(
    watcher: DriveWatcher, drive_index: int, fingerprint: str
):
    selected = next(
        (
            drive
            for drive in watcher.snapshot().drives
            if drive.drive_index == drive_index and drive.has_disc
        ),
        None,
    )
    if selected is None or selected.current_disc_fingerprint != fingerprint:
        raise HTTPException(
            status_code=409,
            detail="The inserted disc identity changed; refresh before resetting it",
        )
    return selected


def _delete_disc_staged_mkvs(request: ForgetDiscIdentityRequest, rip_root: Path) -> int:
    if (
        request.confirm_delete_staged_media is not True
        or request.expected_media_plan_sha256 is None
        or request.authorized_media_file_count is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Exact staged-media deletion confirmation is required",
        )
    media_digest, media_candidates = _disc_staged_mkv_plan(
        rip_root, request.expected_disc_fingerprint
    )
    if (
        media_digest != request.expected_media_plan_sha256
        or len(media_candidates) != request.authorized_media_file_count
    ):
        raise HTTPException(
            status_code=409,
            detail="Staged media changed; review a fresh deletion preview",
        )
    for path, expected_size, expected_mtime, _relative in media_candidates:
        stat = path.stat()
        if stat.st_size != expected_size or stat.st_mtime_ns != expected_mtime:
            raise HTTPException(
                status_code=409,
                detail="Staged media changed; review a fresh deletion preview",
            )
    for path, _size, _mtime, _relative in media_candidates:
        path.unlink()
    return len(media_candidates)


@router.post("/drives/{drive_index}/forget-disc-identity/media-preview")
def preview_forget_drive_disc_media(
    drive_index: int,
    request: ForgetDiscMediaPreviewRequest,
    watcher: Annotated[DriveWatcher, Depends(get_drive_watcher)],
) -> dict[str, object]:
    """Preview exact staged MKVs that a destructive identity reset would delete."""

    _current_forgettable_disc(watcher, drive_index, request.expected_disc_fingerprint)
    config = get_config_manager().load()
    if config.rip_output_root is None or not config.rip_output_root.is_dir():
        raise HTTPException(status_code=409, detail="Rip staging root is unavailable")
    digest, candidates = _disc_staged_mkv_plan(
        config.rip_output_root, request.expected_disc_fingerprint
    )
    return {
        "plan_sha256": digest,
        "file_count": len(candidates),
        "total_size_bytes": sum(size for _path, size, _mtime, _relative in candidates),
        "candidates": [
            {"relative_path": relative, "size_bytes": size}
            for _path, size, _mtime, relative in candidates
        ],
        "jellyfin_files_affected": 0,
        "encoded_files_affected": 0,
    }


@router.post("/drives/{drive_index}/forget-disc-identity")
def forget_drive_disc_identity(
    drive_index: int,
    request: ForgetDiscIdentityRequest,
    watcher: Annotated[DriveWatcher, Depends(get_drive_watcher)],
    public_store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
    private_store: Annotated[PrivateBindingStore, Depends(get_private_binding_store)],
    pipeline_store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Forget one cached disc identity, optionally deleting exact staged MKVs."""

    if request.confirm_forget is not True:
        raise HTTPException(
            status_code=400,
            detail="Explicit disc-identity reset confirmation is required",
        )
    preparation_lock = _claim_drive_preparation(
        drive_index,
        operation="disc identity reset",
    )
    try:
        _current_forgettable_disc(
            watcher, drive_index, request.expected_disc_fingerprint
        )
        matching_items = tuple(
            item
            for item in pipeline_store.list_items()
            if _pipeline_item_response(item)["disc_fingerprint"]
            == request.expected_disc_fingerprint
        )
        if any(item.state == "running" for item in matching_items):
            raise HTTPException(
                status_code=409,
                detail="A pipeline item for this disc is currently running",
            )
        jobs = public_store.jobs_for_disc(request.expected_disc_fingerprint)
        if any(
            job.state
            in {"authorized", "queued", "running", "pause_requested", "paused"}
            or job.executor_attached
            for job in jobs
        ):
            raise HTTPException(
                status_code=409,
                detail="Active rip work for this disc must be stopped first",
            )
        deleted_media_count = 0
        if request.delete_staged_media:
            config = get_config_manager().load()
            if config.rip_output_root is None or not config.rip_output_root.is_dir():
                raise HTTPException(
                    status_code=409, detail="Rip staging root is unavailable"
                )
            deleted_media_count = _delete_disc_staged_mkvs(
                request, config.rip_output_root
            )
        job_ids = public_store.forget_disc_jobs(request.expected_disc_fingerprint)
        queue_count, history_count = pipeline_store.forget_disc_records(
            request.expected_disc_fingerprint
        )
        binding_count = private_store.delete_jobs(job_ids)
        watcher.clear_current_job(
            drive_index,
            expected_disc_fingerprint=request.expected_disc_fingerprint,
        )
    except OSError as exc:
        raise HTTPException(
            status_code=409,
            detail="Staged media could not be deleted; no remaining metadata was forgotten",
        ) from exc
    except (PipelineQueueError, RipError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        preparation_lock.release()
    logger.info(
        "Forgot disc metadata: job_count={}, queue_record_count={}, title_history_count={}",
        len(job_ids),
        queue_count,
        history_count,
    )
    return {
        "forgotten_disc_fingerprint": request.expected_disc_fingerprint,
        "deleted_job_count": len(job_ids),
        "deleted_private_binding_count": binding_count,
        "deleted_queue_record_count": queue_count,
        "deleted_title_history_count": history_count,
        "media_files_changed": deleted_media_count,
        "ready_for_fresh_preparation": True,
    }


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
    title_index: int | None = None
    series_name: str | None = None
    display_name: str | None = None
    match_summary: str | None = None
    location_label: str
    location_relative: str | None = None
    location_root_key: str | None = None
    output_size_bytes: int | None = None
    retained_source_available: bool = False
    retained_source_expired: bool = False
    retention_candidate_available: bool = False
    original_source_unavailable: bool = False
    staged_source_available: bool = False
    pipeline_media_available: bool = False
    provisional_match: bool = False
    gemini_confidence: float | None = None
    gemini_series_proposal: dict[str, object] | None = None
    retained_source_ttl_days: int = 30
    state: str
    stage: str
    created_at: str
    updated_at: str
    error_type: str | None
    review_code: str | None
    visual_review_code: str | None = None
    likely_removable: bool = False
    identification_attempts: list[dict[str, object]] = Field(default_factory=list)
    activity_status: str
    activity_age_seconds: int | None = None
    possibly_stalled: bool = False
    stall_after_seconds: int | None = None


class DiscTitleDispositionResponse(BaseModel):
    disc_fingerprint: str
    title_index: int
    disposition: str
    reason: str


class DiscMatchingScopeResponse(BaseModel):
    disc_fingerprint: str
    relevant_title_indexes: list[int]


class DiscRecoveryScopeResponse(BaseModel):
    disc_fingerprint: str
    required_title_indexes: list[int]


class PipelineQueueResponse(BaseModel):
    paused: bool
    startup_resume_in_seconds: int | None = None
    downstream_worker_limit: int
    automatic_processing_enabled: bool
    automatic_organization_enabled: bool
    items: list[PipelineItemResponse]
    title_dispositions: list[DiscTitleDispositionResponse] = Field(default_factory=list)
    disc_matching_scopes: list[DiscMatchingScopeResponse] = Field(default_factory=list)
    disc_recovery_scopes: list[DiscRecoveryScopeResponse] = Field(default_factory=list)


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
    remember_future_skip: bool = False


class RestoreDiscTitleDispositionRequest(BaseModel):
    disc_fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")
    title_index: int = Field(ge=0, le=999)
    confirm_restore: bool = False


class SkipDiscTitleDispositionRequest(BaseModel):
    disc_fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")
    title_index: int = Field(ge=0, le=999)
    reason: str = Field(pattern=r"^repeated_read_failure$")
    confirm_skip: bool = False


class AmbiguityChoiceRequest(BaseModel):
    choice: str = Field(pattern=r"^(gemini|manual|hold)$")
    confirm_external_fallback: bool = False


class ApplyEpisodeReleaseRequest(BaseModel):
    disc_fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")
    catalog_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    confirm_apply: bool = False


class UnmatchedDiscAnalysisRequest(BaseModel):
    disc_fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")
    series_name: str = Field(min_length=1, max_length=160)
    season: int | None = Field(default=None, ge=0, le=99)
    episode_start: int | None = Field(default=None, ge=1, le=999)
    episode_end: int | None = Field(default=None, ge=1, le=999)
    confirm_media_read: bool = False
    confirm_provider_lookup: bool = False
    confirm_external_fallback: bool = False
    reviewer_scene_descriptions: dict[
        str, Annotated[str, Field(min_length=3, max_length=1200)]
    ] = Field(default_factory=dict, max_length=20)


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


class PostponeRetainedSourceCleanupRequest(BaseModel):
    postpone_days: int = Field(default=7, ge=1, le=90)


class RetainExistingSourceRequest(RetainedSourceRequest):
    expected_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_file_count: int = Field(ge=1, le=999)
    confirm_move: bool = False


class ReencodeRetainedSourceRequest(RetainedSourceRequest):
    profile_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,47}$")
    confirm_reencode: bool = False


class PlayPipelineItemRequest(BaseModel):
    confirm_play: bool = False


class SilentVideoOcrRequest(BaseModel):
    expected_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_media_read: bool = False


class SilentVideoOcrResponse(BaseModel):
    media_id: str
    category: str
    summary: str
    ocr_excerpt: str
    ocr_text_characters: int
    sampled_frame_count: int


class RenameProvisionalItemRequest(BaseModel):
    new_name: str = Field(min_length=1, max_length=160)
    confirm_rename: bool = False


class ManualEpisodeIdentificationRequest(BaseModel):
    new_name: str = Field(min_length=1, max_length=160)
    content_type: str = Field(default="episode", pattern=r"^(episode|bonus)$")
    evidence_source: str = Field(
        default="manual_playback",
        pattern=r"^(manual_playback|catalogue_candidate|provider_candidate)$",
    )
    confirm_identification: bool = False


class CorrectEpisodeIdentificationRequest(BaseModel):
    candidate_episode_id: str = Field(pattern=r"^S\d{2}E\d{2,3}$")
    expected_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_correction: bool = False


class ResolveLibraryCollisionRequest(BaseModel):
    action: str = Field(pattern=r"^(replace-library|delete-new)$")
    expected_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_resolution: bool = False


class InspectLibraryCollisionRequest(BaseModel):
    expected_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_media_read: bool = False


class PlayLibraryCollisionFileRequest(BaseModel):
    target: str = Field(pattern=r"^(new-encode|existing-jellyfin)$")
    expected_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_play: bool = False


class CollisionAudioTrackDetails(BaseModel):
    index: int
    codec: str | None
    language: str | None
    title: str | None
    channels: int | None
    channel_layout: str | None
    bitrate_bps: int | None
    sample_rate_hz: int | None
    default: bool
    commentary: bool


class CollisionFileDetails(BaseModel):
    modified_at: str
    size_bytes: int
    container: str | None
    video_codec: str | None
    audio_codecs: list[str]
    width: int | None
    height: int | None
    field_order: str | None
    duration_seconds: float
    overall_bitrate_bps: int | None
    overall_bitrate_source: str | None
    video_bitrate_bps: int | None
    frame_rate_fps: float | None
    video_profile: str | None
    pixel_format: str | None
    bit_depth: int | None
    hdr_format: str | None
    color_primaries: str | None
    color_transfer: str | None
    color_space: str | None
    color_range: str | None
    video_encoder: str | None
    format_encoder: str | None
    audio_tracks: list[CollisionAudioTrackDetails]


class LibraryCollisionComparisonResponse(BaseModel):
    media_id: str
    new_pipeline_file: CollisionFileDetails
    existing_jellyfin_file: CollisionFileDetails
    size_difference_bytes: int


def _read_pipeline_contract_payload(item) -> dict[str, object]:
    try:
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pipeline_item_display_name(
    item, payload: dict[str, object] | None = None
) -> str | None:
    """Read only a safe matched basename from the current private contract."""

    if payload is None:
        payload = _read_pipeline_contract_payload(item)
    relative = payload.get("library_relative")
    if isinstance(relative, str) and relative.strip():
        parts = tuple(part for part in relative.replace("\\", "/").split("/") if part)
        if parts and not any(part in {".", ".."} for part in parts):
            safe_relative = omit_placeholder_episode_title(
                PurePosixPath(*parts), payload.get("episode_id")
            )
            name = safe_relative.name
            if name.casefold().endswith(".mkv"):
                name = name[:-4]
            return name.strip() or None

    context = payload.get("media_context")
    title_index = payload.get("title_index")
    if not isinstance(context, dict) or not isinstance(title_index, int):
        return None
    series_name = context.get("series_name")
    assignments = context.get("episode_assignments")
    if not isinstance(series_name, str) or not isinstance(assignments, list):
        return None
    for assignment in assignments:
        if (
            not isinstance(assignment, dict)
            or assignment.get("title_index") != title_index
        ):
            continue
        season = assignment.get("season")
        episode = assignment.get("episode")
        title = assignment.get("title")
        if (
            isinstance(season, int)
            and isinstance(episode, int)
            and isinstance(title, str)
            and title.strip()
        ):
            return (
                f"{' '.join(series_name.split())} - "
                f"S{season:02d}E{episode:02d} - {' '.join(title.split())}"
            )
    return None


def _pipeline_item_series_name(
    item, payload: dict[str, object] | None = None
) -> str | None:
    """Read a bounded canonical series label from the current contract."""

    if payload is None:
        payload = _read_pipeline_contract_payload(item)
    context = payload.get("media_context")
    if not isinstance(context, dict):
        return None
    series_name = context.get("series_name")
    if not isinstance(series_name, str):
        return None
    cleaned = " ".join(series_name.split())[:240]
    return cleaned or None


def _pipeline_identification_attempts(
    item,
    dossier: IdentificationDossierStore | None,
    series_name: str | None = None,
) -> list[dict[str, object]]:
    """Return concise attempts and one actionable candidate for legacy holds."""

    if dossier is None:
        return []
    attempts = [
        {
            "branch": attempt["branch"],
            "disposition": attempt["disposition"],
            "summary": dict(attempt["summary"]),
        }
        for attempt in dossier.safe_attempts(item.media_id)
    ]
    if item.review_code not in {
        "episode_match_review",
        "independent_episode_evidence_required",
        "whole_disc_coherence_review_required",
    } or any(
        isinstance(attempt["summary"].get("candidate_episode_id"), str)
        for attempt in attempts
    ):
        return attempts
    try:
        candidate_events = [
            event
            for event in dossier.audit_events(item.media_id)
            if event.get("event_kind") == "candidate"
            and event.get("branch") in {"tv-local", "tv-opensubtitles", "tv-gemini"}
            and isinstance(event.get("summary"), dict)
            and isinstance(event["summary"].get("candidate_episode_id"), str)
            and isinstance(event["summary"].get("candidate_episode_title"), str)
            and isinstance(event["summary"].get("score"), int | float)
            and not isinstance(event["summary"].get("score"), bool)
        ]
    except PipelineQueueError:
        return attempts
    if not candidate_events:
        return attempts
    latest_run_id = candidate_events[-1].get("analysis_run_id")
    latest_candidates = [
        event
        for event in candidate_events
        if event.get("analysis_run_id") == latest_run_id
    ]
    best = max(
        latest_candidates,
        key=lambda event: float(event["summary"]["score"]),
    )
    series_name = series_name or _pipeline_item_series_name(item)
    if series_name is None:
        return attempts
    candidate_summary = best["summary"]
    concise_candidate = {
        "candidate_series_name": series_name,
        "candidate_episode_id": candidate_summary["candidate_episode_id"],
        "candidate_episode_title": candidate_summary["candidate_episode_title"],
        "best_score": float(candidate_summary["score"]),
        "reason": "candidate_requires_human_playback_review",
    }
    branch = str(best["branch"])
    for attempt in reversed(attempts):
        if attempt["branch"] == branch and attempt["disposition"] == "review":
            attempt["summary"].update(concise_candidate)
            return attempts
    attempts.append({
        "branch": branch,
        "disposition": "review",
        "summary": concise_candidate,
    })
    return attempts


def _pipeline_item_match_summary(
    item, payload: dict[str, object] | None = None
) -> str | None:
    """Read a bounded provider/release summary from the current contract."""

    if payload is None:
        payload = _read_pipeline_contract_payload(item)
    direct_summary = payload.get("match_summary")
    if isinstance(direct_summary, str):
        cleaned = " ".join(direct_summary.split())[:320]
        if cleaned:
            return cleaned
    context = payload.get("media_context")
    title_index = payload.get("title_index")
    if not isinstance(context, dict) or not isinstance(title_index, int):
        return None
    assignments = context.get("special_feature_assignments")
    if not isinstance(assignments, list):
        return None
    for assignment in assignments:
        if (
            not isinstance(assignment, dict)
            or assignment.get("title_index") != title_index
        ):
            continue
        summary = assignment.get("match_summary")
        if isinstance(summary, str):
            cleaned = " ".join(summary.split())[:320]
            return cleaned or None
    return None


def _pipeline_catalogue_candidate_help(
    item, payload: dict[str, object] | None = None
) -> dict[str, object] | None:
    """Return one path-free, non-authorizing catalogue candidate for display."""

    if payload is None:
        payload = _read_pipeline_contract_payload(item)
    context = payload.get("media_context")
    title_index = payload.get("title_index")
    if (
        not isinstance(context, dict)
        or not isinstance(title_index, int)
        or isinstance(title_index, bool)
    ):
        return None
    assignments = context.get("catalogue_help_assignments")
    if not isinstance(assignments, list):
        return None
    for assignment in assignments:
        if (
            not isinstance(assignment, dict)
            or assignment.get("title_index") != title_index
            or assignment.get("identification_source") != "ripweaver-catalogue-help"
        ):
            continue
        season = assignment.get("season")
        episode = assignment.get("episode")
        title = assignment.get("title")
        series_name = context.get("series_name")
        if (
            not isinstance(season, int)
            or isinstance(season, bool)
            or season < 0
            or not isinstance(episode, int)
            or isinstance(episode, bool)
            or episode < 1
            or not isinstance(title, str)
            or not isinstance(series_name, str)
        ):
            return None
        clean_title = " ".join(title.split())[:160]
        clean_series = " ".join(series_name.split())[:160]
        if not clean_title or not clean_series:
            return None
        return {
            "series_name": clean_series,
            "season": season,
            "episode": episode,
            "title": clean_title,
            "independent_support": 1,
            "automatic": False,
        }
    return None


def _pipeline_item_location(
    item, payload: dict[str, object] | None = None
) -> tuple[str, str | None, str | None]:
    if payload is None:
        payload = _read_pipeline_contract_payload(item)
        if not payload:
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
                if transcode.get("episode_id") or transcode.get("library_kind") == "tv"
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


def _timestamp_age_seconds(
    timestamp: str, *, now: datetime | None = None
) -> int | None:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(0, round((current - parsed.astimezone(UTC)).total_seconds()))


def _pipeline_activity_snapshot(
    *,
    state: str,
    stage: str,
    updated_at: str,
    review_code: str | None,
    now: datetime | None = None,
) -> dict[str, object]:
    age = _timestamp_age_seconds(updated_at, now=now)
    threshold = None
    if state == "running":
        threshold = {"identify": 2700, "transcode": 21600, "organize": 900}.get(stage)
        status = "working"
    elif (
        state == "review_required" and review_code and review_code.endswith("_running")
    ):
        threshold = 3600
        status = "working"
    else:
        status = {
            "queued": "waiting",
            "review_required": "review",
            "failed": "failed",
            "completed": "completed",
            "dismissed": "inactive",
        }.get(state, "inactive")
    stalled = threshold is not None and age is not None and age > threshold
    return {
        "activity_status": "possibly_stalled" if stalled else status,
        "activity_age_seconds": age,
        "possibly_stalled": stalled,
        "stall_after_seconds": threshold,
    }


def _rip_activity_snapshot(
    *, state: str, updated_at: str, now: datetime | None = None
) -> dict[str, object]:
    age = _timestamp_age_seconds(updated_at, now=now)
    threshold = 300 if state in {"running", "pause_requested"} else None
    status = {
        "queued": "waiting",
        "running": "working",
        "pause_requested": "finishing_active_work",
        "paused": "paused",
        "failed": "failed",
        "completed": "completed",
        "stopped": "stopped",
    }.get(state, "inactive")
    stalled = threshold is not None and age is not None and age > threshold
    return {
        "rip_activity_status": "possibly_stalled" if stalled else status,
        "rip_activity_age_seconds": age,
        "rip_possibly_stalled": stalled,
        "rip_stall_after_seconds": threshold,
    }


def _pipeline_item_response(  # noqa: C901 - bounded contract/status composition
    item,
    dossier: IdentificationDossierStore | None = None,
    visual_review_code: str | None = None,
    config=None,
) -> dict[str, object]:
    payload = _read_pipeline_contract_payload(item)
    location_label, location_relative, location_root_key = _pipeline_item_location(
        item, payload
    )
    if config is None:
        config = get_config_manager().load()
    output_size_bytes = None
    retained_source_available = False
    retained_source_expired = False
    retention_candidate_available = False
    original_source_unavailable = False
    staged_source_available = False
    pipeline_media_available = False
    provisional_match = False
    gemini_confidence = None
    gemini_series_proposal = None
    disc_fingerprint = None
    title_index = None
    try:
        rip_payload = json.loads(
            item.artifact.contract_path.with_name(
                f"{item.media_id}.verified-rip.json"
            ).read_text(encoding="utf-8")
        )
        if not isinstance(rip_payload, dict):
            rip_payload = {}
        fingerprint = rip_payload.get("disc_fingerprint")
        if isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{16}", fingerprint):
            disc_fingerprint = fingerprint
        rip_title_index = rip_payload.get("title_index")
        if isinstance(rip_title_index, int) and not isinstance(rip_title_index, bool):
            title_index = rip_title_index
    except (OSError, json.JSONDecodeError):
        rip_payload = {}
    if item.review_code == "gemini_series_resolution_uncertain":
        try:
            proposal_event = next(
                (
                    event
                    for event in reversed(
                        get_pipeline_queue_store().list_events(item.media_id)
                    )
                    if event.event_type == "gemini_series_resolution_proposed"
                ),
                None,
            )
            if proposal_event is not None:
                details = proposal_event.details
                name = details.get("proposed_series_name")
                confidence = details.get("confidence")
                tmdb_id = details.get("proposed_tmdb_id")
                ranked_names = details.get("proposed_series_names")
                if (
                    isinstance(name, str)
                    and isinstance(confidence, int | float)
                    and not isinstance(confidence, bool)
                ):
                    gemini_series_proposal = {
                        "series_name": name,
                        "series_names": (
                            ranked_names
                            if isinstance(ranked_names, list)
                            and all(isinstance(value, str) for value in ranked_names)
                            else [name]
                        ),
                        "confidence": float(confidence),
                        "tmdb_id": tmdb_id if isinstance(tmdb_id, int) else None,
                    }
        except PipelineQueueError:
            pass
    try:
        if disc_fingerprint is None:
            fingerprint = payload.get("disc_fingerprint")
            if isinstance(fingerprint, str) and re.fullmatch(
                r"[0-9a-f]{16}", fingerprint
            ):
                disc_fingerprint = fingerprint
        if title_index is None:
            payload_title_index = payload.get("title_index")
            if isinstance(payload_title_index, int) and not isinstance(
                payload_title_index, bool
            ):
                title_index = payload_title_index
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
        retained_at = payload.get("archived_source_retained_at")
        retained_status = _retained_source_status(
            retained_path=retained if isinstance(retained, str) else None,
            retained_size=retained_size if isinstance(retained_size, int) else None,
            retained_at=retained_at if isinstance(retained_at, str) else None,
            contract_path=item.artifact.contract_path,
            ttl_days=getattr(config, "retained_source_ttl_days", 30),
        )
        if retained_status == "active":
            retained_source_available = True
        elif retained_status == "expired":
            retained_source_expired = True
        original_source_unavailable = bool(payload.get("original_source_unavailable"))
        if (
            not original_source_unavailable
            and payload.get("mode") == "verified-transcode-contract"
            and isinstance(payload.get("original_source_path"), str)
            and isinstance(payload.get("original_source_size_bytes"), int)
        ):
            original_source_unavailable = not Path(
                payload["original_source_path"]
            ).is_file()
        provisional_match = bool(payload.get("provisional_match"))
        pipeline_media_available = (
            item.stage == "organize" and item.state == "completed"
        ) or _payload_has_verified_media_file(payload)
        if not pipeline_media_available and rip_payload:
            pipeline_media_available = _payload_has_verified_media_file(rip_payload)
        confidence = payload.get("gemini_confidence")
        if isinstance(confidence, int | float) and not isinstance(confidence, bool):
            gemini_confidence = float(confidence)
    except OSError:
        pass
    if (
        item.state == "completed"
        and item.stage == "organize"
        and not retained_source_available
    ):
        source = Path(str(rip_payload.get("source_path", "")))
        source_size = rip_payload.get("source_size_bytes")
        retention_candidate_available = (
            isinstance(source_size, int)
            and source_size > 0
            and source.is_file()
            and source.stat().st_size == source_size
        )
    series_name = _pipeline_item_series_name(item, payload)
    response = {
        "media_id": item.media_id,
        "artifact_sha256": item.artifact.contract_sha256,
        "disc_fingerprint": disc_fingerprint,
        "title_index": title_index,
        "series_name": series_name,
        "display_name": _pipeline_item_display_name(item, payload),
        "match_summary": _pipeline_item_match_summary(item, payload),
        "catalogue_candidate_help": _pipeline_catalogue_candidate_help(item, payload),
        "location_label": location_label,
        "location_relative": location_relative,
        "location_root_key": location_root_key,
        "output_size_bytes": output_size_bytes,
        "retained_source_available": retained_source_available,
        "retained_source_expired": retained_source_expired,
        "retention_candidate_available": retention_candidate_available,
        "original_source_unavailable": original_source_unavailable,
        "staged_source_available": staged_source_available,
        "pipeline_media_available": pipeline_media_available,
        "provisional_match": provisional_match,
        "gemini_confidence": gemini_confidence,
        "gemini_series_proposal": gemini_series_proposal,
        "retained_source_ttl_days": getattr(config, "retained_source_ttl_days", 30),
        "state": item.state,
        "stage": item.stage,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "error_type": item.error_type,
        "review_code": item.review_code,
        "visual_review_code": visual_review_code,
        "likely_removable": visual_review_code
        in {"likely_warning_screen", "likely_disc_menu"},
        "identification_attempts": _pipeline_identification_attempts(
            item, dossier, series_name
        ),
    }
    response.update(
        _pipeline_activity_snapshot(
            state=item.state,
            stage=item.stage,
            updated_at=item.updated_at,
            review_code=item.review_code,
        )
    )
    return response


def _exact_queued_rip_source(item, rip_root: Path) -> Path | None:  # noqa: C901
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
    if source.suffix.lower() != ".mkv":
        raise PipelineQueueError("The queued staged MKV is unavailable")
    if not source.exists():
        # The staging-cleanup tool may have removed this exact file already.
        # The queue record can still be discarded without touching anything.
        return None
    if not source.is_file():
        raise PipelineQueueError("The queued staged MKV is unavailable")
    if source.stat().st_size != source_size:
        raise PipelineQueueError("The queued staged MKV changed after verification")
    return source


def _delete_exact_queued_rip(item, rip_root: Path) -> None:
    try:
        source = _exact_queued_rip_source(item, rip_root)
        if source is not None:
            source.unlink()
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


def _aggregate_rip_progress(job, events) -> dict[str, int | str | None]:
    preview_jobs = [
        item for item in job.preview.get("jobs", []) if isinstance(item, dict)
    ]
    samples = [event for event in events if event.event_type == "rip_progress"]
    completed_scopes = {
        str(event.details.get("scope", ""))
        for event in events
        if event.event_type in {"rip_title_completed", "rip_title_output_closed"}
    }
    fields: dict[str, int | str | None] = {
        "rip_progress_percent": None,
        "rip_progress_scope": None,
        "rip_overall_progress_percent": None,
        "rip_completed_title_count": None,
        "rip_total_title_count": len(preview_jobs),
    }
    if not samples:
        if job.state == "completed":
            fields["rip_overall_progress_percent"] = 100
            fields["rip_completed_title_count"] = len(preview_jobs)
        return fields

    active_samples = [
        event for event in samples if str(event.details.get("scope", "")) != "overall"
    ]
    latest = active_samples[-1] if active_samples else samples[-1]
    scope = str(latest.details.get("scope", "batch"))
    fields["rip_progress_percent"] = int(latest.details.get("percent", 0))
    fields["rip_progress_scope"] = scope
    latest_by_scope = {
        str(event.details.get("scope", "")): int(event.details.get("percent", 0))
        for event in samples
    }
    if "overall" in latest_by_scope:
        fields["rip_overall_progress_percent"] = latest_by_scope["overall"]
        fields["rip_completed_title_count"] = len(completed_scopes)
        if job.state == "completed":
            fields["rip_overall_progress_percent"] = 100
            fields["rip_completed_title_count"] = len(preview_jobs)
        return fields
    if "batch" in latest_by_scope:
        if job.state == "completed":
            fields["rip_overall_progress_percent"] = 100
            fields["rip_completed_title_count"] = len(preview_jobs)
        else:
            fields["rip_overall_progress_percent"] = latest_by_scope["batch"]
            fields["rip_completed_title_count"] = len(completed_scopes)
        return fields
    if not preview_jobs:
        return fields

    known_weights = [
        int(item["estimated_bytes"])
        for item in preview_jobs
        if isinstance(item.get("estimated_bytes"), int)
        and int(item["estimated_bytes"]) > 0
    ]
    fallback_weight = (
        sorted(known_weights)[len(known_weights) // 2] if known_weights else 1
    )
    weighted_progress = 0
    total_weight = 0
    completed_titles = 0
    for item in preview_jobs:
        estimated = item.get("estimated_bytes")
        weight = (
            int(estimated)
            if isinstance(estimated, int) and estimated > 0
            else fallback_weight
        )
        item_scope = str(item.get("job_id"))
        item_percent = (
            100
            if item_scope in completed_scopes
            else latest_by_scope.get(item_scope, 0)
        )
        weighted_progress += weight * item_percent
        total_weight += weight
        completed_titles += int(item_scope in completed_scopes)
    fields["rip_overall_progress_percent"] = round(weighted_progress / total_weight)
    fields["rip_completed_title_count"] = completed_titles
    if job.state == "completed":
        fields["rip_overall_progress_percent"] = 100
        fields["rip_completed_title_count"] = len(preview_jobs)
    return fields


def _pipeline_handoff_response(
    events: tuple[OrchestrationEvent, ...],
) -> dict[str, object]:
    latest_by_scope = {}
    for event in events:
        if event.event_type not in {
            "rip_pipeline_queued",
            "rip_pipeline_handoff_failed",
        }:
            continue
        scope = event.details.get("scope")
        if isinstance(scope, str):
            latest_by_scope[scope] = event.event_type
    queued_count = sum(
        event_type == "rip_pipeline_queued" for event_type in latest_by_scope.values()
    )
    pending_count = sum(
        event_type == "rip_pipeline_handoff_failed"
        for event_type in latest_by_scope.values()
    )
    fields: dict[str, object] = {}
    if latest_by_scope:
        fields.update({
            "pipeline_handoff_status": (
                "attention_required" if pending_count else "streaming"
            ),
            "pipeline_queued_title_count": queued_count,
            "pipeline_handoff_pending_title_count": pending_count,
        })
    terminal = next(
        (
            event
            for event in reversed(events)
            if event.event_type in {"job_completed", "job_failed"}
        ),
        None,
    )
    if terminal is None:
        return fields
    terminal_queued_count = terminal.details.get("pipeline_queued_count")
    pending_ids = terminal.details.get("pipeline_handoff_pending_job_ids", [])
    fields.update({
        "pipeline_handoff_status": terminal.details.get(
            "pipeline_handoff_status", "not_configured"
        ),
        "pipeline_queued_title_count": (
            int(terminal_queued_count)
            if isinstance(terminal_queued_count, int)
            else None
        ),
        "pipeline_handoff_pending_title_count": (
            len(pending_ids) if isinstance(pending_ids, list) else 0
        ),
    })
    return fields


def _job_response(job, store: OrchestrationStore | None = None) -> dict[str, object]:
    response = asdict(job)
    response.update({
        "error_type": None,
        "error_category": None,
        "failed_drive_indexes": [],
        "recommendations": [],
        "rip_title_summary": None,
        "rip_progress_percent": None,
        "rip_transfer_mib_s": None,
        "rip_progress_scope": None,
        "rip_progress_updated_at": None,
        "rip_overall_progress_percent": None,
        "rip_completed_title_count": None,
        "rip_total_title_count": None,
        "rip_activity_status": "inactive",
        "rip_activity_age_seconds": None,
        "rip_possibly_stalled": False,
        "rip_stall_after_seconds": None,
        "pipeline_handoff_status": "not_configured",
        "pipeline_queued_title_count": None,
        "pipeline_handoff_pending_title_count": 0,
    })
    if store is not None:
        events = store.list_events(job.job_id)
        response.update(_pipeline_handoff_response(events))
        samples = [event for event in events if event.event_type == "rip_progress"]
        response.update(_aggregate_rip_progress(job, events))
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
    response.update(
        _rip_activity_snapshot(
            state=job.state,
            updated_at=str(response["rip_progress_updated_at"] or job.updated_at),
        )
    )
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
            completed_ids = {
                str(value) for value in details.get("completed_job_ids", [])
            }
            title_refs = [
                {
                    "job_id": str(item.get("job_id")),
                    "title_index": int(item.get("title_index", -1)),
                    "drive_index": int(item.get("drive_index", -1)),
                }
                for item in job.preview.get("jobs", [])
                if isinstance(item, dict)
                and isinstance(item.get("job_id"), str)
                and isinstance(item.get("title_index"), int)
                and isinstance(item.get("drive_index"), int)
            ]
            response["rip_title_summary"] = {
                "total_titles": len(title_refs),
                "verified_titles": [
                    item for item in title_refs if item["job_id"] in completed_ids
                ],
                "unfinished_titles": [
                    item for item in title_refs if item["job_id"] not in completed_ids
                ],
            }
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
    "/jobs/{job_id}/pipeline-settings", response_model=OrchestrationJobResponse
)
def update_rip_pipeline_settings(
    job_id: str,
    request: PipelineSettingsRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    store: Annotated[OrchestrationStore, Depends(get_orchestration_store)],
) -> dict[str, object]:
    """Update content routing and HandBrake choices before their stages start."""

    try:
        job = store.update_pipeline_settings(
            job_id,
            content_hint=request.content_hint,
            handbrake_profile_id=request.handbrake_profile_id,
            idempotency_key=idempotency_key,
        )
        return _job_response(job, store)
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


def _saved_skipped_titles_for_selection(
    preview: dict[str, object],
    selected: set[int],
    pipeline_store: PipelineQueueStore,
) -> set[int]:
    skipped_selected: set[int] = set()
    for item in preview.get("jobs", []):
        if not isinstance(item, dict) or item.get("title_index") not in selected:
            continue
        staging_destination = item.get("staging_destination")
        if not isinstance(staging_destination, str):
            continue
        fingerprint_match = re.search(
            r"(?:^|/)([0-9a-f]{16})(?:/|$)", staging_destination
        )
        if fingerprint_match is None:
            continue
        dispositions = pipeline_store.title_dispositions(fingerprint_match.group(1))
        title_index = item["title_index"]
        if dispositions.get(title_index, {}).get("disposition") == "skip":
            skipped_selected.add(title_index)
    return skipped_selected


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
    pipeline_store: Annotated[
        PipelineQueueStore,
        Depends(get_pipeline_queue_store),
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
        skipped_selected = _saved_skipped_titles_for_selection(
            source_job.preview, selected, pipeline_store
        )
        if skipped_selected:
            labels = ", ".join(str(index) for index in sorted(skipped_selected))
            raise RipError(
                f"Title indexes {labels} are saved as skipped for this exact disc; "
                "refresh the recovery plan before authorizing another rip"
            )
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
    scope: Literal["all", "active", "dashboard"] = "all",
    disc_fingerprints: str | None = None,
) -> dict[str, object]:
    """Return recent redacted jobs; optional scopes reduce routine UI work."""

    from mkv_episode_matcher.core.config_manager import get_config_manager

    automatic = get_config_manager().load().automatic_processing_enabled
    fingerprints = _parse_dashboard_disc_fingerprints(disc_fingerprints)
    jobs = _filter_dashboard_jobs(
        store.list_jobs(),
        scope=scope,
        disc_fingerprints=fingerprints,
    )
    return {
        "automatic_processing_enabled": automatic,
        "watcher_attached": False,
        "jobs": [_job_response(job, store) for job in jobs],
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


def _verified_rip_handoff_scope(
    bound: BoundRipDispatch,
    results: list[RipResult],
    full_preview: dict[str, object],
    pipeline_store: PipelineQueueStore,
) -> tuple[tuple[RipJob, ...], dict[str, tuple[int, ...]]]:
    """Select exact completed jobs while retaining known whole-disc expectations."""

    results_by_id = {result.job_id: result for result in results}
    if len(results_by_id) != len(results):
        raise PipelineQueueError("Verified rip result set contains duplicate jobs")
    completed_jobs = tuple(
        manifest_job
        for manifest_job in bound.manifest.jobs
        if manifest_job.job_id in results_by_id
    )
    if {item.job_id for item in completed_jobs} != set(results_by_id):
        raise PipelineQueueError(
            "Verified rip result set does not belong to this dispatch"
        )
    expected_by_disc: dict[str, list[int]] = {}
    fingerprints_by_disc: dict[str, str] = {}
    for preview_job in full_preview.get("jobs", []):
        if (
            not isinstance(preview_job, dict)
            or not isinstance(preview_job.get("job_id"), str)
            or not isinstance(preview_job.get("title_index"), int)
        ):
            continue
        disc_id = preview_job["job_id"].rsplit("-title-", 1)[0]
        expected_by_disc.setdefault(disc_id, []).append(preview_job["title_index"])
        staging_destination = preview_job.get("staging_destination")
        if not isinstance(staging_destination, str):
            continue
        fingerprint = next(
            (
                part
                for part in PurePosixPath(staging_destination.replace("\\", "/")).parts
                if re.fullmatch(r"[0-9a-f]{16}", part)
            ),
            None,
        )
        if fingerprint is not None:
            fingerprints_by_disc[disc_id] = fingerprint
    for disc_id, fingerprint in fingerprints_by_disc.items():
        expected_by_disc.setdefault(disc_id, []).extend(
            pipeline_store.expected_title_indexes_for_disc(fingerprint)
        )
    return completed_jobs, {
        disc_id: tuple(sorted(set(title_indexes)))
        for disc_id, title_indexes in expected_by_disc.items()
    }


def _rip_recovery_media_id(job: RipJob) -> str:
    """Derive a stable queue ID for a new isolated copy of the same title."""

    stem = re.sub(
        r"[^A-Za-z0-9._-]+", "-", Path(job.output_basename or job.job_id).stem
    )
    stem = stem.strip("._-")[:96] or "recovered-mkv"
    attempt_digest = hashlib.sha256(
        f"{job.job_id}\0{job.relative_output_dir}\0{job.output_basename or ''}".encode()
    ).hexdigest()[:16]
    return f"{stem}-recovery-{attempt_digest}"


def _enqueue_completed_rip_results(  # noqa: C901
    *,
    orchestration_job_id: str,
    full_preview: dict[str, object],
    bound: BoundRipDispatch,
    results: list[RipResult],
    store: OrchestrationStore,
    pipeline_store: PipelineQueueStore,
    contract_root: Path,
) -> None:
    """Admit verified outputs while keeping failures separate from MakeMKV."""

    try:
        contract_root.mkdir(parents=True, exist_ok=True)
        settings = store.get_pipeline_settings(orchestration_job_id)
        content_hint = settings["content_hint"]
        handbrake_profile_id = settings["handbrake_profile_id"]
        contexts = {
            context.disc_id: replace(
                context,
                content_hint=content_hint or context.content_hint,
                handbrake_profile_id=(
                    handbrake_profile_id or context.handbrake_profile_id
                ),
            )
            for context in bound.manifest.media_contexts
        }
        completed_jobs, expected_title_indexes_by_disc = _verified_rip_handoff_scope(
            bound, results, full_preview, pipeline_store
        )
        enqueue_arguments = {
            "jobs": completed_jobs,
            "results": results,
            "output_root": bound.output_root,
            "contract_root": contract_root,
            "media_contexts": contexts,
            "expected_title_indexes_by_disc": expected_title_indexes_by_disc,
        }
        try:
            enqueue_verified_rip_results(pipeline_store, **enqueue_arguments)
        except PipelineQueueError as exc:
            if "media ID has a different artifact" not in str(exc):
                raise
            enqueue_verified_rip_results(
                pipeline_store,
                **enqueue_arguments,
                media_id_overrides={
                    completed_job.job_id: _rip_recovery_media_id(completed_job)
                    for completed_job in completed_jobs
                },
            )
    except (OSError, PipelineQueueError) as exc:
        for result in results:
            try:
                store.record_pipeline_handoff(
                    orchestration_job_id,
                    result.job_id,
                    queued=False,
                    error_type=type(exc).__name__,
                )
            except RipError:
                pass
        raise RipError(
            f"Verified rip queue handoff failed: {type(exc).__name__}"
        ) from exc
    for result in results:
        try:
            store.record_pipeline_handoff(
                orchestration_job_id, result.job_id, queued=True
            )
        except RipError:
            # Admission already succeeded. A dashboard audit event must not
            # turn that completed handoff into a retry.
            pass


def _execution_drive_indexes(job: OrchestrationJob) -> tuple[int, ...]:
    """Return the exact ordered drive scope retained by an authorized preview."""

    raw_drives = job.preview.get("drives", [])
    if not isinstance(raw_drives, list) or not raw_drives:
        raise RipError("Authorized job has no physical drive scope")
    drive_indexes: list[int] = []
    for drive in raw_drives:
        drive_index = drive.get("drive_index") if isinstance(drive, dict) else None
        if (
            not isinstance(drive_index, int)
            or isinstance(drive_index, bool)
            or not 0 <= drive_index <= 99
            or drive_index in drive_indexes
        ):
            raise RipError("Authorized job has an invalid physical drive scope")
        drive_indexes.append(drive_index)

    preview_job_drives = {
        item.get("drive_index")
        for item in job.preview.get("jobs", [])
        if isinstance(item, dict)
    }
    if preview_job_drives != set(drive_indexes):
        raise RipError("Authorized title and drive scopes do not match")
    return tuple(drive_indexes)


def _prepare_execution_inventory_and_attach(
    *,
    job_id: str,
    run_directory: Path,
    drive_indexes: tuple[int, ...],
    executable: Path,
    inventory_runner: Callable,
    registry: RipExecutionRegistry,
    watcher: DriveWatcher,
    cache_dir: Path,
    timeout_seconds: int,
) -> list[Path]:
    """Rescan exact drives, retain private reports, then atomically claim ripping."""

    leases: list[OpticalWorkLease] = []
    try:
        for drive_index in drive_indexes:
            leases.append(
                registry.claim_drive_preparation(
                    drive_index,
                    operation="execution inventory",
                )
            )

        report_directory = (
            cache_dir.parent
            / "orchestration"
            / "execution-inventories"
            / f"{job_id}-{uuid4().hex}"
        )
        report_directory.mkdir(parents=True, exist_ok=False)
        report_paths: list[Path] = []
        cached_drives = {
            drive.drive_index: drive for drive in watcher.snapshot().drives
        }
        for drive_index in drive_indexes:
            result = inventory_runner(
                executable,
                f"disc:{drive_index}",
                minimum_length=0,
                timeout_seconds=min(timeout_seconds, 300),
            )
            if result.return_code != 0:
                raise PreflightError(
                    "MakeMKV execution inventory did not complete successfully"
                )
            cached_drive = cached_drives.get(drive_index)
            matching_drive = targeted_inventory_drive(
                result.stdout,
                requested_index=drive_index,
                cached_device_name=watcher.device_name(drive_index),
                cached_drive_name=(
                    cached_drive.display_name if cached_drive is not None else None
                ),
                cached_disc_name=(
                    cached_drive.disc_label if cached_drive is not None else None
                ),
            )
            if matching_drive is None or not matching_drive.has_disc:
                raise PreflightError(
                    "Authorized disc disappeared during execution inventory"
                )
            inventory = parse_disc_inventory(result, matching_drive)
            if not inventory.titles:
                raise PreflightError(
                    "MakeMKV execution inventory returned no disc titles"
                )
            report_path, _robot_path = write_inventory_report(
                report_directory,
                inventory,
                result,
                minimum_length_seconds=0,
            )
            report_paths.append(report_path)

        registry.promote_drive_preparations(
            job_id,
            run_directory,
            tuple(leases),
        )
        logger.info(
            "Verified and retained {} execution-time disc inventory report(s)",
            len(report_paths),
        )
        return report_paths
    except (OSError, PreflightError) as exc:
        logger.warning(
            "Execution-time disc inventory failed safely: {}",
            type(exc).__name__,
        )
        raise RipError(
            f"Execution-time disc inventory failed safely: {type(exc).__name__}"
        ) from exc
    finally:
        for lease in leases:
            lease.release()


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
    watcher: Annotated[DriveWatcher, Depends(get_drive_watcher)],
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
            _enqueue_completed_rip_results(
                orchestration_job_id=job_id,
                full_preview=job.preview,
                bound=bound,
                results=results,
                store=store,
                pipeline_store=pipeline_store,
                contract_root=contract_root,
            )

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
                # Physical execution remains behind this exact-digest,
                # exact-count, separately confirmed API boundary. The queue
                # chooses one single-open operation per eligible drive.
                physical_disc_execution_enabled=True,
            ),
            queue_runner=queue_runner,
            completion_sink=enqueue_results,
            progress_sink=lambda kind, message: store.record_progress(
                job_id, kind, message
            ),
        )
        drive_indexes = _execution_drive_indexes(job)
        executor_attached = False

        def prepare_fresh_inventory() -> list[Path]:
            nonlocal executor_attached
            report_paths = _prepare_execution_inventory_and_attach(
                job_id=job_id,
                run_directory=run_directory,
                drive_indexes=drive_indexes,
                executable=executable,
                inventory_runner=inventory_runner,
                registry=registry,
                watcher=watcher,
                cache_dir=config.cache_dir,
                timeout_seconds=request.timeout_seconds,
            )
            executor_attached = True
            return report_paths

        try:
            completed = RipDispatcher(store, private_store).dispatch(
                job_id,
                dispatch_key=idempotency_key,
                executor=executor,
                fresh_inventory_provider=prepare_fresh_inventory,
            )
        finally:
            if executor_attached:
                registry.detach(job_id)
        if (
            executor_attached
            and config.automatic_eject_after_rip
            and completed.state == "completed"
        ):
            _auto_eject_completed_job_drives(
                completed,
                store,
                inventory_runner,
                executable,
                request.timeout_seconds,
                registry,
                watcher,
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
    scope: Literal["all", "active", "attention", "dashboard"] = "all",
    disc_fingerprints: str | None = None,
) -> dict[str, object]:
    """Return path-redacted queue state, optionally scoped for a visible view."""

    fingerprints = _parse_dashboard_disc_fingerprints(disc_fingerprints)
    items = _filter_dashboard_pipeline_items(
        store.list_items(),
        scope=scope,
        disc_fingerprints=fingerprints,
    )
    dossier = IdentificationDossierStore(
        get_pipeline_contract_root().parent / "identification-evidence"
    )
    visual_reviews = store.silent_video_review_flags()
    config = get_config_manager().load()
    item_responses = [
        _pipeline_item_response(
            item,
            dossier,
            visual_reviews.get(item.media_id),
            config,
        )
        for item in items
    ]
    matching_scope_fingerprints = fingerprints or tuple(
        sorted({
            fingerprint
            for item in item_responses
            if isinstance((fingerprint := item.get("disc_fingerprint")), str)
        })
    )
    return {
        "paused": store.is_paused(),
        "startup_resume_in_seconds": startup_queue_resume_seconds(),
        "downstream_worker_limit": 1,
        "automatic_processing_enabled": config.automatic_processing_enabled,
        "automatic_organization_enabled": config.automatic_organization_enabled,
        "title_dispositions": [
            disposition
            for disposition in store.list_title_dispositions()
            if not fingerprints or disposition["disc_fingerprint"] in fingerprints
        ],
        "disc_matching_scopes": _dashboard_disc_matching_scopes(
            store, matching_scope_fingerprints
        ),
        "disc_recovery_scopes": _dashboard_disc_recovery_scopes(
            store, matching_scope_fingerprints
        ),
        "items": item_responses,
    }


@router.get("/pipeline/discs/{disc_fingerprint}/identification-audit")
def get_disc_identification_audit(
    disc_fingerprint: str,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    orchestration_store: Annotated[
        OrchestrationStore, Depends(get_orchestration_store)
    ],
) -> dict[str, object]:
    """Return an explicit, complete, path- and dialogue-redacted disc trace."""

    if re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None:
        raise HTTPException(status_code=422, detail="Disc fingerprint is invalid")
    dossier = IdentificationDossierStore(
        get_pipeline_contract_root().parent / "identification-evidence"
    )
    titles = []
    for item in store.list_items():
        snapshot = _pipeline_item_response(item)
        if snapshot.get("disc_fingerprint") != disc_fingerprint:
            continue
        try:
            audit_events = list(dossier.audit_events(item.media_id))
        except PipelineQueueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        titles.append({
            "media_id": item.media_id,
            "title_index": snapshot.get("title_index"),
            "display_name": snapshot.get("display_name"),
            "match_summary": snapshot.get("match_summary"),
            "stage": item.stage,
            "state": item.state,
            "review_code": item.review_code,
            "error_type": item.error_type,
            "pipeline_events": [
                asdict(event) for event in store.list_events(item.media_id)
            ],
            "identification_audit": audit_events,
        })
    titles.sort(
        key=lambda value: (
            value["title_index"] is None,
            value["title_index"] if value["title_index"] is not None else 0,
            str(value["media_id"]),
        )
    )
    matching_runs = [
        run
        for run in store.matching_performance(limit=200)
        if run.get("disc_fingerprint") == disc_fingerprint
    ]
    title_history = {
        title_index: {
            "outcome_name": outcome.get("outcome_name"),
            "episode_id": outcome.get("episode_id"),
        }
        for title_index, outcome in store.title_history(disc_fingerprint).items()
    }
    rip_jobs = [
        {
            "job_id": job.job_id,
            "state": job.state,
            "plan_sha256": job.plan_sha256,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "executor_attached": job.executor_attached,
            "events": [
                asdict(event) for event in orchestration_store.list_events(job.job_id)
            ],
        }
        for job in orchestration_store.jobs_for_disc(disc_fingerprint)
    ]
    return {
        "disc_fingerprint": disc_fingerprint,
        "rip_jobs": rip_jobs,
        "titles": titles,
        "title_history": title_history,
        "matching_runs": matching_runs,
        "path_redacted": True,
        "dialogue_redacted": True,
        "complete_candidate_audit_is_on_demand": True,
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


@router.get("/pipeline/matching-performance")
def get_matching_performance(
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Return recent path- and dialogue-free all-season timing records."""

    return {
        "runs": list(store.matching_performance()),
        "path_redacted": True,
        "dialogue_redacted": True,
    }


@router.get("/pipeline/learned-coverage")
def get_learned_series_coverage(
    series_name: str,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    try:
        coverage = store.learned_series_coverage(series_name)
    except PipelineQueueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "coverage": coverage,
        "contains_media_paths": False,
        "contains_dialogue": False,
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
            media_id,
            lambda item: _delete_exact_queued_rip(item, root_value),
            remember_future_skip=request.remember_future_skip,
        )
    except PipelineQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return get_pipeline_items(store)


@router.post("/pipeline/disc-title-dispositions/restore")
def restore_disc_title_disposition(
    request: RestoreDiscTitleDispositionRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Restore one explicitly skipped fingerprint/title to future planning."""

    if request.confirm_restore is not True:
        raise HTTPException(
            status_code=400,
            detail="Future-rip title restoration confirmation is required",
        )
    try:
        restored = store.restore_title_disposition(
            request.disc_fingerprint, request.title_index
        )
    except PipelineQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not restored:
        raise HTTPException(
            status_code=409, detail="The saved future-rip skip was not found"
        )
    return {
        "restored": True,
        "disc_fingerprint": request.disc_fingerprint,
        "title_index": request.title_index,
    }


@router.post(
    "/pipeline/disc-title-dispositions/skip",
    response_model=PipelineQueueResponse,
)
def skip_disc_title_after_read_failure(
    request: SkipDiscTitleDispositionRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Exclude one exact repeatedly unreadable title from future rip plans."""

    if request.confirm_skip is not True:
        raise HTTPException(
            status_code=400,
            detail="Future-rip title skip confirmation is required",
        )
    try:
        store.remember_title_skip(
            request.disc_fingerprint,
            request.title_index,
            reason=request.reason,
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
    if request.reviewer_scene_descriptions and not request.confirm_external_fallback:
        raise HTTPException(
            status_code=400,
            detail="Reviewer scene descriptions require explicit Gemini transmission approval",
        )
    if (request.episode_start is None) != (request.episode_end is None):
        raise HTTPException(
            status_code=400,
            detail="Both the first and last reviewed episode are required",
        )
    if request.episode_start is not None:
        if request.season is None:
            raise HTTPException(
                status_code=400,
                detail="A reviewed episode range requires a reviewed season",
            )
        if (
            request.episode_end is None
            or request.episode_end < request.episode_start
            or request.episode_end - request.episode_start > 49
        ):
            raise HTTPException(
                status_code=400,
                detail="The reviewed episode range is invalid or too broad",
            )
    episode_range = (
        (request.episode_start, request.episode_end)
        if request.episode_start is not None and request.episode_end is not None
        else None
    )
    requested_series_name = " ".join(request.series_name.split())
    latest_by_title = {}
    for item in store.list_items():
        try:
            rip_payload = json.loads(
                store.rip_artifact(item.media_id).contract_path.read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError, PipelineQueueError):
            continue
        title_index = rip_payload.get("title_index")
        if (
            rip_payload.get("disc_fingerprint") != request.disc_fingerprint
            or not isinstance(title_index, int)
            or isinstance(title_index, bool)
        ):
            continue
        previous = latest_by_title.get(title_index)
        if previous is None or (item.created_at, item.updated_at, item.media_id) >= (
            previous.created_at,
            previous.updated_at,
            previous.media_id,
        ):
            latest_by_title[title_index] = item
    selected = tuple(
        latest_by_title[index].media_id
        for index in sorted(latest_by_title)
        if latest_by_title[index].stage == "identify"
        and latest_by_title[index].state == "review_required"
        and latest_by_title[index].review_code in TV_DISC_ANALYSIS_REVIEW_CODES
    )
    if not selected:
        raise HTTPException(
            status_code=409, detail="No unmatched disc titles are ready"
        )
    unknown_scene_description_ids = set(request.reviewer_scene_descriptions) - set(
        selected
    )
    if unknown_scene_description_ids:
        raise HTTPException(
            status_code=409,
            detail="A reviewer scene description no longer matches a held disc title",
        )
    reviewer_scene_descriptions = {
        media_id: " ".join(description.split())
        for media_id, description in request.reviewer_scene_descriptions.items()
    }
    # This endpoint is an explicit reviewed recovery boundary.  The submitted
    # canonical name must therefore win over an old packaging label retained in
    # the rip contract (for example, "The Office Superfan Episodes S1").
    # Falling back to contract context made the dashboard's editable canonical
    # name misleading and could repeat the exact failed lookup.
    series_name = requested_series_name
    for media_id in selected:
        store.choose_review_path(media_id, "all_season_analysis_running")

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
                episode_range=episode_range,
                allow_gemini=request.confirm_external_fallback,
                # An ordinary disc pass may route true leftovers to bonus
                # analysis. An explicit scene-guided episode review must stay
                # within the episode candidates the user asked Gemini to assess.
                allow_content_fallback=not reviewer_scene_descriptions,
                reviewer_scene_descriptions=reviewer_scene_descriptions,
            )
        except Exception as exc:
            if isinstance(exc, GeminiAnalysisError):
                logger.error(
                    "All-season Gemini analysis failed safely: {}", exc.diagnostic
                )
                code = exc.review_code
            else:
                diagnostic = (
                    str(exc)
                    if isinstance(exc, PipelineQueueError) and str(exc)
                    else type(exc).__name__
                )
                logger.error("All-season disc analysis failed safely: {}", diagnostic)
                code = {
                    "No TV series matched the reviewed series name": (
                        "all_season_series_not_found"
                    ),
                    "TMDb returned no aired episodes for the resolved TV series": (
                        "all_season_catalog_unavailable"
                    ),
                    "Episode catalogue is unavailable for the reviewed scope": (
                        "all_season_catalog_unavailable"
                    ),
                    "Audio evidence collection failed before episode matching": (
                        "all_season_evidence_failed"
                    ),
                }.get(
                    str(exc),
                    "independent_episode_evidence_required"
                    if str(exc) == "Independent episode evidence requires review"
                    else "all_season_analysis_failed",
                )
            for media_id in selected:
                try:
                    if store.get(media_id).state == "review_required":
                        store.choose_review_path(media_id, code)
                except PipelineQueueError:
                    pass

    threading.Thread(target=run, name="all-season-disc-analysis", daemon=True).start()
    return {
        "started": True,
        "item_count": len(selected),
        "series_name": series_name,
        "reviewed_episode_range": (
            list(episode_range) if episode_range is not None else None
        ),
        "reviewer_scene_description_count": len(reviewer_scene_descriptions),
    }


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
                "episode_match_review",
                "unmatched_disc_analysis_required",
                "all_season_analysis_failed",
                "all_season_sequence_review_required",
                "independent_episode_evidence_required",
                "whole_disc_coherence_review_required",
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


def _retained_source_status(
    *,
    retained_path: str | None,
    retained_size: int | None,
    retained_at: str | None,
    contract_path: Path,
    ttl_days: int,
) -> str:
    if not isinstance(retained_path, str) or not isinstance(retained_size, int):
        return "missing"
    candidate = Path(retained_path).resolve()
    if not candidate.is_file() or candidate.stat().st_size != retained_size:
        return "missing"
    if ttl_days < 1:
        return "active"
    retained_time: datetime
    if isinstance(retained_at, str) and retained_at.strip():
        try:
            retained_time = datetime.fromisoformat(retained_at)
        except ValueError:
            retained_time = datetime.fromtimestamp(
                contract_path.stat().st_mtime, tz=UTC
            )
    else:
        retained_time = datetime.fromtimestamp(contract_path.stat().st_mtime, tz=UTC)
    if retained_time.tzinfo is None:
        retained_time = retained_time.replace(tzinfo=UTC)
    return (
        "active"
        if datetime.now(UTC) - retained_time <= timedelta(days=ttl_days)
        else "expired"
    )


def _retained_source_is_active(**kwargs) -> bool:
    return _retained_source_status(**kwargs) == "active"


def _start_default_player(source: Path) -> None:
    """Open one reviewed path through the supported local desktop boundary."""

    if os.name != "nt":
        raise PipelineQueueError("Default-player review is available only on Windows")
    os.startfile(source)  # type: ignore[attr-defined]  # noqa: S606


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
        _start_default_player(source)
    except (PipelineQueueError, OSError) as exc:
        raise HTTPException(
            status_code=409, detail="Review playback could not start"
        ) from exc
    return {"started": True, "media_id": media_id}


@router.post(
    "/pipeline/items/{media_id}/silent-video-ocr",
    response_model=SilentVideoOcrResponse,
)
def analyze_silent_pipeline_item(
    media_id: str,
    request: SilentVideoOcrRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
    inspector: Annotated[Callable, Depends(get_ffprobe_inspector)],
) -> dict[str, object]:
    """Run bounded local frame OCR for one exact audio-less failed MKV."""

    if request.confirm_media_read is not True:
        raise HTTPException(
            status_code=400, detail="OCR media-read confirmation is required"
        )
    try:
        item = store.get(media_id)
        if item.artifact.contract_sha256 != request.expected_artifact_sha256:
            raise PipelineQueueError("Pipeline item changed; refresh before OCR review")
        if item.state != "failed" or item.error_type != "HandBrakeNoUsableAudio":
            raise PipelineQueueError(
                "OCR review is available only for an audio-less HandBrake failure"
            )
        source, _size = _review_media_path(store, media_id)
        config = get_config_manager().load()
        if config.ffmpeg_path is None:
            raise PipelineQueueError("Configure FFmpeg in Settings before OCR review")
        tesseract_path = resolve_tesseract_path(config.tesseract_path)
        ffprobe = resolve_ffprobe_path(config.ffprobe_path)
        inspection = inspector(ffprobe, source, timeout_seconds=60)
        duration_seconds = float(inspection.media.duration_seconds)
        if duration_seconds <= 0:
            raise PipelineQueueError("The silent video duration could not be verified")
        evidence_root = contract_root.parent / "silent-video-evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)
        run_root = evidence_root / f"review-{uuid4().hex}"
        run_root.mkdir()
        result = collect_silent_video_review(
            media_id=media_id,
            media_path=source,
            duration_seconds=duration_seconds,
            output_root=run_root,
            ffmpeg_path=config.ffmpeg_path,
            tesseract_path=tesseract_path,
        )
        store.record_silent_video_review(media_id, result.category)
    except (
        FFprobeError,
        OSError,
        PipelineQueueError,
        SpecialFeatureEvidenceError,
    ) as exc:
        logger.warning("Silent-video OCR review stopped safely: {}", type(exc).__name__)
        detail = (
            str(exc)
            if isinstance(exc, PipelineQueueError)
            else "Silent-video OCR review failed safely"
        )
        raise HTTPException(status_code=409, detail=detail) from exc
    return {"media_id": media_id, **asdict(result)}


@router.post(
    "/pipeline/items/{media_id}/manual-episode-identification",
    response_model=PipelineItemResponse,
)
def apply_manual_episode_identification(  # noqa: C901 - guarded review boundary
    media_id: str,
    request: ManualEpisodeIdentificationRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict[str, object]:
    """Queue one user-reviewed TV identity without renaming its staged source."""

    if request.confirm_identification is not True:
        raise HTTPException(
            status_code=400, detail="Manual identification confirmation is required"
        )
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", request.new_name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned.casefold().endswith(".mkv"):
        raise HTTPException(
            status_code=400,
            detail="Enter a filename without an extension; .mkv is added automatically",
        )
    match = None
    if request.content_type == "episode":
        match = re.fullmatch(
            r"(.+?)\s+-\s+S(\d{1,2})E(\d{1,3})\s+-\s+(.+)",
            cleaned,
            flags=re.IGNORECASE,
        )
        if match is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Use: Series Name - S03E02 - Episode Title "
                    "(without the .mkv extension)"
                ),
            )
    try:
        item = store.get(media_id)
        if (
            item.stage != "identify"
            or item.state != "review_required"
            or item.review_code
            not in {
                "missing_season_context",
                "episode_match_review",
                "unmatched_disc_analysis_required",
                "all_season_analysis_failed",
                "all_season_sequence_review_required",
                "independent_episode_evidence_required",
                "whole_disc_coherence_review_required",
                "gemini_analysis_interrupted",
                "gemini_analysis_failed",
                "special_feature_manual_assignment_required",
                "gemini_descriptive_review_required",
                "catalogue_candidate_help_available",
            }
        ):
            raise PipelineQueueError(
                "Only a held unidentified title can use manual episode identification"
            )
        rip_artifact = store.rip_artifact(media_id)
        payload = json.loads(rip_artifact.contract_path.read_text(encoding="utf-8"))
        title_index = payload.get("title_index")
        if not isinstance(title_index, int) or isinstance(title_index, bool):
            raise PipelineQueueError("Verified rip title identity is unavailable")
        if request.evidence_source == "catalogue_candidate":
            candidate = _pipeline_catalogue_candidate_help(item)
            if (
                item.review_code != "catalogue_candidate_help_available"
                or request.content_type != "episode"
                or candidate is None
            ):
                raise PipelineQueueError(
                    "Catalogue candidate evidence is unavailable for this title"
                )
            expected_name = (
                f"{candidate['series_name']} - "
                f"S{int(candidate['season']):02d}E{int(candidate['episode']):02d} - "
                f"{candidate['title']}"
            )
            if cleaned.casefold() != expected_name.casefold():
                raise PipelineQueueError(
                    "The reviewed name does not match the displayed catalogue candidate"
                )
        if request.evidence_source == "provider_candidate":
            dossier = IdentificationDossierStore(
                contract_root.parent / "identification-evidence"
            )
            displayed = {
                f"{summary.get('candidate_series_name')} - "
                f"{summary.get('candidate_episode_id')} - "
                f"{summary.get('candidate_episode_title')}"
                for attempt in dossier.safe_attempts(media_id)
                if attempt.get("branch")
                in {"tv-local", "tv-opensubtitles", "tv-gemini"}
                and isinstance((summary := attempt.get("summary")), dict)
                and all(
                    isinstance(summary.get(key), str) and summary.get(key)
                    for key in (
                        "candidate_series_name",
                        "candidate_episode_id",
                        "candidate_episode_title",
                    )
                )
            }
            if (
                item.review_code != "gemini_descriptive_review_required"
                or request.content_type != "episode"
                or cleaned.casefold() not in {name.casefold() for name in displayed}
            ):
                raise PipelineQueueError(
                    "The reviewed name does not match a displayed provider candidate"
                )
        revised = dict(payload)
        context = dict(revised.get("media_context", {}))
        if request.content_type == "bonus":
            series_name = str(context.get("series_name") or "").strip()
            if not series_name or series_name.casefold() in {"unmatched", "unknown"}:
                raise PipelineQueueError(
                    "Canonical TV series context is required for a bonus title"
                )
            context.update(
                episode_assignments=[],
                special_feature_library_title=series_name,
                special_feature_assignments=[
                    {
                        "title_index": title_index,
                        "classification": "matched-feature",
                        "fallback_name_policy": "none",
                        "media_kind": "extra",
                        "library_kind": "tv",
                        "jellyfin_folder": "Extras",
                        "matched_title": cleaned,
                        "user_reviewed_name": True,
                    }
                ],
                episode_assignment_source="manual-bonus-review",
                identification_policy_version=2,
            )
        else:
            assert match is not None
            series_name = re.sub(r"\s+", " ", match.group(1)).strip(" .")
            season = int(match.group(2))
            episode = int(match.group(3))
            title = re.sub(r"\s+", " ", match.group(4)).strip(" .")
            if (
                not series_name
                or not title
                or not 0 <= season <= 99
                or not 1 <= episode <= 999
            ):
                raise PipelineQueueError("Manual episode identity is invalid")
            context.update(
                series_name=series_name,
                season=season,
                episode_assignments=[
                    {
                        "title_index": title_index,
                        "season": season,
                        "episode": episode,
                        "title": title,
                        "user_reviewed_name": True,
                    }
                ],
                special_feature_assignments=[],
                episode_assignment_source=(
                    "ripweaver-catalogue-help-reviewed"
                    if request.evidence_source == "catalogue_candidate"
                    else "provider-candidate-reviewed"
                    if request.evidence_source == "provider_candidate"
                    else "manual-playback-review"
                ),
                identification_policy_version=2,
            )
        revised["media_context"] = context
        if not contract_root.is_dir():
            raise PipelineQueueError("Pipeline contract root is unavailable")
        path = contract_root / (
            f"{media_id}.manual-identification-{uuid4().hex[:12]}.json"
        )
        path.write_text(
            json.dumps(revised, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        updated = store.apply_reviewed_identification_input(
            media_id, build_artifact("rip", path)
        )
        store.set_paused(False)
    except (OSError, json.JSONDecodeError, PipelineQueueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _pipeline_item_response(updated)


_PIPELINE_DISC_TITLE_ID = re.compile(
    r"--disc-\d+-([0-9a-f]{16})-title-(\d{3})(?:-|$)", re.IGNORECASE
)


def _disc_range_elimination_candidate(  # noqa: C901 - explicit safety checks
    media_id: str,
    candidate_episode_id: str,
    store: PipelineQueueStore,
    dossier: IdentificationDossierStore,
) -> tuple[str, int, int, str]:
    """Validate one provider candidate as the sole range-compatible episode."""

    identity = _PIPELINE_DISC_TITLE_ID.search(media_id)
    candidate_id = re.fullmatch(r"S(\d{2})E(\d{2,3})", candidate_episode_id)
    if identity is None or candidate_id is None:
        raise PipelineQueueError("Disc episode identity is unavailable")
    fingerprint = identity.group(1).casefold()
    current_title_index = int(identity.group(2))
    season = int(candidate_id.group(1))
    episode = int(candidate_id.group(2))

    provider_candidates = []
    for attempt in dossier.safe_attempts(media_id):
        summary = attempt.get("summary")
        if (
            attempt.get("branch") == "tv-opensubtitles"
            and isinstance(summary, dict)
            and summary.get("candidate_episode_id") == candidate_episode_id
            and isinstance(summary.get("candidate_series_name"), str)
            and isinstance(summary.get("candidate_episode_title"), str)
        ):
            provider_candidates.append(summary)
    if not provider_candidates:
        raise PipelineQueueError(
            "The correction is not a displayed subtitle-evidence candidate"
        )
    provider = provider_candidates[-1]
    series_name = str(provider["candidate_series_name"]).strip()
    episode_title = str(provider["candidate_episode_title"]).strip()
    if not series_name or not episode_title:
        raise PipelineQueueError("The correction candidate metadata is incomplete")

    history = store.catalogue_title_history(fingerprint)
    observed_title_indexes: set[int] = set()
    independent_anchors: set[tuple[int, int]] = set()
    for sibling in store.list_items():
        sibling_identity = _PIPELINE_DISC_TITLE_ID.search(sibling.media_id)
        if (
            sibling_identity is None
            or sibling_identity.group(1).casefold() != fingerprint
        ):
            continue
        sibling_title_index = int(sibling_identity.group(2))
        observed_title_indexes.add(sibling_title_index)
        if sibling_title_index == current_title_index:
            continue
        outcome = history.get(sibling_title_index, {})
        anchor_episode_id = outcome.get("episode_id")
        if (
            not isinstance(anchor_episode_id, str)
            or outcome.get("series_name", "").casefold() != series_name.casefold()
        ):
            continue
        anchor_match = re.fullmatch(r"S(\d{2})E(\d{2,3})", anchor_episode_id)
        if anchor_match is None:
            continue
        independently_matched = any(
            attempt.get("branch") == "tv-opensubtitles"
            and attempt.get("disposition") == "matched"
            and isinstance((summary := attempt.get("summary")), dict)
            and summary.get("candidate_episode_id") == anchor_episode_id
            and isinstance(summary.get("best_score"), int | float)
            and not isinstance(summary.get("best_score"), bool)
            and float(summary["best_score"]) >= 0.70
            for attempt in dossier.safe_attempts(sibling.media_id)
        )
        if independently_matched:
            independent_anchors.add((
                int(anchor_match.group(1)),
                int(anchor_match.group(2)),
            ))

    expected_indexes = store.expected_title_indexes_for_disc(fingerprint)
    disc_title_count = len(expected_indexes or tuple(sorted(observed_title_indexes)))
    anchor_episodes = sorted(
        anchor_episode
        for anchor_season, anchor_episode in independent_anchors
        if anchor_season == season
    )
    if len(anchor_episodes) < 2 or disc_title_count < len(anchor_episodes):
        raise PipelineQueueError("Independent same-disc range evidence is incomplete")
    anchor_minimum = min(anchor_episodes)
    anchor_maximum = max(anchor_episodes)
    if anchor_maximum - anchor_minimum + 1 > disc_title_count:
        raise PipelineQueueError("Independent same-disc matches do not form one range")
    minimum_episode = max(1, anchor_maximum - disc_title_count + 1)
    maximum_episode = anchor_minimum + disc_title_count - 1
    if not minimum_episode <= episode <= maximum_episode:
        raise PipelineQueueError("The correction candidate is outside the disc range")
    assigned = store.assigned_series_episodes(series_name)
    remaining = tuple(
        value
        for value in range(minimum_episode, maximum_episode + 1)
        if (season, value) not in assigned
    )
    if remaining != (episode,):
        raise PipelineQueueError(
            "The correction candidate is not the sole remaining episode in range"
        )
    return series_name, season, episode, episode_title


@router.post(
    "/pipeline/items/{media_id}/correct-episode-identification",
    response_model=PipelineItemResponse,
)
def correct_pipeline_episode_identification(
    media_id: str,
    request: CorrectEpisodeIdentificationRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict[str, object]:
    """Correct one held wrong match without reading or changing encoded media."""

    if request.confirm_correction is not True:
        raise HTTPException(
            status_code=400, detail="Exact episode correction confirmation is required"
        )
    try:
        item = store.get(media_id)
        if (
            item.state != "review_required"
            or item.stage != "organize"
            or item.review_code != "library_collision"
            or item.artifact.contract_sha256 != request.expected_artifact_sha256
        ):
            raise PipelineQueueError("Held episode collision changed; review it again")
        dossier = IdentificationDossierStore(
            contract_root.parent / "identification-evidence"
        )
        series_name, season, episode, title = _disc_range_elimination_candidate(
            media_id,
            request.candidate_episode_id,
            store,
            dossier,
        )
        current = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
        current_relative = Path(
            str(current.get("library_relative", "")).replace("/", "\\")
        )
        if len(current_relative.parts) < 3 or (
            current_relative.parts[0].casefold() != series_name.casefold()
        ):
            raise PipelineQueueError("Correction candidate belongs to another series")
        cleaned_series = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", series_name)
        cleaned_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title)
        cleaned_series = re.sub(r"\s+", " ", cleaned_series).strip(" .")
        cleaned_title = re.sub(r"\s+", " ", cleaned_title).strip(" .")
        if not cleaned_series or not cleaned_title:
            raise PipelineQueueError("Correction candidate filename is invalid")
        display_title = (
            f"{cleaned_series} - S{season:02d}E{episode:02d} - {cleaned_title}"
        )
        revised = dict(current)
        revised.update(
            episode_id=f"S{season:02d}E{episode:02d}",
            display_title=display_title,
            library_relative=PurePosixPath(
                cleaned_series,
                f"Season {season:02d}",
                f"{display_title}.mkv",
            ).as_posix(),
            identification_order=["disc-range-elimination-reviewed"],
            assignment_evidence_source="disc_range_elimination",
            identification_policy_version=(AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION),
            user_reviewed_name=True,
        )
        if not contract_root.is_dir():
            raise PipelineQueueError("Pipeline contract root is unavailable")
        contract = contract_root / (
            f"{media_id}.transcode.episode-correction-{uuid4().hex[:12]}.json"
        )
        contract.write_text(
            json.dumps(revised, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        updated = store.correct_held_episode_identification(
            media_id,
            build_artifact("transcode", contract),
            expected_artifact_sha256=request.expected_artifact_sha256,
        )
    except (OSError, json.JSONDecodeError, PipelineQueueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _pipeline_item_response(updated)


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
    store: PipelineQueueStore,
    media_ids: list[str],
    deletion_root: Path | None,
    *,
    require_expired: bool = False,
) -> tuple[str, list[tuple[str, Path, int]]]:
    if deletion_root is None or not deletion_root.is_dir():
        raise PipelineQueueError("Configure an existing staging-for-deletion root")
    config = get_config_manager().load()
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
            retained_at = payload.get("archived_source_retained_at")
            path.relative_to(root)
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise PipelineQueueError("Retained source contract is unavailable") from exc
        retained_status = _retained_source_status(
            retained_path=str(path),
            retained_size=size,
            retained_at=retained_at if isinstance(retained_at, str) else None,
            contract_path=item.artifact.contract_path,
            ttl_days=config.retained_source_ttl_days,
        )
        if retained_status == "missing":
            raise PipelineQueueError("Retained source is missing or changed")
        if require_expired and retained_status != "expired":
            raise PipelineQueueError("Retained source has not reached its TTL")
        entries.append((media_id, path, size))
    identity = json.dumps(
        [
            (media_id, path.relative_to(root).as_posix(), size)
            for media_id, path, size in entries
        ],
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(identity).hexdigest(), entries


def _expired_retained_source_ids(store: PipelineQueueStore, ttl_days: int) -> list[str]:
    expired: list[str] = []
    for item in store.list_items():
        if item.state != "completed" or item.stage != "organize":
            continue
        try:
            payload = json.loads(
                item.artifact.contract_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        if (
            _retained_source_status(
                retained_path=payload.get("archived_source_path"),
                retained_size=payload.get("archived_source_size_bytes"),
                retained_at=payload.get("archived_source_retained_at"),
                contract_path=item.artifact.contract_path,
                ttl_days=ttl_days,
            )
            == "expired"
        ):
            expired.append(item.media_id)
    return sorted(expired)


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
                archived_source_path=str(destination),
                archived_source_size_bytes=size,
                archived_source_retained_at=datetime.now(UTC).isoformat(),
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


@router.get("/pipeline/retained-sources/expired")
def get_expired_retained_sources(
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Return an exact, path-redacted cleanup proposal for TTL-expired originals."""

    config = get_config_manager().load()
    media_ids = _expired_retained_source_ids(store, config.retained_source_ttl_days)
    postponed_until = config.retained_source_cleanup_postponed_until
    postponed = False
    if postponed_until:
        try:
            value = datetime.fromisoformat(postponed_until)
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            postponed = value.astimezone(UTC) > datetime.now(UTC)
        except ValueError:
            postponed_until = None
    if not media_ids:
        return {
            "cleanup_due": False,
            "postponed": postponed,
            "postponed_until": postponed_until,
            "ttl_days": config.retained_source_ttl_days,
            "media_ids": [],
            "file_count": 0,
            "total_size_bytes": 0,
            "plan_sha256": None,
            "jellyfin_files_affected": 0,
        }
    try:
        digest, entries = _retained_source_plan(
            store,
            media_ids,
            config.deletion_staging_root,
            require_expired=True,
        )
    except PipelineQueueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "cleanup_due": not postponed,
        "postponed": postponed,
        "postponed_until": postponed_until,
        "ttl_days": config.retained_source_ttl_days,
        "media_ids": media_ids,
        "file_count": len(entries),
        "total_size_bytes": sum(entry[2] for entry in entries),
        "plan_sha256": digest,
        "jellyfin_files_affected": 0,
    }


@router.post("/pipeline/retained-sources/postpone-cleanup")
def postpone_expired_retained_source_cleanup(
    request: PostponeRetainedSourceCleanupRequest,
) -> dict[str, object]:
    """Persist a bounded postponement for the next TTL cleanup prompt."""

    manager = get_config_manager()
    config = manager.load()
    postponed_until = datetime.now(UTC) + timedelta(days=request.postpone_days)
    manager.save(
        config.model_copy(
            update={
                "retained_source_cleanup_postponed_until": postponed_until.isoformat()
            }
        )
    )
    return {
        "postponed": True,
        "postponed_until": postponed_until.isoformat(),
        "postpone_days": request.postpone_days,
    }


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
    manager = get_config_manager()
    config = manager.load()
    if config.retained_source_cleanup_postponed_until is not None:
        manager.save(
            config.model_copy(update={"retained_source_cleanup_postponed_until": None})
        )
    return {"deleted_file_count": len(entries), "jellyfin_files_affected": 0}


def _retained_reencode_verified_payload(
    store: PipelineQueueStore,
    media_id: str,
    new_id: str,
    retained: Path,
    size: int,
) -> dict[str, object]:
    original_rip = store.rip_artifact(media_id)
    verified = json.loads(original_rip.contract_path.read_text(encoding="utf-8"))
    fingerprint = verified.get("disc_fingerprint")
    title_index = verified.get("title_index")
    if not isinstance(fingerprint, str):
        match = re.search(r"-([0-9a-f]{16})-title-", media_id)
        fingerprint = match.group(1) if match is not None else None
    if not isinstance(title_index, int) or isinstance(title_index, bool):
        match = re.search(r"-title-(\d{3})(?:-|$)", media_id)
        title_index = int(match.group(1)) if match is not None else None
    if (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{16}", fingerprint) is None
        or title_index is None
    ):
        raise PipelineQueueError("Saved verified-rip identity is unavailable")
    verified.update(
        media_id=new_id,
        disc_fingerprint=fingerprint,
        title_index=title_index,
        source_path=str(retained),
        source_size_bytes=size,
    )
    return verified


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
            transcode_path = contract_root / f"{media_id}.transcode.json"
            transcoded = json.loads(transcode_path.read_text(encoding="utf-8"))
            if transcoded.get("mode") != "verified-transcode-contract":
                raise PipelineQueueError("Saved matched-name contract is unavailable")
            attempt = 1
            while True:
                new_id = f"{media_id}-reencode-{attempt:03d}"
                new_path = contract_root / f"{new_id}.identify.json"
                new_rip_path = contract_root / f"{new_id}.verified-rip.json"
                if not new_path.exists() and not new_rip_path.exists():
                    break
                attempt += 1
            identified = {
                "schema_version": 1,
                "mode": "identified-episode-contract",
                "media_id": new_id,
                "source_path": str(retained),
                "source_size_bytes": size,
                "library_relative": transcoded.get("library_relative"),
                "episode_id": transcoded.get("episode_id"),
                "confidence": 1.0,
                "existing_output_policy": transcoded.get(
                    "existing_output_policy", "preserve"
                ),
                "provisional_match": bool(transcoded.get("provisional_match")),
                "gemini_confidence": transcoded.get("gemini_confidence"),
            }
            if request.profile_id is not None:
                identified["handbrake_profile_id"] = request.profile_id
            elif transcoded.get("handbrake_profile_id") is not None:
                identified["handbrake_profile_id"] = transcoded["handbrake_profile_id"]

            verified = _retained_reencode_verified_payload(
                store, media_id, new_id, retained, size
            )
            new_path.write_text(
                json.dumps(identified, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            new_rip_path.write_text(
                json.dumps(verified, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            store.enqueue_reencode(
                new_id,
                build_artifact("identify", new_path),
                build_artifact("rip", new_rip_path),
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
def verify_existing_rip_candidates(  # noqa: C901 - per-file recovery isolation
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
        verified_jobs = []
        verified_candidates = []
        rejected_title_indexes = []
        now = datetime.now(UTC).isoformat()
        candidates_by_job = {candidate.job_id: candidate for candidate in candidates}
        for job in jobs:
            candidate = candidates_by_job[job.job_id]
            source = (
                binding.output_root / candidate.relative_parent / candidate.basename
            )
            try:
                inspection = inspector(
                    executable, source, timeout_seconds=request.timeout_seconds
                )
                valid = (
                    inspection.media.duration_seconds > 0
                    and source.stat().st_size == candidate.size_bytes
                )
            except (FFprobeError, OSError):
                valid = False
            if not valid:
                rejected_title_indexes.append(job.title_index)
                continue
            verified_jobs.append(job)
            verified_candidates.append(candidate)
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
        if not verified_jobs:
            raise RipError("No selected existing rip passed read-only verification")
        contract_root.mkdir(parents=True, exist_ok=True)
        queued = enqueue_verified_rip_results(
            pipeline_store,
            jobs=tuple(verified_jobs),
            results=results,
            output_root=binding.output_root,
            contract_root=contract_root,
            media_contexts=binding.media_contexts,
            identity_overrides={
                job.job_id: (fingerprint.group(1), job.title_index)
                for job in verified_jobs
                if (
                    fingerprint := re.search(
                        r"-([0-9a-f]{16})-title-\d{3}\.mkv$",
                        next(
                            original.output_basename or ""
                            for original in manifest.jobs
                            if original.job_id == job.job_id
                        ),
                    )
                )
            },
            repair_recovered_identity=True,
            media_id_overrides={
                job.job_id: _existing_rip_recovery_media_id(candidate)
                for job, candidate in zip(
                    verified_jobs, verified_candidates, strict=True
                )
            },
        )
        return {
            "verified_count": len(queued),
            "verified_title_indexes": [job.title_index for job in verified_jobs],
            "rejected_title_indexes": sorted(rejected_title_indexes),
            "queued_for_identification": True,
        }
    except (RipError, PipelineQueueError, FFprobeError, OSError) as exc:
        logger.warning(
            "Existing-rip verification stopped safely: {}", type(exc).__name__
        )
        detail = (
            "The selected MKV verified, but its recovery record conflicts with an "
            "earlier queue contract. Refresh the recovery choices and try again."
            if isinstance(exc, PipelineQueueError)
            else str(exc)
            if isinstance(exc, RipError)
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
    cancel_startup_queue_resume()
    store.set_paused(True)
    return get_pipeline_items(store)


@router.get("/pipeline/transcode/preview")
def preview_transcode_authorization(
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    profiles: Annotated[HandBrakeProfileStore, Depends(get_handbrake_profile_store)],
    profile_id: str | None = None,
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
            allow_version_coexistence=config.automatic_organization_enabled,
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
            allow_version_coexistence=config.automatic_organization_enabled,
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
    watcher: Annotated[DriveWatcher, Depends(get_drive_watcher)],
) -> dict[str, object]:
    if request.confirm_control is not True:
        raise HTTPException(
            status_code=400, detail="Pipeline resume confirmation is required"
        )
    cancel_startup_queue_resume()
    store.set_paused(False)
    config = get_config_manager().load()
    if config.automatic_processing_enabled:
        from mkv_episode_matcher.backend.automatic_rip import observe_automatic_drives

        observe_automatic_drives(
            watcher.snapshot(), enabled=True, processing_paused=False
        )
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


@router.post(
    "/pipeline/items/{media_id}/restart-identification",
    response_model=PipelineItemResponse,
)
def restart_placeholder_identification(
    media_id: str,
    request: PipelineControlRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Return one rejected placeholder contract to its verified-rip input."""

    if request.confirm_control is not True:
        raise HTTPException(
            status_code=400,
            detail="Identification restart confirmation is required",
        )
    try:
        item = store.get(media_id)
        if (
            item.stage not in {"transcode", "organize"}
            or item.state != "review_required"
            or item.review_code != "placeholder_identification_required"
        ):
            raise PipelineQueueError(
                "Only a held placeholder episode can restart identification here"
            )
        rip_artifact = store.rip_artifact(media_id)
        payload = json.loads(rip_artifact.contract_path.read_text(encoding="utf-8"))
        fingerprint = payload.get("disc_fingerprint")
        title_index = payload.get("title_index")
        if (
            not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{16}", fingerprint)
            or not isinstance(title_index, int)
            or isinstance(title_index, bool)
        ):
            raise PipelineQueueError(
                "Verified rip identity is unavailable for identification restart"
            )
        restarted = store.restart_identification(
            media_id,
            expected_disc_fingerprint=fingerprint,
            expected_title_index=title_index,
        )
        store.set_paused(False)
        return _pipeline_item_response(restarted)
    except (OSError, json.JSONDecodeError, PipelineQueueError) as exc:
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


def _collision_file_details(path: Path, inspection) -> CollisionFileDetails:
    stat = path.stat()
    media = inspection.media
    overall_bitrate = media.overall_bit_rate
    overall_bitrate_source = "reported" if overall_bitrate is not None else None
    if overall_bitrate is None and media.duration_seconds > 0:
        overall_bitrate = round((stat.st_size * 8) / media.duration_seconds)
        overall_bitrate_source = "size-duration"
    return CollisionFileDetails(
        modified_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
        size_bytes=stat.st_size,
        container=media.container,
        video_codec=media.video_codec,
        audio_codecs=sorted({
            stream.codec for stream in media.audio_streams if stream.codec
        }),
        width=media.video_width,
        height=media.video_height,
        field_order=media.video_field_order,
        duration_seconds=media.duration_seconds,
        overall_bitrate_bps=overall_bitrate,
        overall_bitrate_source=overall_bitrate_source,
        video_bitrate_bps=media.video_bit_rate,
        frame_rate_fps=media.video_frame_rate,
        video_profile=media.video_profile,
        pixel_format=media.video_pixel_format,
        bit_depth=media.video_bit_depth,
        hdr_format=media.video_hdr_format,
        color_primaries=media.video_color_primaries,
        color_transfer=media.video_color_transfer,
        color_space=media.video_color_space,
        color_range=media.video_color_range,
        video_encoder=media.video_encoder,
        format_encoder=media.format_encoder,
        audio_tracks=[
            CollisionAudioTrackDetails(
                index=stream.index,
                codec=stream.codec,
                language=stream.language,
                title=stream.title,
                channels=stream.channels,
                channel_layout=stream.channel_layout,
                bitrate_bps=stream.bit_rate,
                sample_rate_hz=stream.sample_rate,
                default=stream.is_default,
                commentary=stream.is_commentary,
            )
            for stream in media.audio_streams
        ],
    )


def _held_library_collision_paths(
    store: PipelineQueueStore,
    media_id: str,
    expected_artifact_sha256: str,
) -> tuple[Path, Path, object]:
    item = store.get(media_id)
    if (
        item.state != "review_required"
        or item.review_code != "library_collision"
        or item.artifact.contract_sha256 != expected_artifact_sha256
    ):
        raise PipelineQueueError("Held collision changed; review it again")
    if item.stage != "organize":
        raise PipelineQueueError(
            "File review is available after the new encode is verified"
        )
    payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
    encoded = Path(str(payload["encoded_path"])).resolve()
    expected_size = int(payload["encoded_size_bytes"])
    if not encoded.is_file() or encoded.stat().st_size != expected_size:
        raise PipelineQueueError("Verified encode is missing or changed")

    config = get_config_manager().load()
    root, planned = _collision_destination(payload, config)
    episode_id = payload.get("episode_id")
    if isinstance(episode_id, str):
        status, names = inspect_episode_destination(
            planned.parent, planned.name, episode_id
        )
        if (
            status
            not in {
                "review-existing-destination",
                "review-existing-episode",
            }
            or len(names) != 1
        ):
            raise PipelineQueueError(
                "The Jellyfin collision changed or requires manual review"
            )
        existing = (planned.parent / names[0]).resolve()
    else:
        existing = planned
    existing.relative_to(root)
    if not existing.is_file():
        raise PipelineQueueError("The reviewed Jellyfin file no longer exists")
    return encoded, existing, config


@router.post("/pipeline/items/{media_id}/play-library-collision")
def play_pipeline_library_collision_file(
    media_id: str,
    request: PlayLibraryCollisionFileRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
) -> dict[str, object]:
    """Open one explicitly selected side of a held collision for review."""

    if not request.confirm_play:
        raise HTTPException(status_code=400, detail="Playback confirmation is required")
    try:
        encoded, existing, _config = _held_library_collision_paths(
            store, media_id, request.expected_artifact_sha256
        )
        source = encoded if request.target == "new-encode" else existing
        _start_default_player(source)
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        PipelineQueueError,
    ) as exc:
        raise HTTPException(
            status_code=409, detail="Collision playback could not start"
        ) from exc
    return {"started": True, "media_id": media_id, "target": request.target}


@router.post(
    "/pipeline/items/{media_id}/library-collision-comparison",
    response_model=LibraryCollisionComparisonResponse,
)
def inspect_pipeline_library_collision(
    media_id: str,
    request: InspectLibraryCollisionRequest,
    store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    inspector: Annotated[Callable, Depends(get_ffprobe_inspector)],
) -> LibraryCollisionComparisonResponse:
    """Read metadata for the exact two files in one held collision."""

    if not request.confirm_media_read:
        raise HTTPException(
            status_code=400,
            detail="Exact collision media-read confirmation is required",
        )
    try:
        encoded, existing, config = _held_library_collision_paths(
            store, media_id, request.expected_artifact_sha256
        )

        ffprobe = resolve_ffprobe_path(config.ffprobe_path)
        before = [
            (path.stat().st_size, path.stat().st_mtime_ns)
            for path in (encoded, existing)
        ]
        new_inspection = inspector(ffprobe, encoded, timeout_seconds=60)
        old_inspection = inspector(ffprobe, existing, timeout_seconds=60)
        after = [
            (path.stat().st_size, path.stat().st_mtime_ns)
            for path in (encoded, existing)
        ]
        if before != after:
            raise PipelineQueueError("A compared file changed during inspection")
        new_details = _collision_file_details(encoded, new_inspection)
        old_details = _collision_file_details(existing, old_inspection)
        return LibraryCollisionComparisonResponse(
            media_id=media_id,
            new_pipeline_file=new_details,
            existing_jellyfin_file=old_details,
            size_difference_bytes=new_details.size_bytes - old_details.size_bytes,
        )
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        FFprobeError,
        PipelineQueueError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
        "gemini_analysis_interrupted",
        "gemini_analysis_failed",
        "gemini_audio_evidence_insufficient",
        "gemini_catalog_unavailable",
        "gemini_provider_failed",
        "gemini_credential_rejected",
        "gemini_rate_limited",
        "gemini_provider_unavailable",
        "gemini_request_rejected",
        "gemini_network_failed",
        "gemini_response_invalid",
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
