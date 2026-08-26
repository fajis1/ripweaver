"""Bounded TheDiscDB identity lookup and MakeMKV-title enrichment.

The integration is independently implemented against TheDiscDB's public
GraphQL contract and permissively licensed reference data/source.  It never
rips, writes to media, or changes MakeMKV's structural inventory.  A live
lookup reads only the explicit disc filesystem root needed to calculate the
database identifier and sends only that identifier to TheDiscDB.
"""

from __future__ import annotations

import hashlib
import re
import struct
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import requests

from mkv_episode_matcher.disc.preflight import DiscInventory

THEDISCDB_GRAPHQL_URL = "https://thediscdb.com/graphql/"
CONTENT_HASH_PATTERN = re.compile(r"^[0-9A-F]{32}$")

# Public query from TheDiscDb/web.  No authentication or private values are
# included.  Keep the structural MakeMKV fields so playlist matching can be
# checked before any descriptive metadata is trusted.
DISC_BY_CONTENT_HASH_QUERY = """
query GetDiscDetailByContentHash($hash: String) {
  mediaItems(
    where: {
      releases: { some: { discs: { some: { contentHash: { eq: $hash } } } } }
    }
  ) {
    nodes {
      title
      type
      externalids { tmdb }
      releases {
        title
        discs(order: { index: ASC }) {
          index
          name
          format
          contentHash
          titles(order: { index: ASC }) {
            index
            duration
            sourceFile
            size
            segmentMap
            item { title season episode type }
          }
        }
      }
    }
  }
}
""".strip()


class TheDiscDbError(RuntimeError):
    """Raised when a bounded TheDiscDB operation cannot complete safely."""


@dataclass(frozen=True)
class DiscHashFile:
    """One path-free file identity used by TheDiscDB's content hash."""

    name: str
    size: int


@dataclass(frozen=True)
class DiscFilesystemIdentity:
    """Private in-memory identity derived from one explicit disc root."""

    content_hash: str
    global_disc_id: str | None
    format: str
    file_count: int


@dataclass(frozen=True)
class TheDiscDbTitle:
    index: int
    source_file: str
    segment_map: str | None
    duration: str | None
    size: int | None
    item_title: str | None
    item_type: str | None
    season: int | None
    episode: int | None


@dataclass(frozen=True)
class TheDiscDbDisc:
    media_title: str
    media_type: str | None
    tmdb_id: int | None
    release_title: str | None
    disc_index: int
    disc_name: str | None
    disc_format: str | None
    titles: tuple[TheDiscDbTitle, ...]


@dataclass(frozen=True)
class TheDiscDbResolution:
    """Path- and hash-free result safe to attach to a pipeline review."""

    status: str
    media_title: str | None = None
    media_type: str | None = None
    tmdb_id: int | None = None
    episode_assignments: tuple[dict[str, object], ...] = ()
    matched_title_indexes: tuple[int, ...] = ()
    unmatched_title_indexes: tuple[int, ...] = ()


def calculate_content_hash(files: Sequence[DiscHashFile]) -> str:
    """Calculate TheDiscDB's uppercase MD5 of sorted little-endian file sizes.

    The algorithm is compatible with the Apache-2.0 reference implementation
    in ``TheDiscDb.Core/DiscHash/HashingExtensions.cs``.  MD5 is used only as a
    database compatibility identifier, never for security.
    """

    if not files:
        raise TheDiscDbError("The disc hash input contains no files")
    seen_names: set[str] = set()
    ordered: list[DiscHashFile] = []
    for item in files:
        if not isinstance(item.name, str) or not item.name.strip():
            raise TheDiscDbError("The disc hash input contains an invalid filename")
        normalized_name = item.name.casefold()
        if normalized_name in seen_names:
            raise TheDiscDbError("The disc hash input contains duplicate filenames")
        if not isinstance(item.size, int) or isinstance(item.size, bool):
            raise TheDiscDbError("The disc hash input contains an invalid file size")
        if not 0 <= item.size <= (2**63 - 1):
            raise TheDiscDbError("The disc hash input contains an invalid file size")
        seen_names.add(normalized_name)
        ordered.append(item)

    digest = hashlib.md5(usedforsecurity=False)
    for item in sorted(ordered, key=lambda value: (value.name.casefold(), value.name)):
        digest.update(struct.pack("<q", item.size))
    return digest.hexdigest().upper()


