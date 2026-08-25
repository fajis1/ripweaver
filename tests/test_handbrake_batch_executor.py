import json
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.media.handbrake import (
    HandBrakeError,
    HandBrakeJob,
    HandBrakeProcessError,
    HandBrakeProfile,
    HandBrakeResult,
    partial_output_path,
)
from mkv_episode_matcher.media.handbrake_batch_executor import (
    BatchExecutionResult,
    HandBrakeBatchExecutionError,
    execute_handbrake_batch,
    load_handbrake_batch_manifest,
)


def _workspace(tmp_path: Path, job_count: int = 3):
    source_root = tmp_path / "private-sources"
    output_root = tmp_path / "encoded-output"
    run_dir = tmp_path / "redacted-events"
    source_root.mkdir()
    output_root.mkdir()
    run_dir.mkdir()
    handbrake = tmp_path / "HandBrakeCLI.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    handbrake.touch()
    ffprobe.touch()

    jobs = []
    total = 0
    for index in range(job_count):
        source_name = f"source-{index}.mkv"
        source_size = 10 + index
        (source_root / source_name).write_bytes(b"x" * source_size)
        total += source_size
        jobs.append({
            "media_id": f"media-{index}",
            "source_name": source_name,
            "source_size_bytes": source_size,
            "destination_relative": (
                "encoded-staging/Test Series/Season 01/"
                f"Test Series - S01E{index + 1:02d}.mkv"
            ),
        })

    profile = HandBrakeProfile(
        quality=26,
        content_kind="live_action",
        nlmeans_preset="ultralight",
        nlmeans_tune="film",
    )
    payload = {
        "mode": "handbrake-batch-manifest",
        "status": "ready-after-directory-creation",
        "profile": asdict(profile),
        "job_count": job_count,
        "total_source_bytes": total,
        "required_free_bytes": total,
        "available_free_bytes": total * 100,
        "missing_directories": [
            "encoded-staging/Test Series/Season 01",
        ],
        "jobs": jobs,
    }
    manifest_path = tmp_path / "batch.safe.json"
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return (
        manifest_path,
        source_root,
        output_root,
        run_dir,
        handbrake,
        ffprobe,
    )


def _successful_runner(calls: list[str] | None = None):
    def run(_handbrake, _ffprobe, job, run_dir, **_kwargs):
        if calls is not None:
            calls.append(job.media_id)
        content = f"encoded-{job.media_id}".encode()
        job.destination.write_bytes(content)
        return HandBrakeResult(
            media_id=job.media_id,
            encoder=job.profile.encoder,
            output_bytes=len(content),
            duration_seconds=1200.0,
            video_codec="hevc",
            audio_streams=2,
            subtitle_streams=0,
            process_log=run_dir / f"{job.media_id}.log",
            event_log=run_dir / f"{job.media_id}.jsonl",
        )

    return run


def _build_handbrake_job(
    media_id: str,
    source_root: Path,
    destination_root: Path,
    source_name: str,
    destination_relative: str,
    profile: HandBrakeProfile,
    attempt_number: int = 1,
) -> HandBrakeJob:
    return HandBrakeJob(
        media_id=media_id,
        source=source_root / source_name,
        destination=destination_root / destination_relative,
        profile=profile,
        attempt_number=attempt_number,
    )


def _execute(workspace, **changes):
    manifest_path, source_root, output_root, run_dir, handbrake, ffprobe = workspace
    values = {
        "confirm_transcode": True,
        "max_workers": 2,
        "job_runner": _successful_runner(),
    }
    values.update(changes)
    return execute_handbrake_batch(
        handbrake,
        ffprobe,
        load_handbrake_batch_manifest(manifest_path),
        source_root,
        output_root,
        run_dir,
        **values,
    )


def test_refuses_without_confirmation_before_creating_staging(tmp_path):
    workspace = _workspace(tmp_path)
    runner = Mock()

    with pytest.raises(HandBrakeBatchExecutionError, match="confirmation"):
        _execute(
            workspace,
            confirm_transcode=False,
            job_runner=runner,
        )

    assert not (workspace[2] / "encoded-staging").exists()
    runner.assert_not_called()


