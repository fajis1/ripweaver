"""Bounded saved-data policy for trying another disc-title content route."""

from __future__ import annotations

from mkv_episode_matcher.disc.routing import DiscAssessment, RouteAttempt, RoutingError

_TV_TERMINAL_REVIEW_OUTCOMES = {
    "all_season_series_not_found": "review",
    "all_season_catalog_unavailable": "service_failed",
    "all_season_analysis_failed": "service_failed",
    "all_season_evidence_failed": "service_failed",
    "all_season_sequence_review_required": "review",
    "independent_episode_evidence_required": "review",
    "whole_disc_coherence_review_required": "review",
    "tv_title_no_match": "no_match",
}


def terminal_tv_review_outcome(review_code: str | None) -> str | None:
    """Classify only final TV coordinator results, never an in-progress hold."""

    return _TV_TERMINAL_REVIEW_OUTCOMES.get(review_code)


def _route_order(role: str, hint: str | None) -> tuple[str, ...]:
    if role == "tv":
        return "tv", "movie", "extra"
    if role == "movie":
        return "movie", "tv", "extra"
    if role == "extra":
        return "extra", "movie", "tv"
    if hint == "tv":
        return "classify", "tv", "movie", "extra"
    if hint == "movie":
        return "classify", "movie", "tv", "extra"
    if hint == "extras":
        return "classify", "extra", "movie", "tv"
    return "classify", "movie", "tv", "extra"


def next_route(
    assessment: DiscAssessment,
    *,
    title_index: int,
    attempts: tuple[RouteAttempt, ...] = (),
) -> str | None:
    """Only a genuine no-match permits another route on the same revision."""

    title = next(
        (item for item in assessment.title_roles() if item.title_index == title_index),
        None,
    )
    if title is None:
        raise RoutingError("Route title is outside the assessment")
    if title.role == "skip":
        return None
    if any(item.outcome == "matched" for item in attempts):
        return None
    current = tuple(item for item in attempts if item.revision == assessment.revision)
    if any(
        item.outcome in {"running", "review", "service_failed", "interrupted"}
        for item in current
    ):
        return None
    if any(item.outcome != "no_match" for item in current):
        raise RoutingError("Route attempt history is invalid")
    tried = {item.route for item in current}
    return next(
        (
            route
            for route in _route_order(title.role, assessment.user_hint)
            if route not in tried
        ),
        None,
    )
