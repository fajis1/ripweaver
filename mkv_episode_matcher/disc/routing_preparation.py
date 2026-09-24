"""Build a non-authorizing routing observation from saved preparation metadata."""

from __future__ import annotations

from collections.abc import Mapping

from mkv_episode_matcher.disc.routing import DiscAssessment, RoutingError, TitleEvidence


def build_preparation_assessment(
    *,
    fingerprint: str,
    title_indexes: tuple[int, ...],
    user_hint: str | None,
    title_classifications: Mapping[int, str],
    explicit_tv_context: bool,
    episode_assignment_indexes: tuple[int, ...] = (),
    feature_assignment_indexes: tuple[int, ...] = (),
    database_status: str | None = None,
) -> DiscAssessment:
    """Only independent context turns a structural title guess into a role."""

    evidence: list[TitleEvidence] = []
    indexes = set(title_indexes)
    if any(index not in indexes for index in title_classifications):
        raise RoutingError("Routing classification is outside the inventory")
    if explicit_tv_context:
        for index, classification in title_classifications.items():
            if classification == "episode":
                evidence.append(TitleEvidence(index, "label", "supported", "tv"))
            elif classification == "extra":
                evidence.append(TitleEvidence(index, "label", "supported", "extra"))
    for index in set(episode_assignment_indexes):
        evidence.append(TitleEvidence(index, "database", "supported", "tv"))
    for index in set(feature_assignment_indexes):
        evidence.append(TitleEvidence(index, "database", "supported", "extra"))
    if database_status in {"unavailable", "ambiguous", "support-required"}:
        status = "unavailable" if database_status == "unavailable" else "ambiguous"
        evidence.extend(
            TitleEvidence(index, "database", status) for index in title_indexes
        )
    return DiscAssessment(
        fingerprint,
        title_indexes,
        user_hint=user_hint,
        evidence=tuple(evidence),
    )