def test_executes_two_at_a_time_and_writes_only_path_free_events(tmp_path):
    workspace = _workspace(tmp_path, job_count=4)
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def runner(_handbrake, _ffprobe, job, run_dir, **_kwargs):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)
        result = _successful_runner()(
            _handbrake,
            _ffprobe,
            job,
            run_dir,
        )
        with lock:
            active -= 1
        return result

    result = _execute(workspace, job_runner=runner)

    assert result.status == "completed"
    assert len(result.completed_ids) == 4
    assert maximum_active == 2
    destination_root = workspace[2] / "encoded-staging"
    assert {
        path.relative_to(destination_root).as_posix()
        for path in destination_root.rglob("*")
        if path.is_dir()
    } == {"Test Series", "Test Series/Season 01"}
    event_text = result.event_log.read_text(encoding="utf-8")
    assert str(workspace[1]) not in event_text
    assert str(workspace[2]) not in event_text
    assert "source-0.mkv" not in event_text


def test_failed_job_is_isolated_and_unaffected_jobs_continue(tmp_path):
    workspace = _workspace(tmp_path)
    calls: list[str] = []
    success = _successful_runner(calls)

    def runner(handbrake, ffprobe, job, run_dir, **kwargs):
        if job.media_id == "media-1":
            calls.append(job.media_id)
            raise HandBrakeError("synthetic failure")
        return success(handbrake, ffprobe, job, run_dir, **kwargs)

    result = _execute(workspace, job_runner=runner)

    assert result.status == "completed-with-failures"
    assert result.failed_ids == ("media-1",)
    assert set(result.completed_ids) == {"media-0", "media-2"}
    assert set(calls) == {"media-0", "media-1", "media-2"}
    event_text = result.event_log.read_text(encoding="utf-8")
    assert "synthetic failure" not in event_text
    assert '"error_type": "HandBrakeError"' in event_text


def test_systemic_process_interruption_stops_before_dispatching_next_chunk(tmp_path):
    workspace = _workspace(tmp_path, job_count=4)
    calls: list[str] = []

    def runner(_handbrake, _ffprobe, job, _run_dir, **_kwargs):
        calls.append(job.media_id)
        raise HandBrakeProcessError(0x40010004)

    result = _execute(workspace, job_runner=runner)

    assert result.status == "interrupted"
    assert set(result.failed_ids) == {"media-0", "media-1"}
    assert result.pending_ids == ("media-2", "media-3")
    assert set(calls) == {"media-0", "media-1"}
    events = result.event_log.read_text(encoding="utf-8")
    assert '"event": "batch-interruption-detected"' in events
    assert "1073807364" not in events


def test_recover_with_one_truncated_final_event_and_continue(tmp_path):
    workspace = _workspace(tmp_path)
    manifest = load_handbrake_batch_manifest(workspace[0])
    partial_destination = (
        workspace[2]
        / "encoded-staging"
        / "Test Series"
        / "Season 01"
        / "Test Series - S01E01.mkv"
    )
    partial_output = partial_destination.with_name(
        f"{partial_destination.stem}.media-0.partial.mkv"
    )
    partial_output.parent.mkdir(parents=True, exist_ok=True)
    partial_output.write_text("preserved", encoding="utf-8")
    attempts: dict[str, int] = {}

    def retrying_runner(_handbrake, _ffprobe, job, _run_dir, **_kwargs):
        attempts[job.media_id] = job.attempt_number
        return _successful_runner()(
            _handbrake,
            _ffprobe,
            job,
            _run_dir,
        )

    event_log = workspace[3] / "handbrake-batch-events.jsonl"
    event_log.write_text(
        "\n".join([
            json.dumps({
                "at": "2026-07-31T00:00:00+00:00",
                "event": "batch-started",
                "manifest_sha256": manifest.sha256,
                "job_count": manifest.manifest.job_count,
                "max_workers": 1,
                "max_jobs": 2,
            }),
            json.dumps({
                "at": "2026-07-31T00:00:01+00:00",
                "event": "job-dispatched",
                "media_id": "media-0",
                "attempt_number": 1,
            }),
            '{"at":"2026-07-31T00:00:02+00:00","event":"job-dispatched","media_id":"media-0"',
        ]),
        encoding="utf-8",
    )

    result = _execute(workspace, max_workers=2, job_runner=retrying_runner)

    assert result.status == "completed"
    assert attempts == {"media-0": 2, "media-1": 1, "media-2": 1}
    assert partial_output.exists()


