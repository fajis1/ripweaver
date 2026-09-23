import json
from types import SimpleNamespace

import pytest

from mkv_episode_matcher.backend.downstream_worker import (
    DownstreamWorker,
    _contract_disc_title_identity,
    _gemini_route_assignment_decision,
)
from mkv_episode_matcher.backend.gemini_fallback import (
    GeminiFallbackOutcome,
    GeminiTitleOutcome,
)
from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact


def test_worker_settles_one_actual_gemini_route_result(tmp_path, monkeypatch):
    fingerprint = "0123456789abcdef"
    media_id = "disc-01-title-000"
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(fingerprint, (0,), user_hint="tv"), expected_revision=0
    )
    original = tmp_path / "original.json"
    original.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": fingerprint,
            "title_index": 0,
            "media_context": {
                "routing_assessment": assessment.to_dict(),
                "routing_assessment_digest": assessment.digest,
                "routing_assessment_revision": assessment.revision,
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", original))
    store.hold_for_review(media_id, "content_classification_required")
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
        "mkv_episode_matcher.backend.dependencies.get_engine",
        lambda: SimpleNamespace(asr=object()),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path / "contracts",
    )
    calls = []

    def fake_gemini(_store, ids, _config, _asr, _root, *, return_outcomes):
        calls.append(ids)
        assert return_outcomes is True
        revised = tmp_path / "revised.json"
        revised_payload = json.loads(original.read_text(encoding="utf-8"))
        revised_payload["media_context"]["special_feature_assignments"] = [
            {
                "title_index": 0,
                "classification": "matched-feature",
                "media_kind": "movie",
                "provisional_match": False,
                "tmdb_movie_id": 123,
                "matched_title": "Example Movie",
                "identity_verification_status": "exact_verified",
                "identification_method": "movie-opensubtitles",
            }
        ]
        revised.write_text(json.dumps(revised_payload), encoding="utf-8")
        _store.apply_reviewed_identification_input(
            media_id, build_artifact("rip", revised)
        )
        return GeminiFallbackOutcome(
            (media_id,), (GeminiTitleOutcome(media_id, "matched", "movie"),)
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback",
        fake_gemini,
    )
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )
    store.set_paused(True)
    assert worker._apply_automatic_assessed_gemini_route() is False
    store.set_paused(False)
    worker._stop.set()
    assert worker._apply_automatic_assessed_gemini_route() is False
    worker._stop.clear()
    assert worker._apply_automatic_assessed_gemini_route() is True
    assert worker._apply_automatic_assessed_gemini_route() is False
    assert calls == [(media_id,)]
    assert store.routing_attempts(fingerprint, 0)[0].outcome == "matched"
    assert store.get(media_id).state == "queued"


