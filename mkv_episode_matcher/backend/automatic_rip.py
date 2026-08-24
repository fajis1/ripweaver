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
_automatic_rip_startup_hold = threading.Event()
_AUTOMATIC_UNMATCHED_CODES = frozenset({
    "episode_match_review",
    "gemini_descriptive_review_required",
    "missing_season_context",
    "unmatched_disc_analysis_required",
    "all_season_sequence_review_required",
    "independent_episode_evidence_required",
})


def set_automatic_rip_startup_hold(enabled: bool) -> None:
    """Hold automatic disc execution for one explicitly staged server lifetime."""

    if enabled:
        _automatic_rip_startup_hold.set()
    else:
        _automatic_rip_startup_hold.clear()


def automatic_rip_startup_held() -> bool:
    """Return whether this process must not launch unattended disc workers."""

    return _automatic_rip_startup_hold.is_set()


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
    """Refuse duplicates only when this inventory reached actionable work."""

    fingerprints = _preview_fingerprints(prepared_job.preview)
    if not fingerprints:
        return False
    for job in store.list_jobs(limit=200):
        if (
            job.job_id == prepared_job.job_id
            or not fingerprints & _preview_fingerprints(job.preview)
        ):
            continue
        if job.state in {
            "authorized",
            "queued",
            "running",
            "pause_requested",
            "paused",
            "completed",
        }:
            return True
        if job.state == "failed" and hasattr(store, "list_events"):
            if any(
                event.event_type in {"rip_title_completed", "rip_title_output_closed"}
                for event in store.list_events(job.job_id)
            ):
                return True
    return False


def _recover_bound_automatic_job(drive_index: int, watcher, store):
    """Recover a clean job durably bound before preparation returned."""

    drive = next(
        (item for item in watcher.snapshot().drives if item.drive_index == drive_index),
        None,
    )
    if drive is None or not drive.current_job_id:
        return None
    try:
        job = store.get_job(drive.current_job_id)
    except RipError:
        return None
    if job.state not in {
        "awaiting_review",
        "authorized",
        "queued",
        "running",
        "completed",
    }:
        return None
    if job.preview.get("requires_review") is not False:
        return None
    fingerprint = drive.current_disc_fingerprint
    if fingerprint and fingerprint not in _preview_fingerprints(job.preview):
        return None
    return job


def _prepare_or_recover_automatic_job(
    drive_index: int, prepare: Callable[[], dict[str, object]], watcher, store
):
    """Return the clean persisted job even if its response construction failed."""

    try:
        prepared = prepare()
        return store.get_job(str(prepared["job_id"]))
    except (HTTPException, RipError, OSError, ValueError) as exc:
        job = _recover_bound_automatic_job(drive_index, watcher, store)
        if job is None:
            raise
        logger.warning(
            "Automatic rip recovered a clean bound job after preparation failed: {}",
            type(exc).__name__,
        )
        return job


def _automatic_job_can_advance(job, store) -> bool:
    """Return whether one persisted job is safe for unattended advancement."""

    if job.state in {"completed", "running"}:
        return False
    if job.state not in {"awaiting_review", "authorized", "queued"}:
        logger.info("Automatic rip held a job whose state requires review")
        return False
    if job.state == "awaiting_review" and _has_prior_disc_work(store, job):
        logger.info(
            "Automatic rip held a previously known disc for existing-work review"
        )
        return False
    if job.preview.get("requires_review") is not False:
        logger.info("Automatic rip held one disc for an explicit review choice")
        return False
    return True


def _automatic_failure_is_retryable(exc: Exception) -> bool:
    """Return whether an unchanged loaded disc may be prepared again.

    HTTP failures are decisions made after the request reached the guarded
    preparation boundary.  Retrying the same disc without a media change just
    repeats deterministic review, configuration, or durable-state conflicts.
    Low-level operating-system failures may be transient, so the coordinator
    retains its existing one-reconciliation retry behavior for those only.
    """

    return isinstance(exc, OSError) and not isinstance(exc, HTTPException)


