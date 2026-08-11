from dataclasses import replace
from datetime import UTC, datetime

import pytest

from mkv_episode_matcher.disc.rip_dispatcher import BoundRipDispatch
from mkv_episode_matcher.disc.rip_execution_adapter import (
    ProductionRipExecutor,
    RipExecutionOptions,
)
from mkv_episode_matcher.disc.rip_manifest import RipManifest
from mkv_episode_matcher.disc.rip_orchestrator import ParallelRipError
from mkv_episode_matcher.disc.ripper import RipError, RipJob, RipResult


def _bound(tmp_path):
    output_root = tmp_path / "output"
    output_root.mkdir()
    jobs = (
        RipJob(
            job_id="disc-01-title-000",
            drive_index=0,
            title_index=0,
            relative_output_dir="staging/one",
        ),
        RipJob(
            job_id="disc-02-title-001",
            drive_index=1,
            title_index=1,
            relative_output_dir="staging/two",
        ),
    )
    manifest = RipManifest(
        mode="approved-rip-plan",
        created_at="2026-07-30T00:00:00+00:00",
        jobs=jobs,
        skipped_discs=(),
    )
    return BoundRipDispatch(
        job_id="orchestration-job",
        manifest=manifest,
        batch_plans={},
        output_root=output_root,
    )


def _result(job_id):
    now = datetime.now(UTC).isoformat()
    return RipResult(
        job_id=job_id,
        return_code=0,
        output_count=1,
        output_bytes=1,
        warning_count=0,
        started_at=now,
        finished_at=now,
    )


def _options(tmp_path):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic executable placeholder")
    return RipExecutionOptions(
        makemkv_executable=executable,
        run_directory=tmp_path / "new-run",
        timeout_seconds=600,
        max_drives=2,
    )


def test_adapter_passes_exact_private_dispatch_to_injected_queue(tmp_path):
    bound = _bound(tmp_path)
    options = _options(tmp_path)
    calls = []

    def fake_queue(executable, output_root, jobs, run_directory, **kwargs):
        calls.append((executable, output_root, tuple(jobs), run_directory, kwargs))
        return [_result(job.job_id) for job in jobs]

    outcome = ProductionRipExecutor(options, queue_runner=fake_queue)(bound)

    assert outcome.completed_count == 2
    assert len(calls) == 1
    executable, output_root, jobs, run_directory, kwargs = calls[0]
    assert executable == options.makemkv_executable.resolve()
    assert output_root == bound.output_root
    assert jobs == bound.manifest.jobs
    assert run_directory == options.run_directory.resolve()
    assert kwargs["batch_plans"] == {}
    assert kwargs["timeout_seconds"] == 600
    assert kwargs["cancel_file"] == run_directory / "STOP"
    assert kwargs["max_drives"] == 2
    assert not run_directory.exists()


def test_adapter_rejects_run_directory_inside_media_root(tmp_path):
    bound = _bound(tmp_path)
    options = RipExecutionOptions(
        makemkv_executable=_options(tmp_path).makemkv_executable,
        run_directory=bound.output_root / "logs",
    )

    with pytest.raises(RipError, match="outside"):
        ProductionRipExecutor(
            options,
            queue_runner=lambda *_args, **_kwargs: [],
        )(bound)


def test_adapter_rejects_existing_run_directory_before_queue(tmp_path):
    bound = _bound(tmp_path)
    options = _options(tmp_path)
    options.run_directory.mkdir()
    called = False

    def fake_queue(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    with pytest.raises(RipError, match="already exists"):
        ProductionRipExecutor(options, queue_runner=fake_queue)(bound)
    assert called is False


def test_adapter_rejects_unexpected_result_set(tmp_path):
    bound = _bound(tmp_path)
    options = _options(tmp_path)

    with pytest.raises(RipError, match="unexpected result set"):
        ProductionRipExecutor(
            options,
            queue_runner=lambda *_args, **_kwargs: [
                _result("disc-01-title-000"),
                _result("wrong-job"),
            ],
        )(bound)


def test_adapter_hands_verified_results_to_completion_sink(tmp_path):
    bound = _bound(tmp_path)
    observed = []

    def fake_queue(_executable, _root, jobs, _run_dir, **_kwargs):
        return [_result(job.job_id) for job in jobs]

    ProductionRipExecutor(
        _options(tmp_path),
        queue_runner=fake_queue,
        completion_sink=lambda dispatch, results: observed.append((dispatch, results)),
    )(bound)

    assert observed[0][0] is bound
    assert {item.job_id for item in observed[0][1]} == {
        job.job_id for job in bound.manifest.jobs
    }


def test_adapter_hands_unaffected_drive_results_to_sink_before_failure(tmp_path):
    bound = _bound(tmp_path)
    observed = []
    completed = _result(bound.manifest.jobs[1].job_id)

    def partially_failing_queue(*_args, **_kwargs):
        raise ParallelRipError(
            "drive worker timed out",
            completed_results=[completed],
            drive_failures={0: "RipError"},
        )

    with pytest.raises(ParallelRipError):
        ProductionRipExecutor(
            _options(tmp_path),
            queue_runner=partially_failing_queue,
            completion_sink=lambda dispatch, results: observed.append((
                dispatch,
                results,
            )),
        )(bound)

    assert observed == [(bound, [completed])]


def test_adapter_hands_partially_completed_disc_results_to_sink(tmp_path):
    bound = _bound(tmp_path)
    first = bound.manifest.jobs[0]
    second = replace(
        bound.manifest.jobs[1],
        job_id="disc-01-title-001",
        drive_index=first.drive_index,
    )
    bound = replace(
        bound,
        manifest=replace(bound.manifest, jobs=(first, second)),
    )
    observed = []
    completed = _result(first.job_id)

    def partially_failing_queue(*_args, **_kwargs):
        raise ParallelRipError(
            "second title failed",
            completed_results=[completed],
            drive_failures={first.drive_index: "RipError"},
        )

    with pytest.raises(ParallelRipError):
        ProductionRipExecutor(
            _options(tmp_path),
            queue_runner=partially_failing_queue,
            completion_sink=lambda dispatch, results: observed.append((
                dispatch,
                results,
            )),
        )(bound)

    assert observed == [(bound, [completed])]


def test_adapter_rejects_unexpected_partial_result_set(tmp_path):
    bound = _bound(tmp_path)

    def partially_failing_queue(*_args, **_kwargs):
        raise ParallelRipError(
            "invalid partial result",
            completed_results=[_result("wrong-job")],
            drive_failures={0: "RipError"},
        )

    with pytest.raises(RipError, match="unexpected partial result set"):
        ProductionRipExecutor(
            _options(tmp_path),
            queue_runner=partially_failing_queue,
        )(bound)