@pytest.mark.parametrize(
    ("provider_result", "expected_outcome", "expected_review_code"),
    [
        ("no_match", "no_match", "gemini_descriptive_review_required"),
        ("review", "review", "gemini_descriptive_review_required"),
        ("failure", "service_failed", "gemini_provider_failed"),
        ("typed_failure", "service_failed", "gemini_provider_failed"),
        ("partial_failure", "review", "gemini_analysis_failed"),
        (
            "provisional_movie",
            "matched",
            "provisional_content_identity_review_required",
        ),
        (
            "provisional_extra",
            "matched",
            "provisional_content_identity_review_required",
        ),
        ("unapplied_match", "review", "gemini_evidence_required"),
    ],
)
def test_worker_distinguishes_gemini_route_results(
    tmp_path, monkeypatch, provider_result, expected_outcome, expected_review_code
):
    fingerprint = "0123456789abcdef"
    media_id = "disc-01-title-000"
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(fingerprint, (0,), user_hint="movie"), expected_revision=0
    )
    contract = tmp_path / "rip.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": fingerprint,
            "title_index": 0,
            "media_context": {
                "routing_assessment": assessment.to_dict(),
                "routing_assessment_digest": assessment.digest,
                "routing_assessment_revision": assessment.revision,
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "content_classification_required")
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
        "mkv_episode_matcher.backend.dependencies.get_engine",
        lambda: SimpleNamespace(asr=object()),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path / "contracts",
    )

    def fake_gemini(_store, ids, _config, _asr, _root, *, return_outcomes):
        assert ids == (media_id,)
        assert return_outcomes is True
        if provider_result == "failure":
            raise RuntimeError("synthetic provider outage")
        if provider_result == "partial_failure":
            revised = tmp_path / "partial.json"
            revised.write_text(contract.read_text(encoding="utf-8"), encoding="utf-8")
            _store.apply_reviewed_identification_input(
                media_id, build_artifact("rip", revised)
            )
            raise RuntimeError("synthetic failure after contract application")
        if provider_result in {"provisional_movie", "provisional_extra"}:
            role = provider_result.removeprefix("provisional_")
            revised = tmp_path / "provisional.json"
            revised_payload = json.loads(contract.read_text(encoding="utf-8"))
            revised_payload["media_context"]["special_feature_assignments"] = [
                {
                    "title_index": 0,
                    "classification": "matched-feature",
                    "media_kind": role,
                    "provisional_match": True,
                }
            ]
            revised.write_text(json.dumps(revised_payload), encoding="utf-8")
            _store.apply_reviewed_identification_input(
                media_id, build_artifact("rip", revised)
            )
            return GeminiFallbackOutcome(
                (media_id,), (GeminiTitleOutcome(media_id, "matched", role),)
            )
        if provider_result in {"no_match", "review"}:
            _store.choose_review_path(media_id, "gemini_descriptive_review_required")
        if provider_result == "typed_failure":
            return GeminiFallbackOutcome(
                (), (GeminiTitleOutcome(media_id, "service_failed"),)
            )
        disposition = (
            "matched" if provider_result == "unapplied_match" else provider_result
        )
        return GeminiFallbackOutcome((), (GeminiTitleOutcome(media_id, disposition),))

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback",
        fake_gemini,
    )
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )
    assert worker._apply_automatic_assessed_gemini_route() is True
    assert store.routing_attempts(fingerprint, 0)[0].outcome == expected_outcome
    assert store.get(media_id).review_code == expected_review_code
    if provider_result in {"provisional_movie", "provisional_extra"}:
        latest = store.routing_latest(fingerprint)
        assert latest is not None
        assert latest.title_roles()[0].role == provider_result.removeprefix(
            "provisional_"
        )
        restarted = PipelineQueueStore(store.database_path)
        assert restarted.routing_latest(fingerprint) == latest
        assert restarted.get(media_id).review_code == (
            "provisional_content_identity_review_required"
        )
    if provider_result != "no_match":
        assert worker._apply_automatic_assessed_gemini_route() is False


@pytest.mark.parametrize(
    ("role", "provisional", "expected_status"),
    [
        ("movie", False, "exact_verified"),
        ("movie", True, "exact_pending"),
        ("extra", False, "exact_verified"),
        ("extra", True, "descriptive_pending"),
    ],
)
def test_gemini_route_assignment_separates_role_from_identity(
    tmp_path, role, provisional, expected_status
):
    contract = tmp_path / "assignment.json"
    contract.write_text(
        json.dumps({
            "media_context": {
                "special_feature_assignments": [
                    {
                        "title_index": 3,
                        "classification": "matched-feature",
                        "media_kind": role,
                        "provisional_match": provisional,
                        **(
                            {
                                "tmdb_movie_id": 123,
                                "matched_title": "Example Movie",
                                "identity_verification_status": "exact_verified",
                                "identification_method": "movie-opensubtitles",
                            }
                            if role == "movie" and provisional is False
                            else {}
                        ),
                    }
                ]
            }
        }),
        encoding="utf-8",
    )
    item = SimpleNamespace(artifact=build_artifact("rip", contract))

    decision = _gemini_route_assignment_decision(item, 3, role)

    assert decision.role_accepted is True
    assert decision.identity_status == expected_status


def test_gemini_route_rejects_movie_exact_flag_without_provider_identity(tmp_path):
    contract = tmp_path / "assignment.json"
    contract.write_text(
        json.dumps({
            "media_context": {
                "special_feature_assignments": [
                    {
                        "title_index": 0,
                        "classification": "matched-feature",
                        "media_kind": "movie",
                        "provisional_match": False,
                    }
                ]
            }
        }),
        encoding="utf-8",
    )
    item = SimpleNamespace(artifact=build_artifact("rip", contract))

    decision = _gemini_route_assignment_decision(item, 0, "movie")

    assert decision.role_accepted is False
    assert decision.identity_status == "invalid"


