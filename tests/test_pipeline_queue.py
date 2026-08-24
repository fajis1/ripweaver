import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from mkv_episode_matcher.backend.identification_dossier import (
    IdentificationDossierStore,
)
from mkv_episode_matcher.backend.routers.rip import (
    AmbiguityChoiceRequest,
    ManualEpisodeIdentificationRequest,
    PipelineControlRequest,
    _delete_exact_queued_rip,
    _disc_range_elimination_candidate,
    _exact_queued_rip_source,
    _pipeline_identification_attempts,
    apply_manual_episode_identification,
    restart_placeholder_identification,
)
from mkv_episode_matcher.disc.rip_manifest import MediaContext
from mkv_episode_matcher.disc.ripper import RipJob, RipResult, resolve_final_output
from mkv_episode_matcher.media.handbrake import HandBrakeError
from mkv_episode_matcher.pipeline_adapters import IdentifyStageAdapter
from mkv_episode_matcher.pipeline_queue import (
    DOWNSTREAM_STAGES,
    DownstreamDispatcher,
    PipelineQueueError,
    PipelineQueueStore,
    PipelineReviewRequiredError,
    _safe_adapter_failure_code,
    build_artifact,
    enqueue_verified_rip_results,
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Requested audio language was not found", "HandBrakeAudioLanguageMissing"),
        ("Source audio inspection returned no usable tracks", "HandBrakeNoUsableAudio"),
        ("unclassified safe preflight", "HandBrakePreflightFailed"),
    ],
)
def test_handbrake_failures_are_reduced_to_safe_actionable_codes(message, expected):
    assert _safe_adapter_failure_code(HandBrakeError(message)) == expected


def _artifact(tmp_path, media_id, stage):
    path = tmp_path / f"{media_id}-{stage}.json"
    path.write_text(
        json.dumps({"media_id": media_id, "stage": stage}),
        encoding="utf-8",
    )
    return build_artifact(stage, path)


def test_identification_outcome_is_remembered_by_disc_title(tmp_path):
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    rip_contract = tmp_path / "verified-rip.json"
    rip_contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0123456789abcdef",
            "title_index": 2,
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip("disc-01-title-002", build_artifact("rip", rip_contract))
    store.claim_next()
    identified = tmp_path / "identified.json"
    identified.write_text(
        json.dumps({
            "mode": "identified-episode-contract",
            "episode_id": "S01E03",
            "library_relative": "Dragons/Season 01/Dragons - S01E03 - Animal House.mkv",
            "identification_order": ["tv-opensubtitles-two-window"],
            "assignment_evidence_source": "opensubtitles-two-window",
            "identification_policy_version": 4,
        }),
        encoding="utf-8",
    )

    store.complete_stage(
        "disc-01-title-002", "identify", build_artifact("identify", identified)
    )

    assert store.title_history("0123456789abcdef")[2] == {
        "outcome_name": "Dragons - S01E03 - Animal House.mkv",
        "library_relative": "Dragons/Season 01/Dragons - S01E03 - Animal House.mkv",
        "episode_id": "S01E03",
    }
    assert store.assigned_series_episodes("Dragons") == frozenset({(1, 3)})
    assert store.assigned_series_episodes("Another Series") == frozenset()
    catalogue_history = store.catalogue_title_history("0123456789abcdef")[2]
    assert catalogue_history["assignment_evidence_source"] == (
        "opensubtitles-two-window"
    )
    assert catalogue_history["identification_policy_version"] == 4
    coverage = PipelineQueueStore(store.database_path).learned_series_coverage(
        "Dragons"
    )
    assert coverage == {
        "series_name": "Dragons",
        "disc_count": 1,
        "episode_count": 1,
        "discs": [
            {
                "disc_fingerprint": "0123456789abcdef",
                "assigned_title_count": 1,
                "episode_count": 1,
                "other_title_count": 0,
                "seasons": [1],
                "episode_ids": ["S01E03"],
                "assignments": [{"title_index": 2, "episode_id": "S01E03"}],
            }
        ],
    }

    restarted = store.restart_identification(
        "disc-01-title-002",
        expected_disc_fingerprint="0123456789abcdef",
        expected_title_index=2,
    )
    assert restarted.state == "queued"
    assert restarted.stage == "identify"
    assert restarted.artifact.contract_path == rip_contract.resolve()
    assert store.list_events("disc-01-title-002")[-1].event_type == (
        "existing_rip_identification_restarted"
    )
    with pytest.raises(PipelineQueueError, match="different disc inventory"):
        store.restart_identification(
            "disc-01-title-002",
            expected_disc_fingerprint="ffffffffffffffff",
            expected_title_index=2,
        )


def _store(tmp_path):
    return PipelineQueueStore(tmp_path / "private-pipeline.sqlite3")


def test_verified_rips_advance_through_one_global_downstream_queue(tmp_path):
    store = _store(tmp_path)
    store.enqueue_verified_rip("media-1", _artifact(tmp_path, "media-1", "rip"))
    store.enqueue_verified_rip("media-2", _artifact(tmp_path, "media-2", "rip"))
    calls = []

    def adapter(item):
        calls.append((item.media_id, item.stage))
        return _artifact(tmp_path, item.media_id, item.stage)

    dispatcher = DownstreamDispatcher(
        store,
        dict.fromkeys(DOWNSTREAM_STAGES, adapter),
    )
    results = dispatcher.drain()

    assert len(results) == 6
    assert calls == [
        ("media-1", "identify"),
        ("media-2", "identify"),
        ("media-1", "transcode"),
        ("media-2", "transcode"),
        ("media-1", "organize"),
        ("media-2", "organize"),
    ]
    assert [item.state for item in store.list_items()] == ["completed", "completed"]


def test_stage_limited_dispatcher_runs_identification_without_transcode(tmp_path):
    store = _store(tmp_path)
    store.enqueue_verified_rip("media-1", _artifact(tmp_path, "media-1", "rip"))
    calls = []

    def adapter(item):
        calls.append((item.media_id, item.stage))
        return _artifact(tmp_path, item.media_id, item.stage)

    dispatcher = DownstreamDispatcher(
        store,
        dict.fromkeys(DOWNSTREAM_STAGES, adapter),
    )

    identified = dispatcher.run_one(allowed_stages=("identify",))
    held = dispatcher.run_one(allowed_stages=("identify",))

    assert identified is not None
    assert identified.stage == "transcode"
    assert identified.state == "queued"
    assert held is None
    assert calls == [("media-1", "identify")]


