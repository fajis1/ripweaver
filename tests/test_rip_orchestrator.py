from pathlib import Path

import pytest

from mkv_episode_matcher.disc.batch_ripper import (
    BatchInventoryTitle,
    plan_single_open_batch,
)
from mkv_episode_matcher.disc.rip_orchestrator import (
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

    with pytest.raises(RipError, match="isolated batch failure"):
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
