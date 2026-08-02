from concurrent.futures import ThreadPoolExecutor

import pytest

from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.rip_manifest import MediaContext
from mkv_episode_matcher.disc.rip_preview import build_rip_preview
from mkv_episode_matcher.disc.ripper import RipError
from tests.test_rip_manifest import (
    _inventory,
    _make_batch_names,
    _write_report,
)


def _preview(tmp_path):
    payload = _make_batch_names(_inventory(3, "Private Disc Label", [1300, 1300, 1300]))
    report = _write_report(tmp_path, "private-report.json", payload)
    output_root = tmp_path / "private-output"
    output_root.mkdir()
    preview = build_rip_preview(
        [report],
        {
            "disc-01": MediaContext(
                disc_id="disc-01",
                series_name="Test Show",
                season=1,
            )
        },
        output_root=output_root,
    )
    return preview, report, output_root


def test_create_is_idempotent_and_database_is_path_redacted(tmp_path):
    preview, report, output_root = _preview(tmp_path)
    database = tmp_path / "state" / "jobs.sqlite3"
    store = OrchestrationStore(database)

    first = store.create_job(preview, idempotency_key="create-request-0001")
    retry = store.create_job(preview, idempotency_key="create-request-0001")
    serialized_database = database.read_bytes()

    assert first.job_id == retry.job_id
    assert first.state == "awaiting_review"
    assert first.executor_attached is False
    assert len(store.list_events(first.job_id)) == 1
    assert str(report).encode() not in serialized_database
    assert str(output_root).encode() not in serialized_database
    assert b"Private Disc Label" not in serialized_database


def test_exact_digest_authorizes_then_queue_pause_resume(tmp_path):
    preview, _report, _root = _preview(tmp_path)
    store = OrchestrationStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(preview, idempotency_key="create-request-0002")

    with pytest.raises(RipError, match="does not match"):
        store.authorize(
            job.job_id,
            expected_plan_sha256="0" * 64,
            idempotency_key="authorize-request-0002",
        )

    authorized = store.authorize(
        job.job_id,
        expected_plan_sha256=job.plan_sha256,
        idempotency_key="authorize-request-0003",
    )
    queued = store.queue(
        job.job_id,
        idempotency_key="start-request-0003",
    )
    retry = store.queue(
        job.job_id,
        idempotency_key="start-request-0003",
    )
    paused = store.pause(
        job.job_id,
        idempotency_key="pause-request-0003",
    )
    resumed = store.resume(
        job.job_id,
        idempotency_key="resume-request-0003",
    )

    assert authorized.state == "authorized"
    assert authorized.authorization_sha256 is not None
    assert queued.state == "queued"
    assert retry.state == "queued"
    assert paused.state == "paused"
    assert resumed.state == "queued"
    events = store.list_events(job.job_id)
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
    assert [event.event_type for event in events] == [
        "job_created",
        "job_authorized",
        "job_queued",
        "job_paused",
        "job_resumed",
    ]


def test_start_before_authorization_is_refused(tmp_path):
    preview, _report, _root = _preview(tmp_path)
    store = OrchestrationStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(preview, idempotency_key="create-request-0004")

    with pytest.raises(RipError, match="awaiting_review"):
        store.queue(job.job_id, idempotency_key="start-request-0004")

    assert store.get_job(job.job_id).state == "awaiting_review"
    assert len(store.list_events(job.job_id)) == 1


def test_queued_job_can_return_to_review_without_changing_plan(tmp_path):
    preview, _report, _root = _preview(tmp_path)
    store = OrchestrationStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(preview, idempotency_key="create-return-0001")
    store.authorize(
        job.job_id,
        expected_plan_sha256=job.plan_sha256,
        idempotency_key="authorize-return-0001",
    )
    store.queue(job.job_id, idempotency_key="queue-return-0001")

    reviewed = store.return_to_review(job.job_id, idempotency_key="return-review-0001")

    assert reviewed.state == "awaiting_review"
    assert reviewed.plan_sha256 == job.plan_sha256
    assert reviewed.executor_attached is False
    assert store.list_events(job.job_id)[-1].event_type == "job_returned_to_review"


