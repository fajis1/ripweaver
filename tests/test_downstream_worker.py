import json
from types import SimpleNamespace

from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker


def test_automatic_analysis_rechecks_complete_disc_when_one_old_item_failed(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    items = []
    for index, review_code in enumerate((
        "unmatched_disc_analysis_required",
        "all_season_analysis_failed",
    )):
        contract = tmp_path / f"item-{index}.json"
        contract.write_text(
            json.dumps({
                "disc_fingerprint": fingerprint,
                "media_context": {"series_name": "Faerie Tale Theatre"},
            }),
            encoding="utf-8",
        )
        items.append(
            SimpleNamespace(
                media_id=f"media-{index}",
                stage="identify",
                state="review_required",
                review_code=review_code,
                artifact=SimpleNamespace(contract_path=contract),
            )
        )
    store = SimpleNamespace(list_items=lambda: items)
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )
    captured = []
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(automatic_processing_enabled=True)
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.automatic_rip._resolve_automatic_unmatched_disc",
        lambda media_ids, *_args: captured.append(media_ids),
    )

    assert worker._apply_automatic_disc_analysis() is True
    assert worker._apply_automatic_disc_analysis() is False
    assert captured == [("media-0", "media-1")]


def test_automatic_analysis_waits_for_every_disc_title_to_settle(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    items = []
    for index, state in enumerate(("review_required", "review_required", "queued")):
        contract = tmp_path / f"pending-{index}.json"
        contract.write_text(
            json.dumps({
                "disc_fingerprint": fingerprint,
                "media_context": {"series_name": "The Flintstones"},
            }),
            encoding="utf-8",
        )
        items.append(
            SimpleNamespace(
                media_id=f"media-{index}",
                stage="identify",
                state=state,
                review_code=(
                    "unmatched_disc_analysis_required"
                    if state == "review_required"
                    else None
                ),
                artifact=SimpleNamespace(contract_path=contract),
            )
        )
    store = SimpleNamespace(list_items=lambda: items)
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )
    captured = []
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(automatic_processing_enabled=True)
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.automatic_rip._resolve_automatic_unmatched_disc",
        lambda media_ids, *_args: captured.append(media_ids),
    )

    assert worker._apply_automatic_disc_analysis() is False
    items[2].state = "review_required"
    items[2].review_code = "unmatched_disc_analysis_required"
    assert worker._apply_automatic_disc_analysis() is True
    assert captured == [("media-0", "media-1", "media-2")]


def test_automatic_analysis_waits_for_expected_titles_not_yet_admitted(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    items = []
    for title_index in (1, 7):
        contract = tmp_path / f"expected-{title_index}.json"
        contract.write_text(
            json.dumps({
                "disc_fingerprint": fingerprint,
                "disc_expected_title_indexes": [1, 7, 8],
                "media_context": {"series_name": "The Flintstones"},
            }),
            encoding="utf-8",
        )
        items.append(SimpleNamespace(
            media_id=f"show--disc-01-{fingerprint}-title-{title_index:03d}",
            stage="identify",
            state="review_required",
            review_code="unmatched_disc_analysis_required",
            artifact=SimpleNamespace(contract_path=contract),
        ))
    store = SimpleNamespace(list_items=lambda: items)
    worker = DownstreamWorker(SimpleNamespace(store=store), allowed_stages=("identify",))
    captured = []
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(load=lambda: SimpleNamespace(automatic_processing_enabled=True)),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.automatic_rip._resolve_automatic_unmatched_disc",
        lambda media_ids, *_args: captured.append(media_ids),
    )

    assert worker._apply_automatic_disc_analysis() is False
    items.append(SimpleNamespace(
        media_id=f"show--disc-01-{fingerprint}-title-008",
        stage="identify",
        state="review_required",
        review_code="unmatched_disc_analysis_required",
        artifact=items[0].artifact,
    ))
    assert worker._apply_automatic_disc_analysis() is True
    assert captured == [(items[0].media_id, items[1].media_id, items[2].media_id)]


def test_post_item_automation_checks_disc_before_queue_becomes_idle(monkeypatch):
    item = SimpleNamespace(media_id="media-1", review_code=None)
    worker = DownstreamWorker(
        SimpleNamespace(store=object()), allowed_stages=("identify",)
    )
    calls = []
    monkeypatch.setattr(
        worker,
        "_apply_automatic_fallback",
        lambda selected: calls.append(("item", selected.media_id)),
    )
    monkeypatch.setattr(
        worker,
        "_apply_automatic_disc_analysis",
        lambda: calls.append(("disc", None)) or True,
    )

    assert worker._apply_post_item_automation(item) is True
    assert calls == [("item", "media-1"), ("disc", None)]


def test_automatic_transcode_uses_resolution_profiles_and_starts_once(monkeypatch):
    store = object()
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )
    plan = SimpleNamespace(
        plan_sha256="a" * 64,
        media_ids=("media-1", "media-2"),
    )
    profiles = object()
    contract_root = object()
    calls = []

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(automatic_processing_enabled=True)
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_handbrake_profile_store",
        lambda: profiles,
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: contract_root,
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.transcode_authorization.build_transcode_authorization_plan",
        lambda selected_store, selected_profiles, config, profile_id: (
            calls.append(("plan", selected_store, selected_profiles, profile_id))
            or plan
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.routers.rip.authorize_transcode_batch",
        lambda request,
        selected_store,
        selected_profiles,
        selected_contract_root: calls.append((
            "authorize",
            request,
            selected_store,
            selected_profiles,
            selected_contract_root,
        )),
    )

    assert worker._start_automatic_transcode_if_ready() is True
    assert worker._start_automatic_transcode_if_ready() is False

    authorize = next(call for call in calls if call[0] == "authorize")
    request = authorize[1]
    assert request.profile_id is None
    assert request.confirm_transcode is True
    assert request.authorized_item_count == 2
    assert authorize[2:] == (store, profiles, contract_root)


def test_automatic_transcode_does_not_redispatch_remaining_batch(monkeypatch):
    queued = {
        "media-1": SimpleNamespace(stage="organize", state="queued"),
        "media-2": SimpleNamespace(stage="transcode", state="queued"),
    }
    store = SimpleNamespace(get=lambda media_id: queued[media_id])
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )
    worker._automatic_transcode_media_ids = ("media-1", "media-2")
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(automatic_processing_enabled=True)
        ),
    )

    assert worker._start_automatic_transcode_if_ready() is False


def test_automatic_transcode_is_disabled_with_automatic_processing(monkeypatch):
    worker = DownstreamWorker(
        SimpleNamespace(store=object()), allowed_stages=("identify",)
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(automatic_processing_enabled=False)
        ),
    )

    assert worker._start_automatic_transcode_if_ready() is False