def test_event_log_rejects_non_final_corruption(tmp_path):
    workspace = _workspace(tmp_path)
    manifest = load_handbrake_batch_manifest(workspace[0])
    event_log = workspace[3] / "handbrake-batch-events.jsonl"
    event_log.write_text(
        "\n".join([
            json.dumps({
                "at": "2026-07-31T00:00:00+00:00",
                "event": "batch-started",
                "manifest_sha256": manifest.sha256,
                "job_count": manifest.manifest.job_count,
                "max_workers": 1,
                "max_jobs": 2,
            }),
            '{"at":"2026-07-31T00:00:01+00:00","event":"job-dispatched","media_id":"media-0"',
            json.dumps({
                "at": "2026-07-31T00:00:02+00:00",
                "event": "job-dispatched",
                "media_id": "media-1",
                "attempt_number": 1,
            }),
        ]),
        encoding="utf-8",
    )

    with pytest.raises(
        HandBrakeBatchExecutionError, match="Batch event log is invalid"
    ):
        _execute(workspace, max_workers=2)


def test_legacy_event_log_without_attempt_numbers_defaults_to_first_attempt(tmp_path):
    workspace = _workspace(tmp_path, job_count=1)
    manifest = load_handbrake_batch_manifest(workspace[0])
    attempts: list[int] = []
    profile = HandBrakeProfile(**manifest.manifest.profile)
    job = manifest.manifest.jobs[0]
    legacy_job = _build_handbrake_job(
        job.media_id,
        workspace[1],
        workspace[2],
        job.source_name,
        job.destination_relative,
        profile,
    )
    partial = partial_output_path(legacy_job)
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text("legacy-attempt-1", encoding="utf-8")

    event_log = workspace[3] / "handbrake-batch-events.jsonl"
    event_log.write_text(
        "\n".join([
            json.dumps({
                "at": "2026-07-31T00:00:00+00:00",
                "event": "batch-started",
                "manifest_sha256": manifest.sha256,
                "job_count": 1,
                "max_workers": 1,
                "max_jobs": 1,
            }),
            json.dumps({
                "at": "2026-07-31T00:00:01+00:00",
                "event": "job-dispatched",
                "media_id": "media-0",
            }),
        ]),
        encoding="utf-8",
    )

    def capturing_runner(_handbrake, _ffprobe, item, run_dir, **_kwargs):
        attempts.append(item.attempt_number)
        return _successful_runner()(
            _handbrake,
            _ffprobe,
            item,
            run_dir,
            **_kwargs,
        )

    result = _execute(workspace, max_workers=1, job_runner=capturing_runner)

    assert result.status == "completed"
    assert attempts == [2]


def test_retry_attempt_derived_from_recorded_max_attempt_number_and_collisions(
    tmp_path,
):
    workspace = _workspace(tmp_path, job_count=1)
    manifest = load_handbrake_batch_manifest(workspace[0])
    attempts: list[int] = []

    profile = HandBrakeProfile(**manifest.manifest.profile)
    job = manifest.manifest.jobs[0]
    base_job = _build_handbrake_job(
        job.media_id,
        workspace[1],
        workspace[2],
        job.source_name,
        job.destination_relative,
        profile,
    )

    for retry in (2, 3):
        partial = partial_output_path(replace(base_job, attempt_number=retry))
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(
            f"attempt-{retry}".encode(),
        )
    event_log = workspace[3] / "handbrake-batch-events.jsonl"
    event_log.write_text(
        "\n".join([
            json.dumps({
                "at": "2026-07-31T00:00:00+00:00",
                "event": "batch-started",
                "manifest_sha256": manifest.sha256,
                "job_count": 1,
                "max_workers": 1,
                "max_jobs": 1,
            }),
            json.dumps({
                "at": "2026-07-31T00:00:01+00:00",
                "event": "job-dispatched",
                "media_id": "media-0",
                "attempt_number": 2,
            }),
        ]),
        encoding="utf-8",
    )

    def capturing_runner(_handbrake, _ffprobe, item, _run_dir, **_kwargs):
        attempts.append(item.attempt_number)
        return _successful_runner()(
            _handbrake,
            _ffprobe,
            item,
            _run_dir,
            **_kwargs,
        )

    result = _execute(workspace, max_workers=1, job_runner=capturing_runner)

    assert result.status == "completed"
    assert attempts == [4]
    assert partial_output_path(replace(base_job, attempt_number=2)).exists()
    assert partial_output_path(replace(base_job, attempt_number=3)).exists()


