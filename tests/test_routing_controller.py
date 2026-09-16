"""Synthetic route decisions and durable claims; no media/provider work."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from mkv_episode_matcher.disc.routing import (
    DiscAssessment,
    RouteAttempt,
    RoutingError,
    TitleEvidence,
)
from mkv_episode_matcher.disc.routing_controller import next_route
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore

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
