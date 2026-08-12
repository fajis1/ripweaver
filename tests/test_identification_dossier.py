from pathlib import Path
from types import SimpleNamespace

import pytest

from mkv_episode_matcher.backend import identification_dossier as dossier_module
from mkv_episode_matcher.backend.identification_dossier import (
    IdentificationDossierStore,
    collect_dossier_evidence,
    source_identity,
)
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.gemini_matcher import UnmatchedFileEvidence
from mkv_episode_matcher.pipeline_queue import PipelineQueueError


def _payload(source, *, title_index=0):
    return {
        "source_path": str(source),
        "source_size_bytes": source.stat().st_size,
        "disc_fingerprint": "0123456789abcdef",
        "title_index": title_index,
    }


def test_exact_source_evidence_survives_restart(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    identity = source_identity(_payload(source), source, "small")
    root = tmp_path / "private"
    first = IdentificationDossierStore(root)
    first.save_evidence(
        identity, UnmatchedFileEvidence("media-1", 1200, ("bounded dialogue",))
    )

    restored = IdentificationDossierStore(root).load_evidence("media-1", identity)

    assert restored == UnmatchedFileEvidence("media-1", 1200, ("bounded dialogue",))


def test_runtime_only_evidence_is_cached_for_provider_fallback(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    identity = source_identity(_payload(source), source, "small")
    root = tmp_path / "private"
    IdentificationDossierStore(root).save_evidence(
        identity, UnmatchedFileEvidence("media-1", 1200, ())
    )

    assert IdentificationDossierStore(root).load_evidence(
        "media-1", identity
    ) == UnmatchedFileEvidence("media-1", 1200, ())


def test_cache_invalidates_when_verified_source_changes(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    payload = _payload(source)
    old_identity = source_identity(payload, source, "small")
    store = IdentificationDossierStore(tmp_path / "private")
    store.save_evidence(
        old_identity, UnmatchedFileEvidence("media-1", 1200, ("evidence",))
    )
    source.write_bytes(b"different-size")
    payload["source_size_bytes"] = source.stat().st_size

    assert (
        store.load_evidence("media-1", source_identity(payload, source, "small"))
        is None
    )


def test_collect_reuses_cache_without_reinvoking_media_tools(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    payload = _payload(source)
    item = SimpleNamespace(media_id="media-1")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store = IdentificationDossierStore(tmp_path / "identification-evidence")
    identity = source_identity(payload, source, "small")
    store.save_evidence(
        identity, UnmatchedFileEvidence("media-1", 1200, ("cached evidence",))
    )
    monkeypatch.setattr(
        dossier_module,
        "resolve_ffprobe_path",
        lambda _path: pytest.fail("FFprobe must not run on a cache hit"),
    )

    evidence, _dossier = collect_dossier_evidence(
        ((item, payload),), Config(asr_model_name="small"), object(), contracts
    )

    assert evidence[0].transcript_excerpts == ("cached evidence",)


def test_gemini_evidence_adds_bounded_on_screen_ocr_text(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    payload = _payload(source)
    item = SimpleNamespace(media_id="media-1")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    identity = source_identity(payload, source, "small")
    IdentificationDossierStore(tmp_path / "identification-evidence").save_evidence(
        identity, UnmatchedFileEvidence("media-1", 1200, ("cached dialogue",))
    )
    monkeypatch.setattr(
        dossier_module, "resolve_ffmpeg_path", lambda _path: Path("ffmpeg")
    )
    monkeypatch.setattr(
        dossier_module, "resolve_tesseract_path", lambda _path: Path("tesseract")
    )
    monkeypatch.setattr(
        dossier_module,
        "collect_silent_video_review",
        lambda **_kwargs: SimpleNamespace(
            category="text_detected",
            ocr_excerpt="Songs of the Synthetic Family featuring the original voices",
            ocr_text_characters=66,
            sampled_frame_count=6,
        ),
    )
    recorded = []

    evidence, _dossier = collect_dossier_evidence(
        ((item, payload),),
        Config(asr_model_name="small"),
        object(),
        contracts,
        True,
        lambda media_id, category: recorded.append((media_id, category)),
    )

    assert evidence[0].transcript_excerpts == (
        "On-screen text (OCR): Songs of the Synthetic Family featuring the original voices",
        "cached dialogue",
    )
    assert recorded == [("media-1", "text_detected")]
    assert next(_dossier.root.glob("visual-*"), None) is not None
    assert _dossier.safe_attempts("media-1")[-1]["branch"] == "visual-ocr"


def test_visual_failure_is_recorded_instead_of_silently_skipped(tmp_path, monkeypatch):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    payload = _payload(source)
    item = SimpleNamespace(media_id="media-1")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    identity = source_identity(payload, source, "small")
    IdentificationDossierStore(tmp_path / "identification-evidence").save_evidence(
        identity, UnmatchedFileEvidence("media-1", 1200, ("cached dialogue",))
    )
    monkeypatch.setattr(
        dossier_module, "resolve_ffmpeg_path", lambda _path: Path("ffmpeg")
    )
    monkeypatch.setattr(
        dossier_module, "resolve_tesseract_path", lambda _path: Path("tesseract")
    )
    monkeypatch.setattr(
        dossier_module,
        "collect_silent_video_review",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )

    evidence, dossier = collect_dossier_evidence(
        ((item, payload),),
        Config(asr_model_name="small"),
        object(),
        contracts,
        True,
    )

    assert evidence[0].transcript_excerpts == ("cached dialogue",)
    attempt = dossier.safe_attempts("media-1")[-1]
    assert attempt["branch"] == "visual-ocr"
    assert attempt["disposition"] == "failed"
    assert attempt["summary"] == {"reason": "RuntimeError"}


def test_collect_reuses_exact_source_cache_after_media_id_changes(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    payload = _payload(source)
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store = IdentificationDossierStore(tmp_path / "identification-evidence")
    identity = source_identity(payload, source, "small")
    store.save_evidence(
        identity, UnmatchedFileEvidence("original-id", 1200, ("cached evidence",))
    )
    monkeypatch.setattr(
        dossier_module,
        "resolve_ffprobe_path",
        lambda _path: pytest.fail("FFprobe must not run for an exact-source alias"),
    )

    evidence, restored = collect_dossier_evidence(
        ((SimpleNamespace(media_id="recovery-id"), payload),),
        Config(asr_model_name="small"),
        object(),
        contracts,
    )

    assert evidence == (
        UnmatchedFileEvidence("recovery-id", 1200, ("cached evidence",)),
    )
    assert restored.load_evidence("recovery-id", identity) == evidence[0]


def test_collect_keeps_audio_less_title_as_runtime_only_evidence(tmp_path, monkeypatch):
    source = tmp_path / "menu-like.mkv"
    source.write_bytes(b"synthetic")
    payload = _payload(source)
    item = SimpleNamespace(media_id="media-1")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    media = SimpleNamespace(duration_seconds=142.0, audio_streams=())
    monkeypatch.setattr(dossier_module, "resolve_ffprobe_path", lambda _path: "ffprobe")
    monkeypatch.setattr(dossier_module, "resolve_ffmpeg_path", lambda _path: "ffmpeg")
    monkeypatch.setattr(
        dossier_module,
        "inspect_mkv",
        lambda *_args, **_kwargs: SimpleNamespace(media=media),
    )
    monkeypatch.setattr(
        dossier_module,
        "collect_transcript_batch",
        lambda *_args, **_kwargs: pytest.fail(
            "Audio-less titles must not enter transcript batch validation"
        ),
    )

    evidence, _dossier = collect_dossier_evidence(
        ((item, payload),), Config(asr_model_name="small"), object(), contracts
    )

    assert evidence == (UnmatchedFileEvidence("media-1", 142.0, ()),)


def test_attempt_history_is_bounded_and_rejects_nested_private_data(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    identity = source_identity(_payload(source), source, "small")
    store = IdentificationDossierStore(tmp_path / "private")
    store.save_evidence(identity, UnmatchedFileEvidence("media-1", 1, ("x",)))
    for index in range(25):
        store.record_attempt(
            ("media-1",),
            branch="tv-local",
            disposition="review",
            summary={"score": index / 100},
        )
    attempts = store.safe_attempts("media-1")
    assert len(attempts) == 12
    assert attempts[0]["summary"] == {"score": 0.13}
    assert attempts[-1]["summary"] == {"score": 0.24}
    with pytest.raises(PipelineQueueError, match="unsafe"):
        store.record_attempt(
            ("media-1",),
            branch="tv-local",
            disposition="failed",
            summary={"private": {"dialogue": "must not be public"}},
        )


def test_complete_identification_audit_survives_restart_outside_polled_summary(
    tmp_path,
):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    identity = source_identity(_payload(source), source, "small")
    root = tmp_path / "private"
    store = IdentificationDossierStore(root)
    store.save_evidence(
        identity,
        UnmatchedFileEvidence("media-1", 1200, ("private dialogue",)),
    )
    run_id = "a" * 32
    store.record_workflow_event(
        ("media-1",),
        analysis_run_id=run_id,
        phase="analysis-started",
        disposition="started",
        summary={"title_count": 1},
    )
    for index in range(25):
        store.record_attempt(
            ("media-1",),
            branch="tv-local",
            disposition="review",
            analysis_run_id=run_id,
            summary={"score": index / 100},
        )
    store.record_candidate_evaluations(
        "media-1",
        analysis_run_id=run_id,
        branch="tv-opensubtitles",
        evaluations=(
            {
                "candidate_episode_id": "S01E01",
                "candidate_episode_title": "First",
                "score": 0.62,
                "disposition": "rejected",
                "reason": "below_confidence_threshold",
            },
            {
                "candidate_episode_id": "S01E02",
                "candidate_episode_title": "Second",
                "score": 0.91,
                "disposition": "selected",
                "reason": "accepted",
            },
        ),
    )

    restored = IdentificationDossierStore(root)
    audit = restored.audit_events("media-1")

    assert len(restored.safe_attempts("media-1")) == 12
    assert len([event for event in audit if event["event_kind"] == "attempt"]) == 25
    assert [
        event["summary"]["reason"]
        for event in audit
        if event["event_kind"] == "candidate"
    ] == ["below_confidence_threshold", "accepted"]
    assert all(event["analysis_run_id"] == run_id for event in audit)
    assert "private dialogue" not in str(audit)
    assert str(source) not in str(audit)


def test_play_all_attempt_retains_bounded_component_episode_ids(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    identity = source_identity(_payload(source), source, "small")
    store = IdentificationDossierStore(tmp_path / "private")
    store.save_evidence(identity, UnmatchedFileEvidence("media-1", 1, ("x",)))

    store.record_attempt(
        ("media-1",),
        branch="tv-play-all",
        disposition="matched",
        summary={
            "component_episode_ids": ["S01E01", "S01E02"],
            "duration_ratio": 1.01,
        },
    )

    attempt = store.safe_attempts("media-1")[-1]
    assert attempt["branch"] == "tv-play-all"
    assert attempt["summary"]["component_episode_ids"] == ["S01E01", "S01E02"]
