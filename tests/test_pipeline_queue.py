import json
import threading

import pytest

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
    )

    assert queued[0].stage == "identify"
    assert queued[0].state == "queued"
    private_payload = json.loads(queued[0].artifact.contract_path.read_text())
    assert private_payload["source_path"] == str(source.resolve())
    assert str(source) not in json.dumps([
        event.details for event in store.list_events()
    ])