def test_gemini_route_accepts_linked_descriptive_extra_identity(tmp_path):
    contract = tmp_path / "assignment.json"
    contract.write_text(
        json.dumps({
            "media_context": {
                "special_feature_assignments": [
                    {
                        "title_index": 2,
                        "classification": "matched-feature",
                        "media_kind": "extra",
                        "matched_title": "Making Of",
                        "provisional_match": False,
                        "identity_verification_status": "descriptive_accepted",
                        "descriptive_identity_accepted": True,
                        "related_tmdb_movie_id": 123,
                        "identification_method": "gemini-descriptive-extra",
                    }
                ]
            }
        }),
        encoding="utf-8",
    )
    item = SimpleNamespace(artifact=build_artifact("rip", contract))

    decision = _gemini_route_assignment_decision(item, 2, "extra")

    assert decision.role_accepted is True
    assert decision.identity_status == "descriptive_accepted"


def test_eleven_title_movie_with_extras_keeps_every_title_in_routing(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(
            fingerprint,
            tuple(range(11)),
            user_hint="tv",
            evidence=(
                TitleEvidence(0, "database", "supported", "movie"),
                *(
                    TitleEvidence(index, "database", "supported", "extra")
                    for index in range(2, 8)
                ),
            ),
        ),
        expected_revision=0,
    )
    for index in range(11):
        media_id = f"disc-01-title-{index:03d}"
        contract = tmp_path / f"{media_id}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "disc_fingerprint": fingerprint,
                "title_index": index,
                "media_context": {
                    "routing_assessment": assessment.to_dict(),
                    "routing_assessment_digest": assessment.digest,
                    "routing_assessment_revision": assessment.revision,
                },
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        store.hold_for_review(media_id, "content_classification_required")
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
        "mkv_episode_matcher.backend.dependencies.get_engine",
        lambda: SimpleNamespace(asr=object()),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path / "contracts",
    )
    requested = []

    def fake_gemini(_store, ids, _config, _asr, _root, *, return_outcomes):
        assert return_outcomes is True
        requested.append(ids[0])
        _store.choose_review_path(ids[0], "gemini_descriptive_review_required")
        return GeminiFallbackOutcome((), (GeminiTitleOutcome(ids[0], "review"),))

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback",
        fake_gemini,
    )
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )
    assert all(worker._apply_automatic_assessed_gemini_route() for _ in range(11))
    assert worker._apply_automatic_assessed_gemini_route() is False
    assert len(set(requested)) == 11
    assert [
        store.routing_attempts(fingerprint, index)[0].route for index in range(11)
    ] == [
        "movie",
        "classify",
        *("extra" for _ in range(6)),
        "classify",
        "classify",
        "classify",
    ]
    assert all(store.get(media_id).state == "review_required" for media_id in requested)


def test_worker_settles_terminal_tv_route_without_rerouting_uncertainty(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(
            fingerprint,
            (0, 1, 2),
            user_hint="movie",
            evidence=(
                TitleEvidence(0, "database", "supported", "tv"),
                TitleEvidence(1, "database", "supported", "tv"),
                TitleEvidence(2, "database", "supported", "tv"),
            ),
        ),
        expected_revision=0,
    )
    for title_index, review_code in enumerate((
        "all_season_series_not_found",
        "all_season_catalog_unavailable",
        "independent_episode_evidence_required",
    )):
        media_id = f"disc-01-title-{title_index:03d}"
        contract = tmp_path / f"{media_id}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "disc_fingerprint": fingerprint,
                "title_index": title_index,
                "media_context": {
                    "routing_assessment": assessment.to_dict(),
                    "routing_assessment_digest": assessment.digest,
                    "routing_assessment_revision": assessment.revision,
                },
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        store.hold_for_review(media_id, review_code)
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(automatic_processing_enabled=True)
        ),
    )
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )
    store.set_paused(True)
    assert worker._settle_terminal_tv_route() is False
    assert store.routing_attempts(fingerprint, 0) == ()
    store.set_paused(False)
    assert worker._settle_terminal_tv_route() is True
    assert worker._settle_terminal_tv_route() is True
    assert worker._settle_terminal_tv_route() is True
    assert worker._settle_terminal_tv_route() is False
    assert store.routing_attempts(fingerprint, 0)[0].outcome == "review"
    assert store.routing_attempts(fingerprint, 1)[0].outcome == "service_failed"
    assert store.routing_attempts(fingerprint, 2)[0].outcome == "review"
    assert store.routing_claim_next(assessment, 2) is None
    assert store.routing_claim_next(assessment, 1) is None
    assert store.routing_claim_next(assessment, 0) is None


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


