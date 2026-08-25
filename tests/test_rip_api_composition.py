import asyncio
import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

from mkv_episode_matcher.backend.dependencies import (
    get_disc_inventory_runner,
    get_drive_watcher,
    get_orchestration_store,
    get_pipeline_contract_root,
    get_pipeline_queue_store,
    get_private_binding_store,
    get_rip_queue_runner,
)
from mkv_episode_matcher.backend.routers import rip
from mkv_episode_matcher.backend.routers.rip import router
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.disc.drive_watcher import (
    DriveStatusSnapshot,
    PublicDriveStatus,
)
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.preflight import CommandResult
from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.disc.rip_manifest import MediaContext
from mkv_episode_matcher.disc.rip_orchestrator import ParallelRipError
from mkv_episode_matcher.disc.rip_preview import build_rip_preview
from mkv_episode_matcher.disc.ripper import RipResult, resolve_final_output
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
from tests.test_rip_manifest import _inventory, _make_batch_names, _write_report


def _inventory_result_from_report(
    report: Path,
    source: str,
    *,
    changed: bool = False,
    include_drive_row: bool = True,
) -> CommandResult:
    payload = json.loads(report.read_text(encoding="utf-8"))
    drive = payload["drive"]
    rows: list[str] = []

    def add_row(values: list[object]) -> None:
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="").writerow(values)
        rows.append(buffer.getvalue())

    if include_drive_row:
        add_row([
            f"DRV:{drive['index']}",
            2,
            999,
            1,
            "private hardware",
            drive["disc_name"],
            "D:",
        ])
    for title_position, title in enumerate(payload["titles"]):
        title_index = title["index"]
        for code, value in title["attributes"].items():
            if changed and title_position == 0 and code == "9":
                value = "0:22:01"
            add_row([f"TINFO:{title_index}", code, 0, value])
        for stream_id, stream in title.get("streams", {}).items():
            for code, value in stream.get("attributes", {}).items():
                add_row([f"SINFO:{title_index}", stream_id, code, 0, value])
    return CommandResult(
        command=("makemkvcon64.exe", "info", source),
        return_code=0,
        stdout="\n".join(rows) + "\n",
        stderr="",
        started_at="2026-08-19T00:00:00+00:00",
        finished_at="2026-08-19T00:00:01+00:00",
    )


def _override_execution_inventory(
    app: FastAPI,
    report: Path,
    calls: list[str],
    *,
    changed: bool = False,
    include_drive_row: bool = True,
) -> None:
    def inventory_runner(_executable, source, **_kwargs):
        calls.append(source)
        return _inventory_result_from_report(
            report,
            source,
            changed=changed,
            include_drive_row=include_drive_row,
        )

    app.dependency_overrides[get_disc_inventory_runner] = lambda: inventory_runner


def test_repeated_read_failure_skip_endpoint_updates_future_plans_only(tmp_path):
    pipeline = PipelineQueueStore(tmp_path / "pipeline.sqlite3")

    response = rip.skip_disc_title_after_read_failure(
        rip.SkipDiscTitleDispositionRequest(
            disc_fingerprint="0123456789abcdef",
            title_index=16,
            reason="repeated_read_failure",
            confirm_skip=True,
        ),
        pipeline,
    )

    assert response["items"] == []
    assert response["title_dispositions"] == [
        {
            "disc_fingerprint": "0123456789abcdef",
            "title_index": 16,
            "disposition": "skip",
            "reason": "repeated_read_failure",
        }
    ]
    assert pipeline.title_dispositions("0123456789abcdef") == {
        16: {"disposition": "skip", "reason": "repeated_read_failure"}
    }


