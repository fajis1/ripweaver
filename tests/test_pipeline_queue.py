import json
import threading

import pytest

from mkv_episode_matcher.backend.routers.rip import _exact_queued_rip_source
from mkv_episode_matcher.disc.ripper import RipJob, RipResult, resolve_final_output
from mkv_episode_matcher.pipeline_queue import (
    DOWNSTREAM_STAGES,
    DownstreamDispatcher,
    PipelineQueueError,
    PipelineQueueStore,
    PipelineReviewRequiredError,
    build_artifact,
    enqueue_verified_rip_results,
)


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


def test_atomic_claim_allows_only_one_downstream_worker(tmp_path):
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
