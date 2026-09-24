"""Restart-safe worker handoff tests for a movie and its dependent extras."""

from __future__ import annotations

import json
from types import SimpleNamespace

from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact

FINGERPRINT = "0123456789abcdef"


def _payload(assessment: DiscAssessment, index: int, assignment: dict) -> dict:
    return {
        "mode": "verified-rip-contract",
        "disc_fingerprint": FINGERPRINT,
        "title_index": index,
        "media_context": {
            "routing_assessment": assessment.to_dict(),
            "routing_assessment_digest": assessment.digest,
            "routing_assessment_revision": assessment.revision,
            "special_feature_assignments": [{"title_index": index, **assignment}],
        },
    }


def _prepare(tmp_path, monkeypatch, *, invalid_extra: int | None = None):
    database = tmp_path / "queue.sqlite3"
    store = PipelineQueueStore(database)
    assessment = store.routing_append(
        DiscAssessment(
            FINGERPRINT,
            (0, 2, 3),
            user_hint="movie",
            evidence=(
                TitleEvidence(0, "content", "supported", "movie"),
                TitleEvidence(2, "content", "supported", "extra"),
                TitleEvidence(3, "content", "supported", "extra"),
            ),
        ),
        expected_revision=0,
    )
    assignments = {
        0: {
            "classification": "matched-feature",
            "media_kind": "movie",
            "matched_title": "Example Movie",
            "provisional_match": False,
            "identity_verification_status": "exact_verified",
            "tmdb_movie_id": 123,
            "identification_method": "movie-opensubtitles",
        },
        2: {
            "classification": "matched-feature",
            "media_kind": "extra",
            "matched_title": "Interview",
            "match_summary": "Evidence supports this descriptive bonus identity.",
            "gemini_confidence": 0.82,
            "provisional_match": True,
            "identity_verification_status": "descriptive_pending",
            "fallback_name_policy": "none",
            "jellyfin_folder": "other",
        },
        3: {
            "classification": "matched-feature",
            "media_kind": "extra",
            "matched_title": "interview",
            "match_summary": "Evidence supports this descriptive bonus identity.",
            "gemini_confidence": 0.81,
            "provisional_match": True,
            "identity_verification_status": "descriptive_pending",
            "fallback_name_policy": "none",
            "jellyfin_folder": "other",
        },
    }
    if invalid_extra is not None:
        assignments[invalid_extra]["match_summary"] = ""
    for index in (0, 2, 3):
        media_id = f"disc-01-title-{index:03d}"
        path = tmp_path / f"title-{index}.json"
        path.write_text(
            json.dumps(_payload(assessment, index, assignments[index])),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", path))
        if index:
            store.hold_for_review(
                media_id, "provisional_content_identity_review_required"
            )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(downstream_processing_enabled=True)
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path / "contracts",
    )
    return database, store


def test_worker_accepts_duplicate_named_extras_and_survives_restart(
    tmp_path, monkeypatch
):
    database, store = _prepare(tmp_path, monkeypatch)
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )

    assert worker._apply_movie_extra_dependents() is True
    assert worker._apply_movie_extra_dependents() is False

    restarted = PipelineQueueStore(database)
    names = []
    for index in (2, 3):
        item = restarted.get(f"disc-01-title-{index:03d}")
        assert item.state == "queued"
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
        assignment = payload["media_context"]["special_feature_assignments"][0]
        assert assignment["identity_verification_status"] == "descriptive_accepted"
        assert assignment["related_tmdb_movie_id"] == 123
        names.append(assignment["matched_title"])
    assert names == ["Interview - Title 002", "interview - Title 003"]


def test_worker_is_pause_and_stop_safe(tmp_path, monkeypatch):
    _database, store = _prepare(tmp_path, monkeypatch)
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )

    store.set_paused(True)
    assert worker._apply_movie_extra_dependents() is False
    assert store.get("disc-01-title-002").state == "review_required"
    store.set_paused(False)
    worker._stop.set()
    assert worker._apply_movie_extra_dependents() is False
    assert store.get("disc-01-title-002").state == "review_required"


def test_invalid_extra_does_not_block_valid_sibling(tmp_path, monkeypatch):
    _database, store = _prepare(tmp_path, monkeypatch, invalid_extra=3)
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )

    assert worker._apply_movie_extra_dependents() is True
    assert store.get("disc-01-title-002").state == "queued"
    invalid = store.get("disc-01-title-003")
    assert invalid.state == "review_required"
    assert invalid.review_code == "descriptive_extra_identity_review_required"


def test_worker_recovers_exact_main_contract_after_main_advanced(tmp_path, monkeypatch):
    _database, store = _prepare(tmp_path, monkeypatch)
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    main = store.get("disc-01-title-000")
    historic = contracts / "disc-01-title-000.exact.verified-rip.json"
    historic.write_text(
        main.artifact.contract_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    claimed = store.claim_next(allowed_stages=("identify",))
    assert claimed is not None and claimed.media_id == "disc-01-title-000"
    identified = tmp_path / "identified.json"
    identified.write_text(
        json.dumps({
            "mode": "verified-identification-contract",
            "library_relative": "Example Movie/Example Movie.mkv",
        }),
        encoding="utf-8",
    )
    store.complete_stage(
        claimed.media_id, "identify", build_artifact("identify", identified)
    )
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )

    assert worker._apply_movie_extra_dependents() is True
    assert store.get("disc-01-title-002").state == "queued"
    assert store.get("disc-01-title-003").state == "queued"
