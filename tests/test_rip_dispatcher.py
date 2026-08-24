import json
from datetime import UTC, datetime

import pytest

from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.disc.rip_dispatcher import (
    DispatchOutcome,
    RipDispatcher,
)
from mkv_episode_matcher.disc.rip_manifest import MediaContext
from mkv_episode_matcher.disc.rip_orchestrator import ParallelRipError
from mkv_episode_matcher.disc.rip_preview import build_rip_preview
from mkv_episode_matcher.disc.ripper import RipError, RipResult, resolve_final_output
from tests.test_rip_manifest import (
    _inventory,
    _make_batch_names,
    _write_report,
)


def _queued_dispatch(tmp_path):
    report = _write_report(
        tmp_path,
        "private-report.json",
        _make_batch_names(_inventory(3, "Private Disc Label", [1300, 1300, 1300])),
    )
    output_root = tmp_path / "private-output"
    output_root.mkdir()
    contexts = {
        "disc-01": MediaContext(
            disc_id="disc-01",
            series_name="Test Show",
            season=1,
        )
    }
    preview = build_rip_preview(
        [report],
        contexts,
        output_root=output_root,
    )
    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    job = public.create_job(preview, idempotency_key="create-dispatch-0001")
    private.bind(
        job_id=job.job_id,
        plan_sha256=job.plan_sha256,
        report_paths=[report],
        output_root=output_root,
        media_contexts=contexts,
    )
    public.authorize(
        job.job_id,
        expected_plan_sha256=job.plan_sha256,
        idempotency_key="authorize-dispatch-0001",
    )
    public.queue(job.job_id, idempotency_key="queue-dispatch-0001")
    return RipDispatcher(public, private), job.job_id, report, output_root


def test_fake_dispatch_revalidates_claims_and_completes(tmp_path):
    dispatcher, job_id, report, output_root = _queued_dispatch(tmp_path)
    calls = []
    inventory_calls = []

    def fresh_inventory():
        inventory_calls.append(True)
        return [report]

    def fake_executor(bound):
        calls.append(bound)
        assert bound.output_root == output_root
        assert set(bound.batch_plans) == {3}
        return DispatchOutcome(completed_count=len(bound.manifest.jobs))

    completed = dispatcher.dispatch(
        job_id,
        dispatch_key="fake-dispatch-0001",
        executor=fake_executor,
        fresh_inventory_provider=fresh_inventory,
    )

    assert len(calls) == 1
    assert completed.state == "completed"
    assert completed.executor_attached is False
    events = dispatcher.public_store.list_events(job_id)
    assert [event.event_type for event in events[-2:]] == [
        "job_dispatch_claimed",
        "job_completed",
    ]

    retry = dispatcher.dispatch(
        job_id,
        dispatch_key="fake-dispatch-0001",
        executor=lambda _bound: calls.append("unexpected"),
        fresh_inventory_provider=fresh_inventory,
    )
    assert retry.state == "completed"
    assert len(calls) == 1
    assert inventory_calls == [True]
    assert len(dispatcher.public_store.list_events(job_id)) == len(events)


def test_pipeline_handoff_warning_does_not_mark_physical_rip_failed(tmp_path):
    dispatcher, job_id, _report, _output_root = _queued_dispatch(tmp_path)
    pending_id = dispatcher.public_store.get_job(job_id).preview["jobs"][0]["job_id"]

    completed = dispatcher.dispatch(
        job_id,
        dispatch_key="fake-handoff-warning-0001",
        executor=lambda bound: DispatchOutcome(
            completed_count=len(bound.manifest.jobs),
            pipeline_queued_count=len(bound.manifest.jobs) - 1,
            pipeline_handoff_pending_job_ids=(pending_id,),
        ),
    )

    assert completed.state == "completed"
    event = dispatcher.public_store.list_events(job_id)[-1]
    assert event.event_type == "job_completed"
    assert event.details["pipeline_handoff_status"] == "attention_required"
    assert event.details["pipeline_handoff_pending_job_ids"] == [pending_id]


