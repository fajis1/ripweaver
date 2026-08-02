"""Deterministic content-identification priority from optional user hints."""

from __future__ import annotations

import re
from typing import Literal

ContentHint = Literal["tv", "movie", "extras", "mixed"]
IdentificationStrategy = Literal["tv", "movie", "extras", "mixed-classifier"]

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


def infer_tv_context_from_disc_label(label: str | None) -> tuple[str, int] | None:
    """Use only an explicit ``Season N`` label; never infer from disc/volume N."""

    if not label:
        return None
    normalized = re.sub(r"[_-]+", " ", label)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    match = re.match(
        r"^(?P<series>.+?)\s+season\s*(?P<season>\d{1,3})(?:\s+.*)?$",
        normalized,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    series = match.group("series").strip(" ._-")
    season = int(match.group("season"))
    if not series or season > 999:
        return None
    return series, season