def test_genuine_tv_no_match_tries_eligible_movie_route(tmp_path, monkeypatch):
    fingerprint = "0123456789abcdef"
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(
            fingerprint,
            (0,),
            user_hint="movie",
            evidence=(TitleEvidence(0, "database", "supported", "tv"),),
        ),
        expected_revision=0,
    )

    media_id = "disc-01-title-000"
    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": fingerprint,
            "title_index": 0,
            "media_context": {
                "routing_assessment": assessment.to_dict(),
                "routing_assessment_digest": assessment.digest,
                "routing_assessment_revision": assessment.revision,
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "tv_title_no_match")

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
        "mkv_episode_matcher.backend.dependencies.get_engine",
        lambda: SimpleNamespace(asr=object()),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path / "contracts",
    )

    calls = []

    def fake_gemini(_store, ids, _config, _asr, _root, *, return_outcomes):
        calls.append(ids)
        assert return_outcomes is True
        revised = tmp_path / "revised.json"
        revised_payload = json.loads(contract.read_text(encoding="utf-8"))
        revised_payload["media_context"]["special_feature_assignments"] = [
            {
                "title_index": 0,
                "classification": "matched-feature",
                "media_kind": "movie",
                "provisional_match": False,
                "tmdb_movie_id": 123,
                "matched_title": "Example Movie",
                "identity_verification_status": "exact_verified",
                "identification_method": "movie-opensubtitles",
            }
        ]
        revised.write_text(json.dumps(revised_payload), encoding="utf-8")
        _store.apply_reviewed_identification_input(
            media_id, build_artifact("rip", revised)
        )
        from mkv_episode_matcher.backend.gemini_fallback import (
            GeminiFallbackOutcome,
            GeminiTitleOutcome,
        )

        return GeminiFallbackOutcome(
            (media_id,), (GeminiTitleOutcome(media_id, "matched", "movie"),)
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback",
        fake_gemini,
    )

    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )

    # 1. Settle TV route as no_match
    assert worker._settle_terminal_tv_route() is True
    assert store.routing_attempts(fingerprint, 0)[0].outcome == "no_match"

    # 2. Worker claims alternate route (movie) and executes Gemini
    assert worker._apply_automatic_assessed_gemini_route() is True
    assert worker._apply_automatic_assessed_gemini_route() is False

    assert calls == [(media_id,)]
    assert store.routing_attempts(fingerprint, 0)[0].outcome == "matched"
    assert store.routing_attempts(fingerprint, 0)[0].route == "movie"
    assert store.get(media_id).state == "queued"


def test_automatic_gemini_route_aborts_when_paused_or_stopped(tmp_path, monkeypatch):
    fingerprint = "0123456789abcdef"
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(
            fingerprint,
            (0, 1),
            user_hint="movie",
            evidence=(
                TitleEvidence(0, "database", "supported", "tv"),
                TitleEvidence(1, "database", "supported", "tv"),
            ),
        ),
        expected_revision=0,
    )
    for title_index in (0, 1):
        media_id = f"disc-01-title-00{title_index}"
        contract = tmp_path / f"{media_id}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "disc_fingerprint": fingerprint,
                "title_index": title_index,
                "media_context": {
                    "routing_assessment": assessment.to_dict(),
                    "routing_assessment_digest": assessment.digest,
                    "routing_assessment_revision": assessment.revision,
                },
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        store.hold_for_review(media_id, "tv_title_no_match")

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                automatic_processing_enabled=True,
                automatic_gemini_ambiguity_fallback=True,
            )
        ),
    )

    worker = DownstreamWorker(
        SimpleNamespace(store=store, stop_event=SimpleNamespace(is_set=lambda: False)),
        allowed_stages=("identify",),
    )

    assert worker._settle_terminal_tv_route() is True
    assert worker._settle_terminal_tv_route() is True

    store.set_paused(True)
    assert worker._apply_automatic_assessed_gemini_route() is False

    store.set_paused(False)
    worker._stop = SimpleNamespace(is_set=lambda: True)
    assert worker._apply_automatic_assessed_gemini_route() is False