def test_dispatcher_claims_only_exact_authorized_media_ids(tmp_path):
    store = _store(tmp_path)
    for media_id in ("media-1", "media-2"):
        store.enqueue_verified_rip(media_id, _artifact(tmp_path, media_id, "rip"))
    calls = []

    def adapter(item):
        calls.append(item.media_id)
        return _artifact(tmp_path, item.media_id, item.stage)

    dispatcher = DownstreamDispatcher(store, dict.fromkeys(DOWNSTREAM_STAGES, adapter))
    dispatcher.run_one(allowed_stages=("identify",), allowed_media_ids=("media-2",))

    assert calls == ["media-2"]
    assert store.get("media-1").stage == "identify"
    assert store.get("media-1").state == "queued"


def test_atomic_claim_allows_only_one_worker_for_the_same_stage(tmp_path):
    store = _store(tmp_path)
    for index in range(3):
        media_id = f"media-{index}"
        store.enqueue_verified_rip(media_id, _artifact(tmp_path, media_id, "rip"))
    barrier = threading.Barrier(3)
    claims = []

    def claim():
        barrier.wait()
        claims.append(store.claim_next())

    threads = [threading.Thread(target=claim) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(item is not None for item in claims) == 1
    assert sum(item.state == "running" for item in store.list_items()) == 1


def test_claim_allows_organization_to_overlap_unrelated_transcode(tmp_path):
    store = _store(tmp_path)
    for media_id in ("transcoding", "organizing"):
        store.enqueue_verified_rip(media_id, _artifact(tmp_path, media_id, "rip"))

    identifying = store.claim_next(
        allowed_stages=("identify",), allowed_media_ids=("transcoding",)
    )
    store.complete_stage(
        identifying.media_id,
        "identify",
        _artifact(tmp_path, identifying.media_id, "identify"),
    )
    identifying = store.claim_next(
        allowed_stages=("identify",), allowed_media_ids=("organizing",)
    )
    store.complete_stage(
        identifying.media_id,
        "identify",
        _artifact(tmp_path, identifying.media_id, "identify"),
    )
    transcoding_for_organize = store.claim_next(
        allowed_stages=("transcode",), allowed_media_ids=("organizing",)
    )
    store.complete_stage(
        transcoding_for_organize.media_id,
        "transcode",
        _artifact(tmp_path, transcoding_for_organize.media_id, "transcode"),
    )

    active_transcode = store.claim_next(
        allowed_stages=("transcode",), allowed_media_ids=("transcoding",)
    )
    active_organize = store.claim_next(allowed_stages=("organize",))

    assert active_transcode.media_id == "transcoding"
    assert active_organize.media_id == "organizing"
    assert store.claim_next(allowed_stages=("transcode",)) is None
    assert store.claim_next(allowed_stages=("organize",)) is None


def test_review_and_failure_isolate_items_and_retry_same_stage(tmp_path):
    store = _store(tmp_path)
    store.enqueue_verified_rip(
        "needs-review", _artifact(tmp_path, "needs-review", "rip")
    )
    store.enqueue_verified_rip("healthy", _artifact(tmp_path, "healthy", "rip"))

    def identify(item):
        if item.media_id == "needs-review":
            raise PipelineReviewRequiredError("weak_episode_match")
        return _artifact(tmp_path, item.media_id, "identify")

    dispatcher = DownstreamDispatcher(
        store,
        {
            "identify": identify,
            "transcode": lambda item: _artifact(tmp_path, item.media_id, "transcode"),
            "organize": lambda item: _artifact(tmp_path, item.media_id, "organize"),
        },
    )

    reviewed = dispatcher.run_one()
    healthy = dispatcher.run_one()
    assert reviewed.state == "review_required"
    assert reviewed.review_code == "weak_episode_match"
    assert healthy.media_id == "healthy"
    assert healthy.stage == "transcode"

    retried = store.retry("needs-review")
    assert retried.state == "queued"
    assert retried.stage == "identify"


def test_drain_continues_past_review_and_failure_items(tmp_path):
    store = _store(tmp_path)
    for media_id in ("review", "failure", "healthy"):
        store.enqueue_verified_rip(media_id, _artifact(tmp_path, media_id, "rip"))

    def identify(item):
        if item.media_id == "review":
            raise PipelineReviewRequiredError("weak_episode_match")
        if item.media_id == "failure":
            raise RuntimeError("synthetic adapter failure")
        return _artifact(tmp_path, item.media_id, "identify")

    dispatcher = DownstreamDispatcher(
        store,
        {
            "identify": identify,
            "transcode": lambda item: _artifact(tmp_path, item.media_id, "transcode"),
            "organize": lambda item: _artifact(tmp_path, item.media_id, "organize"),
        },
    )
    results = dispatcher.drain()

    assert [(item.media_id, item.state) for item in results[:3]] == [
        ("review", "review_required"),
        ("failure", "failed"),
        ("healthy", "queued"),
    ]
    assert store.get("healthy").state == "completed"


def test_restart_requeues_interrupted_stage_and_preserves_contract(tmp_path):
    store = _store(tmp_path)
    original = _artifact(tmp_path, "media-1", "rip")
    store.enqueue_verified_rip("media-1", original)
    claimed = store.claim_next()
    assert claimed.state == "running"

    reopened = PipelineQueueStore(store.database_path)
    assert reopened.reconcile_incomplete() == ("media-1",)
    recovered = reopened.get("media-1")
    assert recovered.state == "queued"
    assert recovered.artifact == original


def test_restart_restores_interrupted_all_season_analysis_to_review(tmp_path):
    store = _store(tmp_path)
    original = _artifact(tmp_path, "media-1", "rip")
    store.enqueue_verified_rip("media-1", original)
    store.claim_next()
    store.require_review("media-1", "unmatched_disc_analysis_required")
    store.choose_review_path("media-1", "all_season_analysis_running")

    reopened = PipelineQueueStore(store.database_path)
    assert reopened.reconcile_incomplete() == ("media-1",)
    recovered = reopened.get("media-1")
    assert recovered.state == "review_required"
    assert recovered.review_code == "all_season_analysis_failed"


def test_episode_match_review_can_enter_all_season_analysis(tmp_path):
    store = _store(tmp_path)
    original = _artifact(tmp_path, "media-1", "rip")
    store.enqueue_verified_rip("media-1", original)
    store.hold_for_review("media-1", "episode_match_review")

    selected = store.choose_review_path("media-1", "all_season_analysis_running")

    assert selected.state == "review_required"
    assert selected.stage == "identify"
    assert selected.review_code == "all_season_analysis_running"


def test_changed_contract_and_path_leaking_events_are_refused(tmp_path):
    store = _store(tmp_path)
    original = _artifact(tmp_path, "media-1", "rip")
    store.enqueue_verified_rip("media-1", original)
    original.contract_path.write_text("changed", encoding="utf-8")

    with pytest.raises(PipelineQueueError, match="missing or changed"):
        store.enqueue_verified_rip("media-1", original)

    serialized = json.dumps([event.details for event in store.list_events()])
    assert str(tmp_path) not in serialized


def test_pause_prevents_new_claim_without_changing_queued_items(tmp_path):
    store = _store(tmp_path)
    store.enqueue_verified_rip("media-1", _artifact(tmp_path, "media-1", "rip"))
    store.set_paused(True)
    assert store.claim_next() is None
    assert store.get("media-1").state == "queued"
    store.set_paused(False)
    assert store.claim_next().state == "running"


def test_restart_reconciliation_clears_persisted_global_pause(tmp_path):
    store = _store(tmp_path)
    store.enqueue_verified_rip("media-1", _artifact(tmp_path, "media-1", "rip"))
    store.set_paused(True)

    store.reconcile_incomplete()
    assert store.is_paused() is True

    store.reconcile_incomplete(clear_pause=True)
    assert store.is_paused() is False
    assert store.claim_next().media_id == "media-1"


def test_ambiguity_choice_is_durable_and_does_not_requeue_item(tmp_path):
    store = _store(tmp_path)
    store.enqueue_verified_rip("media-1", _artifact(tmp_path, "media-1", "rip"))
    claimed = store.claim_next()
    assert claimed is not None
    store.require_review("media-1", "special_feature_evidence_required")

    selected = store.choose_review_path("media-1", "gemini_evidence_required")

    assert selected.state == "review_required"
    assert selected.stage == "identify"
    assert selected.review_code == "gemini_evidence_required"
    assert store.claim_next() is None
    reopened = PipelineQueueStore(store.database_path).get("media-1")
    assert reopened.review_code == "gemini_evidence_required"
    events = store.list_events("media-1")
    assert events[-1].event_type == "ambiguity_resolution_selected"
    assert events[-1].details == {"review_code": "gemini_evidence_required"}


def test_gemini_ambiguity_choice_retains_external_confirmation():
    request = AmbiguityChoiceRequest(choice="gemini", confirm_external_fallback=True)

    assert request.confirm_external_fallback is True


def test_gemini_descriptive_uncertainty_remains_a_durable_review(tmp_path):
    store = _store(tmp_path)
    store.enqueue_verified_rip("media-1", _artifact(tmp_path, "media-1", "rip"))
    store.claim_next()
    store.require_review("media-1", "gemini_evidence_required")
    store.choose_review_path("media-1", "gemini_analysis_running")

    selected = store.choose_review_path("media-1", "gemini_descriptive_review_required")

    assert selected.state == "review_required"
    assert selected.review_code == "gemini_descriptive_review_required"
    assert PipelineQueueStore(store.database_path).get("media-1").review_code == (
        "gemini_descriptive_review_required"
    )


def test_visual_content_classification_becomes_a_durable_review(tmp_path):
    store = _store(tmp_path)
    store.enqueue_verified_rip("media-1", _artifact(tmp_path, "media-1", "rip"))
    store.claim_next()
    store.require_review("media-1", "gemini_evidence_required")
    store.choose_review_path("media-1", "gemini_analysis_running")
    store.record_silent_video_review("media-1", "likely_warning_screen")

    selected = store.choose_review_path("media-1", "visual_content_review_required")

    assert selected.state == "review_required"
    assert selected.review_code == "visual_content_review_required"
    assert PipelineQueueStore(store.database_path).silent_video_review_flags() == {
        "media-1": "likely_warning_screen"
    }


def test_dismiss_held_items_preserves_artifacts_and_history(tmp_path):
    store = _store(tmp_path)
    artifacts = {}
    for media_id in ("held-1", "held-2"):
        artifacts[media_id] = _artifact(tmp_path, media_id, "rip")
        store.enqueue_verified_rip(media_id, artifacts[media_id])
        store.claim_next()
        store.require_review(media_id, "unmatched_disc_analysis_required")

    dismissed = store.dismiss_items(("held-1", "held-2"))

    assert [item.state for item in dismissed] == ["discarded", "discarded"]
    assert all(artifact.contract_path.exists() for artifact in artifacts.values())
    assert all(
        store.list_events(media_id)[-1].event_type == "pipeline_item_dismissed"
        for media_id in artifacts
    )
    assert all(
        store.list_events(media_id)[-1].details == {"media_changed": False}
        for media_id in artifacts
    )


def test_dismiss_items_is_atomic_when_selection_contains_active_work(tmp_path):
    store = _store(tmp_path)
    for media_id in ("held", "queued"):
        store.enqueue_verified_rip(media_id, _artifact(tmp_path, media_id, "rip"))
    store.claim_next()
    store.require_review("held", "unmatched_disc_analysis_required")

    with pytest.raises(PipelineQueueError, match="Only failed or review-held"):
        store.dismiss_items(("held", "queued"))
    assert store.get("held").state == "review_required"
    assert store.get("queued").state == "queued"


def test_cancel_queued_items_preserves_artifacts_and_refuses_running(tmp_path):
    store = _store(tmp_path)
    artifact = _artifact(tmp_path, "queued-1", "rip")
    store.enqueue_verified_rip("queued-1", artifact)

    cancelled = store.cancel_queued_items(("queued-1",))

    assert cancelled[0].state == "discarded"
    assert artifact.contract_path.exists()
    assert (
        store.list_events("queued-1")[-1].event_type == "queued_pipeline_item_cancelled"
    )

    store.enqueue_verified_rip("running-1", _artifact(tmp_path, "running-1", "rip"))
    store.claim_next()
    with pytest.raises(PipelineQueueError, match="not-running"):
        store.cancel_queued_items(("running-1",))


def test_delete_queued_item_media_runs_exact_callback_and_records_change(tmp_path):
    store = _store(tmp_path)
    artifact = _artifact(tmp_path, "queued-delete", "rip")
    store.enqueue_verified_rip("queued-delete", artifact)
    deleted = []

    result = store.delete_queued_item_media(
        "queued-delete", lambda item: deleted.append(item.media_id)
    )

    assert deleted == ["queued-delete"]
    assert result.state == "discarded"
    event = store.list_events("queued-delete")[-1]
    assert event.event_type == "pipeline_staged_media_deleted"
    assert event.details == {"deleted": "staged_source", "media_changed": True}


def test_likely_removable_delete_teaches_reversible_future_rip_skip(tmp_path):
    store = _store(tmp_path)
    contract = tmp_path / "warning-rip.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0123456789abcdef",
            "title_index": 3,
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip("warning-title", build_artifact("rip", contract))
    store.record_silent_video_review("warning-title", "likely_warning_screen")

    result = store.delete_queued_item_media(
        "warning-title",
        lambda _item: None,
        remember_future_skip=True,
    )

    assert result.state == "discarded"
    assert store.title_dispositions("0123456789abcdef") == {
        3: {"disposition": "skip", "reason": "likely_warning_screen"}
    }
    assert store.list_events("warning-title")[-1].details == {
        "deleted": "staged_source",
        "future_rip_disposition": "skip",
        "media_changed": True,
        "reason": "likely_warning_screen",
    }
    assert store.restore_title_disposition("0123456789abcdef", 3) is True
    assert store.title_dispositions("0123456789abcdef") == {}


def test_repeated_read_failure_teaches_reversible_future_rip_skip(tmp_path):
    store = _store(tmp_path)

    saved = store.remember_title_skip(
        "0123456789abcdef", 16, reason="repeated_read_failure"
    )

    assert saved == {
        "disc_fingerprint": "0123456789abcdef",
        "title_index": 16,
        "disposition": "skip",
        "reason": "repeated_read_failure",
    }
    assert store.title_dispositions("0123456789abcdef") == {
        16: {"disposition": "skip", "reason": "repeated_read_failure"}
    }
    assert store.list_title_dispositions() == (saved,)
    assert store.restore_title_disposition("0123456789abcdef", 16) is True
    assert store.list_title_dispositions() == ()


def test_future_rip_skip_rejects_an_unreviewed_reason(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(PipelineQueueError, match="reason is invalid"):
        store.remember_title_skip(
            "0123456789abcdef", 16, reason="filename_looked_suspicious"
        )

    assert store.list_title_dispositions() == ()


def test_future_rip_skip_requires_likely_removable_ocr_result(tmp_path):
    store = _store(tmp_path)
    contract = tmp_path / "ordinary-rip.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0123456789abcdef",
            "title_index": 4,
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip("ordinary-title", build_artifact("rip", contract))
    store.record_silent_video_review("ordinary-title", "text_detected")

    with pytest.raises(PipelineQueueError, match="likely-removable OCR"):
        store.delete_queued_item_media(
            "ordinary-title",
            lambda _item: None,
            remember_future_skip=True,
        )

    assert store.get("ordinary-title").state == "queued"
    assert store.title_dispositions("0123456789abcdef") == {}


def test_delete_queued_item_media_accepts_source_already_removed_by_cleanup(tmp_path):
    rip_root = tmp_path / "rip-root"
    rip_root.mkdir()
    source = rip_root / "disc-01-0123456789abcdef-title-000.mkv"
    source.write_bytes(b"verified")
    contract = tmp_path / "queued-already-cleaned-rip.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
        }),
        encoding="utf-8",
    )
    artifact = build_artifact("rip", contract)
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip("queued-already-cleaned", artifact)
    source.unlink()

    result = store.delete_queued_item_media(
        "queued-already-cleaned",
        lambda item: _delete_exact_queued_rip(item, rip_root),
    )

    assert result.state == "discarded"
    assert store.list_events("queued-already-cleaned")[-1].event_type == (
        "pipeline_staged_media_deleted"
    )