def test_existing_partials_are_preserved_for_retry_attempts(tmp_path):
    workspace = _workspace(tmp_path, job_count=3)
    manifest = load_handbrake_batch_manifest(workspace[0])
    profile = HandBrakeProfile(**manifest.manifest.profile)
    attempts: dict[str, int] = {}

    for job in manifest.manifest.jobs:
        partial = partial_output_path(
            _build_handbrake_job(
                job.media_id,
                workspace[1],
                workspace[2],
                job.source_name,
                job.destination_relative,
                profile,
            )
        )
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(f"retry-{job.media_id}".encode())

    def capturing_runner(_handbrake, _ffprobe, item, _run_dir, **_kwargs):
        attempts[item.media_id] = item.attempt_number
        return _successful_runner()(
            _handbrake,
            _ffprobe,
            item,
            _run_dir,
            **_kwargs,
        )

    result = _execute(workspace, max_workers=2, job_runner=capturing_runner)

    assert result.status == "completed"
    assert set(attempts.values()) == {2}
    assert all(v == 2 for v in attempts.values())
    for job in manifest.manifest.jobs:
        assert partial_output_path(
            _build_handbrake_job(
                job.media_id,
                workspace[1],
                workspace[2],
                job.source_name,
                job.destination_relative,
                profile,
            )
        ).exists()


def test_repair_event_log_keeps_truncated_fragment_and_appends_new_events(tmp_path):
    workspace = _workspace(tmp_path, job_count=1)
    manifest = load_handbrake_batch_manifest(workspace[0])
    event_log = workspace[3] / "handbrake-batch-events.jsonl"
    broken = '{"at":"2026-07-31T00:00:01+00:00","event":"job-dispatched","media_id":"media-0"'
    event_log.write_text(
        "\n".join([
            json.dumps({
                "at": "2026-07-31T00:00:00+00:00",
                "event": "batch-started",
                "manifest_sha256": manifest.sha256,
                "job_count": 1,
                "max_workers": 1,
                "max_jobs": 1,
            }),
            broken,
        ]),
        encoding="utf-8",
    )

    result = _execute(workspace, max_workers=1, job_runner=_successful_runner())

    assert result.status == "completed"
    event_text = event_log.read_text(encoding="utf-8")
    assert broken in event_text
    assert f"{broken}\n{{" in event_text
    assert '"event": "batch-resumed"' in event_text


def test_interrupted_job_retries_with_new_partial_and_preserves_old_one(tmp_path):
    workspace = _workspace(tmp_path, job_count=1)
    original_partial: Path | None = None

    def interrupted_runner(_handbrake, _ffprobe, job, _run_dir, **_kwargs):
        nonlocal original_partial
        original_partial = partial_output_path(job)
        original_partial.write_bytes(b"preserve-interrupted-output")
        raise HandBrakeProcessError(0x40010004)

    first = _execute(workspace, max_workers=1, job_runner=interrupted_runner)

    assert first.status == "interrupted"
    assert original_partial is not None
    assert original_partial.read_bytes() == b"preserve-interrupted-output"
    attempts: list[int] = []

    def retry_runner(handbrake, ffprobe, job, run_dir, **kwargs):
        attempts.append(job.attempt_number)
        assert partial_output_path(job) != original_partial
        return _successful_runner()(handbrake, ffprobe, job, run_dir, **kwargs)

    second = _execute(workspace, max_workers=1, job_runner=retry_runner)

    assert second.status == "completed"
    assert attempts == [2]
    assert original_partial.read_bytes() == b"preserve-interrupted-output"

    third = _execute(workspace, max_workers=1, job_runner=Mock())
    assert third.status == "completed"
    assert third.resumed_ids == ("media-0",)


