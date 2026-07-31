from pathlib import Path

import pytest

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
    assert "mkv" not in command
    assert "backup" not in command


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
