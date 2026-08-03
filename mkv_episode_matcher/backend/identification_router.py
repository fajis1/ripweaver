"""Bounded routing policy for cross-kind identification attempts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IdentificationRoute:
    branch: str
    reason: str


def route_order(content_hint: str | None) -> tuple[IdentificationRoute, ...]:
    """Return each classifier branch at most once for one analysis cycle."""

    hint = (content_hint or "").strip().casefold()
    tv = IdentificationRoute("tv", "compare against aired episode catalogues")
    descriptive = IdentificationRoute(
        "movie-bonus", "classify as movie, TV movie, bonus feature, menu, or unknown"
    )
    synthesis = IdentificationRoute(
        "gemini-synthesis", "review the safe summaries from every earlier attempt"
    )
    if hint in {"movie", "extras", "bonus", "mixed"}:
        return descriptive, tv, synthesis
    return tv, descriptive, synthesis


def remaining_routes(
    content_hint: str | None, attempted: set[str]
) -> tuple[IdentificationRoute, ...]:
    return tuple(
        route for route in route_order(content_hint) if route.branch not in attempted
    )