def test_delete_review_held_item_media_runs_exact_callback(tmp_path):
    store = _store(tmp_path)
    store.enqueue_verified_rip("held-delete", _artifact(tmp_path, "held-delete", "rip"))
    store.claim_next()
    store.require_review("held-delete", "gemini_provider_failed")
    deleted = []

    result = store.delete_queued_item_media(
        "held-delete", lambda item: deleted.append(item.media_id)
    )

    assert deleted == ["held-delete"]
    assert result.state == "discarded"
    event = store.list_events("held-delete")[-1]
    assert event.event_type == "pipeline_staged_media_deleted"
    assert event.details == {"deleted": "staged_source", "media_changed": True}


def test_delete_discarded_item_media_after_preserving_it(tmp_path):
    store = _store(tmp_path)
    store.enqueue_verified_rip(
        "discarded-delete", _artifact(tmp_path, "discarded-delete", "rip")
    )
    store.claim_next()
    store.require_review("discarded-delete", "gemini_provider_failed")
    store.dismiss_items(("discarded-delete",))
    deleted = []

    result = store.delete_queued_item_media(
        "discarded-delete", lambda item: deleted.append(item.media_id)
    )

    assert deleted == ["discarded-delete"]
    assert result.state == "discarded"
    assert store.list_events("discarded-delete")[-1].event_type == (
        "pipeline_staged_media_deleted"
    )


