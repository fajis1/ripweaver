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
            }
        payload = {
            "schema_version": 1,
            "mode": "verified-rip-contract",
            "media_id": job.job_id,
            "source_path": str(source),
            "source_size_bytes": result.output_bytes,
            "warning_count": result.warning_count,
            "media_context": context_payload,
        }
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        contract = contract_root / f"{job.job_id}.verified-rip.json"
        if contract.exists():
            try:
                if contract.read_text(encoding="utf-8") != serialized:
                    raise PipelineQueueError(
                        "Verified rip contract exists with different content"
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
            store.enqueue_verified_rip(job.job_id, build_artifact("rip", contract))
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
                """
            )

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

    def claim_next(self) -> QueuedPipelineItem | None:
        """Atomically claim one item; at most one downstream stage may run globally."""

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
            row = connection.execute(
                """
                SELECT media_id, stage FROM pipeline_items
                WHERE state = 'queued'
                ORDER BY updated_at, created_at, media_id LIMIT 1
                """
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
    ) -> QueuedPipelineItem:
        if expected_stage not in DOWNSTREAM_STAGES:
            raise PipelineQueueError("Pipeline stage is invalid")
        _validate_artifact(artifact, expected_stage)
        next_stage = {
            "identify": "transcode",
            "transcode": "organize",
            "organize": "complete",
        }[expected_stage]
        next_state = "completed" if next_stage == "complete" else "queued"
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
                    error_type = NULL, review_code = NULL WHERE media_id = ?
                """,
                (
                    next_state,
                    retained_stage,
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
                event_type="stage_completed",
                stage=expected_stage,
                state=next_state,
                details={"item_count": artifact.item_count},
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
            connection.commit()
        return tuple(recovered)


StageAdapter = Callable[[QueuedPipelineItem], PipelineArtifact]


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

    def run_one(self) -> QueuedPipelineItem | None:
        item = self.store.claim_next()
        if item is None:
            return None
        try:
            artifact = self.adapters[item.stage](item)
            return self.store.complete_stage(item.media_id, item.stage, artifact)
        except PipelineReviewRequiredError as exc:
            return self.store.require_review(item.media_id, exc.code)
        except Exception as exc:
            self.store.fail(item.media_id, type(exc).__name__)
            if isinstance(exc, PipelineQueueError):
                raise
            raise PipelineQueueError(
                f"Downstream stage failed safely: {item.stage}"
            ) from exc

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
