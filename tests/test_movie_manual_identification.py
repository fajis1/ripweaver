import json
from mkv_episode_matcher.backend.routers.rip import (
    ManualEpisodeIdentificationRequest,
    _pipeline_item_response,
    apply_manual_episode_identification,
)
from mkv_episode_matcher.pipeline_adapters import IdentifyStageAdapter
from mkv_episode_matcher.pipeline_queue import (
    PipelineQueueStore,
    build_artifact,
)


def test_manual_movie_identification_provisional_review(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic-content")
    database = tmp_path / "pipeline.sqlite3"
    store = PipelineQueueStore(database)
    rip = tmp_path / "rip.json"
    rip.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "disc_fingerprint": "2a03751d904bec03",
            "title_index": 0,
            "media_context": {
                "series_name": "THREE MUSKET DISC4 SIDEA REVISED",
                "special_feature_assignments": [
                    {
                        "title_index": 0,
                        "classification": "matched-feature",
                        "fallback_name_policy": "none",
                        "matched_title": "The Three Musketeers Part 2",
                        "media_kind": "movie",
                        "provisional_match": True,
                    }
                ],
            },
        }),
        encoding="utf-8",
    )
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store.enqueue_verified_rip("media-movie-1", build_artifact("rip", rip))
    store.claim_next()
    store.require_review("media-movie-1", "provisional_content_identity_review_required")

    item = store.get("media-movie-1")
    resp = _pipeline_item_response(item)
    assert resp["display_name"] == "The Three Musketeers Part 2"
    assert resp["provisional_match"] is True
    assert resp["provisional_name"] == "The Three Musketeers Part 2"

    response = apply_manual_episode_identification(
        "media-movie-1",
        ManualEpisodeIdentificationRequest(
            new_name="The Three Musketeers (1993) - pt1",
            content_type="movie",
            confirm_identification=True,
        ),
        store,
        contracts,
    )

    assert response["stage"] == "identify"
    assert response["state"] == "queued"
    running = store.claim_next()
    identified = IdentifyStageAdapter(object(), contracts)(running)
    completed = store.complete_stage("media-movie-1", "identify", identified)
    assert completed.stage == "transcode"
    identified_payload = json.loads(identified.contract_path.read_text())
    assert identified_payload["library_kind"] == "movie"
    assert identified_payload["library_relative"] == (
        "The Three Musketeers (1993)/The Three Musketeers (1993) - pt1.mkv"
    )
