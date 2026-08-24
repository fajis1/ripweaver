import json
from types import SimpleNamespace

from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker


def test_worker_quarantines_legacy_sequence_only_downstream_assignments(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    sequence_transcode = f"show--disc-01-{fingerprint}-title-001"
    confirmed_transcode = f"show--disc-01-{fingerprint}-title-002"
    sequence_organize = f"show--disc-01-{fingerprint}-title-003"
    items = []
    for media_id, stage in (
        (sequence_transcode, "transcode"),
        (confirmed_transcode, "transcode"),
        (sequence_organize, "organize"),
    ):
        contract = tmp_path / f"{media_id}.json"
        contract.write_text(
            json.dumps({
                "mode": "identified-episode-contract",
                "identification_order": ["reviewed-release-catalogue"],
            }),
            encoding="utf-8",
        )
        items.append(
            SimpleNamespace(
                media_id=media_id,
                stage=stage,
                state="queued",
                artifact=SimpleNamespace(contract_path=contract),
            )
        )

    restarted = []
    held = []
    store = SimpleNamespace(
        list_items=lambda: items,
        restart_identification=lambda media_id, **kwargs: restarted.append((
            media_id,
            kwargs,
        )),
        hold_for_review=lambda media_id, code: held.append((media_id, code)),
    )
    attempts = {
        sequence_transcode: ({"branch": "tv-local", "disposition": "matched"},),
        confirmed_transcode: (
            {"branch": "tv-local", "disposition": "matched"},
            {"branch": "tv-opensubtitles", "disposition": "matched"},
        ),
        sequence_organize: ({"branch": "tv-local", "disposition": "matched"},),
    }

    class FakeDossier:
        def __init__(self, _root):
            pass

        @staticmethod
        def safe_attempts(media_id):
            return attempts[media_id]

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path / "contracts",
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.identification_dossier.IdentificationDossierStore",
        FakeDossier,
    )
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify", "transcode")
    )

    assert worker._reconcile_legacy_sequence_assignments() is True
    assert worker._reconcile_legacy_sequence_assignments() is False
    assert restarted == [
        (
            sequence_transcode,
            {
                "expected_disc_fingerprint": fingerprint,
                "expected_title_index": 1,
            },
        )
    ]
    assert held == [(sequence_organize, "legacy_sequence_assignment_review_required")]


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
    store = SimpleNamespace(
        list_items=lambda: items, silent_video_review_flags=lambda: {}
    )
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