def test_forget_disc_records_removes_only_metadata_and_preserves_media(tmp_path):
    store = _store(tmp_path)
    sources = {}
    for media_id, fingerprint in (
        ("forgotten-disc-item", "0123456789abcdef"),
        ("other-disc-item", "fedcba9876543210"),
    ):
        source = tmp_path / f"{media_id}.mkv"
        source.write_bytes(media_id.encode())
        contract = tmp_path / f"{media_id}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "source_path": str(source),
                "source_size_bytes": source.stat().st_size,
                "disc_fingerprint": fingerprint,
                "title_index": 0,
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        sources[media_id] = source

    record_count, history_count = store.forget_disc_records("0123456789abcdef")

    assert (record_count, history_count) == (1, 0)
    assert {item.media_id for item in store.list_items()} == {"other-disc-item"}
    assert all(source.is_file() for source in sources.values())


def test_review_held_verified_rip_source_is_eligible_for_exact_deletion(tmp_path):
    rip_root = tmp_path / "rips"
    rip_root.mkdir()
    source = rip_root / "held.mkv"
    source.write_bytes(b"verified staged rip")
    contract = tmp_path / "held.verified-rip.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
        }),
        encoding="utf-8",
    )
    store = _store(tmp_path)
    store.enqueue_verified_rip("held-source", build_artifact("rip", contract))
    store.claim_next()
    store.require_review("held-source", "gemini_provider_failed")

    resolved = _exact_queued_rip_source(store.get("held-source"), rip_root)

    assert resolved == source.resolve()


