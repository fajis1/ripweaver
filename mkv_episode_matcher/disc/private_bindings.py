"""Private local execution bindings, isolated from public job state and APIs."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from mkv_episode_matcher.disc.rip_manifest import MediaContext, media_context_from_dict
from mkv_episode_matcher.disc.ripper import RipError

_JOB_ID_PATTERN = re.compile(r"rip-[0-9a-f]{32}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class PrivateExecutionBinding:
    job_id: str
    plan_sha256: str
    report_paths: tuple[Path, ...]
    output_root: Path
    media_contexts: dict[str, MediaContext]
    created_at: str


class PrivateBindingStore:
    """Persist only local private paths needed by a future dispatcher."""

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
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._schema_lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS private_bindings (
                    job_id TEXT PRIMARY KEY,
                    plan_sha256 TEXT NOT NULL,
                    report_paths_json TEXT NOT NULL,
                    output_root TEXT NOT NULL,
                    media_contexts_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _validate_job_id(value: str) -> str:
        if _JOB_ID_PATTERN.fullmatch(value) is None:
            raise RipError("Private binding job ID is invalid")
        return value

    @staticmethod
    def _validate_sha256(value: str) -> str:
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise RipError("Private binding plan digest is invalid")
        return value

    @staticmethod
    def _normalize_reports(report_paths: list[Path]) -> tuple[Path, ...]:
        if not 1 <= len(report_paths) <= 16:
            raise RipError("Private binding requires 1-16 explicit reports")
        resolved = tuple(path.resolve() for path in report_paths)
        if len(set(resolved)) != len(resolved):
            raise RipError("Private binding reports must be unique")
        if not all(path.is_file() for path in resolved):
            raise RipError("Private binding report is not an existing file")
        return resolved

    @staticmethod
    def _normalize_output_root(output_root: Path) -> Path:
        resolved = output_root.resolve()
        if not resolved.is_dir():
            raise RipError("Private binding output root must exist")
        return resolved

    @staticmethod
    def _serialize_contexts(contexts: dict[str, MediaContext]) -> str:
        return json.dumps(
            {disc_id: asdict(context) for disc_id, context in sorted(contexts.items())},
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _decode(row: sqlite3.Row) -> PrivateExecutionBinding:
        raw_contexts = json.loads(row["media_contexts_json"])
        return PrivateExecutionBinding(
            job_id=row["job_id"],
            plan_sha256=row["plan_sha256"],
            report_paths=tuple(
                Path(value) for value in json.loads(row["report_paths_json"])
            ),
            output_root=Path(row["output_root"]),
            media_contexts={
                disc_id: media_context_from_dict(value)
                for disc_id, value in raw_contexts.items()
            },
            created_at=row["created_at"],
        )

    def bind(
        self,
        *,
        job_id: str,
        plan_sha256: str,
        report_paths: list[Path],
        output_root: Path,
        media_contexts: dict[str, MediaContext],
    ) -> PrivateExecutionBinding:
        """Create one immutable binding or accept an exact idempotent retry."""

        checked_job_id = self._validate_job_id(job_id)
        checked_digest = self._validate_sha256(plan_sha256)
        reports = self._normalize_reports(report_paths)
        root = self._normalize_output_root(output_root)
        contexts_json = self._serialize_contexts(media_contexts)
        reports_json = json.dumps(
            [str(path) for path in reports],
            separators=(",", ":"),
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM private_bindings WHERE job_id = ?",
                (checked_job_id,),
            ).fetchone()
            if existing is not None:
                binding = self._decode(existing)
                if (
                    binding.plan_sha256 != checked_digest
                    or binding.report_paths != reports
                    or binding.output_root != root
                    or self._serialize_contexts(binding.media_contexts) != contexts_json
                ):
                    connection.rollback()
                    raise RipError("Private execution binding is immutable and differs")
                connection.commit()
                return binding

            connection.execute(
                """
                INSERT INTO private_bindings (
                    job_id, plan_sha256, report_paths_json, output_root,
                    media_contexts_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    checked_job_id,
                    checked_digest,
                    reports_json,
                    str(root),
                    contexts_json,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
        return self.get(checked_job_id)

    def delete_jobs(self, job_ids: tuple[str, ...]) -> int:
        """Delete exact private bindings selected by a disc metadata reset."""

        checked = tuple(self._validate_job_id(job_id) for job_id in job_ids)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = 0
            for job_id in checked:
                cursor = connection.execute(
                    "DELETE FROM private_bindings WHERE job_id = ?", (job_id,)
                )
                deleted += cursor.rowcount
            connection.commit()
        return deleted

    def get(self, job_id: str) -> PrivateExecutionBinding:
        checked_job_id = self._validate_job_id(job_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM private_bindings WHERE job_id = ?",
                (checked_job_id,),
            ).fetchone()
        if row is None:
            raise RipError("Private execution binding was not found")
        return self._decode(row)
