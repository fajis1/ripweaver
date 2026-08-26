import json
import wave
from array import array

import pytest

from mkv_episode_matcher.media.transcript_diagnostic import (
    TranscriptDiagnostic,
    TranscriptDiagnosticError,
    diagnose_transcript,
    write_safe_report,
)


class FakeAsr:
    def transcribe(self, _path):
        return "We should follow the dragon before it reaches the hidden cave."


def _fake_extract(
    _media,
    _start,
    _duration,
    output,
    *,
    audio_stream_index=None,
):
    assert audio_stream_index == 2
    samples = array("h", [0, 1000, -1000, 2000, -2000] * 100)
    with wave.open(str(output), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(samples.tobytes())


def test_diagnostic_reports_excerpt_and_signal_without_retaining_path(
    tmp_path,
    monkeypatch,
):
    media = tmp_path / "private-title.mkv"
    media.touch()
    monkeypatch.setattr(
        "mkv_episode_matcher.media.transcript_diagnostic.extract_audio_chunk",
        _fake_extract,
    )

    result = diagnose_transcript(
        media,
        FakeAsr(),
        media_id="media-1",
        start_seconds=300,
        duration_seconds=30,
        model_name="small",
        audio_stream_index=2,
    )

    assert result.transcript_words == 11
    assert "dragon" in result.excerpt
    assert result.mean_dbfs is not None
    assert result.peak_dbfs is not None
    assert str(media) not in json.dumps(result.safe_report())
    assert "dragon" not in json.dumps(result.safe_report())


def test_report_refuses_overwrite(tmp_path):
    diagnostic = TranscriptDiagnostic(
        "media-1",
        2,
        300,
        30,
        "small",
        10,
        2,
        -30,
        -5,
        "private dialogue",
    )
    path = tmp_path / "report.json"

    write_safe_report(path, diagnostic)
    with pytest.raises(TranscriptDiagnosticError, match="refusing overwrite"):
        write_safe_report(path, diagnostic)


def test_duration_is_bounded(tmp_path):
    media = tmp_path / "title.mkv"
    media.touch()

    with pytest.raises(TranscriptDiagnosticError, match="between 5 and 60"):
        diagnose_transcript(
            media,
            FakeAsr(),
            media_id="media-1",
            start_seconds=0,
            duration_seconds=120,
            model_name="small",
        )


def test_audio_stream_index_must_not_be_negative(tmp_path):
    media = tmp_path / "title.mkv"
    media.touch()

    with pytest.raises(TranscriptDiagnosticError, match="must not be negative"):
        diagnose_transcript(
            media,
            FakeAsr(),
            media_id="media-1",
            start_seconds=0,
            duration_seconds=30,
            model_name="small",
            audio_stream_index=-1,
        )
