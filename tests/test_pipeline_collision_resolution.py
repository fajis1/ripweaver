import json
from types import SimpleNamespace

from mkv_episode_matcher.backend.routers import rip
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact


def _held_collision(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    raw = tmp_path / "raw.mkv"
    raw.write_bytes(b"raw")
    encoded = tmp_path / "encoded.mkv"
    encoded.write_bytes(b"new-verified")
    rip_contract = contracts / "media-1.verified-rip.json"
    rip_contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "source_path": str(raw),
            "source_size_bytes": raw.stat().st_size,
        }),
        encoding="utf-8",
    )
    identify_contract = contracts / "media-1.identify.json"
    identify_contract.write_text(
        json.dumps({
            "mode": "identified-episode-contract",
            "source_path": str(raw),
            "source_size_bytes": raw.stat().st_size,
            "episode_id": "S01E01",
            "library_relative": "Show/Season 01/Show - S01E01 - First.mkv",
        }),
        encoding="utf-8",
    )
    transcode_contract = contracts / "media-1.transcode.json"
    transcode_contract.write_text(
        json.dumps({
            "mode": "verified-transcode-contract",
            "encoded_path": str(encoded),
            "encoded_size_bytes": encoded.stat().st_size,
            "original_source_path": str(raw),
            "original_source_size_bytes": raw.stat().st_size,
            "episode_id": "S01E01",
            "encoded_height": 1080,
            "encoded_field_order": "progressive",
            "library_relative": "Show/Season 01/Show - S01E01 - First.mkv",
        }),
        encoding="utf-8",
    )
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip("media-1", build_artifact("rip", rip_contract))
    store.claim_next()
    store.complete_stage(
        "media-1", "identify", build_artifact("identify", identify_contract)
    )
    store.claim_next()
    store.complete_stage(
        "media-1", "transcode", build_artifact("transcode", transcode_contract)
    )
    store.claim_next()
    held = store.require_review("media-1", "library_collision")
    return store, contracts, raw, encoded, held


def test_delete_new_collision_media_preserves_raw_and_library(tmp_path):
    store, contracts, raw, encoded, held = _held_collision(tmp_path)

    response = rip.resolve_pipeline_library_collision(
        "media-1",
        rip.ResolveLibraryCollisionRequest(
            action="delete-new",
            expected_artifact_sha256=held.artifact.contract_sha256,
            confirm_resolution=True,
        ),
        store,
        contracts,
    )

    assert response["state"] == "discarded"
    assert not encoded.exists()
    assert raw.read_bytes() == b"raw"


def test_collision_resolution_retires_held_duplicate_title_lineage(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    fingerprint = "0123456789abcdef"
    media_ids = (
        f"disc-01-{fingerprint}-title-000",
        f"disc-01-{fingerprint}-title-000-recovery-abc123",
    )
    for media_id in media_ids:
        contract = contracts / f"{media_id}.json"
        contract.write_text(
            json.dumps({
                "disc_fingerprint": fingerprint,
                "title_index": 0,
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        store.claim_next()
        store.require_review(media_id, "library_collision")

    retired = rip._dismiss_duplicate_title_lineage(
        store,
        resolved_media_id=media_ids[0],
        disc_fingerprint=fingerprint,
        title_index=0,
    )

    assert retired == 1
    assert store.get(media_ids[0]).state == "review_required"
    assert store.get(media_ids[1]).state == "discarded"


def test_verified_replacement_backs_up_old_library_file(tmp_path, monkeypatch):
    store, contracts, raw, _encoded, held = _held_collision(tmp_path)
    library = tmp_path / "library"
    season = library / "Show" / "Season 01"
    season.mkdir(parents=True)
    existing = season / "Show - S01E01 - Existing - 720p.mkv"
    existing.write_bytes(b"old-library")
    deletion = tmp_path / "ready-to-delete"
    deletion.mkdir()
    config = SimpleNamespace(
        jellyfin_tv_root=library,
        jellyfin_movie_root=None,
        deletion_staging_root=deletion,
    )
    monkeypatch.setattr(
        rip, "get_config_manager", lambda: SimpleNamespace(load=lambda: config)
    )

    response = rip.resolve_pipeline_library_collision(
        "media-1",
        rip.ResolveLibraryCollisionRequest(
            action="replace-library",
            expected_artifact_sha256=held.artifact.contract_sha256,
            confirm_resolution=True,
        ),
        store,
        contracts,
    )

    assert response["state"] == "completed"
    assert existing.read_bytes() == b"new-verified"
    backup = deletion / "library-replacements" / "media-1" / existing.name
    assert backup.read_bytes() == b"old-library"
    assert raw.read_bytes() == b"raw"
