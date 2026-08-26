import io
import json
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from mkv_episode_matcher.disc.ripper import (
    JsonlRipLog,
    RipError,
    RipJob,
    build_rip_command,
    classify_output,
    progress_fraction,
    resolve_final_output,
    resolve_job_output,
    run_parallel_rip_queue,
    run_rip_job,
    sample_output_throughput,
    sanitize_output,
)


class FakeProcess:
    def __init__(
        self,
        lines: str,
        *,
        return_code: int = 0,
        running: bool = False,
    ):
        self.stdout = io.StringIO(lines)
        self.return_code = return_code
        self.running = running
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self.running else self.return_code

    def wait(self, timeout=None):
        self.running = False
        return self.return_code

    def terminate(self):
        self.terminated = True
        self.running = False

    def kill(self):
        self.killed = True
        self.running = False


class SlowStream(io.StringIO):
    def readline(self, *args, **kwargs):
        time.sleep(0.2)
        return ""


def _job() -> RipJob:
    return RipJob(
        job_id="disc-01-title-003",
        drive_index=1,
        title_index=3,
        relative_output_dir="disc-01/title-003",
        estimated_bytes=2_000_000,
        output_basename="disc-01-fingerprint-title-003.mkv",
    )


def _final_job() -> RipJob:
    return RipJob(
        job_id="disc-01-title-003",
        drive_index=1,
        title_index=3,
        relative_output_dir=".staging/disc-01/fingerprint/title-003",
        estimated_bytes=2_000_000,
        output_basename="disc-01-fingerprint-title-003.mkv",
        final_relative_dir="TV Shows/Test Show/Season 01",
    )


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    executable = tmp_path / "makemkvcon64.exe"
    executable.touch()
    output_root = tmp_path / "output"
    output_root.mkdir()
    log_path = tmp_path / "logs" / "run.jsonl"
    return executable, output_root, log_path


def test_build_rip_command_allows_one_title_only(tmp_path):
    executable, output_root, _ = _paths(tmp_path)
    job = _job()
    destination = resolve_job_output(output_root, job)

    command = build_rip_command(executable, job, destination)

    assert command == (
        str(executable),
        "-r",
        "--noscan",
        "--messages=-stdout",
        "--progress=-same",
        "--minlength=0",
        "mkv",
        "disc:1",
        "3",
        str(destination),
    )
    assert "backup" not in command
    assert "all" not in command


@pytest.mark.parametrize(
    "relative",
    ("../escape", "disc-01/../../escape"),
)
def test_output_path_cannot_escape_root(tmp_path, relative):
    _, output_root, _ = _paths(tmp_path)
    job = RipJob("safe-id", 0, 0, relative)

    with pytest.raises(RipError, match="safe relative path"):
        resolve_job_output(output_root, job)


def test_final_output_path_cannot_escape_root(tmp_path):
    _, output_root, _ = _paths(tmp_path)
    job = RipJob(
        "safe-id",
        0,
        0,
        ".staging/safe-id",
        output_basename="safe.mkv",
        final_relative_dir="../escape",
    )

    with pytest.raises(RipError, match="safe relative path"):
        resolve_final_output(output_root, job)


def test_existing_makemkv_basename_may_contain_spaces(tmp_path):
    _, output_root, _ = _paths(tmp_path)
    job = RipJob(
        "disc-01-title-003",
        1,
        3,
        ".staging/disc-01/attempt/title-003",
        output_basename="Synthetic Show Disc 8_t02.mkv",
    )

    assert resolve_job_output(output_root, job).is_relative_to(output_root)


def test_output_sanitization_removes_hardware_and_destination(tmp_path):
    destination = tmp_path / "private" / "output"

    assert (
        sanitize_output(
            'DRV:0,2,999,1,"Drive Serial","Disc","I:"\n',
            destination,
        )
        == "DRV:0,<hardware-redacted>"
    )
    assert (
        sanitize_output(f'Saving files to "{destination}"\n', destination)
        == 'Saving files to "<output>"'
    )


def test_output_sanitization_removes_hardware_embedded_in_error(tmp_path):
    destination = tmp_path / "private" / "output"
    line = (
        "MSG:2003,0,3,\"Error while reading 'BD-ROM Example SERIAL'\","
        '"BD-ROM Example SERIAL"'
    )

    sanitized = sanitize_output(line, destination)

    assert "SERIAL" not in sanitized
    assert sanitized.count("<hardware-redacted>") == 2


def test_progress_is_bounded():
    assert progress_fraction("PRGV:32768,0,65536") == 0.5
    assert progress_fraction("PRGV:99999,0,65536") == 1.0
    assert progress_fraction("MSG:1005,0,0") is None


def test_output_growth_emits_actual_path_free_throughput(tmp_path, monkeypatch):
    output = tmp_path / "title.mkv"
    output.write_bytes(b"x")
    with output.open("r+b") as stream:
        stream.truncate(3 * 1024 * 1024)
    events = []
    monkeypatch.setattr("mkv_episode_matcher.disc.ripper.time.monotonic", lambda: 4.0)

    sample = sample_output_throughput(
        tmp_path,
        (1.0, 1024 * 1024),
        "disc-01-title-000",
        lambda kind, message: events.append((kind, message)),
    )

    assert sample == (4.0, 3 * 1024 * 1024)
    assert events == [("throughput", "disc-01-title-000: 0.67 MiB/s")]