def test_collision_resolution_can_create_missing_only_review(tmp_path):
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
    context = MediaContext(
        disc_id="disc-01",
        series_name="Synthetic Show",
        season=1,
        staging_attempt="attempt-original01",
    )
    initial = build_rip_preview([report], {"disc-01": context})
    (output_root / initial.jobs[0].staging_destination).mkdir(parents=True)
    preview = build_rip_preview([report], {"disc-01": context}, output_root=output_root)
    source = public.create_job(preview, idempotency_key="source-review-0001")
    private.bind(
        job_id=source.job_id,
        plan_sha256=source.plan_sha256,
        report_paths=[report],
        output_root=output_root,
        media_contexts={"disc-01": context},
    )

    response = rip.resolve_rip_collisions(
        source.job_id,
        rip.ResolveRipCollisionsRequest(policy="missing-only", confirm_resolution=True),
        "missing-review-0001",
        public,
        private,
    )

    assert len(response["preview"]["jobs"]) == 1
    assert response["preview"]["jobs"][0]["title_index"] == 1
    assert response["preview"]["collision_count"] == 0
    assert response["state"] == "queued"
    assert (
        private.get(response["job_id"]).media_contexts["disc-01"].staging_attempt
        != context.staging_attempt
    )
    replacement = rip.resolve_rip_collisions(
        source.job_id,
        rip.ResolveRipCollisionsRequest(
            policy="replace-after-verification", confirm_resolution=True
        ),
        "replace-review-0001",
        public,
        private,
    )
    replacement_context = private.get(replacement["job_id"]).media_contexts["disc-01"]
    assert replacement["state"] == "queued"
    assert replacement_context.existing_output_policy == "replace-after-verification"
    assert len(replacement["preview"]["jobs"]) == 2

    selected = rip.select_rip_titles(
        source.job_id,
        rip.SelectRipTitlesRequest(title_indexes=[1], confirm_selection=True),
        "selected-review-0001",
        public,
        private,
        pipeline,
    )
    assert [item["title_index"] for item in selected["preview"]["jobs"]] == [1]

    fingerprint = next(
        part
        for part in source.preview["jobs"][0]["staging_destination"].split("/")
        if len(part) == 16
        and all(character in "0123456789abcdef" for character in part)
    )
    pipeline.remember_title_skip(fingerprint, 1, reason="repeated_read_failure")
    with pytest.raises(HTTPException, match="saved as skipped"):
        rip.select_rip_titles(
            source.job_id,
            rip.SelectRipTitlesRequest(title_indexes=[1], confirm_selection=True),
            "selected-review-skipped-0001",
            public,
            private,
            pipeline,
        )


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


