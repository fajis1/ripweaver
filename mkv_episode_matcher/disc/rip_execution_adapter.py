"""Explicit production adapter from a private dispatch to the rip orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mkv_episode_matcher.disc.rip_dispatcher import (
    BoundRipDispatch,
    DispatchOutcome,
)
from mkv_episode_matcher.disc.rip_orchestrator import run_parallel_auto_rip_queue
from mkv_episode_matcher.disc.ripper import RipError, RipResult

QueueRunner = Callable[..., list[RipResult]]
CompletionSink = Callable[[BoundRipDispatch, list[RipResult]], None]


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


class ProductionRipExecutor:
    """Invoke the existing guarded queue only when explicitly constructed."""

    def __init__(
        self,
        options: RipExecutionOptions,
        *,
        queue_runner: QueueRunner = run_parallel_auto_rip_queue,
        completion_sink: CompletionSink | None = None,
    ):
        self.options = options
        self.queue_runner = queue_runner
        self.completion_sink = completion_sink

    def _validate(self, bound: BoundRipDispatch) -> tuple[Path, Path]:
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
        results = self.queue_runner(
            executable,
            bound.output_root,
            bound.manifest.jobs,
            run_directory,
            batch_plans=bound.batch_plans,
            timeout_seconds=self.options.timeout_seconds,
            cancel_file=run_directory / "STOP",
            max_drives=self.options.max_drives,
        )
        result_ids = [result.job_id for result in results]
        if len(result_ids) != len(expected_ids) or set(result_ids) != expected_ids:
            raise RipError("Rip orchestrator returned an unexpected result set")
        if self.completion_sink is not None:
            self.completion_sink(bound, results)
        return DispatchOutcome(completed_count=len(results))
