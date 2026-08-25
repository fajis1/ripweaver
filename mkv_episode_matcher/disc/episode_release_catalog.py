"""Reviewed, path-free episode assignments for known multi-season disc releases."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class EpisodeAssignment:
    title_index: int
    season: int
    episode: int
    title: str


@dataclass(frozen=True)
class EpisodeReleaseCatalog:
    catalog_id: str
    series_name: str
    label_pattern: re.Pattern[str]
    assignments: tuple[EpisodeAssignment, ...]


_CATALOGS = (
    EpisodeReleaseCatalog(
        catalog_id="faerie-tale-theatre-volume-4-aired",
        series_name="Faerie Tale Theatre",
        label_pattern=re.compile(r"^FAERIE[_ ]TALE[_ ]THEATRE[_ ]4$", re.IGNORECASE),
        assignments=(
            EpisodeAssignment(0, 3, 5, "Snow White and the Seven Dwarfs"),
            EpisodeAssignment(1, 3, 6, "Beauty and the Beast"),
            EpisodeAssignment(
                2, 3, 7, "The Boy Who Left Home to Find Out About the Shivers"
            ),
            EpisodeAssignment(3, 4, 1, "The Three Little Pigs"),
        ),
    ),
)


def catalog_by_id(catalog_id: str) -> EpisodeReleaseCatalog | None:
    return next((item for item in _CATALOGS if item.catalog_id == catalog_id), None)


def match_episode_release(
    disc_label: str, title_indexes: tuple[int, ...]
) -> EpisodeReleaseCatalog | None:
    """Return a catalogue only when its label and complete title set agree."""

    normalized = re.sub(r"\s+", " ", disc_label.strip())
    for catalog in _CATALOGS:
        expected = tuple(item.title_index for item in catalog.assignments)
        if catalog.label_pattern.fullmatch(normalized) and title_indexes == expected:
            return catalog
    return None


def public_assignments(catalog: EpisodeReleaseCatalog) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "title_index": item.title_index,
            "season": item.season,
            "episode": item.episode,
            "title": item.title,
        }
        for item in catalog.assignments
    )