def test_success_streams_sanitized_log_and_verifies_output(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    job = _job()
    destination = resolve_job_output(output_root, job)

    def fake_popen(command, **kwargs):
        assert kwargs.get("shell") is None
        (destination / "private-source-name.mkv").write_bytes(b"x" * 2_000_000)
        return FakeProcess(
            'DRV:1,2,999,1,"Drive Serial","Disc","F:"\n'
            f'MSG:1000,0,0,"Saving to {destination}"\n'
            "PRGV:32768,0,65536\n"
            "PRGV:65536,65536,65536\n"
        )

    events = []
    with JsonlRipLog(log_path) as event_log:
        result = run_rip_job(
            executable,
            output_root,
            job,
            event_log,
            popen_factory=fake_popen,
            on_event=lambda level, message: events.append((level, message)),
        )

    serialized = log_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in serialized.splitlines()]
    assert result.output_count == 1
    assert result.output_bytes == 2_000_000
    assert (destination / "disc-01-fingerprint-title-003.mkv").is_file()
    assert not (destination / "private-source-name.mkv").exists()
    assert "Drive Serial" not in serialized
    assert str(destination) not in serialized
    assert any(record["event"] == "job_completed" for record in records)
    assert ("progress", "disc-01-title-003: 100%") in events


def test_scsi_read_message_allows_makemkv_retry_and_success(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    job = _job()
    destination = resolve_job_output(output_root, job)
    scsi_message = (
        "MSG:2003,0,3,\"Error 'Scsi error - MEDIUM ERROR:"
        "L-EC UNCORRECTABLE ERROR' occurred while reading 'source'\"\n"
    )

    def fake_popen(_command, **_kwargs):
        (destination / "recovered.mkv").write_bytes(b"x" * 2_000_000)
        return FakeProcess(scsi_message + "PRGV:65536,65536,65536\n")

    assert classify_output(scsi_message) == "warning"
    with JsonlRipLog(log_path) as event_log:
        result = run_rip_job(
            executable,
            output_root,
            job,
            event_log,
            popen_factory=fake_popen,
        )

    records = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert result.return_code == 0
    assert result.warning_count == 1
    assert not any(record["event"] == "job_fatal_output" for record in records)
    assert any(
        record["event"] == "makemkv_output" and record["level"] == "warning"
        for record in records
    )


def test_scsi_read_message_still_honors_makemkv_nonzero_exit(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    process = FakeProcess(
        'MSG:2003,0,3,"Scsi error - MEDIUM ERROR"\n',
        return_code=1,
    )

    with JsonlRipLog(log_path) as event_log:
        with pytest.raises(RipError, match="exited with code 1"):
            run_rip_job(
                executable,
                output_root,
                _job(),
                event_log,
                popen_factory=lambda *_args, **_kwargs: process,
            )

    assert process.terminated is False


def test_success_finalizes_directly_in_flat_season_folder(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    job = _final_job()
    destination = resolve_job_output(output_root, job)
    final_output = resolve_final_output(output_root, job)
    assert final_output is not None

    def fake_popen(_command, **_kwargs):
        (destination / "A1_t00.mkv").write_bytes(b"x" * 2_000_000)
        return FakeProcess("PRGV:65536,65536,65536\n")

    with JsonlRipLog(log_path) as event_log:
        result = run_rip_job(
            executable,
            output_root,
            job,
            event_log,
            popen_factory=fake_popen,
        )

    assert result.output_bytes == 2_000_000
    assert final_output.is_file()
    assert list(final_output.parent.glob("*.mkv")) == [final_output]
    assert not list(destination.glob("*.mkv"))
    assert "job_output_finalized" in log_path.read_text(encoding="utf-8")


def test_final_collision_preserves_staged_output(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    job = _final_job()
    destination = resolve_job_output(output_root, job)
    final_output = resolve_final_output(output_root, job)
    assert final_output is not None
    final_output.parent.mkdir(parents=True)
    final_output.write_bytes(b"existing")

    def fake_popen(_command, **_kwargs):
        (destination / "A1_t00.mkv").write_bytes(b"x" * 2_000_000)
        return FakeProcess("PRGV:65536,65536,65536\n")

    with JsonlRipLog(log_path) as event_log:
        with pytest.raises(RipError, match="already exists"):
            run_rip_job(
                executable,
                output_root,
                job,
                event_log,
                popen_factory=fake_popen,
            )

    assert final_output.read_bytes() == b"existing"
    assert (destination / "A1_t00.mkv").is_file()


def test_existing_staging_directory_refuses_overwrite(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    job = _job()
    resolve_job_output(output_root, job).mkdir(parents=True)
    popen = Mock()

    with JsonlRipLog(log_path) as event_log:
        with pytest.raises(RipError, match="refusing overwrite"):
            run_rip_job(
                executable,
                output_root,
                job,
                event_log,
                popen_factory=popen,
            )

    popen.assert_not_called()


def test_fatal_output_terminates_and_pauses(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    process = FakeProcess(
        'MSG:5010,0,0,"Failed to open disc","Failed to open disc"\n',
        running=True,
    )

    with JsonlRipLog(log_path) as event_log:
        with pytest.raises(RipError, match="fatal error"):
            run_rip_job(
                executable,
                output_root,
                _job(),
                event_log,
                popen_factory=lambda *_args, **_kwargs: process,
            )

    assert process.terminated is True
    assert "job_fatal_output" in log_path.read_text(encoding="utf-8")


def test_timeout_terminates_and_preserves_staging_directory(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    process = FakeProcess("", running=True)
    process.stdout = SlowStream()
    job = _job()

    with JsonlRipLog(log_path) as event_log:
        with pytest.raises(RipError, match="timed out"):
            run_rip_job(
                executable,
                output_root,
                job,
                event_log,
                timeout_seconds=0.05,
                popen_factory=lambda *_args, **_kwargs: process,
            )

    assert process.terminated is True
    assert resolve_job_output(output_root, job).is_dir()
    assert "job_timeout" in log_path.read_text(encoding="utf-8")


def test_cancel_file_stops_before_media_output(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    cancel_file = tmp_path / "STOP"
    cancel_file.touch()
    process = FakeProcess("", running=True)
    process.stdout = SlowStream()

    with JsonlRipLog(log_path) as event_log:
        with pytest.raises(RipError, match="cancellation"):
            run_rip_job(
                executable,
                output_root,
                _job(),
                event_log,
                cancel_file=cancel_file,
                popen_factory=lambda *_args, **_kwargs: process,
            )

    assert process.terminated is True
    assert "job_cancel_requested" in log_path.read_text(encoding="utf-8")


def test_unexpected_progress_callback_failure_settles_process(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    process = FakeProcess("PRGV:65536,65536,65536\n", running=True)

    def fail_progress(_kind: str, _message: str) -> None:
        raise RuntimeError("synthetic progress persistence failure")

    with JsonlRipLog(log_path) as event_log:
        with pytest.raises(RuntimeError, match="progress persistence"):
            run_rip_job(
                executable,
                output_root,
                _job(),
                event_log,
                popen_factory=lambda *_args, **_kwargs: process,
                on_event=fail_progress,
            )

    assert process.terminated is True


def test_parallel_queue_runs_drives_concurrently_but_titles_in_order(
    tmp_path, monkeypatch
):
    executable, output_root, _ = _paths(tmp_path)
    jobs = [
        RipJob("drive-0-title-0", 0, 0, "d0/t0"),
        RipJob("drive-0-title-1", 0, 1, "d0/t1"),
        RipJob("drive-1-title-0", 1, 0, "d1/t0"),
        RipJob("drive-1-title-1", 1, 1, "d1/t1"),
    ]
    active = 0
    maximum_active = 0
    per_drive: dict[int, list[int]] = {0: [], 1: []}
    lock = threading.Lock()

    def fake_run(_executable, _root, job, _log, **_kwargs):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            per_drive[job.drive_index].append(job.title_index)
        time.sleep(0.05)
        with lock:
            active -= 1
        return Mock(job_id=job.job_id)

    monkeypatch.setattr(
        "mkv_episode_matcher.disc.ripper.run_rip_job",
        fake_run,
    )

    results = run_parallel_rip_queue(
        executable,
        output_root,
        jobs,
        tmp_path / "parallel-logs",
    )

    assert maximum_active == 2
    assert per_drive == {0: [0, 1], 1: [0, 1]}
    assert [result.job_id for result in results] == [job.job_id for job in jobs]
    assert (tmp_path / "parallel-logs" / "drive-00.jsonl").is_file()
    assert (tmp_path / "parallel-logs" / "drive-01.jsonl").is_file()


def test_parallel_queue_isolates_drive_failure(tmp_path, monkeypatch):
    executable, output_root, _ = _paths(tmp_path)
    jobs = [
        RipJob("drive-0-title-0", 0, 0, "d0/t0"),
        RipJob("drive-1-title-0", 1, 0, "d1/t0"),
        RipJob("drive-1-title-1", 1, 1, "d1/t1"),
    ]
    completed: list[str] = []

    def fake_run(_executable, _root, job, _log, **_kwargs):
        if job.drive_index == 0:
            raise RipError("synthetic drive failure")
        completed.append(job.job_id)
        return Mock(job_id=job.job_id)

    monkeypatch.setattr(
        "mkv_episode_matcher.disc.ripper.run_rip_job",
        fake_run,
    )

    with pytest.raises(RipError, match="synthetic drive failure"):
        run_parallel_rip_queue(
            executable,
            output_root,
            jobs,
            tmp_path / "parallel-logs",
        )

    assert completed == ["drive-1-title-0", "drive-1-title-1"]
    coordinator = (tmp_path / "parallel-logs" / "parallel-coordinator.jsonl").read_text(
        encoding="utf-8"
    )
    assert "parallel_queue_completed_with_failures" in coordinator
