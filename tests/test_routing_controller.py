"""Synthetic route decisions and durable claims; no media/provider work."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from mkv_episode_matcher.disc.routing import (
    DiscAssessment,
    RouteAttempt,
    RoutingError,
    TitleEvidence,
)
from mkv_episode_matcher.disc.routing_controller import (
    next_route,
    terminal_tv_review_outcome,
)
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact

FINGERPRINT = "0123456789abcdef"


@pytest.mark.parametrize(
    ("role", "hint", "first"),
    [
        ("tv", "movie", "tv"),
        ("movie", "tv", "movie"),
        ("extra", "tv", "extra"),
        (None, "movie", "classify"),
        (None, "tv", "classify"),
    ],
)
def test_evidence_decides_first_route_and_hint_only_orders_unknown(role, hint, first):
    evidence = (TitleEvidence(0, "database", "supported", role),) if role else ()
    assessment = DiscAssessment(FINGERPRINT, (0,), hint, evidence)
    assert next_route(assessment, title_index=0) == first


def test_no_match_advances_but_review_and_failure_hold():
    assessment = DiscAssessment(FINGERPRINT, (0,), user_hint="tv")
    no_match = (RouteAttempt(0, 1, "classify", "no_match"),)
    assert next_route(assessment, title_index=0, attempts=no_match) == "tv"
    assert (
        next_route(
            assessment,
            title_index=0,
            attempts=no_match + (RouteAttempt(0, 1, "tv", "no_match"),),
        )
        == "movie"
    )
    for outcome in ("running", "review", "service_failed", "interrupted", "matched"):
        assert (
            next_route(
                assessment,
                title_index=0,
                attempts=(RouteAttempt(0, 1, "classify", outcome),),
            )
            is None
        )
    with pytest.raises(RoutingError):
        next_route(assessment, title_index=3)


def test_claim_next_is_atomic_and_restart_safe(tmp_path):
    path = tmp_path / "queue.sqlite3"
    store = PipelineQueueStore(path)
    assessment = store.routing_append(
        DiscAssessment(FINGERPRINT, (0,), user_hint="movie"), expected_revision=0
    )

    def claim(_):
        return PipelineQueueStore(path).routing_claim_next(assessment, 0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, range(2)))
    assert claimed.count("classify") == 1
    assert claimed.count(None) == 1
    store.routing_settle(assessment, 0, "classify", "no_match")
    assert PipelineQueueStore(path).routing_claim_next(assessment, 0) == "movie"
    store.routing_settle(assessment, 0, "movie", "service_failed")
    assert store.routing_claim_next(assessment, 0) is None


def test_new_revision_does_not_repeat_old_matched_assignment(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    first = store.routing_append(DiscAssessment(FINGERPRINT, (0,)), expected_revision=0)
    assert store.routing_claim_next(first, 0) == "classify"
    store.routing_settle(first, 0, "classify", "matched")
    revised = store.routing_append(
        replace(first, revision=2, user_hint="tv"), expected_revision=1
    )
    assert store.routing_claim_next(revised, 0) is None


def test_provider_failure_is_not_movie_evidence_or_fallback_permission(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(FINGERPRINT, (0,), user_hint="movie"), expected_revision=0
    )
    assert store.routing_claim_next(assessment, 0) == "classify"
    store.routing_settle(assessment, 0, "classify", "service_failed")
    assert store.routing_claim_next(assessment, 0) is None
    assert store.routing_latest(FINGERPRINT).title_roles()[0].role == "unknown"


def test_tv_catalogue_no_match_can_try_movie_but_outage_cannot(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(
            FINGERPRINT,
            (0, 1),
            evidence=(
                TitleEvidence(0, "database", "supported", "tv"),
                TitleEvidence(1, "database", "supported", "tv"),
            ),
        ),
        expected_revision=0,
    )
    assert store.routing_claim_next(assessment, 0) == "tv"
    store.routing_settle(assessment, 0, "tv", "no_match")
    assert store.routing_claim_next(assessment, 0) == "movie"
    assert store.routing_claim_next(assessment, 1) == "tv"
    store.routing_settle(assessment, 1, "tv", "service_failed")
    assert store.routing_claim_next(assessment, 1) is None


def test_movie_with_extras_and_multiple_movies_keep_per_title_routes(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(
            FINGERPRINT,
            (0, 1, 2),
            user_hint="tv",
            evidence=(
                TitleEvidence(0, "database", "supported", "movie"),
                TitleEvidence(1, "database", "supported", "movie"),
                TitleEvidence(2, "database", "supported", "extra"),
            ),
        ),
        expected_revision=0,
    )
    assert assessment.composition == "movies_with_extras"
    assert [store.routing_claim_next(assessment, index) for index in range(3)] == [
        "movie",
        "movie",
        "extra",
    ]


def test_conflicting_metadata_requires_classification_before_hint_route():
    assessment = DiscAssessment(
        FINGERPRINT,
        (0,),
        user_hint="tv",
        evidence=(
            TitleEvidence(0, "database", "supported", "tv"),
            TitleEvidence(0, "database", "supported", "movie"),
        ),
    )
    assert assessment.title_roles()[0].role == "conflicting"
    assert next_route(assessment, title_index=0) == "classify"
    assert (
        next_route(
            assessment,
            title_index=0,
            attempts=(RouteAttempt(0, 1, "classify", "review"),),
        )
        is None
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("all_season_series_not_found", "review"),
        ("all_season_catalog_unavailable", "service_failed"),
        ("all_season_analysis_failed", "service_failed"),
        ("all_season_evidence_failed", "service_failed"),
        ("whole_disc_coherence_review_required", "review"),
        ("tv_title_no_match", "no_match"),
        ("episode_match_review", None),
        ("all_season_analysis_running", None),
    ],
)
def test_only_terminal_tv_codes_become_route_outcomes(code, expected):
    assert terminal_tv_review_outcome(code) == expected


def test_route_claim_checks_held_queue_state_atomically(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(FINGERPRINT, (0,)), expected_revision=0
    )
    contract = tmp_path / "rip.json"
    contract.write_text(json.dumps({"mode": "verified-rip-contract"}), encoding="utf-8")
    store.enqueue_verified_rip("disc-01-title-000", build_artifact("rip", contract))
    with pytest.raises(RoutingError, match="queue state changed"):
        store.routing_claim_next(
            assessment,
            0,
            media_id="disc-01-title-000",
            expected_review_code="all_season_series_not_found",
        )
    assert store.routing_attempts(FINGERPRINT, 0) == ()
    store.hold_for_review("disc-01-title-000", "all_season_series_not_found")
    assert (
        store.routing_claim_next(
            assessment,
            0,
            media_id="disc-01-title-000",
            expected_review_code="all_season_series_not_found",
        )
        == "classify"
    )


def test_restart_reconciles_running_route_without_retrying(tmp_path):
    path = tmp_path / "queue.sqlite3"
    store = PipelineQueueStore(path)
    assessment = store.routing_append(
        DiscAssessment(FINGERPRINT, (0,), user_hint="movie"), expected_revision=0
    )
    assert store.routing_claim_next(assessment, 0) == "classify"
    restarted = PipelineQueueStore(path)
    from mkv_episode_matcher.backend.main import _reconcile_downstream_at_startup

    _reconcile_downstream_at_startup(restarted)
    assert restarted.routing_reconcile_interrupted() == 0
    assert restarted.routing_attempts(FINGERPRINT, 0)[0].outcome == "interrupted"
    assert restarted.routing_claim_next(assessment, 0) is None