def test_delete_queued_item_media_rolls_back_when_delete_callback_fails(tmp_path):
    store = _store(tmp_path)
    store.enqueue_verified_rip(
        "queued-delete", _artifact(tmp_path, "queued-delete", "rip")
    )

    def fail_delete(_item):
        raise PipelineQueueError("Synthetic delete refusal")

    with pytest.raises(PipelineQueueError, match="Synthetic delete refusal"):
        store.delete_queued_item_media("queued-delete", fail_delete)

    assert store.get("queued-delete").state == "queued"


def test_ambiguity_choice_rejects_unrelated_review_items(tmp_path):
    store = _store(tmp_path)
    store.enqueue_verified_rip("media-1", _artifact(tmp_path, "media-1", "rip"))
    store.claim_next()
    store.require_review("media-1", "missing_series_context")

    with pytest.raises(PipelineQueueError, match="not awaiting"):
        store.choose_review_path("media-1", "gemini_evidence_required")


def test_gemini_assignment_uses_new_contract_and_requeues_identification(tmp_path):
    store = _store(tmp_path)
    original = _artifact(tmp_path, "media-1", "rip")
    store.enqueue_verified_rip("media-1", original)
    store.claim_next()
    store.require_review("media-1", "special_feature_evidence_required")
    store.choose_review_path("media-1", "gemini_evidence_required")
    revised = _artifact(tmp_path, "media-1-gemini", "rip")

    item = store.apply_reviewed_identification_input("media-1", revised)

    assert item.state == "queued"
    assert item.stage == "identify"
    assert item.review_code is None
    assert item.artifact == revised
    assert original.contract_path.exists()


def test_placeholder_identification_can_restart_from_verified_rip(tmp_path):
    store = _store(tmp_path)
    rip = tmp_path / "verified-rip.json"
    rip.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0123456789abcdef",
            "title_index": 0,
        }),
        encoding="utf-8",
    )
    identified = tmp_path / "identified.json"
    identified.write_text(
        json.dumps({
            "mode": "identified-episode-contract",
            "episode_id": "S01E01",
            "library_relative": "Unmatched/Season 01/Unmatched - S01E01 - Episode 1.mkv",
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip("media-1", build_artifact("rip", rip))
    store.claim_next()
    store.complete_stage("media-1", "identify", build_artifact("identify", identified))
    store.claim_next()
    store.require_review("media-1", "placeholder_identification_required")

    response = restart_placeholder_identification(
        "media-1", PipelineControlRequest(confirm_control=True), store
    )

    restarted = store.get("media-1")
    assert response["state"] == "queued"
    assert restarted.stage == "identify"
    assert restarted.artifact.contract_path == rip.resolve()


def test_encoded_placeholder_can_restart_from_verified_rip(tmp_path):
    store = _store(tmp_path)
    rip = tmp_path / "verified-rip.json"
    rip.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0123456789abcdef",
            "title_index": 0,
        }),
        encoding="utf-8",
    )
    identified = _artifact(tmp_path, "identified", "identify")
    encoded = _artifact(tmp_path, "encoded", "transcode")
    store.enqueue_verified_rip("media-1", build_artifact("rip", rip))
    store.claim_next()
    store.complete_stage("media-1", "identify", identified)
    store.claim_next()
    store.complete_stage("media-1", "transcode", encoded)
    store.claim_next()
    store.require_review("media-1", "placeholder_identification_required")

    response = restart_placeholder_identification(
        "media-1", PipelineControlRequest(confirm_control=True), store
    )

    restarted = store.get("media-1")
    assert response["state"] == "queued"
    assert restarted.stage == "identify"
    assert restarted.artifact.contract_path == rip.resolve()


def test_disc_range_elimination_accepts_only_remaining_provider_candidate():
    fingerprint = "0123456789abcdef"
    media_ids = {
        title_index: (
            f"The-Office-S4-D2--disc-01-{fingerprint}-title-{title_index:03d}"
        )
        for title_index in range(1, 8)
    }
    history = {
        title_index: {
            "episode_id": f"S04E{title_index + 6:02d}",
            "series_name": "The Office",
        }
        for title_index in range(1, 7)
    }
    attempts = {
        media_ids[title_index]: (
            {
                "branch": "tv-opensubtitles",
                "disposition": "matched",
                "summary": {
                    "candidate_episode_id": f"S04E{title_index + 6:02d}",
                    "best_score": 0.82,
                },
            },
        )
        for title_index in range(1, 7)
    }
    attempts[media_ids[7]] = (
        {
            "branch": "tv-opensubtitles",
            "disposition": "review",
            "summary": {
                "candidate_episode_id": "S04E13",
                "candidate_episode_title": "Job Fair",
                "candidate_series_name": "The Office",
                "best_score": 0.55,
            },
        },
    )
    store = SimpleNamespace(
        catalogue_title_history=lambda _fingerprint: history,
        list_items=lambda: tuple(
            SimpleNamespace(media_id=media_id) for media_id in media_ids.values()
        ),
        expected_title_indexes_for_disc=lambda _fingerprint: tuple(range(1, 8)),
        assigned_series_episodes=lambda _series: frozenset(
            (4, episode) for episode in range(1, 13)
        ),
    )
    dossier = SimpleNamespace(safe_attempts=lambda media_id: attempts[media_id])

    assert _disc_range_elimination_candidate(
        media_ids[7], "S04E13", store, dossier
    ) == ("The Office", 4, 13, "Job Fair")


