"""Authoritative, path-free episode catalogues for unmatched planning."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from rapidfuzz import fuzz


class EpisodeCatalogError(RuntimeError):
    """Raised when provider metadata cannot form a safe episode catalogue."""


@dataclass(frozen=True, order=True)
class EpisodeCatalogEntry:
    episode_id: str
    season: int
    episode: int
    title: str
    overview: str
    runtime_seconds: float | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogCandidateScore:
    episode_id: str
    title_score: float
    overview_score: float
    runtime_score: float
    combined_score: float


CatalogFetcher = Callable[[str], dict]
_EPISODE_ID = re.compile(r"^S(\d{2})E(\d{2,3})$")
_TEXT_TOKEN = re.compile(r"[a-z0-9']+")


def _normalized_text(value: str) -> str:
    return " ".join(_TEXT_TOKEN.findall(value.lower()))


def _aired_season_numbers(show: dict) -> list[int]:
    raw_seasons = show.get("seasons")
    if not isinstance(raw_seasons, list):
        raise EpisodeCatalogError("TMDb show metadata did not include seasons")
    return sorted({
        int(item["season_number"])
        for item in raw_seasons
        if isinstance(item, dict)
        and isinstance(item.get("season_number"), int)
        and item["season_number"] > 0
    })


def _entry_from_tmdb(
    season_number: int,
    item: object,
) -> EpisodeCatalogEntry | None:
    if not isinstance(item, dict):
        return None
    episode_number = item.get("episode_number")
    title = item.get("name")
    if not isinstance(episode_number, int) or episode_number < 1:
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    runtime_minutes = item.get("runtime")
    runtime_seconds = (
        float(runtime_minutes * 60)
        if isinstance(runtime_minutes, int | float) and runtime_minutes > 0
        else None
    )
    overview = item.get("overview")
    return EpisodeCatalogEntry(
        episode_id=f"S{season_number:02d}E{episode_number:02d}",
        season=season_number,
        episode=episode_number,
        title=title.strip(),
        overview=overview.strip() if isinstance(overview, str) else "",
        runtime_seconds=runtime_seconds,
    )


def validate_catalog(
    entries: list[EpisodeCatalogEntry],
) -> tuple[EpisodeCatalogEntry, ...]:
    """Validate IDs, ordering metadata, and uniqueness."""

    seen: set[str] = set()
    for entry in entries:
        match = _EPISODE_ID.fullmatch(entry.episode_id)
        if match is None:
            raise EpisodeCatalogError("Episode catalogue contains an invalid ID")
        if (int(match.group(1)), int(match.group(2))) != (
            entry.season,
            entry.episode,
        ):
            raise EpisodeCatalogError(
                "Episode catalogue ID does not match its season and episode"
            )
        if entry.episode_id in seen:
            raise EpisodeCatalogError("Episode catalogue IDs must be unique")
        if entry.season < 0 or entry.episode < 1:
            raise EpisodeCatalogError("Episode catalogue numbering is invalid")
        if not entry.title.strip():
            raise EpisodeCatalogError("Episode catalogue titles are required")
        if entry.runtime_seconds is not None and entry.runtime_seconds <= 0:
            raise EpisodeCatalogError("Episode runtime must be positive")
        seen.add(entry.episode_id)
    return tuple(sorted(entries, key=lambda item: (item.season, item.episode)))


def build_tmdb_aired_catalog(
    show_id: int,
    fetch_json: CatalogFetcher,
) -> tuple[EpisodeCatalogEntry, ...]:
    """Build an aired-order catalogue from TMDb show and season responses."""

    if show_id <= 0:
        raise EpisodeCatalogError("TMDb show ID must be positive")
    show = fetch_json(f"/tv/{show_id}")
    season_numbers = _aired_season_numbers(show)
    if not season_numbers:
        raise EpisodeCatalogError("TMDb show has no aired seasons")

    entries: list[EpisodeCatalogEntry] = []
    for season_number in season_numbers:
        season_data = fetch_json(f"/tv/{show_id}/season/{season_number}")
        raw_episodes = season_data.get("episodes")
        if not isinstance(raw_episodes, list):
            raise EpisodeCatalogError("TMDb season metadata did not include episodes")
        for item in raw_episodes:
            entry = _entry_from_tmdb(season_number, item)
            if entry is not None:
                entries.append(entry)
    if not entries:
        raise EpisodeCatalogError("TMDb returned no usable aired episodes")
    return validate_catalog(entries)


def write_episode_catalog(
    path: Path,
    entries: tuple[EpisodeCatalogEntry, ...],
) -> Path:
    """Write a new path-free authoritative catalogue without overwriting."""

    validated = validate_catalog(list(entries))
    if not validated:
        raise EpisodeCatalogError("Episode catalogue contains no entries")
    if path.exists():
        raise EpisodeCatalogError("Episode catalogue report exists; refusing overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mode": "aired-episode-catalog",
                "episodes": [entry.to_dict() for entry in validated],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _runtime_score(
    media_seconds: float,
    episode_seconds: float | None,
    *,
    tolerance_seconds: float = 600.0,
) -> float:
    if episode_seconds is None:
        return 0.5
    difference = abs(media_seconds - episode_seconds)
    return max(0.0, 1.0 - difference / tolerance_seconds)


def rank_catalog_candidates(
    transcript_excerpts: tuple[str, ...],
    duration_seconds: float,
    catalog: tuple[EpisodeCatalogEntry, ...],
    *,
    top_k: int = 10,
) -> tuple[CatalogCandidateScore, ...]:
    """Rank authoritative metadata locally before considering an LLM call."""

    if not transcript_excerpts or not catalog:
        return ()
    if duration_seconds <= 0 or top_k <= 0:
        raise ValueError("Catalogue ranking settings must be positive")
    query = _normalized_text(" ".join(transcript_excerpts))
    if not query:
        return ()

    ranked: list[CatalogCandidateScore] = []
    for entry in catalog:
        title_score = fuzz.token_set_ratio(query, _normalized_text(entry.title)) / 100.0
        overview_score = (
            fuzz.partial_ratio(query, _normalized_text(entry.overview)) / 100.0
            if entry.overview
            else 0.0
        )
        runtime_score = _runtime_score(
            duration_seconds,
            entry.runtime_seconds,
        )
        text_score = max(title_score, overview_score)
        combined_score = 0.85 * text_score + 0.15 * runtime_score
        ranked.append(
            CatalogCandidateScore(
                episode_id=entry.episode_id,
                title_score=title_score,
                overview_score=overview_score,
                runtime_score=runtime_score,
                combined_score=combined_score,
            )
        )
    ranked.sort(
        key=lambda item: (
            item.combined_score,
            item.title_score,
            item.overview_score,
            item.episode_id,
        ),
        reverse=True,
    )
    return tuple(ranked[:top_k])
