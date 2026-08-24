"""Explicit Windows optical-tray ejection boundary."""

from __future__ import annotations

import ctypes
import re
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass


class DiscEjectError(RuntimeError):
    """Raised when an exact optical drive cannot be ejected safely."""


@dataclass(frozen=True)
class EjectFailure:
    """One path-free Windows eject failure suitable for a public response."""

    method: str
    stage: str
    error_code: int


@dataclass(frozen=True)
class EjectMethodResult:
    """Bounded result from one Windows tray-control mechanism."""

    accepted: bool
    failures: tuple[EjectFailure, ...] = ()


def _eject_with_storage_ioctl(
    device_name: str,
    *,
    kernel32: object | None = None,
    get_last_error: Callable[[], int] | None = None,
) -> EjectMethodResult:
    """Try the native storage eject control using conservative access modes."""

    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    if get_last_error is None:
        get_last_error = ctypes.get_last_error  # type: ignore[attr-defined]
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    invalid_handle = wintypes.HANDLE(-1).value
    failures: list[EjectFailure] = []
    access_modes = (
        ("read-write", 0x80000000 | 0x40000000),
        ("read-only", 0x80000000),
        ("metadata-only", 0),
    )
    for access_name, desired_access in access_modes:
        handle = create_file(
            rf"\\.\{device_name.upper()}",
            desired_access,
            0x00000001 | 0x00000002,
            None,
            3,
            0,
            None,
        )
        if handle == invalid_handle:
            failures.append(
                EjectFailure(
                    method="storage",
                    stage=f"open-{access_name}",
                    error_code=max(0, int(get_last_error())),
                )
            )
            continue
        try:
            returned = wintypes.DWORD()
            if kernel32.DeviceIoControl(
                handle,
                0x002D4808,
                None,
                0,
                None,
                0,
                ctypes.byref(returned),
                None,
            ):
                return EjectMethodResult(accepted=True)
            failures.append(
                EjectFailure(
                    method="storage",
                    stage=f"device-control-{access_name}",
                    error_code=max(0, int(get_last_error())),
                )
            )
        finally:
            kernel32.CloseHandle(handle)
    return EjectMethodResult(accepted=False, failures=tuple(failures))


def _eject_with_mci(
    device_name: str, *, winmm: object | None = None
) -> EjectMethodResult:
    """Fallback for optical drivers that reject the storage eject control."""

    if winmm is None:
        winmm = ctypes.WinDLL("winmm", use_last_error=True)  # type: ignore[attr-defined]
    send = winmm.mciSendStringW
    send.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.UINT,
        wintypes.HANDLE,
    )
    send.restype = wintypes.DWORD
    alias = f"ripweaver_{device_name[0].lower()}"
    open_error = int(
        send(f"open {device_name.upper()} type cdaudio alias {alias}", None, 0, None)
    )
    if open_error:
        return EjectMethodResult(
            accepted=False,
            failures=(EjectFailure("mci", "open", max(0, open_error)),),
        )
    try:
        door_error = int(send(f"set {alias} door open", None, 0, None))
        if door_error:
            return EjectMethodResult(
                accepted=False,
                failures=(EjectFailure("mci", "door-open", max(0, door_error)),),
            )
        return EjectMethodResult(accepted=True)
    finally:
        send(f"close {alias}", None, 0, None)


def _format_eject_failures(failures: tuple[EjectFailure, ...]) -> str:
    """Render a bounded diagnostic without device names or hardware details."""

    return "; ".join(
        f"{failure.method}/{failure.stage}=error-{failure.error_code}"
        for failure in failures[:4]
    )


def eject_optical_drive(device_name: str) -> None:
    """Eject one Windows drive-letter device after an explicit API confirmation."""

    if sys.platform != "win32":
        raise DiscEjectError("Optical tray ejection is available only on Windows")
    if re.fullmatch(r"[A-Za-z]:", device_name) is None:
        raise DiscEjectError("Optical drive device name is invalid")
    # Some optical drivers acknowledge the storage control without physically
    # opening the tray. Send the independent MCI door-open command as well so
    # one false-positive mechanism does not suppress the fallback.
    storage_result = _eject_with_storage_ioctl(device_name)
    mci_result = _eject_with_mci(device_name)
    if storage_result.accepted or mci_result.accepted:
        return
    diagnostics = _format_eject_failures((
        *storage_result.failures,
        *mci_result.failures,
    ))
    raise DiscEjectError(
        "Optical tray could not be ejected; close programs using the disc and try "
        f"again. Windows diagnostics: {diagnostics or 'unavailable'}"
    )
