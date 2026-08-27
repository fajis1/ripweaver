from pathlib import Path

import pytest
from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.disc import preflight
from mkv_episode_matcher.disc.preflight import (
    CommandResult,
    MakeMKVDrive,
    PreflightError,
    build_info_command,
    inventory_to_dict,
    parse_disc_inventory,
    parse_drives,
    safe_report_stem,
    sanitize_robot_output,
)

ROBOT_OUTPUT = """\
DRV:0,2,999,1,"BD-RE ASUS BW-16D1HT","FAERIE_TALE_THEATRE_5","D:"
DRV:1,2,999,1,"BD-RE ASUS BW-16D1HT","","F:"
CINFO:30,0,"FAERIE_TALE_THEATRE_5"
TINFO:0,2,0,"Main Feature"
TINFO:0,8,0,"12"
TINFO:0,9,0,"0:51:23"
TINFO:0,16,0,"00800.mpls"
TINFO:0,26,0,"46,47"
TINFO:0,27,0,"title_t00.mkv"
SINFO:0,0,1,0,"Video"
SINFO:0,0,6,0,"Mpeg4"
SINFO:0,1,1,0,"Audio"
SINFO:0,1,3,0,"eng"
SINFO:0,1,6,0,"DD"
SINFO:0,1,14,0,"6"
SINFO:0,1,38,0,"d"
SINFO:0,1,40,0,"5.1(side)"
MSG:5005,0,0,"Title #1 was skipped because it is too short"
"""


def test_build_info_command_allows_info_only():
    command = build_info_command(Path("makemkvcon64.exe"), "disc:3", 120)

    assert command[-2:] == ("info", "disc:3")
    assert "--minlength=120" in command
    assert "--noscan" in command
    assert "mkv" not in command
    assert "backup" not in command


def test_all_drive_discovery_uses_documented_cache_list_command():
    command = build_info_command(Path("makemkvcon64.exe"), "disc:9999")

    assert command == (
        "makemkvcon64.exe",
        "-r",
        "--cache=1",
        "info",
        "disc:9999",
    )


def test_targeted_inventory_reuses_cached_drive_when_global_row_is_omitted():
    drive = preflight.targeted_inventory_drive(
        'TINFO:0,9,0,"0:42:00"\n',
        requested_index=3,
        cached_device_name="D:",
        cached_drive_name="Reviewed drive",
        cached_disc_name="INSERTED_DISC",
    )

    assert drive is not None
    assert drive.index == 3
    assert drive.device_name == "D:"
    assert drive.disc_name == "INSERTED_DISC"


def test_targeted_inventory_refuses_cached_drive_without_title_metadata():
    assert (
        preflight.targeted_inventory_drive(
            'MSG:1005,0,1,"No disc"\n',
            requested_index=3,
            cached_device_name="D:",
            cached_drive_name="Reviewed drive",
            cached_disc_name="INSERTED_DISC",
        )
        is None
    )


@pytest.mark.parametrize("source", ["dev:D:", "disc:1; eject", "file:test", "disc:-1"])
def test_build_info_command_rejects_unsafe_sources(source):
    with pytest.raises(PreflightError):
        build_info_command(Path("makemkvcon64.exe"), source)


def test_parse_drives():
    drives = parse_drives(ROBOT_OUTPUT)

    assert len(drives) == 2
    assert drives[0].index == 0
    assert drives[0].device_name == "D:"
    assert drives[0].has_disc is True
    assert drives[1].has_disc is False


def test_parse_disc_inventory():
    drive = parse_drives(ROBOT_OUTPUT)[0]
    result = CommandResult(
        command=("makemkvcon64.exe", "info", "disc:0"),
        return_code=0,
        stdout=ROBOT_OUTPUT,
        stderr="",
        started_at="2026-07-30T12:00:00+00:00",
        finished_at="2026-07-30T12:01:00+00:00",
    )

    inventory = parse_disc_inventory(result, drive)

    assert inventory.disc_attributes[30] == "FAERIE_TALE_THEATRE_5"
    assert inventory.titles[0].duration == "0:51:23"
    assert inventory.titles[0].chapters == "12"
    assert inventory.titles[0].source_file == "00800.mpls"
    assert inventory.titles[0].segment_map == "46,47"
    assert inventory.titles[0].output_name == "title_t00.mkv"
    assert inventory.titles[0].streams[1].channels == 6
    assert inventory.titles[0].streams[1].channel_layout == "5.1(side)"
    assert inventory.titles[0].streams[1].is_default is True
    assert inventory.warnings == ["Title #1 was skipped because it is too short"]

    serialized = inventory_to_dict(inventory)
    assert serialized["drive"]["drive_name"] == "<hardware-redacted>"
    assert serialized["drive"]["device_name"] == "<device-redacted>"
    assert serialized["minimum_length_seconds"] == 0


