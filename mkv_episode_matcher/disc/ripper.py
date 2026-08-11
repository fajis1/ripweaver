"""Explicitly authorized, sequential MakeMKV title execution.

Planning remains outside this module. Each job represents one already-approved
title and receives its own new staging directory so MakeMKV cannot overwrite an
existing media file.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO


class RipError(RuntimeError):
    """Raised when a rip cannot start or must stop safely."""


@dataclass(frozen=True)
class RipJob:
    """One approved MakeMKV title staged under an ordinal identifier."""

    job_id: str
    drive_index: int
    title_index: int
    relative_output_dir: str
    estimated_bytes: int | None = None
    output_basename: str | None = None
    final_relative_dir: str | None = None


@dataclass(frozen=True)
class RipResult:
    """Verified output summary without retaining source or output filenames."""

    job_id: str
    return_code: int
    output_count: int
    output_bytes: int
    warning_count: int
    started_at: str
    finished_at: str


FATAL_PATTERNS = (
    "failed to open disc",
    "failed to save title",
    "copy complete. 0 titles saved",
    "no titles saved",
    "evaluation period has expired",
    "application failed to initialize",
    "scsi error",
)
WARNING_PATTERNS = (
    "warning",
    "corrupt",
    "invalid at offset",
    "attempting to work around",
    "damaged vob",
    "skipped",
)
JOB_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PROGRESS_PATTERN = re.compile(r"^PRGV:(\d+),(\d+),(\d+)")


def validate_job(job: RipJob) -> None:
    """Validate all values that can reach a MakeMKV command or output path."""

    if JOB_ID_PATTERN.fullmatch(job.job_id) is None:
        raise RipError("Rip job ID contains unsupported characters")
    if not 0 <= job.drive_index <= 99:
        raise RipError("MakeMKV drive index is outside the supported range")
    if not 0 <= job.title_index <= 9999:
        raise RipError("MakeMKV title index is outside the supported range")
    relative = Path(job.relative_output_dir)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise RipError("Rip output directory must be a safe relative path")
    if job.final_relative_dir is not None:
        final_relative = Path(job.final_relative_dir)
        if (
            final_relative.is_absolute()
            or ".." in final_relative.parts
            or not final_relative.parts
        ):
            raise RipError("Final media directory must be a safe relative path")
        if job.output_basename is None:
            raise RipError("A final media directory requires a unique output filename")
    if job.output_basename is not None:
        output_name = Path(job.output_basename)
        if (
            output_name.name != job.output_basename
            or output_name.suffix.lower() != ".mkv"
            or re.fullmatch(r"[A-Za-z0-9._ -]{1,180}", job.output_basename) is None
        ):
            raise RipError("Rip output filename is not a safe MKV basename")


def resolve_job_output(output_root: Path, job: RipJob) -> Path:
    """Resolve and contain a job's staging directory beneath the output root."""

    validate_job(job)
    root = output_root.resolve()
    destination = (root / job.relative_output_dir).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise RipError("Rip output escaped the approved output root") from exc
    return destination


def resolve_final_output(output_root: Path, job: RipJob) -> Path | None:
    """Resolve a flat final MKV path beneath the approved output root."""

    validate_job(job)
    if job.final_relative_dir is None:
        return None
    root = output_root.resolve()
    destination = (root / job.final_relative_dir / str(job.output_basename)).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise RipError("Final media output escaped the approved output root") from exc
    return destination


def build_rip_command(
    executable: Path,
    job: RipJob,
    destination: Path,
) -> tuple[str, ...]:
    """Build the sole allowed MakeMKV mutation: one title to one new folder."""

    validate_job(job)
    return (
        str(executable),
        "-r",
        "--messages=-stdout",
        "--progress=-same",
        "--minlength=0",
        "mkv",
        f"disc:{job.drive_index}",
        str(job.title_index),
        str(destination),
    )


def classify_output(line: str) -> str:
    """Classify one sanitized MakeMKV output line."""

    lowered = line.lower()
    if any(pattern in lowered for pattern in FATAL_PATTERNS):
        return "fatal"
    if any(pattern in lowered for pattern in WARNING_PATTERNS):
        return "warning"
    if line.startswith("PRGV:"):
        return "progress"
    return "info"


