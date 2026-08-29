"""Synthetic-tested, single-open MakeMKV batch adapter.

MakeMKV's public command shape accepts one title per ``mkv`` invocation.  Its
``all`` selector avoids reopening the disc, but it is safe for an approved
subset only when ``--minlength`` selects exactly that subset.  This module
refuses every other shape.  A reviewed physical validation confirmed the
installed MakeMKV version's ``all`` behavior and selected-output renumbering.
"""

from __future__ import annotations

import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from mkv_episode_matcher.core.datetime_compat import UTC
from mkv_episode_matcher.disc.batch_output_validation import (
    MINIMUM_SUBSTANTIAL_MKV_BYTES,
    is_complete_batch_output_size,
    is_inventory_planned_tiny_output,
)
from mkv_episode_matcher.disc.makemkv_process_control import (
    audit_makemkv_process_exit,
    start_makemkv_process,
)
from mkv_episode_matcher.disc.ripper import (
    JsonlRipLog,
    RipError,
    RipJob,
    RipResult,
    _read_lines,
    _stop_process,
    classify_output,
    progress_fraction,
    resolve_final_output,
    resolve_job_output,
    sample_output_throughput,
    sanitize_output,
    validate_job,
)


@dataclass(frozen=True)
class BatchInventoryTitle:
    """The saved inventory fields needed to prove an exact ``all`` selection."""

    title_index: int
    duration_seconds: int
    output_name: str


@dataclass(frozen=True)
class SingleOpenBatchPlan:
    """One drive-open operation whose selected titles equal the approved jobs."""

    drive_index: int
    minimum_length_seconds: int
    jobs: tuple[RipJob, ...]
    inventory_output_names: tuple[str, ...]
    batch_output_names: tuple[str, ...]


_TITLE_OUTPUT_PATTERN = re.compile(
    r"^(?P<prefix>.+)_t(?P<ordinal>[0-9]{2,4})\.mkv$",
    re.IGNORECASE,
)


def _safe_mkv_basename(value: str) -> bool:
    path = Path(value)
    return (
        bool(value)
        and path.name == value
        and path.suffix.lower() == ".mkv"
        and len(value) <= 240
        and value not in {".", ".."}
    )


def _parse_inventory_output_name(value: str, title_index: int) -> str:
    """Return the stable prefix from a strict title-indexed MakeMKV name."""

    if not _safe_mkv_basename(value):
        raise RipError("Saved batch inventory has an unsafe output filename")
    match = _TITLE_OUTPUT_PATTERN.fullmatch(value)
    if match is None or not match.group("prefix").strip():
        raise RipError(
            "Saved batch inventory output filename lacks a strict _tNN suffix"
        )
    if int(match.group("ordinal")) != title_index:
        raise RipError(
            "Saved batch inventory output suffix does not match its title index"
        )
    return match.group("prefix")


def _batch_output_name(prefix: str, batch_ordinal: int) -> str:
    width = max(2, len(str(batch_ordinal)))
    return f"{prefix}_t{batch_ordinal:0{width}d}.mkv"


def _report_closed_batch_outputs(
    workspace: Path,
    plan: SingleOpenBatchPlan,
    reported_job_ids: set[str],
    on_event: Callable[[str, str], None] | None,
) -> None:
    """Report outputs MakeMKV has closed after starting the following title."""

    if on_event is None:
        return
    for index in range(len(plan.jobs) - 1):
        job = plan.jobs[index]
        if job.job_id in reported_job_ids:
            continue
        current = workspace / plan.batch_output_names[index]
        following = workspace / plan.batch_output_names[index + 1]
        if (
            not current.is_file()
            or current.stat().st_size <= 0
            or not following.is_file()
        ):
            break
        reported_job_ids.add(job.job_id)
        on_event("output-closed", f"{job.job_id}: completed")


