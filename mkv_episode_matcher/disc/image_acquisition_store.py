"""Durable path-redacted control plane for whole-disc acquisition jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from mkv_episode_matcher.core.datetime_compat import UTC
from mkv_episode_matcher.disc.image_acquisition import DiscImagePlan, plan_sha256
from mkv_episode_matcher.disc.ripper import RipError

_TRANSITIONS = {
    "planned": {"authorized"},
    "authorized": {"queued"},
    "queued": {"running"},
    "running": {"verified", "failed"},
}


@dataclass(frozen=True)
class AcquisitionJob:
    job_id: str
    plan_sha256: str
    plan: dict[str, object]
    state: str
    created_at: str
    updated_at: str


class ImageAcquisitionStore:
    """Persist public acquisition state without device or filesystem paths."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self._lock = threading.Lock()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS acquisition_jobs (
                    job_id TEXT PRIMARY KEY, plan_sha256 TEXT NOT NULL,
                    plan_json TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS acquisition_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    event_type TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(job_id, idempotency_key)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _decode(row: sqlite3.Row) -> AcquisitionJob:
        return AcquisitionJob(
            job_id=row["job_id"],
            plan_sha256=row["plan_sha256"],
            plan=json.loads(row["plan_json"]),
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create(self, plan: DiscImagePlan, *, idempotency_key: str) -> AcquisitionJob:
        if not idempotency_key.strip():
            raise RipError("Acquisition idempotency key is required")
        payload = asdict(plan)
        if plan.execution_authorized is not False:
            raise RipError("Acquisition plan authority flag is invalid")
        digest = plan_sha256(plan)
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """SELECT j.* FROM acquisition_jobs j JOIN acquisition_events e
                   ON e.job_id=j.job_id WHERE e.idempotency_key=?
                   AND e.event_type='created'""",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                job = self._decode(existing)
                if job.plan_sha256 != digest:
                    raise RipError("Acquisition idempotency key has different content")
                return job
            job_id = f"acq-{uuid4().hex}"
            connection.execute(
                "INSERT INTO acquisition_jobs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    digest,
                    json.dumps(payload, sort_keys=True),
                    "planned",
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO acquisition_events (job_id,idempotency_key,event_type,created_at) VALUES (?,?,?,?)",
                (job_id, idempotency_key, "created", now),
            )
            connection.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> AcquisitionJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM acquisition_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise RipError("Acquisition job was not found")
        return self._decode(row)

    def transition(
        self,
        job_id: str,
        target: str,
        *,
        idempotency_key: str,
        expected_plan_sha256: str,
    ) -> AcquisitionJob:
        if not idempotency_key.strip():
            raise RipError("Acquisition idempotency key is required")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM acquisition_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise RipError("Acquisition job was not found")
            job = self._decode(row)
            if job.plan_sha256 != expected_plan_sha256:
                raise RipError("Acquisition plan digest does not match")
            prior = connection.execute(
                "SELECT event_type FROM acquisition_events WHERE job_id=? AND idempotency_key=?",
                (job_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if prior["event_type"] != target:
                    raise RipError("Acquisition idempotency key has different content")
                connection.commit()
                return self.get(job_id)
            if target not in _TRANSITIONS.get(job.state, set()):
                raise RipError("Acquisition state transition is not allowed")
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "UPDATE acquisition_jobs SET state=?,updated_at=? WHERE job_id=?",
                (target, now, job_id),
            )
            connection.execute(
                "INSERT INTO acquisition_events (job_id,idempotency_key,event_type,created_at) VALUES (?,?,?,?)",
                (job_id, idempotency_key, target, now),
            )
            connection.commit()
        return self.get(job_id)

    def events(self, job_id: str) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_type FROM acquisition_events WHERE job_id=? ORDER BY event_id",
                (job_id,),
            ).fetchall()
        return tuple(row["event_type"] for row in rows)