def test_episode_correction_preserves_encode_and_updates_history(tmp_path):
    store = _store(tmp_path)
    media_id = "The-Office-S4-D2--disc-01-0123456789abcdef-title-007"
    rip = tmp_path / "rip.json"
    rip.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0123456789abcdef",
            "title_index": 7,
        }),
        encoding="utf-8",
    )
    identify = tmp_path / "identify.json"
    identify.write_text(
        json.dumps({
            "mode": "identified-episode-contract",
            "episode_id": "S04E04",
            "library_relative": (
                "The Office/Season 04/The Office - S04E04 - Money.mkv"
            ),
            "identification_order": ["tv-gemini"],
        }),
        encoding="utf-8",
    )
    encoded = tmp_path / "encoded.mkv"
    encoded.write_bytes(b"verified-encode")
    transcode_payload = {
        "mode": "verified-transcode-contract",
        "media_id": media_id,
        "encoded_path": str(encoded),
        "encoded_size_bytes": encoded.stat().st_size,
        "encoded_height": 1080,
        "encoded_width": 1920,
        "episode_id": "S04E04",
        "library_relative": "The Office/Season 04/The Office - S04E04 - Money.mkv",
    }
    transcode = tmp_path / "transcode.json"
    transcode.write_text(json.dumps(transcode_payload), encoding="utf-8")
    store.enqueue_verified_rip(media_id, build_artifact("rip", rip))
    store.claim_next()
    store.complete_stage(media_id, "identify", build_artifact("identify", identify))
    store.claim_next()
    store.complete_stage(media_id, "transcode", build_artifact("transcode", transcode))
    store.claim_next()
    store.require_review(media_id, "library_collision")
    held = store.get(media_id)

    corrected_payload = dict(transcode_payload)
    corrected_payload.update(
        episode_id="S04E13",
        display_title="The Office - S04E13 - Job Fair",
        library_relative=("The Office/Season 04/The Office - S04E13 - Job Fair.mkv"),
        identification_order=["disc-range-elimination-reviewed"],
        assignment_evidence_source="disc_range_elimination",
        identification_policy_version=4,
        user_reviewed_name=True,
    )
    corrected = tmp_path / "corrected.json"
    corrected.write_text(json.dumps(corrected_payload), encoding="utf-8")

    updated = store.correct_held_episode_identification(
        media_id,
        build_artifact("transcode", corrected),
        expected_artifact_sha256=held.artifact.contract_sha256,
    )

    assert updated.state == "review_required"
    assert updated.stage == "organize"
    assert updated.review_code == "corrected_identification_ready"
    assert encoded.read_bytes() == b"verified-encode"
    history = store.catalogue_title_history("0123456789abcdef")[7]
    assert history["episode_id"] == "S04E13"
    assert history["assignment_evidence_source"] == "disc_range_elimination"
    assert history["identification_policy_version"] == 4
    assert store.list_events(media_id)[-1].event_type == (
        "episode_identification_corrected"
    )


@pytest.mark.parametrize(
    "review_code",
    ["all_season_analysis_failed", "episode_match_review", "gemini_analysis_failed"],
)
def test_manual_playback_review_teaches_disc_title_history(tmp_path, review_code):
    store = _store(tmp_path)
    source = tmp_path / "staged-rip.mkv"
    source.write_bytes(b"verified-rip")
    rip = tmp_path / "verified-rip.json"
    rip.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "disc_fingerprint": "0123456789abcdef",
            "title_index": 4,
            "media_context": {"series_name": "Unmatched"},
        }),
        encoding="utf-8",
    )
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store.enqueue_verified_rip("media-1", build_artifact("rip", rip))
    store.claim_next()
    store.require_review("media-1", review_code)

    response = apply_manual_episode_identification(
        "media-1",
        ManualEpisodeIdentificationRequest(
            new_name="Faerie Tale Theatre - S03E02 - The Princess and the Pea",
            confirm_identification=True,
        ),
        store,
        contracts,
    )

    assert response["stage"] == "identify"
    assert response["state"] == "queued"
    running = store.claim_next()
    identified = IdentifyStageAdapter(object(), contracts)(running)
    completed = store.complete_stage("media-1", "identify", identified)
    assert completed.stage == "transcode"
    assert store.title_history("0123456789abcdef")[4] == {
        "outcome_name": ("Faerie Tale Theatre - S03E02 - The Princess and the Pea.mkv"),
        "library_relative": (
            "Faerie Tale Theatre/Season 03/"
            "Faerie Tale Theatre - S03E02 - The Princess and the Pea.mkv"
        ),
        "episode_id": "S03E02",
    }


def test_legacy_episode_review_surfaces_best_audit_candidate(tmp_path):
    contract = tmp_path / "rip.json"
    contract.write_text(
        json.dumps({"media_context": {"series_name": "The Office"}}),
        encoding="utf-8",
    )
    dossier = IdentificationDossierStore(tmp_path / "evidence")
    dossier.record_initial_match_trace(
        "media-1",
        {
            "schema_version": 1,
            "engine_decision": "review",
            "engine_reason": "no_candidate_met_segment_threshold",
            "segments": [
                {
                    "segment_index": 0,
                    "status": "below_threshold",
                    "candidate_evaluations": [
                        {
                            "rank": 1,
                            "candidate_episode_id": "S06E12",
                            "candidate_episode_title": "Scott's Tots",
                            "score": 0.50348,
                            "segment_threshold": 0.6,
                            "qualified": False,
                        }
                    ],
                }
            ],
        },
    )
    item = SimpleNamespace(
        media_id="media-1",
        review_code="episode_match_review",
        artifact=SimpleNamespace(contract_path=contract),
    )

    attempts = _pipeline_identification_attempts(item, dossier)

    assert attempts[0]["summary"]["candidate_series_name"] == "The Office"
    assert attempts[0]["summary"]["candidate_episode_id"] == "S06E12"
    assert attempts[0]["summary"]["candidate_episode_title"] == "Scott's Tots"
    assert attempts[0]["summary"]["best_score"] == pytest.approx(0.50348)


