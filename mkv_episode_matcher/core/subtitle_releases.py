"""Pure release-edition inference and OpenSubtitles option ranking.

The provider may return several subtitle releases for one episode.  These
helpers classify the provider metadata before any download occurs and keep a
small, deterministic set for dialogue matching.  Release metadata is useful
evidence, but never overrides the actual transcript/subtitle comparison.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SubtitleReleaseMatch = Literal["exact", "compatible", "generic", "unresolved"]

_SUPERFAN = re.compile(r"\bsuper[ ._-]*fan(?:[ ._-]+episodes?)?\b", re.IGNORECASE)
_EXTENDED = re.compile(
    r"\b(?:extended(?:[ ._-]+cut|[ ._-]+edition)?|uncut)\b", re.IGNORECASE
)
_UNRATED = re.compile(r"\bunrated(?:[ ._-]+cut|[ ._-]+edition)?\b", re.IGNORECASE)
_SUPERCUT = re.compile(r"\bsuper[ ._-]*cut\b", re.IGNORECASE)
_DIRECTORS_CUT = re.compile(r"\bdirector'?s[ ._-]+cut\b", re.IGNORECASE)
_PEACOCK = re.compile(r"\b(?:peacock|pcok|playweb)\b", re.IGNORECASE)
_EDITION_WORDS = re.compile(
    r"\b(?:super[ ._-]*fan|super[ ._-]*cut|extended|uncut|unrated"
    r"|director'?s[ ._-]+cut)\b",
    re.IGNORECASE,
)
_ALTERED_CUT = re.compile(
    r"\b(?:super[ ._-]*fan(?:[ ._-]+episodes?)?|super[ ._-]*cut"
    r"|extended(?:[ ._-]+cut|[ ._-]+edition)?|uncut"
    r"|unrated(?:[ ._-]+cut|[ ._-]+edition)?|director'?s[ ._-]+cut)\b",
    re.IGNORECASE,
)

_FAILOVER_QUERY_LABELS = (
    ("superfan", "Superfan Episodes"),
    ("extended", "Extended"),
    ("extended", "Uncut"),
    ("unrated", "Unrated"),
    ("supercut", "Supercut"),
    ("directors_cut", "Director's Cut"),
)


@dataclass(frozen=True)
class SubtitleReleaseProfile:
    """A release hint inferred without consulting media contents."""

    key: str | None
    display_name: str
    canonical_series_name: str

    @property
    def understood(self) -> bool:
        return self.key is not None


@dataclass(frozen=True)
class RankedSubtitleOption:
    """One provider result plus its path-free release classification."""

    candidate: object
    release_name: str
    release_match: SubtitleReleaseMatch
    provider_order: int


def _clean_name(value: str) -> str:
    return " ".join(value.replace("_", " ").replace(".", " ").split()).strip()


def infer_subtitle_release_profile(
    show_name: str, video_files: list[Path] | tuple[Path, ...] | None = None
) -> SubtitleReleaseProfile:
    """Infer a known edition while retaining a canonical search fallback."""

    values = [show_name]
    values.extend(path.name for path in video_files or ())
    combined = " ".join(values)
    if _SUPERFAN.search(combined):
        canonical = _SUPERFAN.sub(" ", show_name)
        canonical = _clean_name(canonical).strip(" -:()[]") or _clean_name(show_name)
        return SubtitleReleaseProfile(
            key="superfan",
            display_name="Superfan / extended cut",
            canonical_series_name=canonical,
        )
    if _EXTENDED.search(combined):
        canonical = _EXTENDED.sub(" ", show_name)
        canonical = _clean_name(canonical).strip(" -:()[]") or _clean_name(show_name)
        return SubtitleReleaseProfile(
            key="extended",
            display_name="Extended edition",
            canonical_series_name=canonical,
        )
    for pattern, key, display_name in (
        (_UNRATED, "unrated", "Unrated edition"),
        (_SUPERCUT, "supercut", "Supercut edition"),
        (_DIRECTORS_CUT, "directors_cut", "Director's cut"),
    ):
        if pattern.search(combined):
            canonical = pattern.sub(" ", show_name)
            canonical = _clean_name(canonical).strip(" -:()[]") or _clean_name(
                show_name
            )
            return SubtitleReleaseProfile(
                key=key,
                display_name=display_name,
                canonical_series_name=canonical,
            )
    return SubtitleReleaseProfile(
        key=None,
        display_name="Unresolved edition",
        canonical_series_name=_clean_name(show_name),
    )


def subtitle_candidate_release_name(candidate: object) -> str:
    """Collect bounded release text exposed by different client versions."""

    values: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str):
            cleaned = " ".join(value.split())[:320]
            if cleaned and cleaned not in values:
                values.append(cleaned)

    add(getattr(candidate, "file_name", None))
    add(getattr(candidate, "release", None))
    attributes = getattr(candidate, "attributes", None)
    if isinstance(attributes, dict):
        add(attributes.get("release"))
        add(attributes.get("feature_details"))
    elif attributes is not None:
        add(getattr(attributes, "release", None))
        add(getattr(attributes, "feature_details", None))
    files = getattr(candidate, "files", None)
    if isinstance(files, list | tuple):
        for item in files[:4]:
            if isinstance(item, dict):
                add(item.get("file_name"))
            else:
                add(getattr(item, "file_name", None))
    return " | ".join(values) or "Unlabeled subtitle release"


def classify_subtitle_release(
    release_name: str, profile: SubtitleReleaseProfile
) -> SubtitleReleaseMatch:
    """Classify provider metadata against the requested edition."""

    if not profile.understood:
        return "unresolved"
    if profile.key == "superfan":
        if _SUPERFAN.search(release_name):
            return "exact"
        if _ALTERED_CUT.search(release_name) or _PEACOCK.search(release_name):
            return "compatible"
        return "generic"
    if profile.key == "extended":
        if _EXTENDED.search(release_name):
            return "exact"
        return (
            "compatible"
            if _ALTERED_CUT.search(release_name) or _PEACOCK.search(release_name)
            else "generic"
        )
    exact_pattern = {
        "unrated": _UNRATED,
        "supercut": _SUPERCUT,
        "directors_cut": _DIRECTORS_CUT,
    }.get(profile.key)
    if exact_pattern is not None:
        if exact_pattern.search(release_name):
            return "exact"
        return (
            "compatible"
            if _ALTERED_CUT.search(release_name) or _PEACOCK.search(release_name)
            else "generic"
        )
    return "unresolved"


def release_match_priority(value: SubtitleReleaseMatch | str | None) -> int:
    return {
        "exact": 3,
        "compatible": 2,
        "generic": 1,
        "unresolved": 0,
    }.get(value or "unresolved", 0)


def subtitle_release_family(release_name: str) -> str:
    """Return one stable altered-cut family for bounded failover diversity."""

    for pattern, family in (
        (_SUPERFAN, "superfan"),
        (_EXTENDED, "extended"),
        (_UNRATED, "unrated"),
        (_SUPERCUT, "supercut"),
        (_DIRECTORS_CUT, "directors_cut"),
        (_PEACOCK, "peacock"),
    ):
        if pattern.search(release_name):
            return family
    return "generic"


def select_subtitle_release_options(
    candidates: list[object] | tuple[object, ...],
    profile: SubtitleReleaseProfile,
    *,
    maximum: int = 2,
) -> tuple[RankedSubtitleOption, ...]:
    """Keep multiple distinct releases, bounded per episode.

    Exact-edition candidates rank first.  When the edition is unknown, the
    provider order remains the tie-break and two distinct release labels are
    retained so dialogue evidence can choose between them.
    """

    if maximum < 1:
        return ()
    ranked = []
    for index, candidate in enumerate(candidates):
        release_name = subtitle_candidate_release_name(candidate)
        ranked.append(
            RankedSubtitleOption(
                candidate=candidate,
                release_name=release_name,
                release_match=classify_subtitle_release(release_name, profile),
                provider_order=index,
            )
        )
    ranked.sort(
        key=lambda item: (
            -release_match_priority(item.release_match),
            item.provider_order,
        )
    )
    selected = []
    seen = set()
    for item in ranked:
        signature = re.sub(r"[^a-z0-9]+", "", item.release_name.casefold())
        if signature in seen:
            continue
        seen.add(signature)
        selected.append(item)
        if len(selected) == maximum:
            break
    return tuple(selected)


def release_profile_query(profile: SubtitleReleaseProfile, season: int) -> str:
    """Return a canonical fallback query without edition packaging words."""

    return f"{profile.canonical_series_name} S{season:02d}".strip()


def alternate_release_queries(
    profile: SubtitleReleaseProfile, season: int
) -> tuple[str, ...]:
    """Return a bounded set of edition aliases for a failed evidence pass.

    These queries are deliberately separate from the normal lookup.  Callers
    should use them only after the canonical series and season are established
    and usable dialogue fails against the already-retained references.
    """

    queries = []
    for key, label in _FAILOVER_QUERY_LABELS:
        if profile.key == key:
            continue
        queries.append(f"{profile.canonical_series_name} {label} S{season:02d}".strip())
    return tuple(queries[:6])


def release_metadata_present(value: str) -> bool:
    """Identify edition-bearing provider text for safe diagnostics/tests."""

    return bool(_EDITION_WORDS.search(value) or _PEACOCK.search(value))
