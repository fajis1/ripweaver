"""Saved-contract policy for a verified movie and descriptive related extras."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass

from mkv_episode_matcher.disc.routing import (
    DiscAssessment,
    RoutingError,
    assessment_from_contract,
)

_UNSAFE_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class MovieExtrasIdentityError(ValueError):
    """A movie-with-extras identity contract is incomplete or unsafe."""


@dataclass(frozen=True)
class VerifiedMovieIdentity:
    title: str
    year: int | None
    tmdb_movie_id: int


def _safe_description(value: object) -> str:
    if not isinstance(value, str):
        raise MovieExtrasIdentityError("Extra description is missing")
    cleaned = _UNSAFE_COMPONENT.sub(" ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")[:120].rstrip(" .")
    if not cleaned:
        raise MovieExtrasIdentityError("Extra description is empty after sanitizing")
    return cleaned


def _bound_assessment(payload: object) -> DiscAssessment:
    try:
        assessment = assessment_from_contract(payload)
    except RoutingError as exc:
        raise MovieExtrasIdentityError("Routing assessment is invalid") from exc
    if assessment is None:
        raise MovieExtrasIdentityError("Routing assessment is required")
    return assessment


def _title_role(payload: dict, assessment: DiscAssessment) -> str:
    title_index = payload.get("title_index")
    if type(title_index) is not int:
        raise MovieExtrasIdentityError("Title index is invalid")
    return next(
        role.role
        for role in assessment.title_roles()
        if role.title_index == title_index
    )


def _assignment(payload: dict, media_kind: str) -> dict:
    context = payload.get("media_context")
    if not isinstance(context, dict):
        raise MovieExtrasIdentityError("Media context is invalid")
    title_index = payload.get("title_index")
    assignments = context.get("special_feature_assignments")
    if not isinstance(assignments, list):
        raise MovieExtrasIdentityError("Feature assignment is missing")
    assignment = next(
        (
            item
            for item in assignments
            if isinstance(item, dict)
            and item.get("title_index") == title_index
            and item.get("classification") == "matched-feature"
            and item.get("media_kind") == media_kind
        ),
        None,
    )
    if assignment is None:
        raise MovieExtrasIdentityError("Feature assignment is invalid")
    return assignment


def verified_movie_identity(payload: dict) -> VerifiedMovieIdentity:
    """Read an exact provider-verified main-movie identity."""

    assessment = _bound_assessment(payload)
    if assessment.composition not in {"movie", "movie_with_extras"}:
        raise MovieExtrasIdentityError("Disc is not a single-main-movie composition")
    if _title_role(payload, assessment) != "movie":
        raise MovieExtrasIdentityError("Title is not the assessed main movie")
    assignment = _assignment(payload, "movie")
    movie_id = assignment.get("tmdb_movie_id")
    title = assignment.get("matched_title")
    year = payload["media_context"].get("special_feature_library_year")
    if not (
        assignment.get("provisional_match") is False
        and assignment.get("identity_verification_status") == "exact_verified"
        and assignment.get("identification_method")
        in {"movie-opensubtitles", "tv-related-movie-opensubtitles"}
        and type(movie_id) is int
        and movie_id > 0
        and isinstance(title, str)
        and bool(title.strip())
        and (year is None or type(year) is int)
    ):
        raise MovieExtrasIdentityError("Main movie identity is not exact")
    return VerifiedMovieIdentity(_safe_description(title), year, movie_id)


def accept_descriptive_extra_identities(
    main_movie_payload: dict, extra_payloads: tuple[dict, ...]
) -> tuple[dict, ...]:
    """Accept safe evidence-derived extra names linked to one verified movie."""

    identity = verified_movie_identity(main_movie_payload)
    fingerprint = main_movie_payload.get("disc_fingerprint")
    prepared: list[tuple[int, str, dict, dict]] = []
    for original in extra_payloads:
        payload = copy.deepcopy(original)
        if payload.get("disc_fingerprint") != fingerprint:
            raise MovieExtrasIdentityError("Extra belongs to another disc")
        assessment = _bound_assessment(payload)
        if assessment.composition != "movie_with_extras":
            raise MovieExtrasIdentityError("Extra disc composition is not settled")
        if _title_role(payload, assessment) != "extra":
            raise MovieExtrasIdentityError("Title is not an assessed extra")
        assignment = _assignment(payload, "extra")
        summary = assignment.get("match_summary")
        confidence = assignment.get("gemini_confidence")
        if not (
            assignment.get("provisional_match") is True
            and assignment.get("identity_verification_status") == "descriptive_pending"
            and isinstance(summary, str)
            and bool(summary.strip())
            and type(confidence) in {int, float}
            and 0.0 <= float(confidence) <= 1.0
        ):
            raise MovieExtrasIdentityError("Extra relationship evidence is incomplete")
        title_index = payload["title_index"]
        prepared.append((
            title_index,
            _safe_description(assignment.get("matched_title")),
            payload,
            assignment,
        ))

    counts: dict[str, int] = {}
    for _index, name, _payload, _assignment_item in prepared:
        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
    accepted = []
    for title_index, name, payload, assignment in prepared:
        if counts[name.casefold()] > 1:
            name = f"{name} - Title {title_index:03d}"
        assignment.update(
            matched_title=name,
            provisional_match=False,
            identity_verification_status="descriptive_accepted",
            descriptive_identity_accepted=True,
            related_tmdb_movie_id=identity.tmdb_movie_id,
            identification_method="gemini-descriptive-extra",
            jellyfin_folder="Extras",
            library_kind="movie",
        )
        context = payload["media_context"]
        context["special_feature_library_title"] = identity.title
        context["special_feature_library_year"] = identity.year
        accepted.append(payload)
    return tuple(accepted)


def descriptive_extra_identity_is_accepted(
    payload: dict, assignment: dict, assessment: DiscAssessment
) -> bool:
    """Validate the final fields consumed by the identify adapter."""

    context = payload.get("media_context")
    return bool(
        assessment.composition == "movie_with_extras"
        and _title_role(payload, assessment) == "extra"
        and assignment.get("media_kind") == "extra"
        and assignment.get("provisional_match") is False
        and assignment.get("identity_verification_status") == "descriptive_accepted"
        and assignment.get("descriptive_identity_accepted") is True
        and type(assignment.get("related_tmdb_movie_id")) is int
        and assignment["related_tmdb_movie_id"] > 0
        and assignment.get("identification_method") == "gemini-descriptive-extra"
        and isinstance(context, dict)
        and bool(_safe_description(assignment.get("matched_title")))
        and bool(_safe_description(context.get("special_feature_library_title")))
    )
