from dataclasses import replace
from pathlib import Path

import pytest

from mkv_episode_matcher.disc.batch_ripper import (
    BatchInventoryTitle,
    _report_closed_batch_outputs,
    build_single_open_batch_command,
    plan_single_open_batch,
    run_single_open_batch,
    verify_single_open_batch_outputs,
)
from mkv_episode_matcher.disc.ripper import JsonlRipLog, RipError, RipJob
from tests.test_ripper import FakeProcess


def _job(index: int) -> RipJob:
    return RipJob(
        job_id=f"disc-01-title-{index:03d}",
        drive_index=1,
        title_index=index,
        relative_output_dir=f"disc-01/title-{index:03d}",
        estimated_bytes=2_000_000,
        output_basename=f"safe-title-{index:03d}.mkv",
    )


def test_reports_closed_outputs_when_next_batch_file_starts(tmp_path):
    jobs = (_job(1), _job(2), _job(3))
    plan = plan_single_open_batch(
        jobs,
        tuple(
            BatchInventoryTitle(
                job.title_index, 1200, f"disc_t{job.title_index:02d}.mkv"
            )
            for job in jobs
        ),
    )
    events: list[tuple[str, str]] = []
    reported: set[str] = set()
    (tmp_path / plan.batch_output_names[0]).write_bytes(b"first")
    (tmp_path / plan.batch_output_names[1]).write_bytes(b"second")

    _report_closed_batch_outputs(
        tmp_path, plan, reported, lambda kind, message: events.append((kind, message))
    )

    assert events == [("output-closed", f"{jobs[0].job_id}: completed")]
    assert reported == {jobs[0].job_id}


def _titles() -> tuple[BatchInventoryTitle, ...]:
    return (
        BatchInventoryTitle(0, 600, "title_t00.mkv"),
        BatchInventoryTitle(1, 300, "title_t01.mkv"),
        BatchInventoryTitle(2, 7, "title_t02.mkv"),
    )


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"fixture")
    output_root = tmp_path / "output"
    output_root.mkdir()
    log_path = tmp_path / "logs" / "events.jsonl"
    return executable, output_root, log_path


def test_plan_uses_cutoff_only_when_it_selects_exact_authorized_set():
    plan = plan_single_open_batch((_job(0), _job(1)), _titles())

    assert plan.minimum_length_seconds == 8
    assert plan.inventory_output_names == ("title_t00.mkv", "title_t01.mkv")
    assert plan.batch_output_names == ("title_t00.mkv", "title_t01.mkv")


def test_plan_sorts_titles_and_derives_selected_output_ordinals():
    titles = (
        BatchInventoryTitle(0, 600, "main_t00.mkv"),
        BatchInventoryTitle(1, 7, "menu_t01.mkv"),
        BatchInventoryTitle(2, 500, "feature_t02.mkv"),
    )

    plan = plan_single_open_batch((_job(2), _job(0)), titles)

    assert [job.title_index for job in plan.jobs] == [0, 2]
    assert plan.inventory_output_names == ("main_t00.mkv", "feature_t02.mkv")
    assert plan.batch_output_names == ("main_t00.mkv", "feature_t01.mkv")


def test_plan_refuses_subset_that_cutoff_cannot_represent():
    with pytest.raises(RipError, match="exact minimum-runtime cutoff"):
        plan_single_open_batch((_job(0), _job(2)), _titles())


def test_plan_refuses_duplicate_inventory_title_indexes():
    titles = (
        BatchInventoryTitle(0, 600, "first_t00.mkv"),
        BatchInventoryTitle(0, 300, "second_t00.mkv"),
    )

    with pytest.raises(RipError, match="duplicate titles"):
        plan_single_open_batch((_job(0), _job(1)), titles)


@pytest.mark.parametrize(
    "output_name, message",
    [
        ("title.mkv", "strict _tNN suffix"),
        ("title_t01.mkv", "does not match its title index"),
    ],
)
def test_plan_refuses_unrecognized_or_mismatched_title_suffix(
    output_name,
    message,
):
    titles = (
        BatchInventoryTitle(0, 600, output_name),
        BatchInventoryTitle(1, 300, "other_t01.mkv"),
    )

    with pytest.raises(RipError, match=message):
        plan_single_open_batch((_job(0), _job(1)), titles)


def test_build_command_uses_one_all_invocation():
    plan = plan_single_open_batch((_job(0), _job(1)), _titles())

    command = build_single_open_batch_command(
        Path("makemkvcon64.exe"), plan, Path("batch")
    )

    assert command[-4:] == ("mkv", "disc:1", "all", "batch")
    assert "--minlength=8" in command
    assert command.count("mkv") == 1