def plan_single_open_batch(  # noqa: C901
    jobs: tuple[RipJob, ...],
    inventory_titles: tuple[BatchInventoryTitle, ...],
) -> SingleOpenBatchPlan:
    """Prove that one ``all`` command cannot include an unauthorized title."""

    if len(jobs) < 2:
        raise RipError("Single-open batching requires at least two approved titles")
    if not inventory_titles:
        raise RipError("Single-open batching requires a complete saved inventory")

    drives = {job.drive_index for job in jobs}
    if len(drives) != 1:
        raise RipError("Single-open batching is limited to one physical drive")
    selected_indexes: set[int] = set()
    for job in jobs:
        validate_job(job)
        if job.title_index in selected_indexes:
            raise RipError("Single-open batch contains a duplicate title")
        selected_indexes.add(job.title_index)

    inventory_by_index: dict[int, BatchInventoryTitle] = {}
    output_prefixes: dict[int, str] = {}
    output_names: set[str] = set()
    for title in inventory_titles:
        if title.title_index < 0 or title.duration_seconds < 0:
            raise RipError("Saved batch inventory contains invalid title metadata")
        if title.title_index in inventory_by_index:
            raise RipError("Saved batch inventory contains duplicate titles")
        output_prefixes[title.title_index] = _parse_inventory_output_name(
            title.output_name,
            title.title_index,
        )
        folded_name = title.output_name.casefold()
        if folded_name in output_names:
            raise RipError("Saved batch inventory output filenames are not unique")
        output_names.add(folded_name)
        inventory_by_index[title.title_index] = title

    if not selected_indexes.issubset(inventory_by_index):
        raise RipError("An approved title is absent from the saved batch inventory")

    excluded = [
        title
        for index, title in inventory_by_index.items()
        if index not in selected_indexes
    ]
    minimum_length = (
        max(title.duration_seconds for title in excluded) + 1 if excluded else 0
    )
    selected_by_cutoff = {
        title.title_index
        for title in inventory_titles
        if title.duration_seconds >= minimum_length
    }
    if selected_by_cutoff != selected_indexes:
        raise RipError(
            "Approved titles cannot be represented by one exact minimum-runtime cutoff"
        )

    ordered_jobs = tuple(sorted(jobs, key=lambda job: job.title_index))
    inventory_output_names = tuple(
        inventory_by_index[job.title_index].output_name for job in ordered_jobs
    )
    batch_output_names = tuple(
        _batch_output_name(output_prefixes[job.title_index], batch_ordinal)
        for batch_ordinal, job in enumerate(ordered_jobs)
    )
    if len({name.casefold() for name in batch_output_names}) != len(batch_output_names):
        raise RipError("Derived MakeMKV batch output filenames are ambiguous")
    return SingleOpenBatchPlan(
        drive_index=next(iter(drives)),
        minimum_length_seconds=minimum_length,
        jobs=ordered_jobs,
        inventory_output_names=inventory_output_names,
        batch_output_names=batch_output_names,
    )


def build_single_open_batch_command(
    executable: Path,
    plan: SingleOpenBatchPlan,
    destination: Path,
) -> tuple[str, ...]:
    """Build one exact-selection MakeMKV ``all`` command."""

    if plan.minimum_length_seconds < 0:
        raise RipError("Batch minimum title length cannot be negative")
    return (
        str(executable),
        "-r",
        "--noscan",
        "--messages=-stdout",
        "--progress=-same",
        f"--minlength={plan.minimum_length_seconds}",
        "mkv",
        f"disc:{plan.drive_index}",
        "all",
        str(destination),
    )


def _prepare_destinations(output_root: Path, plan: SingleOpenBatchPlan) -> list[Path]:
    destinations = [resolve_job_output(output_root, job) for job in plan.jobs]
    if len({path.resolve() for path in destinations}) != len(destinations):
        raise RipError("Single-open batch staging directories must be distinct")
    for destination in destinations:
        if destination.exists():
            raise RipError("Single-open batch staging collision; nothing was started")
    final_outputs = [resolve_final_output(output_root, job) for job in plan.jobs]
    concrete_finals = [path for path in final_outputs if path is not None]
    if len({path.resolve() for path in concrete_finals}) != len(concrete_finals):
        raise RipError("Single-open batch final destinations are not distinct")
    for final_output in concrete_finals:
        if final_output.exists():
            raise RipError("Single-open batch final collision; nothing was started")
    for destination in destinations:
        destination.mkdir(parents=True)
    return destinations


