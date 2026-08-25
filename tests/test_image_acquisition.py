from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from mkv_episode_matcher.disc.image_acquisition import (
    build_backup_command,
    build_dvd_image_command,
    execute_disc_image,
    local_info_command,
    plan_disc_image,
    plan_sha256,
    run_contained_image_command,
)
from mkv_episode_matcher.disc.ripper import RipError


def test_plans_are_path_free_for_bluray_and_dvd():
    plan = plan_disc_image(drive_index=2, media_kind="bluray", estimated_bytes=100)
    assert plan.relative_destination.startswith("disc-images/")
    assert plan.physical_process_count == 1
    assert plan.execution_authorized is False
    dvd = plan_disc_image(drive_index=2, media_kind="dvd", estimated_bytes=100)
    assert dvd.image_format == "dvd-iso"
    assert dvd.relative_destination.endswith(".iso")


def test_backup_command_is_one_explicit_drive_with_noscan(tmp_path: Path):
    plan = plan_disc_image(drive_index=2, media_kind="bluray", estimated_bytes=100)
    command = build_backup_command(Path("makemkvcon64.exe"), plan, tmp_path)
    assert "--noscan" in command
    assert command[-3:] == ("backup", "disc:2", str(tmp_path))
    assert "all" not in command


def test_dvd_command_is_one_explicit_drive_and_iso(tmp_path: Path):
    destination = tmp_path / "disc.iso"
    command = build_dvd_image_command(
        Path("DiscImageCreator.exe"),
        drive_letter="e:",
        destination=destination,
    )
    assert command[1:] == ("dvd", "E", str(destination), "0")
    assert "all" not in command


def test_execute_runs_once_and_hands_off_only_local_source(tmp_path: Path):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"exe")
    image_root = tmp_path / "images"
    image_root.mkdir()
    plan = plan_disc_image(drive_index=1, media_kind="bluray", estimated_bytes=100)
    calls = []

    def runner(command, *, timeout_seconds):
        calls.append((command, timeout_seconds))
        destination = Path(command[-1])
        (destination / "BDMV").mkdir(parents=True)
        (destination / "BDMV" / "index.bdmv").write_bytes(b"index")
        return CompletedProcess(command, 0, "", "")

    source = execute_disc_image(
        plan,
        executable=executable,
        image_root=image_root,
        authorized_plan_sha256=plan_sha256(plan),
        authorized_acquisition_count=1,
        confirm_acquisition=True,
        command_runner=runner,
        disk_usage=lambda _path: SimpleNamespace(free=10 * 1024**3),
    )
    assert len(calls) == 1
    assert source.source_specifier.startswith("file:")
    info = local_info_command(executable, source)
    assert "--noscan" in info
    assert info[-1].startswith("file:")
    assert not any(part.startswith("disc:") for part in info)


def test_failed_acquisition_preserves_partial_and_never_retries(tmp_path: Path):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"exe")
    image_root = tmp_path / "images"
    image_root.mkdir()
    plan = plan_disc_image(drive_index=0, media_kind="bluray", estimated_bytes=100)
    calls = 0

    def runner(command, *, timeout_seconds):
        nonlocal calls
        calls += 1
        destination = Path(command[-1])
        destination.mkdir(parents=True)
        (destination / "partial.bin").write_bytes(b"partial")
        return CompletedProcess(command, 1, "", "")

    with pytest.raises(RipError, match="partial output was preserved"):
        execute_disc_image(
            plan,
            executable=executable,
            image_root=image_root,
            authorized_plan_sha256=plan_sha256(plan),
            authorized_acquisition_count=1,
            confirm_acquisition=True,
            command_runner=runner,
            disk_usage=lambda _path: SimpleNamespace(free=10 * 1024**3),
        )
    assert calls == 1
    assert (image_root / plan.relative_destination / "partial.bin").is_file()


def test_dvd_executes_once_and_hands_off_iso(tmp_path: Path):
    executable = tmp_path / "DiscImageCreator.exe"
    executable.write_bytes(b"exe")
    image_root = tmp_path / "images"
    image_root.mkdir()
    plan = plan_disc_image(drive_index=0, media_kind="dvd", estimated_bytes=2048)
    calls = []

    def runner(command, *, timeout_seconds):
        calls.append(command)
        Path(command[-2]).write_bytes(b"x" * 2048)
        return CompletedProcess(command, 0, "", "")

    source = execute_disc_image(
        plan,
        executable=executable,
        image_root=image_root,
        authorized_plan_sha256=plan_sha256(plan),
        authorized_acquisition_count=1,
        confirm_acquisition=True,
        drive_letter="E",
        command_runner=runner,
        disk_usage=lambda _path: SimpleNamespace(free=10 * 1024**3),
    )
    assert len(calls) == 1
    assert source.source_specifier.startswith("iso:")


def test_collision_refuses_before_runner(tmp_path: Path):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"exe")
    image_root = tmp_path / "images"
    image_root.mkdir()
    plan = plan_disc_image(drive_index=0, media_kind="bluray", estimated_bytes=100)
    (image_root / plan.relative_destination).mkdir(parents=True)

    with pytest.raises(RipError, match="collision"):
        execute_disc_image(
            plan,
            executable=executable,
            image_root=image_root,
            authorized_plan_sha256=plan_sha256(plan),
            authorized_acquisition_count=1,
            confirm_acquisition=True,
            command_runner=lambda *_args, **_kwargs: pytest.fail("must not run"),
            disk_usage=lambda _path: SimpleNamespace(free=10 * 1024**3),
        )


def test_contained_runner_closes_supervisor(monkeypatch):
    class Process:
        returncode = 0

        def communicate(self, *, timeout):
            assert timeout == 60
            return "ok", ""

    class Supervisor:
        closed = False

        def start(self, command, **kwargs):
            assert command == ("imager.exe", "dvd")
            assert kwargs["stdout"] is not None
            return Process()

        def close(self):
            self.closed = True

    supervisor = Supervisor()
    monkeypatch.setattr(
        "mkv_episode_matcher.disc.image_acquisition.get_makemkv_startup_control",
        lambda: SimpleNamespace(start=lambda: None),
    )
    result = run_contained_image_command(
        ("imager.exe", "dvd"),
        timeout_seconds=60,
        supervisor_factory=lambda: supervisor,
    )
    assert result.returncode == 0
    assert supervisor.closed is True
