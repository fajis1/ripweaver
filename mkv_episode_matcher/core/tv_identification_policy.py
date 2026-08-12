"""Shared policy markers for automatic television episode assignments."""

AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION = 4
AUTOMATIC_TV_MIN_CONFIDENCE = 0.70

OPENSUBTITLES_TWO_WINDOW_SOURCE = "opensubtitles-two-window"
OPENSUBTITLES_RESIDUAL_SOURCE = "opensubtitles-residual-elimination"
GEMINI_TWO_PASS_SOURCE = "gemini-two-pass"

AUTOMATIC_TV_ASSIGNMENT_SOURCES = frozenset({
    OPENSUBTITLES_TWO_WINDOW_SOURCE,
    OPENSUBTITLES_RESIDUAL_SOURCE,
    GEMINI_TWO_PASS_SOURCE,
})


def identification_order_for_assignment(source: object) -> str | None:
    """Return the audit label for one independently supported assignment."""

    return {
        OPENSUBTITLES_TWO_WINDOW_SOURCE: "tv-opensubtitles-two-window",
        OPENSUBTITLES_RESIDUAL_SOURCE: ("tv-opensubtitles-residual-elimination"),
        GEMINI_TWO_PASS_SOURCE: "tv-gemini-two-pass",
    }.get(source)