def _case_insensitive_child(directory: Path, name: str) -> Path | None:
    try:
        children = tuple(directory.iterdir())
    except OSError as exc:
        raise TheDiscDbError(
            f"The disc filesystem could not be inspected ({type(exc).__name__})"
        ) from exc
    matches = [child for child in children if child.name.casefold() == name.casefold()]
    if len(matches) > 1:
        raise TheDiscDbError("The disc filesystem contains ambiguous directory names")
    return matches[0] if matches else None


def _hash_files(
    directory: Path, *, suffix: str | None = None
) -> tuple[DiscHashFile, ...]:
    try:
        children = tuple(directory.iterdir())
    except OSError as exc:
        raise TheDiscDbError(
            f"The disc filesystem could not be inspected ({type(exc).__name__})"
        ) from exc
    files: list[DiscHashFile] = []
    for child in children:
        try:
            if child.is_symlink() or not child.is_file():
                continue
            if suffix is not None and child.suffix.casefold() != suffix.casefold():
                continue
            files.append(DiscHashFile(name=child.name, size=child.stat().st_size))
        except OSError as exc:
            raise TheDiscDbError(
                f"The disc filesystem could not be inspected ({type(exc).__name__})"
            ) from exc
    return tuple(files)


def read_disc_filesystem_identity(root: Path) -> DiscFilesystemIdentity:
    """Read bounded metadata from one explicit Blu-ray/UHD or DVD root."""

    if not root.is_dir():
        raise TheDiscDbError("The selected drive has no readable disc filesystem")
    bdmv = _case_insensitive_child(root, "BDMV")
    video_ts = _case_insensitive_child(root, "VIDEO_TS")
    if bdmv is not None and bdmv.is_dir():
        stream = _case_insensitive_child(bdmv, "STREAM")
        if stream is None or not stream.is_dir():
            raise TheDiscDbError("The Blu-ray STREAM directory is unavailable")
        files = _hash_files(stream, suffix=".m2ts")
        return DiscFilesystemIdentity(
            content_hash=calculate_content_hash(files),
            # The public lookup requires only ContentHash. Avoid reading AACS
            # file contents for the unused optional GlobalDiscId.
            global_disc_id=None,
            format="Blu-ray",
            file_count=len(files),
        )
    if video_ts is not None and video_ts.is_dir():
        files = _hash_files(video_ts)
        return DiscFilesystemIdentity(
            content_hash=calculate_content_hash(files),
            global_disc_id=None,
            format="DVD",
            file_count=len(files),
        )
    raise TheDiscDbError("No Blu-ray or DVD filesystem was found on the selected drive")


def disc_root_from_device_name(device_name: str) -> Path:
    """Convert only a MakeMKV Windows drive-letter device into a rooted path."""

    match = re.fullmatch(r"([A-Za-z]):(?:[\\/])?", device_name.strip())
    if match is None:
        raise TheDiscDbError("The selected drive has no browsable filesystem root")
    return Path(f"{match.group(1).upper()}:\\")


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_int(value: object, *, minimum: int = 0) -> int | None:
    try:
        parsed = (
            int(value) if value is not None and not isinstance(value, bool) else None
        )
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed >= minimum else None


def _parse_title(value: object) -> TheDiscDbTitle | None:
    if not isinstance(value, dict):
        return None
    index = _optional_int(value.get("index"))
    source_file = _optional_string(value.get("sourceFile"))
    if index is None or source_file is None:
        return None
    item = value.get("item")
    item = item if isinstance(item, dict) else {}
    return TheDiscDbTitle(
        index=index,
        source_file=PurePosixPath(source_file.replace("\\", "/")).name,
        segment_map=_optional_string(value.get("segmentMap")),
        duration=_optional_string(value.get("duration")),
        size=_optional_int(value.get("size")),
        item_title=_optional_string(item.get("title")),
        item_type=_optional_string(item.get("type")),
        season=_optional_int(item.get("season"), minimum=0),
        episode=_optional_int(item.get("episode"), minimum=1),
    )