def test_unknown_existing_output_is_blocked_without_stopping_other_jobs(tmp_path):
    workspace = _workspace(tmp_path)
    blocked_destination = (
        workspace[2]
        / "encoded-staging"
        / "Test Series"
        / "Season 01"
        / "Test Series - S01E01.mkv"
    )
    blocked_destination.parent.mkdir(parents=True)
    blocked_destination.write_bytes(b"keep-me")
    calls: list[str] = []

    result = _execute(
        workspace,
        job_runner=_successful_runner(calls),
    )

    assert result.status == "completed-with-failures"
    assert result.blocked_ids == ("media-0",)
    assert set(result.completed_ids) == {"media-1", "media-2"}
    assert "media-0" not in calls
    assert blocked_destination.read_bytes() == b"keep-me"


@pytest.mark.parametrize(
    ("marker_name", "expected_status"),
    [("STOP", "stopped"), ("PAUSE", "paused")],
)
def test_existing_marker_stops_before_staging_or_runner(
    tmp_path,
    marker_name,
    expected_status,
):
    workspace = _workspace(tmp_path)
    (workspace[3] / marker_name).touch()
    runner = Mock()

    result = _execute(workspace, job_runner=runner)

    assert result.status == expected_status
    assert len(result.pending_ids) == 3
    assert not (workspace[2] / "encoded-staging").exists()
    runner.assert_not_called()


def test_pause_then_resume_skips_verified_completed_output(tmp_path):
    workspace = _workspace(tmp_path)
    first_calls: list[str] = []
    success = _successful_runner(first_calls)

    def pausing_runner(handbrake, ffprobe, job, run_dir, **kwargs):
        result = success(handbrake, ffprobe, job, run_dir, **kwargs)
        (run_dir / "PAUSE").touch()
        return result

    first = _execute(
        workspace,
        max_workers=1,
        job_runner=pausing_runner,
    )

    assert first.status == "paused"
    assert first.completed_ids == ("media-0",)
    assert first.pending_ids == ("media-1", "media-2")
    (workspace[3] / "PAUSE").unlink()
    resumed_calls: list[str] = []

    second = _execute(
        workspace,
        max_workers=1,
        job_runner=_successful_runner(resumed_calls),
    )

    assert second.status == "completed"
    assert second.resumed_ids == ("media-0",)
    assert set(second.completed_ids) == {"media-0", "media-1", "media-2"}
    assert resumed_calls == ["media-1", "media-2"]


def test_total_job_limit_returns_limited_and_resumes_remaining_jobs(tmp_path):
    workspace = _workspace(tmp_path)
    first_calls: list[str] = []

    first = _execute(
        workspace,
        max_jobs=2,
        job_runner=_successful_runner(first_calls),
    )

    assert first.status == "limited"
    assert set(first.completed_ids) == {"media-0", "media-1"}
    assert first.pending_ids == ("media-2",)
    second_calls: list[str] = []

    second = _execute(
        workspace,
        max_jobs=1,
        job_runner=_successful_runner(second_calls),
    )

    assert second.status == "completed"
    assert set(second.resumed_ids) == {"media-0", "media-1"}
    assert second_calls == ["media-2"]


def test_job_limit_creates_only_directories_needed_by_attempted_jobs(tmp_path):
    workspace = _workspace(tmp_path)
    payload = json.loads(workspace[0].read_text(encoding="utf-8"))
    payload["jobs"][2]["destination_relative"] = (
        "encoded-staging/Test Series/Season 02/Test Series - S02E01.mkv"
    )
    payload["missing_directories"].append("encoded-staging/Test Series/Season 02")
    workspace[0].write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    result = _execute(workspace, max_workers=1, max_jobs=1)

    assert result.status == "limited"
    assert (workspace[2] / "encoded-staging/Test Series/Season 01").is_dir()
    assert not (workspace[2] / "encoded-staging/Test Series/Season 02").exists()


