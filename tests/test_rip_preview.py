import json

import pytest
from fastapi import HTTPException

from mkv_episode_matcher.backend.routers.rip import (
    AuthorizeJobRequest,
    RipPreviewRequest,
    RipPreviewResponse,
    StartJobRequest,
    authorize_rip_job,
    create_rip_job,
    preview_rip,
    router,
    start_rip_job,
)
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.disc.rip_manifest import MediaContext
from mkv_episode_matcher.disc.rip_preview import build_rip_preview
from tests.test_rip_manifest import (
    _inventory,
    _make_batch_names,
    _write_report,
)


def _report(tmp_path, *, batch_names=True):
    payload = _inventory(3, "Private Disc Label", [1300, 1300, 1300])
    if batch_names:
        payload = _make_batch_names(payload)
    return _write_report(tmp_path, "fresh.json", payload)


def _contexts():
    return {
        "disc-01": MediaContext(
            disc_id="disc-01",
            series_name="Test Show",
            season=1,
            disc_number=1,
        )
    }


def test_preview_is_redacted_non_executable_and_selects_single_open(tmp_path):
    report = _report(tmp_path)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    preview = build_rip_preview([report], _contexts())
    serialized = json.dumps(preview.to_dict())

    assert preview.execution_authorized is False
    assert preview.drives[0].strategy == "single-open"
    assert preview.drives[0].minimum_length_seconds == 0
    assert all(job.collision_status == "not-checked" for job in preview.jobs)
    assert str(report) not in serialized
    assert report.name not in serialized
    assert "Private Disc Label" not in serialized
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_preview_reports_final_collision_without_modifying_it(tmp_path):
    report = _report(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    initial = build_rip_preview([report], _contexts())
    relative_target = initial.jobs[1].final_destination
    assert relative_target is not None
    existing = output_root / relative_target
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")

    preview = build_rip_preview(
        [report],
        _contexts(),
        output_root=output_root,
    )

    assert preview.collision_count == 1
    assert preview.requires_review is True
    assert preview.jobs[1].collision_status == "final-exists"
    assert existing.read_bytes() == b"existing"


def test_preview_keeps_ineligible_inventory_on_per_title_strategy(tmp_path):
    report = _report(tmp_path, batch_names=False)

    preview = build_rip_preview([report], _contexts())

    assert preview.drives[0].strategy == "per-title"
    assert preview.drives[0].reason == "no-exact-runtime-cutoff"


def test_preview_digest_is_stable_across_replanning(tmp_path):
    report = _report(tmp_path)

    first = build_rip_preview([report], _contexts())
    second = build_rip_preview([report], _contexts())

    assert first.plan_sha256 == second.plan_sha256


def test_api_exposes_preview_without_execution_route(tmp_path):
    report = _report(tmp_path)
    payload = preview_rip(
        RipPreviewRequest(
            report_paths=[str(report)],
            media_contexts={
                "disc-01": {
                    "series_name": "Test Show",
                    "season": 1,
                    "disc_number": 1,
                }
            },
        )
    )

    response = RipPreviewResponse.model_validate(payload)
    assert response.execution_authorized is False
    assert response.drives[0].strategy == "single-open"
    assert not any(route.path.startswith("/rip/execute") for route in router.routes)


def test_api_rejects_duplicate_report_paths(tmp_path):
    report = _report(tmp_path)
    request = RipPreviewRequest(
        report_paths=[str(report), str(report)],
        media_contexts={"disc-01": {"series_name": "Test Show", "season": 1}},
    )

    with pytest.raises(HTTPException, match="unique"):
        preview_rip(request)


def test_api_persists_authorizes_and_queues_without_executor(tmp_path):
    report = _report(tmp_path)
    store = OrchestrationStore(tmp_path / "jobs.sqlite3")
    private_store = PrivateBindingStore(tmp_path / "private-bindings.sqlite3")
    output_root = tmp_path / "output"
    output_root.mkdir()
    request = RipPreviewRequest(
        report_paths=[str(report)],
        media_contexts={"disc-01": {"series_name": "Test Show", "season": 1}},
        output_root=str(output_root),
    )

    created = create_rip_job(
        request,
        "create-api-request-0001",
        store,
        private_store,
    )
    authorized = authorize_rip_job(
        str(created["job_id"]),
        AuthorizeJobRequest(
            expected_plan_sha256=str(created["plan_sha256"]),
            confirm_authorization=True,
        ),
        "authorize-api-request-0001",
        store,
    )
    queued = start_rip_job(
        str(created["job_id"]),
        StartJobRequest(confirm_queue=True),
        "start-api-request-0001",
        store,
    )

    assert created["state"] == "awaiting_review"
    assert authorized["state"] == "authorized"
    assert queued["state"] == "queued"
    assert queued["executor_attached"] is False
    assert len(store.list_events(str(created["job_id"]))) == 3
    assert private_store.get(str(created["job_id"])).output_root == output_root
    serialized = json.dumps(created)
    assert str(report) not in serialized
    assert str(output_root) not in serialized