def _parse_database_disc(
    media: dict[str, object],
    release: dict[str, object],
    disc: object,
    normalized_hash: str,
) -> TheDiscDbDisc | None:
    if not isinstance(disc, dict):
        return None
    disc_hash = _optional_string(disc.get("contentHash"))
    disc_index = _optional_int(disc.get("index"))
    raw_titles = disc.get("titles")
    media_title = _optional_string(media.get("title"))
    if (
        disc_hash is None
        or disc_hash.upper() != normalized_hash
        or disc_index is None
        or not isinstance(raw_titles, list)
        or media_title is None
    ):
        return None
    external_ids = media.get("externalids")
    tmdb_id = (
        _optional_int(external_ids.get("tmdb"), minimum=1)
        if isinstance(external_ids, dict)
        else None
    )
    titles = tuple(
        parsed
        for parsed in (_parse_title(title) for title in raw_titles)
        if parsed is not None
    )
    return TheDiscDbDisc(
        media_title=media_title,
        media_type=_optional_string(media.get("type")),
        tmdb_id=tmdb_id,
        release_title=_optional_string(release.get("title")),
        disc_index=disc_index,
        disc_name=_optional_string(disc.get("name")),
        disc_format=_optional_string(disc.get("format")),
        titles=titles,
    )


def _iter_disc_payloads(
    nodes: list[object],
) -> Iterator[tuple[dict[str, object], dict[str, object], object]]:
    for media in nodes:
        if not isinstance(media, dict):
            continue
        releases = media.get("releases")
        if not isinstance(releases, list):
            continue
        for release in releases:
            if not isinstance(release, dict):
                continue
            discs = release.get("discs")
            if not isinstance(discs, list):
                continue
            for disc in discs:
                yield media, release, disc


def parse_lookup_response(
    payload: object, content_hash: str
) -> tuple[TheDiscDbDisc, ...]:
    """Parse only exact-hash discs from a TheDiscDB GraphQL response."""

    normalized_hash = content_hash.strip().upper()
    if CONTENT_HASH_PATTERN.fullmatch(normalized_hash) is None:
        raise TheDiscDbError("TheDiscDB content hash is invalid")
    if not isinstance(payload, dict):
        raise TheDiscDbError("TheDiscDB returned an invalid response")
    if payload.get("errors"):
        raise TheDiscDbError("TheDiscDB lookup failed safely (GraphQLError)")
    data = payload.get("data")
    media_items = data.get("mediaItems") if isinstance(data, dict) else None
    nodes = media_items.get("nodes") if isinstance(media_items, dict) else None
    if not isinstance(nodes, list):
        raise TheDiscDbError("TheDiscDB returned an invalid response")

    return tuple(
        parsed
        for parsed in (
            _parse_database_disc(media, release, disc, normalized_hash)
            for media, release, disc in _iter_disc_payloads(nodes)
        )
        if parsed is not None
    )


def _disc_signature(disc: TheDiscDbDisc) -> tuple[object, ...]:
    return (
        disc.media_title.casefold(),
        (disc.media_type or "").casefold(),
        disc.tmdb_id,
        tuple(
            (
                title.source_file.casefold(),
                _normalized_segment_map(title.segment_map),
                (title.item_title or "").casefold(),
                (title.item_type or "").casefold(),
                title.season,
                title.episode,
            )
            for title in disc.titles
        ),
    )


def select_consistent_disc(matches: Sequence[TheDiscDbDisc]) -> TheDiscDbDisc | None:
    """Collapse copied releases only when their title mappings are identical."""

    if not matches:
        return None
    first = matches[0]
    signature = _disc_signature(first)
    if any(_disc_signature(candidate) != signature for candidate in matches[1:]):
        raise TheDiscDbError("TheDiscDB returned conflicting records for this disc")
    return first


