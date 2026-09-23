"""Saved-contract tests for movie-with-extras descriptive identities."""

from __future__ import annotations

from copy import deepcopy

import pytest

from mkv_episode_matcher.disc.movie_extras_identity import (
    MovieExtrasIdentityError,
    accept_descriptive_extra_identities,
    descriptive_extra_identity_is_accepted,
    verified_movie_identity,
)
from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence

FINGERPRINT = "0123456789abcdef"


def _assessment(extra_indexes: tuple[int, ...] = (2, 3)) -> DiscAssessment:
    return DiscAssessment(
        FINGERPRINT,
        (0, *extra_indexes),
        evidence=(
            TitleEvidence(0, "content", "supported", "movie"),
            *(
                TitleEvidence(index, "content", "supported", "extra")
                for index in extra_indexes
            ),
        ),
    )


def _payload(
    index: int, assignment: dict, assessment: DiscAssessment | None = None
) -> dict:
    assessment = assessment or _assessment()
    return {
        "mode": "verified-rip-contract",
        "disc_fingerprint": FINGERPRINT,
        "title_index": index,
        "media_context": {
            "routing_assessment": assessment.to_dict(),
            "routing_assessment_digest": assessment.digest,
            "routing_assessment_revision": assessment.revision,
            "special_feature_assignments": [{"title_index": index, **assignment}],
        },
    }


def _main(assessment: DiscAssessment | None = None) -> dict:
    payload = _payload(
        0,
        {
            "classification": "matched-feature",
            "media_kind": "movie",
            "matched_title": "Example Movie",
            "provisional_match": False,
            "identity_verification_status": "exact_verified",
            "tmdb_movie_id": 123,
            "identification_method": "movie-opensubtitles",
        },
        assessment,
    )
    payload["media_context"]["special_feature_library_year"] = 1988
    return payload


def _extra(
    index: int,
    name: str = "Behind: the Scenes",
    assessment: DiscAssessment | None = None,
) -> dict:
    return _payload(
        index,
        {
            "classification": "matched-feature",
            "media_kind": "extra",
            "matched_title": name,
            "match_summary": "Bounded dialogue and visual evidence links this bonus title.",
            "gemini_confidence": 0.82,
            "provisional_match": True,
            "identity_verification_status": "descriptive_pending",
            "fallback_name_policy": "none",
            "jellyfin_folder": "other",
        },
        assessment,
    )


def test_accepts_safe_descriptions_linked_to_verified_movie():
    main = _main()
    accepted = accept_descriptive_extra_identities(
        main, (_extra(2, "Making <Of>"), _extra(3, "Deleted Scene"))
    )

    assert verified_movie_identity(main).tmdb_movie_id == 123
    assert [
        payload["media_context"]["special_feature_assignments"][0]["matched_title"]
        for payload in accepted
    ] == ["Making Of", "Deleted Scene"]
    for payload in accepted:
        context = payload["media_context"]
        assignment = context["special_feature_assignments"][0]
        assert context["special_feature_library_title"] == "Example Movie"
        assert context["special_feature_library_year"] == 1988
        assert assignment["related_tmdb_movie_id"] == 123
        assert assignment["identity_verification_status"] == "descriptive_accepted"
        assert assignment["provisional_match"] is False
        assert descriptive_extra_identity_is_accepted(
            payload, assignment, DiscAssessment.from_dict(context["routing_assessment"])
        )


def test_duplicate_descriptions_receive_stable_title_suffixes():
    forward = accept_descriptive_extra_identities(
        _main(), (_extra(2, "Interview"), _extra(3, "interview"))
    )
    reverse = accept_descriptive_extra_identities(
        _main(), (_extra(3, "interview"), _extra(2, "Interview"))
    )

    def names(payloads):
        return {
            payload["title_index"]: payload["media_context"][
                "special_feature_assignments"
            ][0]["matched_title"]
            for payload in payloads
        }

    assert (
        names(forward)
        == names(reverse)
        == {
            2: "Interview - Title 002",
            3: "interview - Title 003",
        }
    )


def test_accepts_six_extra_live_shape_with_stable_descriptions():
    assessment = _assessment((2, 3, 4, 5, 6, 7))
    descriptions = (
        "Making the Movie",
        "Deleted Scenes",
        "Cast Interviews",
        "Visual Effects Featurette",
        "Behind the Scenes",
        "Theatrical Trailer",
    )
    accepted = accept_descriptive_extra_identities(
        _main(assessment),
        tuple(
            _extra(index, description, assessment)
            for index, description in zip(range(2, 8), descriptions, strict=True)
        ),
    )

    assert len(accepted) == 6
    assert [
        payload["media_context"]["special_feature_assignments"][0]["matched_title"]
        for payload in accepted
    ] == list(descriptions)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda payload: payload["media_context"]["special_feature_assignments"][
                0
            ].update(matched_title='<>:"/\\|?*'),
            "empty after sanitizing",
        ),
        (
            lambda payload: payload["media_context"]["special_feature_assignments"][
                0
            ].update(match_summary=""),
            "relationship evidence",
        ),
        (
            lambda payload: payload.update(disc_fingerprint="fedcba9876543210"),
            "another disc",
        ),
    ],
)
def test_rejects_unsafe_unrelated_or_unsupported_extra(mutate, message):
    extra = _extra(2)
    mutate(extra)
    with pytest.raises(MovieExtrasIdentityError, match=message):
        accept_descriptive_extra_identities(_main(), (extra,))


def test_does_not_mutate_provisional_input_contracts():
    original = _extra(2)
    before = deepcopy(original)
    accept_descriptive_extra_identities(_main(), (original,))
    assert original == before
