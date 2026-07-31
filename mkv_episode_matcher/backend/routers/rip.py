"""Read-only rip orchestration preview routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from mkv_episode_matcher.backend.control_access import require_local_control
from mkv_episode_matcher.backend.dependencies import (
    get_orchestration_store,
    get_pipeline_contract_root,
    get_pipeline_queue_store,
    get_private_binding_store,
    get_rip_execution_registry,
    get_rip_queue_runner,
    get_special_feature_queue_runner,
)
from mkv_episode_matcher.backend.rip_runtime import RipExecutionRegistry
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.disc.rip_dispatcher import RipDispatcher
from mkv_episode_matcher.disc.rip_execution_adapter import (
    ProductionRipExecutor,
    RipExecutionOptions,
)
from mkv_episode_matcher.disc.rip_manifest import MediaContext
from mkv_episode_matcher.disc.rip_preview import RipPreview, build_rip_preview
from mkv_episode_matcher.disc.ripper import RipError
from mkv_episode_matcher.disc.special_feature_binder import (
    SpecialFeatureBindError,
    load_bound_special_feature_manifest,
)
from mkv_episode_matcher.disc.special_feature_executor import (
    execute_bound_special_feature_manifest,
)
from mkv_episode_matcher.pipeline_queue import (
    PipelineQueueError,
    PipelineQueueStore,
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


class RipPreviewJobResponse(BaseModel):
    job_id: str
    drive_index: int
    title_index: int
    estimated_bytes: int | None
    staging_destination: str
    final_destination: str | None
    collision_status: str


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


class AuthorizeJobRequest(BaseModel):
    expected_plan_sha256: str
    confirm_authorization: bool = False


class StartJobRequest(BaseModel):
    confirm_queue: bool = False


class ExecuteJobRequest(BaseModel):
    expected_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_job_count: int = Field(ge=1)
    makemkv_executable: str = Field(min_length=1)
    run_directory: str = Field(min_length=1)
    timeout_seconds: int = Field(default=7200, ge=60, le=86400)
    max_drives: int | None = Field(default=None, ge=1, le=16)
    confirm_execute: bool = False


class ControlJobRequest(BaseModel):
    confirm_control: bool = False


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


def _pipeline_item_response(item) -> dict[str, object]:
    return {
        "media_id": item.media_id,
        "state": item.state,
        "stage": item.stage,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "error_type": item.error_type,
        "review_code": item.review_code,
    }


def _job_response(job) -> dict[str, object]:
    return asdict(job)


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
        return _job_response(store.get_job(job_id))
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
        "jobs": [_job_response(job) for job in store.list_jobs()],
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

        executor = ProductionRipExecutor(
            RipExecutionOptions(
                makemkv_executable=Path(request.makemkv_executable),
                run_directory=Path(request.run_directory),
                timeout_seconds=request.timeout_seconds,
                max_drives=request.max_drives,
            ),
            queue_runner=queue_runner,
            completion_sink=enqueue_results,
        )
        registry.attach(job_id, Path(request.run_directory))
        try:
            completed = RipDispatcher(store, private_store).dispatch(
                job_id,
                dispatch_key=idempotency_key,
                executor=executor,
            )
        finally:
            registry.detach(job_id)
        return _job_response(completed)
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
