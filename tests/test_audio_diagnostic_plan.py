import json

import pytest
from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.media.audio_diagnostics import (
    build_audio_diagnostic_plan,
)
from mkv_episode_matcher.media.probe import (
    ProbeDataError,
    parse_ffprobe_payload,
)


def _probe_payload(duration: float = 1370.0):
    return {
        "format": {
            "filename": "private-media-name.mkv",
            "duration": str(duration),
            "size": "750000000",
            "format_name": "matroska,webm",
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "ac3",
                "channels": 6,
                "channel_layout": "5.1(side)",
                "tags": {"language": "eng", "title": "Main Audio"},
                "disposition": {"default": 1},
            },
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "ac3",
                "channels": 2,
                "channel_layout": "stereo",
                "tags": {"language": "eng", "title": "Main Stereo"},
                "disposition": {"default": 0},
            },
            {
                "index": 3,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "channel_layout": "stereo",
                "tags": {"language": "eng", "title": "Director Commentary"},
                "disposition": {"default": 0},
            },
        ],
    }


def test_saved_ffprobe_normalization_drops_filename():
    media = parse_ffprobe_payload(_probe_payload())

    assert media.duration_seconds == 1370.0
    assert media.size_bytes == 750_000_000
    assert len(media.audio_streams) == 3
    assert media.audio_streams[2].is_commentary is True
    assert not hasattr(media, "filename")


def test_audio_plan_prefers_stereo_then_5_1_then_commentary():
    plan = build_audio_diagnostic_plan(
        parse_ffprobe_payload(_probe_payload()),
        media_id="media-1",
    )

    assert [stream.stream_index for stream in plan.streams] == [2, 1, 3]
    assert plan.streams[0].role == "primary"
    assert plan.streams[0].downmix == "none"
    assert plan.streams[1].downmix == "dialogue-preserving-stereo"
    assert len(plan.sample_windows) == 3
    assert "silence_ratio" in plan.measurements


def test_5_1_only_plan_remains_usable():
    payload = _probe_payload()
    payload["streams"] = payload["streams"][:2]

    plan = build_audio_diagnostic_plan(
        parse_ffprobe_payload(payload),
        media_id="control",
    )

    assert len(plan.streams) == 1
    assert plan.streams[0].stream_index == 1
    assert plan.streams[0].role == "primary"
    assert plan.streams[0].downmix == "dialogue-preserving-stereo"


def test_invalid_probe_duration_stops_safely():
    payload = _probe_payload()
    payload["format"]["duration"] = "unknown"

    with pytest.raises(ProbeDataError, match="positive media duration"):
        parse_ffprobe_payload(payload)


def test_audio_plan_contains_no_command_or_source_filename():
    plan = build_audio_diagnostic_plan(
        parse_ffprobe_payload(_probe_payload()),
        media_id="media-1",
    )
    serialized = json.dumps(plan.to_dict()).lower()

    assert "private-media-name" not in serialized
    assert "command" not in serialized
    assert "ffmpeg" not in serialized
    assert "ffprobe" not in serialized
    assert "input_path" not in serialized


def test_plan_audio_cli_reads_saved_json_without_exposing_path(tmp_path):
    probe_path = tmp_path / "private-media-name.ffprobe.json"
    probe_path.write_text(json.dumps(_probe_payload()), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["plan-audio", str(probe_path), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "plan-only"
    assert payload["plans"][0]["media_id"] == "media-1"
    assert str(probe_path) not in result.output