def test_reviewed_catalogue_candidate_remains_server_assisted_history(tmp_path):
    store = _store(tmp_path)
    source = tmp_path / "staged-rip.mkv"
    source.write_bytes(b"verified-rip")
    rip = tmp_path / "verified-rip.json"
    rip.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "disc_fingerprint": "0123456789abcdef",
            "title_index": 4,
            "media_context": {
                "series_name": "Faerie Tale Theatre",
                "catalogue_help_assignments": [
                    {
                        "title_index": 4,
                        "season": 3,
                        "episode": 2,
                        "title": "The Princess and the Pea",
                        "identification_source": "ripweaver-catalogue-help",
                    }
                ],
            },
        }),
        encoding="utf-8",
    )
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store.enqueue_verified_rip("media-1", build_artifact("rip", rip))
    store.claim_next()
    store.require_review("media-1", "catalogue_candidate_help_available")

    response = apply_manual_episode_identification(
        "media-1",
        ManualEpisodeIdentificationRequest(
            new_name="Faerie Tale Theatre - S03E02 - The Princess and the Pea",
            evidence_source="catalogue_candidate",
            confirm_identification=True,
        ),
        store,
        contracts,
    )

    assert response["state"] == "queued"
    identified = IdentifyStageAdapter(object(), contracts)(store.claim_next())
    payload = json.loads(identified.contract_path.read_text(encoding="utf-8"))
    assert payload["identification_order"] == ["ripweaver-catalogue-help-reviewed"]
    store.complete_stage("media-1", "identify", identified)
    assert (
        store.catalogue_title_history("0123456789abcdef")[4]["match_source"]
        == "server_assisted"
    )


def test_manual_bonus_review_routes_to_tv_series_extras(tmp_path):
    store = _store(tmp_path)
    source = tmp_path / "staged-bonus.mkv"
    source.write_bytes(b"verified-rip")
    rip = tmp_path / "verified-rip.json"
    rip.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "disc_fingerprint": "0123456789abcdef",
            "title_index": 2,
            "media_context": {"series_name": "The Flintstones"},
        }),
        encoding="utf-8",
    )
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store.enqueue_verified_rip("media-1", build_artifact("rip", rip))
    store.claim_next()
    store.require_review("media-1", "all_season_sequence_review_required")

    response = apply_manual_episode_identification(
        "media-1",
        ManualEpisodeIdentificationRequest(
            new_name="Carved in Stone - The Flintstones Phenomenon",
            content_type="bonus",
            confirm_identification=True,
        ),
        store,
        contracts,
    )

    assert response["stage"] == "identify"
    assert response["state"] == "queued"
    running = store.claim_next()
    identified = IdentifyStageAdapter(object(), contracts)(running)
    payload = json.loads(identified.contract_path.read_text(encoding="utf-8"))
    assert payload["episode_id"] is None
    assert payload["library_kind"] == "tv"
    assert payload["library_relative"] == (
        "The Flintstones/Extras/Carved in Stone - The Flintstones Phenomenon.mkv"
    )


def test_verified_rip_result_is_admitted_without_discovering_media(tmp_path):
    store = _store(tmp_path)
    output_root = tmp_path / "media-output"
    contract_root = tmp_path / "private-contracts"
    output_root.mkdir()
    contract_root.mkdir()
    job = RipJob(
        job_id="disc-title000",
        drive_index=0,
        title_index=0,
        relative_output_dir="isolated/title-000",
        output_basename="disc-title000.mkv",
        final_relative_dir="Test Show/Season 01",
    )
    source = resolve_final_output(output_root, job)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"verified-rip")
    result = RipResult(
        job_id=job.job_id,
        return_code=0,
        output_count=1,
        output_bytes=source.stat().st_size,
        warning_count=0,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:01:00+00:00",
    )

    queued = enqueue_verified_rip_results(
        store,
        jobs=(job,),
        results=[result],
        output_root=output_root,
        contract_root=contract_root,
        media_id_overrides={job.job_id: "disc-title000-recovery-0123456789ab"},
    )

    assert queued[0].media_id == "disc-title000-recovery-0123456789ab"
    assert queued[0].stage == "identify"
    assert queued[0].state == "queued"
    private_payload = json.loads(queued[0].artifact.contract_path.read_text())
    assert private_payload["source_path"] == str(source.resolve())
    assert str(source) not in json.dumps([
        event.details for event in store.list_events()
    ])


def test_verified_non_episode_is_retained_without_downstream_queue(tmp_path):
    store = _store(tmp_path)
    output_root = tmp_path / "media-output"
    contract_root = tmp_path / "private-contracts"
    output_root.mkdir()
    contract_root.mkdir()
    basename = "Test-Disc--disc-01-0123456789abcdef-title-000.mkv"
    job = RipJob(
        job_id="disc-01-title-000",
        drive_index=0,
        title_index=0,
        relative_output_dir="Test Show/Season 01/isolated/title-000",
        output_basename=basename,
        final_relative_dir="Test Show/Season 01",
    )
    source = resolve_final_output(output_root, job)
    assert source is not None
    source.parent.mkdir(parents=True)
    source.write_bytes(b"verified-short-title")
    result = RipResult(
        job_id=job.job_id,
        return_code=0,
        output_count=1,
        output_bytes=source.stat().st_size,
        warning_count=0,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:01:00+00:00",
    )

    handled = enqueue_verified_rip_results(
        store,
        jobs=(job,),
        results=[result],
        output_root=output_root,
        contract_root=contract_root,
        media_contexts={
            "disc-01": MediaContext(
                disc_id="disc-01",
                series_name="Test Show",
                season=1,
                content_hint="tv",
                downstream_skip_title_indexes=(0,),
            )
        },
        expected_title_indexes_by_disc={"disc-01": (0, 1)},
    )

    assert source.is_file()
    assert handled[0].state == "completed"
    assert handled[0].stage == "identify"
    assert store.claim_next() is None
    assert store.title_dispositions("0123456789abcdef") == {
        0: {"disposition": "skip", "reason": "automatic_non_episode"}
    }
    assert store.list_events(handled[0].media_id)[-1].event_type == (
        "verified_rip_downstream_skipped"
    )
    private_payload = json.loads(
        handled[0].artifact.contract_path.read_text(encoding="utf-8")
    )
    assert private_payload["disc_expected_title_indexes"] == [1]
    assert store.expected_title_indexes_for_disc("0123456789abcdef") == (1,)
    store.remember_disc_matching_scope("0123456789abcdef", (1, 3))
    assert store.disc_matching_scope("0123456789abcdef") == (1, 3)
    assert store.expected_title_indexes_for_disc("0123456789abcdef") == (1, 3)
    assert store.restore_title_disposition("0123456789abcdef", 0) is True
    assert store.expected_title_indexes_for_disc("0123456789abcdef") == (0, 1, 3)


