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
                "existing_output_policy": context.existing_output_policy,
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
        if basename_match is not None:
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
        queued.append(
            store.enqueue_verified_rip(queue_media_id, build_artifact("rip", contract))
        )
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
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (disc_fingerprint, title_index)
                );
                """
            )

    def title_history(self, disc_fingerprint: str) -> dict[int, dict[str, str | None]]:
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
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT media_id FROM pipeline_items ORDER BY created_at, media_id"
                ).fetchall()
            ]
        return tuple(self.get(media_id) for media_id in ids)

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
            or candidate_scope not in {"all", "missing"}
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

    def claim_next(
        self,
        *,
        allowed_stages: tuple[str, ...] | None = None,
        allowed_media_ids: tuple[str, ...] | None = None,
    ) -> QueuedPipelineItem | None:
        """Atomically claim one item; at most one downstream stage may run globally."""

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
            running = connection.execute(
                "SELECT 1 FROM pipeline_items WHERE state = 'running' LIMIT 1"
            ).fetchone()
            if running is not None:
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
                SELECT media_id, stage FROM pipeline_items
                WHERE {" AND ".join(conditions)}
                ORDER BY updated_at, created_at, media_id LIMIT 1
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
        history: tuple[str, int, str, str, str | None] | None = None
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
                        library_relative, episode_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(disc_fingerprint, title_index) DO UPDATE SET
                        outcome_name = excluded.outcome_name,
                        library_relative = excluded.library_relative,
                        episode_id = excluded.episode_id,
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
        self, media_id: str, delete_media: Callable[[QueuedPipelineItem], None]
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
            item = self._decode(row)
            delete_media(item)
            now = self._now()
            connection.execute(
                """
                UPDATE pipeline_items SET state = 'discarded', updated_at = ?,
                    error_type = NULL, review_code = NULL WHERE media_id = ?
                """,
                (now, checked),
            )
            self._append_event(
                connection,
                media_id=checked,
                event_type="pipeline_staged_media_deleted",
                stage=row["stage"],
                state="discarded",
                details={"media_changed": True, "deleted": "staged_source"},
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
                    "gemini_series_resolution_uncertain",
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

    def reconcile_incomplete(self) -> tuple[str, ...]:
        """Return interrupted downstream work to its stage queue after restart."""

        recovered: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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


class DownstreamDispatcher:
    """Run at most one non-rip operation at a time across every stage."""

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
            # A media-specific adapter failure must release the global worker so
            # unrelated queued discs can continue. Store failures still raise
            # from this call and stop the dispatcher because queue integrity is
            # then unknown.
            return self.store.fail(item.media_id, type(exc).__name__)

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
