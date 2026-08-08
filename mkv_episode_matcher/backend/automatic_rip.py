"""Server-side automatic rip dispatch for newly inserted optical discs."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import HTTPException
from loguru import logger

from mkv_episode_matcher.disc.content_policy import infer_release_name_from_disc_label
from mkv_episode_matcher.disc.drive_watcher import DriveStatusSnapshot
from mkv_episode_matcher.disc.ripper import RipError

_downstream_lock = threading.Lock()


def _preview_fingerprints(preview: dict[str, object]) -> frozenset[str]:
    """Return redacted inventory fingerprints carried by preview destinations."""

    fingerprints: set[str] = set()
    jobs = preview.get("jobs", [])
    if not isinstance(jobs, list | tuple):
        return frozenset()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        destination = job.get("staging_destination")
        if not isinstance(destination, str):
            continue
        for part in PurePosixPath(destination.replace("\\", "/")).parts:
            if len(part) == 16 and all(char in "0123456789abcdef" for char in part):
                fingerprints.add(part)
    return frozenset(fingerprints)


def _has_prior_disc_work(store, prepared_job) -> bool:
    """Refuse automatic rerips when this exact inventory is already durable."""

    fingerprints = _preview_fingerprints(prepared_job.preview)
    if not fingerprints:
        return False
    return any(
        job.job_id != prepared_job.job_id
        and bool(fingerprints & _preview_fingerprints(job.preview))
        and job.state != "cancelled"
        for job in store.list_jobs(limit=200)
    )


class AutomaticRipCoordinator:
    """Launch one worker per newly loaded drive and suppress refresh duplicates."""

    def __init__(self, worker: Callable[[int], None]):
        self._worker = worker
        self._lock = threading.Lock()
        self._present: dict[int, tuple[str | None, str | None]] = {}
        self._running: set[int] = set()

    def observe(self, snapshot: DriveStatusSnapshot, *, enabled: bool) -> None:
        loaded = {
            drive.drive_index: (
                drive.disc_label.casefold() if drive.disc_label else None,
                drive.current_disc_fingerprint,
            )
            for drive in snapshot.drives
            if drive.has_disc
        }
        with self._lock:
            newly_loaded = {
                drive_index
                for drive_index, identity in loaded.items()
                if drive_index not in self._present
                or self._present[drive_index] != identity
            }
            self._present = loaded
            if not enabled:
                return
            launch = sorted(newly_loaded - self._running)
            self._running.update(launch)
        for drive_index in launch:
            threading.Thread(
                target=self._run,
                args=(drive_index,),
                name=f"automatic-rip-drive-{drive_index}",
                daemon=True,
            ).start()

    def _run(self, drive_index: int) -> None:
        try:
            self._worker(drive_index)
        except Exception as exc:
            logger.warning(
                "Automatic rip preparation stopped safely: {}", type(exc).__name__
            )
        finally:
            with self._lock:
                self._running.discard(drive_index)


def run_automatic_drive(drive_index: int) -> None:
    """Prepare, authorize, queue, and execute one collision-free inserted disc."""

    from mkv_episode_matcher.backend.dependencies import (
        get_disc_inventory_runner,
        get_drive_watcher,
        get_orchestration_store,
        get_pipeline_contract_root,
        get_pipeline_queue_store,
        get_private_binding_store,
        get_rip_execution_registry,
        get_rip_queue_runner,
    )
    from mkv_episode_matcher.backend.routers.rip import (
        ExecuteJobRequest,
        PrepareDrivePipelineRequest,
        execute_rip_job,
        prepare_drive_pipeline,
    )
    from mkv_episode_matcher.core.config_manager import get_config_manager

    config = get_config_manager().load()
    if not config.automatic_processing_enabled:
        return
    token = uuid4().hex
    public_store = get_orchestration_store()
    private_store = get_private_binding_store()
    pipeline_store = get_pipeline_queue_store()
    try:
        prepared = prepare_drive_pipeline(
            PrepareDrivePipelineRequest(
                drive_index=drive_index,
                handbrake_profile_id=None,
                confirm_read=True,
            ),
            f"automatic-prepare-{drive_index}-{token}",
            get_drive_watcher(),
            public_store,
            private_store,
            pipeline_store,
            get_disc_inventory_runner(),
        )
        job_id = str(prepared["job_id"])
        job = public_store.get_job(job_id)
        if _has_prior_disc_work(public_store, job):
            logger.info(
                "Automatic rip held a previously known disc for existing-work review"
            )
            return
        if job.preview.get("requires_review") is not False:
            logger.info("Automatic rip held one disc for an explicit review choice")
            return
        public_store.authorize(
            job_id,
            expected_plan_sha256=job.plan_sha256,
            idempotency_key=f"automatic-authorize-{token}",
        )
        public_store.queue(job_id, idempotency_key=f"automatic-queue-{token}")
        completed = execute_rip_job(
            job_id,
            ExecuteJobRequest(
                expected_plan_sha256=job.plan_sha256,
                authorized_job_count=len(job.preview.get("jobs", [])),
                max_drives=1,
                confirm_execute=True,
                preserve_failed_partials=True,
            ),
            f"automatic-execute-{token}",
            public_store,
            private_store,
            get_rip_execution_registry(),
            get_rip_queue_runner(),
            pipeline_store,
            get_pipeline_contract_root(),
            get_disc_inventory_runner(),
        )
        if completed.get("state") == "completed":
            media_ids = tuple(
                str(item["job_id"])
                for item in job.preview.get("jobs", [])
                if isinstance(item, dict) and isinstance(item.get("job_id"), str)
            )
            settings = public_store.get_pipeline_settings(job_id)
            _continue_automatic_downstream(
                media_ids,
                profile_id=settings["handbrake_profile_id"],
            )
    except (HTTPException, RipError, OSError, ValueError) as exc:
        logger.warning("Automatic rip stopped safely: {}", type(exc).__name__)


def _wait_for_stage_to_settle(
    media_ids: tuple[str, ...], stage: str, *, timeout_seconds: int = 86400
) -> None:
    from mkv_episode_matcher.backend.dependencies import get_pipeline_queue_store

    store = get_pipeline_queue_store()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        selected = [store.get(media_id) for media_id in media_ids]
        if not any(
            item.stage == stage and item.state in {"queued", "running"}
            for item in selected
        ):
            return
        time.sleep(1)
    raise RipError(f"Automatic {stage} wait timed out")


def _continue_automatic_downstream(
    media_ids: tuple[str, ...], *, profile_id: str | None = None
) -> None:
    """Serialize default-profile transcode and collision-free organization."""

    if not media_ids:
        return
    from mkv_episode_matcher.backend.dependencies import (
        get_handbrake_profile_store,
        get_pipeline_contract_root,
        get_pipeline_queue_store,
    )
    from mkv_episode_matcher.backend.organization_authorization import (
        build_organization_authorization_plan,
    )
    from mkv_episode_matcher.backend.routers.rip import (
        AuthorizeOrganizationRequest,
        AuthorizeTranscodeRequest,
        authorize_organization_batch,
        authorize_transcode_batch,
    )
    from mkv_episode_matcher.backend.transcode_authorization import (
        build_transcode_authorization_plan,
    )
    from mkv_episode_matcher.core.config_manager import get_config_manager

    with _downstream_lock:
        store = get_pipeline_queue_store()
        config = get_config_manager().load()
        _wait_for_stage_to_settle(media_ids, "identify")
        _resolve_automatic_unmatched_disc(
            media_ids, store, config, get_pipeline_contract_root()
        )
        _wait_for_stage_to_settle(media_ids, "identify")
        if any(
            store.get(media_id).stage == "transcode"
            and store.get(media_id).state == "queued"
            for media_id in media_ids
        ):
            profiles = get_handbrake_profile_store()
            plan = build_transcode_authorization_plan(
                store,
                profiles,
                config,
                profile_id=profile_id,
            )
            authorize_transcode_batch(
                AuthorizeTranscodeRequest(
                    expected_plan_sha256=plan.plan_sha256,
                    authorized_item_count=len(plan.media_ids),
                    profile_id=plan.profile_override_id,
                    confirm_transcode=True,
                ),
                store,
                profiles,
                get_pipeline_contract_root(),
            )
            _wait_for_stage_to_settle(media_ids, "transcode")
        if any(
            store.get(media_id).stage == "organize"
            and store.get(media_id).state == "queued"
            for media_id in media_ids
        ):
            plan = build_organization_authorization_plan(store, config)
            if plan.collision_media_ids:
                return
            authorize_organization_batch(
                AuthorizeOrganizationRequest(
                    expected_plan_sha256=plan.plan_sha256,
                    authorized_item_count=len(plan.media_ids),
                    confirm_organize=True,
                ),
                store,
                get_pipeline_contract_root(),
            )


def _resolve_automatic_unmatched_disc(  # noqa: C901
    media_ids, store, config, contract_root
) -> None:
    """Run the normal disc-sequence fallback for one automatic TV batch."""

    import json

    from mkv_episode_matcher.backend.dependencies import get_engine
    from mkv_episode_matcher.backend.unmatched_disc_analysis import (
        GeminiAnalysisError,
        execute_unmatched_disc_analysis,
    )
    from mkv_episode_matcher.pipeline_queue import PipelineQueueError

    held = [
        store.get(media_id)
        for media_id in media_ids
        if store.get(media_id).stage == "identify"
        and store.get(media_id).state == "review_required"
        and store.get(media_id).review_code
        in {
            "missing_season_context",
            "unmatched_disc_analysis_required",
            "all_season_analysis_running",
            "all_season_analysis_failed",
            "all_season_sequence_review_required",
        }
    ]
    if not held:
        return
    try:
        payload = json.loads(held[0].artifact.contract_path.read_text(encoding="utf-8"))
        context = payload.get("media_context", {})
        if context.get("content_hint") in {"movie", "extras", "mixed"}:
            # Movie and bonus-feature batches must never be passed to the TV
            # all-season sequence matcher merely because they have no season.
            return
        fingerprint = payload.get("disc_fingerprint")
        series_name = _automatic_series_name(
            context.get("series_name"), held[0].media_id
        )
        season = context.get("season")
        if not isinstance(fingerprint, str) or series_name is None:
            raise PipelineQueueError("Automatic episode context is incomplete")
        if not isinstance(season, int) or isinstance(season, bool):
            season = None
        for item in held:
            store.choose_review_path(item.media_id, "all_season_analysis_running")
        execute_unmatched_disc_analysis(
            store,
            fingerprint,
            series_name,
            config,
            get_engine().asr,
            contract_root,
            season=season,
            allow_gemini=config.automatic_gemini_ambiguity_fallback,
        )
    except Exception as exc:
        if isinstance(exc, GeminiAnalysisError):
            logger.warning(
                "Automatic Gemini analysis stopped safely: {}", exc.diagnostic
            )
            code = exc.review_code
        else:
            logger.warning(
                "Automatic disc-sequence analysis stopped safely: {}",
                type(exc).__name__,
            )
            code = (
                "all_season_sequence_review_required"
                if str(exc) == "All-season sequence result requires review"
                else "all_season_analysis_failed"
            )
        for item in held:
            try:
                if store.get(item.media_id).state == "review_required":
                    store.choose_review_path(item.media_id, code)
            except PipelineQueueError:
                pass


automatic_rip_coordinator = AutomaticRipCoordinator(run_automatic_drive)


def _automatic_series_name(series_name: object, media_id: str) -> str | None:
    """Recover a real series name without treating a disc ordinal as a season."""

    if isinstance(series_name, str):
        normalized = re.sub(r"[^a-z0-9]+", "", series_name.casefold())
        if normalized not in {"", "unmatched", "unknown", "unknownseries"}:
            return series_name.strip()
    disc_label = media_id.split("--disc-", 1)[0]
    inferred = infer_release_name_from_disc_label(disc_label)
    if inferred is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "", inferred.casefold())
    return (
        inferred
        if normalized not in {"", "unmatched", "unknown", "unknownseries"}
        else None
    )