def sanitize_output(line: str, destination: Path) -> str:
    """Redact hardware identity and destination paths from MakeMKV output."""

    stripped = line.rstrip("\r\n")
    if stripped.startswith("DRV:"):
        drive_id = stripped.split(",", 1)[0]
        return f"{drive_id},<hardware-redacted>"

    stripped = re.sub(
        r"(?i)(reading\s+)'[^']+'",
        r"\1'<hardware-redacted>'",
        stripped,
    )
    stripped = re.sub(
        r'(?i)"(?:BD-ROM|BD-RE|DVD(?:ROM|RW)?|CD-ROM)[^"]*"',
        '"<hardware-redacted>"',
        stripped,
    )

    candidates = {
        str(destination),
        str(destination.resolve()),
        str(destination).replace("\\", "/"),
        str(destination.resolve()).replace("\\", "/"),
    }
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate:
            stripped = stripped.replace(candidate, "<output>")
    return stripped


def progress_fraction(line: str) -> float | None:
    """Parse MakeMKV robot progress into a zero-to-one fraction."""

    match = PROGRESS_PATTERN.match(line)
    if match is None:
        return None
    current, _subcurrent, maximum = (int(value) for value in match.groups())
    if maximum <= 0:
        return None
    return max(0.0, min(1.0, current / maximum))


def sample_output_throughput(
    root: Path,
    previous: tuple[float, int],
    scope: str,
    on_event: Callable[[str, str], None] | None,
    *,
    interval_seconds: float = 2.0,
) -> tuple[float, int]:
    """Emit path-free staged MKV growth throughput at a bounded interval."""

    now = time.monotonic()
    if now - previous[0] < interval_seconds:
        return previous
    try:
        size = sum(
            path.stat().st_size for path in root.rglob("*.mkv") if path.is_file()
        )
    except OSError:
        return previous
    elapsed = now - previous[0]
    delta = max(0, size - previous[1])
    if on_event is not None and previous[1] > 0 and delta > 0:
        on_event("throughput", f"{scope}: {delta / elapsed / (1024**2):.2f} MiB/s")
    return now, size


