"""SQLite-backed, path-redacted orchestration state and append-only events."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mkv_episode_matcher.disc.rip_preview import RipPreview
from mkv_episode_matcher.disc.ripper import RipError

JOB_STATES = {
    "awaiting_review",
    "authorized",
    "queued",
    "running",
    "pause_requested",
    "paused",
    "failed",
    "completed",
}
_IDEMPOTENCY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class OrchestrationJob:
    job_id: str
    plan_sha256: str
    state: str
    created_at: str
    updated_at: str
    authorization_sha256: str | None
    executor_attached: bool
    preview: dict[str, Any]


@dataclass(frozen=True)
class OrchestrationEvent:
    sequence: int
    created_at: str
    event_type: str
    from_state: str | None
    to_state: str
    details: dict[str, Any]


class OrchestrationStore:
    """Own transactional job transitions without storing private filesystem paths."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self._schema_lock = threading.Lock()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._schema_lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    plan_sha256 TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    authorization_sha256 TEXT,
                    executor_attached INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    PRIMARY KEY (job_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS commands (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    action TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    resulting_state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, action, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS creation_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id)
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        if _IDEMPOTENCY_PATTERN.fullmatch(value) is None:
            raise RipError("Idempotency key must use 8-128 safe ASCII characters")
        return value

    @staticmethod
    def _validate_sha256(value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise RipError("Plan digest must be a lowercase SHA-256")
        return value

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> OrchestrationJob:
        preview = json.loads(row["preview_json"])
        return OrchestrationJob(
            job_id=row["job_id"],
            plan_sha256=row["plan_sha256"],
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            authorization_sha256=row["authorization_sha256"],
            executor_attached=bool(row["executor_attached"]),
            preview=preview,
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event_type: str,
        from_state: str | None,
        to_state: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO events (
                job_id, sequence, created_at, event_type, from_state,
                to_state, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                sequence,
                OrchestrationStore._now(),
                event_type,
                from_state,
                to_state,
                json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
            ),
        )

    def create_job(
        self,
        preview: RipPreview,
        *,
        idempotency_key: str,
    ) -> OrchestrationJob:
        """Persist one immutable redacted preview or return its exact retry."""

        key = self._validate_idempotency_key(idempotency_key)
        if preview.execution_authorized is not False:
            raise RipError("Only an unauthorized preview can create a job")
        payload = preview.to_dict()
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        if any(
            forbidden in serialized.casefold()
            for forbidden in ('"report_paths"', '"output_root"', '"command"')
        ):
            raise RipError("Preview contains private execution fields")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT jobs.* FROM creation_keys
                JOIN jobs USING (job_id)
                WHERE creation_keys.idempotency_key = ?
                """,
                (key,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return self._decode_job(existing)

            job_id = f"rip-{uuid.uuid4().hex}"
            timestamp = self._now()
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, plan_sha256, preview_json, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    self._validate_sha256(preview.plan_sha256),
                    serialized,
                    "awaiting_review",
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO creation_keys (idempotency_key, job_id) VALUES (?, ?)",
                (key, job_id),
            )
            self._append_event(
                connection,
                job_id=job_id,
                event_type="job_created",
                from_state=None,
                to_state="awaiting_review",
                details={
                    "drive_count": len(preview.drives),
                    "title_count": len(preview.jobs),
                    "requires_review": preview.requires_review,
                },
            )
            connection.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> OrchestrationJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise RipError("Orchestration job was not found")
        return self._decode_job(row)

    def list_events(self, job_id: str) -> tuple[OrchestrationEvent, ...]:
        self.get_job(job_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE job_id = ? ORDER BY sequence",
                (job_id,),
            ).fetchall()
        return tuple(
            OrchestrationEvent(
                sequence=row["sequence"],
                created_at=row["created_at"],
                event_type=row["event_type"],
                from_state=row["from_state"],
                to_state=row["to_state"],
                details=json.loads(row["details_json"]),
            )
            for row in rows
        )

    def command_result(
        self,
        job_id: str,
        *,
        action: str,
        idempotency_key: str,
    ) -> str | None:
        """Return a retained command result without changing job state."""

        key = self._validate_idempotency_key(idempotency_key)
        if re.fullmatch(r"[a-z][a-z-]{0,31}", action) is None:
            raise RipError("Orchestration command action is invalid")
        self.get_job(job_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT resulting_state FROM commands
                WHERE job_id = ? AND action = ? AND idempotency_key = ?
                """,
                (job_id, action, key),
            ).fetchone()
        return str(row["resulting_state"]) if row is not None else None

    def _transition(  # noqa: C901
        self,
        job_id: str,
        *,
        action: str,
        idempotency_key: str,
        allowed_from: set[str],
        to_state: str,
        event_type: str,
        details: dict[str, Any] | None = None,
        authorization_sha256: str | None = None,
        executor_attached: bool | None = None,
    ) -> OrchestrationJob:
        key = self._validate_idempotency_key(idempotency_key)
        if to_state not in JOB_STATES:
            raise RipError("Requested orchestration state is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT resulting_state FROM commands
                WHERE job_id = ? AND action = ? AND idempotency_key = ?
                """,
                (job_id, action, key),
            ).fetchone()
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RipError("Orchestration job was not found")
            if prior is not None:
                connection.commit()
                return self._decode_job(row)
            current = row["state"]
            if current not in allowed_from:
                connection.rollback()
                raise RipError(
                    f"Cannot {action} an orchestration job in state {current}"
                )
            timestamp = self._now()
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, updated_at = ?,
                    authorization_sha256 = COALESCE(?, authorization_sha256),
                    executor_attached = COALESCE(?, executor_attached)
                WHERE job_id = ?
                """,
                (
                    to_state,
                    timestamp,
                    authorization_sha256,
                    (int(executor_attached) if executor_attached is not None else None),
                    job_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO commands (
                    job_id, action, idempotency_key, resulting_state, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, action, key, to_state, timestamp),
            )
            self._append_event(
                connection,
                job_id=job_id,
                event_type=event_type,
                from_state=current,
                to_state=to_state,
                details=details,
            )
            connection.commit()
        return self.get_job(job_id)

    def authorize(
        self,
        job_id: str,
        *,
        expected_plan_sha256: str,
        idempotency_key: str,
    ) -> OrchestrationJob:
        job = self.get_job(job_id)
        digest = self._validate_sha256(expected_plan_sha256)
        if digest != job.plan_sha256:
            raise RipError("Reviewed plan digest does not match this job")
        if job.preview.get("requires_review") is not False:
            raise RipError("Job still contains unresolved review items")
        authorization = hashlib_sha256({
            "job_id": job_id,
            "plan_sha256": digest,
            "operation": "rip",
        })
        return self._transition(
            job_id,
            action="authorize",
            idempotency_key=idempotency_key,
            allowed_from={"awaiting_review"},
            to_state="authorized",
            event_type="job_authorized",
            details={"operation": "rip"},
            authorization_sha256=authorization,
        )

    def queue(
        self,
        job_id: str,
        *,
        idempotency_key: str,
    ) -> OrchestrationJob:
        return self._transition(
            job_id,
            action="start",
            idempotency_key=idempotency_key,
            allowed_from={"authorized"},
            to_state="queued",
            event_type="job_queued",
            details={"executor_attached": False},
        )

    def pause(
        self,
        job_id: str,
        *,
        idempotency_key: str,
    ) -> OrchestrationJob:
        job = self.get_job(job_id)
        if job.state == "running":
            target = "pause_requested"
            event_type = "job_pause_requested"
        else:
            target = "paused"
            event_type = "job_paused"
        return self._transition(
            job_id,
            action="pause",
            idempotency_key=idempotency_key,
            allowed_from={"queued", "running"},
            to_state=target,
            event_type=event_type,
        )

    def resume(
        self,
        job_id: str,
        *,
        idempotency_key: str,
    ) -> OrchestrationJob:
        return self._transition(
            job_id,
            action="resume",
            idempotency_key=idempotency_key,
            allowed_from={"paused"},
            to_state="queued",
            event_type="job_resumed",
            details={"executor_attached": False},
        )

    def claim_for_dispatch(
        self,
        job_id: str,
        *,
        idempotency_key: str,
    ) -> OrchestrationJob:
        """Atomically claim one queued job for exactly one dispatcher."""

        return self._transition(
            job_id,
            action="dispatch",
            idempotency_key=idempotency_key,
            allowed_from={"queued"},
            to_state="running",
            event_type="job_dispatch_claimed",
            details={"executor_attached": True},
            executor_attached=True,
        )

    def complete(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        completed_count: int,
    ) -> OrchestrationJob:
        if completed_count < 0:
            raise RipError("Completed job count cannot be negative")
        return self._transition(
            job_id,
            action="complete",
            idempotency_key=idempotency_key,
            allowed_from={"running"},
            to_state="completed",
            event_type="job_completed",
            details={"completed_count": completed_count},
            executor_attached=False,
        )

    def fail(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        error_type: str,
    ) -> OrchestrationJob:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type) is None:
            raise RipError("Dispatcher error type is invalid")
        return self._transition(
            job_id,
            action="fail",
            idempotency_key=idempotency_key,
            allowed_from={"queued", "running", "pause_requested"},
            to_state="failed",
            event_type="job_failed",
            details={"error_type": error_type},
            executor_attached=False,
        )

    def acknowledge_pause(
        self,
        job_id: str,
        *,
        idempotency_key: str,
    ) -> OrchestrationJob:
        return self._transition(
            job_id,
            action="acknowledge-pause",
            idempotency_key=idempotency_key,
            allowed_from={"pause_requested"},
            to_state="paused",
            event_type="job_pause_acknowledged",
            executor_attached=False,
        )

    def reconcile_incomplete(self) -> tuple[OrchestrationJob, ...]:
        """On restart, pause claimed work until retained outputs are reviewed."""

        reconciled_ids: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT job_id, state FROM jobs
                WHERE state IN ('running', 'pause_requested')
                ORDER BY created_at
                """
            ).fetchall()
            for row in rows:
                timestamp = self._now()
                connection.execute(
                    """
                    UPDATE jobs SET state = 'paused', updated_at = ?,
                        executor_attached = 0
                    WHERE job_id = ?
                    """,
                    (timestamp, row["job_id"]),
                )
                self._append_event(
                    connection,
                    job_id=row["job_id"],
                    event_type="job_restart_reconciled",
                    from_state=row["state"],
                    to_state="paused",
                    details={"requires_output_review": True},
                )
                reconciled_ids.append(row["job_id"])
            connection.commit()
        return tuple(self.get_job(job_id) for job_id in reconciled_ids)


def hashlib_sha256(value: dict[str, str]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
