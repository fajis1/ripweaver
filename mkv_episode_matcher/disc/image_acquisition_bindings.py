"""Private filesystem and device bindings for acquisition execution."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mkv_episode_matcher.disc.ripper import RipError


@dataclass(frozen=True)
class PrivateAcquisitionBinding:
    job_id: str
    plan_sha256: str
    executable: Path
    image_root: Path
    drive_letter: str | None


class PrivateAcquisitionBindingStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS acquisition_bindings (
                job_id TEXT PRIMARY KEY, plan_sha256 TEXT NOT NULL,
                executable TEXT NOT NULL, image_root TEXT NOT NULL,
                drive_letter TEXT)"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def bind(
        self,
        *,
        job_id: str,
        plan_sha256: str,
        executable: Path,
        image_root: Path,
        drive_letter: str | None,
    ) -> PrivateAcquisitionBinding:
        exe = executable.resolve()
        root = image_root.resolve()
        letter = drive_letter.upper().rstrip(":") if drive_letter else None
        if not exe.is_file() or not root.is_dir():
            raise RipError("Private acquisition paths must already exist")
        if letter is not None and (len(letter) != 1 or not letter.isalpha()):
            raise RipError("Private acquisition drive letter is invalid")
        values = (job_id, plan_sha256, str(exe), str(root), letter)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM acquisition_bindings WHERE job_id=?", (job_id,)
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise RipError("Private acquisition binding is immutable")
                return self.get(job_id)
            connection.execute(
                "INSERT INTO acquisition_bindings VALUES (?,?,?,?,?)", values
            )
            connection.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> PrivateAcquisitionBinding:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM acquisition_bindings WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise RipError("Private acquisition binding was not found")
        return PrivateAcquisitionBinding(
            job_id=row["job_id"],
            plan_sha256=row["plan_sha256"],
            executable=Path(row["executable"]),
            image_root=Path(row["image_root"]),
            drive_letter=row["drive_letter"],
        )
