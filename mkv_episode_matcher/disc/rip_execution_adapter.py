"""Explicit production adapter from a private dispatch to the rip orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from threading import Lock, Thread

from mkv_episode_matcher.disc.rip_dispatcher import (
    BoundRipDispatch,
    DispatchOutcome,
)
from mkv_episode_matcher.disc.rip_orchestrator import (
    ParallelRipError,
    run_parallel_auto_rip_queue,
)
from mkv_episode_matcher.disc.ripper import RipError, RipResult

QueueRunner = Callable[..., list[RipResult]]
CompletionSink = Callable[[BoundRipDispatch, list[RipResult]], None]
ProgressSink = Callable[[str, str], None]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class RipExecutionOptions:
    """Explicit non-secret inputs required to construct a physical executor."""

    makemkv_executable: Path
    run_directory: Path
    timeout_seconds: int = 7200
    max_drives: int | None = None
    physical_disc_execution_enabled: bool = True


class _PipelineHandoff:
    """Keep queue admission work entirely off MakeMKV drive threads."""

    def __init__(
        self,
        bound: BoundRipDispatch,
        expected_ids: set[str],
        sink: CompletionSink | None,
    ):
        self.bound = bound
        self.expected_ids = expected_ids
        self.sink = sink
        self.queue: Queue[RipResult | object] | None = Queue() if sink else None
        self.stop = object()
        self.lock = Lock()
        self.offered: dict[str, RipResult] = {}
        self.completed: set[str] = set()
        self.failures: dict[str, str] = {}
        self.thread = (
            Thread(target=self._run, name="rip-pipeline-handoff", daemon=True)
            if sink
            else None
        )
        if self.thread is not None:
            self.thread.start()

    def _run(self) -> None:
        assert self.queue is not None and self.sink is not None
        while True:
            item = self.queue.get()
            try:
                if item is self.stop:
                    return
                assert isinstance(item, RipResult)
                try:
                    self.sink(self.bound, [item])
                except Exception as exc:
                    # Queue admission is recoverable and must never turn a
                    # verified MakeMKV result into a rip failure.
                    with self.lock:
                        self.failures[item.job_id] = type(exc).__name__
                else:
                    with self.lock:
                        self.completed.add(item.job_id)
                        self.failures.pop(item.job_id, None)
            finally:
                self.queue.task_done()

    def offer(self, result: RipResult) -> None:
        if self.queue is None or result.job_id not in self.expected_ids:
            return
        with self.lock:
            if result.job_id in self.offered:
                return
            self.offered[result.job_id] = result
        # This is intentionally the only work done on a MakeMKV drive thread.
        self.queue.put(result)

    def finish(self, results: list[RipResult]) -> tuple[int | None, tuple[str, ...]]:
        if self.queue is None or self.thread is None:
            return None, ()
        for result in results:
            self.offer(result)
        self.queue.join()
        # Retry one transient failure after all first-pass handoffs complete.
        with self.lock:
            retry_results = [
                self.offered[job_id]
                for job_id in self.failures
                if job_id in self.offered
            ]
        for result in retry_results:
            self.queue.put(result)
        self.queue.join()
        self.queue.put(self.stop)
        self.queue.join()
        self.thread.join()
        with self.lock:
            return len(self.completed), tuple(sorted(self.failures))


class ProductionRipExecutor:
    """Invoke the existing guarded queue only when explicitly constructed."""

    def __init__(
        self,
        options: RipExecutionOptions,
        *,
        queue_runner: QueueRunner = run_parallel_auto_rip_queue,
        completion_sink: CompletionSink | None = None,
        progress_sink: ProgressSink | None = None,
    ):
        self.options = options
        self.queue_runner = queue_runner
        self.completion_sink = completion_sink
        self.progress_sink = progress_sink

    def _validate(self, bound: BoundRipDispatch) -> tuple[Path, Path]:
        if not self.options.physical_disc_execution_enabled:
            raise RipError("Physical disc execution is disabled for this executor")
        executable = self.options.makemkv_executable.resolve()
        run_directory = self.options.run_directory.resolve()
        output_root = bound.output_root.resolve()

        if not executable.is_file():
            raise RipError("Configured MakeMKV executable is not a file")
        if not output_root.is_dir():
            raise RipError("Bound rip output root no longer exists")
        if run_directory.exists():
            raise RipError("Dedicated rip run directory already exists")
        if _is_relative_to(run_directory, output_root):
            raise RipError("Rip run directory must be outside the media output root")
        if self.options.timeout_seconds < 60:
            raise RipError("Rip timeout must be at least 60 seconds")
        if self.options.max_drives is not None and self.options.max_drives < 1:
            raise RipError("Parallel drive limit must be positive")
        return executable, run_directory

    def __call__(self, bound: BoundRipDispatch) -> DispatchOutcome:
        executable, run_directory = self._validate(bound)
        expected_ids = {job.job_id for job in bound.manifest.jobs}
        handoff = _PipelineHandoff(bound, expected_ids, self.completion_sink)

        try:
            results = self.queue_runner(
                executable,
                bound.output_root,
                bound.manifest.jobs,
                run_directory,
                batch_plans=bound.batch_plans,
                timeout_seconds=self.options.timeout_seconds,
                cancel_file=run_directory / "STOP",
                max_drives=self.options.max_drives,
                on_event=self.progress_sink,
                on_result=handoff.offer,
            )
        except ParallelRipError as exc:
            completed_results = list(exc.completed_results)
            completed_ids = [result.job_id for result in completed_results]
            if (
                len(completed_ids) != len(set(completed_ids))
                or not set(completed_ids) <= expected_ids
            ):
                handoff.finish([])
                raise RipError(
                    "Rip orchestrator returned an unexpected partial result set"
                ) from exc
            queued_count, pending_ids = handoff.finish(completed_results)
            exc.pipeline_queued_count = queued_count
            exc.pipeline_handoff_pending_job_ids = pending_ids
            raise
        except Exception:
            handoff.finish([])
            raise
        result_ids = [result.job_id for result in results]
        if len(result_ids) != len(expected_ids) or set(result_ids) != expected_ids:
            handoff.finish([])
            raise RipError("Rip orchestrator returned an unexpected result set")
        queued_count, pending_ids = handoff.finish(results)
        return DispatchOutcome(
            completed_count=len(results),
            pipeline_queued_count=queued_count,
            pipeline_handoff_pending_job_ids=pending_ids,
        )
