"""Deterministic content-identification priority from optional user hints."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ContentHint = Literal["tv", "movie", "extras", "mixed"]
IdentificationStrategy = Literal["tv", "movie", "extras", "mixed-classifier"]


@dataclass(frozen=True)
class DiscLabelContext:
    """Unambiguous TV structure parsed from a human-readable disc label."""

    series_hint: str
    season: int
    disc_number: int | None = None


_ORDERS: dict[ContentHint | None, tuple[IdentificationStrategy, ...]] = {
    None: ("mixed-classifier", "tv", "movie", "extras"),
    "tv": ("tv", "movie", "extras"),
    "movie": ("movie", "tv", "extras"),
    "extras": ("extras", "movie", "tv"),
    "mixed": ("mixed-classifier", "tv", "movie", "extras"),
}


def identification_order(
    hint: ContentHint | None,
) -> tuple[IdentificationStrategy, ...]:
    """Return preferred-first strategies; every hint retains safe fallbacks."""

    try:
        return _ORDERS[hint]
    except (KeyError, TypeError) as exc:
        raise ValueError("Unsupported disc content hint") from exc


def parse_tv_disc_label_context(label: str | None) -> DiscLabelContext | None:
    """Parse explicit season/disc tokens without treating a volume as a season."""

    if not label:
        return None
    normalized = re.sub(r"[_-]+", " ", label)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    season_matches = tuple(
        re.finditer(
            r"(?<![A-Za-z0-9])(?:season\s*|s\s*0*)(?P<number>\d{1,3})(?!\d)",
            normalized,
            flags=re.IGNORECASE,
        )
    )
    if len(season_matches) != 1:
        return None
    season_match = season_matches[0]
    season = int(season_match.group("number"))
    disc_matches = tuple(
        re.finditer(
            r"(?:\b(?:disc|disk|dvd)|(?<![A-Za-z])d)\s*0*(?P<number>\d{1,3})(?!\d)",
            normalized,
            flags=re.IGNORECASE,
        )
    )
    if len(disc_matches) > 1:
        return None
    disc_number = int(disc_matches[0].group("number")) if disc_matches else None
    spans = [season_match.span(), *(match.span() for match in disc_matches)]
    pieces = []
    cursor = 0
    for start, end in sorted(spans):
        pieces.append(normalized[cursor:start])
        cursor = end
    pieces.append(normalized[cursor:])
    series = re.sub(r"\s+", " ", " ".join(pieces)).strip(" ._-")
    if not series or season > 999:
        return None
    return DiscLabelContext(series, season, disc_number)


def infer_tv_context_from_disc_label(label: str | None) -> tuple[str, int] | None:
    """Return the backward-compatible series/season pair for a disc label."""

    context = parse_tv_disc_label_context(label)
    if context is None:
        return None
    return context.series_hint, context.season


def infer_release_name_from_disc_label(label: str | None) -> str | None:
    """Return a useful release name without treating a volume as a season."""

    if not label:
        return None
    normalized = re.sub(r"[_-]+", " ", label)
    normalized = re.sub(r"\s+", " ", normalized).strip(" ._-")
    if not normalized:
        return None
    normalized = re.sub(
        r"\s+(?:disc|disk|dvd|volume|vol)\s*\d{1,3}$",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).strip()
    normalized = re.sub(
        r"\s+csr\s+dim\s*\d{1,3}$",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).strip()
    if re.search(r"[_-]\d{1,3}$", label):
        normalized = re.sub(r"\s+\d{1,3}$", "", normalized).strip()
    return normalized or None
