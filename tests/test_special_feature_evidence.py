from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mkv_episode_matcher.media import special_feature_evidence
from mkv_episode_matcher.media.special_feature_evidence import (
    SpecialFeatureEvidenceError,
    SpecialFeatureEvidenceItem,
    SpecialFeatureEvidencePlan,
    build_audio_sample_command,
    build_contact_sheet_command,
    collect_special_feature_evidence,
    validate_evidence_plan,
    write_safe_evidence_report,
)


def _file(path: Path, content: bytes = b"x") -> Path:
    path.write_bytes(content)
    return path


def _plan(tmp_path: Path) -> SpecialFeatureEvidencePlan:
    output = tmp_path / "evidence"
    output.mkdir()
    ffmpeg = _file(tmp_path / "ffmpeg.exe")
    tesseract = _file(tmp_path / "tesseract.exe")
    items = (
        SpecialFeatureEvidenceItem(
            "title-004",
            _file(tmp_path / "four.mkv"),
            102,
        ),
        SpecialFeatureEvidenceItem(
            "title-011",
            _file(tmp_path / "eleven.mkv"),
            102,
        ),
        SpecialFeatureEvidenceItem(
            "title-012",
            _file(tmp_path / "twelve.mkv"),
            145,
            audio_stream_indexes=(1, 2, 3, 4),
        ),
    )
    return SpecialFeatureEvidencePlan(items, output, ffmpeg, tesseract)


def test_commands_are_fixed_collision_refusing_and_explicit(tmp_path):
    plan = _plan(tmp_path)
    item = plan.items[2]
    sheet = plan.output_root / "sheet.png"
    contact = build_contact_sheet_command(plan.ffmpeg_path, item, sheet)
    audio = build_audio_sample_command(
        plan.ffmpeg_path,
        item,
        stream_index=4,
        start_seconds=15,
        duration_seconds=30,
        output_path=plan.output_root / "stream.wav",
    )

    assert "-n" in contact
    assert "-n" in audio
    assert "0:4" in audio
    assert "-ac" in audio and audio[audio.index("-ac") + 1] == "2"
    assert str(item.media_path.resolve()) in contact
    assert not any(token in contact for token in ("-y", "shell=True"))


def test_plan_preflights_all_collisions_before_running(tmp_path):
    plan = _plan(tmp_path)
    (plan.output_root / "title-011").mkdir()

    with pytest.raises(SpecialFeatureEvidenceError, match="refusing overwrite"):
        validate_evidence_plan(plan)


def test_collection_runs_three_items_and_safe_report_has_no_paths_or_text(tmp_path):
    plan = _plan(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake_runner(command: tuple[str, ...], *, timeout_seconds: float):
        calls.append(command)
        if Path(command[0]).name == "tesseract.exe":
            return subprocess.CompletedProcess(command, 0, "PRIVATE OCR WORDS", "")
        output = Path(command[-1])
        output.write_bytes(b"x" * 2048)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = collect_special_feature_evidence(plan, runner=fake_runner, max_workers=3)

    assert [item.status for item in result.items] == ["collected"] * 3
    assert result.items[2].sampled_audio_streams == (1, 2, 3, 4)
    serialized = str(result.safe_report())
    assert "PRIVATE OCR WORDS" not in serialized
    assert str(tmp_path) not in serialized
    assert len(calls) == 10  # 3 contact sheets, 3 OCR passes, 4 audio samples


def test_failure_isolated_and_redacted(tmp_path):
    plan = _plan(tmp_path)

    def fake_runner(command: tuple[str, ...], *, timeout_seconds: float):
        if "eleven.mkv" in " ".join(command):
            return subprocess.CompletedProcess(command, 9, "", "private path")
        if Path(command[0]).name == "tesseract.exe":
            return subprocess.CompletedProcess(command, 0, "words", "")
        Path(command[-1]).write_bytes(b"x" * 2048)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = collect_special_feature_evidence(plan, runner=fake_runner)

    assert [item.status for item in result.items] == [
        "collected",
        "failed",
        "collected",
    ]
    serialized = str(result.safe_report())
    assert "private path" not in serialized
    assert "eleven.mkv" not in serialized


def test_rejects_unplanned_audio_stream_and_unsafe_id(tmp_path):
    plan = _plan(tmp_path)
    with pytest.raises(SpecialFeatureEvidenceError, match="not authorized"):
        build_audio_sample_command(
            plan.ffmpeg_path,
            plan.items[2],
            stream_index=7,
            start_seconds=15,
            duration_seconds=30,
            output_path=plan.output_root / "stream.wav",
        )

    bad = SpecialFeatureEvidencePlan(
        (
            SpecialFeatureEvidenceItem(
                "../escape",
                plan.items[0].media_path,
                102,
            ),
        ),
        plan.output_root,
        plan.ffmpeg_path,
        plan.tesseract_path,
    )
    with pytest.raises(SpecialFeatureEvidenceError, match="path-safe"):
        validate_evidence_plan(bad)


def test_safe_report_writer_refuses_overwrite_and_omits_private_data(tmp_path):
    plan = _plan(tmp_path)

    def fake_runner(command: tuple[str, ...], *, timeout_seconds: float):
        if Path(command[0]).name == "tesseract.exe":
            return subprocess.CompletedProcess(command, 0, "PRIVATE OCR WORDS", "")
        Path(command[-1]).write_bytes(b"x" * 2048)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = collect_special_feature_evidence(plan, runner=fake_runner)
    report = plan.output_root / "metrics.json"

    write_safe_evidence_report(report, result)

    serialized = report.read_text(encoding="utf-8")
    assert "PRIVATE OCR WORDS" not in serialized
    assert str(tmp_path) not in serialized
    with pytest.raises(SpecialFeatureEvidenceError, match="refusing overwrite"):
        write_safe_evidence_report(report, result)


def test_default_runner_uses_utf8_replacement_decoding(monkeypatch):
    observed = {}

    def run(command, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(special_feature_evidence.subprocess, "run", run)

    special_feature_evidence._default_runner(("tool",), timeout_seconds=5)

    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "replace"
