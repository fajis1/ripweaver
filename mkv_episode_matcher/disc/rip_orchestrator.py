"""Choose a proven single-open rip per drive or retain per-title fallback."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

from mkv_episode_matcher.disc.batch_ripper import (
    SingleOpenBatchPlan,
    run_single_open_batch,
)
from mkv_episode_matcher.disc.ripper import (
    JsonlRipLog,
    RipError,
    RipJob,
    RipResult,
    run_rip_job,
    validate_job,
)

JobRunner = Callable[..., RipResult]
BatchRunner = Callable[..., list[RipResult]]
ResultSink = Callable[[RipResult], None]


class ParallelRipError(RipError):
    """Retain path-free partial success data when one or more drives fail."""

    def __init__(
        self,
        message: str,
        *,
        completed_results: list[RipResult],
        drive_failures: dict[int, str],
    ):
        super().__init__(message)
        self.completed_results = tuple(completed_results)
        self.drive_failures = dict(drive_failures)
        self.pipeline_queued_count: int | None = None
        self.pipeline_handoff_pending_job_ids: tuple[str, ...] = ()


def _group_and_validate(
    jobs: Iterable[RipJob],
    batch_plans: Mapping[int, SingleOpenBatchPlan],
) -> tuple[list[RipJob], dict[int, list[RipJob]]]:
    job_list = list(jobs)
    if not job_list:
        raise RipError("Approved rip queue is empty")
    if len({job.job_id for job in job_list}) != len(job_list):
        raise RipError("Approved rip queue contains duplicate job IDs")

    jobs_by_drive: dict[int, list[RipJob]] = {}
    for job in job_list:
        validate_job(job)
        jobs_by_drive.setdefault(job.drive_index, []).append(job)
    if not set(batch_plans).issubset(jobs_by_drive):
        raise RipError("A single-open plan refers to an unapproved drive")
    for drive_index, plan in batch_plans.items():
        expected = jobs_by_drive[drive_index]
        if plan.drive_index != drive_index or {job.job_id for job in plan.jobs} != {
            job.job_id for job in expected
        }:
            raise RipError("A single-open plan does not exactly cover its drive")
    return job_list, jobs_by_drive


def _run_drive(
    executable: Path,
    output_root: Path,
    drive_index: int,
    drive_jobs: list[RipJob],
    event_log: JsonlRipLog,
    *,
    batch_plan: SingleOpenBatchPlan | None,
    timeout_seconds: int,
    cancel_file: Path | None,
    on_event: Callable[[str, str], None] | None,
    on_result: ResultSink | None,
    job_runner: JobRunner,
    batch_runner: BatchRunner,
) -> list[RipResult]:
    def publish(result: RipResult) -> None:
        if on_result is None:
            return
        try:
            on_result(result)
        except Exception as exc:  # A consumer must never interrupt MakeMKV.
            event_log.write(
                "result_handoff_notification_failed",
                job_id=result.job_id,
                error_type=type(exc).__name__,
            )

    if batch_plan is not None:
        event_log.write(
            "drive_strategy_selected",
            drive_index=drive_index,
            strategy="single-open",
            job_count=len(drive_jobs),
            minimum_length_seconds=batch_plan.minimum_length_seconds,
        )
        results = batch_runner(
            executable,
            output_root,
            batch_plan,
            event_log,
            timeout_seconds=timeout_seconds,
            cancel_file=cancel_file,
            on_event=on_event,
        )
        # A single-open batch is publishable only after the adapter verifies the
        # complete isolated output set. At that point each MKV can enter the
        # downstream queue independently.
        for result in results:
            publish(result)
        return results

    event_log.write(
        "drive_strategy_selected",
        drive_index=drive_index,
        strategy="per-title",
        job_count=len(drive_jobs),
    )
    results: list[RipResult] = []
    for job in drive_jobs:
        result = job_runner(
            executable,
            output_root,
            job,
            event_log,
            timeout_seconds=timeout_seconds,
            cancel_file=cancel_file,
            on_event=on_event,
        )
        results.append(result)
        publish(result)
    return results


def run_auto_rip_queue(
    executable: Path,
    output_root: Path,
    jobs: Iterable[RipJob],
    log_path: Path,
    *,
    batch_plans: Mapping[int, SingleOpenBatchPlan],
    timeout_seconds: int = 7200,
    cancel_file: Path | None = None,
    on_event: Callable[[str, str], None] | None = None,
    on_result: ResultSink | None = None,
    job_runner: JobRunner = run_rip_job,
    batch_runner: BatchRunner = run_single_open_batch,
) -> list[RipResult]:
    """Run drives sequentially, selecting one safe strategy for each drive."""

    job_list, jobs_by_drive = _group_and_validate(jobs, batch_plans)
    with JsonlRipLog(log_path) as event_log:
        event_log.write(
            "auto_queue_started",
            drive_count=len(jobs_by_drive),
            job_count=len(job_list),
            single_open_drive_count=len(batch_plans),
        )
        results: list[RipResult] = []
        try:
            for drive_index, drive_jobs in jobs_by_drive.items():
                results.extend(
                    _run_drive(
                        executable,
                        output_root,
                        drive_index,
                        drive_jobs,
                        event_log,
                        batch_plan=batch_plans.get(drive_index),
                        timeout_seconds=timeout_seconds,
                        cancel_file=cancel_file,
                        on_event=on_event,
                        on_result=on_result,
                        job_runner=job_runner,
                        batch_runner=batch_runner,
                    )
                )
        except RipError as exc:
            event_log.write(
                "auto_queue_paused",
                completed_count=len(results),
                error_type=type(exc).__name__,
            )
            raise
        event_log.write("auto_queue_completed", completed_count=len(results))
        return results


def run_parallel_auto_rip_queue(
    executable: Path,
    output_root: Path,
    jobs: Iterable[RipJob],
    log_dir: Path,
    *,
    batch_plans: Mapping[int, SingleOpenBatchPlan],
    timeout_seconds: int = 7200,
    cancel_file: Path | None = None,
    max_drives: int | None = None,
    on_event: Callable[[str, str], None] | None = None,
    on_result: ResultSink | None = None,
    job_runner: JobRunner = run_rip_job,
    batch_runner: BatchRunner = run_single_open_batch,
) -> list[RipResult]:
    """Run one isolated worker per drive with a preselected safe strategy."""

    job_list, jobs_by_drive = _group_and_validate(jobs, batch_plans)
    worker_count = max_drives or len(jobs_by_drive)
    if worker_count <= 0:
        raise RipError("Parallel drive limit must be positive")
    worker_count = min(worker_count, len(jobs_by_drive))
    log_dir.mkdir(parents=True, exist_ok=True)

    coordinator_path = log_dir / "parallel-coordinator.jsonl"
    with JsonlRipLog(coordinator_path) as coordinator:
        coordinator.write(
            "parallel_auto_queue_started",
            drive_count=len(jobs_by_drive),
            job_count=len(job_list),
            max_drives=worker_count,
            single_open_drive_count=len(batch_plans),
        )

        completed_lock = Lock()
        completed_by_id: dict[str, RipResult] = {}

        def record_result(result: RipResult) -> None:
            # Capture partial success before notifying any downstream consumer.
            # This preserves titles completed earlier on a drive if a later
            # title on that same drive fails.
            with completed_lock:
                completed_by_id.setdefault(result.job_id, result)
            if on_result is not None:
                on_result(result)

        def run_drive(
            drive_index: int,
            drive_jobs: list[RipJob],
        ) -> list[RipResult]:
            drive_log_path = log_dir / f"drive-{drive_index:02d}.jsonl"
            with JsonlRipLog(drive_log_path) as drive_log:
                drive_log.write(
                    "drive_worker_started",
                    drive_index=drive_index,
                    job_count=len(drive_jobs),
                )
                results = _run_drive(
                    executable,
                    output_root,
                    drive_index,
                    drive_jobs,
                    drive_log,
                    batch_plan=batch_plans.get(drive_index),
                    timeout_seconds=timeout_seconds,
                    cancel_file=cancel_file,
                    on_event=on_event,
                    on_result=record_result,
                    job_runner=job_runner,
                    batch_runner=batch_runner,
                )
                drive_log.write(
                    "drive_worker_completed",
                    drive_index=drive_index,
                    completed_count=len(results),
                )
                return results

        futures: dict[Future[list[RipResult]], int] = {}
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="mkv-drive",
        ) as executor:
            for drive_index, drive_jobs in jobs_by_drive.items():
                futures[executor.submit(run_drive, drive_index, drive_jobs)] = (
                    drive_index
                )

            collected: list[RipResult] = []
            first_error: RipError | None = None
            drive_failures: dict[int, str] = {}
            for future in as_completed(futures):
                drive_index = futures[future]
                try:
                    collected.extend(future.result())
                except Exception as exc:
                    drive_failures[drive_index] = type(exc).__name__
                    if first_error is None:
                        first_error = (
                            exc
                            if isinstance(exc, RipError)
                            else RipError(
                                f"Drive worker {drive_index} failed: "
                                f"{type(exc).__name__}"
                            )
                        )
                    coordinator.write(
                        "drive_worker_failed",
                        drive_index=drive_index,
                        error_type=type(exc).__name__,
                    )

        by_id = {result.job_id: result for result in collected}
        with completed_lock:
            by_id.update(completed_by_id)
        order = {job.job_id: index for index, job in enumerate(job_list)}
        collected = sorted(
            by_id.values(),
            key=lambda result: order.get(result.job_id, len(order)),
        )

        if first_error is not None:
            coordinator.write(
                "parallel_auto_queue_completed_with_failures",
                completed_count=len(collected),
                error_type=type(first_error).__name__,
            )
            raise ParallelRipError(
                str(first_error),
                completed_results=collected,
                drive_failures=drive_failures,
            ) from first_error

        collected.sort(key=lambda result: order[result.job_id])
        coordinator.write(
            "parallel_auto_queue_completed",
            completed_count=len(collected),
        )
        return collected
