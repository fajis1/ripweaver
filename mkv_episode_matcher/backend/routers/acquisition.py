"""Loopback-only whole-disc acquisition control API."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mkv_episode_matcher.backend.control_access import require_local_control
from mkv_episode_matcher.backend.dependencies import (
    get_image_acquisition_executor,
    get_image_acquisition_store,
    get_private_acquisition_binding_store,
)
from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.disc.image_acquisition import DiscImagePlan, plan_disc_image
from mkv_episode_matcher.disc.image_acquisition_bindings import (
    PrivateAcquisitionBindingStore,
)
from mkv_episode_matcher.disc.image_acquisition_store import ImageAcquisitionStore
from mkv_episode_matcher.disc.ripper import RipError

router = APIRouter(
    prefix="/rip/acquisitions",
    tags=["rip"],
    dependencies=[Depends(require_local_control)],
)

AcquisitionStoreDep = Annotated[
    ImageAcquisitionStore, Depends(get_image_acquisition_store)
]
AcquisitionBindingsDep = Annotated[
    PrivateAcquisitionBindingStore, Depends(get_private_acquisition_binding_store)
]
AcquisitionExecutorDep = Annotated[object, Depends(get_image_acquisition_executor)]


class PlanRequest(BaseModel):
    drive_index: int = Field(ge=0, le=99)
    media_kind: Literal["dvd", "bluray"]
    estimated_bytes: int = Field(gt=0)
    drive_letter: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=128)


class TransitionRequest(BaseModel):
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)


class ExecuteRequest(TransitionRequest):
    authorized_acquisition_count: int
    confirm_acquisition: bool
    timeout_seconds: int = Field(default=14400, ge=60, le=86400)


def _public(job) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "plan_sha256": job.plan_sha256,
        "plan": job.plan,
        "state": job.state,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


@router.post("")
def create_acquisition(
    request: PlanRequest,
    store: AcquisitionStoreDep,
    bindings: AcquisitionBindingsDep,
):
    config = get_config_manager().load()
    executable = (
        config.disc_image_creator_path
        if request.media_kind == "dvd"
        else config.makemkv_path
    )
    try:
        if executable is None or config.disc_image_root is None:
            raise RipError("Disc-image tool and staging root must be configured")
        if request.media_kind == "dvd" and request.drive_letter is None:
            raise RipError("DVD acquisition requires the privately bound drive letter")
        plan = plan_disc_image(
            drive_index=request.drive_index,
            media_kind=request.media_kind,
            estimated_bytes=request.estimated_bytes,
        )
        job = store.create(plan, idempotency_key=request.idempotency_key)
        bindings.bind(
            job_id=job.job_id,
            plan_sha256=job.plan_sha256,
            executable=executable,
            image_root=config.disc_image_root,
            drive_letter=request.drive_letter,
        )
        return _public(job)
    except RipError as exc:
        raise _error(exc) from exc


def _transition(job_id: str, request: TransitionRequest, target: str, store):
    try:
        return _public(
            store.transition(
                job_id,
                target,
                idempotency_key=request.idempotency_key,
                expected_plan_sha256=request.plan_sha256,
            )
        )
    except RipError as exc:
        raise _error(exc) from exc


@router.post("/{job_id}/authorize")
def authorize(job_id: str, request: TransitionRequest, store: AcquisitionStoreDep):
    return _transition(job_id, request, "authorized", store)


@router.post("/{job_id}/queue")
def queue(job_id: str, request: TransitionRequest, store: AcquisitionStoreDep):
    return _transition(job_id, request, "queued", store)


@router.get("/{job_id}")
def status(job_id: str, store: AcquisitionStoreDep):
    try:
        return _public(store.get(job_id))
    except RipError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{job_id}/execute")
def execute(
    job_id: str,
    request: ExecuteRequest,
    store: AcquisitionStoreDep,
    bindings: AcquisitionBindingsDep,
    executor: AcquisitionExecutorDep,
):
    try:
        job = store.transition(
            job_id,
            "running",
            idempotency_key=request.idempotency_key,
            expected_plan_sha256=request.plan_sha256,
        )
        binding = bindings.get(job_id)
        if binding.plan_sha256 != job.plan_sha256:
            raise RipError("Private acquisition binding digest does not match")
        plan = DiscImagePlan(**job.plan)
        result = executor(
            plan,
            executable=binding.executable,
            image_root=binding.image_root,
            drive_letter=binding.drive_letter,
            authorized_plan_sha256=request.plan_sha256,
            authorized_acquisition_count=request.authorized_acquisition_count,
            confirm_acquisition=request.confirm_acquisition,
            timeout_seconds=request.timeout_seconds,
        )
        finished = store.transition(
            job_id,
            "verified",
            idempotency_key=f"{request.idempotency_key}:verified",
            expected_plan_sha256=request.plan_sha256,
        )
        response = _public(finished)
        response["result"] = {
            "acquisition_id": result.acquisition_id,
            "output_bytes": result.output_bytes,
            "local_source_verified": True,
        }
        return response
    except RipError as exc:
        try:
            current = store.get(job_id)
            if current.state == "running":
                store.transition(
                    job_id,
                    "failed",
                    idempotency_key=f"{request.idempotency_key}:failed",
                    expected_plan_sha256=request.plan_sha256,
                )
        except RipError:
            pass
        raise _error(exc) from exc
