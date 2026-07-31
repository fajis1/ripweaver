"""Process-local control for explicitly attached rip executors."""

from __future__ import annotations

import threading
from pathlib import Path

from mkv_episode_matcher.disc.ripper import RipError


class RipExecutionRegistry:
    """Track active run directories without exposing them through public state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, Path] = {}

    def attach(self, job_id: str, run_directory: Path) -> None:
        with self._lock:
            if job_id in self._active:
                raise RipError("A physical executor is already attached to this job")
            self._active[job_id] = run_directory.resolve()

    def detach(self, job_id: str) -> None:
        with self._lock:
            self._active.pop(job_id, None)

    def request_marker(self, job_id: str, marker_name: str) -> None:
        if marker_name not in {"PAUSE", "STOP"}:
            raise RipError("Rip control marker is invalid")
        with self._lock:
            run_directory = self._active.get(job_id)
        if run_directory is None:
            raise RipError("No physical executor is attached to this job")
        if not run_directory.is_dir():
            raise RipError("The active rip run directory is not ready")
        marker = run_directory / marker_name
        try:
            marker.touch(exist_ok=True)
        except OSError as exc:
            raise RipError("Rip control marker could not be created") from exc
