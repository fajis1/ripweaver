import json
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.media.transcript_batch import (
    CollectedFile,
    CollectedWindow,
    TranscriptBatchResult,
)


def _inputs(tmp_path):
    media = tmp_path / "episode.mkv"
    media.touch()
    probe = tmp_path / "probe.json"
    probe.write_text(
        json.dumps({
            "format": {
                "duration": 1800,
                "size": 1000,
                "format_name": "matroska",
            },
            "streams": [
                {
                    "index": 2,
                    "codec_type": "audio",
                    "codec_name": "ac3",
                    "channels": 6,
                    "channel_layout": "5.1",
                    "tags": {"language": "eng"},
                    "disposition": {"default": 1},
                }
            ],
        }),
        encoding="utf-8",
    )
    return media, probe


@patch("mkv_episode_matcher.core.providers.asr.get_asr_provider")
def test_cli_stops_before_media_or_model_without_confirmation(mock_asr, tmp_path):
    media, probe = _inputs(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "collect-transcripts",
            str(media),
            "--media-id",
            "disc-01-title-000",
            "--probe-report",
            str(probe),
        ],
    )

    assert result.exit_code == 2
    assert "stopped before media access" in result.stdout
    mock_asr.assert_not_called()


@patch("mkv_episode_matcher.media.transcript_batch.resolve_ffmpeg_path")
@patch("mkv_episode_matcher.core.providers.asr.get_asr_provider")
@patch("mkv_episode_matcher.media.transcript_batch.collect_transcript_batch")
def test_confirmed_cli_writes_private_and_safe_reports(
    mock_collect,
    mock_asr,
    mock_resolve,
    tmp_path,
):
    media, probe = _inputs(tmp_path)
    executable = tmp_path / "ffmpeg.exe"
    executable.touch()
    private = tmp_path / "private.json"
    metrics = tmp_path / "metrics.json"
    mock_resolve.return_value = executable
    mock_asr.return_value = Mock()
    mock_collect.return_value = TranscriptBatchResult(
        "saved-transcript-evidence",
        "small",
        "cpu",
        (
            CollectedFile(
                "disc-01-title-000",
                1800,
                2,
                "collected",
                (
                    CollectedWindow(
                        300,
                        "Private dragon dialogue.",
                        3,
                        -30,
                        -5,
                    ),
                ),
                (2,),
            ),
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "collect-transcripts",
            str(media),
            "--media-id",
            "disc-01-title-000",
            "--probe-report",
            str(probe),
            "--report-out",
            str(private),
            "--metrics-out",
            str(metrics),
            "--sampling-mode",
            "intro",
            "--intro-start",
            "120",
            "--preferred-audio-stream",
            "2",
            "--confirm-read",
        ],
    )

    assert result.exit_code == 0
    assert "one CPU ASR model" in result.stdout
    assert "Private dragon dialogue" in private.read_text(encoding="utf-8")
    assert "dragon" not in metrics.read_text(encoding="utf-8")
    mock_collect.assert_called_once()
    assert mock_collect.call_args.kwargs["sampling_mode"] == "intro"
    assert mock_collect.call_args.kwargs["intro_start_seconds"] == 120
    assert mock_collect.call_args.kwargs["preferred_stream_index"] == 2