def verify_single_open_batch_outputs(
    workspace: Path,
    plan: SingleOpenBatchPlan,
) -> tuple[tuple[Path, int], ...]:
    """Read only names and sizes; do not distribute or alter batch outputs."""

    if not workspace.is_dir():
        raise RipError("MakeMKV batch workspace does not exist")
    outputs = tuple(path for path in workspace.glob("*.mkv") if path.is_file())
    expected = {name.casefold() for name in plan.batch_output_names}
    actual = {path.name.casefold() for path in outputs}
    if len(outputs) != len(plan.jobs) or actual != expected:
        raise RipError(
            "MakeMKV batch output set did not exactly match the saved inventory; "
            "all files were preserved"
        )

    by_name = {path.name.casefold(): path for path in outputs}
    verified: list[tuple[Path, int]] = []
    for job, output_name in zip(plan.jobs, plan.batch_output_names, strict=True):
        output = by_name[output_name.casefold()]
        output_bytes = output.stat().st_size
        # Exactly 0 bytes remains a graceful MakeMKV no-content skip. A small
        # nonzero file is complete only when the saved inventory also predicted
        # a tiny title (typically menu/control data) and at least half of that
        # estimate was written. Episode-sized plans retain the strict guard.
        if (
            0 < output_bytes < MINIMUM_SUBSTANTIAL_MKV_BYTES
            and not is_inventory_planned_tiny_output(job.estimated_bytes)
        ):
            raise RipError(
                "A batch MKV is unexpectedly small; all files were preserved"
            )
        if not is_complete_batch_output_size(
            actual_bytes=output_bytes,
            estimated_bytes=job.estimated_bytes,
        ):
            raise RipError(
                "A batch MKV is less than half its planned size; all files were "
                "preserved"
            )
        verified.append((output, output_bytes))
    return tuple(verified)


