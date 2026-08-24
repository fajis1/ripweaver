"""Private queued-job dispatcher with no default physical executor."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from mkv_episode_matcher.disc.batch_ripper import SingleOpenBatchPlan
from mkv_episode_matcher.disc.orchestration_store import (
    OrchestrationJob,
    OrchestrationStore,
)
from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.disc.rip_manifest import (
    RipManifest,
    bind_fresh_batch_plans,
    build_rip_manifest,
)
from mkv_episode_matcher.disc.rip_orchestrator import ParallelRipError
from mkv_episode_matcher.disc.rip_preview import (
    build_rip_preview,
    compatible_manifest_sha256s,
)
from mkv_episode_matcher.disc.ripper import RipError


@dataclass(frozen=True)
class BoundRipDispatch:
    """Private execution inputs that must never be returned by routine APIs."""

    job_id: str
    manifest: RipManifest
    batch_plans: dict[int, SingleOpenBatchPlan]
    output_root: Path


@dataclass(frozen=True)
class DispatchOutcome:
    completed_count: int
    pipeline_queued_count: int | None = None
    pipeline_handoff_pending_job_ids: tuple[str, ...] = ()


RipExecutor = Callable[[BoundRipDispatch], DispatchOutcome]
FreshInventoryProvider = Callable[[], list[Path]]


def _phase_key(job_id: str, dispatch_key: str, phase: str) -> str:
    digest = hashlib.sha256(f"{job_id}\0{dispatch_key}\0{phase}".encode()).hexdigest()
    return f"{phase}-{digest}"


class RipDispatcher:
    """Revalidate and dispatch one queued job through an injected executor."""

    def __init__(
        self,
        public_store: OrchestrationStore,
        private_store: PrivateBindingStore,
    ):
        if (
            public_store.database_path.resolve()
            == private_store.database_path.resolve()
        ):
            raise RipError("Public state and private bindings require separate stores")
        self.public_store = public_store
        self.private_store = private_store

    def _bind(  # noqa: C901
        self,
        job: OrchestrationJob,
        *,
        fresh_inventory_paths: list[Path] | None = None,
    ) -> BoundRipDispatch:
        if job.state != "queued":
            raise RipError("Only a queued orchestration job can be dispatched")
        if job.authorization_sha256 is None:
            raise RipError("Queued job has no exact authorization record")
        binding = self.private_store.get(job.job_id)
        if binding.plan_sha256 != job.plan_sha256:
            raise RipError("Private binding digest does not match the public job")

        report_paths = (
            list(binding.report_paths)
            if fresh_inventory_paths is None
            else list(fresh_inventory_paths)
        )

        fresh_manifest = build_rip_manifest(report_paths, binding.media_contexts)
        preview = build_rip_preview(
            report_paths,
            binding.media_contexts,
            output_root=binding.output_root,
        )
        if job.plan_sha256 not in compatible_manifest_sha256s(fresh_manifest):
            raise RipError("Fresh private inputs no longer match the authorized plan")
        completed_ids: set[str] = set()
        for event in self.public_store.list_events(job.job_id):
            if event.event_type == "job_failed":
                completed_ids.update(event.details.get("completed_job_ids", []))
        unexpected_collisions = [
            item
            for item in preview.jobs
            if item.collision_status not in {"clear", "not-checked"}
            and item.job_id not in completed_ids
        ]
        if unexpected_collisions:
            raise RipError("A destination collision appeared after authorization")
        if preview.skipped_discs:
            raise RipError("Authorized job returned to review before dispatch")

        manifest = fresh_manifest
        batch_plans = bind_fresh_batch_plans(
            manifest,
            report_paths,
        )
        if completed_ids:
            remaining_jobs = tuple(
                item for item in manifest.jobs if item.job_id not in completed_ids
            )
            if not remaining_jobs:
                raise RipError("No unfinished rip titles remain to retry")
            remaining_by_drive: dict[int, set[str]] = {}
            for item in remaining_jobs:
                remaining_by_drive.setdefault(item.drive_index, set()).add(item.job_id)
            batch_plans = {
                drive: plan
                for drive, plan in batch_plans.items()
                if {item.job_id for item in plan.jobs} == remaining_by_drive.get(drive)
            }
            manifest = replace(manifest, jobs=remaining_jobs)
        return BoundRipDispatch(
            job_id=job.job_id,
            manifest=manifest,
            batch_plans=batch_plans,
            output_root=binding.output_root,
        )

    def dispatch(  # noqa: C901
        self,
        job_id: str,
        *,
        dispatch_key: str,
        executor: RipExecutor,
        fresh_inventory_provider: FreshInventoryProvider | None = None,
    ) -> OrchestrationJob:
        """Dispatch once; no physical executor exists unless explicitly injected."""

        claim_key = _phase_key(job_id, dispatch_key, "claim")
        if (
            self.public_store.command_result(
                job_id,
                action="dispatch",
                idempotency_key=claim_key,
            )
            is not None
        ):
            retained = self.public_store.get_job(job_id)
            if retained.state in {"completed", "failed"}:
                return retained
            raise RipError("Dispatch key was already claimed and requires state review")

        fresh_inventory_paths = (
            fresh_inventory_provider() if fresh_inventory_provider is not None else None
        )
        bound = self._bind(
            self.public_store.get_job(job_id),
            fresh_inventory_paths=fresh_inventory_paths,
        )
        self.public_store.claim_for_dispatch(
            job_id,
            idempotency_key=claim_key,
        )
        try:
            outcome = executor(bound)
            if outcome.completed_count != len(bound.manifest.jobs):
                raise RipError("Executor returned an incomplete result count")
        except Exception as exc:
            completed_results = (
                exc.completed_results if isinstance(exc, ParallelRipError) else ()
            )
            failed_drives = (
                tuple(sorted(exc.drive_failures))
                if isinstance(exc, ParallelRipError)
                else ()
            )
            message = str(exc).casefold()
            if "timed out" in message or "timeout" in message:
                category = "timeout"
            elif isinstance(exc, OSError) or (
                isinstance(exc, ParallelRipError)
                and "OSError" in exc.drive_failures.values()
            ):
                category = "io_error"
            elif "collision" in message:
                category = "collision"
            elif "space" in message:
                category = "storage"
            elif "cancel" in message or "stop" in message:
                category = "interrupted"
            else:
                category = "makemkv_failure"
            self.public_store.fail(
                job_id,
                idempotency_key=_phase_key(job_id, dispatch_key, "fail"),
                error_type=type(exc).__name__,
                error_category=category,
                failed_drive_indexes=failed_drives,
                completed_job_ids=tuple(item.job_id for item in completed_results),
                pipeline_queued_count=getattr(exc, "pipeline_queued_count", None),
                pipeline_handoff_pending_job_ids=getattr(
                    exc, "pipeline_handoff_pending_job_ids", ()
                ),
            )
            if isinstance(exc, RipError):
                raise
            raise RipError(
                f"Injected rip executor failed: {type(exc).__name__}"
            ) from exc
        return self.public_store.complete(
            job_id,
            idempotency_key=_phase_key(job_id, dispatch_key, "complete"),
            completed_count=outcome.completed_count,
            pipeline_queued_count=outcome.pipeline_queued_count,
            pipeline_handoff_pending_job_ids=(outcome.pipeline_handoff_pending_job_ids),
        )