def test_batch_runs_one_process_then_distributes_verified_outputs(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    plan = plan_single_open_batch((_job(0), _job(1)), _titles())
    calls = []

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        workspace = output_root / plan.jobs[0].relative_output_dir
        for index, name in enumerate(plan.batch_output_names):
            (workspace / name).write_bytes(bytes([97 + index]) * 2_000_000)
        return FakeProcess("PRGV:65536,65536,65536\n")

    with JsonlRipLog(log_path) as event_log:
        results = run_single_open_batch(
            executable,
            output_root,
            plan,
            event_log,
            popen_factory=fake_popen,
        )

    assert len(calls) == 1
    assert calls[0][1].get("shell") is None
    assert len(results) == 2
    assert (
        output_root / plan.jobs[0].relative_output_dir / "safe-title-000.mkv"
    ).is_file()
    assert (
        output_root / plan.jobs[1].relative_output_dir / "safe-title-001.mkv"
    ).is_file()
    serialized = log_path.read_text(encoding="utf-8")
    assert '"event": "batch_completed"' in serialized
    assert serialized.count('"event": "job_completed"') == 2


def test_batch_finalizes_verified_outputs_into_flat_season_folder(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    jobs = tuple(
        replace(_job(index), final_relative_dir="TV Shows/Show/Season 01")
        for index in (0, 1)
    )
    plan = plan_single_open_batch(jobs, _titles())

    def fake_popen(_command, **_kwargs):
        workspace = output_root / plan.jobs[0].relative_output_dir
        for name in plan.batch_output_names:
            (workspace / name).write_bytes(b"x" * 2_000_000)
        return FakeProcess("PRGV:65536,65536,65536\n")

    with JsonlRipLog(log_path) as event_log:
        results = run_single_open_batch(
            executable,
            output_root,
            plan,
            event_log,
            popen_factory=fake_popen,
        )

    season = output_root / "TV Shows" / "Show" / "Season 01"
    assert len(results) == 2
    assert sorted(path.name for path in season.glob("*.mkv")) == [
        "safe-title-000.mkv",
        "safe-title-001.mkv",
    ]
    assert all(
        not list((output_root / job.relative_output_dir).glob("*.mkv")) for job in jobs
    )


def test_batch_final_collision_stops_before_makemkv(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    jobs = tuple(
        replace(_job(index), final_relative_dir="TV Shows/Show/Season 01")
        for index in (0, 1)
    )
    plan = plan_single_open_batch(jobs, _titles())
    existing = output_root / "TV Shows" / "Show" / "Season 01" / "safe-title-001.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")
    calls = []

    with JsonlRipLog(log_path) as event_log:
        with pytest.raises(RipError, match="final collision"):
            run_single_open_batch(
                executable,
                output_root,
                plan,
                event_log,
                popen_factory=lambda *_args, **_kwargs: calls.append(True),
            )

    assert calls == []
    assert existing.read_bytes() == b"existing"


def test_read_only_verifier_accepts_renumbered_outputs_without_distributing(
    tmp_path,
):
    plan = plan_single_open_batch(
        (_job(0), _job(2)),
        (
            BatchInventoryTitle(0, 600, "main_t00.mkv"),
            BatchInventoryTitle(1, 7, "menu_t01.mkv"),
            BatchInventoryTitle(2, 500, "feature_t02.mkv"),
        ),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in plan.batch_output_names:
        (workspace / name).write_bytes(b"x" * 2_000_000)

    verified = verify_single_open_batch_outputs(workspace, plan)

    assert [path.name for path, _size in verified] == [
        "main_t00.mkv",
        "feature_t01.mkv",
    ]
    assert all((workspace / name).is_file() for name in plan.batch_output_names)


def test_unexpected_batch_output_preserves_every_file(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    plan = plan_single_open_batch((_job(0), _job(1)), _titles())

    def fake_popen(_command, **_kwargs):
        workspace = output_root / plan.jobs[0].relative_output_dir
        (workspace / "title_t00.mkv").write_bytes(b"a" * 2_000_000)
        (workspace / "unexpected.mkv").write_bytes(b"b" * 2_000_000)
        return FakeProcess("")

    with JsonlRipLog(log_path) as event_log:
        with pytest.raises(RipError, match="did not exactly match"):
            run_single_open_batch(
                executable,
                output_root,
                plan,
                event_log,
                popen_factory=fake_popen,
            )

    workspace = output_root / plan.jobs[0].relative_output_dir
    assert (workspace / "title_t00.mkv").is_file()
    assert (workspace / "unexpected.mkv").is_file()


def test_collision_refuses_before_starting_process(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    plan = plan_single_open_batch((_job(0), _job(1)), _titles())
    (output_root / plan.jobs[1].relative_output_dir).mkdir(parents=True)
    called = False

    def fake_popen(*_args, **_kwargs):
        nonlocal called
        called = True
        return FakeProcess("")

    with JsonlRipLog(log_path) as event_log:
        with pytest.raises(RipError, match="collision"):
            run_single_open_batch(
                executable,
                output_root,
                plan,
                event_log,
                popen_factory=fake_popen,
            )

    assert called is False


def test_fatal_batch_output_terminates_and_preserves_directories(tmp_path):
    executable, output_root, log_path = _paths(tmp_path)
    plan = plan_single_open_batch((_job(0), _job(1)), _titles())
    process = FakeProcess(
        'MSG:5010,0,0,"Failed to open disc","Failed to open disc"\n',
        running=True,
    )

    with JsonlRipLog(log_path) as event_log:
        with pytest.raises(RipError, match="fatal batch error"):
            run_single_open_batch(
                executable,
                output_root,
                plan,
                event_log,
                popen_factory=lambda *_args, **_kwargs: process,
            )

    assert process.terminated is True
    assert all((output_root / job.relative_output_dir).is_dir() for job in plan.jobs)
