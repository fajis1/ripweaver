"""Bounded saved-data policy for trying another disc-title content route."""

from __future__ import annotations

from mkv_episode_matcher.disc.routing import DiscAssessment, RouteAttempt, RoutingError


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