def test_automatic_gemini_route_aborts_on_sibling_revision(tmp_path, monkeypatch):
    fingerprint = "0123456789abcdef"
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(
            fingerprint,
            (0, 1),
            user_hint="movie",
            evidence=(
                TitleEvidence(0, "database", "supported", "tv"),
                TitleEvidence(1, "database", "supported", "tv"),
            ),
        ),
        expected_revision=0,
    )
    for title_index in (0, 1):
        media_id = f"disc-01-title-00{title_index}"
        contract = tmp_path / f"{media_id}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "disc_fingerprint": fingerprint,
                "title_index": title_index,
                "media_context": {
                    "routing_assessment": assessment.to_dict(),
                    "routing_assessment_digest": assessment.digest,
                    "routing_assessment_revision": assessment.revision,
                },
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        store.hold_for_review(media_id, "tv_title_no_match")

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                automatic_processing_enabled=True,
                automatic_gemini_ambiguity_fallback=True,
            )
        ),
    )

    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )

    # 1. Settle TV route for both
    assert worker._settle_terminal_tv_route() is True
    assert worker._settle_terminal_tv_route() is True

    # 2. Advance the database revision
    from dataclasses import replace

    store.routing_append(
        replace(
            assessment,
            revision=2,
            evidence=(
                TitleEvidence(0, "database", "supported", "tv"),
                TitleEvidence(1, "database", "supported", "movie"),
            ),
        ),
        expected_revision=1,
    )

    # 3. Attempt Gemini route -> should fail since the database revision moved on
    assert worker._apply_automatic_assessed_gemini_route() is False


def test_automatic_disc_analysis_preserves_tv_title_no_match_for_movie_route(
    tmp_path, monkeypatch
):
    import json
    from types import SimpleNamespace

    from mkv_episode_matcher.backend.automatic_rip import (
        _resolve_automatic_unmatched_disc,
    )
    from mkv_episode_matcher.backend.unmatched_disc_analysis import GeminiAnalysisError
    from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact

    fingerprint = "0123456789abcdef"
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(
            fingerprint,
            (0,),
            user_hint="movie",
            evidence=(TitleEvidence(0, "database", "supported", "tv"),),
        ),
        expected_revision=0,
    )

    media_id = "disc-01-title-000"
    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": fingerprint,
            "title_index": 0,
            "media_context": {
                "routing_assessment": assessment.to_dict(),
                "routing_assessment_digest": assessment.digest,
                "routing_assessment_revision": assessment.revision,
                "series_name": "Unmatched",
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    # It starts out waiting for unmatched disc analysis
    store.hold_for_review(media_id, "unmatched_disc_analysis_required")

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
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(
                automatic_processing_enabled=True,
                automatic_gemini_ambiguity_fallback=True,
            )
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_engine",
        lambda: SimpleNamespace(asr=object()),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_engine",
        lambda: SimpleNamespace(asr=object()),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path / "contracts",
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path / "contracts",
    )

    def fake_execute(
        _store, _fingerprint, _series_name, _config, _asr, _contract_root, **kwargs
    ):
        _store.choose_review_path(media_id, "tv_title_no_match")
        raise GeminiAnalysisError(
            "gemini_analysis_failed",
            "Gemini did not identify any disc title confidently",
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.unmatched_disc_analysis.execute_unmatched_disc_analysis",
        fake_execute,
    )

    # 1. Run the automatic disc analysis entry point
    _resolve_automatic_unmatched_disc(
        (media_id,),
        store,
        SimpleNamespace(automatic_gemini_ambiguity_fallback=True),
        tmp_path / "contracts",
    )

    # Verify that the tv_title_no_match result survived the outer exception handler
    assert store.get(media_id).review_code == "tv_title_no_match"

    def fake_gemini(_store, ids, _config, _asr, _root, *, return_outcomes):
        import json

        from mkv_episode_matcher.backend.gemini_fallback import (
            GeminiFallbackOutcome,
            GeminiTitleOutcome,
        )

        assert return_outcomes is True
        revised = tmp_path / "revised.json"
        revised_payload = json.loads(contract.read_text(encoding="utf-8"))
        revised_payload["media_context"]["special_feature_assignments"] = [
            {
                "title_index": 0,
                "classification": "matched-feature",
                "media_kind": "movie",
                "provisional_match": False,
                "tmdb_movie_id": 123,
                "matched_title": "Example Movie",
                "identity_verification_status": "exact_verified",
                "identification_method": "movie-opensubtitles",
            }
        ]
        revised.write_text(json.dumps(revised_payload), encoding="utf-8")
        _store.apply_reviewed_identification_input(
            media_id, build_artifact("rip", revised)
        )
        return GeminiFallbackOutcome(
            (media_id,), (GeminiTitleOutcome(media_id, "matched", "movie"),)
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback",
        fake_gemini,
    )

    # 2. Run the routing worker
    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker

    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )
    assert worker._settle_terminal_tv_route() is True
    worker._apply_automatic_assessed_gemini_route()

    # Verify it routed to the movie route!
    final_item = store.get(media_id)
    assert final_item.state == "queued"
    assert final_item.review_code is None

    # Reload assessment to verify it transitioned
    attempts = store.routing_attempts(fingerprint, 0)
    assert len(attempts) == 2
    assert {(a.route, a.outcome) for a in attempts} == {
        ("tv", "no_match"),
        ("movie", "matched"),
    }


