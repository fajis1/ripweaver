from pathlib import Path
from threading import Event

import pytest

from mkv_episode_matcher.disc.batch_ripper import (
    BatchInventoryTitle,
    plan_single_open_batch,
)
from mkv_episode_matcher.disc.rip_orchestrator import (
    ParallelRipError,
    run_auto_rip_queue,
    run_parallel_auto_rip_queue,
)
from mkv_episode_matcher.disc.ripper import RipError, RipJob, RipResult


def _job(drive: int, title: int) -> RipJob:
    return RipJob(
        job_id=f"drive-{drive}-title-{title}",
        drive_index=drive,
        title_index=title,
        relative_output_dir=f"drive-{drive}/title-{title}",
        estimated_bytes=2_000_000,
        output_basename=f"safe-{drive}-{title}.mkv",
    )


def _result(job: RipJob) -> RipResult:
    return RipResult(
        job_id=job.job_id,
        return_code=0,
        output_count=1,
        output_bytes=2_000_000,
        warning_count=0,
        started_at="start",
        finished_at="finish",
    )


def _plan(jobs: tuple[RipJob, ...]):
    return plan_single_open_batch(
        jobs,
        tuple(
            BatchInventoryTitle(
                title_index=job.title_index,
                duration_seconds=600,
                output_name=f"feature_t{job.title_index:02d}.mkv",
            )
            for job in jobs
        ),
    )


def test_auto_queue_selects_batch_and_per_title_by_drive(tmp_path):
    batch_jobs = (_job(0, 0), _job(0, 1))
    fallback_job = _job(1, 0)
    calls = []

    def fake_batch(_executable, _root, plan, _log, **_kwargs):
        calls.append(("batch", plan.drive_index))
        return [_result(job) for job in plan.jobs]

    def fake_job(_executable, _root, job, _log, **_kwargs):
        calls.append(("job", job.drive_index))
        return _result(job)

    results = run_auto_rip_queue(
        Path("makemkvcon64.exe"),
        tmp_path,
        (*batch_jobs, fallback_job),
        tmp_path / "events.jsonl",
        batch_plans={0: _plan(batch_jobs)},
        batch_runner=fake_batch,
        job_runner=fake_job,
    )

    assert calls == [("batch", 0), ("job", 1)]
    assert [result.job_id for result in results] == [
        "drive-0-title-0",
        "drive-0-title-1",
        "drive-1-title-0",
    ]
    serialized = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert '"strategy": "single-open"' in serialized
    assert '"strategy": "per-title"' in serialized


def test_batch_failure_never_retries_titles_individually(tmp_path):
    jobs = (_job(0, 0), _job(0, 1))
    per_title_calls = []

    def failing_batch(*_args, **_kwargs):
        raise RipError("batch failed after start")

    with pytest.raises(RipError, match="batch failed"):
        run_auto_rip_queue(
            Path("makemkvcon64.exe"),
            tmp_path,
            jobs,
            tmp_path / "events.jsonl",
            batch_plans={0: _plan(jobs)},
            batch_runner=failing_batch,
            job_runner=lambda *_args, **_kwargs: per_title_calls.append(True),
        )

    assert per_title_calls == []


def test_parallel_batch_failure_does_not_cancel_other_drive(tmp_path):
    batch_jobs = (_job(0, 0), _job(0, 1))
    unaffected = _job(1, 0)
    completed = []

    def failing_batch(*_args, **_kwargs):
        raise RipError("isolated batch failure")

    def successful_job(_executable, _root, job, _log, **_kwargs):
        completed.append(job.job_id)
        return _result(job)

    with pytest.raises(ParallelRipError, match="isolated batch failure") as caught:
        run_parallel_auto_rip_queue(
            Path("makemkvcon64.exe"),
            tmp_path,
            (*batch_jobs, unaffected),
            tmp_path / "logs",
            batch_plans={0: _plan(batch_jobs)},
            batch_runner=failing_batch,
            job_runner=successful_job,
        )

    assert completed == [unaffected.job_id]
    assert [item.job_id for item in caught.value.completed_results] == [
        unaffected.job_id
    ]
    assert caught.value.drive_failures == {0: "RipError"}


def test_parallel_queue_publishes_each_title_without_waiting_for_other_drive(
    tmp_path,
):
    waiting_drive = _job(0, 0)
    fast_drive = _job(1, 0)
    fast_handoff = Event()

    def fake_job(_executable, _root, job, _log, **_kwargs):
        if job.job_id == waiting_drive.job_id:
            if not fast_handoff.wait(timeout=2):
                raise RipError("completed title was not handed off promptly")
        return _result(job)

    observed = []

    def on_result(result):
        observed.append(result.job_id)
        if result.job_id == fast_drive.job_id:
            fast_handoff.set()

    results = run_parallel_auto_rip_queue(
        Path("makemkvcon64.exe"),
        tmp_path,
        (waiting_drive, fast_drive),
        tmp_path / "streaming-logs",
        batch_plans={},
        job_runner=fake_job,
        on_result=on_result,
    )

    assert observed[0] == fast_drive.job_id
    assert {result.job_id for result in results} == {
        waiting_drive.job_id,
        fast_drive.job_id,
    }


def test_parallel_failure_retains_earlier_title_from_same_drive(tmp_path):
    first = _job(0, 0)
    second = _job(0, 1)

    def fake_job(_executable, _root, job, _log, **_kwargs):
        if job.job_id == second.job_id:
            raise RipError("second title failed")
        return _result(job)

    with pytest.raises(ParallelRipError, match="second title failed") as caught:
        run_parallel_auto_rip_queue(
            Path("makemkvcon64.exe"),
            tmp_path,
            (first, second),
            tmp_path / "partial-logs",
            batch_plans={},
            job_runner=fake_job,
        )

    assert [result.job_id for result in caught.value.completed_results] == [
        first.job_id
    ]
