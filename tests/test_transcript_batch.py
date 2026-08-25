import json
import wave
from array import array
from pathlib import Path

import pytest

from mkv_episode_matcher.media.probe import ProbedAudioStream, ProbedMedia
from mkv_episode_matcher.media.transcript_batch import (
    CollectedFile,
    CollectedWindow,
    FFmpegSampleExtractor,
    TranscriptBatchError,
    TranscriptBatchItem,
    TranscriptBatchResult,
    _collection_windows,
    build_ffmpeg_sample_command,
    collect_transcript_batch,
    validate_new_report_paths,
    write_private_transcript_report,
    write_safe_metrics_report,
)


def _stream(index, *, channels=6, default=True):
    return ProbedAudioStream(
        index=index,
        codec="ac3",
        language="eng",
        title=None,
        channels=channels,
        channel_layout="5.1" if channels == 6 else "stereo",
        is_default=default,
        is_commentary=False,
    )


def test_offset_expanded_windows_are_distinct_from_initial_six():
    media = ProbedMedia(
        duration_seconds=1400,
        size_bytes=1,
        container="matroska",
        audio_streams=(_stream(0),),
    )
    item = TranscriptBatchItem("media-1", Path("synthetic.mkv"), media)

    initial = _collection_windows(item, "expanded", intro_start_seconds=60)
    retry = _collection_windows(item, "expanded-offset", intro_start_seconds=60)

    assert len(initial) == len(retry) == 6
    assert {window.start_seconds for window in initial}.isdisjoint(
        window.start_seconds for window in retry
    )


def _media(*streams):
    return ProbedMedia(1800, 1000, "matroska", tuple(streams))


def _item(tmp_path, file_id="disc-01-title-000", streams=None):
    path = tmp_path / f"{file_id}.mkv"
    path.touch()
    return TranscriptBatchItem(
        file_id,
        path,
        _media(*(streams or (_stream(2),))),
    )


def _write_wav(path, amplitude=2000):
    samples = array("h", [amplitude, -amplitude] * 800)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(samples.tobytes())


class FakeExtractor:
    def __init__(self, amplitudes=None, failures=None):
        self.amplitudes = amplitudes or {}
        self.failures = failures or set()
        self.calls = []
        self.temporary_paths = []

    def extract(self, media_path, **kwargs):
        self.calls.append((media_path, kwargs))
        output = kwargs["output_path"]
        self.temporary_paths.append(output)
        stream_index = kwargs["audio_stream_index"]
        if stream_index in self.failures:
            raise RuntimeError("private extraction detail")
        _write_wav(output, self.amplitudes.get(stream_index, 2000))


class FakeAsr:
    def __init__(self, text_by_stream=None):
        self.text_by_stream = text_by_stream or {}
        self.load_calls = 0
        self.transcribe_calls = []

    def load(self):
        self.load_calls += 1

    def transcribe(self, path):
        self.transcribe_calls.append(path)
        stream_index = int(path.name.split("-stream-")[1].split("-")[0])
        return self.text_by_stream.get(
            stream_index,
            "The dragon follows a rider into the hidden cave beyond the village.",
        )


def test_ffmpeg_command_is_fixed_no_shell_and_refuses_overwrite(tmp_path):
    executable = tmp_path / "ffmpeg.exe"
    executable.touch()
    media = tmp_path / "episode.mkv"
    media.touch()
    output = tmp_path / "sample.wav"

    command = build_ffmpeg_sample_command(
        executable,
        media,
        start_seconds=300,
        duration_seconds=30,
        audio_stream_index=2,
        output_path=output,
    )

    assert command[0] == str(executable.resolve())
    assert command[-2:] == ("-n", str(output))
    assert ("-map", "0:2") == command[command.index("-map") : command.index("-map") + 2]
    assert "-y" not in command
    output.touch()
    with pytest.raises(TranscriptBatchError, match="new .wav"):
        build_ffmpeg_sample_command(
            executable,
            media,
            start_seconds=300,
            duration_seconds=30,
            audio_stream_index=2,
            output_path=output,
        )


def test_batch_loads_model_once_and_removes_temporary_audio(tmp_path):
    items = (
        _item(tmp_path, "disc-01-title-000"),
        _item(tmp_path, "disc-01-title-001"),
    )
    asr = FakeAsr()
    extractor = FakeExtractor()

    result = collect_transcript_batch(
        items,
        asr,
        extractor,
        model_name="small",
    )

    assert result.succeeded
    assert asr.load_calls == 1
    assert len(asr.transcribe_calls) == 6
    assert {item.audio_stream_index for item in result.files} == {2}
    assert all(not path.exists() for path in extractor.temporary_paths)