def test_m5_end_to_end_tv_no_match_to_movie_route(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from mkv_episode_matcher.backend import unmatched_disc_analysis as analysis
    from mkv_episode_matcher.backend.automatic_rip import (
        _resolve_automatic_unmatched_disc,
    )
    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
    from mkv_episode_matcher.backend.gemini_fallback import (
        GeminiFallbackOutcome,
        GeminiTitleOutcome,
    )
    from mkv_episode_matcher.backend.identification_dossier import UnmatchedFileEvidence
    from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence
    from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
    from mkv_episode_matcher.tmdb_client import TvShowCandidate

    contracts = tmp_path / "contracts"
    contracts.mkdir(exist_ok=True)
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    fingerprint = "0123456789abcdef"

    assessment = store.routing_append(
        DiscAssessment(
            fingerprint,
            (0,),
            user_hint="movie",
            evidence=(TitleEvidence(0, "database", "supported", "tv"),),
        ),
        expected_revision=0,
    )

    media_id = "disc-01-title-000"
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contract = contracts / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "media_id": media_id,
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "disc_fingerprint": fingerprint,
            "title_index": 0,
            "media_context": {
                "series_name": "Example",
                "season": None,
                "routing_assessment": assessment.to_dict(),
                "routing_assessment_digest": assessment.digest,
                "routing_assessment_revision": assessment.revision,
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "unmatched_disc_analysis_required")

    catalog = tuple(
        EpisodeCatalogEntry(
            f"S01E{episode:02d}", 1, episode, f"Title {episode}", "", 1200
        )
        for episode in range(1, 4)
    )

    monkeypatch.setattr(
        analysis,
        "search_tv_show_candidates",
        lambda _name: (TvShowCandidate(1, "Example", "", None, ""),),
    )
    monkeypatch.setattr(
        analysis, "fetch_aired_episode_catalog", lambda _show_id: catalog
    )
    monkeypatch.setattr(
        analysis, "existing_library_episodes", lambda *_args: frozenset()
    )

    class FakeDossier:
        def record_attempt(self, *_args, **_kwargs):
            pass

        def safe_attempts(self, _media_id):
            return ()

    monkeypatch.setattr(
        analysis,
        "collect_dossier_evidence",
        lambda items, *_args: (
            (UnmatchedFileEvidence(media_id, 1200, ("dialogue",)),),
            FakeDossier(),
        ),
    )

    def fake_rank(*_args, **_kwargs):
        return {media_id: SimpleNamespace(episode_id=None, confidence=0.9)}

    monkeypatch.setattr(analysis, "_rank_gemini_chunks", fake_rank)
    monkeypatch.setattr(
        analysis, "match_opensubtitles_seasons", lambda *_args, **_kwargs: ({}, {})
    )
    monkeypatch.setattr(
        analysis,
        "plan_disc_sequences",
        lambda *_args, **_kwargs: analysis._ReviewSequencePlan(),
    )
    monkeypatch.setattr(
        analysis, "discover_opensubtitles_season", lambda *_args, **_kwargs: ()
    )

    config = SimpleNamespace(
        gemini_model="gemini-test",
        automatic_processing_enabled=True,
        automatic_gemini_ambiguity_fallback=True,
        min_confidence=0.8,
    )

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(load=lambda: config),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_engine",
        lambda: SimpleNamespace(asr=object()),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: contracts,
    )

    # 1. Run the automatic disc analysis entry point - REAL coordinator (which raises)
    # caught by REAL exception handler (which preserves tv_title_no_match)
    _resolve_automatic_unmatched_disc((media_id,), store, config, contracts)

    # Verify that the tv_title_no_match result survived the outer exception handler
    assert store.get(media_id).review_code == "tv_title_no_match"

    def fake_gemini(_store, ids, _config, _asr, _root, *, return_outcomes):
        assert return_outcomes is True
        revised = tmp_path / "revised.json"
        revised_payload = json.loads(contract.read_text(encoding="utf-8"))
        revised_payload["media_context"]["special_feature_assignments"] = [
            {
                "title_index": 0,
                "classification": "matched-feature",
                "media_kind": "movie",
                "provisional_match": False,
                "tmdb_movie_id": 123,
                "matched_title": "Example Movie",
                "identity_verification_status": "exact_verified",
                "identification_method": "movie-opensubtitles",
            }
        ]
        revised.write_text(json.dumps(revised_payload), encoding="utf-8")
        _store.apply_reviewed_identification_input(
            media_id, build_artifact("rip", revised)
        )
        return GeminiFallbackOutcome(
            (media_id,), (GeminiTitleOutcome(media_id, "matched", "movie"),)
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback",
        fake_gemini,
    )

    # 2. Run the routing worker
    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )
    assert worker._settle_terminal_tv_route() is True
    worker._apply_automatic_assessed_gemini_route()

    # Verify it routed to the movie route!
    final_item = store.get(media_id)
    assert final_item.state == "queued"
    assert final_item.review_code is None

    # Reload assessment to verify it transitioned
    attempts = store.routing_attempts(fingerprint, 0)
    assert len(attempts) == 2
    assert {(a.route, a.outcome) for a in attempts} == {
        ("tv", "no_match"),
        ("movie", "matched"),
    }


