"""Explicit Windows optical-tray ejection boundary."""

from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes


class DiscEjectError(RuntimeError):
    """Raised when an exact optical drive cannot be ejected safely."""


def _eject_with_storage_ioctl(device_name: str) -> bool:
    """Try the native storage eject control using conservative access modes."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
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
    for desired_access in (0x80000000 | 0x40000000, 0x80000000, 0):
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
                return True
        finally:
            kernel32.CloseHandle(handle)
    return False


def _eject_with_mci(device_name: str) -> bool:
    """Fallback for optical drivers that reject the storage eject control."""

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
    if send(f"open {device_name.upper()} type cdaudio alias {alias}", None, 0, None):
        return False
    try:
        return send(f"set {alias} door open", None, 0, None) == 0
    finally:
        send(f"close {alias}", None, 0, None)


def eject_optical_drive(device_name: str) -> None:
    """Eject one Windows drive-letter device after an explicit API confirmation."""

    if sys.platform != "win32":
        raise DiscEjectError("Optical tray ejection is available only on Windows")
    if re.fullmatch(r"[A-Za-z]:", device_name) is None:
        raise DiscEjectError("Optical drive device name is invalid")
    # Some optical drivers acknowledge the storage control without physically
    # opening the tray. Send the independent MCI door-open command as well so
    # one false-positive mechanism does not suppress the fallback.
    storage_accepted = _eject_with_storage_ioctl(device_name)
    mci_accepted = _eject_with_mci(device_name)
    if storage_accepted or mci_accepted:
        return
    raise DiscEjectError(
        "Optical tray could not be ejected; close programs using the disc and try again"
    )
