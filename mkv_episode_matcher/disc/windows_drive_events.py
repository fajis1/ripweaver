"""Windows volume-event listener and debounced read-only refresh coordinator."""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes

WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_DEVICECHANGE = 0x0219
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE = 0x8004
DBT_DEVTYP_VOLUME = 0x00000002


class DriveEventError(RuntimeError):
    """Raised when the native Windows event listener cannot initialize."""


class _DevBroadcastHeader(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("device_type", wintypes.DWORD),
        ("reserved", wintypes.DWORD),
    ]


class _WndClass(ctypes.Structure):
    pass


if sys.platform == "win32":
    _WndProc = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    _WndClass._fields_ = [
        ("style", wintypes.UINT),
        ("wnd_proc", _WndProc),
        ("class_extra", ctypes.c_int),
        ("window_extra", ctypes.c_int),
        ("instance", wintypes.HINSTANCE),
        ("icon", wintypes.HICON),
        ("cursor", wintypes.HANDLE),
        ("background", wintypes.HBRUSH),
        ("menu_name", wintypes.LPCWSTR),
        ("class_name", wintypes.LPCWSTR),
    ]


class WindowsVolumeEventListener:
    """Receive volume arrival/removal events on one hidden top-level window."""

    def __init__(self, on_change: Callable[[], None]):
        self.on_change = on_change
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._window: int | None = None
        self._wnd_proc = None

    def start(self) -> None:
        if sys.platform != "win32":
            raise DriveEventError("Windows volume events require Windows")
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._message_loop, name="optical-volume-events", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=5) or self._error is not None:
            raise DriveEventError("Windows volume event listener failed to start")

    def stop(self) -> None:
        if self._window and sys.platform == "win32":
            post_message = ctypes.windll.user32.PostMessageW
            post_message.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            post_message(self._window, WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _message_loop(self) -> None:  # noqa: C901
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HMENU,
                wintypes.HINSTANCE,
                wintypes.LPVOID,
            ]
            user32.RegisterClassW.argtypes = [ctypes.POINTER(_WndClass)]
            user32.RegisterClassW.restype = wintypes.ATOM
            user32.DefWindowProcW.restype = ctypes.c_ssize_t
            user32.DefWindowProcW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.PostQuitMessage.argtypes = [ctypes.c_int]
            class_name = f"MkvMatcherVolumeEvents-{id(self):x}"

            @_WndProc
            def window_proc(window, message, wparam, lparam):
                if message == WM_DEVICECHANGE and wparam in {
                    DBT_DEVICEARRIVAL,
                    DBT_DEVICEREMOVECOMPLETE,
                }:
                    if lparam:
                        header = ctypes.cast(
                            lparam, ctypes.POINTER(_DevBroadcastHeader)
                        ).contents
                        if header.device_type == DBT_DEVTYP_VOLUME:
                            self.on_change()
                    return 0
                if message == WM_CLOSE:
                    user32.DestroyWindow(window)
                    return 0
                if message == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(window, message, wparam, lparam)

            self._wnd_proc = window_proc
            window_class = _WndClass()
            window_class.instance = kernel32.GetModuleHandleW(None)
            window_class.class_name = class_name
            window_class.wnd_proc = window_proc
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise DriveEventError("Windows event class registration failed")
            window = user32.CreateWindowExW(
                0,
                class_name,
                class_name,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                window_class.instance,
                None,
            )
            if not window:
                raise DriveEventError("Windows event window creation failed")
            self._window = window
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            self._window = None


class DriveRefreshCoordinator:
    """Debounce media events and defer discovery until no rip is active."""

    def __init__(
        self,
        refresh: Callable[[], None],
        rip_is_active: Callable[[], bool],
        *,
        debounce_seconds: float = 1.5,
    ):
        self.refresh = refresh
        self.rip_is_active = rip_is_active
        self.debounce_seconds = debounce_seconds
        self._pending = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="optical-refresh-coordinator", daemon=True
            )
            self._thread.start()

    def notify_change(self) -> None:
        self._pending.set()

    def stop(self) -> None:
        self._stop.set()
        self._pending.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._pending.wait()
            if self._stop.is_set():
                return
            self._stop.wait(self.debounce_seconds)
            if self._stop.is_set():
                return
            if self.rip_is_active():
                self._stop.wait(1)
                continue
            self._pending.clear()
            try:
                self.refresh()
            except Exception:
                # The DriveWatcher retains typed error state for the UI.
                continue
