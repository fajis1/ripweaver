"""Bounded alternate-route decisions for one title and one assessment revision."""

from __future__ import annotations

from dataclasses import dataclass

from mkv_episode_matcher.disc.routing import DiscRoutingAssessment, RoutingError

_OUTCOMES = frozenset({"matched", "no_match", "review", "service_failed"})


@dataclass(frozen=True)
class RouteAttempt:
    assessment_revision: int
    route: str
    outcome: str

    def __post_init__(self) -> None:
        if type(self.assessment_revision) is not int or self.assessment_revision < 1:
            raise RoutingError("Route attempt revision is invalid")
        if self.route not in {"tv", "movie", "extras", "mixed-classifier"}:
            raise RoutingError("Route attempt is invalid")
        if self.outcome not in _OUTCOMES:
            raise RoutingError("Route attempt outcome is invalid")


def next_route(
    assessment: DiscRoutingAssessment,
    *,
    title_index: int,
    attempts: tuple[RouteAttempt, ...] = (),
) -> str | None:
    """Return one untried route, or None when review/service failure must hold."""

    route = next(
        (item for item in assessment.title_routes() if item.title_index == title_index),
        None,
    )
    if route is None:
        raise RoutingError("Route title is outside the assessment inventory")
    relevant = tuple(
        item for item in attempts if item.assessment_revision == assessment.revision
    )
    if any(item.outcome == "matched" for item in relevant):
        return None
    if any(item.outcome == "service_failed" for item in relevant):
        return None
    tried = {item.route for item in relevant}
    if any(item.outcome not in {"no_match", "review"} for item in relevant):
        raise RoutingError("Route attempt history is invalid")
    return next(
        (
            candidate
            for candidate in route.investigation_order
            if candidate not in tried
        ),
        None,
    )


def route_attempts_for_revision(
    attempts: tuple[RouteAttempt, ...], revision: int
) -> tuple[RouteAttempt, ...]:
    """Bound the history used by a retry and ignore older assessment revisions."""

    if type(revision) is not int or revision < 1:
        raise RoutingError("Route revision is invalid")
    return tuple(item for item in attempts if item.assessment_revision == revision)