class AutomaticRipCoordinator:
    """Launch one worker per newly loaded drive and suppress refresh duplicates."""

    def __init__(self, worker: Callable[[int], bool | None]):
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
            if (
                drive.available
                and drive.has_disc
                and drive.mapping_status == "trusted"
                and drive.makemkv_confirmed
            )
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

    def active_drive_indexes(self) -> tuple[int, ...]:
        """Return a path-free snapshot of drives being prepared or advanced."""

        with self._lock:
            return tuple(sorted(self._running))

    def _run(self, drive_index: int) -> None:
        retry = False
        try:
            retry = self._worker(drive_index) is False
        except Exception as exc:
            retry = True
            logger.warning(
                "Automatic rip preparation stopped safely: {}", type(exc).__name__
            )
        finally:
            with self._lock:
                self._running.discard(drive_index)
                if retry:
                    # The unchanged disc must look newly loaded on the next
                    # backend reconciliation turn. Otherwise one transient
                    # preparation failure can only be recovered by a browser
                    # refresh, tray cycle, or another Windows volume event.
                    self._present.pop(drive_index, None)


def run_automatic_drive(drive_index: int) -> bool:
    """Prepare, authorize, queue, and execute one collision-free inserted disc."""

    if automatic_rip_startup_held():
        logger.info("Automatic rip remained held for exact-plan review")
        return True

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
        return True
    token = uuid4().hex
    public_store = get_orchestration_store()
    private_store = get_private_binding_store()
    pipeline_store = get_pipeline_queue_store()
    watcher = get_drive_watcher()
    stage = "preparation"
    try:
        job = _prepare_or_recover_automatic_job(
            drive_index,
            lambda: prepare_drive_pipeline(
                PrepareDrivePipelineRequest(
                    drive_index=drive_index,
                    handbrake_profile_id=None,
                    confirm_read=True,
                ),
                f"automatic-prepare-{drive_index}-{token}",
                watcher,
                public_store,
                private_store,
                pipeline_store,
                get_disc_inventory_runner(),
            ),
            watcher,
            public_store,
        )
        job_id = job.job_id
        if not _automatic_job_can_advance(job, public_store):
            return True
        if job.state == "awaiting_review":
            stage = "authorization"
            public_store.authorize(
                job_id,
                expected_plan_sha256=job.plan_sha256,
                idempotency_key=f"automatic-authorize-{token}",
            )
            job = public_store.get_job(job_id)
        if job.state == "authorized":
            stage = "queueing"
            public_store.queue(job_id, idempotency_key=f"automatic-queue-{token}")
            job = public_store.get_job(job_id)
        if job.state != "queued":
            return job.state in {"running", "completed"}
        stage = "execution"
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
            watcher,
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
        return True
    except (HTTPException, RipError, OSError, ValueError) as exc:
        retry = _automatic_failure_is_retryable(exc)
        status_code = exc.status_code if isinstance(exc, HTTPException) else None
        logger.warning(
            "Automatic rip stopped safely during {}: failure_type={} "
            "status_code={} retry_unchanged_disc={}",
            stage,
            type(exc).__name__,
            status_code,
            retry,
        )
        return not retry


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
        and store.get(media_id).review_code in _AUTOMATIC_UNMATCHED_CODES
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
            if (
                exc.proposed_series_name is not None
                and exc.proposed_confidence is not None
            ):
                try:
                    store.record_series_resolution_diagnostic(
                        tuple(item.media_id for item in held),
                        proposed_series_name=exc.proposed_series_name,
                        proposed_series_names=exc.proposed_series_names,
                        confidence=exc.proposed_confidence,
                        proposed_tmdb_id=exc.proposed_tmdb_id,
                    )
                except PipelineQueueError:
                    pass
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
                "all_season_catalog_unavailable"
                if str(exc) == "Episode catalogue is unavailable for the reviewed scope"
                else (
                    "independent_episode_evidence_required"
                    if str(exc) == "Independent episode evidence requires review"
                    else "all_season_analysis_failed"
                )
            )
        for item in held:
            try:
                current = store.get(item.media_id)
                if (
                    current.state == "review_required"
                    and current.review_code != "visual_content_review_required"
                ):
                    store.choose_review_path(item.media_id, code)
            except PipelineQueueError:
                pass


automatic_rip_coordinator = AutomaticRipCoordinator(run_automatic_drive)


def observe_automatic_drives(
    snapshot: DriveStatusSnapshot,
    *,
    enabled: bool,
    processing_paused: bool = False,
) -> bool:
    """Observe loaded media unless startup or durable processing is held."""

    if automatic_rip_startup_held() or processing_paused:
        return False
    automatic_rip_coordinator.observe(snapshot, enabled=enabled)
    return True


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
