"""Confirmed, resumable execution of reviewed HandBrake batch manifests."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, fields, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from mkv_episode_matcher.core.datetime_compat import UTC
from mkv_episode_matcher.media.handbrake import (
    HandBrakeError,
    HandBrakeJob,
    HandBrakeProcessError,
    HandBrakeProfile,
    HandBrakeResult,
    execute_handbrake_job,
    partial_output_path,
    validate_handbrake_profile,
)
from mkv_episode_matcher.media.handbrake_batch import (
    HandBrakeBatchJob,
    HandBrakeBatchManifest,
)


class HandBrakeBatchExecutionError(RuntimeError):
    """Raised when a batch cannot cross the safe execution boundary."""


@dataclass(frozen=True)
class LoadedBatchManifest:
    manifest: HandBrakeBatchManifest
    sha256: str


@dataclass(frozen=True)
class PreparedBatchJob:
    manifest_job: HandBrakeBatchJob
    handbrake_job: HandBrakeJob


@dataclass(frozen=True)
class _ManifestHeader:
    schema_version: int
    mode: str
    status: str
    job_count: int
    total_source_bytes: int
    required_free_bytes: int
    available_free_bytes: int
    missing_raw: list[Any]
    jobs_raw: list[Any]


@dataclass(frozen=True)
class BatchExecutionResult:
    status: str
    manifest_sha256: str
    job_count: int
    completed_ids: tuple[str, ...]
    resumed_ids: tuple[str, ...]
    failed_ids: tuple[str, ...]
    blocked_ids: tuple[str, ...]
    pending_ids: tuple[str, ...]
    event_log: Path

    def safe_report(self) -> dict[str, object]:
        return {
            "mode": "handbrake-batch-result",
            "status": self.status,
            "manifest_sha256": self.manifest_sha256,
            "job_count": self.job_count,
            "completed_ids": self.completed_ids,
            "resumed_ids": self.resumed_ids,
            "failed_ids": self.failed_ids,
            "blocked_ids": self.blocked_ids,
            "pending_ids": self.pending_ids,
        }


JobRunner = Callable[..., HandBrakeResult]
DiskUsage = Callable[[Path], Any]

_MANIFEST_KEYS = {
    "schema_version",
    "mode",
    "status",
    "profile",
    "job_count",
    "total_source_bytes",
    "required_free_bytes",
    "available_free_bytes",
    "missing_directories",
    "jobs",
}
_JOB_KEYS = {
    "media_id",
    "source_name",
    "source_size_bytes",
    "destination_relative",
}
_PROFILE_KEYS = {field.name for field in fields(HandBrakeProfile)}
_MAX_WORKERS = 2
_EVENT_LOG_NAME = "handbrake-batch-events.jsonl"
_STOP_MARKER = "STOP"
_PAUSE_MARKER = "PAUSE"


def _parse_profile(payload: Any) -> HandBrakeProfile:
    if not isinstance(payload, dict) or set(payload) != _PROFILE_KEYS:
        raise HandBrakeBatchExecutionError("Batch profile schema is invalid")
    try:
        profile = HandBrakeProfile(**payload)
        return validate_handbrake_profile(profile)
    except (HandBrakeError, TypeError, ValueError) as exc:
        raise HandBrakeBatchExecutionError("Batch profile is invalid") from exc


def _safe_relative_destination(value: Any) -> str:
    if not isinstance(value, str):
        raise HandBrakeBatchExecutionError("Batch destination is invalid")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "." in relative.parts
        or len(relative.parts) != 4
        or relative.suffix.lower() != ".mkv"
    ):
        raise HandBrakeBatchExecutionError("Batch destination is unsafe")
    return relative.as_posix()


def _parse_job(payload: Any) -> HandBrakeBatchJob:
    if not isinstance(payload, dict) or set(payload) != _JOB_KEYS:
        raise HandBrakeBatchExecutionError("Batch job schema is invalid")
    try:
        media_id = str(payload["media_id"])
        source_name = str(payload["source_name"])
        source_size = int(payload["source_size_bytes"])
        destination = _safe_relative_destination(payload["destination_relative"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HandBrakeBatchExecutionError("Batch job is invalid") from exc
    if (
        not media_id
        or not source_name
        or Path(source_name).name != source_name
        or Path(source_name).suffix.lower() != ".mkv"
        or source_size <= 0
    ):
        raise HandBrakeBatchExecutionError("Batch job fields are unsafe")
    return HandBrakeBatchJob(
        media_id=media_id,
        source_name=source_name,
        source_size_bytes=source_size,
        destination_relative=destination,
    )


def _parse_manifest_header(payload: Any) -> _ManifestHeader:
    payload_keys = set(payload) if isinstance(payload, dict) else set()
    if not isinstance(payload, dict) or payload_keys not in (
        _MANIFEST_KEYS,
        _MANIFEST_KEYS - {"schema_version"},
    ):
        raise HandBrakeBatchExecutionError("Batch manifest schema is invalid")
    try:
        header = _ManifestHeader(
            schema_version=int(payload.get("schema_version", 1)),
            mode=str(payload["mode"]),
            status=str(payload["status"]),
            job_count=int(payload["job_count"]),
            total_source_bytes=int(payload["total_source_bytes"]),
            required_free_bytes=int(payload["required_free_bytes"]),
            available_free_bytes=int(payload["available_free_bytes"]),
            missing_raw=payload["missing_directories"],
            jobs_raw=payload["jobs"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HandBrakeBatchExecutionError("Batch manifest fields are invalid") from exc
    if (
        header.schema_version not in {1, 2}
        or header.mode != "handbrake-batch-manifest"
        or header.status not in {"ready", "ready-after-directory-creation"}
        or header.job_count <= 0
        or header.total_source_bytes <= 0
        or header.required_free_bytes < header.total_source_bytes
        or header.available_free_bytes < 0
        or not isinstance(header.missing_raw, list)
        or not isinstance(header.jobs_raw, list)
    ):
        raise HandBrakeBatchExecutionError("Batch manifest is not executable")
    return header


def load_handbrake_batch_manifest(path: Path) -> LoadedBatchManifest:
    """Load and strictly validate one immutable, path-redacted batch manifest."""

    try:
        raw = path.read_bytes()
        payload: Any = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HandBrakeBatchExecutionError(
            "Batch manifest could not be loaded"
        ) from exc
    header = _parse_manifest_header(payload)
    profile = _parse_profile(payload["profile"])
    jobs = tuple(_parse_job(item) for item in header.jobs_raw)
    if len(jobs) != header.job_count:
        raise HandBrakeBatchExecutionError("Batch job count does not match manifest")
    if sum(job.source_size_bytes for job in jobs) != header.total_source_bytes:
        raise HandBrakeBatchExecutionError("Batch source total does not match manifest")
    if len({job.media_id for job in jobs}) != len(jobs):
        raise HandBrakeBatchExecutionError("Batch media IDs are not unique")
    if len({job.source_name.casefold() for job in jobs}) != len(jobs):
        raise HandBrakeBatchExecutionError("Batch source names are not unique")
    if len({job.destination_relative.casefold() for job in jobs}) != len(jobs):
        raise HandBrakeBatchExecutionError("Batch destinations are not unique")

    expected_parents = {
        PurePosixPath(job.destination_relative).parent.as_posix() for job in jobs
    }
    missing = tuple(sorted(str(item) for item in header.missing_raw))
    if len(set(missing)) != len(missing) or any(
        item not in expected_parents for item in missing
    ):
        raise HandBrakeBatchExecutionError(
            "Batch missing-directory list is not derived from jobs"
        )
    return LoadedBatchManifest(
        manifest=HandBrakeBatchManifest(
            schema_version=header.schema_version,
            mode=header.mode,
            status=header.status,
            profile={
                field.name: getattr(profile, field.name)
                for field in fields(HandBrakeProfile)
            },
            job_count=header.job_count,
            total_source_bytes=header.total_source_bytes,
            required_free_bytes=header.required_free_bytes,
            available_free_bytes=header.available_free_bytes,
            missing_directories=missing,
            jobs=jobs,
        ),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _profile_from_manifest(manifest: HandBrakeBatchManifest) -> HandBrakeProfile:
    return _parse_profile(manifest.profile)


def _resolve_destination(output_root: Path, relative_value: str) -> Path:
    relative = PurePosixPath(relative_value)
    destination = (output_root / Path(*relative.parts)).resolve()
    try:
        destination.relative_to(output_root)
    except ValueError as exc:
        raise HandBrakeBatchExecutionError(
            "Batch destination escapes output root"
        ) from exc
    return destination


def prepare_batch_jobs(
    loaded: LoadedBatchManifest,
    source_root: Path,
    output_root: Path,
    *,
    disk_usage: DiskUsage = shutil.disk_usage,
) -> tuple[PreparedBatchJob, ...]:
    """Resolve explicit roots and revalidate files, sizes, paths, and capacity."""

    source_root = source_root.resolve()
    output_root = output_root.resolve()
    if not source_root.is_dir() or not output_root.is_dir():
        raise HandBrakeBatchExecutionError(
            "Batch source and output roots must already exist"
        )
    if source_root == output_root:
        raise HandBrakeBatchExecutionError(
            "Batch source and output roots must be separate"
        )
    try:
        free_bytes = int(disk_usage(output_root).free)
    except OSError as exc:
        raise HandBrakeBatchExecutionError(
            f"Batch free space could not be checked: {type(exc).__name__}"
        ) from exc
    if free_bytes < loaded.manifest.required_free_bytes:
        raise HandBrakeBatchExecutionError("Batch output root has insufficient space")

    profile = _profile_from_manifest(loaded.manifest)
    prepared: list[PreparedBatchJob] = []
    for manifest_job in loaded.manifest.jobs:
        source = (source_root / manifest_job.source_name).resolve()
        try:
            source.relative_to(source_root)
        except ValueError as exc:
            raise HandBrakeBatchExecutionError(
                "Batch source escapes source root"
            ) from exc
        if (
            not source.is_file()
            or source.suffix.lower() != ".mkv"
            or source.stat().st_size != manifest_job.source_size_bytes
        ):
            raise HandBrakeBatchExecutionError(
                "Batch source is missing or changed since planning"
            )
        destination = _resolve_destination(
            output_root,
            manifest_job.destination_relative,
        )
        if destination.parent == source_root or destination == source:
            raise HandBrakeBatchExecutionError(
                "Batch destination overlaps source staging"
            )
        prepared.append(
            PreparedBatchJob(
                manifest_job=manifest_job,
                handbrake_job=HandBrakeJob(
                    media_id=manifest_job.media_id,
                    source=source,
                    destination=destination,
                    profile=profile,
                ),
            )
        )
    return tuple(prepared)


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HandBrakeBatchExecutionError("Batch event log is invalid") from exc

    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1:
                break
            raise HandBrakeBatchExecutionError("Batch event log is invalid") from exc
        if not isinstance(item, dict):
            raise HandBrakeBatchExecutionError("Batch event log is invalid")
        events.append(item)
    return events


def _append_event(
    path: Path,
    lock: threading.Lock,
    event: str,
    **fields: object,
) -> None:
    payload = {
        "at": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    with lock, path.open("a", encoding="utf-8") as stream:
        if path.exists() and path.stat().st_size > 0:
            tail = path.read_bytes()[-1:]
            if tail not in (b"\n",):
                stream.write("\n")
        stream.write(json.dumps(payload, sort_keys=True))
        stream.write("\n")


def _dispatched_attempts(events: list[dict[str, Any]]) -> dict[str, int]:
    attempts: dict[str, int] = {}
    for event in events:
        if event.get("event") != "job-dispatched":
            continue
        media_id = str(event.get("media_id", ""))
        attempt = event.get("attempt_number", 1)
        if (
            not media_id
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 1
        ):
            raise HandBrakeBatchExecutionError(
                "Batch event log has an invalid dispatched job event"
            )
        attempts[media_id] = max(attempts.get(media_id, 0), attempt)
    return attempts


def _completed_from_events(
    events: list[dict[str, Any]],
    manifest_sha256: str,
) -> dict[str, int]:
    if not events:
        return {}
    first = events[0]
    if (
        first.get("event") != "batch-started"
        or first.get("manifest_sha256") != manifest_sha256
    ):
        raise HandBrakeBatchExecutionError(
            "Batch event log belongs to a different manifest"
        )
    completed: dict[str, int] = {}
    for event in events:
        if event.get("event") == "job-completed":
            try:
                completed[str(event["media_id"])] = int(event["output_bytes"])
            except (KeyError, TypeError, ValueError) as exc:
                raise HandBrakeBatchExecutionError(
                    "Batch completion event is invalid"
                ) from exc
    return completed


def _marker_status(run_dir: Path) -> str | None:
    if (run_dir / _STOP_MARKER).exists():
        return "stopped"
    if (run_dir / _PAUSE_MARKER).exists():
        return "paused"
    return None


def _validate_resumed_outputs(
    prepared: tuple[PreparedBatchJob, ...],
    completed: dict[str, int],
) -> tuple[str, ...]:
    known_ids = {job.manifest_job.media_id for job in prepared}
    if not set(completed).issubset(known_ids):
        raise HandBrakeBatchExecutionError(
            "Batch event log contains an unknown completed media ID"
        )
    resumed: list[str] = []
    for item in prepared:
        media_id = item.manifest_job.media_id
        if media_id not in completed:
            continue
        destination = item.handbrake_job.destination
        if (
            not destination.is_file()
            or destination.stat().st_size != completed[media_id]
        ):
            raise HandBrakeBatchExecutionError(
                "Previously completed batch output is missing or changed"
            )
        resumed.append(media_id)
    return tuple(resumed)


def load_verified_encoded_outputs(
    loaded: LoadedBatchManifest,
    output_root: Path,
    event_log: Path,
) -> dict[str, Path]:
    """Resolve only encoded outputs proven complete by the append-only event log."""

    output_root = output_root.resolve()
    if not output_root.is_dir():
        raise HandBrakeBatchExecutionError("Encoded output root is unavailable")
    events = _read_events(event_log)
    completed = _completed_from_events(events, loaded.sha256)
    jobs_by_id = {job.media_id: job for job in loaded.manifest.jobs}
    if set(completed) != set(jobs_by_id):
        raise HandBrakeBatchExecutionError(
            "HandBrake batch is not fully completed and verified"
        )
    resolved: dict[str, Path] = {}
    for media_id, job in jobs_by_id.items():
        destination = _resolve_destination(output_root, job.destination_relative)
        if (
            not destination.is_file()
            or destination.stat().st_size != completed[media_id]
        ):
            raise HandBrakeBatchExecutionError(
                "A verified encoded output is missing or changed"
            )
        resolved[media_id] = destination
    return resolved


def _classify_pending_jobs(
    prepared: tuple[PreparedBatchJob, ...],
    resumed: tuple[str, ...],
    events: list[dict[str, Any]],
) -> tuple[list[PreparedBatchJob], list[str]]:
    resumed_set = set(resumed)
    dispatched_attempts = _dispatched_attempts(events)
    pending: list[PreparedBatchJob] = []
    blocked: list[str] = []
    for item in prepared:
        media_id = item.manifest_job.media_id
        if media_id in resumed_set:
            continue
        if item.handbrake_job.destination.exists():
            blocked.append(media_id)
            continue
        next_attempt = dispatched_attempts.get(media_id, 0) + 1
        retry_job = replace(item.handbrake_job, attempt_number=next_attempt)
        while partial_output_path(retry_job).exists():
            retry_job = replace(
                retry_job,
                attempt_number=retry_job.attempt_number + 1,
            )
        pending.append(replace(item, handbrake_job=retry_job))
    return pending, blocked


def _create_reviewed_directories(jobs: tuple[PreparedBatchJob, ...]) -> None:
    for directory in sorted(
        {item.handbrake_job.destination.parent for item in jobs},
        key=lambda item: str(item).casefold(),
    ):
        directory.mkdir(parents=True, exist_ok=True)


def _run_one(
    runner: JobRunner,
    executable: Path,
    ffprobe: Path,
    item: PreparedBatchJob,
    run_dir: Path,
    timeout_seconds: int,
) -> HandBrakeResult:
    return runner(
        executable,
        ffprobe,
        item.handbrake_job,
        run_dir,
        confirm_transcode=True,
        timeout_seconds=timeout_seconds,
    )


def _validate_execution_options(
    executable: Path,
    ffprobe: Path,
    run_dir: Path,
    *,
    confirm_transcode: bool,
    max_workers: int,
    max_jobs: int | None,
    timeout_seconds: int,
) -> None:
    if not confirm_transcode:
        raise HandBrakeBatchExecutionError(
            "Batch execution requires explicit confirmation"
        )
    if not executable.is_file() or not ffprobe.is_file():
        raise HandBrakeBatchExecutionError("Batch external tool is unavailable")
    if not run_dir.is_dir():
        raise HandBrakeBatchExecutionError("Batch run-log directory must already exist")
    if not 1 <= max_workers <= _MAX_WORKERS:
        raise HandBrakeBatchExecutionError("Batch workers must be between 1 and 2")
    if max_jobs is not None and max_jobs <= 0:
        raise HandBrakeBatchExecutionError("Batch job limit must be positive")
    if timeout_seconds <= 0:
        raise HandBrakeBatchExecutionError("Batch timeout must be positive")


def validate_batch_run_dir_scope(
    run_dir: Path,
    source_root: Path,
    output_root: Path,
) -> Path:
    """Require operational logs to remain outside source and output trees."""

    resolved = run_dir.resolve()
    for media_root in (source_root.resolve(), output_root.resolve()):
        try:
            resolved.relative_to(media_root)
        except ValueError:
            continue
        raise HandBrakeBatchExecutionError(
            "Batch run-log directory must be outside media roots"
        )
    return resolved


def _initialize_batch_events(
    event_log: Path,
    lock: threading.Lock,
    loaded: LoadedBatchManifest,
    *,
    max_workers: int,
    max_jobs: int,
    resumed: tuple[str, ...],
    pending_count: int,
    blocked: list[str],
) -> None:
    events = _read_events(event_log)
    if not events:
        _append_event(
            event_log,
            lock,
            "batch-started",
            manifest_sha256=loaded.sha256,
            job_count=loaded.manifest.job_count,
            max_workers=max_workers,
            max_jobs=max_jobs,
        )
    else:
        _append_event(
            event_log,
            lock,
            "batch-resumed",
            manifest_sha256=loaded.sha256,
            resumed_count=len(resumed),
            pending_count=pending_count,
        )
    for media_id in blocked:
        _append_event(
            event_log,
            lock,
            "job-blocked-existing-output",
            media_id=media_id,
        )


def _run_pending_chunks(
    pending: list[PreparedBatchJob],
    *,
    executable: Path,
    ffprobe: Path,
    run_dir: Path,
    event_log: Path,
    lock: threading.Lock,
    max_workers: int,
    timeout_seconds: int,
    job_runner: JobRunner,
) -> tuple[list[str], list[str], bool]:
    completed: list[str] = []
    failed: list[str] = []
    interrupted = False
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="handbrake-batch",
    ) as executor:
        while pending and _marker_status(run_dir) is None:
            chunk = [pending.pop(0) for _ in range(min(max_workers, len(pending)))]
            futures: dict[Future[HandBrakeResult], PreparedBatchJob] = {}
            for item in chunk:
                media_id = item.manifest_job.media_id
                _append_event(
                    event_log,
                    lock,
                    "job-dispatched",
                    media_id=media_id,
                    attempt_number=item.handbrake_job.attempt_number,
                )
                future = executor.submit(
                    _run_one,
                    job_runner,
                    executable,
                    ffprobe,
                    item,
                    run_dir,
                    timeout_seconds,
                )
                futures[future] = item
            for future in as_completed(futures):
                item = futures[future]
                media_id = item.manifest_job.media_id
                try:
                    result = future.result()
                except Exception as exc:
                    failed.append(media_id)
                    interrupted = interrupted or (
                        isinstance(exc, HandBrakeProcessError) and exc.interrupted
                    )
                    _append_event(
                        event_log,
                        lock,
                        "job-failed",
                        media_id=media_id,
                        error_type=type(exc).__name__,
                        attempt_number=item.handbrake_job.attempt_number,
                    )
                else:
                    completed.append(media_id)
                    _append_event(
                        event_log,
                        lock,
                        "job-completed",
                        media_id=media_id,
                        attempt_number=item.handbrake_job.attempt_number,
                        output_bytes=result.output_bytes,
                        duration_seconds=result.duration_seconds,
                        video_codec=result.video_codec,
                        audio_streams=result.audio_streams,
                        subtitle_streams=result.subtitle_streams,
                    )
            if interrupted:
                _append_event(
                    event_log,
                    lock,
                    "batch-interruption-detected",
                    failed_count=len(failed),
                    pending_count=len(pending),
                )
                break
    return completed, failed, interrupted


def _batch_status(
    marker: str | None,
    *,
    failed: list[str],
    blocked: list[str],
    pending_ids: tuple[str, ...],
    limit_reached: bool,
    interrupted: bool,
) -> str:
    if marker is not None:
        return marker
    if interrupted:
        return "interrupted"
    if failed or blocked:
        return "completed-with-failures"
    if limit_reached:
        return "limited"
    if pending_ids:
        return "incomplete"
    return "completed"


def execute_handbrake_batch(
    executable: Path,
    ffprobe: Path,
    loaded: LoadedBatchManifest,
    source_root: Path,
    output_root: Path,
    run_dir: Path,
    *,
    confirm_transcode: bool = False,
    max_workers: int = 2,
    max_jobs: int | None = None,
    timeout_seconds: int = 21600,
    job_runner: JobRunner = execute_handbrake_job,
    disk_usage: DiskUsage = shutil.disk_usage,
) -> BatchExecutionResult:
    """Execute a reviewed manifest with bounded workers and resumable events."""

    _validate_execution_options(
        executable,
        ffprobe,
        run_dir,
        confirm_transcode=confirm_transcode,
        max_workers=max_workers,
        max_jobs=max_jobs,
        timeout_seconds=timeout_seconds,
    )
    effective_max_jobs = loaded.manifest.job_count if max_jobs is None else max_jobs
    if effective_max_jobs > loaded.manifest.job_count:
        raise HandBrakeBatchExecutionError("Batch job limit exceeds manifest job count")
    validate_batch_run_dir_scope(run_dir, source_root, output_root)
    prepared = prepare_batch_jobs(
        loaded,
        source_root,
        output_root,
        disk_usage=disk_usage,
    )
    event_log = run_dir / _EVENT_LOG_NAME
    events = _read_events(event_log)
    completed_sizes = _completed_from_events(events, loaded.sha256)
    resumed = _validate_resumed_outputs(prepared, completed_sizes)
    pending, blocked = _classify_pending_jobs(prepared, resumed, events)
    lock = threading.Lock()
    _initialize_batch_events(
        event_log,
        lock,
        loaded,
        max_workers=max_workers,
        max_jobs=effective_max_jobs,
        resumed=resumed,
        pending_count=len(pending),
        blocked=blocked,
    )
    marker = _marker_status(run_dir)
    runnable = pending[:effective_max_jobs]
    deferred = pending[effective_max_jobs:]
    if marker is None:
        _create_reviewed_directories(tuple(runnable))
    completed_now, failed, interrupted = _run_pending_chunks(
        runnable,
        executable=executable,
        ffprobe=ffprobe,
        run_dir=run_dir,
        event_log=event_log,
        lock=lock,
        max_workers=max_workers,
        timeout_seconds=timeout_seconds,
        job_runner=job_runner,
    )
    marker = _marker_status(run_dir)
    pending_ids = tuple(item.manifest_job.media_id for item in (*runnable, *deferred))
    status = _batch_status(
        marker,
        failed=failed,
        blocked=blocked,
        pending_ids=pending_ids,
        limit_reached=marker is None and bool(deferred),
        interrupted=interrupted,
    )
    all_completed = (*resumed, *completed_now)
    _append_event(
        event_log,
        lock,
        f"batch-{status}",
        manifest_sha256=loaded.sha256,
        completed_count=len(all_completed),
        failed_count=len(failed),
        blocked_count=len(blocked),
        pending_count=len(pending_ids),
    )
    return BatchExecutionResult(
        status=status,
        manifest_sha256=loaded.sha256,
        job_count=loaded.manifest.job_count,
        completed_ids=all_completed,
        resumed_ids=resumed,
        failed_ids=tuple(failed),
        blocked_ids=tuple(blocked),
        pending_ids=pending_ids,
        event_log=event_log,
    )
