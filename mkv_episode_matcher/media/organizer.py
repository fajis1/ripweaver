"""Plan-only Plex/Jellyfin television organization.

This module reads a saved sequence plan, authoritative catalogue metadata, and
destination directory entries. It never reads media contents or mutates either
staging or library files.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_EPISODE_ID = re.compile(r"^S(\d{2})E(\d{2,3})$")
_INVALID_COMPONENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class OrganizationPlanError(RuntimeError):
    """Raised when a plan-only organization proposal is unsafe."""


@dataclass(frozen=True)
class SequenceAssignment:
    file_id: str
    episode_id: str


@dataclass(frozen=True)
class OrganizationItem:
    file_id: str
    episode_id: str
    relative_destination: str
    status: str
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class OrganizationPlan:
    mode: str
    series_name: str
    item_count: int
    proposed_count: int
    review_count: int
    missing_directories: tuple[str, ...]
    items: tuple[OrganizationItem, ...]

    def safe_report(self) -> dict[str, object]:
        return asdict(self)


def sanitize_media_component(value: str) -> str:
    """Return one deterministic Windows-safe media-library component."""

    cleaned = _INVALID_COMPONENT.sub(" ", value)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" .")
    if not cleaned:
        raise OrganizationPlanError("Media name has no usable characters")
    if cleaned.upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def build_episode_filename(
    series_name: str,
    episode_id: str,
    episode_title: str,
    *,
    maximum_characters: int = 240,
) -> str:
    """Build a canonical collision-checkable Jellyfin/Plex MKV filename."""

    series = sanitize_media_component(series_name)
    title = sanitize_media_component(episode_title)
    if _EPISODE_ID.fullmatch(episode_id) is None:
        raise OrganizationPlanError("Episode assignment contains an invalid ID")
    fixed = f"{series} - {episode_id} - "
    extension = ".mkv"
    available = maximum_characters - len(fixed) - len(extension)
    if available < 1:
        raise OrganizationPlanError("Series name is too long for a safe filename")
    title = title[:available].rstrip(" .")
    return f"{fixed}{title}{extension}"


def _assignments_from_group(group: object) -> list[SequenceAssignment]:
    if not isinstance(group, dict) or group.get("disposition") != "proposed":
        raise OrganizationPlanError("A sequence group still requires review")
    raw_items = group.get("items")
    if not isinstance(raw_items, list):
        raise OrganizationPlanError("Saved sequence group items are invalid")
    assignments: list[SequenceAssignment] = []
    for item in raw_items:
        try:
            file_id = str(item["file_id"])
            episode_id = str(item["proposed_episode"])
        except (KeyError, TypeError) as exc:
            raise OrganizationPlanError(
                "Saved sequence assignment fields are invalid"
            ) from exc
        if (
            _SAFE_ID.fullmatch(file_id) is None
            or _EPISODE_ID.fullmatch(episode_id) is None
        ):
            raise OrganizationPlanError(
                "Saved sequence assignment contains an invalid ID"
            )
        assignments.append(SequenceAssignment(file_id, episode_id))
    return assignments


def load_sequence_assignments(path: Path) -> tuple[SequenceAssignment, ...]:
    """Load only a fully proposed, path-free saved sequence plan."""

    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload["mode"] != "saved-disc-sequence-plan"
            or payload["disposition"] != "proposed"
        ):
            raise TypeError
        raw_groups = payload["groups"]
        if not isinstance(raw_groups, list):
            raise TypeError
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise OrganizationPlanError(
            "Saved sequence plan is invalid or still requires review"
        ) from exc

    assignments: list[SequenceAssignment] = []
    for group in raw_groups:
        assignments.extend(_assignments_from_group(group))
    if not assignments:
        raise OrganizationPlanError("Saved sequence plan contains no assignments")
    if len({item.file_id for item in assignments}) != len(assignments):
        raise OrganizationPlanError("Saved sequence file IDs must be unique")
    if len({item.episode_id for item in assignments}) != len(assignments):
        raise OrganizationPlanError("Saved sequence episode IDs must be unique")
    return tuple(assignments)


def _catalog_by_id(
    catalog: tuple[EpisodeCatalogEntry, ...],
) -> dict[str, EpisodeCatalogEntry]:
    by_id = {entry.episode_id: entry for entry in catalog}
    if len(by_id) != len(catalog):
        raise OrganizationPlanError("Episode catalogue IDs must be unique")
    return by_id


def _existing_names(directory: Path) -> tuple[str, ...]:
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise OrganizationPlanError("A planned season destination is not a directory")
    try:
        return tuple(entry.name for entry in directory.iterdir() if entry.is_file())
    except OSError as exc:
        raise OrganizationPlanError(
            f"Destination names could not be inspected: {type(exc).__name__}"
        ) from exc


def _episode_conflicts(
    filename: str,
    episode_id: str,
    existing_names: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    exact = tuple(
        name for name in existing_names if name.casefold() == filename.casefold()
    )
    if exact:
        return "review-existing-destination", exact
    episode_pattern = re.compile(rf"(?i)(?<![A-Z0-9]){re.escape(episode_id)}(?!\d)")
    episode_matches = tuple(
        name for name in existing_names if episode_pattern.search(name)
    )
    if episode_matches:
        return "review-existing-episode", episode_matches
    return "proposed", ()


def plan_tv_organization(
    assignments: tuple[SequenceAssignment, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    *,
    library_root: Path,
    series_name: str,
) -> OrganizationPlan:
    """Inspect destination names and produce a non-mutating relative plan."""

    if not library_root.is_dir():
        raise OrganizationPlanError("TV library root must be an existing directory")
    if "/" in series_name or "\\" in series_name or series_name.strip() in {".", ".."}:
        raise OrganizationPlanError("Series name must be one safe path component")
    series_component = sanitize_media_component(series_name)
    catalog_by_id = _catalog_by_id(catalog)
    missing_episode_ids = {
        item.episode_id for item in assignments
    } - catalog_by_id.keys()
    if missing_episode_ids:
        raise OrganizationPlanError(
            "Sequence assignments are outside the authoritative catalogue"
        )

    items: list[OrganizationItem] = []
    missing_directories: set[str] = set()
    planned_destinations: set[str] = set()
    for assignment in assignments:
        entry = catalog_by_id[assignment.episode_id]
        season_name = f"Season {entry.season:02d}"
        filename = build_episode_filename(
            series_component,
            assignment.episode_id,
            entry.title,
        )
        relative = Path(series_component) / season_name / filename
        relative_text = relative.as_posix()
        destination_key = relative_text.casefold()
        if destination_key in planned_destinations:
            raise OrganizationPlanError(
                "Multiple assignments produce the same destination"
            )
        planned_destinations.add(destination_key)

        season_directory = library_root / series_component / season_name
        if not season_directory.exists():
            missing_directories.add((Path(series_component) / season_name).as_posix())
        status, conflicts = _episode_conflicts(
            filename,
            assignment.episode_id,
            _existing_names(season_directory),
        )
        items.append(
            OrganizationItem(
                file_id=assignment.file_id,
                episode_id=assignment.episode_id,
                relative_destination=relative_text,
                status=status,
                conflicts=conflicts,
            )
        )

    review_count = sum(item.status != "proposed" for item in items)
    return OrganizationPlan(
        mode="tv-organization-plan",
        series_name=series_component,
        item_count=len(items),
        proposed_count=len(items) - review_count,
        review_count=review_count,
        missing_directories=tuple(sorted(missing_directories)),
        items=tuple(items),
    )


def write_safe_organization_plan(path: Path, plan: OrganizationPlan) -> Path:
    """Write a new relative-path plan without overwriting."""

    if path.exists():
        raise OrganizationPlanError("Organization plan exists; refusing overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(plan.safe_report(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path
