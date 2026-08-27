"""Exclusive Windows process control for MakeMKV CLI children.

RipWeaver owns MakeMKV exclusively while its backend is running.  Startup first
acquires a named single-instance mutex, terminates surviving MakeMKV CLI
processes, and verifies that none remain.  New MakeMKV children are assigned to
one Windows Job Object whose kill-on-close policy prevents an application crash
from abandoning a drive-reading child.

Startup, child, and shutdown audits deliberately report counts and bounded
error types only.  Process identifiers, command lines, executable paths, drive
details, environments, and media labels never enter routine logs.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger

MAKEMKV_CLI_NAMES = frozenset({"makemkvcon.exe", "makemkvcon64.exe"})

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_ERROR_NO_MORE_FILES = 18
_ERROR_ALREADY_EXISTS = 183
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_STARTUP_MUTEX_NAME = r"Global\RipWeaver-MakeMKV-Control-v1"
_DISC_SOURCE_PATTERN = re.compile(r"^disc:(\d+)$", re.IGNORECASE)
_MAKEMKV_ACTIONS = frozenset({"info", "mkv", "backup"})


class MakeMKVProcessControlError(OSError):
    """Raised when exclusive MakeMKV process control cannot be proven."""


@dataclass(frozen=True)
class ProcessSnapshotEntry:
    """Minimal private process identity needed for startup cleanup."""

    process_id: int
    executable_name: str


@dataclass(frozen=True)
class StartupCleanupResult:
    """Path- and PID-free startup cleanup summary."""

    found_count: int
    cleared_count: int
    verification_passes: int


@dataclass(frozen=True)
class SupervisedShutdownResult:
    """PID- and path-free summary of explicit child settlement."""

    tracked_count: int
    settled_count: int


@dataclass(frozen=True)
class MakeMKVCommandScope:
    """Physical hardware touched by one already-validated MakeMKV command."""

    kind: Literal["none", "drive", "all-drives"]
    drive_index: int | None = None


def makemkv_command_scope(command: Sequence[object]) -> MakeMKVCommandScope:
    """Classify only ordinal physical sources; local images own no drive."""

    values = tuple(str(value) for value in command)
    action = next(
        (value.casefold() for value in values if value.casefold() in _MAKEMKV_ACTIONS),
        None,
    )
    indexes = {
        int(match.group(1))
        for value in values
        if (match := _DISC_SOURCE_PATTERN.fullmatch(value)) is not None
    }
    if len(indexes) > 1:
        raise MakeMKVProcessControlError(
            "One MakeMKV child cannot own multiple physical drives"
        )
    if not indexes:
        if action is not None and any(
            value.casefold().startswith("dev:") for value in values
        ):
            raise MakeMKVProcessControlError(
                "Physical MakeMKV commands require an ordinal drive binding"
            )
        return MakeMKVCommandScope("none")
    drive_index = next(iter(indexes))
    if not 0 <= drive_index <= 9999:
        raise MakeMKVProcessControlError("MakeMKV physical drive scope is invalid")
    if drive_index == 9999:
        return MakeMKVCommandScope("all-drives")
    return MakeMKVCommandScope("drive", drive_index)


class _MakeMKVHardwareLease:
    def __init__(
        self,
        arbiter: MakeMKVHardwareArbiter,
        token: object,
        scope: MakeMKVCommandScope,
    ) -> None:
        self._arbiter = arbiter
        self._token = token
        self._scope = scope
        self._released = False

    def close(self) -> None:
        if self._released:
            return
        self._arbiter.release(self._token, self._scope)
        self._released = True


class MakeMKVHardwareArbiter:
    """Fail closed if two MakeMKV children could touch the same drive."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._drives: dict[int, object] = {}
        self._all_drives: object | None = None

    def claim(self, scope: MakeMKVCommandScope) -> _MakeMKVHardwareLease | None:
        if scope.kind == "none":
            return None
        token = object()
        with self._lock:
            if scope.kind == "all-drives":
                if self._all_drives is not None or self._drives:
                    raise MakeMKVProcessControlError(
                        "All-drive MakeMKV discovery is blocked by active optical work"
                    )
                self._all_drives = token
            else:
                drive_index = scope.drive_index
                if drive_index is None:
                    raise MakeMKVProcessControlError(
                        "MakeMKV physical drive scope is invalid"
                    )
                if self._all_drives is not None or drive_index in self._drives:
                    raise MakeMKVProcessControlError(
                        "A MakeMKV child already owns the selected optical drive"
                    )
                self._drives[drive_index] = token
        return _MakeMKVHardwareLease(self, token, scope)

    def release(self, token: object, scope: MakeMKVCommandScope) -> None:
        with self._lock:
            if scope.kind == "all-drives":
                if self._all_drives is token:
                    self._all_drives = None
                return
            if (
                scope.drive_index is not None
                and self._drives.get(scope.drive_index) is token
            ):
                self._drives.pop(scope.drive_index, None)

    def active_drive_indexes(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(sorted(self._drives))

    def all_drive_operation_active(self) -> bool:
        with self._lock:
            return self._all_drives is not None


_MAKEMKV_HARDWARE_ARBITER = MakeMKVHardwareArbiter()


def is_makemkv_cli_name(value: str) -> bool:
    """Recognize only MakeMKV's command-line executable basenames."""

    return value.strip().casefold() in MAKEMKV_CLI_NAMES


def audit_makemkv_process_start(
    process: subprocess.Popen[str], command: Sequence[object]
) -> None:
    """Write one count-only process-start record."""

    del command
    if getattr(process, "pid", None) is None:
        return
    logger.info("MakeMKV child started; child_count=1")


def audit_makemkv_process_exit(process: subprocess.Popen[str]) -> None:
    """Write one count/error-type process-exit record at most once."""

    if getattr(process, "_ripweaver_exit_audited", False):
        return
    return_code = process.poll()
    if return_code is None:
        return
    logger.info(
        "MakeMKV child exited; child_count=1 error_type={}",
        "none" if int(return_code) == 0 else "nonzero_exit",
    )
    lease = getattr(process, "_ripweaver_hardware_lease", None)
    if lease is not None:
        lease.close()
        process._ripweaver_hardware_lease = None
    process._ripweaver_exit_audited = True


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _last_windows_error(message: str) -> MakeMKVProcessControlError:
    error_code = ctypes.get_last_error()
    return MakeMKVProcessControlError(f"{message} (Windows error {error_code})")


class _WindowsProcessApi:
    """Small ctypes boundary for process enumeration and termination."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise MakeMKVProcessControlError(
                "Windows MakeMKV process control is unavailable"
            )
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateToolhelp32Snapshot.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self._kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        ]
        self._kernel32.Process32FirstW.restype = wintypes.BOOL
        self._kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        ]
        self._kernel32.Process32NextW.restype = wintypes.BOOL
        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateProcess.restype = wintypes.BOOL
        self._kernel32.WaitForSingleObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def list_processes(self) -> tuple[ProcessSnapshotEntry, ...]:
        invalid_handle = ctypes.c_void_p(-1).value
        snapshot = self._kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snapshot or int(snapshot) == invalid_handle:
            raise _last_windows_error("Windows process enumeration could not start")
        try:
            entry = _ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not self._kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() == _ERROR_NO_MORE_FILES:
                    return ()
                raise _last_windows_error("Windows process enumeration failed")
            found: list[ProcessSnapshotEntry] = []
            while True:
                found.append(
                    ProcessSnapshotEntry(
                        process_id=int(entry.th32ProcessID),
                        executable_name=str(entry.szExeFile),
                    )
                )
                if not self._kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    if ctypes.get_last_error() != _ERROR_NO_MORE_FILES:
                        raise _last_windows_error(
                            "Windows process enumeration ended unexpectedly"
                        )
                    break
            return tuple(found)
        finally:
            self._kernel32.CloseHandle(snapshot)

    def terminate_and_wait(self, process_id: int, timeout_seconds: float) -> None:
        process = self._kernel32.OpenProcess(
            _PROCESS_TERMINATE | _SYNCHRONIZE,
            False,
            process_id,
        )
        if not process:
            raise _last_windows_error("A MakeMKV CLI process could not be opened")
        try:
            if not self._kernel32.TerminateProcess(process, 1):
                raise _last_windows_error(
                    "A MakeMKV CLI process could not be terminated"
                )
            wait_result = self._kernel32.WaitForSingleObject(
                process,
                max(1, int(timeout_seconds * 1000)),
            )
            if wait_result != _WAIT_OBJECT_0:
                raise MakeMKVProcessControlError(
                    "A MakeMKV CLI process did not terminate before the timeout"
                )
        finally:
            self._kernel32.CloseHandle(process)


def _makemkv_processes(
    entries: Sequence[ProcessSnapshotEntry],
) -> tuple[ProcessSnapshotEntry, ...]:
    current_process_id = os.getpid()
    return tuple(
        entry
        for entry in entries
        if entry.process_id != current_process_id
        and is_makemkv_cli_name(entry.executable_name)
    )


def _snapshot_makemkv_processes(
    list_processes: Callable[[], Sequence[ProcessSnapshotEntry]],
    error_message: str,
) -> tuple[ProcessSnapshotEntry, ...]:
    try:
        return _makemkv_processes(tuple(list_processes()))
    except MakeMKVProcessControlError:
        raise
    except Exception as exc:
        raise MakeMKVProcessControlError(error_message) from exc


def cleanup_existing_makemkv_processes(
    *,
    list_processes: Callable[[], Sequence[ProcessSnapshotEntry]] | None = None,
    terminate_and_wait: Callable[[int, float], None] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    terminate_timeout_seconds: float = 10.0,
    verification_delay_seconds: float = 0.25,
) -> StartupCleanupResult:
    """Terminate every existing MakeMKV CLI and prove two empty snapshots."""

    if terminate_timeout_seconds <= 0 or verification_delay_seconds < 0:
        raise MakeMKVProcessControlError("MakeMKV startup cleanup timing is invalid")
    if list_processes is None or terminate_and_wait is None:
        if sys.platform != "win32":
            return StartupCleanupResult(0, 0, 0)
        api = _WindowsProcessApi()
        list_processes = api.list_processes
        terminate_and_wait = api.terminate_and_wait

    initial = _snapshot_makemkv_processes(
        list_processes,
        "MakeMKV startup process enumeration failed",
    )

    for entry in initial:
        try:
            terminate_and_wait(entry.process_id, terminate_timeout_seconds)
        except Exception:
            # A process may exit between the snapshot and termination.  The
            # verification snapshots below decide whether startup is safe.
            continue

    for _verification_pass in range(2):
        if verification_delay_seconds:
            sleeper(verification_delay_seconds)
        remaining = _snapshot_makemkv_processes(
            list_processes,
            "MakeMKV startup verification failed",
        )
        if remaining:
            raise MakeMKVProcessControlError(
                "MakeMKV CLI processes remain after startup cleanup"
            )

    return StartupCleanupResult(
        found_count=len(initial),
        cleared_count=len(initial),
        verification_passes=2,
    )


class _WindowsMutexLease:
    """One named mutex held for the backend process lifetime."""

    def __init__(self, handle: int, kernel32: Any) -> None:
        self._handle = handle
        self._kernel32 = kernel32

    @classmethod
    def acquire(cls) -> _WindowsMutexLease:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, True, _STARTUP_MUTEX_NAME)
        if not handle:
            raise _last_windows_error("RipWeaver's MakeMKV mutex could not be created")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise MakeMKVProcessControlError(
                "Another RipWeaver instance already owns MakeMKV process control"
            )
        return cls(int(handle), kernel32)

    def close(self) -> None:
        if self._handle:
            self._kernel32.ReleaseMutex(self._handle)
            self._kernel32.CloseHandle(self._handle)
            self._handle = 0


class _NoopLease:
    def close(self) -> None:
        return None


def acquire_makemkv_startup_mutex() -> _WindowsMutexLease | _NoopLease:
    """Prevent a second backend from killing the first backend's children."""

    if sys.platform != "win32":
        return _NoopLease()
    return _WindowsMutexLease.acquire()


class ExclusiveMakeMKVStartupControl:
    """Acquire exclusivity, clean stale CLI processes, and retain the lease."""

    def __init__(
        self,
        *,
        acquire_mutex: Callable[[], Any] = acquire_makemkv_startup_mutex,
        cleanup: Callable[[], StartupCleanupResult] = (
            cleanup_existing_makemkv_processes
        ),
    ) -> None:
        self._acquire_mutex = acquire_mutex
        self._cleanup = cleanup
        self._lock = threading.Lock()
        self._lease: Any | None = None
        self._result: StartupCleanupResult | None = None

    def start(self) -> StartupCleanupResult:
        with self._lock:
            if self._lease is not None and self._result is not None:
                return self._result
            lease = self._acquire_mutex()
            try:
                result = self._cleanup()
            except Exception as exc:
                lease.close()
                if isinstance(exc, MakeMKVProcessControlError):
                    raise
                raise MakeMKVProcessControlError(
                    "MakeMKV startup cleanup failed"
                ) from exc
            self._lease = lease
            self._result = result
            return result

    def close(self) -> None:
        with self._lock:
            if self._lease is not None:
                self._lease.close()
            self._lease = None
            self._result = None


_MAKEMKV_STARTUP_CONTROL = ExclusiveMakeMKVStartupControl()


def get_makemkv_startup_control() -> ExclusiveMakeMKVStartupControl:
    """Return the shared backend and CLI exclusivity gate."""

    return _MAKEMKV_STARTUP_CONTROL


class _WindowsKillOnCloseJob:
    """Windows Job Object that terminates assigned children when closed."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise MakeMKVProcessControlError("Windows Job Objects are unavailable")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise _last_windows_error("MakeMKV Job Object could not be created")
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            self._kernel32.CloseHandle(handle)
            raise _last_windows_error("MakeMKV Job Object policy could not be set")
        self._handle = int(handle)

    def assign(self, process: subprocess.Popen[str]) -> None:
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise MakeMKVProcessControlError(
                "MakeMKV child handle is unavailable for Job Object assignment"
            )
        if not self._kernel32.AssignProcessToJobObject(
            self._handle, int(process_handle)
        ):
            raise _last_windows_error(
                "MakeMKV child could not be assigned to its Job Object"
            )

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = 0


def _settle_failed_assignment(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    except Exception:
        # The caller still fails closed; startup reconciliation will catch a
        # survivor before a later backend is allowed to discover drives.
        pass


def _settle_supervised_process(
    process: subprocess.Popen[str], *, wait_timeout_seconds: float
) -> bool:
    """Wait for one Job child, then use the fallback termination boundary."""

    try:
        if process.poll() is None:
            process.wait(timeout=wait_timeout_seconds)
    except Exception:
        _settle_failed_assignment(process)
    if process.poll() is None:
        _settle_failed_assignment(process)
    return process.poll() is not None


class MakeMKVJobSupervisor:
    """Start MakeMKV only after a kill-on-close Job Object is ready."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        job_factory: Callable[[], Any] = _WindowsKillOnCloseJob,
    ) -> None:
        self._enabled = sys.platform == "win32" if enabled is None else enabled
        self._job_factory = job_factory
        self._lock = threading.Lock()
        self._job: Any | None = None
        self._processes: list[subprocess.Popen[str]] = []
        self._failed = False

    def start(
        self,
        command: Sequence[str],
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        **kwargs: Any,
    ) -> subprocess.Popen[str]:
        if not self._enabled:
            return popen_factory(command, **kwargs)
        with self._lock:
            if self._failed:
                raise OSError("MakeMKV child containment is unavailable")
            if self._job is None:
                self._job = self._job_factory()
            process = popen_factory(command, **kwargs)
            try:
                self._job.assign(process)
            except Exception as exc:
                self._failed = True
                _settle_failed_assignment(process)
                raise OSError(
                    "MakeMKV child containment could not be established"
                ) from exc
            self._processes.append(process)
            return process

    def close(self, *, wait_timeout_seconds: float = 10.0) -> SupervisedShutdownResult:
        """Close containment and prove every tracked child has settled."""

        if wait_timeout_seconds <= 0:
            raise MakeMKVProcessControlError(
                "MakeMKV shutdown settlement timing is invalid"
            )
        with self._lock:
            tracked = tuple(self._processes)
            job_close_error: Exception | None = None
            if self._job is not None:
                try:
                    self._job.close()
                except Exception as exc:
                    job_close_error = exc
            self._job = None
            settled_count = 0
            unsettled: list[subprocess.Popen[str]] = []
            for process in tracked:
                if _settle_supervised_process(
                    process,
                    wait_timeout_seconds=wait_timeout_seconds,
                ):
                    settled_count += 1
                    audit_makemkv_process_exit(process)
                else:
                    unsettled.append(process)
            self._processes = unsettled
            if job_close_error is not None or unsettled:
                self._failed = True
                raise MakeMKVProcessControlError(
                    "MakeMKV shutdown could not prove every supervised child exited"
                ) from job_close_error
            self._failed = False
            return SupervisedShutdownResult(
                tracked_count=len(tracked),
                settled_count=settled_count,
            )


