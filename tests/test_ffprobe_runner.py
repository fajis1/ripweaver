import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.media.ffprobe_runner import (
    FFprobeError,
    build_ffprobe_command,
    inspect_mkv,
    write_sanitized_probe_report,
)
from mkv_episode_matcher.media.probe import load_ffprobe_payload


def _payload(source: str) -> dict:
    return {
        "format": {
            "filename": source,
            "duration": "1320.5",
            "size": "700000000",
            "format_name": "matroska,webm",
            "tags": {"title": "Private global title"},
        },
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264"},
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "ac3",
                "channels": 6,
                "channel_layout": "5.1(side)",
                "tags": {"language": "eng", "title": "Main Audio"},
                "disposition": {"default": 1},
            },
        ],
    }


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "ffprobe.exe"
    executable.touch()
    media = tmp_path / "private episode name.mkv"
    media.touch()
    return executable, media


def test_command_is_fixed_read_only_argument_list(tmp_path):
    executable, media = _paths(tmp_path)

    command = build_ffprobe_command(executable, media)

    assert command == (
        str(executable),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-i",
        str(media.resolve()),
    )
    assert not any(token in command for token in ("ffmpeg", "-o", "delete", "mkv"))


def test_rejects_non_mkv_input_before_subprocess(tmp_path, monkeypatch):
    executable = tmp_path / "ffprobe.exe"
    executable.touch()
    media = tmp_path / "episode.mp4"
    media.touch()
    run = Mock()
    monkeypatch.setattr("mkv_episode_matcher.media.ffprobe_runner.subprocess.run", run)

    with pytest.raises(FFprobeError, match=r"explicit \.mkv"):
        inspect_mkv(executable, media)

    run.assert_not_called()


def test_inspection_captures_output_without_shell(tmp_path, monkeypatch):
    executable, media = _paths(tmp_path)
    run = Mock(
        return_value=subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout=json.dumps(_payload(str(media.resolve()))),
            stderr="",
        )
    )
    monkeypatch.setattr("mkv_episode_matcher.media.ffprobe_runner.subprocess.run", run)

    inspection = inspect_mkv(executable, media, timeout_seconds=12)

    assert inspection.return_code == 0
    assert inspection.media.duration_seconds == 1320.5
    assert inspection.media.audio_streams[0].channels == 6
    assert str(media.resolve()) not in inspection.stdout
    assert media.name not in inspection.stdout
    _, kwargs = run.call_args
    assert kwargs["timeout"] == 12
    assert kwargs["check"] is False
    assert "shell" not in kwargs


def test_timeout_stops_without_exposing_source_path(tmp_path, monkeypatch):
    executable, media = _paths(tmp_path)
    monkeypatch.setattr(
        "mkv_episode_matcher.media.ffprobe_runner.subprocess.run",
        Mock(side_effect=subprocess.TimeoutExpired(("ffprobe",), 3)),
    )

    with pytest.raises(FFprobeError) as raised:
        inspect_mkv(executable, media, timeout_seconds=3)

    assert "timed out after 3s" in str(raised.value)
    assert str(media) not in str(raised.value)
    assert media.name not in str(raised.value)


def test_failed_command_redacts_source_path(tmp_path, monkeypatch):
    executable, media = _paths(tmp_path)
    monkeypatch.setattr(
        "mkv_episode_matcher.media.ffprobe_runner.subprocess.run",
        Mock(
            return_value=subprocess.CompletedProcess(
                args=(),
                returncode=1,
                stdout="",
                stderr=f"{media.resolve()}: invalid data",
            )
        ),
    )

    with pytest.raises(FFprobeError) as raised:
        inspect_mkv(executable, media)

    message = str(raised.value)
    assert "exit code 1" in message
    assert "<media>: invalid data" in message
    assert str(media.resolve()) not in message
    assert media.name not in message


def test_malformed_json_stops_safely(tmp_path, monkeypatch):
    executable, media = _paths(tmp_path)
    monkeypatch.setattr(
        "mkv_episode_matcher.media.ffprobe_runner.subprocess.run",
        Mock(
            return_value=subprocess.CompletedProcess(
                args=(),
                returncode=0,
                stdout="{not-json",
                stderr="",
            )
        ),
    )

    with pytest.raises(FFprobeError, match="malformed JSON"):
        inspect_mkv(executable, media)


def test_saved_report_is_replayable_and_drops_source_identity(tmp_path, monkeypatch):
    executable, media = _paths(tmp_path)
    monkeypatch.setattr(
        "mkv_episode_matcher.media.ffprobe_runner.subprocess.run",
        Mock(
            return_value=subprocess.CompletedProcess(
                args=(),
                returncode=0,
                stdout=json.dumps(_payload(str(media.resolve()))),
                stderr="",
            )
        ),
    )

    inspection = inspect_mkv(executable, media)
    report = write_sanitized_probe_report(
        tmp_path / "reports",
        inspection.media,
        media_id="media-1",
    )
    serialized = report.read_text(encoding="utf-8")
    replayed = load_ffprobe_payload(report)

    assert report.name == "media-1.ffprobe.json"
    assert str(media) not in serialized
    assert media.name not in serialized
    assert "filename" not in serialized
    assert "Private global title" not in serialized
    assert replayed.duration_seconds == 1320.5
    assert replayed.audio_streams[0].title == "Main Audio"


def test_probe_mkv_cli_uses_ordinal_report_id(tmp_path, monkeypatch):
    executable, media = _paths(tmp_path)
    output_dir = tmp_path / "reports"
    monkeypatch.setattr(
        "mkv_episode_matcher.media.ffprobe_runner.subprocess.run",
        Mock(
            return_value=subprocess.CompletedProcess(
                args=(),
                returncode=0,
                stdout=json.dumps(_payload(str(media.resolve()))),
                stderr="",
            )
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "probe-mkv",
            str(media),
            "--ffprobe-path",
            str(executable),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    assert (output_dir / "media-1.ffprobe.json").is_file()
    assert media.name not in result.output


def test_probe_mkv_cli_redacts_rejected_source_path(tmp_path):
    executable = tmp_path / "ffprobe.exe"
    executable.touch()
    media = tmp_path / "private episode name.mp4"
    media.touch()

    result = CliRunner().invoke(
        app,
        [
            "probe-mkv",
            str(media),
            "--ffprobe-path",
            str(executable),
        ],
    )

    assert result.exit_code == 1
    assert "explicit .mkv files only" in " ".join(result.output.split())
    assert str(media) not in result.output
    assert media.name not in result.output