def test_api_job_composes_through_private_dispatch_with_fake_queue(
    tmp_path, monkeypatch
):
    config = Config(cache_dir=tmp_path / "cache", automatic_eject_after_rip=False)
    monkeypatch.setattr(
        rip, "get_config_manager", lambda: SimpleNamespace(load=lambda: config)
    )
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
    monkeypatch.setattr(rip, "get_pipeline_queue_store", lambda: pipeline)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_orchestration_store] = lambda: public
    app.dependency_overrides[get_private_binding_store] = lambda: private
    app.dependency_overrides[get_pipeline_queue_store] = lambda: pipeline
    app.dependency_overrides[get_pipeline_contract_root] = lambda: (
        tmp_path / "pipeline-contracts"
    )
    app.dependency_overrides[get_drive_watcher] = lambda: SimpleNamespace(
        snapshot=lambda: DriveStatusSnapshot(
            drives=(
                PublicDriveStatus(
                    drive_index=4,
                    available=True,
                    has_disc=True,
                    display_name="Synthetic optical drive",
                    disc_label="Synthetic Disc",
                ),
            ),
            refreshed_at="2026-08-20T00:00:00+00:00",
            status="ready",
        ),
        device_name=lambda drive_index: "D:" if drive_index == 4 else None,
    )
    inventory_calls: list[str] = []
    _override_execution_inventory(app, report, inventory_calls)
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

    # Simulate an older retained attempt with the same normal media ID. The new
    # verified MKV must receive a recovery lineage instead of blocking MakeMKV
    # completion or overwriting the earlier queue record.
    first_media_id = Path(created["preview"]["jobs"][0]["final_destination"]).stem
    older_contract = tmp_path / "older-rip-contract.json"
    older_contract.write_text('{"mode":"synthetic-old-rip"}\n', encoding="utf-8")
    pipeline.enqueue_verified_rip(
        first_media_id,
        build_artifact("rip", older_contract),
    )

    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic executable placeholder")
    calls = []

    def fake_queue(_executable, root, jobs, _run_directory, **kwargs):
        calls.append(tuple(jobs))
        now = datetime.now(UTC).isoformat()
        for job in jobs:
            destination = resolve_final_output(root, job)
            assert destination is not None
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"x")
        results = [
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
        for result in results:
            kwargs["on_result"](result)
        return results

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
    assert inventory_calls == []

    _override_execution_inventory(
        app,
        report,
        inventory_calls,
        changed=True,
    )
    fresh_mismatch_status, _, fresh_mismatch = _post(
        app,
        f"/rip/jobs/{created['job_id']}/execute",
        {
            "expected_plan_sha256": created["plan_sha256"],
            "authorized_job_count": len(created["preview"]["jobs"]),
            "makemkv_executable": str(executable),
            "run_directory": str(tmp_path / "fresh-mismatch-fake-run"),
            "timeout_seconds": 600,
            "max_drives": 1,
            "confirm_execute": True,
        },
        idempotency_key="dispatch-fresh-mismatch-0001",
    )
    assert fresh_mismatch_status == 409
    assert "no longer match" in fresh_mismatch["detail"]
    assert calls == []
    assert public.get_job(created["job_id"]).state == "queued"

    # Targeted --noscan inventory may contain valid title rows while omitting
    # the global DRV row. Execution must use the same trusted-slot rebinding as
    # drive preparation before it validates the immutable inventory signature.
    _override_execution_inventory(
        app,
        report,
        inventory_calls,
        include_drive_row=False,
    )

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
    assert completed["pipeline_handoff_status"] == "complete"
    assert completed["pipeline_queued_title_count"] == 2
    assert completed["pipeline_handoff_pending_title_count"] == 0
    assert str(output_root).encode() not in execute_body
    assert str(executable).encode() not in execute_body
    assert len(calls) == 1
    assert inventory_calls == ["disc:4", "disc:4"]
    assert len(calls[0]) == len(created["preview"]["jobs"])
    assert not (tmp_path / "unused-fake-run").exists()
    assert len(pipeline.list_items()) == len(created["preview"]["jobs"]) + 1
    assert {item.stage for item in pipeline.list_items()} == {"identify"}
    new_items = [
        item for item in pipeline.list_items() if item.media_id != first_media_id
    ]
    assert any("-recovery-" in item.media_id for item in new_items)
    assert {rip._pipeline_item_response(item)["title_index"] for item in new_items} == {
        0,
        1,
    }


def test_failed_disc_queues_verified_titles_and_retry_runs_only_unfinished(
    tmp_path, monkeypatch
):
    config = Config(cache_dir=tmp_path / "cache", automatic_eject_after_rip=False)
    monkeypatch.setattr(
        rip, "get_config_manager", lambda: SimpleNamespace(load=lambda: config)
    )
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
    inventory_calls: list[str] = []
    _override_execution_inventory(app, report, inventory_calls)
    created_status, _, created = _post(
        app,
        "/rip/jobs",
        {
            "report_paths": [str(report)],
            "media_contexts": {
                "disc-01": {"series_name": "Synthetic Show", "season": 1}
            },
            "output_root": str(output_root),
        },
        idempotency_key="create-partial-composition-0001",
    )
    assert created_status == 200
    assert (
        _post(
            app,
            f"/rip/jobs/{created['job_id']}/authorize",
            {
                "expected_plan_sha256": created["plan_sha256"],
                "confirm_authorization": True,
            },
            idempotency_key="authorize-partial-composition-0001",
        )[0]
        == 200
    )
    assert (
        _post(
            app,
            f"/rip/jobs/{created['job_id']}/start",
            {"confirm_queue": True},
            idempotency_key="queue-partial-composition-0001",
        )[0]
        == 200
    )

    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic executable placeholder")
    calls = []

    def fake_queue(_executable, root, jobs, _run_directory, **_kwargs):
        calls.append(tuple(jobs))
        selected = tuple(jobs) if len(calls) > 1 else tuple(jobs[:1])
        now = datetime.now(UTC).isoformat()
        results = []
        for job in selected:
            destination = resolve_final_output(root, job)
            assert destination is not None
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"x")
            results.append(
                RipResult(
                    job_id=job.job_id,
                    return_code=0,
                    output_count=1,
                    output_bytes=1,
                    warning_count=0,
                    started_at=now,
                    finished_at=now,
                )
            )
        if len(calls) == 1:
            raise ParallelRipError(
                "second title failed",
                completed_results=results,
                drive_failures={jobs[0].drive_index: "RipError"},
            )
        return results

    app.dependency_overrides[get_rip_queue_runner] = lambda: fake_queue
    execute_payload = {
        "expected_plan_sha256": created["plan_sha256"],
        "authorized_job_count": len(created["preview"]["jobs"]),
        "makemkv_executable": str(executable),
        "run_directory": str(tmp_path / "first-fake-run"),
        "timeout_seconds": 600,
        "max_drives": 1,
        "confirm_execute": True,
    }
    failed_status, _, failed = _post(
        app,
        f"/rip/jobs/{created['job_id']}/execute",
        execute_payload,
        idempotency_key="dispatch-partial-composition-0001",
    )

    assert failed_status == 409
    assert failed["detail"] == "second title failed"
    assert len(pipeline.list_items()) == 1
    first_contract = json.loads(
        pipeline.list_items()[0].artifact.contract_path.read_text(encoding="utf-8")
    )
    assert first_contract["disc_expected_title_indexes"] == [0, 1]

    missing_title_index = created["preview"]["jobs"][1]["title_index"]
    selection_status, _, selected = _post(
        app,
        f"/rip/jobs/{created['job_id']}/select-titles",
        {"title_indexes": [missing_title_index], "confirm_selection": True},
        idempotency_key="select-partial-composition-0001",
    )
    assert selection_status == 200
    assert [item["title_index"] for item in selected["preview"]["jobs"]] == [
        missing_title_index
    ]
    assert (
        _post(
            app,
            f"/rip/jobs/{selected['job_id']}/authorize",
            {
                "expected_plan_sha256": selected["plan_sha256"],
                "confirm_authorization": True,
            },
            idempotency_key="authorize-partial-composition-0002",
        )[0]
        == 200
    )
    assert (
        _post(
            app,
            f"/rip/jobs/{selected['job_id']}/start",
            {"confirm_queue": True},
            idempotency_key="queue-partial-composition-0002",
        )[0]
        == 200
    )
    execute_payload["expected_plan_sha256"] = selected["plan_sha256"]
    execute_payload["authorized_job_count"] = 1
    execute_payload["run_directory"] = str(tmp_path / "retry-fake-run")
    completed_status, _, completed = _post(
        app,
        f"/rip/jobs/{selected['job_id']}/execute",
        execute_payload,
        idempotency_key="dispatch-partial-composition-0002",
    )

    assert completed_status == 200
    assert completed["state"] == "completed"
    assert len(calls) == 2
    assert inventory_calls == ["disc:4", "disc:4"]
    assert len(calls[0]) == 2
    assert len(calls[1]) == 1
    assert calls[1][0].job_id != calls[0][0].job_id
    assert len(pipeline.list_items()) == 2
    for item in pipeline.list_items():
        contract = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
        assert contract["disc_expected_title_indexes"] == [0, 1]
