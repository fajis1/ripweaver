import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mkv_episode_matcher.disc.batch_validation import (
    plan_batch_physical_validation,
    write_batch_validation_manifest,
)
from mkv_episode_matcher.disc.batch_validation_executor import (
    bind_batch_validation_manifest,
    execute_bound_batch_validation,
)
from mkv_episode_matcher.disc.ripper import RipError, RipResult
from tests.test_batch_validation import _inventory, _title


def _inputs(tmp_path: Path) -> tuple[Path, Path, str]:
    inventory = _inventory(
        tmp_path,
        [
            _title(0, "0:10:00", 10_000_000, "title_t00.mkv"),
            _title(1, "0:05:00", 5_000_000, "title_t01.mkv"),
            _title(2, "0:00:07", 1_000_000, "title_t02.mkv"),
        ],
    )
    manifest_path = tmp_path / "validation.json"
    _, digest = write_batch_validation_manifest(
        manifest_path,
        plan_batch_physical_validation(inventory),
    )
    return inventory, manifest_path, digest


def _bound(tmp_path: Path):
    inventory, manifest, digest = _inputs(tmp_path)
    return bind_batch_validation_manifest(
        manifest,
        inventory,
        expected_manifest_sha256=digest,
    )


def _executable(tmp_path: Path) -> Path:
    executable = tmp_path / "makemkvcon.exe"
    executable.write_bytes(b"synthetic")
    return executable


def _result(job_id: str) -> RipResult:
    return RipResult(
        job_id=job_id,
        return_code=0,
        output_count=1,
        output_bytes=10_000_000,
        warning_count=0,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:01:00+00:00",
    )


def test_binder_requires_exact_digest_and_fresh_identical_inventory(tmp_path):
    inventory, manifest, digest = _inputs(tmp_path)

    bound = bind_batch_validation_manifest(
        manifest,
        inventory,
        expected_manifest_sha256=digest,
    )

    assert bound.manifest_sha256 == digest
    assert bound.batch_plan.minimum_length_seconds == 8
    assert [job.title_index for job in bound.batch_plan.jobs] == [0, 1]
    assert all(
        "batch-validation/" in job.relative_output_dir for job in bound.batch_plan.jobs
    )

    with pytest.raises(RipError, match="SHA-256 does not match"):
        bind_batch_validation_manifest(
            manifest,
            inventory,
            expected_manifest_sha256="0" * 64,
        )


def test_binder_rejects_changed_fresh_inventory(tmp_path):
    inventory, manifest, digest = _inputs(tmp_path)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["titles"][0]["attributes"]["11"] = "99999999"
    inventory.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RipError, match="no longer matches"):
        bind_batch_validation_manifest(
            manifest,
            inventory,
            expected_manifest_sha256=digest,
        )


def test_executor_calls_fake_runner_after_all_checks(tmp_path):
    bound = _bound(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    run_dir = tmp_path / "run"
    observed = {}

    def fake_runner(executable, root, plan, event_log, **kwargs):
        observed["executable"] = executable
        observed["root"] = root
        observed["plan"] = plan
        observed["timeout"] = kwargs["timeout_seconds"]
        observed["stop"] = kwargs["cancel_file"]
        event_log.write("synthetic_runner_called", job_count=len(plan.jobs))
        return [_result(job.job_id) for job in plan.jobs]

    results = execute_bound_batch_validation(
        bound,
        executable=_executable(tmp_path),
        output_root=output_root,
        run_dir=run_dir,
        authorized_manifest_sha256=bound.manifest_sha256,
        authorized_title_count=2,
        confirm_validation=True,
        timeout_seconds=123,
        batch_runner=fake_runner,
    )

    assert len(results) == 2
    assert observed["timeout"] == 123
    assert observed["stop"] == run_dir / "STOP"
    assert observed["plan"].minimum_length_seconds == 8
    audit = (run_dir / "authorization.jsonl").read_text(encoding="utf-8")
    assert bound.manifest_sha256 in audit
    assert str(output_root.resolve()) not in audit
    assert "title_t00.mkv" not in audit


def test_executor_requires_the_exact_authorized_manifest_digest(tmp_path):
    bound = _bound(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    run_dir = tmp_path / "run"

    with pytest.raises(RipError, match="digest does not match"):
        execute_bound_batch_validation(
            bound,
            executable=_executable(tmp_path),
            output_root=output_root,
            run_dir=run_dir,
            authorized_manifest_sha256="0" * 64,
            authorized_title_count=2,
            confirm_validation=True,
            batch_runner=lambda *_args, **_kwargs: [],
        )

    assert not run_dir.exists()


@pytest.mark.parametrize(
    ("authorized_count", "confirmed", "timeout", "message"),
    [
        (1, True, 10, "title count"),
        (2, False, 10, "confirmation"),
        (2, True, 0, "timeout"),
    ],
)
def test_executor_refuses_authority_mismatch_before_run_dir(
    tmp_path,
    authorized_count,
    confirmed,
    timeout,
    message,
):
    output_root = tmp_path / "output"
    output_root.mkdir()
    run_dir = tmp_path / "run"
    bound = _bound(tmp_path)

    with pytest.raises(RipError, match=message):
        execute_bound_batch_validation(
            bound,
            executable=_executable(tmp_path),
            output_root=output_root,
            run_dir=run_dir,
            authorized_manifest_sha256=bound.manifest_sha256,
            authorized_title_count=authorized_count,
            confirm_validation=confirmed,
            timeout_seconds=timeout,
            batch_runner=lambda *_args, **_kwargs: [],
        )

    assert not run_dir.exists()


def test_executor_refuses_collision_and_low_space_before_runner(tmp_path):
    bound = _bound(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    collision = output_root / bound.manifest.relative_staging_dir
    collision.mkdir(parents=True)
    called = False

    def fake_runner(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    with pytest.raises(RipError, match="collision"):
        execute_bound_batch_validation(
            bound,
            executable=_executable(tmp_path),
            output_root=output_root,
            run_dir=tmp_path / "run-collision",
            authorized_manifest_sha256=bound.manifest_sha256,
            authorized_title_count=2,
            confirm_validation=True,
            batch_runner=fake_runner,
        )
    assert called is False

    collision.rmdir()
    with pytest.raises(RipError, match="free space"):
        execute_bound_batch_validation(
            bound,
            executable=_executable(tmp_path),
            output_root=output_root,
            run_dir=tmp_path / "run-space",
            authorized_manifest_sha256=bound.manifest_sha256,
            authorized_title_count=2,
            confirm_validation=True,
            batch_runner=fake_runner,
            disk_usage=lambda _path: SimpleNamespace(free=1),
        )
    assert called is False


def test_runner_failure_is_logged_by_type_without_message_or_paths(tmp_path):
    bound = _bound(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    run_dir = tmp_path / "run"
    sensitive_text = str(output_root.resolve())

    def failing_runner(*_args, **_kwargs):
        raise RuntimeError(f"synthetic private path {sensitive_text}")

    with pytest.raises(RipError, match="RuntimeError"):
        execute_bound_batch_validation(
            bound,
            executable=_executable(tmp_path),
            output_root=output_root,
            run_dir=run_dir,
            authorized_manifest_sha256=bound.manifest_sha256,
            authorized_title_count=2,
            confirm_validation=True,
            batch_runner=failing_runner,
        )

    events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert '"error_type": "RuntimeError"' in events
    assert sensitive_text not in events
    assert "synthetic private path" not in events
