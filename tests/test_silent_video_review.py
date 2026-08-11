from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from mkv_episode_matcher.backend.routers import rip
from mkv_episode_matcher.backend.routers.rip import SilentVideoOcrRequest
from mkv_episode_matcher.media import silent_video_review
from mkv_episode_matcher.media.silent_video_review import (
    classify_silent_video_text,
    collect_silent_video_review,
    resolve_tesseract_path,
)
from mkv_episode_matcher.media.special_feature_evidence import (
    EvidenceItemResult,
    SpecialFeatureEvidenceResult,
)
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact


def test_classifies_warning_and_menu_text_conservatively():
    warning = classify_silent_video_text(
        "WARNING Unauthorized duplication is prohibited"
    )
    menu = classify_silent_video_text("Play All   Episode Selection")
    unknown = classify_silent_video_text("A Hanna-Barbera Production")

    assert warning[0] == "likely_warning_screen"
    assert menu[0] == "likely_disc_menu"
    assert unknown[0] == "text_detected"
    assert "delete" not in warning[1].casefold().replace(
        "deciding whether to delete", ""
    )


def test_classifies_common_non_english_rights_warnings():
    french = classify_silent_video_text(
        "ATTENTION usage strictement familial reproduction publique interdite"
    )
    german = classify_silent_video_text("WARNUNG Urheberrecht alle Rechte vorbehalten")
    spanish = classify_silent_video_text("ADVERTENCIA reproducción no autorizada")

    assert french[0] == "likely_warning_screen"
    assert german[0] == "likely_warning_screen"
    assert spanish[0] == "likely_warning_screen"


def test_resolves_standard_tesseract_when_setting_is_empty(tmp_path, monkeypatch):
    installed = tmp_path / "tesseract.exe"
    installed.write_bytes(b"tool")
    monkeypatch.setattr(
        silent_video_review.shutil, "which", lambda _name: str(installed)
    )

    assert resolve_tesseract_path(None) == installed.resolve()


def test_collects_six_frames_and_returns_bounded_private_excerpt(
    tmp_path: Path, monkeypatch
):
    media = tmp_path / "silent.mkv"
    media.write_bytes(b"synthetic")
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"tool")
    tesseract = tmp_path / "tesseract.exe"
    tesseract.write_bytes(b"tool")
    output = tmp_path / "evidence"
    output.mkdir()

    def fake_collect(plan, **_kwargs):
        item = plan.items[0]
        item_root = plan.output_root / item.media_id
        item_root.mkdir()
        (item_root / "contact-sheet-ocr.txt").write_text(
            "WARNING " + ("private words " * 100), encoding="utf-8"
        )
        return SpecialFeatureEvidenceResult(
            items=(
                EvidenceItemResult(
                    media_id=item.media_id,
                    status="collected",
                    contact_sheet_created=True,
                    ocr_text_characters=1410,
                    sampled_audio_streams=(),
                ),
            )
        )

    monkeypatch.setattr(
        silent_video_review, "collect_special_feature_evidence", fake_collect
    )
    result = collect_silent_video_review(
        media_id="a-long-internal-recovery-id-that-does-not-become-a-path",
        media_path=media,
        duration_seconds=120,
        output_root=output,
        ffmpeg_path=ffmpeg,
        tesseract_path=tesseract,
    )

    assert result.category == "likely_warning_screen"
    assert result.sampled_frame_count == 6
    assert len(result.ocr_excerpt) == 600
    assert list(output.iterdir())[0].name.startswith("silent-")
    assert "recovery-id" not in list(output.iterdir())[0].name


def test_silent_video_endpoint_binds_exact_failed_artifact(tmp_path, monkeypatch):
    media = tmp_path / "silent.mkv"
    media.write_bytes(b"synthetic media")
    contract = tmp_path / "rip.json"
    contract.write_text(
        '{"source_path": "'
        + str(media).replace("\\", "\\\\")
        + f'", "source_size_bytes": {media.stat().st_size}}}',
        encoding="utf-8",
    )
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip("silent-title", build_artifact("rip", contract))
    assert store.claim_next() is not None
    failed = store.fail("silent-title", "HandBrakeNoUsableAudio")
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"tool")
    tesseract = tmp_path / "tesseract.exe"
    tesseract.write_bytes(b"tool")

    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                ffmpeg_path=ffmpeg,
                ffprobe_path=tmp_path / "ffprobe.exe",
                tesseract_path=tesseract,
            )
        ),
    )
    monkeypatch.setattr(rip, "resolve_ffprobe_path", lambda path: path)
    monkeypatch.setattr(
        rip,
        "collect_silent_video_review",
        lambda **_kwargs: silent_video_review.SilentVideoReviewResult(
            category="likely_warning_screen",
            summary="Review before deleting.",
            ocr_excerpt="WARNING",
            ocr_text_characters=7,
        ),
    )
    response = rip.analyze_silent_pipeline_item(
        "silent-title",
        SilentVideoOcrRequest(
            expected_artifact_sha256=failed.artifact.contract_sha256,
            confirm_media_read=True,
        ),
        store,
        tmp_path / "contracts",
        lambda *_args, **_kwargs: SimpleNamespace(
            media=SimpleNamespace(duration_seconds=120)
        ),
    )

    assert response["category"] == "likely_warning_screen"
    assert response["ocr_excerpt"] == "WARNING"
    assert store.get("silent-title").state == "failed"
    assert PipelineQueueStore(store.database_path).silent_video_review_flags() == {
        "silent-title": "likely_warning_screen"
    }
    event = store.list_events("silent-title")[-1]
    assert event.event_type == "silent_video_reviewed"
    assert "WARNING" not in str(event.details)
