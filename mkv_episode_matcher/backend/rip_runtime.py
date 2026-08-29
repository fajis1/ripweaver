"""Process-local control for explicitly attached rip executors."""

from __future__ import annotations

import threading
from pathlib import Path

from mkv_episode_matcher.disc.ripper import RipError


class RipExecutionRegistry:
    """Atomically arbitrate MakeMKV discovery, preparation, and rip execution."""

    _PREPARATION_OPERATIONS = frozenset({
        "disc preparation scan",
        "execution inventory",
        "manual eject check",
        "automatic eject check",
        "disc identity reset",
    })

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, tuple[Path, frozenset[int]]] = {}
        self._preparing: dict[int, tuple[object, str]] = {}
        self._all_drive_discovery: object | None = None

    @staticmethod
    def _validate_drive_index(drive_index: int) -> None:
        if (
            not isinstance(drive_index, int)
            or isinstance(drive_index, bool)
            or not 0 <= drive_index <= 99
        ):
            raise RipError("Physical optical-drive claim is invalid")

    def _claimed_executor_drives(self) -> frozenset[int]:
        return frozenset(
            index for _path, indexes in self._active.values() for index in indexes
        )

    def _active_rip_job_for_drive(self, drive_index: int) -> str | None:
        return next(
            (
                job_id
                for job_id, (_path, indexes) in self._active.items()
                if drive_index in indexes
            ),
            None,
        )

    def _drive_operation(self, drive_index: int) -> str | None:
        preparation = self._preparing.get(drive_index)
        if preparation is not None:
            return preparation[1]
        if self._active_rip_job_for_drive(drive_index) is not None:
            return "MakeMKV rip"
        return None

    @staticmethod
    def _operation_subject(operation: str) -> str:
        return operation[:1].upper() + operation[1:]

    def claim_drive_preparation(
        self,
        drive_index: int,
        *,
        operation: str = "disc preparation scan",
    ) -> OpticalWorkLease:
        """Claim one drive for inventory/planning without blocking other drives."""

        self._validate_drive_index(drive_index)
        if operation not in self._PREPARATION_OPERATIONS:
            raise RipError("Physical optical-drive operation is invalid")
        token = object()
        with self._lock:
            if self._all_drive_discovery is not None:
                raise AllDriveDiscoveryBlocksDriveError(
                    "Read-only all-drive discovery is active; disc preparation was not started"
                )
            existing_operation = self._drive_operation(drive_index)
            if existing_operation is not None:
                raise RipError(
                    f"{self._operation_subject(existing_operation)} is already active on optical "
                    f"drive {drive_index + 1}; {operation} was not started"
                )
            self._preparing[drive_index] = (token, operation)
        return OpticalWorkLease(self, "preparation", token, drive_index)

    def claim_all_drive_discovery(self) -> OpticalWorkLease:
        """Claim an atomic barrier because ``info disc:9999`` touches every drive."""

        token = object()
        with self._lock:
            if self._all_drive_discovery is not None:
                raise AllDriveDiscoveryInProgressError(
                    "Read-only all-drive discovery is already active"
                )
            if self._active or self._preparing:
                raise AllDriveDiscoveryDeferredError(
                    "Read-only all-drive discovery was deferred until active optical work finishes"
                )
            self._all_drive_discovery = token
        return OpticalWorkLease(self, "all-drive-discovery", token, None)

    def _release_lease(self, kind: str, token: object, drive_index: int | None) -> None:
        with self._lock:
            if kind == "all-drive-discovery":
                if self._all_drive_discovery is token:
                    self._all_drive_discovery = None
                return
            if kind == "preparation" and drive_index is not None:
                preparation = self._preparing.get(drive_index)
                if preparation is not None and preparation[0] is token:
                    self._preparing.pop(drive_index, None)

    def attach(
        self, job_id: str, run_directory: Path, drive_indexes: frozenset[int]
    ) -> None:
        if not drive_indexes or any(
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index <= 99
            for index in drive_indexes
        ):
            raise RipError("Physical executor drive claim is invalid")
        with self._lock:
            if self._all_drive_discovery is not None:
                raise RipError(
                    "Read-only all-drive discovery is active; physical rip was not started"
                )
            if job_id in self._active:
                raise RipError("A physical executor is already attached to this job")
            claimed = self._claimed_executor_drives() | frozenset(self._preparing)
            overlap = sorted(claimed & drive_indexes)
            if overlap:
                drive_index = overlap[0]
                operation = self._drive_operation(drive_index) or "physical drive work"
                raise RipError(
                    f"{self._operation_subject(operation)} is already active on optical drive "
                    f"{drive_index + 1}; a second MakeMKV rip was not started"
                )
            self._active[job_id] = (run_directory.resolve(), drive_indexes)

    def promote_drive_preparations(
        self,
        job_id: str,
        run_directory: Path,
        leases: tuple[OpticalWorkLease, ...],
    ) -> None:
        """Atomically convert exact per-drive scan leases into one rip claim."""

        drive_indexes = frozenset(
            lease._drive_index for lease in leases if lease._drive_index is not None
        )
        if not leases or len(drive_indexes) != len(leases):
            raise RipError("Execution inventory drive claims are invalid")
        for drive_index in drive_indexes:
            self._validate_drive_index(drive_index)

        with self._lock:
            if self._all_drive_discovery is not None:
                raise RipError(
                    "Read-only all-drive discovery is active; physical rip was not started"
                )
            if job_id in self._active:
                raise RipError("A physical executor is already attached to this job")
            if self._claimed_executor_drives() & drive_indexes:
                raise RipError("An execution inventory drive is already being ripped")

            for lease in leases:
                drive_index = lease._drive_index
                preparation = self._preparing.get(drive_index)
                if (
                    lease._registry is not self
                    or lease._kind != "preparation"
                    or lease._released
                    or preparation is None
                    or preparation[0] is not lease._token
                    or preparation[1] != "execution inventory"
                ):
                    raise RipError("Execution inventory drive claim changed before rip")

            self._active[job_id] = (run_directory.resolve(), drive_indexes)
            for lease in leases:
                self._preparing.pop(lease._drive_index, None)
                lease._released = True

    def detach(self, job_id: str) -> None:
        with self._lock:
            self._active.pop(job_id, None)

    def has_active_executor(self) -> bool:
        """Return whether any MakeMKV rip executor is currently attached."""

        with self._lock:
            return bool(self._active)

    def has_active_optical_work(self) -> bool:
        """Return whether discovery, preparation, or rip execution owns hardware."""

        with self._lock:
            return bool(
                self._active or self._preparing or self._all_drive_discovery is not None
            )

    def all_drive_discovery_active(self) -> bool:
        with self._lock:
            return self._all_drive_discovery is not None

    def is_job_active(self, job_id: str) -> bool:
        """Return whether the exact orchestration job owns a live executor."""

        with self._lock:
            return job_id in self._active

    def active_drive_indexes(self) -> tuple[int, ...]:
        """Return the path-free physical-drive claims held by live executors."""

        with self._lock:
            return tuple(
                sorted({
                    index
                    for _path, indexes in self._active.values()
                    for index in indexes
                })
            )

    def busy_drive_indexes(self) -> tuple[int, ...]:
        """Return all path-free per-drive preparation and executor claims."""

        with self._lock:
            return tuple(sorted(self._claimed_executor_drives() | set(self._preparing)))

    def physical_drive_operations(self) -> dict[int, str]:
        """Return safe user-facing operation labels for claimed physical drives."""

        with self._lock:
            drive_indexes = self._claimed_executor_drives() | set(self._preparing)
            return {
                drive_index: operation
                for drive_index in sorted(drive_indexes)
                if (operation := self._drive_operation(drive_index)) is not None
            }

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


class AllDriveDiscoveryDeferredError(RipError):
    """Raised when full discovery would overlap active per-drive MakeMKV work."""


class AllDriveDiscoveryInProgressError(RipError):
    """Raised when a second full discovery is requested concurrently."""


class AllDriveDiscoveryBlocksDriveError(RipError):
    """Raised when full discovery temporarily blocks exact-drive work."""


class OpticalWorkLease:
    """Idempotent runtime lease for one safe MakeMKV hardware scope."""

    def __init__(
        self,
        registry: RipExecutionRegistry,
        kind: str,
        token: object,
        drive_index: int | None,
    ) -> None:
        self._registry = registry
        self._kind = kind
        self._token = token
        self._drive_index = drive_index
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._registry._release_lease(self._kind, self._token, self._drive_index)
        self._released = True

    def __enter__(self) -> OpticalWorkLease:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()
