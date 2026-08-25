import json
from pathlib import Path

import pytest

from mkv_episode_matcher.media.subtitle_ocr import (
    OcrCandidate,
    SubtitleOcrDiagnostic,
    SubtitleOcrError,
    build_seconv_ocr_command,
    parse_srt_text,
    rank_ocr_references,
    run_seconv_ocr,
    write_safe_ocr_report,
)


def test_parse_srt_text_removes_structure_and_counts_captions():
    content = """1
00:00:01,000 --> 00:00:02,000
<i>Follow the red dragon.</i>

2
00:00:03,000 --> 00:00:04,000
It entered the hidden cave.
"""

    text, caption_count = parse_srt_text(content)

    assert text == "Follow the red dragon. It entered the hidden cave."
    assert caption_count == 2


def test_shingle_ranking_has_large_margin_for_matching_reference():
    query = "follow the red dragon into the hidden cave before sunset"
    references = {
        "S03E01": "we should follow the red dragon into the hidden cave before sunset",
        "S03E02": "the riders repair their boats beside the village",
    }

    candidates = rank_ocr_references(query, references, top_k=2)

    assert [candidate.reference_id for candidate in candidates] == [
        "S03E01",
        "S03E02",
    ]
    assert candidates[0].query_coverage > 0.8
    assert candidates[1].query_coverage == 0


def test_command_uses_exact_output_filename_and_refuses_collision(tmp_path):
    media = tmp_path / "episode.mkv"
    media.touch()
    seconv = tmp_path / "seconv.exe"
    seconv.touch()
    output = tmp_path / "episode-ocr.srt"

    command = build_seconv_ocr_command(
        media,
        output,
        seconv,
        track_number=3,
    )

    assert f"--output-filename:{output.name}" in command
    assert "--track-number:3" in command
    assert "--overwrite" not in command

    output.touch()
    with pytest.raises(SubtitleOcrError, match="refusing overwrite"):
        build_seconv_ocr_command(media, output, seconv)


def test_run_ocr_is_sequential_and_reads_generated_srt(tmp_path, monkeypatch):
    media = tmp_path / "episode.mkv"
    media.touch()
    seconv = tmp_path / "seconv.exe"
    seconv.touch()
    tesseract = tmp_path / "tesseract.exe"
    tesseract.touch()
    output = tmp_path / "diagnostics" / "episode.srt"

    def fake_run(command, **kwargs):
        Path(next(arg.split(":", 1)[1] for arg in command if arg.startswith(
            "--output-folder:"
        ))).mkdir(parents=True, exist_ok=True)
        output.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nReadable dialogue.\n",
            encoding="utf-8",
        )
        assert kwargs["timeout"] == 30
        assert str(tesseract.parent) in kwargs["env"]["PATH"]

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(
        "mkv_episode_matcher.media.subtitle_ocr.subprocess.run",
        fake_run,
    )

    text, captions = run_seconv_ocr(
        media,
        output,
        seconv,
        tesseract,
        timeout_seconds=30,
    )

    assert text == "Readable dialogue."
    assert captions == 1


def test_safe_report_omits_dialogue_and_paths_and_refuses_overwrite(tmp_path):
    diagnostic = SubtitleOcrDiagnostic(
        media_id="media-1",
        caption_count=10,
        text_characters=100,
        text_words=20,
        candidates=(OcrCandidate("S03E08", 0.96, 0.91, 1900),),
    )
    report_path = tmp_path / "ocr-report.json"

    write_safe_ocr_report(report_path, diagnostic)
    serialized = report_path.read_text(encoding="utf-8")

    assert "S03E08" in serialized
    assert "dialogue" not in serialized
    assert "G:\\" not in serialized
    assert json.loads(serialized)["mode"] == "embedded-subtitle-ocr-diagnostic"
    with pytest.raises(SubtitleOcrError, match="refusing overwrite"):
        write_safe_ocr_report(report_path, diagnostic)