def test_incomplete_fake_executor_marks_job_failed(tmp_path):
    dispatcher, job_id, _report, _output_root = _queued_dispatch(tmp_path)

    with pytest.raises(RipError, match="incomplete"):
        dispatcher.dispatch(
            job_id,
            dispatch_key="fake-dispatch-0002",
            executor=lambda _bound: DispatchOutcome(completed_count=1),
        )

    failed = dispatcher.public_store.get_job(job_id)
    assert failed.state == "failed"
    assert failed.executor_attached is False
    assert (
        dispatcher.public_store.list_events(job_id)[-1].details["error_type"]
        == "RipError"
    )


def test_changed_fresh_report_refuses_before_claim(tmp_path):
    dispatcher, job_id, report, _output_root = _queued_dispatch(tmp_path)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["titles"][0]["attributes"]["9"] = "0:22:00"
    changed_report = tmp_path / "changed-fresh-report.json"
    changed_report.write_text(json.dumps(payload), encoding="utf-8")
    calls = []

    with pytest.raises(RipError, match="no longer match"):
        dispatcher.dispatch(
            job_id,
            dispatch_key="fake-dispatch-0003",
            executor=lambda _bound: calls.append(True),
            fresh_inventory_provider=lambda: [changed_report],
        )

    assert calls == []
    assert dispatcher.public_store.get_job(job_id).state == "queued"


def test_new_collision_refuses_before_claim(tmp_path):
    dispatcher, job_id, _report, output_root = _queued_dispatch(tmp_path)
    job = dispatcher.public_store.get_job(job_id)
    target = job.preview["jobs"][0]["final_destination"]
    assert isinstance(target, str)
    collision = output_root / target
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"existing")

    with pytest.raises(RipError, match="collision"):
        dispatcher.dispatch(
            job_id,
            dispatch_key="fake-dispatch-0004",
            executor=lambda _bound: DispatchOutcome(completed_count=3),
        )

    assert collision.read_bytes() == b"existing"
    assert dispatcher.public_store.get_job(job_id).state == "queued"


def test_restart_reconciliation_pauses_claimed_job(tmp_path):
    dispatcher, job_id, _report, _output_root = _queued_dispatch(tmp_path)
    claimed = dispatcher.public_store.claim_for_dispatch(
        job_id,
        idempotency_key="claim-before-restart-0001",
    )
    assert claimed.state == "running"
    assert claimed.executor_attached is True

    reopened = OrchestrationStore(dispatcher.public_store.database_path)
    reconciled = reopened.reconcile_incomplete()

    assert [job.job_id for job in reconciled] == [job_id]
    assert reconciled[0].state == "paused"
    assert reconciled[0].executor_attached is False
    assert reopened.list_events(job_id)[-1].event_type == "job_restart_reconciled"


def test_failed_multi_drive_retry_dispatches_only_unfinished_titles(tmp_path):
    dispatcher, job_id, _report, output_root = _queued_dispatch(tmp_path)
    job = dispatcher.public_store.get_job(job_id)
    completed_job = job.preview["jobs"][0]
    now = datetime.now(UTC).isoformat()
    completed_result = RipResult(
        job_id=completed_job["job_id"],
        return_code=0,
        output_count=1,
        output_bytes=1,
        warning_count=0,
        started_at=now,
        finished_at=now,
    )

    def partially_failing(bound):
        source_job = next(
            item
            for item in bound.manifest.jobs
            if item.job_id == completed_result.job_id
        )
        destination = resolve_final_output(output_root, source_job)
        assert destination is not None
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x")
        raise ParallelRipError(
            "drive worker timed out",
            completed_results=[completed_result],
            drive_failures={3: "RipError"},
        )

    with pytest.raises(ParallelRipError):
        dispatcher.dispatch(
            job_id, dispatch_key="partial-dispatch-0001", executor=partially_failing
        )

    failure = dispatcher.public_store.list_events(job_id)[-1]
    assert failure.details["error_category"] == "timeout"
    assert failure.details["completed_job_ids"] == [completed_result.job_id]
    dispatcher.public_store.retry_failed(
        job_id, idempotency_key="retry-partial-dispatch-0001"
    )
    received = []

    def retry_executor(bound):
        received.extend(item.job_id for item in bound.manifest.jobs)
        assert bound.batch_plans == {}
        return DispatchOutcome(completed_count=len(bound.manifest.jobs))

    completed = dispatcher.dispatch(
        job_id, dispatch_key="partial-dispatch-0002", executor=retry_executor
    )
    assert completed.state == "completed"
    assert completed_result.job_id not in received
    assert len(received) == 2
