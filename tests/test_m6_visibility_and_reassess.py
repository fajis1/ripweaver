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
        )
    assert excinfo.value.status_code == 400

    result = reassess_disc_metadata(
        fingerprint,
        DiscReassessmentRequest(content_hint="movie", confirm_reassessment=True),
        store,
    )
    assert result["status"] == "reassessed"

    latest = store.routing_latest(fingerprint)
    assert latest.user_hint == "movie"
    assert latest.revision == 2
    assert len(latest.evidence) == 1
    assert latest.evidence[0].source == "database"
    assert latest.evidence[0].role == "tv"