class JsonlRipLog:
    """Append-only structured event log with no command or source paths."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream: IO[str] = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, event: str, **fields: object) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            **fields,
        }
        with self._lock:
            self._stream.write(json.dumps(payload, sort_keys=True) + "\n")
            self._stream.flush()

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> JsonlRipLog:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _read_lines(stream: IO[str], output_queue: queue.Queue[str | None]) -> None:
    try:
        for line in iter(stream.readline, ""):
            output_queue.put(line)
    finally:
        output_queue.put(None)


def _verify_output(destination: Path, estimated_bytes: int | None) -> tuple[Path, int]:
    outputs = list(destination.glob("*.mkv"))
    if len(outputs) != 1:
        raise RipError(
            f"Expected one MKV output but found {len(outputs)}; partial files were preserved"
        )
    output_bytes = outputs[0].stat().st_size
    if output_bytes < 1_000_000:
        raise RipError("MKV output is unexpectedly small; the file was preserved")
    if estimated_bytes and output_bytes < estimated_bytes * 0.5:
        raise RipError(
            "MKV output is less than half its planned size; the file was preserved"
        )
    return outputs[0], output_bytes


def run_rip_job(  # noqa: C901 - guarded external-process state machine
    executable: Path,
    output_root: Path,
    job: RipJob,
    event_log: JsonlRipLog,
    *,
    timeout_seconds: int = 7200,
    cancel_file: Path | None = None,
    cancel_event: threading.Event | None = None,
    on_event: Callable[[str, str], None] | None = None,
    popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> RipResult:
    """Run one title, stream structured logs, and preserve failures for review."""

    if timeout_seconds <= 0:
        raise RipError("Rip timeout must be positive")
    if not executable.is_file():
        raise RipError("MakeMKV executable was not found")
    if not output_root.is_dir():
        raise RipError("Approved output root does not exist")

    destination = resolve_job_output(output_root, job)
    if destination.exists():
        raise RipError(
            f"Staging directory for {job.job_id} already exists; refusing overwrite"
        )
    destination.mkdir(parents=True)

    command = build_rip_command(executable, job, destination)
    started = datetime.now(UTC)
    event_log.write(
        "job_started",
        job_id=job.job_id,
        drive_index=job.drive_index,
        title_index=job.title_index,
        estimated_bytes=job.estimated_bytes,
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
        event_log.write(
            "job_start_failed",
            job_id=job.job_id,
            error_type=type(exc).__name__,
        )
        raise RipError(f"MakeMKV could not be started: {type(exc).__name__}") from exc

    if process.stdout is None:
        _stop_process(process)
        raise RipError("MakeMKV output stream was unavailable")

    output_queue: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(
        target=_read_lines,
        args=(process.stdout, output_queue),
        daemon=True,
    )
    reader.start()

    warnings = 0
    fatal_message: str | None = None
    last_progress = -1
    throughput_sample = (time.monotonic(), 0)
    deadline = time.monotonic() + timeout_seconds
    stream_finished = False

    try:
        while not stream_finished or process.poll() is None:
            throughput_sample = sample_output_throughput(
                destination, throughput_sample, job.job_id, on_event
            )
            if (cancel_file is not None and cancel_file.exists()) or (
                cancel_event is not None and cancel_event.is_set()
            ):
                event_log.write("job_cancel_requested", job_id=job.job_id)
                _stop_process(process)
                raise RipError(
                    "Rip cancellation was requested; partial files were preserved"
                )
            if time.monotonic() >= deadline:
                event_log.write(
                    "job_timeout",
                    job_id=job.job_id,
                    timeout_seconds=timeout_seconds,
                )
                _stop_process(process)
                raise RipError("Rip timed out; partial files were preserved")

            try:
                raw_line = output_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if raw_line is None:
                stream_finished = True
                continue

            line = sanitize_output(raw_line, destination)
            classification = classify_output(line)
            event_log.write(
                "makemkv_output",
                job_id=job.job_id,
                level=classification,
                message=line,
            )
            if classification == "warning":
                warnings += 1
            if classification == "fatal":
                fatal_message = line
                event_log.write(
                    "job_fatal_output",
                    job_id=job.job_id,
                    message=line,
                )
                _stop_process(process)
                break

            fraction = progress_fraction(line)
            if fraction is not None:
                percent = round(fraction * 100)
                if last_progress >= 0 and percent + 25 < last_progress:
                    last_progress = -1
                if percent >= last_progress + 5 or percent == 100:
                    last_progress = percent
                    event_log.write(
                        "job_progress",
                        job_id=job.job_id,
                        percent=percent,
                    )
                    if on_event is not None:
                        on_event("progress", f"{job.job_id}: {percent}%")
            elif classification in {"warning", "fatal"} and on_event is not None:
                on_event(classification, f"{job.job_id}: {line}")
    except KeyboardInterrupt as exc:
        event_log.write("job_keyboard_interrupt", job_id=job.job_id)
        _stop_process(process)
        raise RipError("Rip was interrupted; partial files were preserved") from exc
    finally:
        reader.join(timeout=2)
        process.stdout.close()

    return_code = process.wait(timeout=10)
    event_log.write(
        "job_process_exit",
        job_id=job.job_id,
        return_code=return_code,
        warning_count=warnings,
    )
    if fatal_message is not None:
        raise RipError(f"MakeMKV reported a fatal error for {job.job_id}; queue paused")
    if return_code != 0:
        raise RipError(
            f"MakeMKV exited with code {return_code} for {job.job_id}; queue paused"
        )

    output_path, output_bytes = _verify_output(
        destination,
        job.estimated_bytes,
    )
    final_output = resolve_final_output(output_root, job)
    if final_output is not None:
        if final_output.exists():
            raise RipError(
                "Final media filename already exists; staged output was preserved"
            )
        final_output.parent.mkdir(parents=True, exist_ok=True)
        try:
            output_path.rename(final_output)
        except OSError as exc:
            raise RipError(
                "Could not finalize the verified MKV; staged output was preserved"
            ) from exc
        if not final_output.is_file() or final_output.stat().st_size != output_bytes:
            raise RipError("Finalized MKV verification failed")
        event_log.write(
            "job_output_finalized",
            job_id=job.job_id,
            output_basename=job.output_basename,
        )
    elif job.output_basename and output_path.name != job.output_basename:
        named_output = destination / job.output_basename
        if named_output.exists():
            raise RipError(
                "Unique staging filename already exists; original output was preserved"
            )
        try:
            output_path.rename(named_output)
        except OSError as exc:
            raise RipError(
                "Could not assign the unique staging filename; original output "
                "was preserved"
            ) from exc
        event_log.write(
            "job_output_named",
            job_id=job.job_id,
            output_basename=job.output_basename,
        )
    finished = datetime.now(UTC)
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
    return result


def run_rip_queue(
    executable: Path,
    output_root: Path,
    jobs: Iterable[RipJob],
    log_path: Path,
    *,
    timeout_seconds: int = 7200,
    cancel_file: Path | None = None,
    on_event: Callable[[str, str], None] | None = None,
) -> list[RipResult]:
    """Run approved jobs sequentially and stop the queue on the first error."""

    job_list = list(jobs)
    if not job_list:
        raise RipError("Approved rip queue is empty")
    if len({job.job_id for job in job_list}) != len(job_list):
        raise RipError("Approved rip queue contains duplicate job IDs")

    with JsonlRipLog(log_path) as event_log:
        event_log.write("queue_started", job_count=len(job_list))
        results: list[RipResult] = []
        try:
            for job in job_list:
                results.append(
                    run_rip_job(
                        executable,
                        output_root,
                        job,
                        event_log,
                        timeout_seconds=timeout_seconds,
                        cancel_file=cancel_file,
                        on_event=on_event,
                    )
                )
        except RipError as exc:
            event_log.write(
                "queue_paused",
                completed_count=len(results),
                error=str(exc),
            )
            raise
        event_log.write("queue_completed", completed_count=len(results))
        return results


def run_parallel_rip_queue(  # noqa: C901 - bounded parallel drive coordinator
    executable: Path,
    output_root: Path,
    jobs: Iterable[RipJob],
    log_dir: Path,
    *,
    timeout_seconds: int = 7200,
    cancel_file: Path | None = None,
    max_drives: int | None = None,
    on_event: Callable[[str, str], None] | None = None,
) -> list[RipResult]:
    """Run one sequential worker per drive and isolate drive-specific errors."""

    job_list = list(jobs)
    if not job_list:
        raise RipError("Approved rip queue is empty")
    if len({job.job_id for job in job_list}) != len(job_list):
        raise RipError("Approved rip queue contains duplicate job IDs")

    jobs_by_drive: dict[int, list[RipJob]] = {}
    for job in job_list:
        validate_job(job)
        jobs_by_drive.setdefault(job.drive_index, []).append(job)

    worker_count = max_drives or len(jobs_by_drive)
    if worker_count <= 0:
        raise RipError("Parallel drive limit must be positive")
    worker_count = min(worker_count, len(jobs_by_drive))
    log_dir.mkdir(parents=True, exist_ok=True)

    coordinator_path = log_dir / "parallel-coordinator.jsonl"
    with JsonlRipLog(coordinator_path) as coordinator:
        coordinator.write(
            "parallel_queue_started",
            drive_count=len(jobs_by_drive),
            job_count=len(job_list),
            max_drives=worker_count,
        )

        def run_drive(drive_index: int, drive_jobs: list[RipJob]) -> list[RipResult]:
            drive_log_path = log_dir / f"drive-{drive_index:02d}.jsonl"
            with JsonlRipLog(drive_log_path) as drive_log:
                drive_log.write(
                    "drive_worker_started",
                    drive_index=drive_index,
                    job_count=len(drive_jobs),
                )
                results: list[RipResult] = []
                for job in drive_jobs:
                    results.append(
                        run_rip_job(
                            executable,
                            output_root,
                            job,
                            drive_log,
                            timeout_seconds=timeout_seconds,
                            cancel_file=cancel_file,
                            on_event=on_event,
                        )
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
            for future in as_completed(futures):
                drive_index = futures[future]
                try:
                    collected.extend(future.result())
                except Exception as exc:
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
                        error=str(first_error),
                    )

        if first_error is not None:
            coordinator.write(
                "parallel_queue_completed_with_failures",
                completed_count=len(collected),
                error=str(first_error),
            )
            raise first_error

        order = {job.job_id: index for index, job in enumerate(job_list)}
        collected.sort(key=lambda result: order[result.job_id])
        coordinator.write(
            "parallel_queue_completed",
            completed_count=len(collected),
        )
        return collected