def test_non_running_job_can_be_cancelled_without_changing_media(tmp_path):
    preview, _report, _root = _preview(tmp_path)
    store = OrchestrationStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(preview, idempotency_key="create-cancel-0001")

    cancelled = store.cancel(job.job_id, idempotency_key="cancel-job-0001")

    assert cancelled.state == "cancelled"
    event = store.list_events(job.job_id)[-1]
    assert event.event_type == "job_cancelled"
    assert event.details == {"media_changed": False}


def test_running_job_records_path_free_progress_and_updates_timestamp(tmp_path):
    preview, _report, _root = _preview(tmp_path)
    store = OrchestrationStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(preview, idempotency_key="create-progress-0001")
    store.authorize(
        job.job_id,
        expected_plan_sha256=job.plan_sha256,
        idempotency_key="authorize-progress-0001",
    )
    store.queue(job.job_id, idempotency_key="queue-progress-0001")
    store.claim_for_dispatch(job.job_id, idempotency_key="claim-progress-0001")

    store.record_progress(job.job_id, "progress", "disc-01-title-000: 25%")
    store.record_progress(job.job_id, "throughput", "disc-01-title-000: 12.34 MiB/s")

    progress, throughput = store.list_events(job.job_id)[-2:]
    assert progress.event_type == "rip_progress"
    assert progress.details == {"percent": 25, "scope": "disc-01-title-000"}
    assert throughput.event_type == "rip_throughput"
    assert throughput.details == {
        "mib_per_second": 12.34,
        "scope": "disc-01-title-000",
    }
    assert store.get_job(job.job_id).updated_at >= job.updated_at


def test_failed_job_can_be_queued_for_collision_safe_retry(tmp_path):
    preview, _report, _root = _preview(tmp_path)
    store = OrchestrationStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(preview, idempotency_key="create-retry-store-0001")
    store.authorize(
        job.job_id,
        expected_plan_sha256=job.plan_sha256,
        idempotency_key="authorize-retry-store-0001",
    )
    store.queue(job.job_id, idempotency_key="queue-retry-store-0001")
    store.fail(
        job.job_id,
        idempotency_key="fail-retry-store-0001",
        error_type="RipError",
        error_category="timeout",
        failed_drive_indexes=(2,),
        completed_job_ids=("disc-01-title-000",),
    )
    retried = store.retry_failed(job.job_id, idempotency_key="retry-retry-store-0001")
    assert retried.state == "queued"
    assert store.list_events(job.job_id)[-1].event_type == "job_retry_queued"


def test_restart_recovers_job_and_events(tmp_path):
    preview, _report, _root = _preview(tmp_path)
    database = tmp_path / "jobs.sqlite3"
    first_store = OrchestrationStore(database)
    created = first_store.create_job(
        preview,
        idempotency_key="create-request-0005",
    )

    reopened = OrchestrationStore(database)

    assert reopened.get_job(created.job_id) == created
    assert reopened.list_events(created.job_id)[0].event_type == "job_created"


def test_recent_job_listing_is_path_redacted_and_newest_first(tmp_path):
    preview, _report, _root = _preview(tmp_path)
    store = OrchestrationStore(tmp_path / "jobs.sqlite3")
    first = store.create_job(preview, idempotency_key="list-create-0001")
    second = store.create_job(preview, idempotency_key="list-create-0002")

    listed = store.list_jobs(limit=2)

    assert {job.job_id for job in listed} == {first.job_id, second.job_id}
    assert all("report_paths" not in str(job.preview) for job in listed)


def test_concurrent_creation_retry_produces_one_job(tmp_path):
    preview, _report, _root = _preview(tmp_path)
    store = OrchestrationStore(tmp_path / "jobs.sqlite3")

    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs = list(
            executor.map(
                lambda _index: store.create_job(
                    preview,
                    idempotency_key="concurrent-create-0001",
                ),
                range(8),
            )
        )

    assert len({job.job_id for job in jobs}) == 1
    assert len(store.list_events(jobs[0].job_id)) == 1
