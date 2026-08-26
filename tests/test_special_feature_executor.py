from pathlib import Path

import pytest

from mkv_episode_matcher.backend.routers.rip import (
    ExecuteSpecialFeatureRequest,
    execute_special_feature_job,
)
from mkv_episode_matcher.disc.ripper import RipError, RipResult
from mkv_episode_matcher.disc.special_feature_binder import (
    bind_diagnostic_special_feature_manifest,
    file_sha256,
    write_bound_special_feature_manifest,
)
from mkv_episode_matcher.disc.special_feature_executor import (
    execute_bound_special_feature_manifest,
)
from tests.test_special_feature_binder import _write_inputs


def _bound(tmp_path):
    diagnostic_path, inventory_path = _write_inputs(tmp_path)
    return bind_diagnostic_special_feature_manifest(
        diagnostic_path,
        inventory_path,
        expected_diagnostic_sha256=file_sha256(diagnostic_path),
    )


def _executable(tmp_path):
    executable = tmp_path / "makemkvcon.exe"
    executable.write_bytes(b"fixture")
    return executable


def _result(job_id):
    return RipResult(
        job_id=job_id,
        return_code=0,
        output_count=1,
        output_bytes=10_000_000,
        warning_count=0,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:01:00+00:00",
    )


def test_executor_runs_exact_jobs_sequentially_with_audit(tmp_path):
    manifest = _bound(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    run_dir = tmp_path / "run"
    observed = {}

    def fake_queue(executable, root, jobs, log_path, **kwargs):
        observed["jobs"] = tuple(jobs)
        observed["log_path"] = log_path
        observed["cancel_file"] = kwargs["cancel_file"]
        return [_result(job.job_id) for job in jobs]

    results = execute_bound_special_feature_manifest(
        manifest,
        bound_manifest_sha256="a" * 64,
        executable=_executable(tmp_path),
        output_root=output_root,
        run_dir=run_dir,
        authorized_job_count=1,
        queue_runner=fake_queue,
    )

    assert len(results) == 1
    assert observed["jobs"][0].title_index == 0
    assert observed["log_path"] == run_dir / "events.jsonl"
    assert observed["cancel_file"] == run_dir / "STOP"
    assert (run_dir / "authorization.jsonl").is_file()


def test_executor_refuses_job_count_mismatch_before_creating_run(tmp_path):
    output_root = tmp_path / "output"
    output_root.mkdir()
    run_dir = tmp_path / "run"

    with pytest.raises(RipError, match="job count"):
        execute_bound_special_feature_manifest(
            _bound(tmp_path),
            bound_manifest_sha256="a" * 64,
            executable=_executable(tmp_path),
            output_root=output_root,
            run_dir=run_dir,
            authorized_job_count=2,
        )

    assert not run_dir.exists()


def test_executor_refuses_staging_collision_before_queue(tmp_path):
    manifest = _bound(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    collision = output_root / Path(manifest.jobs[0].relative_output_dir)
    collision.mkdir(parents=True)
    run_dir = tmp_path / "run"

    with pytest.raises(RipError, match="collision"):
        execute_bound_special_feature_manifest(
            manifest,
            bound_manifest_sha256="a" * 64,
            executable=_executable(tmp_path),
            output_root=output_root,
            run_dir=run_dir,
            authorized_job_count=1,
        )

    assert not run_dir.exists()


def test_executor_preserves_run_log_when_queue_fails(tmp_path):
    output_root = tmp_path / "output"
    output_root.mkdir()
    run_dir = tmp_path / "run"

    def failing_queue(*_args, **_kwargs):
        raise RipError("synthetic queue failure")

    with pytest.raises(RipError, match="synthetic queue failure"):
        execute_bound_special_feature_manifest(
            _bound(tmp_path),
            bound_manifest_sha256="a" * 64,
            executable=_executable(tmp_path),
            output_root=output_root,
            run_dir=run_dir,
            authorized_job_count=1,
            queue_runner=failing_queue,
        )

    assert (run_dir / "authorization.jsonl").is_file()


def test_api_boundary_rebinds_and_uses_injected_special_feature_runner(tmp_path):
    diagnostic_path, inventory_path = _write_inputs(tmp_path)
    manifest = bind_diagnostic_special_feature_manifest(
        diagnostic_path,
        inventory_path,
        expected_diagnostic_sha256=file_sha256(diagnostic_path),
    )
    bound_path = tmp_path / "bound.json"
    write_bound_special_feature_manifest(bound_path, manifest)
    output_root = tmp_path / "output"
    output_root.mkdir()
    executable = _executable(tmp_path)
    calls = []

    def fake_queue(_executable, _root, jobs, _log_path, **_kwargs):
        calls.append(tuple(jobs))
        return [_result(job.job_id) for job in jobs]

    result = execute_special_feature_job(
        ExecuteSpecialFeatureRequest(
            bound_manifest=str(bound_path),
            fresh_inventory=str(inventory_path),
            bound_sha256=file_sha256(bound_path),
            authorized_job_count=len(manifest.jobs),
            makemkv_executable=str(executable),
            output_root=str(output_root),
            run_directory=str(tmp_path / "api-run"),
            timeout_seconds=600,
            confirm_execute=True,
        ),
        fake_queue,
    )

    assert result["status"] == "completed"
    assert result["completed_count"] == len(manifest.jobs)
    assert len(calls) == 1