def test_automatic_analysis_runs_for_one_remaining_unresolved_title(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    contract = tmp_path / "singleton.json"
    contract.write_text(
        json.dumps({
            "disc_fingerprint": fingerprint,
            "disc_expected_title_indexes": [1, 2, 3],
            "media_context": {"series_name": "The Office", "season": 6},
        }),
        encoding="utf-8",
    )
    completed = [
        SimpleNamespace(
            media_id=f"show--disc-01-{fingerprint}-title-{index:03d}",
            stage="organize",
            state="completed",
            review_code=None,
            artifact=SimpleNamespace(contract_path=contract),
        )
        for index in (1, 2)
    ]
    unresolved = SimpleNamespace(
        media_id=f"show--disc-01-{fingerprint}-title-003",
        stage="identify",
        state="review_required",
        review_code="episode_match_review",
        artifact=SimpleNamespace(contract_path=contract),
    )
    items = completed + [unresolved]
    store = SimpleNamespace(
        list_items=lambda: items,
        silent_video_review_flags=lambda: {},
    )
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
    assert captured == [(unresolved.media_id,)]


def test_descriptive_review_retries_after_another_same_disc_title_resolves(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    contract = tmp_path / "descriptive.json"
    contract.write_text(
        json.dumps({
            "disc_fingerprint": fingerprint,
            "disc_expected_title_indexes": [1, 2, 3],
            "media_context": {"series_name": "The Office", "season": 7},
        }),
        encoding="utf-8",
    )
    resolved = [
        SimpleNamespace(
            media_id=f"show--disc-01-{fingerprint}-title-001",
            stage="organize",
            state="completed",
            review_code=None,
            artifact=SimpleNamespace(contract_path=contract),
        )
    ]
    unresolved = SimpleNamespace(
        media_id=f"show--disc-01-{fingerprint}-title-003",
        stage="identify",
        state="review_required",
        review_code="gemini_descriptive_review_required",
        artifact=SimpleNamespace(contract_path=contract),
    )
    sibling = SimpleNamespace(
        media_id=f"show--disc-01-{fingerprint}-title-002",
        stage="identify",
        state="review_required",
        review_code="episode_match_review",
        artifact=SimpleNamespace(contract_path=contract),
    )
    items = resolved + [sibling, unresolved]
    store = SimpleNamespace(
        list_items=lambda: items,
        silent_video_review_flags=lambda: {},
    )
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

    sibling.stage = "transcode"
    sibling.state = "queued"
    sibling.review_code = None

    assert worker._apply_automatic_disc_analysis() is True
    assert worker._apply_automatic_disc_analysis() is False
    assert captured == [
        (sibling.media_id, unresolved.media_id),
        (unresolved.media_id,),
    ]


def test_independent_evidence_hold_still_runs_local_disc_fallback_without_gemini(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    contract = tmp_path / "independent.json"
    contract.write_text(
        json.dumps({
            "disc_fingerprint": fingerprint,
            "media_context": {"series_name": "Example Show", "season": 1},
        }),
        encoding="utf-8",
    )
    item = SimpleNamespace(
        media_id=f"show--disc-01-{fingerprint}-title-001",
        stage="identify",
        state="review_required",
        review_code="independent_episode_evidence_required",
        artifact=SimpleNamespace(contract_path=contract),
    )
    store = SimpleNamespace(
        list_items=lambda: [item],
        silent_video_review_flags=lambda: {},
    )
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )
    captured = []
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                automatic_processing_enabled=True,
                automatic_gemini_ambiguity_fallback=False,
            )
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
    assert captured == [(item.media_id,)]


def test_automatic_analysis_waits_for_every_disc_title_to_settle(tmp_path, monkeypatch):
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
    store = SimpleNamespace(
        list_items=lambda: items, silent_video_review_flags=lambda: {}
    )
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


def test_automatic_analysis_uses_prepared_relevant_matching_scope(
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
        items.append(
            SimpleNamespace(
                media_id=f"show--disc-01-{fingerprint}-title-{title_index:03d}",
                stage="identify",
                state="review_required",
                review_code="unmatched_disc_analysis_required",
                artifact=SimpleNamespace(contract_path=contract),
            )
        )
    store = SimpleNamespace(
        list_items=lambda: items, silent_video_review_flags=lambda: {}
    )
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
    store.disc_matching_scope = lambda _fingerprint: (1, 7)
    assert worker._apply_automatic_disc_analysis() is True
    assert captured == [(items[0].media_id, items[1].media_id)]


def test_automatic_analysis_coordinates_newest_episode_review_lineages(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    expected_indexes = [2, 4, 5]

    def queue_item(title_index, *, suffix="", state="review_required", updated_at):
        media_id = f"show--disc-01-{fingerprint}-title-{title_index:03d}{suffix}"
        contract = tmp_path / f"{title_index}{suffix or '-original'}.json"
        contract.write_text(
            json.dumps({
                "disc_fingerprint": fingerprint,
                "disc_expected_title_indexes": expected_indexes,
                "title_index": title_index,
            }),
            encoding="utf-8",
        )
        return SimpleNamespace(
            media_id=media_id,
            stage="identify",
            state=state,
            review_code=(
                "episode_match_review" if state == "review_required" else None
            ),
            artifact=SimpleNamespace(contract_path=contract),
            created_at=updated_at,
            updated_at=updated_at,
        )

    original_2 = queue_item(2, updated_at="2026-08-16T10:00:00Z")
    recovery_2 = queue_item(
        2,
        suffix="-recovery-newer",
        updated_at="2026-08-16T10:05:00Z",
    )
    original_4 = queue_item(4, updated_at="2026-08-16T10:00:00Z")
    recovery_4 = queue_item(
        4,
        suffix="-recovery-newer",
        state="queued",
        updated_at="2026-08-16T10:05:00Z",
    )
    current_5 = queue_item(5, updated_at="2026-08-16T10:05:00Z")
    items = [original_2, recovery_2, original_4, recovery_4, current_5]
    store = SimpleNamespace(
        list_items=lambda: items,
        silent_video_review_flags=lambda: {},
    )
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
    recovery_4.state = "review_required"
    recovery_4.review_code = "episode_match_review"
    assert worker._apply_automatic_disc_analysis() is True
    assert captured == [(recovery_2.media_id, recovery_4.media_id, current_5.media_id)]


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


def test_automatic_analysis_retries_old_sequence_hold_without_visual_result(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    items = []
    for title_index in (1, 2):
        contract = tmp_path / f"visual-retry-{title_index}.json"
        contract.write_text(
            json.dumps({"disc_fingerprint": fingerprint}), encoding="utf-8"
        )
        items.append(
            SimpleNamespace(
                media_id=f"show--disc-01-{fingerprint}-title-{title_index:03d}",
                stage="identify",
                state="review_required",
                review_code="all_season_sequence_review_required",
                artifact=SimpleNamespace(contract_path=contract),
            )
        )
    store = SimpleNamespace(
        list_items=lambda: items,
        silent_video_review_flags=lambda: {},
    )
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )
    captured = []
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                automatic_processing_enabled=True,
                automatic_gemini_ambiguity_fallback=True,
            )
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
    assert captured == [(items[0].media_id, items[1].media_id)]


def test_automatic_pipeline_requeues_prior_broad_episode_collision(monkeypatch):
    item = SimpleNamespace(
        media_id="media-1",
        stage="identify",
        state="review_required",
        review_code="library_collision",
    )
    retried = []
    store = SimpleNamespace(
        list_items=lambda: [item],
        retry=lambda media_id: retried.append(media_id),
    )
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify", "organize")
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                automatic_processing_enabled=True,
                automatic_organization_enabled=True,
            )
        ),
    )

    assert worker._resume_version_coexistence_reviews() is True
    assert worker._resume_version_coexistence_reviews() is False
    assert retried == ["media-1"]


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