_MAKEMKV_JOB_SUPERVISOR = MakeMKVJobSupervisor()
_MAKEMKV_LIFECYCLE_LOCK = threading.Lock()
_MAKEMKV_LAUNCHES_CLOSED = False


def start_makemkv_process_control() -> StartupCleanupResult:
    """Start or restart the exclusive backend process-control lifetime."""

    global _MAKEMKV_LAUNCHES_CLOSED
    with _MAKEMKV_LIFECYCLE_LOCK:
        result = _MAKEMKV_STARTUP_CONTROL.start()
        _MAKEMKV_LAUNCHES_CLOSED = False
        return result


def shutdown_makemkv_process_control() -> SupervisedShutdownResult:
    """Settle children before releasing machine-wide MakeMKV exclusivity."""

    global _MAKEMKV_LAUNCHES_CLOSED
    with _MAKEMKV_LIFECYCLE_LOCK:
        _MAKEMKV_LAUNCHES_CLOSED = True
        result = _MAKEMKV_JOB_SUPERVISOR.close()
        # If child settlement raises, retain the mutex until final process
        # teardown. A new backend must never overlap an unproven old child.
        _MAKEMKV_STARTUP_CONTROL.close()
        return result


def start_makemkv_process(
    command: Sequence[str], **kwargs: Any
) -> subprocess.Popen[str]:
    """Atomically claim its hardware and start one contained MakeMKV child."""

    with _MAKEMKV_LIFECYCLE_LOCK:
        if _MAKEMKV_LAUNCHES_CLOSED:
            raise OSError("MakeMKV process control is shutting down")
        _MAKEMKV_STARTUP_CONTROL.start()
        scope = makemkv_command_scope(command)
        lease = _MAKEMKV_HARDWARE_ARBITER.claim(scope)
        try:
            process = _MAKEMKV_JOB_SUPERVISOR.start(command, **kwargs)
        except Exception:
            if lease is not None:
                lease.close()
            raise
        process._ripweaver_hardware_lease = lease
    audit_makemkv_process_start(process, command)
    return process


def run_makemkv_command(
    command: Sequence[str], *, timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    """Capture one supervised MakeMKV command with bounded timeout handling."""

    process = start_makemkv_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            exc.cmd,
            exc.timeout,
            output=stdout if stdout is not None else exc.output,
            stderr=stderr if stderr is not None else exc.stderr,
        ) from exc
    finally:
        audit_makemkv_process_exit(process)
    return subprocess.CompletedProcess(
        args=tuple(command),
        returncode=int(process.returncode),
        stdout=stdout,
        stderr=stderr,
    )
