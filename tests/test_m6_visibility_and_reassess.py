import hashlib
import json

import pytest

from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence
from mkv_episode_matcher.pipeline import PipelineArtifact
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, QueuedPipelineItem


def _file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            digest.update(chunk)
    return digest.hexdigest()


def test_pipeline_item_response_includes_assessed_fields(tmp_path):
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")

    fingerprint = "0123456789abcdef"
    assessment = DiscAssessment(
        fingerprint,
        title_indexes=(1, 2),
        user_hint="tv",
        evidence=(TitleEvidence(1, "label", "supported", "tv"),),
    )
    store.routing_save_observation(assessment)

    contract_path = tmp_path / "contract.json"
    payload = {
        "media_context": {
            "routing_assessment": assessment.to_dict(),
            "routing_assessment_digest": assessment.digest,
            "routing_assessment_revision": assessment.revision,
        },
        "title_index": 1,
        "disc_fingerprint": fingerprint,
    }
    contract_path.write_text(json.dumps(payload), encoding="utf-8")

    artifact = PipelineArtifact(
        stage="rip",
        contract_path=contract_path,
        contract_sha256=_file_sha256(contract_path),
        item_count=1,
    )
    item = QueuedPipelineItem(
        media_id="test_media_id_1",
        state="review_required",
        stage="identify",
        artifact=artifact,
        created_at="2026-09-17T00:00:00Z",
        updated_at="2026-09-17T00:00:00Z",
        error_type=None,
        review_code="gemini_descriptive_review_required",
    )

    import mkv_episode_matcher.backend.routers.rip as rip_module
    from mkv_episode_matcher.backend.routers.rip import _pipeline_item_response

    old_get = rip_module.get_pipeline_queue_store
    rip_module.get_pipeline_queue_store = lambda: store
    try:
        data = _pipeline_item_response(item)
    finally:
        rip_module.get_pipeline_queue_store = old_get

    assert data["media_id"] == "test_media_id_1"
    assert data["user_hint"] == "tv"
    assert data["assessed_role"] == "tv"
    assert data["evidence_status"] == "supported"
    assert data["assessed_composition"] == "tv"


def test_pipeline_item_response_separates_role_from_movie_identity(tmp_path):
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    fingerprint = "0123456789abcdef"
    assessment = store.routing_append(
        DiscAssessment(
            fingerprint,
            title_indexes=(0, 2),
            evidence=(
                TitleEvidence(0, "content", "supported", "movie"),
                TitleEvidence(2, "content", "supported", "extra"),
            ),
        ),
        expected_revision=0,
    )
    contract_path = tmp_path / "movie.json"
    contract_path.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": fingerprint,
            "title_index": 0,
            "media_context": {
                "routing_assessment": assessment.to_dict(),
                "routing_assessment_digest": assessment.digest,
                "routing_assessment_revision": assessment.revision,
                "special_feature_assignments": [
                    {
                        "title_index": 0,
                        "media_kind": "movie",
                        "identity_verification_status": "exact_verified",
                        "identification_method": "movie-opensubtitles",
                    }
                ],
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(
        "disc-01-title-000",
        PipelineArtifact(
            stage="rip",
            contract_path=contract_path,
            contract_sha256=_file_sha256(contract_path),
            item_count=1,
        ),
    )

    import mkv_episode_matcher.backend.routers.rip as rip_module

    old_get = rip_module.get_pipeline_queue_store
    rip_module.get_pipeline_queue_store = lambda: store
    try:
        data = rip_module._pipeline_item_response(store.get("disc-01-title-000"))
    finally:
        rip_module.get_pipeline_queue_store = old_get

    assert data["assessed_composition"] == "movie_with_extras"
    assert data["assessed_role"] == "movie"
    assert data["identity_status"] == "exact_verified"
    assert data["identity_method"] == "movie-opensubtitles"
    assert data["identity_review_requires_rerip"] is None


def test_pipeline_item_response_labels_descriptive_extra_hold_without_rerip(tmp_path):
    contract_path = tmp_path / "extra.json"
    contract_path.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0123456789abcdef",
            "title_index": 2,
            "media_context": {
                "special_feature_assignments": [
                    {
                        "title_index": 2,
                        "media_kind": "extra",
                        "identity_verification_status": "descriptive_pending",
                    }
                ]
            },
        }),
        encoding="utf-8",
    )
    item = QueuedPipelineItem(
        media_id="disc-01-title-002",
        state="review_required",
        stage="identify",
        artifact=PipelineArtifact(
            stage="rip",
            contract_path=contract_path,
            contract_sha256=_file_sha256(contract_path),
            item_count=1,
        ),
        created_at="2026-09-23T00:00:00Z",
        updated_at="2026-09-23T00:00:00Z",
        error_type=None,
        review_code="provisional_content_identity_review_required",
    )

    from mkv_episode_matcher.backend.routers.rip import _pipeline_item_response

    data = _pipeline_item_response(item)
    assert data["identity_status"] == "descriptive_pending"
    assert data["identity_review_requires_rerip"] is False


