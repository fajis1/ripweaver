"""Durable item queue with globally serialized downstream media work."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mkv_episode_matcher.disc.rip_manifest import MediaContext
from mkv_episode_matcher.disc.ripper import (
    RipJob,
    RipResult,
    resolve_final_output,
    resolve_job_output,
)
from mkv_episode_matcher.pipeline import PipelineArtifact


class PipelineQueueError(RuntimeError):
    """Raised when durable downstream queue state cannot advance safely."""


DOWNSTREAM_STAGES = ("identify", "transcode", "organize")
_MEDIA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RIP_BASENAME = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,81}--)?"
    r"disc-[0-9]+-([0-9a-f]{16})-title-([0-9]{3})\.mkv"
)
_MATCHING_OUTCOMES = {"completed", "failed"}
_MATCHING_FAILURE_STAGES = {
    "selection",
    "catalogue",
    "evidence",
    "local-sequence",
    "opensubtitles",
    "gemini",
    "content-fallback",
    "analysis",
}
_MATCHING_PROVIDER_BRANCHES = {
    "tv-local",
    "tv-opensubtitles",
    "tv-gemini",
    "tv-play-all",
    "tv-movie",
    "movie-bonus",
    "tv-bonus",
    "gemini-synthesis",
}
_SILENT_VIDEO_CLASSIFICATIONS = {
    "likely_warning_screen",
    "likely_disc_menu",
    "text_detected",
    "no_text_detected",
}
_SAFE_DIAGNOSTIC_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,95}")


def _catalogue_history_metadata(
    identified: dict[str, object],
) -> tuple[
    str,
    str,
    str | None,
    str | None,
    int | None,
    str | None,
    int | None,
]:
    order = identified.get("identification_order")
    branches = (
        tuple(value.casefold() for value in order if isinstance(value, str))
        if isinstance(order, list)
        else ()
    )
    if any("ripweaver-catalogue" in branch for branch in branches):
        match_source = "server_assisted"
    elif any("manual" in branch for branch in branches):
        match_source = "manual_playback"
    elif any("gemini" in branch for branch in branches):
        match_source = "gemini"
    elif any(
        "thediscdb" in branch or "reviewed-release-catalogue" in branch
        for branch in branches
    ):
        match_source = "deterministic"
    else:
        match_source = "local_evidence"

    episode_id = identified.get("episode_id")
    relative = identified.get("library_relative")
    parts = (
        Path(relative.replace("/", "\\")).parts
        if isinstance(relative, str) and relative
        else ()
    )
    series_name = parts[0] if len(parts) >= 3 else None
    display_title = identified.get("display_title")
    if not isinstance(display_title, str) or not display_title.strip():
        display_title = Path(parts[-1]).stem if parts else None
    movie_year = identified.get("movie_year")
    movie_year = (
        movie_year
        if isinstance(movie_year, int) and not isinstance(movie_year, bool)
        else None
    )
    classification = identified.get("classification")
    if isinstance(episode_id, str) and re.fullmatch(
        r"(?i)S\d{1,3}E\d{1,4}", episode_id
    ):
        classification = "episode"
        if isinstance(display_title, str):
            stem = re.sub(r"\s+-\s+\d{3,4}[pi]$", "", display_title)
            marker = re.search(rf"(?i)\s+-\s+{re.escape(episode_id)}\s+-\s+(.+)$", stem)
            if marker is not None:
                display_title = marker.group(1)
    elif classification not in {
        "movie",
        "extra",
        "commentary",
        "play_all",
        "menu",
        "warning",
        "unknown",
    }:
        classification = (
            "movie"
            if any("descriptive-movie" in branch for branch in branches)
            else "extra"
        )
    evidence_source = identified.get("assignment_evidence_source")
    evidence_source = (
        evidence_source
        if isinstance(evidence_source, str)
        and _SAFE_DIAGNOSTIC_CODE.fullmatch(evidence_source)
        else None
    )
    policy_version = identified.get("identification_policy_version")
    policy_version = (
        policy_version
        if isinstance(policy_version, int)
        and not isinstance(policy_version, bool)
        and 1 <= policy_version <= 999
        else None
    )
    return (
        str(classification),
        match_source,
        display_title.strip()[:300]
        if isinstance(display_title, str) and display_title.strip()
        else None,
        series_name.strip()[:200]
        if isinstance(series_name, str) and series_name.strip()
        else None,
        movie_year,
        evidence_source,
        policy_version,
    )


@dataclass(frozen=True)
class QueuedPipelineItem:
    media_id: str
    state: str
    stage: str
    artifact: PipelineArtifact
    created_at: str
    updated_at: str
    error_type: str | None
    review_code: str | None


@dataclass(frozen=True)
class QueueEvent:
    sequence: int
    media_id: str
    event_type: str
    stage: str
    state: str
    created_at: str
    details: dict[str, object]


class PipelineReviewRequiredError(RuntimeError):
    """Signal that one item needs review without failing the queue."""

    def __init__(self, code: str):
        if re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", code) is None:
            raise PipelineQueueError("Pipeline review code is invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StageOutcome:
    artifact: PipelineArtifact
    next_review_code: str | None = None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise PipelineQueueError("Pipeline artifact could not be read") from exc
    return digest.hexdigest()


def build_artifact(
    stage: str, contract_path: Path, item_count: int = 1
) -> PipelineArtifact:
    """Create and validate a private immutable artifact reference."""

    path = contract_path.resolve()
    if stage not in {"rip", *DOWNSTREAM_STAGES} or not path.is_file():
        raise PipelineQueueError("Pipeline artifact is invalid")
    if item_count < 0:
        raise PipelineQueueError("Pipeline artifact item count is invalid")
    return PipelineArtifact(stage, path, _file_sha256(path), item_count)


def enqueue_verified_rip_results(  # noqa: C901
    store: PipelineQueueStore,
    *,
    jobs: tuple[RipJob, ...],
    results: list[RipResult],
    output_root: Path,
    contract_root: Path,
    media_contexts: Mapping[str, MediaContext] | None = None,
    media_id_overrides: Mapping[str, str] | None = None,
    identity_overrides: Mapping[str, tuple[str, int]] | None = None,
    expected_title_indexes_by_disc: Mapping[str, tuple[int, ...]] | None = None,
    repair_recovered_identity: bool = False,
) -> tuple[QueuedPipelineItem, ...]:
    """Convert exact verified rip results into private downstream contracts."""

    output_root = output_root.resolve()
    contract_root = contract_root.resolve()
    if not contract_root.is_dir():
        raise PipelineQueueError("Pipeline contract root must already exist")
    try:
        contract_root.relative_to(output_root)
    except ValueError:
        pass
    else:
        raise PipelineQueueError("Pipeline contracts must be outside media output")
    jobs_by_id = {job.job_id: job for job in jobs}
    results_by_id = {result.job_id: result for result in results}
    if (
        len(jobs_by_id) != len(jobs)
        or len(results_by_id) != len(results)
        or set(jobs_by_id) != set(results_by_id)
    ):
        raise PipelineQueueError("Verified rip result set does not match its jobs")

    expected_titles_by_disc: dict[str, set[int]] = {}
    for job in jobs:
        basename_match = _RIP_BASENAME.fullmatch(job.output_basename or "")
        if basename_match is None:
            continue
        disc_id = job.job_id.rsplit("-title-", 1)[0]
        expected_titles_by_disc.setdefault(disc_id, set()).add(
            int(basename_match.group(2))
        )
    if expected_title_indexes_by_disc is not None:
        expected_titles_by_disc = {
            disc_id: set(title_indexes)
            for disc_id, title_indexes in expected_title_indexes_by_disc.items()
        }
    # Whole-disc acquisition deliberately includes every MakeMKV title, but
    # episode coordination must wait only for titles classified as relevant to
    # downstream matching. Menus/extras retained by the batch are not episode
    # candidates and must never block or influence disc-aware identification.
    for disc_id, context in (media_contexts or {}).items():
        expected_titles_by_disc.setdefault(disc_id, set()).difference_update(
            context.downstream_skip_title_indexes
        )

    queued: list[QueuedPipelineItem] = []
    for job in jobs:
        result = results_by_id[job.job_id]
        source = resolve_final_output(output_root, job)
        if source is None:
            if job.output_basename is None:
                raise PipelineQueueError(
                    "Verified rip has no deterministic output name"
                )
            source = resolve_job_output(output_root, job) / job.output_basename
        source = source.resolve()
        try:
            source.relative_to(output_root)
        except ValueError as exc:
            raise PipelineQueueError(
                "Verified rip output escapes its media root"
            ) from exc
        if not source.is_file() or source.stat().st_size != result.output_bytes:
            raise PipelineQueueError("Verified rip output is missing or changed")
        disc_id = job.job_id.rsplit("-title-", 1)[0]
        context = (media_contexts or {}).get(disc_id)
        context_payload = None
        if context is not None:
            context_payload = {
                "series_name": str(context.series_name),
                "season": context.season,
                "tmdb_id": context.tmdb_id,
                "content_hint": context.content_hint,
                "handbrake_profile_id": context.handbrake_profile_id,
                "special_feature_catalog_id": context.special_feature_catalog_id,
                "special_feature_release_id": context.special_feature_release_id,
                "special_feature_library_title": context.special_feature_library_title,
                "special_feature_library_year": context.special_feature_library_year,
                "special_feature_assignments": list(
                    context.special_feature_assignments
                ),
                "episode_assignments": list(context.episode_assignments),
                "catalogue_help_assignments": list(context.catalogue_help_assignments),
                "existing_output_policy": context.existing_output_policy,
                "downstream_skip_title_indexes": list(
                    context.downstream_skip_title_indexes
                ),
            }
        basename_match = _RIP_BASENAME.fullmatch(job.output_basename or "")
        queue_media_id = (media_id_overrides or {}).get(job.job_id)
        if queue_media_id is None:
            queue_media_id = (
                Path(job.output_basename).stem
                if basename_match is not None and job.output_basename is not None
                else job.job_id
            )
        payload = {
            "schema_version": 1,
            "mode": "verified-rip-contract",
            "media_id": queue_media_id,
            "source_path": str(source),
            "source_size_bytes": result.output_bytes,
            "warning_count": result.warning_count,
            "media_context": context_payload,
        }
        identity_override = (identity_overrides or {}).get(job.job_id)
        if identity_override is not None:
            payload["disc_fingerprint"], payload["title_index"] = identity_override
            payload["disc_expected_title_indexes"] = sorted(
                expected_titles_by_disc.get(
                    disc_id,
                    {
                        index
                        for _fingerprint, index in (identity_overrides or {}).values()
                    },
                )
            )
        elif basename_match is not None:
            payload["disc_fingerprint"] = basename_match.group(1)
            payload["title_index"] = int(basename_match.group(2))
            payload["disc_expected_title_indexes"] = sorted(
                expected_titles_by_disc.get(disc_id, ())
            )
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        contract = contract_root / f"{queue_media_id}.verified-rip.json"
        if contract.exists():
            try:
                if contract.read_text(encoding="utf-8") != serialized:
                    contract_digest = hashlib.sha256(
                        serialized.encode("utf-8")
                    ).hexdigest()[:16]
                    contract = contract_root / (
                        f"{queue_media_id}.verified-rip-{contract_digest}.json"
                    )
            except OSError as exc:
                raise PipelineQueueError(
                    "Verified rip contract could not be read"
                ) from exc
        if contract.exists():
            try:
                if contract.read_text(encoding="utf-8") != serialized:
                    raise PipelineQueueError(
                        "Digest-bound verified rip contract has different content"
                    )
            except OSError as exc:
                raise PipelineQueueError(
                    "Verified rip contract could not be read"
                ) from exc
        else:
            try:
                contract.write_text(serialized, encoding="utf-8")
            except OSError as exc:
                raise PipelineQueueError(
                    "Verified rip contract could not be written"
                ) from exc
        artifact = build_artifact("rip", contract)
        try:
            downstream_skip = (
                context is not None
                and job.title_index in context.downstream_skip_title_indexes
            )
            if downstream_skip:
                fingerprint = payload.get("disc_fingerprint")
                title_index = payload.get("title_index")
                if not isinstance(fingerprint, str) or not isinstance(title_index, int):
                    raise PipelineQueueError(
                        "Automatic downstream skip requires exact disc identity"
                    )
                store.remember_title_skip(
                    fingerprint,
                    title_index,
                    reason="automatic_non_episode",
                )
                queued.append(
                    store.complete_verified_rip_without_downstream(
                        queue_media_id,
                        artifact,
                        reason="automatic_non_episode",
                    )
                )
            else:
                queued.append(store.enqueue_verified_rip(queue_media_id, artifact))
        except PipelineQueueError:
            if not repair_recovered_identity:
                raise
            queued.append(store.repair_recovered_rip_identity(queue_media_id, artifact))
    return tuple(queued)


def _validate_artifact(artifact: PipelineArtifact, stage: str) -> None:
    if (
        artifact.stage != stage
        or artifact.item_count < 0
        or _SHA256.fullmatch(artifact.contract_sha256) is None
        or not artifact.contract_path.is_file()
        or _file_sha256(artifact.contract_path) != artifact.contract_sha256
    ):
        raise PipelineQueueError("Pipeline artifact is missing or changed")


class PipelineQueueStore:
    """Private SQLite queue; public events never contain artifact paths."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self._schema_lock = threading.Lock()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=30, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._schema_lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pipeline_items (
                    media_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    artifact_count INTEGER NOT NULL,
                    rip_artifact_path TEXT NOT NULL,
                    rip_artifact_sha256 TEXT NOT NULL,
                    rip_artifact_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_type TEXT,
                    review_code TEXT
                );
                CREATE TABLE IF NOT EXISTS pipeline_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pipeline_control (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    paused INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO pipeline_control (singleton, paused) VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS disc_title_history (
                    disc_fingerprint TEXT NOT NULL,
                    title_index INTEGER NOT NULL,
                    outcome_name TEXT NOT NULL,
                    library_relative TEXT NOT NULL,
                    episode_id TEXT,
                    classification TEXT,
                    match_source TEXT,
                    display_title TEXT,
                    series_name TEXT,
                    movie_year INTEGER,
                    assignment_evidence_source TEXT,
                    identification_policy_version INTEGER,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (disc_fingerprint, title_index)
                );
                CREATE TABLE IF NOT EXISTS matching_performance (
                    run_id TEXT PRIMARY KEY,
                    disc_fingerprint TEXT NOT NULL,
                    series_name TEXT NOT NULL,
                    title_count INTEGER NOT NULL,
                    anchor_count INTEGER NOT NULL,
                    season_scope TEXT NOT NULL,
                    proposed_count INTEGER NOT NULL,
                    applied_count INTEGER NOT NULL,
                    unresolved_count INTEGER NOT NULL,
                    anchor_elapsed_ms INTEGER NOT NULL,
                    total_elapsed_ms INTEGER NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'completed',
                    failure_stage TEXT,
                    failure_code TEXT,
                    provider_branches TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS silent_video_reviews (
                    media_id TEXT PRIMARY KEY,
                    classification TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS disc_title_dispositions (
                    disc_fingerprint TEXT NOT NULL,
                    title_index INTEGER NOT NULL,
                    disposition TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (disc_fingerprint, title_index)
                );
                CREATE TABLE IF NOT EXISTS disc_matching_scopes (
                    disc_fingerprint TEXT PRIMARY KEY,
                    relevant_title_indexes_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS disc_recovery_scopes (
                    disc_fingerprint TEXT PRIMARY KEY,
                    required_title_indexes_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            matching_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(matching_performance)"
                ).fetchall()
            }
            migrations = {
                "outcome": "TEXT NOT NULL DEFAULT 'completed'",
                "failure_stage": "TEXT",
                "failure_code": "TEXT",
                "provider_branches": "TEXT NOT NULL DEFAULT ''",
            }
            for column, declaration in migrations.items():
                if column not in matching_columns:
                    connection.execute(
                        f"ALTER TABLE matching_performance ADD COLUMN {column} {declaration}"
                    )
            history_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(disc_title_history)"
                ).fetchall()
            }
            history_migrations = {
                "classification": "TEXT",
                "match_source": "TEXT",
                "display_title": "TEXT",
                "series_name": "TEXT",
                "movie_year": "INTEGER",
                "assignment_evidence_source": "TEXT",
                "identification_policy_version": "INTEGER",
            }
            for column, declaration in history_migrations.items():
                if column not in history_columns:
                    connection.execute(
                        f"ALTER TABLE disc_title_history ADD COLUMN {column} {declaration}"
                    )

    def record_silent_video_review(self, media_id: str, classification: str) -> None:
        """Persist one path- and OCR-text-free visual review classification."""

        checked_id = self._check_media_id(media_id)
        if classification not in _SILENT_VIDEO_CLASSIFICATIONS:
            raise PipelineQueueError("Silent-video classification is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT stage, state FROM pipeline_items WHERE media_id = ?",
                (checked_id,),
            ).fetchone()
            if item is None:
                connection.rollback()
                raise PipelineQueueError("Pipeline item was not found")
            now = self._now()
            connection.execute(
                """
                INSERT INTO silent_video_reviews (media_id, classification, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(media_id) DO UPDATE SET
                    classification = excluded.classification,
                    updated_at = excluded.updated_at
                """,
                (checked_id, classification, now),
            )
            self._append_event(
                connection,
                media_id=checked_id,
                event_type="silent_video_reviewed",
                stage=item["stage"],
                state=item["state"],
                details={"classification": classification},
            )
            connection.commit()

    def silent_video_review_flags(self) -> dict[str, str]:
        """Return durable path-free visual classifications by media ID."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT media_id, classification FROM silent_video_reviews"
            ).fetchall()
        return {row["media_id"]: row["classification"] for row in rows}

    def title_dispositions(self, disc_fingerprint: str) -> dict[int, dict[str, str]]:
        """Return explicit future-rip decisions for one exact disc identity."""

        if re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None:
            raise PipelineQueueError("Disc fingerprint is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT title_index, disposition, reason FROM disc_title_dispositions "
                "WHERE disc_fingerprint = ? ORDER BY title_index",
                (disc_fingerprint,),
            ).fetchall()
        return {
            int(row["title_index"]): {
                "disposition": row["disposition"],
                "reason": row["reason"],
            }
            for row in rows
        }

    def remember_disc_matching_scope(
        self, disc_fingerprint: str, title_indexes: tuple[int, ...]
    ) -> None:
        """Persist the path-free classifier scope used by disc-aware matching."""

        if re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None:
            raise PipelineQueueError("Disc fingerprint is invalid")
        if len(set(title_indexes)) != len(title_indexes) or any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            for index in title_indexes
        ):
            raise PipelineQueueError("Disc matching scope is invalid")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO disc_matching_scopes (
                    disc_fingerprint, relevant_title_indexes_json, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(disc_fingerprint) DO UPDATE SET
                    relevant_title_indexes_json = excluded.relevant_title_indexes_json,
                    updated_at = excluded.updated_at
                """,
                (
                    disc_fingerprint,
                    json.dumps(sorted(title_indexes), separators=(",", ":")),
                    self._now(),
                ),
            )

    def disc_matching_scope(self, disc_fingerprint: str) -> tuple[int, ...] | None:
        """Return the latest classifier-derived matching scope, if prepared."""

        if re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None:
            raise PipelineQueueError("Disc fingerprint is invalid")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT relevant_title_indexes_json FROM disc_matching_scopes "
                "WHERE disc_fingerprint = ?",
                (disc_fingerprint,),
            ).fetchone()
        if row is None:
            return None
        try:
            values = json.loads(row["relevant_title_indexes_json"])
        except json.JSONDecodeError as exc:
            raise PipelineQueueError("Disc matching scope is invalid") from exc
        if not isinstance(values, list) or any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            for index in values
        ):
            raise PipelineQueueError("Disc matching scope is invalid")
        return tuple(sorted(set(values)))

    def remember_disc_recovery_scope(
        self, disc_fingerprint: str, title_indexes: tuple[int, ...]
    ) -> None:
        """Persist content titles that must be safe before recovery completes."""

        if re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None:
            raise PipelineQueueError("Disc fingerprint is invalid")
        if len(set(title_indexes)) != len(title_indexes) or any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            for index in title_indexes
        ):
            raise PipelineQueueError("Disc recovery scope is invalid")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO disc_recovery_scopes (
                    disc_fingerprint, required_title_indexes_json, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(disc_fingerprint) DO UPDATE SET
                    required_title_indexes_json = excluded.required_title_indexes_json,
                    updated_at = excluded.updated_at
                """,
                (
                    disc_fingerprint,
                    json.dumps(sorted(title_indexes), separators=(",", ":")),
                    self._now(),
                ),
            )

    def disc_recovery_scope(self, disc_fingerprint: str) -> tuple[int, ...] | None:
        """Return the latest substantial-content recovery scope, if prepared."""

        if re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None:
            raise PipelineQueueError("Disc fingerprint is invalid")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT required_title_indexes_json FROM disc_recovery_scopes "
                "WHERE disc_fingerprint = ?",
                (disc_fingerprint,),
            ).fetchone()
        if row is None:
            return None
        try:
            values = json.loads(row["required_title_indexes_json"])
        except json.JSONDecodeError as exc:
            raise PipelineQueueError("Disc recovery scope is invalid") from exc
        if not isinstance(values, list) or any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            for index in values
        ):
            raise PipelineQueueError("Disc recovery scope is invalid")
        return tuple(sorted(set(values)))

    def list_title_dispositions(self) -> tuple[dict[str, str | int], ...]:
        """Return every path-free future-rip decision for local presentation."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT disc_fingerprint, title_index, disposition, reason "
                "FROM disc_title_dispositions "
                "ORDER BY disc_fingerprint, title_index"
            ).fetchall()
        return tuple(
            {
                "disc_fingerprint": row["disc_fingerprint"],
                "title_index": int(row["title_index"]),
                "disposition": row["disposition"],
                "reason": row["reason"],
            }
            for row in rows
        )

    def remember_title_skip(
        self,
        disc_fingerprint: str,
        title_index: int,
        *,
        reason: str,
    ) -> dict[str, str | int]:
        """Exclude one exact disc title from future plans without changing media."""

        if re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None:
            raise PipelineQueueError("Disc fingerprint is invalid")
        if (
            isinstance(title_index, bool)
            or not isinstance(title_index, int)
            or title_index < 0
        ):
            raise PipelineQueueError("Disc title index is invalid")
        if reason not in {
            "likely_warning_screen",
            "likely_disc_menu",
            "repeated_read_failure",
            "automatic_non_episode",
        }:
            raise PipelineQueueError("Future-rip skip reason is invalid")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO disc_title_dispositions (
                    disc_fingerprint, title_index, disposition, reason, updated_at
                ) VALUES (?, ?, 'skip', ?, ?)
                ON CONFLICT(disc_fingerprint, title_index) DO UPDATE SET
                    disposition = excluded.disposition,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (disc_fingerprint, title_index, reason, self._now()),
            )
        return {
            "disc_fingerprint": disc_fingerprint,
            "title_index": title_index,
            "disposition": "skip",
            "reason": reason,
        }

    def restore_title_disposition(
        self, disc_fingerprint: str, title_index: int
    ) -> bool:
        """Restore one exact title to future rip planning."""

        if re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None:
            raise PipelineQueueError("Disc fingerprint is invalid")
        if (
            isinstance(title_index, bool)
            or not isinstance(title_index, int)
            or title_index < 0
        ):
            raise PipelineQueueError("Disc title index is invalid")
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM disc_title_dispositions "
                "WHERE disc_fingerprint = ? AND title_index = ?",
                (disc_fingerprint, title_index),
            )
        return cursor.rowcount == 1

    def record_matching_performance(
        self,
        *,
        disc_fingerprint: str,
        series_name: str,
        title_count: int,
        anchor_count: int,
        season_scope: tuple[int, ...],
        proposed_count: int,
        applied_count: int,
        unresolved_count: int,
        anchor_elapsed_ms: int,
        total_elapsed_ms: int,
        outcome: str = "completed",
        failure_stage: str | None = None,
        failure_code: str | None = None,
        provider_branches: tuple[str, ...] = (),
    ) -> None:
        """Persist one path- and dialogue-free all-season performance record."""

        canonical = series_name.strip()
        counts = (
            title_count,
            anchor_count,
            proposed_count,
            applied_count,
            unresolved_count,
            anchor_elapsed_ms,
            total_elapsed_ms,
        )
        if (
            re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None
            or not canonical
            or len(canonical) > 160
            or any(not isinstance(value, int) or value < 0 for value in counts)
            or outcome not in _MATCHING_OUTCOMES
            or (outcome == "completed" and title_count < 2)
            or (outcome == "completed" and not 1 <= anchor_count <= title_count)
            or (outcome == "failed" and not 0 <= anchor_count <= title_count)
            or proposed_count > title_count
            or applied_count > title_count
            or unresolved_count > title_count
            or any(not isinstance(value, int) or value < 0 for value in season_scope)
            or (
                failure_stage is not None
                and failure_stage not in _MATCHING_FAILURE_STAGES
            )
            or (
                failure_code is not None
                and _SAFE_DIAGNOSTIC_CODE.fullmatch(failure_code) is None
            )
            or any(
                branch not in _MATCHING_PROVIDER_BRANCHES
                for branch in provider_branches
            )
            or (outcome == "completed" and (failure_stage or failure_code))
        ):
            raise PipelineQueueError("Matching performance record is invalid")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO matching_performance (
                    run_id, disc_fingerprint, series_name, title_count,
                    anchor_count, season_scope, proposed_count, applied_count,
                    unresolved_count, anchor_elapsed_ms, total_elapsed_ms,
                    outcome, failure_stage, failure_code, provider_branches, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid4().hex,
                    disc_fingerprint,
                    canonical,
                    title_count,
                    anchor_count,
                    ",".join(str(value) for value in season_scope),
                    proposed_count,
                    applied_count,
                    unresolved_count,
                    anchor_elapsed_ms,
                    total_elapsed_ms,
                    outcome,
                    failure_stage,
                    failure_code,
                    ",".join(dict.fromkeys(provider_branches)),
                    self._now(),
                ),
            )

    def matching_performance(self, *, limit: int = 50) -> tuple[dict[str, object], ...]:
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise PipelineQueueError("Matching performance limit is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM matching_performance ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            {
                "run_id": row["run_id"],
                "disc_fingerprint": row["disc_fingerprint"],
                "series_name": row["series_name"],
                "title_count": row["title_count"],
                "anchor_count": row["anchor_count"],
                "season_scope": [
                    int(value) for value in row["season_scope"].split(",") if value
                ],
                "proposed_count": row["proposed_count"],
                "applied_count": row["applied_count"],
                "unresolved_count": row["unresolved_count"],
                "anchor_elapsed_ms": row["anchor_elapsed_ms"],
                "total_elapsed_ms": row["total_elapsed_ms"],
                "outcome": row["outcome"],
                "failure_stage": row["failure_stage"],
                "failure_code": row["failure_code"],
                "provider_branches": [
                    value for value in row["provider_branches"].split(",") if value
                ],
                "created_at": row["created_at"],
            }
            for row in rows
        )

    def title_history(
        self, disc_fingerprint: str
    ) -> dict[int, dict[str, str | int | None]]:
        if re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None:
            raise PipelineQueueError("Disc fingerprint is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM disc_title_history WHERE disc_fingerprint = ?",
                (disc_fingerprint,),
            ).fetchall()
        return {
            int(row["title_index"]): {
                "outcome_name": row["outcome_name"],
                "library_relative": row["library_relative"],
                "episode_id": row["episode_id"],
            }
            for row in rows
        }

    def catalogue_title_history(
        self, disc_fingerprint: str
    ) -> dict[int, dict[str, str | int | None]]:
        """Return the private durable fields needed for a path-free contribution."""

        if re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None:
            raise PipelineQueueError("Disc fingerprint is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM disc_title_history WHERE disc_fingerprint = ?",
                (disc_fingerprint,),
            ).fetchall()
        return {
            int(row["title_index"]): {
                "outcome_name": row["outcome_name"],
                "library_relative": row["library_relative"],
                "episode_id": row["episode_id"],
                "classification": row["classification"],
                "match_source": row["match_source"],
                "display_title": row["display_title"],
                "series_name": row["series_name"],
                "movie_year": row["movie_year"],
                "assignment_evidence_source": row["assignment_evidence_source"],
                "identification_policy_version": row["identification_policy_version"],
            }
            for row in rows
        }

    def assigned_series_episodes(self, series_name: str) -> frozenset[tuple[int, int]]:
        """Return durable episode assignments for one canonical series."""

        normalized = re.sub(r"[^a-z0-9]+", "", series_name.casefold())
        found: set[tuple[int, int]] = set()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT library_relative, episode_id FROM disc_title_history "
                "WHERE episode_id IS NOT NULL"
            ).fetchall()
        for row in rows:
            relative = row["library_relative"]
            episode_id = row["episode_id"]
            if not isinstance(relative, str) or not isinstance(episode_id, str):
                continue
            parts = Path(relative).parts
            if (
                not parts
                or re.sub(r"[^a-z0-9]+", "", parts[0].casefold()) != normalized
            ):
                continue
            match = re.fullmatch(r"(?i)S(\d{1,3})E(\d{1,3})", episode_id)
            if match is not None:
                found.add((int(match.group(1)), int(match.group(2))))
        return frozenset(found)

    def learned_series_coverage(self, series_name: str) -> dict[str, object]:
        """Project durable title history into reviewable, path-free disc coverage."""

        canonical = series_name.strip()
        if not canonical or len(canonical) > 160:
            raise PipelineQueueError("Canonical series name is invalid")
        normalized = re.sub(r"[^a-z0-9]+", "", canonical.casefold())
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT disc_fingerprint, title_index, library_relative, episode_id "
                "FROM disc_title_history ORDER BY disc_fingerprint, title_index"
            ).fetchall()

        grouped: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            relative = row["library_relative"]
            if not isinstance(relative, str):
                continue
            parts = Path(relative).parts
            if (
                not parts
                or re.sub(r"[^a-z0-9]+", "", parts[0].casefold()) != normalized
            ):
                continue
            episode_id = row["episode_id"]
            grouped.setdefault(row["disc_fingerprint"], []).append({
                "title_index": int(row["title_index"]),
                "episode_id": episode_id
                if isinstance(episode_id, str)
                and re.fullmatch(r"(?i)S\d{1,3}E\d{1,3}", episode_id)
                else None,
            })

        discs: list[dict[str, object]] = []
        all_episode_ids: set[str] = set()
        for fingerprint, assignments in grouped.items():
            episode_ids = sorted(
                {
                    assignment["episode_id"]
                    for assignment in assignments
                    if isinstance(assignment["episode_id"], str)
                },
                key=lambda value: tuple(
                    int(part) for part in re.findall(r"\d+", value)
                ),
            )
            all_episode_ids.update(episode_ids)
            seasons = sorted({
                int(re.match(r"(?i)S(\d+)", value).group(1)) for value in episode_ids
            })
            discs.append({
                "disc_fingerprint": fingerprint,
                "assigned_title_count": len(assignments),
                "episode_count": len(episode_ids),
                "other_title_count": len(assignments) - len(episode_ids),
                "seasons": seasons,
                "episode_ids": episode_ids,
                "assignments": assignments,
            })
        return {
            "series_name": canonical,
            "disc_count": len(discs),
            "episode_count": len(all_episode_ids),
            "discs": discs,
        }

    def forget_disc_records(self, disc_fingerprint: str) -> tuple[int, int]:
        """Delete inactive queue metadata and learned outcomes for one exact disc."""

        if re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None:
            raise PipelineQueueError("Disc fingerprint is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT media_id, state, rip_artifact_path FROM pipeline_items"
            ).fetchall()
            media_ids: list[str] = []
            for row in rows:
                try:
                    payload = json.loads(
                        Path(row["rip_artifact_path"]).read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    continue
                if payload.get("disc_fingerprint") != disc_fingerprint:
                    continue
                if row["state"] == "running":
                    connection.rollback()
                    raise PipelineQueueError(
                        "A pipeline item for this disc is currently running"
                    )
                media_ids.append(row["media_id"])
            history_count = connection.execute(
                "SELECT COUNT(*) FROM disc_title_history WHERE disc_fingerprint = ?",
                (disc_fingerprint,),
            ).fetchone()[0]
            for media_id in media_ids:
                connection.execute(
                    "DELETE FROM pipeline_events WHERE media_id = ?", (media_id,)
                )
                connection.execute(
                    "DELETE FROM pipeline_items WHERE media_id = ?", (media_id,)
                )
            connection.execute(
                "DELETE FROM disc_title_history WHERE disc_fingerprint = ?",
                (disc_fingerprint,),
            )
            connection.execute(
                "DELETE FROM disc_title_dispositions WHERE disc_fingerprint = ?",
                (disc_fingerprint,),
            )
            connection.execute(
                "DELETE FROM disc_matching_scopes WHERE disc_fingerprint = ?",
                (disc_fingerprint,),
            )
            connection.execute(
                "DELETE FROM disc_recovery_scopes WHERE disc_fingerprint = ?",
                (disc_fingerprint,),
            )
            connection.commit()
        return len(media_ids), int(history_count)

    def rebuild_title_history(self) -> int:
        """Backfill safe outcomes from existing private pipeline contracts."""

        restored = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rip_artifact_path, artifact_path FROM pipeline_items"
            ).fetchall()
            for row in rows:
                try:
                    rip_payload = json.loads(
                        Path(row["rip_artifact_path"]).read_text(encoding="utf-8")
                    )
                    outcome = json.loads(
                        Path(row["artifact_path"]).read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    continue
                fingerprint = rip_payload.get("disc_fingerprint")
                title_index = rip_payload.get("title_index")
                if not isinstance(fingerprint, str) or not isinstance(title_index, int):
                    source_name = Path(str(rip_payload.get("source_path", ""))).name
                    match = _RIP_BASENAME.fullmatch(source_name)
                    if match is None:
                        continue
                    fingerprint = match.group(1)
                    title_index = int(match.group(2))
                relative = outcome.get("library_relative")
                if not isinstance(relative, str) or not relative:
                    continue
                episode_id = outcome.get("episode_id")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO disc_title_history (
                        disc_fingerprint, title_index, outcome_name,
                        library_relative, episode_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fingerprint,
                        title_index,
                        Path(relative).name,
                        relative,
                        episode_id if isinstance(episode_id, str) else None,
                        self._now(),
                    ),
                )
                restored += 1
        return restored

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _check_media_id(media_id: str) -> str:
        if _MEDIA_ID.fullmatch(media_id) is None:
            raise PipelineQueueError("Pipeline media ID is invalid")
        return media_id

    @staticmethod
    def _decode(row: sqlite3.Row) -> QueuedPipelineItem:
        return QueuedPipelineItem(
            media_id=row["media_id"],
            state=row["state"],
            stage=row["stage"],
            artifact=PipelineArtifact(
                stage=row["artifact_stage"],
                contract_path=Path(row["artifact_path"]),
                contract_sha256=row["artifact_sha256"],
                item_count=row["artifact_count"],
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error_type=row["error_type"],
            review_code=row["review_code"],
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        media_id: str,
        event_type: str,
        stage: str,
        state: str,
        details: dict[str, object] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO pipeline_events
                (media_id, event_type, stage, state, created_at, details_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                media_id,
                event_type,
                stage,
                state,
                PipelineQueueStore._now(),
                json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
            ),
        )

    def enqueue_verified_rip(
        self,
        media_id: str,
        artifact: PipelineArtifact,
    ) -> QueuedPipelineItem:
        """Admit one verified rip to identification exactly once."""

        checked_id = self._check_media_id(media_id)
        _validate_artifact(artifact, "rip")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM pipeline_items WHERE media_id = ?",
                (checked_id,),
            ).fetchone()
            if existing is not None:
                if (
                    Path(existing["rip_artifact_path"]) != artifact.contract_path
                    or existing["rip_artifact_sha256"] != artifact.contract_sha256
                    or existing["rip_artifact_count"] != artifact.item_count
                ):
                    connection.rollback()
                    raise PipelineQueueError(
                        "Pipeline media ID has a different artifact"
                    )
                connection.commit()
                return self.get(checked_id)
            connection.execute(
                """
                INSERT INTO pipeline_items (
                    media_id, state, stage, artifact_path, artifact_sha256,
                    artifact_count, rip_artifact_path, rip_artifact_sha256,
                    rip_artifact_count, created_at, updated_at
                ) VALUES (?, 'queued', 'identify', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checked_id,
                    str(artifact.contract_path),
                    artifact.contract_sha256,
                    artifact.item_count,
                    str(artifact.contract_path),
                    artifact.contract_sha256,
                    artifact.item_count,
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                media_id=checked_id,
                event_type="verified_rip_queued",
                stage="identify",
                state="queued",
                details={"item_count": artifact.item_count},
            )
            connection.commit()
        return self.get(checked_id)

    def complete_verified_rip_without_downstream(
        self,
        media_id: str,
        artifact: PipelineArtifact,
        *,
        reason: str,
    ) -> QueuedPipelineItem:
        """Retain a verified rip while excluding it from CPU-heavy stages."""

        checked_id = self._check_media_id(media_id)
        _validate_artifact(artifact, "rip")
        if reason != "automatic_non_episode":
            raise PipelineQueueError("Pipeline skip reason is invalid")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM pipeline_items WHERE media_id = ?", (checked_id,)
            ).fetchone()
            if existing is not None:
                if (
                    Path(existing["rip_artifact_path"]) != artifact.contract_path
                    or existing["rip_artifact_sha256"] != artifact.contract_sha256
                    or existing["rip_artifact_count"] != artifact.item_count
                ):
                    connection.rollback()
                    raise PipelineQueueError(
                        "Pipeline media ID has a different artifact"
                    )
                connection.commit()
                return self.get(checked_id)
            connection.execute(
                """
                INSERT INTO pipeline_items (
                    media_id, state, stage, artifact_path, artifact_sha256,
                    artifact_count, rip_artifact_path, rip_artifact_sha256,
                    rip_artifact_count, created_at, updated_at
                ) VALUES (?, 'completed', 'identify', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checked_id,
                    str(artifact.contract_path),
                    artifact.contract_sha256,
                    artifact.item_count,
                    str(artifact.contract_path),
                    artifact.contract_sha256,
                    artifact.item_count,
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                media_id=checked_id,
                event_type="verified_rip_downstream_skipped",
                stage="identify",
                state="completed",
                details={"reason": reason, "media_changed": False},
            )
            connection.commit()
        return self.get(checked_id)

    def enqueue_reencode(
        self,
        media_id: str,
        identify_artifact: PipelineArtifact,
        rip_artifact: PipelineArtifact,
    ) -> QueuedPipelineItem:
        """Queue one reviewed retained source directly at transcode."""

        checked_id = self._check_media_id(media_id)
        _validate_artifact(identify_artifact, "identify")
        _validate_artifact(rip_artifact, "rip")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM pipeline_items WHERE media_id = ?", (checked_id,)
            ).fetchone():
                connection.rollback()
                raise PipelineQueueError("Re-encode media ID already exists")
            connection.execute(
                """
                INSERT INTO pipeline_items (
                    media_id, state, stage, artifact_path, artifact_sha256,
                    artifact_count, rip_artifact_path, rip_artifact_sha256,
                    rip_artifact_count, created_at, updated_at
                ) VALUES (?, 'queued', 'transcode', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checked_id,
                    str(identify_artifact.contract_path),
                    identify_artifact.contract_sha256,
                    identify_artifact.item_count,
                    str(rip_artifact.contract_path),
                    rip_artifact.contract_sha256,
                    rip_artifact.item_count,
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                media_id=checked_id,
                event_type="retained_source_reencode_queued",
                stage="transcode",
                state="queued",
            )
            connection.commit()
        return self.get(checked_id)

    def repair_recovered_rip_identity(
        self, media_id: str, artifact: PipelineArtifact
    ) -> QueuedPipelineItem:
        """Requeue one held recovered MKV after adding its missing disc identity."""

        checked_id = self._check_media_id(media_id)
        _validate_artifact(artifact, "rip")
        try:
            corrected = json.loads(artifact.contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineQueueError("Corrected recovery contract is invalid") from exc
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pipeline_items WHERE media_id = ?", (checked_id,)
            ).fetchone()
            if (
                row is None
                or row["state"] != "review_required"
                or row["stage"] != "identify"
                or row["review_code"]
                not in {"missing_season_context", "unmatched_disc_analysis_required"}
            ):
                connection.rollback()
                raise PipelineQueueError("Recovered identity repair is not applicable")
            try:
                original = json.loads(
                    Path(row["rip_artifact_path"]).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                connection.rollback()
                raise PipelineQueueError(
                    "Original recovery contract is invalid"
                ) from exc
            if (
                corrected.get("media_id") != checked_id
                or corrected.get("source_path") != original.get("source_path")
                or corrected.get("source_size_bytes")
                != original.get("source_size_bytes")
                or not isinstance(corrected.get("disc_fingerprint"), str)
                or not isinstance(corrected.get("title_index"), int)
            ):
                connection.rollback()
                raise PipelineQueueError("Recovered identity repair changed media")
            connection.execute(
                """
                UPDATE pipeline_items SET state = 'queued', stage = 'identify',
                    artifact_path = ?, artifact_sha256 = ?, artifact_count = ?,
                    rip_artifact_path = ?, rip_artifact_sha256 = ?,
                    rip_artifact_count = ?, updated_at = ?, error_type = NULL,
                    review_code = NULL WHERE media_id = ?
                """,
                (
                    str(artifact.contract_path),
                    artifact.contract_sha256,
                    artifact.item_count,
                    str(artifact.contract_path),
                    artifact.contract_sha256,
                    artifact.item_count,
                    self._now(),
                    checked_id,
                ),
            )
            self._append_event(
                connection,
                media_id=checked_id,
                event_type="recovered_identity_repaired",
                stage="identify",
                state="queued",
                details={"media_unchanged": True},
            )
            connection.commit()
        return self.get(checked_id)

    def get(self, media_id: str) -> QueuedPipelineItem:
        checked_id = self._check_media_id(media_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *, CASE
                    WHEN stage = 'identify' THEN 'rip'
                    WHEN stage = 'transcode' THEN 'identify'
                    WHEN stage = 'organize' THEN 'transcode'
                    ELSE 'organize'
                END AS artifact_stage
                FROM pipeline_items WHERE media_id = ?
                """,
                (checked_id,),
            ).fetchone()
        if row is None:
            raise PipelineQueueError("Pipeline item was not found")
        return self._decode(row)

    def rip_artifact(self, media_id: str) -> PipelineArtifact:
        """Return the immutable original rip artifact for a private queue item."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT rip_artifact_path, rip_artifact_sha256, rip_artifact_count
                FROM pipeline_items WHERE media_id = ?
                """,
                (self._check_media_id(media_id),),
            ).fetchone()
        if row is None:
            raise PipelineQueueError("Pipeline item was not found")
        artifact = PipelineArtifact(
            "rip",
            Path(row["rip_artifact_path"]),
            row["rip_artifact_sha256"],
            row["rip_artifact_count"],
        )
        _validate_artifact(artifact, "rip")
        return artifact

    def revise_completed_organization(
        self, media_id: str, artifact: PipelineArtifact
    ) -> QueuedPipelineItem:
        """Attach an immutable reviewed rename contract to one completed item."""

        _validate_artifact(artifact, "organize")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, stage, review_code FROM pipeline_items WHERE media_id = ?",
                (self._check_media_id(media_id),),
            ).fetchone()
            if row is None or row["state"] != "completed" or row["stage"] != "organize":
                connection.rollback()
                raise PipelineQueueError("Only completed organization may be renamed")
            connection.execute(
                """
                UPDATE pipeline_items SET artifact_path = ?, artifact_sha256 = ?,
                    artifact_count = ?, updated_at = ? WHERE media_id = ?
                """,
                (
                    str(artifact.contract_path),
                    artifact.contract_sha256,
                    artifact.item_count,
                    self._now(),
                    media_id,
                ),
            )
            self._append_event(
                connection,
                media_id=media_id,
                event_type="provisional_library_name_revised",
                stage="organize",
                state="completed",
            )
            connection.commit()
        return self.get(media_id)

    def list_items(self) -> tuple[QueuedPipelineItem, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *, CASE
                    WHEN stage = 'identify' THEN 'rip'
                    WHEN stage = 'transcode' THEN 'identify'
                    WHEN stage = 'organize' THEN 'transcode'
                    ELSE 'organize'
                END AS artifact_stage
                FROM pipeline_items ORDER BY created_at, media_id
                """
            ).fetchall()
        return tuple(self._decode(row) for row in rows)

    def expected_title_indexes_for_disc(self, disc_fingerprint: str) -> tuple[int, ...]:
        """Return path-free whole-disc title expectations from durable rip contracts."""

        if re.fullmatch(r"[0-9a-f]{16}", disc_fingerprint) is None:
            raise PipelineQueueError("Disc fingerprint is invalid")
        prepared_scope = self.disc_matching_scope(disc_fingerprint)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT rip_artifact_path FROM pipeline_items "
                "WHERE rip_artifact_path IS NOT NULL"
            ).fetchall()
        expected: set[int] = set(prepared_scope or ())
        for row in rows:
            try:
                payload = json.loads(
                    Path(row["rip_artifact_path"]).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("disc_fingerprint") != disc_fingerprint:
                continue
            # The classifier scope is predictive. Any title already admitted
            # to this fingerprint's pipeline is concrete evidence that the
            # file participates until it receives an explicit non-episode
            # disposition. This retains held and already matched episodes that
            # a later metadata-only refresh heuristic may omit.
            observed_title_index = payload.get("title_index")
            if (
                isinstance(observed_title_index, int)
                and not isinstance(observed_title_index, bool)
                and observed_title_index >= 0
            ):
                expected.add(observed_title_index)
            values = payload.get("disc_expected_title_indexes", [])
            if not isinstance(values, list):
                continue
            if prepared_scope is None:
                expected.update(
                    value
                    for value in values
                    if isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                )
        dispositions = self.title_dispositions(disc_fingerprint)
        expected.difference_update(
            title_index
            for title_index, disposition in dispositions.items()
            if disposition.get("disposition") == "skip"
        )
        return tuple(sorted(expected))

    def list_events(self, media_id: str | None = None) -> tuple[QueueEvent, ...]:
        parameters: tuple[str, ...] = ()
        where = ""
        if media_id is not None:
            parameters = (self._check_media_id(media_id),)
            where = " WHERE media_id = ?"
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pipeline_events" + where + " ORDER BY sequence",
                parameters,
            ).fetchall()
        return tuple(
            QueueEvent(
                sequence=row["sequence"],
                media_id=row["media_id"],
                event_type=row["event_type"],
                stage=row["stage"],
                state=row["state"],
                created_at=row["created_at"],
                details=json.loads(row["details_json"]),
            )
            for row in rows
        )

    def record_sequence_diagnostic(
        self,
        media_ids: tuple[str, ...],
        *,
        catalog_episode_count: int,
        file_count: int,
        best_score: float,
        runner_up_score: float,
        global_margin: float,
        disposition: str,
        library_episode_count: int = 0,
        candidate_scope: str = "all",
    ) -> None:
        """Persist path- and dialogue-free sequence scoring for held titles."""

        checked_ids = tuple(self._check_media_id(value) for value in media_ids)
        if (
            not checked_ids
            or len(set(checked_ids)) != len(checked_ids)
            or catalog_episode_count <= 0
            or file_count != len(checked_ids)
            or disposition not in {"proposed", "review", "review-ambiguous"}
            or library_episode_count < 0
            or re.fullmatch(r"(?:all|missing|S\d{2,3})", candidate_scope) is None
            or any(
                not isinstance(value, int | float) or not 0 <= value <= 1
                for value in (best_score, runner_up_score, global_margin)
            )
        ):
            raise PipelineQueueError("Sequence diagnostic is invalid")
        details = {
            "catalog_episode_count": catalog_episode_count,
            "file_count": file_count,
            "best_score": round(float(best_score), 6),
            "runner_up_score": round(float(runner_up_score), 6),
            "global_margin": round(float(global_margin), 6),
            "disposition": disposition,
            "library_episode_count": library_episode_count,
            "candidate_scope": candidate_scope,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for media_id in checked_ids:
                row = connection.execute(
                    "SELECT stage, state FROM pipeline_items WHERE media_id = ?",
                    (media_id,),
                ).fetchone()
                if row is None or row["stage"] != "identify":
                    connection.rollback()
                    raise PipelineQueueError(
                        "Sequence diagnostic item is unavailable for identification"
                    )
                self._append_event(
                    connection,
                    media_id=media_id,
                    event_type="sequence_match_scored",
                    stage="identify",
                    state=row["state"],
                    details=details,
                )
            connection.commit()

    def record_series_resolution_diagnostic(
        self,
        media_ids: tuple[str, ...],
        *,
        proposed_series_name: str,
        confidence: float,
        proposed_tmdb_id: int | None,
        proposed_series_names: tuple[str, ...] = (),
    ) -> None:
        """Retain one bounded, path-free Gemini proposal for a held disc."""

        checked_ids = tuple(self._check_media_id(value) for value in media_ids)
        clean_name = " ".join(proposed_series_name.split()).strip(" .")
        ranked_names = tuple(
            dict.fromkeys(
                " ".join(value.split()).strip(" .")
                for value in (proposed_series_names or (clean_name,))
            )
        )
        if (
            not checked_ids
            or len(set(checked_ids)) != len(checked_ids)
            or not clean_name
            or len(clean_name) > 160
            or not 1 <= len(ranked_names) <= 5
            or ranked_names[0] != clean_name
            or any(not value or len(value) > 160 for value in ranked_names)
            or not isinstance(confidence, int | float)
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
            or (
                proposed_tmdb_id is not None
                and (
                    not isinstance(proposed_tmdb_id, int)
                    or isinstance(proposed_tmdb_id, bool)
                    or proposed_tmdb_id <= 0
                )
            )
        ):
            raise PipelineQueueError("Series-resolution diagnostic is invalid")
        details = {
            "proposed_series_name": clean_name,
            "proposed_series_names": list(ranked_names),
            "confidence": round(float(confidence), 6),
            "proposed_tmdb_id": proposed_tmdb_id,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for media_id in checked_ids:
                row = connection.execute(
                    "SELECT stage, state FROM pipeline_items WHERE media_id = ?",
                    (media_id,),
                ).fetchone()
                if row is None or row["stage"] != "identify":
                    connection.rollback()
                    raise PipelineQueueError(
                        "Series-resolution item is unavailable for identification"
                    )
                self._append_event(
                    connection,
                    media_id=media_id,
                    event_type="gemini_series_resolution_proposed",
                    stage="identify",
                    state=row["state"],
                    details=details,
                )
            connection.commit()

    def claim_next(
        self,
        *,
        allowed_stages: tuple[str, ...] | None = None,
        allowed_media_ids: tuple[str, ...] | None = None,
    ) -> QueuedPipelineItem | None:
        """Atomically claim one item; at most one item may run per stage."""

        if allowed_stages is not None and (
            not allowed_stages
            or len(set(allowed_stages)) != len(allowed_stages)
            or any(stage not in DOWNSTREAM_STAGES for stage in allowed_stages)
        ):
            raise PipelineQueueError("Allowed pipeline stages are invalid")
        if allowed_media_ids is not None and (
            not allowed_media_ids
            or len(set(allowed_media_ids)) != len(allowed_media_ids)
            or any(_MEDIA_ID.fullmatch(value) is None for value in allowed_media_ids)
        ):
            raise PipelineQueueError("Allowed pipeline media IDs are invalid")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            paused = connection.execute(
                "SELECT paused FROM pipeline_control WHERE singleton = 1"
            ).fetchone()[0]
            if paused:
                connection.commit()
                return None
            conditions = ["state = 'queued'"]
            parameters: list[str] = []
            if allowed_stages is not None:
                placeholders = ",".join("?" for _ in allowed_stages)
                conditions.append(f"stage IN ({placeholders})")
                parameters.extend(allowed_stages)
            if allowed_media_ids is not None:
                placeholders = ",".join("?" for _ in allowed_media_ids)
                conditions.append(f"media_id IN ({placeholders})")
                parameters.extend(allowed_media_ids)
            row = connection.execute(
                f"""
                SELECT queued.media_id, queued.stage FROM pipeline_items AS queued
                WHERE {" AND ".join(f"queued.{condition}" for condition in conditions)}
                  AND NOT EXISTS (
                    SELECT 1 FROM pipeline_items AS active
                    WHERE active.state = 'running' AND active.stage = queued.stage
                  )
                ORDER BY queued.updated_at, queued.created_at, queued.media_id LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            now = self._now()
            connection.execute(
                "UPDATE pipeline_items SET state = 'running', updated_at = ? WHERE media_id = ?",
                (now, row["media_id"]),
            )
            self._append_event(
                connection,
                media_id=row["media_id"],
                event_type="stage_claimed",
                stage=row["stage"],
                state="running",
            )
            connection.commit()
        return self.get(row["media_id"])

    def complete_stage(
        self,
        media_id: str,
        expected_stage: str,
        artifact: PipelineArtifact,
        *,
        next_review_code: str | None = None,
    ) -> QueuedPipelineItem:
        if expected_stage not in DOWNSTREAM_STAGES:
            raise PipelineQueueError("Pipeline stage is invalid")
        _validate_artifact(artifact, expected_stage)
        history: (
            tuple[
                str,
                int,
                str,
                str,
                str | None,
                str,
                str,
                str | None,
                str | None,
                int | None,
                str | None,
                int | None,
            ]
            | None
        ) = None
        if expected_stage == "identify":
            try:
                identified = json.loads(
                    artifact.contract_path.read_text(encoding="utf-8")
                )
                rip_item = self.get(media_id)
                rip_payload = json.loads(
                    rip_item.artifact.contract_path.read_text(encoding="utf-8")
                )
                fingerprint = rip_payload.get("disc_fingerprint")
                title_index = rip_payload.get("title_index")
                relative = identified.get("library_relative")
                if (
                    isinstance(fingerprint, str)
                    and isinstance(title_index, int)
                    and isinstance(relative, str)
                    and relative
                ):
                    history = (
                        fingerprint,
                        title_index,
                        Path(relative).name,
                        relative,
                        identified.get("episode_id")
                        if isinstance(identified.get("episode_id"), str)
                        else None,
                        *_catalogue_history_metadata(identified),
                    )
            except (OSError, json.JSONDecodeError):
                history = None
        next_stage = {
            "identify": "transcode",
            "transcode": "organize",
            "organize": "complete",
        }[expected_stage]
        if (
            next_review_code is not None
            and re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", next_review_code) is None
        ):
            raise PipelineQueueError("Pipeline review code is invalid")
        next_state = (
            "completed"
            if next_stage == "complete"
            else "review_required"
            if next_review_code is not None
            else "queued"
        )
        retained_stage = expected_stage if next_state == "completed" else next_stage
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, stage FROM pipeline_items WHERE media_id = ?",
                (self._check_media_id(media_id),),
            ).fetchone()
            if (
                row is None
                or row["state"] != "running"
                or row["stage"] != expected_stage
            ):
                connection.rollback()
                raise PipelineQueueError(
                    "Pipeline item is not running the expected stage"
                )
            connection.execute(
                """
                UPDATE pipeline_items SET state = ?, stage = ?, artifact_path = ?,
                    artifact_sha256 = ?, artifact_count = ?, updated_at = ?,
                    error_type = NULL, review_code = ? WHERE media_id = ?
                """,
                (
                    next_state,
                    retained_stage,
                    str(artifact.contract_path.resolve()),
                    artifact.contract_sha256,
                    artifact.item_count,
                    self._now(),
                    next_review_code,
                    media_id,
                ),
            )
            self._append_event(
                connection,
                media_id=media_id,
                event_type="stage_completed",
                stage=expected_stage,
                state=next_state,
                details={"item_count": artifact.item_count},
            )
            if history is not None:
                connection.execute(
                    """
                    INSERT INTO disc_title_history (
                        disc_fingerprint, title_index, outcome_name,
                        library_relative, episode_id, classification, match_source,
                        display_title, series_name, movie_year,
                        assignment_evidence_source, identification_policy_version,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(disc_fingerprint, title_index) DO UPDATE SET
                        outcome_name = excluded.outcome_name,
                        library_relative = excluded.library_relative,
                        episode_id = excluded.episode_id,
                        classification = excluded.classification,
                        match_source = excluded.match_source,
                        display_title = excluded.display_title,
                        series_name = excluded.series_name,
                        movie_year = excluded.movie_year,
                        assignment_evidence_source = excluded.assignment_evidence_source,
                        identification_policy_version = excluded.identification_policy_version,
                        updated_at = excluded.updated_at
                    """,
                    (*history, self._now()),
                )
            connection.commit()
        return self.get(media_id)

    def _stop_item(
        self, media_id: str, *, state: str, value: str
    ) -> QueuedPipelineItem:
        column = "review_code" if state == "review_required" else "error_type"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, stage FROM pipeline_items WHERE media_id = ?",
                (self._check_media_id(media_id),),
            ).fetchone()
            if row is None or row["state"] != "running":
                connection.rollback()
                raise PipelineQueueError("Only a running pipeline item can be stopped")
            connection.execute(
                f"UPDATE pipeline_items SET state = ?, {column} = ?, updated_at = ? WHERE media_id = ?",
                (state, value, self._now(), media_id),
            )
            self._append_event(
                connection,
                media_id=media_id,
                event_type=(
                    "stage_review_required"
                    if state == "review_required"
                    else "stage_failed"
                ),
                stage=row["stage"],
                state=state,
                details={column: value},
            )
            connection.commit()
        return self.get(media_id)

    def require_review(self, media_id: str, code: str) -> QueuedPipelineItem:
        if re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", code) is None:
            raise PipelineQueueError("Pipeline review code is invalid")
        return self._stop_item(media_id, state="review_required", value=code)

    def hold_for_review(self, media_id: str, code: str) -> QueuedPipelineItem:
        """Hold a newly admitted queued item before a worker can claim it."""

        if re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", code) is None:
            raise PipelineQueueError("Pipeline review code is invalid")
        checked_id = self._check_media_id(media_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT stage, state FROM pipeline_items WHERE media_id = ?",
                (checked_id,),
            ).fetchone()
            if row is None or row["state"] != "queued":
                connection.rollback()
                raise PipelineQueueError("Only a queued pipeline item can be held")
            now = self._now()
            connection.execute(
                """
                UPDATE pipeline_items SET state = 'review_required',
                    review_code = ?, updated_at = ? WHERE media_id = ?
                """,
                (code, now, checked_id),
            )
            self._append_event(
                connection,
                media_id=checked_id,
                event_type="stage_review_required",
                stage=row["stage"],
                state="review_required",
                details={"review_code": code},
            )
            connection.commit()
        return self.get(checked_id)

    def fail(self, media_id: str, error_type: str) -> QueuedPipelineItem:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type) is None:
            raise PipelineQueueError("Pipeline error type is invalid")
        return self._stop_item(media_id, state="failed", value=error_type)

    def retry(self, media_id: str) -> QueuedPipelineItem:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, stage FROM pipeline_items WHERE media_id = ?",
                (self._check_media_id(media_id),),
            ).fetchone()
            if row is None or row["state"] not in {"failed", "review_required"}:
                connection.rollback()
                raise PipelineQueueError("Pipeline item is not retryable")
            connection.execute(
                """
                UPDATE pipeline_items SET state = 'queued', updated_at = ?,
                    error_type = NULL, review_code = NULL WHERE media_id = ?
                """,
                (self._now(), media_id),
            )
            self._append_event(
                connection,
                media_id=media_id,
                event_type="stage_requeued",
                stage=row["stage"],
                state="queued",
            )
            connection.commit()
        return self.get(media_id)

    def dismiss_items(
        self, media_ids: tuple[str, ...]
    ) -> tuple[QueuedPipelineItem, ...]:
        """Hide held items from the active queue without touching their artifacts."""

        checked = tuple(self._check_media_id(media_id) for media_id in media_ids)
        if not checked or len(set(checked)) != len(checked):
            raise PipelineQueueError("Dismissed pipeline item selection is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _item in checked)
            rows = connection.execute(
                f"SELECT media_id, state, stage FROM pipeline_items WHERE media_id IN ({placeholders})",
                checked,
            ).fetchall()
            if len(rows) != len(checked) or any(
                row["state"] not in {"failed", "review_required"} for row in rows
            ):
                connection.rollback()
                raise PipelineQueueError(
                    "Only failed or review-held items can be cleared"
                )
            now = self._now()
            for row in rows:
                connection.execute(
                    """
                    UPDATE pipeline_items SET state = 'discarded', updated_at = ?,
                        error_type = NULL, review_code = NULL WHERE media_id = ?
                    """,
                    (now, row["media_id"]),
                )
                self._append_event(
                    connection,
                    media_id=row["media_id"],
                    event_type="pipeline_item_dismissed",
                    stage=row["stage"],
                    state="discarded",
                    details={"media_changed": False},
                )
            connection.commit()
        return tuple(self.get(media_id) for media_id in checked)

    def cancel_queued_items(
        self, media_ids: tuple[str, ...]
    ) -> tuple[QueuedPipelineItem, ...]:
        """Cancel queued work without touching any staged artifact or history."""

        checked = tuple(self._check_media_id(media_id) for media_id in media_ids)
        if not checked or len(set(checked)) != len(checked):
            raise PipelineQueueError("Cancelled pipeline item selection is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _item in checked)
            rows = connection.execute(
                f"SELECT media_id, state, stage FROM pipeline_items WHERE media_id IN ({placeholders})",
                checked,
            ).fetchall()
            if len(rows) != len(checked) or any(
                row["state"] != "queued" for row in rows
            ):
                connection.rollback()
                raise PipelineQueueError(
                    "Only queued, not-running items can be removed"
                )
            now = self._now()
            for row in rows:
                connection.execute(
                    """
                    UPDATE pipeline_items SET state = 'discarded', updated_at = ?,
                        error_type = NULL, review_code = NULL WHERE media_id = ?
                    """,
                    (now, row["media_id"]),
                )
                self._append_event(
                    connection,
                    media_id=row["media_id"],
                    event_type="queued_pipeline_item_cancelled",
                    stage=row["stage"],
                    state="discarded",
                    details={"media_changed": False},
                )
            connection.commit()
        return tuple(self.get(media_id) for media_id in checked)

    def delete_queued_item_media(
        self,
        media_id: str,
        delete_media: Callable[[QueuedPipelineItem], None],
        *,
        remember_future_skip: bool = False,
    ) -> QueuedPipelineItem:
        """Atomically exclude one inactive item while its exact source is deleted."""

        checked = self._check_media_id(media_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *, CASE
                    WHEN stage = 'identify' THEN 'rip'
                    WHEN stage = 'transcode' THEN 'identify'
                    WHEN stage = 'organize' THEN 'transcode'
                    ELSE 'organize'
                END AS artifact_stage
                FROM pipeline_items WHERE media_id = ?
                """,
                (checked,),
            ).fetchone()
            if row is None or row["state"] not in {
                "queued",
                "failed",
                "review_required",
                "discarded",
            }:
                connection.rollback()
                raise PipelineQueueError(
                    "Only queued, failed, review-held, or discarded media can be deleted"
                )
            if row["review_code"] == "gemini_analysis_running":
                connection.rollback()
                raise PipelineQueueError(
                    "Staged media cannot be deleted while evidence analysis is running"
                )
            future_skip: tuple[str, int, str] | None = None
            if remember_future_skip:
                visual = connection.execute(
                    "SELECT classification FROM silent_video_reviews WHERE media_id = ?",
                    (checked,),
                ).fetchone()
                if visual is None or visual["classification"] not in {
                    "likely_warning_screen",
                    "likely_disc_menu",
                }:
                    connection.rollback()
                    raise PipelineQueueError(
                        "A likely-removable OCR decision is required for a future rip skip"
                    )
                try:
                    rip_payload = json.loads(
                        Path(row["rip_artifact_path"]).read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError) as exc:
                    connection.rollback()
                    raise PipelineQueueError(
                        "Verified rip identity is unavailable for a future rip skip"
                    ) from exc
                fingerprint = rip_payload.get("disc_fingerprint")
                title_index = rip_payload.get("title_index")
                if (
                    not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{16}", fingerprint) is None
                    or not isinstance(title_index, int)
                    or isinstance(title_index, bool)
                    or title_index < 0
                ):
                    connection.rollback()
                    raise PipelineQueueError(
                        "Verified rip identity is unavailable for a future rip skip"
                    )
                future_skip = (fingerprint, title_index, visual["classification"])
            item = self._decode(row)
            delete_media(item)
            now = self._now()
            if future_skip is not None:
                connection.execute(
                    """
                    INSERT INTO disc_title_dispositions (
                        disc_fingerprint, title_index, disposition, reason, updated_at
                    ) VALUES (?, ?, 'skip', ?, ?)
                    ON CONFLICT(disc_fingerprint, title_index) DO UPDATE SET
                        disposition = excluded.disposition,
                        reason = excluded.reason,
                        updated_at = excluded.updated_at
                    """,
                    (*future_skip, now),
                )
            connection.execute(
                """
                UPDATE pipeline_items SET state = 'discarded', updated_at = ?,
                    error_type = NULL, review_code = NULL WHERE media_id = ?
                """,
                (now, checked),
            )
            details: dict[str, object] = {
                "media_changed": True,
                "deleted": "staged_source",
            }
            if future_skip is not None:
                details.update(
                    future_rip_disposition="skip",
                    reason=future_skip[2],
                )
            self._append_event(
                connection,
                media_id=checked,
                event_type="pipeline_staged_media_deleted",
                stage=row["stage"],
                state="discarded",
                details=details,
            )
            connection.commit()
        return self.get(checked)

    def resolve_review_terminal(
        self,
        media_id: str,
        *,
        state: str,
        event_type: str,
        artifact: PipelineArtifact | None = None,
    ) -> QueuedPipelineItem:
        """Finish one reviewed item after an exact external media decision."""

        if state not in {"completed", "discarded"} or event_type not in {
            "library_replacement_completed",
            "new_pipeline_media_discarded",
        }:
            raise PipelineQueueError("Pipeline review resolution is invalid")
        if artifact is not None:
            _validate_artifact(artifact, "organize")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, stage, review_code FROM pipeline_items WHERE media_id = ?",
                (self._check_media_id(media_id),),
            ).fetchone()
            if (
                row is None
                or row["state"] != "review_required"
                or row["review_code"] != "library_collision"
            ):
                connection.rollback()
                raise PipelineQueueError(
                    "Only a held library collision can use this resolution"
                )
            values: list[object] = [state, self._now()]
            assignments = (
                "state = ?, updated_at = ?, error_type = NULL, review_code = NULL"
            )
            if artifact is not None:
                assignments += (
                    ", stage = 'organize', artifact_path = ?, artifact_sha256 = ?, "
                    "artifact_count = ?"
                )
                values.extend([
                    str(artifact.contract_path.resolve()),
                    artifact.contract_sha256,
                    artifact.item_count,
                ])
            values.append(media_id)
            connection.execute(
                f"UPDATE pipeline_items SET {assignments} WHERE media_id = ?", values
            )
            self._append_event(
                connection,
                media_id=media_id,
                event_type=event_type,
                stage=row["stage"],
                state=state,
            )
            connection.commit()
        return self.get(media_id)

    def resolve_review_with_artifact(
        self, media_id: str, artifact: PipelineArtifact
    ) -> QueuedPipelineItem:
        """Replace a held stage contract with an immutable reviewed contract."""

        _validate_artifact(artifact, artifact.stage)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, stage, review_code FROM pipeline_items WHERE media_id = ?",
                (self._check_media_id(media_id),),
            ).fetchone()
            if (
                row is None
                or row["state"] != "review_required"
                or row["review_code"] != "library_collision"
                or artifact.stage
                != {
                    "identify": "rip",
                    "transcode": "identify",
                    "organize": "transcode",
                }[row["stage"]]
            ):
                connection.rollback()
                raise PipelineQueueError(
                    "Only a held library collision can use this resolution"
                )
            connection.execute(
                """
                UPDATE pipeline_items SET state = 'queued', artifact_path = ?,
                    artifact_sha256 = ?, artifact_count = ?, updated_at = ?,
                    error_type = NULL, review_code = NULL WHERE media_id = ?
                """,
                (
                    str(artifact.contract_path.resolve()),
                    artifact.contract_sha256,
                    artifact.item_count,
                    self._now(),
                    media_id,
                ),
            )
            self._append_event(
                connection,
                media_id=media_id,
                event_type="library_replacement_authorized",
                stage=row["stage"],
                state="queued",
            )
            connection.commit()
        return self.get(media_id)

    def correct_held_episode_identification(
        self,
        media_id: str,
        artifact: PipelineArtifact,
        *,
        expected_artifact_sha256: str,
    ) -> QueuedPipelineItem:
        """Correct only episode metadata while preserving a held verified encode."""

        _validate_artifact(artifact, "transcode")
        if re.fullmatch(r"[0-9a-f]{64}", expected_artifact_sha256) is None:
            raise PipelineQueueError("Expected pipeline artifact digest is invalid")
        try:
            corrected = json.loads(artifact.contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineQueueError("Corrected episode contract is invalid") from exc
        episode_id = corrected.get("episode_id")
        relative = corrected.get("library_relative")
        if (
            corrected.get("mode") != "verified-transcode-contract"
            or not isinstance(episode_id, str)
            or re.fullmatch(r"S\d{2}E\d{2,3}", episode_id) is None
            or not isinstance(relative, str)
            or not relative
        ):
            raise PipelineQueueError("Corrected episode identity is invalid")
        relative_path = Path(relative.replace("/", "\\"))
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or len(relative_path.parts) < 3
            or relative_path.suffix.casefold() != ".mkv"
        ):
            raise PipelineQueueError("Corrected episode destination is unsafe")

        checked_id = self._check_media_id(media_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pipeline_items WHERE media_id = ?", (checked_id,)
            ).fetchone()
            if (
                row is None
                or row["state"] != "review_required"
                or row["stage"] != "organize"
                or row["review_code"] != "library_collision"
                or row["artifact_sha256"] != expected_artifact_sha256
            ):
                connection.rollback()
                raise PipelineQueueError(
                    "Held episode collision changed; review it again"
                )
            try:
                current = json.loads(
                    Path(row["artifact_path"]).read_text(encoding="utf-8")
                )
                rip_payload = json.loads(
                    Path(row["rip_artifact_path"]).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                connection.rollback()
                raise PipelineQueueError("Held episode contracts are invalid") from exc
            mutable_fields = {
                "assignment_evidence_source",
                "display_title",
                "episode_id",
                "identification_order",
                "identification_policy_version",
                "library_relative",
                "user_reviewed_name",
            }
            if any(
                current.get(key) != corrected.get(key)
                for key in set(current) | set(corrected)
                if key not in mutable_fields
            ):
                connection.rollback()
                raise PipelineQueueError("Episode correction changed verified media")
            fingerprint = rip_payload.get("disc_fingerprint")
            title_index = rip_payload.get("title_index")
            if (
                not isinstance(fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{16}", fingerprint) is None
                or not isinstance(title_index, int)
                or isinstance(title_index, bool)
            ):
                connection.rollback()
                raise PipelineQueueError("Verified rip identity is unavailable")
            metadata = _catalogue_history_metadata(corrected)
            now = self._now()
            connection.execute(
                """
                UPDATE pipeline_items SET artifact_path = ?, artifact_sha256 = ?,
                    artifact_count = ?, updated_at = ?, error_type = NULL,
                    review_code = 'corrected_identification_ready'
                WHERE media_id = ?
                """,
                (
                    str(artifact.contract_path.resolve()),
                    artifact.contract_sha256,
                    artifact.item_count,
                    now,
                    checked_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO disc_title_history (
                    disc_fingerprint, title_index, outcome_name,
                    library_relative, episode_id, classification, match_source,
                    display_title, series_name, movie_year,
                    assignment_evidence_source, identification_policy_version,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(disc_fingerprint, title_index) DO UPDATE SET
                    outcome_name = excluded.outcome_name,
                    library_relative = excluded.library_relative,
                    episode_id = excluded.episode_id,
                    classification = excluded.classification,
                    match_source = excluded.match_source,
                    display_title = excluded.display_title,
                    series_name = excluded.series_name,
                    movie_year = excluded.movie_year,
                    assignment_evidence_source = excluded.assignment_evidence_source,
                    identification_policy_version = excluded.identification_policy_version,
                    updated_at = excluded.updated_at
                """,
                (
                    fingerprint,
                    title_index,
                    relative_path.name,
                    relative.replace("\\", "/"),
                    episode_id,
                    *metadata,
                    now,
                ),
            )
            self._append_event(
                connection,
                media_id=checked_id,
                event_type="episode_identification_corrected",
                stage="organize",
                state="review_required",
                details={
                    "previous_episode_id": current.get("episode_id"),
                    "episode_id": episode_id,
                    "evidence_source": corrected.get("assignment_evidence_source"),
                },
            )
            connection.commit()
        return self.get(checked_id)

    def choose_review_path(self, media_id: str, code: str) -> QueuedPipelineItem:
        """Record an ambiguity-resolution choice without queueing the item."""

        if code not in {
            "gemini_evidence_required",
            "gemini_analysis_running",
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
            "gemini_descriptive_review_required",
            "special_feature_manual_assignment_required",
            "special_feature_evidence_required",
            "all_season_analysis_running",
            "all_season_analysis_failed",
            "all_season_series_not_found",
            "all_season_evidence_failed",
            "all_season_catalog_unavailable",
            "all_season_sequence_review_required",
            "independent_episode_evidence_required",
            "whole_disc_coherence_review_required",
            "visual_content_review_required",
            "gemini_series_resolution_uncertain",
            "play_all_aggregate_detected",
        }:
            raise PipelineQueueError("Pipeline review choice is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, stage, review_code FROM pipeline_items WHERE media_id = ?",
                (self._check_media_id(media_id),),
            ).fetchone()
            if (
                row is None
                or row["state"] != "review_required"
                or row["stage"] != "identify"
                or row["review_code"]
                not in {
                    "episode_match_review",
                    "special_feature_evidence_required",
                    "gemini_evidence_required",
                    "gemini_analysis_running",
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
                    "gemini_descriptive_review_required",
                    "special_feature_manual_assignment_required",
                    "missing_season_context",
                    "unmatched_disc_analysis_required",
                    "all_season_analysis_running",
                    "all_season_analysis_failed",
                    "all_season_series_not_found",
                    "all_season_evidence_failed",
                    "all_season_catalog_unavailable",
                    "all_season_sequence_review_required",
                    "independent_episode_evidence_required",
                    "whole_disc_coherence_review_required",
                    "visual_content_review_required",
                    "gemini_series_resolution_uncertain",
                }
            ):
                connection.rollback()
                raise PipelineQueueError(
                    "Pipeline item is not awaiting special-feature ambiguity review"
                )
            connection.execute(
                "UPDATE pipeline_items SET review_code = ?, updated_at = ? WHERE media_id = ?",
                (code, self._now(), media_id),
            )
            self._append_event(
                connection,
                media_id=media_id,
                event_type="ambiguity_resolution_selected",
                stage="identify",
                state="review_required",
                details={"review_code": code},
            )
            connection.commit()
        return self.get(media_id)

    def apply_reviewed_identification_input(
        self, media_id: str, artifact: PipelineArtifact
    ) -> QueuedPipelineItem:
        """Attach a new immutable rip contract and retry reviewed identification."""

        _validate_artifact(artifact, "rip")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, stage, review_code FROM pipeline_items WHERE media_id = ?",
                (self._check_media_id(media_id),),
            ).fetchone()
            if (
                row is None
                or row["state"] != "review_required"
                or row["stage"] != "identify"
                or row["review_code"]
                not in {
                    "gemini_evidence_required",
                    "gemini_analysis_running",
                    "gemini_analysis_failed",
                    "gemini_descriptive_review_required",
                    "special_feature_manual_assignment_required",
                    "missing_season_context",
                    "episode_match_review",
                    "unmatched_disc_analysis_required",
                    "all_season_analysis_running",
                    "all_season_analysis_failed",
                    "all_season_series_not_found",
                    "all_season_evidence_failed",
                    "all_season_catalog_unavailable",
                    "all_season_sequence_review_required",
                    "independent_episode_evidence_required",
                    "whole_disc_coherence_review_required",
                    "gemini_series_resolution_uncertain",
                    "catalogue_candidate_help_available",
                }
            ):
                connection.rollback()
                raise PipelineQueueError(
                    "Pipeline item is not awaiting reviewed identification input"
                )
            connection.execute(
                """
                UPDATE pipeline_items SET state = 'queued', artifact_path = ?,
                    artifact_sha256 = ?, artifact_count = ?, updated_at = ?,
                    review_code = NULL, error_type = NULL WHERE media_id = ?
                """,
                (
                    str(artifact.contract_path.resolve()),
                    artifact.contract_sha256,
                    artifact.item_count,
                    self._now(),
                    media_id,
                ),
            )
            self._append_event(
                connection,
                media_id=media_id,
                event_type="reviewed_identification_input_applied",
                stage="identify",
                state="queued",
                details={},
            )
            connection.commit()
        return self.get(media_id)

    def restart_identification(
        self,
        media_id: str,
        *,
        expected_disc_fingerprint: str,
        expected_title_index: int,
    ) -> QueuedPipelineItem:
        """Restart one previously verified rip at identify without changing media."""

        checked_id = self._check_media_id(media_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pipeline_items WHERE media_id = ?", (checked_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise PipelineQueueError("No verified existing rip is recorded")
            rip_path = Path(row["rip_artifact_path"])
            if (
                not rip_path.is_file()
                or _file_sha256(rip_path) != row["rip_artifact_sha256"]
            ):
                connection.rollback()
                raise PipelineQueueError("Verified rip contract is missing or changed")
            try:
                rip_payload = json.loads(rip_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                connection.rollback()
                raise PipelineQueueError("Verified rip contract is invalid") from exc
            fingerprint = rip_payload.get("disc_fingerprint")
            title_index = rip_payload.get("title_index")
            if not isinstance(fingerprint, str) or not isinstance(title_index, int):
                match = _RIP_BASENAME.fullmatch(
                    Path(str(rip_payload.get("source_path", ""))).name
                )
                if match is not None:
                    fingerprint = match.group(1)
                    title_index = int(match.group(2))
            if (
                fingerprint != expected_disc_fingerprint
                or title_index != expected_title_index
            ):
                connection.rollback()
                raise PipelineQueueError(
                    "Verified rip belongs to a different disc inventory or title"
                )
            connection.execute(
                """
                UPDATE pipeline_items SET state = 'queued', stage = 'identify',
                    artifact_path = rip_artifact_path,
                    artifact_sha256 = rip_artifact_sha256,
                    artifact_count = rip_artifact_count,
                    updated_at = ?, error_type = NULL, review_code = NULL
                WHERE media_id = ?
                """,
                (self._now(), checked_id),
            )
            self._append_event(
                connection,
                media_id=checked_id,
                event_type="existing_rip_identification_restarted",
                stage="identify",
                state="queued",
                details={},
            )
            connection.commit()
        return self.get(checked_id)

    def set_paused(self, paused: bool) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE pipeline_control SET paused = ? WHERE singleton = 1",
                (int(paused),),
            )
            connection.commit()

    def is_paused(self) -> bool:
        with self._connect() as connection:
            return bool(
                connection.execute(
                    "SELECT paused FROM pipeline_control WHERE singleton = 1"
                ).fetchone()[0]
            )

    def reconcile_incomplete(self, *, clear_pause: bool = False) -> tuple[str, ...]:
        """Return interrupted work to its stage queue and optionally resume startup."""

        recovered: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if clear_pause:
                connection.execute(
                    "UPDATE pipeline_control SET paused = 0 WHERE singleton = 1"
                )
            rows = connection.execute(
                "SELECT media_id, stage FROM pipeline_items WHERE state = 'running'"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE pipeline_items SET state = 'queued', updated_at = ? WHERE media_id = ?",
                    (self._now(), row["media_id"]),
                )
                self._append_event(
                    connection,
                    media_id=row["media_id"],
                    event_type="stage_restart_requeued",
                    stage=row["stage"],
                    state="queued",
                )
                recovered.append(row["media_id"])
            interrupted_reviews = connection.execute(
                """
                SELECT media_id FROM pipeline_items
                WHERE state = 'review_required'
                  AND stage = 'identify'
                  AND review_code = 'all_season_analysis_running'
                """
            ).fetchall()
            for row in interrupted_reviews:
                connection.execute(
                    """
                    UPDATE pipeline_items
                    SET review_code = 'all_season_analysis_failed', updated_at = ?
                    WHERE media_id = ?
                    """,
                    (self._now(), row["media_id"]),
                )
                self._append_event(
                    connection,
                    media_id=row["media_id"],
                    event_type="analysis_restart_review_required",
                    stage="identify",
                    state="review_required",
                    details={"review_code": "all_season_analysis_failed"},
                )
                recovered.append(row["media_id"])
            connection.commit()
        return tuple(recovered)


StageAdapter = Callable[[QueuedPipelineItem], PipelineArtifact | StageOutcome]


def _safe_adapter_failure_code(exc: Exception) -> str:
    """Reduce adapter exceptions to actionable, path-free durable codes."""

    if type(exc).__name__ == "HandBrakeProcessError":
        return "HandBrakeProcessFailed"
    if type(exc).__name__ != "HandBrakeError":
        return type(exc).__name__
    message = str(exc).casefold()
    categories = (
        ("partial transcode exists", "HandBrakePartialExists"),
        ("amd vcn is not available", "HandBrakeEncoderUnavailable"),
        ("encoder is not available", "HandBrakeEncoderUnavailable"),
        ("requested audio language was not found", "HandBrakeAudioLanguageMissing"),
        ("no usable tracks", "HandBrakeNoUsableAudio"),
        ("no usable height", "HandBrakeNoUsableVideo"),
        ("source audio inspection failed", "HandBrakeAudioInspectionFailed"),
        ("source video inspection failed", "HandBrakeVideoInspectionFailed"),
        ("timed out", "HandBrakeTimedOut"),
        ("destination exists", "HandBrakeDestinationExists"),
        ("destination appeared", "HandBrakeDestinationExists"),
        ("produced no output", "HandBrakeNoOutput"),
        ("stream verification", "HandBrakeOutputVerificationFailed"),
        ("unexpected video codec", "HandBrakeOutputVerificationFailed"),
    )
    return next(
        (code for fragment, code in categories if fragment in message),
        "HandBrakePreflightFailed",
    )


class DownstreamDispatcher:
    """Run one claimed operation while the store enforces per-stage limits."""

    def __init__(
        self,
        store: PipelineQueueStore,
        adapters: Mapping[str, StageAdapter],
    ):
        if set(adapters) != set(DOWNSTREAM_STAGES):
            raise PipelineQueueError("Every downstream stage requires one adapter")
        self.store = store
        self.adapters = dict(adapters)

    def run_one(
        self,
        *,
        allowed_stages: tuple[str, ...] | None = None,
        allowed_media_ids: tuple[str, ...] | None = None,
    ) -> QueuedPipelineItem | None:
        item = self.store.claim_next(
            allowed_stages=allowed_stages, allowed_media_ids=allowed_media_ids
        )
        if item is None:
            return None
        try:
            result = self.adapters[item.stage](item)
            if isinstance(result, StageOutcome):
                return self.store.complete_stage(
                    item.media_id,
                    item.stage,
                    result.artifact,
                    next_review_code=result.next_review_code,
                )
            return self.store.complete_stage(item.media_id, item.stage, result)
        except PipelineReviewRequiredError as exc:
            return self.store.require_review(item.media_id, exc.code)
        except Exception as exc:
            # A media-specific adapter failure must release its stage so
            # unrelated queued work can continue. Store failures still raise
            # from this call and stop the dispatcher because queue integrity is
            # then unknown.
            return self.store.fail(item.media_id, _safe_adapter_failure_code(exc))

    def drain(self, *, max_items: int | None = None) -> tuple[QueuedPipelineItem, ...]:
        if max_items is not None and max_items <= 0:
            raise PipelineQueueError("Downstream item limit must be positive")
        completed: list[QueuedPipelineItem] = []
        while max_items is None or len(completed) < max_items:
            item = self.run_one()
            if item is None:
                break
            completed.append(item)
        return tuple(completed)
