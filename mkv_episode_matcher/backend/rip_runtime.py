"""Process-local control for explicitly attached rip executors."""

from __future__ import annotations

import threading
from pathlib import Path

from mkv_episode_matcher.disc.ripper import RipError


class RipExecutionRegistry:
    """Atomically claim active jobs and physical drives for rip execution."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, tuple[Path, frozenset[int]]] = {}

    def attach(
        self, job_id: str, run_directory: Path, drive_indexes: frozenset[int]
    ) -> None:
        if not drive_indexes or any(
            not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 99
            for index in drive_indexes
        ):
            raise RipError("Physical executor drive claim is invalid")
        with self._lock:
            if job_id in self._active:
                raise RipError("A physical executor is already attached to this job")
            claimed = frozenset(
                index
                for _path, indexes in self._active.values()
                for index in indexes
            )
            overlap = sorted(claimed & drive_indexes)
            if overlap:
                raise RipError(
                    "Another physical executor is already attached to optical drive "
                    f"{overlap[0] + 1}"
                )
            self._active[job_id] = (run_directory.resolve(), drive_indexes)

    def detach(self, job_id: str) -> None:
        with self._lock:
            self._active.pop(job_id, None)

    def has_active_executor(self) -> bool:
        """Return whether any MakeMKV rip executor is currently attached."""

        with self._lock:
            return bool(self._active)

    def is_job_active(self, job_id: str) -> bool:
        """Return whether the exact orchestration job owns a live executor."""

        with self._lock:
            return job_id in self._active

    def active_drive_indexes(self) -> tuple[int, ...]:
        """Return the path-free physical-drive claims held by live executors."""

        with self._lock:
            return tuple(
                sorted(
                    {
                        index
                        for _path, indexes in self._active.values()
                        for index in indexes
                    }
                )
            )

    def request_marker(self, job_id: str, marker_name: str) -> None:
        if marker_name not in {"PAUSE", "STOP"}:
            raise RipError("Rip control marker is invalid")
        with self._lock:
            active = self._active.get(job_id)
        if active is None:
            raise RipError("No physical executor is attached to this job")
        run_directory, _drive_indexes = active
        if not run_directory.is_dir():
            raise RipError("The active rip run directory is not ready")
        marker = run_directory / marker_name
        try:
            marker.touch(exist_ok=True)
        except OSError as exc:
            raise RipError("Rip control marker could not be created") from exc