def test_m5_sibling_revision_idempotent_retry(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
    from mkv_episode_matcher.backend.gemini_fallback import (
        GeminiFallbackOutcome,
        GeminiTitleOutcome,
    )
    from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact

    config_mock = SimpleNamespace(
        automatic_processing_enabled=True, automatic_gemini_ambiguity_fallback=True
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: type("m", (), {"load": lambda self: config_mock})(),
    )

    def fake_execute_gemini_fallback(store, media_ids, *args, **kwargs):
        contract_path = tmp_path / f"{media_ids[0]}_new.json"
        contract_path.write_text("{}", encoding="utf-8")
        from mkv_episode_matcher.pipeline_queue import build_artifact

        store.apply_reviewed_identification_input(
            media_ids[0], build_artifact("rip", contract_path)
        )
        return GeminiFallbackOutcome(
            handled_ids=media_ids,
            titles=(
                GeminiTitleOutcome(
                    media_id=media_ids[0], disposition="matched", accepted_role="movie"
                ),
            ),
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path,
    )

    store = PipelineQueueStore(tmp_path / "test.db")
    media_id = "test-media"

    initial_assessment = DiscAssessment("0" * 16, (1, 2, 3))
    initial_assessment = store.routing_append(initial_assessment, expected_revision=0)

    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0" * 16,
            "title_index": 1,
            "media_context": {
                "routing_assessment": initial_assessment.to_dict(),
                "routing_assessment_digest": initial_assessment.digest,
                "routing_assessment_revision": initial_assessment.revision,
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "gemini_evidence_required")
    media_id = store.list_items()[0].media_id

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_queue_store",
        lambda: store,
    )

    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )

    real_routing_append = store.routing_append

    call_count = 0

    def mock_routing_append(assessment, expected_revision=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            from dataclasses import replace

            sibling_assessment = replace(
                initial_assessment,
                revision=initial_assessment.revision + 1,
                evidence=(
                    TitleEvidence(
                        title_index=1,
                        source="content",
                        status="supported",
                        role="movie",
                    ),
                ),
            )
            real_routing_append(
                sibling_assessment, expected_revision=initial_assessment.revision
            )
        return real_routing_append(assessment, expected_revision=expected_revision)

    store.routing_append = mock_routing_append

    import unittest.mock as mock

    with mock.patch(
        "mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback",
        fake_execute_gemini_fallback,
    ):
        with mock.patch(
            "mkv_episode_matcher.backend.downstream_worker._gemini_route_assignment_decision",
            return_value=SimpleNamespace(
                role_accepted=True, identity_status="exact_verified"
            ),
        ):
            assert worker._apply_automatic_assessed_gemini_route() is True
            assert store.get(media_id).state == "queued"


def test_m5_sibling_revision_idempotent_retry_fail(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
    from mkv_episode_matcher.backend.gemini_fallback import (
        GeminiFallbackOutcome,
        GeminiTitleOutcome,
    )
    from mkv_episode_matcher.disc.routing import (
        DiscAssessment,
        RoutingError,
        TitleEvidence,
    )
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact

    config_mock = SimpleNamespace(
        automatic_processing_enabled=True, automatic_gemini_ambiguity_fallback=True
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: type("m", (), {"load": lambda self: config_mock})(),
    )

    def fake_execute_gemini_fallback(store, media_ids, *args, **kwargs):
        contract_path = tmp_path / f"{media_ids[0]}_new.json"
        contract_path.write_text("{}", encoding="utf-8")
        from mkv_episode_matcher.pipeline_queue import build_artifact

        store.apply_reviewed_identification_input(
            media_ids[0], build_artifact("rip", contract_path)
        )
        return GeminiFallbackOutcome(
            handled_ids=media_ids,
            titles=(
                GeminiTitleOutcome(
                    media_id=media_ids[0], disposition="matched", accepted_role="movie"
                ),
            ),
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path,
    )

    store = PipelineQueueStore(tmp_path / "test.db")
    media_id = "test-media"

    initial_assessment = DiscAssessment("0" * 16, (1, 2, 3))
    initial_assessment = store.routing_append(initial_assessment, expected_revision=0)

    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0" * 16,
            "title_index": 1,
            "media_context": {
                "routing_assessment": initial_assessment.to_dict(),
                "routing_assessment_digest": initial_assessment.digest,
                "routing_assessment_revision": initial_assessment.revision,
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "gemini_evidence_required")

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_queue_store",
        lambda: store,
    )

    worker = DownstreamWorker(
        SimpleNamespace(store=store), allowed_stages=("identify",)
    )

    real_routing_append = store.routing_append

    call_count = 0

    def mock_routing_append(assessment, expected_revision=None):
        nonlocal call_count
        call_count += 1
        from dataclasses import replace

        current = store.routing_latest("0" * 16)
        sibling = replace(
            current,
            revision=current.revision + 1,
            evidence=current.evidence
            + (
                TitleEvidence(
                    title_index=100 + call_count,
                    source="content",
                    status="supported",
                    role="tv",
                ),
            ),
        )
        real_routing_append(sibling, expected_revision=current.revision)
        raise RoutingError("Routing revision is stale")

    store.routing_append = mock_routing_append

    import unittest.mock as mock

    with mock.patch(
        "mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback",
        fake_execute_gemini_fallback,
    ):
        with mock.patch(
            "mkv_episode_matcher.backend.downstream_worker._gemini_route_assignment_decision",
            return_value=SimpleNamespace(
                role_accepted=True, identity_status="exact_verified"
            ),
        ):
            assert worker._apply_automatic_assessed_gemini_route() is False


def test_manual_endpoints_queue_transition_lock(tmp_path, monkeypatch):
    import json
    import time
    import unittest.mock as mock

    from mkv_episode_matcher.backend.automatic_rip import _downstream_lock
    from mkv_episode_matcher.backend.routers.rip import (
        GeminiFallbackExecutionRequest,
        execute_pipeline_gemini_fallback,
    )
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact

    store = PipelineQueueStore(tmp_path / "test.db")
    media_id = "test-media"
    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0" * 16,
            "title_index": 1,
            "media_context": {},
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "gemini_evidence_required")

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: tmp_path,
    )

    _downstream_lock.acquire()
    try:
        request = GeminiFallbackExecutionRequest(
            media_ids=[media_id],
            confirm_media_read=True,
            confirm_external_transmission=True,
            confirm_classification=True,
            disc_fingerprint="0" * 16,
        )

        with mock.patch(
            "mkv_episode_matcher.backend.routers.rip.execute_gemini_fallback"
        ) as mock_exec:
            mock_exec.side_effect = lambda store, ids, *a: [ids[0]]
            execute_pipeline_gemini_fallback(request, store, tmp_path)
            assert store.get(media_id).review_code == "gemini_evidence_required"

            _downstream_lock.release()
            time.sleep(0.1)

            assert (
                store.get(media_id).review_code == "gemini_analysis_running"
                or store.get(media_id).state != "review_required"
            )
    except Exception:
        if _downstream_lock.locked():
            _downstream_lock.release()
        raise
