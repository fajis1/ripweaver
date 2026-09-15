from dataclasses import replace

import pytest

from mkv_episode_matcher.disc.routing import (
    DiscRoutingAssessment,
    RoutingError,
    TitleRoutingEvidence,
)
from mkv_episode_matcher.disc.routing_controller import (
    RouteAttempt,
    next_route,
    route_attempts_for_revision,
)


def _assessment():
    return DiscRoutingAssessment(
        "0123456789abcdef",
        (0,),
    )


def test_unknown_hint_starts_classifier_then_tries_ordered_alternates():
    assessment = _assessment()
    assert next_route(assessment, title_index=0) == "mixed-classifier"
    attempts = (RouteAttempt(1, "mixed-classifier", "no_match"),)
    assert next_route(assessment, title_index=0, attempts=attempts) == "tv"
    attempts += (RouteAttempt(1, "tv", "review"),)
    assert next_route(assessment, title_index=0, attempts=attempts) == "movie"


def test_matched_or_provider_failure_stops_alternates():
    assessment = _assessment()
    assert (
        next_route(
            assessment, title_index=0, attempts=(RouteAttempt(1, "tv", "matched"),)
        )
        is None
    )
    assert (
        next_route(
            assessment,
            title_index=0,
            attempts=(RouteAttempt(1, "tv", "service_failed"),),
        )
        is None
    )


def test_same_route_cannot_repeat_and_old_revision_is_ignored():
    assessment = _assessment()
    attempts = (
        RouteAttempt(1, "mixed-classifier", "no_match"),
        RouteAttempt(1, "tv", "no_match"),
        RouteAttempt(2, "mixed-classifier", "no_match"),
    )
    assert next_route(assessment, title_index=0, attempts=attempts) == "movie"
    assert route_attempts_for_revision(attempts, 2) == (attempts[2],)


@pytest.mark.parametrize("outcome", ["bad", "matched"])
def test_invalid_route_history_is_rejected(outcome):
    assessment = _assessment()
    if outcome == "matched":
        assert (
            next_route(
                assessment, title_index=0, attempts=(RouteAttempt(1, "tv", outcome),)
            )
            is None
        )
    else:
        with pytest.raises(RoutingError):
            RouteAttempt(1, "tv", outcome)


def test_new_revision_does_not_repeat_old_route():
    assessment = _assessment()
    revised = replace(
        assessment, revision=2, evidence=(TitleRoutingEvidence(0, "movie", "content"),)
    )
    assert (
        next_route(
            revised, title_index=0, attempts=(RouteAttempt(1, "tv", "no_match"),)
        )
        == "movie"
    )
