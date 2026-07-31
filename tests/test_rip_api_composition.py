import asyncio
import json
from datetime import UTC, datetime

from fastapi import FastAPI

from mkv_episode_matcher.backend.dependencies import (
    get_orchestration_store,
    get_pipeline_contract_root,
    get_pipeline_queue_store,
    get_private_binding_store,
    get_rip_queue_runner,
)
from mkv_episode_matcher.backend.routers.rip import router
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.disc.ripper import RipResult, resolve_final_output
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore
from tests.test_rip_manifest import _inventory, _make_batch_names, _write_report


def _post(app, path, payload, *, idempotency_key):
    body = json.dumps(payload).encode()
    sent = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"idempotency-key", idempotency_key.encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }
    asyncio.run(app(scope, receive, send))
    status = next(
        message["status"]
        for message in sent
        if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return status, response_body, json.loads(response_body)


def test_api_job_composes_through_private_dispatch_with_fake_queue(tmp_path):
    report = _write_report(
        tmp_path,
        "saved-report.json",
        _make_batch_names(_inventory(4, "Synthetic Disc", [1300, 1300])),
    )
    output_root = tmp_path / "output"
    output_root.mkdir()
    public = OrchestrationStore(tmp_path / "public.sqlite3")
    private = PrivateBindingStore(tmp_path / "private.sqlite3")
    pipeline = PipelineQueueStore(tmp_path / "pipeline.sqlite3")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_orchestration_store] = lambda: public
    app.dependency_overrides[get_private_binding_store] = lambda: private
    app.dependency_overrides[get_pipeline_queue_store] = lambda: pipeline
    app.dependency_overrides[get_pipeline_contract_root] = lambda: (
        tmp_path / "pipeline-contracts"
    )
    request = {
        "report_paths": [str(report)],
        "media_contexts": {
            "disc-01": {
                "series_name": "Synthetic Show",
                "season": 1,
            }
        },
        "output_root": str(output_root),
    }

    created_status, created_body, created = _post(
        app,
        "/rip/jobs",
        request,
        idempotency_key="create-composition-0001",
    )
    assert created_status == 200
    assert b"report_paths" not in created_body
    assert str(output_root).encode() not in created_body

    authorized_status, _, _ = _post(
        app,
        f"/rip/jobs/{created['job_id']}/authorize",
        {
            "expected_plan_sha256": created["plan_sha256"],
            "confirm_authorization": True,
        },
        idempotency_key="authorize-composition-0001",
    )
    assert authorized_status == 200
    queued_status, _, queued = _post(
        app,
        f"/rip/jobs/{created['job_id']}/start",
        {"confirm_queue": True},
        idempotency_key="queue-composition-0001",
    )
    assert queued_status == 200
    assert queued["state"] == "queued"
    assert queued["executor_attached"] is False

    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic executable placeholder")
    calls = []

    def fake_queue(_executable, root, jobs, _run_directory, **_kwargs):
        calls.append(tuple(jobs))
        now = datetime.now(UTC).isoformat()
        for job in jobs:
            destination = resolve_final_output(root, job)
            assert destination is not None
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"x")
        return [
            RipResult(
                job_id=job.job_id,
                return_code=0,
                output_count=1,
                output_bytes=1,
                warning_count=0,
                started_at=now,
                finished_at=now,
            )
            for job in jobs
        ]

    app.dependency_overrides[get_rip_queue_runner] = lambda: fake_queue
    refused_status, refused_body, _ = _post(
        app,
        f"/rip/jobs/{created['job_id']}/execute",
        {
            "expected_plan_sha256": created["plan_sha256"],
            "authorized_job_count": len(created["preview"]["jobs"]),
            "makemkv_executable": str(executable),
            "run_directory": str(tmp_path / "refused-fake-run"),
            "confirm_execute": False,
        },
        idempotency_key="dispatch-refused-0001",
    )
    assert refused_status == 400
    assert b"physical rip confirmation" in refused_body
    assert calls == []

    mismatch_status, _, _ = _post(
        app,
        f"/rip/jobs/{created['job_id']}/execute",
        {
            "expected_plan_sha256": created["plan_sha256"],
            "authorized_job_count": len(created["preview"]["jobs"]) + 1,
            "makemkv_executable": str(executable),
            "run_directory": str(tmp_path / "mismatch-fake-run"),
            "confirm_execute": True,
        },
        idempotency_key="dispatch-mismatch-0001",
    )
    assert mismatch_status == 409
    assert calls == []

    execute_status, execute_body, completed = _post(
        app,
        f"/rip/jobs/{created['job_id']}/execute",
        {
            "expected_plan_sha256": created["plan_sha256"],
            "authorized_job_count": len(created["preview"]["jobs"]),
            "makemkv_executable": str(executable),
            "run_directory": str(tmp_path / "unused-fake-run"),
            "timeout_seconds": 600,
            "max_drives": 1,
            "confirm_execute": True,
        },
        idempotency_key="dispatch-composition-0001",
    )

    assert execute_status == 200
    assert completed["state"] == "completed"
    assert str(output_root).encode() not in execute_body
    assert str(executable).encode() not in execute_body
    assert len(calls) == 1
    assert len(calls[0]) == len(created["preview"]["jobs"])
    assert not (tmp_path / "unused-fake-run").exists()
    assert len(pipeline.list_items()) == len(created["preview"]["jobs"])
    assert {item.stage for item in pipeline.list_items()} == {"identify"}