def test_verified_rip_retry_preserves_old_contract_and_uses_digest_name(tmp_path):
    store = _store(tmp_path)
    output_root = tmp_path / "media-output"
    contract_root = tmp_path / "private-contracts"
    output_root.mkdir()
    contract_root.mkdir()
    job = RipJob(
        job_id="disc-title000",
        drive_index=0,
        title_index=0,
        relative_output_dir="isolated/title-000",
        output_basename="disc-title000.mkv",
        final_relative_dir="Test Show/Season 01",
    )
    source = resolve_final_output(output_root, job)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"new-verified-rip")
    old_contract = contract_root / "disc-title000.verified-rip.json"
    old_contract.write_text('{"source_path":"old-attempt.mkv"}\n', encoding="utf-8")
    result = RipResult(
        job_id=job.job_id,
        return_code=0,
        output_count=1,
        output_bytes=source.stat().st_size,
        warning_count=0,
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:01:00+00:00",
    )

    queued = enqueue_verified_rip_results(
        store,
        jobs=(job,),
        results=[result],
        output_root=output_root,
        contract_root=contract_root,
    )

    assert old_contract.read_text(encoding="utf-8") == (
        '{"source_path":"old-attempt.mkv"}\n'
    )
    assert queued[0].artifact.contract_path.name.startswith(
        "disc-title000.verified-rip-"
    )
    assert queued[0].artifact.contract_path.name.endswith(".json")
    assert json.loads(queued[0].artifact.contract_path.read_text(encoding="utf-8"))[
        "source_path"
    ] == str(source.resolve())


def test_retained_source_reencode_starts_at_transcode(tmp_path):
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    rip = _artifact(tmp_path, "original", "rip")
    identify = _artifact(tmp_path, "reencode", "identify")
    store.enqueue_verified_rip("original", rip)

    queued = store.enqueue_reencode("original-reencode-001", identify, rip)

    assert queued.state == "queued"
    assert queued.stage == "transcode"
    assert queued.artifact == identify
    assert store.rip_artifact(queued.media_id) == rip
    assert store.list_events(queued.media_id)[-1].event_type == (
        "retained_source_reencode_queued"
    )


def test_sequence_diagnostic_accepts_real_ambiguous_planner_disposition(tmp_path):
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    store.enqueue_verified_rip("media-1", _artifact(tmp_path, "media-1", "rip"))

    store.record_sequence_diagnostic(
        ("media-1",),
        catalog_episode_count=27,
        file_count=1,
        best_score=0.51,
        runner_up_score=0.50,
        global_margin=0.01,
        disposition="review-ambiguous",
    )

    event = store.list_events("media-1")[-1]
    assert event.event_type == "sequence_match_scored"
    assert event.details["disposition"] == "review-ambiguous"


def test_matching_performance_is_durable_and_path_free(tmp_path):
    database = tmp_path / "queue.sqlite3"
    store = PipelineQueueStore(database)
    store.record_matching_performance(
        disc_fingerprint="0123456789abcdef",
        series_name="Example Series",
        title_count=18,
        anchor_count=3,
        season_scope=(6,),
        proposed_count=16,
        applied_count=16,
        unresolved_count=2,
        anchor_elapsed_ms=1200,
        total_elapsed_ms=6400,
    )

    records = PipelineQueueStore(database).matching_performance()

    assert len(records) == 1
    assert records[0]["series_name"] == "Example Series"
    assert records[0]["season_scope"] == [6]
    assert records[0]["anchor_count"] == 3
    assert records[0]["total_elapsed_ms"] == 6400
    assert records[0]["outcome"] == "completed"
    assert records[0]["failure_stage"] is None
    assert records[0]["provider_branches"] == []
    assert "path" not in records[0]


def test_matching_performance_migrates_existing_database_and_records_failure(
    tmp_path,
):
    database = tmp_path / "queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE matching_performance (
                run_id TEXT PRIMARY KEY,
                disc_fingerprint TEXT NOT NULL,
                series_name TEXT NOT NULL,
                title_count INTEGER NOT NULL,
                anchor_count INTEGER NOT NULL,
                season_scope TEXT NOT NULL,
                proposed_count INTEGER NOT NULL,
                applied_count INTEGER NOT NULL,
                unresolved_count INTEGER NOT NULL,
                anchor_elapsed_ms INTEGER NOT NULL,
                total_elapsed_ms INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
    store = PipelineQueueStore(database)
    store.record_matching_performance(
        disc_fingerprint="0123456789abcdef",
        series_name="Example Series",
        title_count=0,
        anchor_count=0,
        season_scope=(),
        proposed_count=0,
        applied_count=0,
        unresolved_count=0,
        anchor_elapsed_ms=0,
        total_elapsed_ms=20,
        outcome="failed",
        failure_stage="selection",
        failure_code="insufficient_held_titles",
        provider_branches=("tv-local", "tv-opensubtitles"),
    )

    record = store.matching_performance()[0]

    assert record["outcome"] == "failed"
    assert record["failure_stage"] == "selection"
    assert record["failure_code"] == "insufficient_held_titles"
    assert record["provider_branches"] == ["tv-local", "tv-opensubtitles"]
