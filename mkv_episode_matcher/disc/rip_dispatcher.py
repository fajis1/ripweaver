"""Private queued-job dispatcher with no default physical executor."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
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
from mkv_episode_matcher.disc.rip_preview import build_rip_preview
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


RipExecutor = Callable[[BoundRipDispatch], DispatchOutcome]


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

    def _bind(self, job: OrchestrationJob) -> BoundRipDispatch:
        if job.state != "queued":
            raise RipError("Only a queued orchestration job can be dispatched")
        if job.authorization_sha256 is None:
            raise RipError("Queued job has no exact authorization record")
        binding = self.private_store.get(job.job_id)
        if binding.plan_sha256 != job.plan_sha256:
            raise RipError("Private binding digest does not match the public job")

        preview = build_rip_preview(
            list(binding.report_paths),
            binding.media_contexts,
            output_root=binding.output_root,
        )
        if preview.plan_sha256 != job.plan_sha256:
            raise RipError("Fresh private inputs no longer match the authorized plan")
        if preview.collision_count:
            raise RipError("A destination collision appeared after authorization")
        if preview.requires_review:
            raise RipError("Authorized job returned to review before dispatch")

        manifest = build_rip_manifest(
            list(binding.report_paths),
            binding.media_contexts,
        )
        batch_plans = bind_fresh_batch_plans(
            manifest,
            list(binding.report_paths),
        )
        return BoundRipDispatch(
            job_id=job.job_id,
            manifest=manifest,
            batch_plans=batch_plans,
            output_root=binding.output_root,
        )

    def dispatch(
        self,
        job_id: str,
        *,
        dispatch_key: str,
        executor: RipExecutor,
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

        bound = self._bind(self.public_store.get_job(job_id))
        self.public_store.claim_for_dispatch(
            job_id,
            idempotency_key=claim_key,
        )
        try:
            outcome = executor(bound)
            if outcome.completed_count != len(bound.manifest.jobs):
                raise RipError("Executor returned an incomplete result count")
        except Exception as exc:
            self.public_store.fail(
                job_id,
                idempotency_key=_phase_key(job_id, dispatch_key, "fail"),
                error_type=type(exc).__name__,
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
        )