def test_usable_multichannel_stops_before_stereo_fallback(tmp_path):
    item = _item(
        tmp_path,
        streams=(
            _stream(2, channels=6, default=True),
            _stream(3, channels=2, default=False),
        ),
    )
    extractor = FakeExtractor()

    result = collect_transcript_batch(
        (item,),
        FakeAsr(),
        extractor,
        model_name="small",
    )

    assert result.files[0].audio_stream_index == 2
    assert {call[1]["audio_stream_index"] for call in extractor.calls} == {2}


def test_weak_primary_uses_ranked_alternate(tmp_path):
    item = _item(
        tmp_path,
        streams=(
            _stream(2, channels=2, default=True),
            _stream(3, channels=6, default=False),
        ),
    )
    asr = FakeAsr({
        2: "uh",
        3: "The princess enters the hidden kingdom and discovers the magic mirror.",
    })

    result = collect_transcript_batch(
        (item,),
        asr,
        FakeExtractor(),
        model_name="small",
    )

    assert result.files[0].status == "collected"
    assert result.files[0].audio_stream_index == 3
    assert result.files[0].attempted_streams == (2, 3)


def test_intro_mode_collects_only_the_reviewed_start_window(tmp_path):
    extractor = FakeExtractor()

    result = collect_transcript_batch(
        (_item(tmp_path),),
        FakeAsr(),
        extractor,
        model_name="small",
        sampling_mode="intro",
        intro_start_seconds=120,
    )

    assert result.succeeded
    assert len(extractor.calls) == 1
    assert extractor.calls[0][1]["start_seconds"] == 120
    assert result.files[0].windows[0].start_seconds == 120


def test_preferred_audio_stream_is_attempted_first(tmp_path):
    item = _item(
        tmp_path,
        streams=(
            _stream(2, channels=6, default=True),
            _stream(3, channels=2, default=False),
        ),
    )
    extractor = FakeExtractor()

    result = collect_transcript_batch(
        (item,),
        FakeAsr(),
        extractor,
        model_name="small",
        preferred_stream_index=3,
    )

    assert result.files[0].audio_stream_index == 3
    assert result.files[0].attempted_streams[0] == 3
    assert {call[1]["audio_stream_index"] for call in extractor.calls} == {3}


def test_missing_preferred_audio_stream_stops_safely(tmp_path):
    with pytest.raises(TranscriptBatchError, match="absent"):
        collect_transcript_batch(
            (_item(tmp_path),),
            FakeAsr(),
            FakeExtractor(),
            model_name="small",
            preferred_stream_index=99,
        )


def test_failed_file_does_not_prevent_later_file(tmp_path):
    first = _item(
        tmp_path,
        "disc-01-title-000",
        streams=(_stream(2),),
    )
    second = _item(
        tmp_path,
        "disc-01-title-001",
        streams=(_stream(3),),
    )

    result = collect_transcript_batch(
        (first, second),
        FakeAsr(),
        FakeExtractor(failures={2}),
        model_name="small",
    )

    assert result.files[0].status == "review-audio"
    assert result.files[0].failure_code == "sample-failed:RuntimeError"
    assert result.files[1].status == "collected"


def test_private_and_safe_reports_separate_dialogue_and_paths(tmp_path):
    result = TranscriptBatchResult(
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
    private_path = tmp_path / "private.json"
    safe_path = tmp_path / "safe.json"

    validate_new_report_paths(private_path, safe_path)
    write_private_transcript_report(private_path, result)
    write_safe_metrics_report(safe_path, result)

    assert "Private dragon dialogue" in private_path.read_text(encoding="utf-8")
    safe_text = safe_path.read_text(encoding="utf-8")
    assert "dragon" not in safe_text
    assert "media_path" not in safe_text
    assert json.loads(safe_text)["collected_count"] == 1


def test_ffmpeg_runner_redacts_process_detail(tmp_path, monkeypatch):
    executable = tmp_path / "ffmpeg.exe"
    executable.touch()
    media = tmp_path / "private-name.mkv"
    media.touch()
    output = tmp_path / "sample.wav"

    class Completed:
        returncode = 1
        stderr = b"private-name.mkv failed"

    monkeypatch.setattr(
        "mkv_episode_matcher.media.transcript_batch.subprocess.run",
        lambda *_args, **_kwargs: Completed(),
    )

    with pytest.raises(TranscriptBatchError) as raised:
        FFmpegSampleExtractor(executable).extract(
            media,
            start_seconds=300,
            duration_seconds=30,
            audio_stream_index=2,
            output_path=output,
        )

    assert "private-name" not in str(raised.value)