def _normalized_source_file(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    return PurePosixPath(value.strip().replace("\\", "/")).name.casefold()


def _normalized_segment_map(value: str | None) -> tuple[str, ...] | None:
    if not value or not value.strip():
        return None
    parts = tuple(part.strip().lstrip("0") or "0" for part in value.split(","))
    return parts if all(part.isdigit() for part in parts) else None


def _same_database_title(left: TheDiscDbTitle, right: TheDiscDbTitle) -> bool:
    return (
        _normalized_segment_map(left.segment_map)
        == _normalized_segment_map(right.segment_map)
        and left.item_title == right.item_title
        and left.item_type == right.item_type
        and left.season == right.season
        and left.episode == right.episode
    )


def enrich_inventory(
    inventory: DiscInventory, disc: TheDiscDbDisc
) -> TheDiscDbResolution:
    """Match local titles by source playlist and compatible segment map."""

    database_by_source: dict[str, list[TheDiscDbTitle]] = {}
    for title in disc.titles:
        source = _normalized_source_file(title.source_file)
        if source is not None:
            database_by_source.setdefault(source, []).append(title)

    assignments: list[dict[str, object]] = []
    matched: list[int] = []
    unmatched: list[int] = []
    for local in inventory.titles:
        source = _normalized_source_file(local.source_file)
        candidates = database_by_source.get(source or "", [])
        if not candidates:
            unmatched.append(local.index)
            continue
        candidate = candidates[0]
        if any(not _same_database_title(candidate, other) for other in candidates[1:]):
            unmatched.append(local.index)
            continue
        local_segments = _normalized_segment_map(local.segment_map)
        database_segments = _normalized_segment_map(candidate.segment_map)
        if (
            local_segments is not None
            and database_segments is not None
            and local_segments != database_segments
        ):
            unmatched.append(local.index)
            continue
        matched.append(local.index)
        if (
            (candidate.item_type or "").casefold() == "episode"
            and candidate.season is not None
            and candidate.episode is not None
        ):
            assignment: dict[str, object] = {
                "title_index": local.index,
                "season": candidate.season,
                "episode": candidate.episode,
                "identification_source": "thediscdb",
            }
            if candidate.item_title is not None:
                assignment["title"] = candidate.item_title
            assignments.append(assignment)

    status = "matched" if matched else "title-mismatch"
    return TheDiscDbResolution(
        status=status,
        media_title=disc.media_title,
        media_type=disc.media_type,
        tmdb_id=disc.tmdb_id,
        episode_assignments=tuple(
            sorted(assignments, key=lambda item: int(item["title_index"]))
        ),
        matched_title_indexes=tuple(sorted(matched)),
        unmatched_title_indexes=tuple(sorted(unmatched)),
    )


class TheDiscDbClient:
    """Small unauthenticated GraphQL client with redacted safe failures."""

    def __init__(
        self,
        *,
        endpoint: str = THEDISCDB_GRAPHQL_URL,
        post: Callable[..., Any] = requests.post,
    ) -> None:
        self._endpoint = endpoint
        self._post = post

    def lookup(
        self, content_hash: str, *, timeout_seconds: int = 15
    ) -> tuple[TheDiscDbDisc, ...]:
        normalized_hash = content_hash.strip().upper()
        if CONTENT_HASH_PATTERN.fullmatch(normalized_hash) is None:
            raise TheDiscDbError("TheDiscDB content hash is invalid")
        try:
            response = self._post(
                self._endpoint,
                json={
                    "query": DISC_BY_CONTENT_HASH_QUERY,
                    "variables": {"hash": normalized_hash},
                },
                headers={"User-Agent": "RipWeaver/TheDiscDB-lookup"},
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError, OSError) as exc:
            raise TheDiscDbError(
                f"TheDiscDB lookup failed safely ({type(exc).__name__})"
            ) from exc
        return parse_lookup_response(payload, normalized_hash)


def lookup_disc_metadata(
    root: Path,
    inventory: DiscInventory,
    *,
    timeout_seconds: int = 15,
    client: TheDiscDbClient | None = None,
) -> TheDiscDbResolution:
    """Resolve one explicit disc root and return only path/hash-free metadata."""

    identity = read_disc_filesystem_identity(root)
    matches = (client or TheDiscDbClient()).lookup(
        identity.content_hash, timeout_seconds=timeout_seconds
    )
    disc = select_consistent_disc(matches)
    if disc is None:
        return TheDiscDbResolution(status="not-found")
    return enrich_inventory(inventory, disc)


def inferred_content_hint(media_type: str | None) -> str | None:
    """Translate TheDiscDB's broad media type without inventing a season."""

    normalized = (media_type or "").casefold()
    if "series" in normalized or normalized in {"show", "tv"}:
        return "tv"
    if "movie" in normalized or "film" in normalized:
        return "movie"
    return None


def unique_assignment_season(
    assignments: Iterable[dict[str, object]],
) -> int | None:
    seasons = {
        int(assignment["season"])
        for assignment in assignments
        if isinstance(assignment.get("season"), int)
    }
    return next(iter(seasons)) if len(seasons) == 1 else None
