from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mkv_episode_matcher.backend.routers.rip import (
    _filter_dashboard_jobs,
    _filter_dashboard_pipeline_items,
    _parse_dashboard_disc_fingerprints,
)
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact


def _job(*, state: str, fingerprint: str) -> SimpleNamespace:
    return SimpleNamespace(
        state=state,
        preview={
            "jobs": [
                {"staging_destination": (f"disc-01/{fingerprint}/title-001/source.mkv")}
            ]
        },
    )


def _item(*, state: str, fingerprint: str) -> SimpleNamespace:
    return SimpleNamespace(
        state=state,
        media_id=f"series--disc-01-{fingerprint}-title-001",
    )


def test_dashboard_disc_scope_is_bounded_validated_and_deduplicated() -> None:
    assert _parse_dashboard_disc_fingerprints(
        "a5c6de13f86cc16b,a5c6de13f86cc16b,5b2c2f4143e17730"
    ) == ("a5c6de13f86cc16b", "5b2c2f4143e17730")

    with pytest.raises(HTTPException, match="Dashboard disc scope is invalid"):
        _parse_dashboard_disc_fingerprints("not-a-fingerprint")


def test_dashboard_job_scope_keeps_selected_disc_and_all_active_work() -> None:
    selected = _job(state="failed", fingerprint="a5c6de13f86cc16b")
    other_active = _job(state="running", fingerprint="5b2c2f4143e17730")
    unrelated_history = _job(state="completed", fingerprint="1111111111111111")

    assert _filter_dashboard_jobs(
        (selected, other_active, unrelated_history),
        scope="dashboard",
        disc_fingerprints=("a5c6de13f86cc16b",),
    ) == (selected, other_active)


def test_active_job_scope_does_not_serialize_completed_history() -> None:
    running = _job(state="running", fingerprint="a5c6de13f86cc16b")
    paused = _job(state="paused", fingerprint="5b2c2f4143e17730")
    completed = _job(state="completed", fingerprint="1111111111111111")

    assert _filter_dashboard_jobs(
        (running, paused, completed), scope="active", disc_fingerprints=()
    ) == (running, paused)


def test_pipeline_scopes_return_only_items_visible_in_each_view() -> None:
    selected_completed = _item(state="completed", fingerprint="a5c6de13f86cc16b")
    selected_review = _item(state="review_required", fingerprint="a5c6de13f86cc16b")
    other_running = _item(state="running", fingerprint="5b2c2f4143e17730")
    unrelated_completed = _item(state="completed", fingerprint="1111111111111111")
    items = (
        selected_completed,
        selected_review,
        other_running,
        unrelated_completed,
    )

    assert _filter_dashboard_pipeline_items(
        items,
        scope="dashboard",
        disc_fingerprints=("a5c6de13f86cc16b",),
    ) == (selected_completed, selected_review)
    assert _filter_dashboard_pipeline_items(
        items, scope="attention", disc_fingerprints=()
    ) == (selected_review,)
    assert _filter_dashboard_pipeline_items(
        items, scope="active", disc_fingerprints=()
    ) == (selected_review, other_running)


def test_empty_dashboard_disc_scope_keeps_only_actionable_pipeline_work() -> None:
    running = _item(state="running", fingerprint="a5c6de13f86cc16b")
    completed = _item(state="completed", fingerprint="5b2c2f4143e17730")

    assert _filter_dashboard_pipeline_items(
        (running, completed), scope="dashboard", disc_fingerprints=()
    ) == (running,)


def test_pipeline_list_decodes_all_rows_without_per_item_gets(
    tmp_path, monkeypatch
) -> None:
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    contract = tmp_path / "verified-rip.json"
    contract.write_text("{}", encoding="utf-8")
    store.enqueue_verified_rip("polling-title-001", build_artifact("rip", contract))

    def fail_per_item_get(_media_id: str):
        raise AssertionError("list_items opened a per-item query")

    monkeypatch.setattr(store, "get", fail_per_item_get)

    assert [item.media_id for item in store.list_items()] == ["polling-title-001"]
