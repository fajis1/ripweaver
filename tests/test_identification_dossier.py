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


def test_collect_keeps_audio_less_title_as_runtime_only_evidence(
    tmp_path, monkeypatch
):
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
