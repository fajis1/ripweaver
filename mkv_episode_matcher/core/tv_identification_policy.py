"""Shared policy markers for automatic television episode assignments."""

AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION = 4
AUTOMATIC_TV_MIN_CONFIDENCE = 0.70

LOCAL_DIALOGUE_TWO_WINDOW_SOURCE = "local-dialogue-two-window"
LOCAL_DIALOGUE_DISC_CORROBORATED_SOURCE = "local-dialogue-disc-corroborated"
OPENSUBTITLES_TWO_WINDOW_SOURCE = "opensubtitles-two-window"
OPENSUBTITLES_RESIDUAL_SOURCE = "opensubtitles-residual-elimination"
GEMINI_TWO_PASS_SOURCE = "gemini-two-pass"

AUTOMATIC_TV_ASSIGNMENT_SOURCES = frozenset({
    OPENSUBTITLES_TWO_WINDOW_SOURCE,
    OPENSUBTITLES_RESIDUAL_SOURCE,
    GEMINI_TWO_PASS_SOURCE,
})

# A season hint narrows the episode catalogue; it must not select a weaker
# identification workflow.  These review codes all retain verified TV-disc
# media and are eligible to re-enter the same disc-wide evidence ladder.
TV_DISC_ANALYSIS_REVIEW_CODES = frozenset({
    "missing_season_context",
    "episode_match_review",
    "unmatched_disc_analysis_required",
    "all_season_analysis_failed",
    "all_season_series_not_found",
    "all_season_evidence_failed",
    "all_season_catalog_unavailable",
    "all_season_sequence_review_required",
    "independent_episode_evidence_required",
    "whole_disc_coherence_review_required",
    "gemini_descriptive_review_required",
    "gemini_analysis_interrupted",
    "gemini_analysis_failed",
    "gemini_audio_evidence_insufficient",
    "gemini_catalog_unavailable",
    "gemini_provider_failed",
    "gemini_credential_rejected",
    "gemini_rate_limited",
    "gemini_provider_unavailable",
    "gemini_request_rejected",
    "gemini_network_failed",
    "gemini_response_invalid",
    "gemini_series_resolution_uncertain",
})

# Retry transient or evidence-related failures once per backend lifetime and
# resolved-sibling set.  Persistent credential, quota, and rejected-request
# failures remain explicitly retryable through the reviewed Web UI, but should
# not generate another automatic provider request after every restart.
AUTOMATIC_TV_DISC_RETRY_CODES = frozenset({
    "all_season_analysis_failed",
    "gemini_analysis_interrupted",
    "gemini_analysis_failed",
    "gemini_audio_evidence_insufficient",
    "gemini_catalog_unavailable",
    "gemini_provider_failed",
    "gemini_provider_unavailable",
    "gemini_network_failed",
    "gemini_response_invalid",
})


def identification_order_for_assignment(source: object) -> str | None:
    """Return the audit label for one independently supported assignment."""

    return {
        OPENSUBTITLES_TWO_WINDOW_SOURCE: "tv-opensubtitles-two-window",
        OPENSUBTITLES_RESIDUAL_SOURCE: ("tv-opensubtitles-residual-elimination"),
        GEMINI_TWO_PASS_SOURCE: "tv-gemini-two-pass",
    }.get(source)