def test_metadata_only_reassessment_updates_hint(tmp_path):
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")

    fingerprint = "0123456789abcdef"
    assessment = DiscAssessment(
        fingerprint,
        title_indexes=(1, 2),
        user_hint="tv",
        evidence=(TitleEvidence(1, "database", "supported", "tv"),),
    )
    store.routing_save_observation(assessment)

    contract_path = tmp_path / "contract.json"
    payload = {
        "media_context": {
            "routing_assessment": assessment.to_dict(),
            "routing_assessment_digest": assessment.digest,
            "routing_assessment_revision": assessment.revision,
        },
        "title_index": 1,
        "disc_fingerprint": fingerprint,
    }
    contract_path.write_text(json.dumps(payload), encoding="utf-8")

    artifact = PipelineArtifact(
        stage="rip",
        contract_path=contract_path,
        contract_sha256=_file_sha256(contract_path),
        item_count=1,
    )
    store.enqueue_verified_rip(
        "test_media_id_1", artifact, review_code="episode_match_review"
    )

    from fastapi import HTTPException

    from mkv_episode_matcher.backend.routers.rip import (
        DiscReassessmentRequest,
        reassess_disc_metadata,
    )

    with pytest.raises(HTTPException) as excinfo:
        reassess_disc_metadata(
            fingerprint,
            DiscReassessmentRequest(content_hint="movie", confirm_reassessment=False),
            store,
            tmp_path / "contracts",
        )
    assert excinfo.value.status_code == 400

    result = reassess_disc_metadata(
        fingerprint,
        DiscReassessmentRequest(content_hint="movie", confirm_reassessment=True),
        store,
        tmp_path / "contracts",
    )
    assert result["status"] == "reassessed"

    latest = store.routing_latest(fingerprint)
    assert latest.user_hint == "movie"
    assert latest.revision == 2
    assert len(latest.evidence) == 1
    assert latest.evidence[0].source == "database"
    assert latest.evidence[0].role == "tv"


def test_metadata_reassessment_imports_saved_provisional_content_roles(tmp_path):
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    fingerprint = "0123456789abcdef"
    initial = store.routing_append(
        DiscAssessment(fingerprint, title_indexes=(0, 2)), expected_revision=0
    )
    for index, role in ((0, "movie"), (2, "extra")):
        path = tmp_path / f"title-{index}.json"
        path.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "disc_fingerprint": fingerprint,
                "title_index": index,
                "media_context": {
                    "routing_assessment": initial.to_dict(),
                    "routing_assessment_digest": initial.digest,
                    "routing_assessment_revision": initial.revision,
                    "special_feature_assignments": [
                        {
                            "title_index": index,
                            "classification": "matched-feature",
                            "media_kind": role,
                            "provisional_match": True,
                        }
                    ],
                },
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(
            f"disc-01-title-{index:03d}",
            PipelineArtifact(
                stage="rip",
                contract_path=path,
                contract_sha256=_file_sha256(path),
                item_count=1,
            ),
            review_code="provisional_content_identity_review_required",
        )

    from mkv_episode_matcher.backend.routers.rip import (
        DiscReassessmentRequest,
        reassess_disc_metadata,
    )

    result = reassess_disc_metadata(
        fingerprint,
        DiscReassessmentRequest(content_hint="movie", confirm_reassessment=True),
        store,
        tmp_path / "contracts",
    )

    assert result["status"] == "reassessed"
    latest = store.routing_latest(fingerprint)
    assert latest is not None
    assert latest.composition == "movie_with_extras"
    assert [(item.title_index, item.role) for item in latest.title_roles()] == [
        (0, "movie"),
        (2, "extra"),
    ]
    for index in (0, 2):
        held = store.get(f"disc-01-title-{index:03d}")
        assert held.state == "review_required"
        rebound = json.loads(held.artifact.contract_path.read_text(encoding="utf-8"))
        assert rebound["media_context"]["routing_assessment_digest"] == latest.digest