def test_persisted_robot_output_redacts_hardware_identity():
    sanitized = sanitize_robot_output(ROBOT_OUTPUT)

    assert "BD-RE ASUS" not in sanitized
    assert '"D:"' not in sanitized
    assert "<hardware-redacted>" in sanitized
    assert "<device-redacted>" in sanitized
    assert 'CINFO:30,0,"FAERIE_TALE_THEATRE_5"' in sanitized


def test_safe_report_stem_removes_path_characters():
    drive = MakeMKVDrive(
        index=4,
        visible=2,
        enabled=999,
        flags=1,
        drive_name="Test",
        disc_name=r"..\Disc:Name/One",
        device_name="J:",
    )

    assert safe_report_stem(drive) == "drive_4_Disc_Name_One"


def test_info_runner_uses_supervised_makemkv_boundary(monkeypatch):
    calls = []

    def run(command, *, timeout_seconds):
        calls.append((command, timeout_seconds))
        return preflight.subprocess.CompletedProcess(command, 0, "output", "")

    monkeypatch.setattr(preflight, "run_makemkv_command", run)

    result = preflight.run_info_command(
        Path("makemkvcon64.exe"), "disc:2", timeout_seconds=15
    )

    assert calls == [(result.command, 15)]
    assert result.stdout == "output"


def test_info_runner_redacts_containment_start_failure(monkeypatch):
    monkeypatch.setattr(
        preflight,
        "run_makemkv_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private detail")),
    )

    with pytest.raises(PreflightError, match="OSError") as exc_info:
        preflight.run_info_command(Path("makemkvcon64.exe"), "disc:2")

    assert "private detail" not in str(exc_info.value)


def test_all_drive_timeout_preserves_completed_drive_rows(monkeypatch):
    command = build_info_command(Path("makemkvcon64.exe"), "disc:9999")
    output = 'DRV:0,2,999,1,"hardware","disc","D:"\nDRV:1,2,999,0,"hardware","","E:"\n'
    monkeypatch.setattr(
        preflight,
        "run_makemkv_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            preflight.subprocess.TimeoutExpired(
                command,
                15,
                output=output.encode("utf-8"),
                stderr=b"",
            )
        ),
    )

    result = preflight.run_info_command(
        Path("makemkvcon64.exe"),
        "disc:9999",
        timeout_seconds=15,
    )

    assert result.return_code == 124
    assert [drive.index for drive in preflight.parse_drives(result.stdout)] == [0, 1]


def test_targeted_timeout_never_accepts_partial_inventory(monkeypatch):
    command = build_info_command(Path("makemkvcon64.exe"), "disc:0")
    monkeypatch.setattr(
        preflight,
        "run_makemkv_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            preflight.subprocess.TimeoutExpired(
                command,
                15,
                output=b'DRV:0,2,999,1,"hardware","disc","D:"\n',
            )
        ),
    )

    with pytest.raises(PreflightError, match="timed out"):
        preflight.run_info_command(
            Path("makemkvcon64.exe"),
            "disc:0",
            timeout_seconds=15,
        )


def test_explicit_drive_preflight_uses_one_targeted_info_call(tmp_path, monkeypatch):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    output_dir = tmp_path / "reports"
    calls = []

    def run_info(_executable, source, **kwargs):
        calls.append((source, kwargs))
        return CommandResult(
            command=("makemkvcon64.exe", "info", source),
            return_code=0,
            stdout=ROBOT_OUTPUT,
            stderr="",
            started_at="2026-08-19T00:00:00+00:00",
            finished_at="2026-08-19T00:00:01+00:00",
        )

    monkeypatch.setattr(preflight, "run_info_command", run_info)

    result = CliRunner().invoke(
        app,
        [
            "preflight",
            "--drive",
            "0",
            "--output-dir",
            str(output_dir),
            "--makemkv-path",
            str(executable),
            "--min-length",
            "0",
            "--timeout",
            "300",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "disc:0",
            {"minimum_length": 0, "timeout_seconds": 300},
        )
    ]
    assert not (output_dir / "drive-discovery.robot.log").exists()
    assert len(list(output_dir.glob("drive_0_*.json"))) == 1