def test_resume_rejects_modified_manifest_digest(tmp_path):
    workspace = _workspace(tmp_path)

    def pausing_runner(handbrake, ffprobe, job, run_dir, **kwargs):
        result = _successful_runner()(handbrake, ffprobe, job, run_dir, **kwargs)
        (run_dir / "PAUSE").touch()
        return result

    first = _execute(workspace, max_workers=1, job_runner=pausing_runner)
    assert first.status == "paused"
    (workspace[3] / "PAUSE").unlink()
    payload = json.loads(workspace[0].read_text(encoding="utf-8"))
    payload["profile"]["quality"] = 25
    workspace[0].write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(HandBrakeBatchExecutionError, match="different manifest"):
        _execute(workspace)


def test_manifest_rejects_nlmeans_for_animation_and_unsafe_paths(tmp_path):
    workspace = _workspace(tmp_path)
    payload = json.loads(workspace[0].read_text(encoding="utf-8"))
    payload["profile"]["content_kind"] = "animation"
    workspace[0].write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HandBrakeBatchExecutionError, match="profile"):
        load_handbrake_batch_manifest(workspace[0])

    payload["profile"]["content_kind"] = "live_action"
    payload["jobs"][0]["destination_relative"] = "../escape.mkv"
    workspace[0].write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HandBrakeBatchExecutionError, match="destination"):
        load_handbrake_batch_manifest(workspace[0])


def test_cli_refuses_without_confirmation_and_does_not_create_run_dir(tmp_path):
    workspace = _workspace(tmp_path)
    requested_run_dir = tmp_path / "not-created"

    result = CliRunner().invoke(
        app,
        [
            "execute-handbrake-batch",
            str(workspace[0]),
            "--source-root",
            str(workspace[1]),
            "--output-root",
            str(workspace[2]),
            "--run-dir",
            str(requested_run_dir),
        ],
    )

    assert result.exit_code == 2
    assert "Batch execution refused" in result.output
    assert not requested_run_dir.exists()
    assert not (workspace[2] / "encoded-staging").exists()


def test_cli_rejects_run_dir_inside_output_root_before_creating_it(tmp_path):
    workspace = _workspace(tmp_path)
    unsafe_run_dir = workspace[2] / "unsafe-events"

    result = CliRunner().invoke(
        app,
        [
            "execute-handbrake-batch",
            str(workspace[0]),
            "--source-root",
            str(workspace[1]),
            "--output-root",
            str(workspace[2]),
            "--run-dir",
            str(unsafe_run_dir),
            "--confirm-transcode",
        ],
    )

    assert result.exit_code == 1
    assert "outside media" in result.output
    assert "roots" in result.output
    assert not unsafe_run_dir.exists()


def test_confirmed_cli_creates_only_run_dir_and_calls_executor(
    tmp_path,
    monkeypatch,
):
    workspace = _workspace(tmp_path)
    requested_run_dir = tmp_path / "new-events"
    execute = Mock(
        return_value=BatchExecutionResult(
            status="completed",
            manifest_sha256="a" * 64,
            job_count=3,
            completed_ids=("media-0", "media-1", "media-2"),
            resumed_ids=(),
            failed_ids=(),
            blocked_ids=(),
            pending_ids=(),
            event_log=requested_run_dir / "handbrake-batch-events.jsonl",
        )
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.media.handbrake_batch_executor.execute_handbrake_batch",
        execute,
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.media.handbrake_batch_executor."
        "load_handbrake_batch_manifest",
        Mock(return_value=Mock()),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.media.handbrake.resolve_handbrake_path",
        Mock(return_value=workspace[4]),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.media.ffprobe_runner.resolve_ffprobe_path",
        Mock(return_value=workspace[5]),
    )

    result = CliRunner().invoke(
        app,
        [
            "execute-handbrake-batch",
            str(workspace[0]),
            "--source-root",
            str(workspace[1]),
            "--output-root",
            str(workspace[2]),
            "--run-dir",
            str(requested_run_dir),
            "--confirm-transcode",
        ],
    )

    assert result.exit_code == 0
    assert "Status: completed" in result.output
    assert requested_run_dir.is_dir()
    assert not (workspace[2] / "encoded-staging").exists()
    execute.assert_called_once()
