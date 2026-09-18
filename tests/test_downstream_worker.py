import json
from types import SimpleNamespace

from mkv_episode_matcher.backend.downstream_worker import (
    DownstreamWorker,
    _contract_disc_title_identity,
)


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


def test_recovered_downstream_item_reads_lineage_from_saved_rip_artifact(tmp_path):
    fingerprint = "0123456789abcdef"
    current = tmp_path / "identified.json"
    current.write_text(
        json.dumps({"mode": "identified-episode-contract"}), encoding="utf-8"
    )
    rip = tmp_path / "recovered.verified-rip.json"
    rip.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": fingerprint,
            "title_index": 7,
        }),
        encoding="utf-8",
    )
    item = SimpleNamespace(
        media_id="Disc_Name_t06-recovery-deadbeef",
        artifact=SimpleNamespace(contract_path=current),
    )
    store = SimpleNamespace(
        rip_artifact=lambda _media_id: SimpleNamespace(contract_path=rip)
    )

    assert _contract_disc_title_identity(item, store) == (fingerprint, 7)


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


def test_automatic_analysis_retries_failed_only_disc_once_per_restart(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    items = []
    for title_index, review_code in (
        (3, "all_season_analysis_failed"),
        (4, "gemini_analysis_failed"),
        (5, "gemini_provider_failed"),
    ):
        contract = tmp_path / f"failed-{title_index}.json"
        contract.write_text(
            json.dumps({
                "disc_fingerprint": fingerprint,
                "title_index": title_index,
                "disc_expected_title_indexes": [3, 4, 5],
                "media_context": {"series_name": "The Office", "season": 7},
            }),
            encoding="utf-8",
        )
        items.append(
            SimpleNamespace(
                media_id=f"show--disc-01-{fingerprint}-title-{title_index:03d}",
                stage="identify",
                state="review_required",
                review_code=review_code,
                artifact=SimpleNamespace(contract_path=contract),
            )
        )
    store = SimpleNamespace(
        list_items=lambda: items,
        silent_video_review_flags=lambda: {},
        disc_matching_scope=lambda _fingerprint: (3, 4, 5),
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
    assert captured == [tuple(item.media_id for item in items)]


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


def test_automatic_analysis_uses_contract_identity_for_recovered_media_ids(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    items = []
    for title_index in (1, 2):
        contract = tmp_path / f"recovered-{title_index}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "disc_fingerprint": fingerprint,
                "title_index": title_index,
                "disc_expected_title_indexes": [1, 2],
                "media_context": {"series_name": "The Office", "season": None},
            }),
            encoding="utf-8",
        )
        items.append(
            SimpleNamespace(
                media_id=f"Disc_Name_t{title_index:02d}-recovery-deadbeef{title_index}",
                stage="identify",
                state="review_required",
                review_code="unmatched_disc_analysis_required",
                artifact=SimpleNamespace(contract_path=contract),
                created_at=f"2026-08-24T00:00:0{title_index}Z",
                updated_at=f"2026-08-24T00:00:0{title_index}Z",
            )
        )
    store = SimpleNamespace(
        list_items=lambda: items,
        silent_video_review_flags=lambda: {},
        disc_matching_scope=lambda _fingerprint: (1, 2),
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



def test_m5_sibling_revision_idempotent_retry(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
    from mkv_episode_matcher.backend.gemini_fallback import GeminiTitleOutcome, GeminiFallbackOutcome
    from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
    import json

    config_mock = SimpleNamespace(automatic_processing_enabled=True, automatic_gemini_ambiguity_fallback=True)
    monkeypatch.setattr("mkv_episode_matcher.backend.downstream_worker.get_config_manager", lambda: type("m", (), {"load": lambda self: config_mock})())


    def fake_execute_gemini_fallback(store, media_ids, *args, **kwargs):
        contract_path = tmp_path / f"{media_ids[0]}_new.json"
        contract_path.write_text("{}", encoding="utf-8")
        from mkv_episode_matcher.pipeline_queue import build_artifact
        store.apply_reviewed_identification_input(media_ids[0], build_artifact("rip", contract_path))
        return GeminiFallbackOutcome(handled_ids=media_ids, titles=(GeminiTitleOutcome(media_id=media_ids[0], disposition="matched", accepted_role="movie"),))

    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root", lambda: tmp_path)

    store = PipelineQueueStore(tmp_path / "test.db")
    media_id = "test-media"
    
    initial_assessment = DiscAssessment("0"*16, (1,2,3))
    initial_assessment = store.routing_append(initial_assessment, expected_revision=0)

    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0"*16,
            "title_index": 1,
            "media_context": {
                "routing_assessment": initial_assessment.to_dict(),
                "routing_assessment_digest": initial_assessment.digest,
                "routing_assessment_revision": initial_assessment.revision
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "gemini_evidence_required")
    media_id = store.list_items()[0].media_id

    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_queue_store", lambda: store)

    worker = DownstreamWorker(SimpleNamespace(store=store), allowed_stages=("identify",))

    real_routing_append = store.routing_append
    
    call_count = 0
    def mock_routing_append(assessment, expected_revision=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            from dataclasses import replace
            sibling_assessment = replace(initial_assessment, revision=initial_assessment.revision + 1, evidence=(TitleEvidence(title_index=1, source="content", status="supported", role="movie"),))
            real_routing_append(sibling_assessment, expected_revision=initial_assessment.revision)
        return real_routing_append(assessment, expected_revision=expected_revision)
        
    store.routing_append = mock_routing_append

    import unittest.mock as mock
    with mock.patch("mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback", fake_execute_gemini_fallback):
        with mock.patch("mkv_episode_matcher.backend.downstream_worker._verified_gemini_route_assignment", return_value=True):
            assert worker._apply_automatic_assessed_gemini_route() is True
            assert store.get(media_id).state == "queued"

def test_m5_sibling_revision_idempotent_retry_fail(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
    from mkv_episode_matcher.backend.gemini_fallback import GeminiTitleOutcome, GeminiFallbackOutcome
    from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence, RoutingError
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
    import json

    config_mock = SimpleNamespace(automatic_processing_enabled=True, automatic_gemini_ambiguity_fallback=True)
    monkeypatch.setattr("mkv_episode_matcher.backend.downstream_worker.get_config_manager", lambda: type("m", (), {"load": lambda self: config_mock})())

    def fake_execute_gemini_fallback(store, media_ids, *args, **kwargs):
        contract_path = tmp_path / f"{media_ids[0]}_new.json"
        contract_path.write_text("{}", encoding="utf-8")
        from mkv_episode_matcher.pipeline_queue import build_artifact
        store.apply_reviewed_identification_input(media_ids[0], build_artifact("rip", contract_path))
        return GeminiFallbackOutcome(handled_ids=media_ids, titles=(GeminiTitleOutcome(media_id=media_ids[0], disposition="matched", accepted_role="movie"),))

    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root", lambda: tmp_path)

    store = PipelineQueueStore(tmp_path / "test.db")
    media_id = "test-media"
    
    initial_assessment = DiscAssessment("0"*16, (1,2,3))
    initial_assessment = store.routing_append(initial_assessment, expected_revision=0)

    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0"*16,
            "title_index": 1,
            "media_context": {
                "routing_assessment": initial_assessment.to_dict(),
                "routing_assessment_digest": initial_assessment.digest,
                "routing_assessment_revision": initial_assessment.revision
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "gemini_evidence_required")

    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_queue_store", lambda: store)

    worker = DownstreamWorker(SimpleNamespace(store=store), allowed_stages=("identify",))

    real_routing_append = store.routing_append
    
    call_count = 0
    def mock_routing_append(assessment, expected_revision=None):
        nonlocal call_count
        call_count += 1
        from dataclasses import replace
        current = store.routing_latest("0"*16)
        sibling = replace(current, revision=current.revision + 1, evidence=current.evidence + (TitleEvidence(title_index=100+call_count, source="content", status="supported", role="tv"),))
        real_routing_append(sibling, expected_revision=current.revision)
        raise RoutingError("Routing revision is stale")
        
    store.routing_append = mock_routing_append

    import unittest.mock as mock
    with mock.patch("mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback", fake_execute_gemini_fallback):
        with mock.patch("mkv_episode_matcher.backend.downstream_worker._verified_gemini_route_assignment", return_value=True):
            assert worker._apply_automatic_assessed_gemini_route() is False
def test_manual_endpoints_queue_transition_lock(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
    from mkv_episode_matcher.backend.routers.rip import execute_pipeline_gemini_fallback, GeminiFallbackExecutionRequest
    from mkv_episode_matcher.backend.automatic_rip import _downstream_lock
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
    import json
    import threading
    import time
    import unittest.mock as mock

    store = PipelineQueueStore(tmp_path / "test.db")
    media_id = "test-media"
    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0"*16,
            "title_index": 1,
            "media_context": {},
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "gemini_evidence_required")

    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root", lambda: tmp_path)

    _downstream_lock.acquire()
    try:
        request = GeminiFallbackExecutionRequest(media_ids=[media_id], confirm_media_read=True, confirm_external_transmission=True, confirm_classification=True, disc_fingerprint="0"*16)
        
        with mock.patch("mkv_episode_matcher.backend.routers.rip.execute_gemini_fallback") as mock_exec:
            mock_exec.side_effect = lambda store, ids, *a: [ids[0]]
            response = execute_pipeline_gemini_fallback(request, store, tmp_path)
            assert store.get(media_id).review_code == "gemini_evidence_required"
            
            _downstream_lock.release()
            time.sleep(0.1)
            
            assert store.get(media_id).review_code == "gemini_analysis_running" or store.get(media_id).state != "review_required"
    except Exception:
        if _downstream_lock.locked():
            _downstream_lock.release()
        raise