def run_single_open_batch(  # noqa: C901
    executable: Path,
    output_root: Path,
    plan: SingleOpenBatchPlan,
    event_log: JsonlRipLog,
    *,
    timeout_seconds: int = 7200,
    cancel_file: Path | None = None,
    on_event: Callable[[str, str], None] | None = None,
    popen_factory: Callable[..., subprocess.Popen[str]] = start_makemkv_process,
) -> list[RipResult]:
    """Run one exact ``all`` operation and distribute only verified outputs."""

    if timeout_seconds <= 0:
        raise RipError("Rip timeout must be positive")
    if not executable.is_file():
        raise RipError("MakeMKV executable was not found")
    if not output_root.is_dir():
        raise RipError("Approved output root does not exist")

    destinations = _prepare_destinations(output_root, plan)
    workspace = destinations[0]
    command = build_single_open_batch_command(executable, plan, workspace)
    started = datetime.now(UTC)
    event_log.write(
        "batch_started",
        drive_index=plan.drive_index,
        job_count=len(plan.jobs),
        minimum_length_seconds=plan.minimum_length_seconds,
    )
    try:
        process = popen_factory(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        event_log.write("batch_start_failed", error_type=type(exc).__name__)
        raise RipError(f"MakeMKV could not be started: {type(exc).__name__}") from exc
    if process.stdout is None:
        _stop_process(process)
        audit_makemkv_process_exit(process)
        raise RipError("MakeMKV output stream was unavailable")

    output_queue: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(
        target=_read_lines,
        args=(process.stdout, output_queue),
        daemon=True,
    )
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    stream_finished = False
    warnings = 0
    fatal = False
    last_progress = -1
    last_overall_progress = -1
    reported_job_ids: set[str] = set()
    estimated_total_bytes = sum(int(job.estimated_bytes or 0) for job in plan.jobs)
    throughput_sample = (time.monotonic(), 0)
    try:
        while not stream_finished or process.poll() is None:
            throughput_sample = sample_output_throughput(
                workspace, throughput_sample, "batch", on_event
            )
            _report_closed_batch_outputs(workspace, plan, reported_job_ids, on_event)
            if estimated_total_bytes > 0:
                overall_percent = min(
                    99,
                    int(throughput_sample[1] * 100 / estimated_total_bytes),
                )
                if overall_percent > last_overall_progress:
                    last_overall_progress = overall_percent
                    event_log.write("batch_overall_progress", percent=overall_percent)
                    if on_event is not None:
                        on_event("progress", f"overall: {overall_percent}%")
            if cancel_file is not None and cancel_file.exists():
                event_log.write("batch_cancel_requested")
                _stop_process(process)
                raise RipError(
                    "Batch cancellation was requested; partial files were preserved"
                )
            if time.monotonic() >= deadline:
                event_log.write("batch_timeout", timeout_seconds=timeout_seconds)
                _stop_process(process)
                raise RipError("Batch rip timed out; partial files were preserved")
            try:
                raw_line = output_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if raw_line is None:
                stream_finished = True
                continue
            line = sanitize_output(raw_line, workspace)
            level = classify_output(line)
            event_log.write("makemkv_output", level=level, message=line)
            warnings += level == "warning"
            if level == "fatal":
                fatal = True
                event_log.write("batch_fatal_output", message=line)
                _stop_process(process)
                break
            fraction = progress_fraction(line)
            if fraction is not None:
                percent = round(fraction * 100)
                if last_progress >= 0 and percent + 25 < last_progress:
                    last_progress = -1
                if percent >= last_progress + 5 or percent == 100:
                    last_progress = percent
                    event_log.write("batch_progress", percent=percent)
                    if on_event is not None:
                        on_event("progress", f"batch-phase: {percent}%")
            elif level == "warning" and on_event is not None:
                on_event(level, line)
    except KeyboardInterrupt as exc:
        event_log.write("batch_keyboard_interrupt")
        _stop_process(process)
        raise RipError(
            "Batch rip was interrupted; partial files were preserved"
        ) from exc
    finally:
        # Keep the physical-drive claim meaningful: no unexpected callback or
        # stream exception may return while this MakeMKV child is still alive.
        if process.poll() is None:
            _stop_process(process)
        reader.join(timeout=2)
        process.stdout.close()
        audit_makemkv_process_exit(process)

    return_code = process.wait(timeout=10)
    event_log.write(
        "batch_process_exit",
        return_code=return_code,
        warning_count=warnings,
    )
    if fatal:
        raise RipError("MakeMKV reported a fatal batch error; files were preserved")
    if return_code != 0:
        raise RipError(
            f"MakeMKV batch exited with code {return_code}; files were preserved"
        )

    verified = verify_single_open_batch_outputs(workspace, plan)
    results: list[RipResult] = []
    for job, destination, (source, output_bytes) in zip(
        plan.jobs, destinations, verified, strict=True
    ):
        finished = datetime.now(UTC)
        if output_bytes == 0:
            # MakeMKV produced nothing for this title (menu or navigation
            # track).  Record it as a graceful skip with no output file.
            result = RipResult(
                job_id=job.job_id,
                return_code=return_code,
                output_count=0,
                output_bytes=0,
                warning_count=warnings,
                started_at=started.isoformat(),
                finished_at=finished.isoformat(),
            )
            event_log.write("job_completed", **asdict(result))
            if on_event is not None:
                on_event("completed", f"{job.job_id}: skipped (no content)")
            results.append(result)
            continue
        final_output = resolve_final_output(output_root, job)
        target = (
            final_output
            if final_output is not None
            else destination / str(job.output_basename or source.name)
        )
        if target.exists() and target.resolve() != source.resolve():
            raise RipError(
                "Batch destination collision; remaining files were preserved"
            )
        if target.resolve() != source.resolve():
            if final_output is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
            try:
                source.rename(target)
            except OSError as exc:
                raise RipError(
                    "Could not distribute a verified batch MKV; files were preserved"
                ) from exc
        if final_output is not None:
            event_log.write(
                "job_output_finalized",
                job_id=job.job_id,
                output_basename=job.output_basename,
            )
        result = RipResult(
            job_id=job.job_id,
            return_code=return_code,
            output_count=1,
            output_bytes=output_bytes,
            warning_count=warnings,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
        )
        event_log.write("job_completed", **asdict(result))
        if on_event is not None:
            on_event("completed", f"{job.job_id}: completed")
        results.append(result)
    event_log.write("batch_completed", completed_count=len(results))
    return results
