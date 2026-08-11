"""Durable, consent-gated, path-free catalogue contribution outbox."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from mkv_episode_matcher.disc.preflight import DiscInventory
from mkv_episode_matcher.disc.title_selector import parse_duration_seconds
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore

_CONTENT_HASH = re.compile(r"[0-9A-F]{32}")
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")
_SOURCE_FILE = re.compile(r"[A-Za-z0-9_.-]{1,96}")
_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,32}")
_EPISODE = re.compile(r"(?i)S(\d{1,3})E(\d{1,4})")
_VERSION_SUFFIX = re.compile(r"\s+-\s+\d{3,4}[pi]$", re.IGNORECASE)
_MATCH_SOURCES = {
    "manual_playback",
    "deterministic",
    "local_evidence",
    "gemini",
    "server_assisted",
}
_CLASSIFICATIONS = {
    "episode",
    "movie",
    "extra",
    "commentary",
    "play_all",
    "menu",
    "warning",
    "unknown",
}


class CatalogueContributionError(RuntimeError):
    """A private snapshot or path-free outbox record is unsafe or inconsistent."""


@dataclass(frozen=True)
class CatalogueSnapshotTitle:
    title_index: int
    source_file: str
    segment_map: tuple[str, ...]
    duration_seconds: int | None
    size_bytes: int | None


@dataclass(frozen=True)
class CatalogueDiscSnapshot:
    content_hash: str
    disc_fingerprint: str
    media_type: str
    release_name: str | None
    titles: tuple[CatalogueSnapshotTitle, ...]


@dataclass(frozen=True)
class PendingContribution:
    payload_sha256: str
    content_hash: str
    payload: dict[str, object]
    attempt_count: int


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _optional_size(value: object) -> int | None:
    try:
        parsed = int(value) if value is not None and not isinstance(value, bool) else 0
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def snapshot_from_inventory(  # noqa: C901 - validates a strict public projection
    *,
    content_hash: str,
    disc_fingerprint: str,
    media_type: str,
    release_name: str | None,
    inventory: DiscInventory,
    selected_title_indexes: tuple[int, ...],
) -> CatalogueDiscSnapshot:
    """Project one explicit inventory into a path-free structural snapshot."""

    normalized_hash = content_hash.strip().upper()
    normalized_fingerprint = disc_fingerprint.strip().lower()
    if _CONTENT_HASH.fullmatch(normalized_hash) is None:
        raise CatalogueContributionError("Catalogue content hash is invalid")
    if _FINGERPRINT.fullmatch(normalized_fingerprint) is None:
        raise CatalogueContributionError("Catalogue disc fingerprint is invalid")
    if media_type not in {"dvd", "bluray", "uhd"}:
        raise CatalogueContributionError("Catalogue media type is invalid")
    selected = set(selected_title_indexes)
    if not selected or len(selected) != len(selected_title_indexes):
        raise CatalogueContributionError("Catalogue title selection is invalid")
    titles: list[CatalogueSnapshotTitle] = []
    for title in inventory.titles:
        if title.index not in selected:
            continue
        source_file = (title.source_file or title.output_name or "").strip()
        if _SOURCE_FILE.fullmatch(source_file) is None:
            raise CatalogueContributionError(
                "Catalogue source identifier is unavailable or unsafe"
            )
        segments = tuple(
            part.strip()
            for part in (title.segment_map or "").split(",")
            if part.strip()
        )
        if any(_SEGMENT.fullmatch(part) is None for part in segments):
            raise CatalogueContributionError("Catalogue segment map is invalid")
        titles.append(
            CatalogueSnapshotTitle(
                title_index=title.index,
                source_file=source_file,
                segment_map=segments,
                duration_seconds=parse_duration_seconds(title.duration),
                size_bytes=_optional_size(title.attributes.get(11)),
            )
        )
    if {title.title_index for title in titles} != selected:
        raise CatalogueContributionError(
            "Catalogue snapshot does not cover every selected title"
        )
    safe_release = release_name.strip() if isinstance(release_name, str) else None
    if safe_release and len(safe_release) > 300:
        raise CatalogueContributionError("Catalogue release name is invalid")
    return CatalogueDiscSnapshot(
        content_hash=normalized_hash,
        disc_fingerprint=normalized_fingerprint,
        media_type=media_type,
        release_name=safe_release or None,
        titles=tuple(sorted(titles, key=lambda item: item.title_index)),
    )


def _episode_title(outcome_name: str, episode_id: str) -> str | None:
    stem = _VERSION_SUFFIX.sub("", Path(outcome_name).stem)
    marker = re.search(rf"(?i)\s+-\s+{re.escape(episode_id)}\s+-\s+(.+)$", stem)
    if marker is None:
        return None
    value = marker.group(1).strip()
    return value[:300] if value else None


def _series_name(history: dict[str, str | int | None]) -> str | None:
    explicit = history.get("series_name")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()[:200]
    relative = history.get("library_relative")
    if not isinstance(relative, str):
        return None
    parts = PurePosixPath(relative.replace("\\", "/")).parts
    return parts[0][:200] if parts and parts[0] not in {".", "..", "/"} else None


def _display_title(history: dict[str, str | int | None]) -> str | None:
    explicit = history.get("display_title")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()[:300]
    outcome = history.get("outcome_name")
    if not isinstance(outcome, str) or not outcome.strip():
        return None
    value = _VERSION_SUFFIX.sub("", Path(outcome).stem).strip()
    return value[:300] if value else None


def _title_payload(
    title: CatalogueSnapshotTitle,
    history: dict[str, str | int | None],
) -> dict[str, object] | None:
    match_source = history.get("match_source")
    if match_source not in _MATCH_SOURCES:
        match_source = "local_evidence"
    classification = history.get("classification")
    episode_id = history.get("episode_id")
    if isinstance(episode_id, str) and _EPISODE.fullmatch(episode_id):
        classification = "episode"
    if classification not in _CLASSIFICATIONS:
        classification = "extra"
    payload: dict[str, object] = {
        "title_index": title.title_index,
        "source_file": title.source_file,
        "segment_map": list(title.segment_map),
        "duration_seconds": title.duration_seconds,
        "size_bytes": title.size_bytes,
        "classification": classification,
        "series_name": None,
        "season_number": None,
        "episode_number": None,
        "episode_title": None,
        "movie_title": None,
        "movie_year": None,
        "display_title": None,
        "contained_title_indexes": [],
        "match_source": match_source,
    }
    if classification == "episode":
        match = _EPISODE.fullmatch(str(episode_id))
        series_name = _series_name(history)
        if match is None or series_name is None:
            return None
        payload.update({
            "series_name": series_name,
            "season_number": int(match.group(1)),
            "episode_number": int(match.group(2)),
            "episode_title": (
                history.get("display_title")
                if isinstance(history.get("display_title"), str)
                else _episode_title(str(history.get("outcome_name") or ""), episode_id)
            ),
        })
    elif classification == "movie":
        display = _display_title(history)
        if display is None:
            return None
        year = history.get("movie_year")
        payload["movie_title"] = display
        payload["movie_year"] = year if isinstance(year, int) else None
    else:
        payload["display_title"] = _display_title(history)
    return payload


def build_contribution_payload(
    snapshot: CatalogueDiscSnapshot,
    history: dict[int, dict[str, str | int | None]],
) -> dict[str, object] | None:
    """Build only when every selected structural title has a durable match."""

    titles: list[dict[str, object]] = []
    for title in snapshot.titles:
        outcome = history.get(title.title_index)
        if outcome is None:
            return None
        payload = _title_payload(title, outcome)
        if payload is None:
            return None
        titles.append(payload)
    return {
        "schema_version": 2,
        "content_hash": snapshot.content_hash,
        "media_type": snapshot.media_type,
        "release_name": snapshot.release_name,
        "edition": None,
        "titles": titles,
    }


class CatalogueContributionStore:
    """Private SQLite snapshots and retryable public-metadata outbox."""

    def __init__(self, database_path: Path):
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS catalogue_disc_snapshots (
                    content_hash TEXT PRIMARY KEY,
                    disc_fingerprint TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    release_name TEXT,
                    titles_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalogue_contribution_outbox (
                    payload_sha256 TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    next_attempt_at TEXT NOT NULL,
                    last_error_type TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_catalogue_outbox_pending
                    ON catalogue_contribution_outbox(state, next_attempt_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def record_snapshot(self, snapshot: CatalogueDiscSnapshot) -> None:
        titles_json = _canonical_json([
            {
                "title_index": title.title_index,
                "source_file": title.source_file,
                "segment_map": list(title.segment_map),
                "duration_seconds": title.duration_seconds,
                "size_bytes": title.size_bytes,
            }
            for title in snapshot.titles
        ])
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO catalogue_disc_snapshots (
                    content_hash, disc_fingerprint, media_type, release_name,
                    titles_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET
                    disc_fingerprint = excluded.disc_fingerprint,
                    media_type = excluded.media_type,
                    release_name = excluded.release_name,
                    titles_json = excluded.titles_json,
                    updated_at = excluded.updated_at
                """,
                (
                    snapshot.content_hash,
                    snapshot.disc_fingerprint,
                    snapshot.media_type,
                    snapshot.release_name,
                    titles_json,
                    self._now(),
                ),
            )
            connection.commit()

    def _snapshots(self) -> tuple[CatalogueDiscSnapshot, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM catalogue_disc_snapshots ORDER BY content_hash"
            ).fetchall()
        snapshots: list[CatalogueDiscSnapshot] = []
        for row in rows:
            raw_titles = json.loads(row["titles_json"])
            snapshots.append(
                CatalogueDiscSnapshot(
                    content_hash=row["content_hash"],
                    disc_fingerprint=row["disc_fingerprint"],
                    media_type=row["media_type"],
                    release_name=row["release_name"],
                    titles=tuple(
                        CatalogueSnapshotTitle(
                            title_index=item["title_index"],
                            source_file=item["source_file"],
                            segment_map=tuple(item["segment_map"]),
                            duration_seconds=item["duration_seconds"],
                            size_bytes=item["size_bytes"],
                        )
                        for item in raw_titles
                    ),
                )
            )
        return tuple(snapshots)

    def prepare_ready(self, pipeline_store: PipelineQueueStore) -> int:
        prepared = 0
        for snapshot in self._snapshots():
            payload = build_contribution_payload(
                snapshot,
                pipeline_store.catalogue_title_history(snapshot.disc_fingerprint),
            )
            if payload is None:
                continue
            encoded = _canonical_json(payload)
            payload_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            now = self._now()
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO catalogue_contribution_outbox (
                        payload_sha256, content_hash, payload_json, state,
                        attempt_count, next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
                    """,
                    (
                        payload_sha256,
                        snapshot.content_hash,
                        encoded,
                        now,
                        now,
                        now,
                    ),
                )
                prepared += max(0, cursor.rowcount)
                connection.commit()
        return prepared

    def pending(self, *, limit: int = 1) -> tuple[PendingContribution, ...]:
        if not 1 <= limit <= 100:
            raise CatalogueContributionError("Contribution outbox limit is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_sha256, content_hash, payload_json, attempt_count
                FROM catalogue_contribution_outbox
                WHERE state = 'pending' AND next_attempt_at <= ?
                ORDER BY created_at LIMIT ?
                """,
                (self._now(), limit),
            ).fetchall()
        return tuple(
            PendingContribution(
                payload_sha256=row["payload_sha256"],
                content_hash=row["content_hash"],
                payload=json.loads(row["payload_json"]),
                attempt_count=int(row["attempt_count"]),
            )
            for row in rows
        )

    def mark_sent(self, payload_sha256: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE catalogue_contribution_outbox
                SET state = 'sent', last_error_type = NULL, updated_at = ?
                WHERE payload_sha256 = ? AND state = 'pending'
                """,
                (self._now(), payload_sha256),
            )
            if cursor.rowcount != 1:
                raise CatalogueContributionError("Contribution is not pending")
            connection.commit()

    def mark_failed(self, payload_sha256: str, *, error_type: str) -> None:
        safe_error = (
            error_type
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,95}", error_type)
            else "Error"
        )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt_count FROM catalogue_contribution_outbox "
                "WHERE payload_sha256 = ? AND state = 'pending'",
                (payload_sha256,),
            ).fetchone()
            if row is None:
                raise CatalogueContributionError("Contribution is not pending")
            attempts = int(row["attempt_count"]) + 1
            delay = min(3_600, 30 * (2 ** min(attempts - 1, 7)))
            next_attempt = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
            connection.execute(
                """
                UPDATE catalogue_contribution_outbox
                SET attempt_count = ?, next_attempt_at = ?, last_error_type = ?,
                    updated_at = ? WHERE payload_sha256 = ?
                """,
                (attempts, next_attempt, safe_error, self._now(), payload_sha256),
            )
            connection.commit()

    def status(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM catalogue_contribution_outbox "
                "GROUP BY state"
            ).fetchall()
            snapshots = connection.execute(
                "SELECT COUNT(*) AS count FROM catalogue_disc_snapshots"
            ).fetchone()
        counts = {row["state"]: int(row["count"]) for row in rows}
        return {
            "snapshots": int(snapshots["count"]),
            "pending": counts.get("pending", 0),
            "sent": counts.get("sent", 0),
        }
