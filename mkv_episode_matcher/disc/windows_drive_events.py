"""Windows volume-event listener and debounced read-only refresh coordinator."""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes

from loguru import logger

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
        invalidate_bindings: Callable[[], None] | None = None,
        periodic_refresh: Callable[[], None] | None = None,
        debounce_seconds: float = 1.5,
        poll_seconds: float | None = None,
        failure_backoff_seconds: tuple[float, ...] = (5.0, 15.0, 60.0, 300.0),
        timeout_failure_limit: int = 2,
    ):
        if not failure_backoff_seconds or any(
            delay <= 0 for delay in failure_backoff_seconds
        ):
            raise ValueError(
                "Drive refresh failure backoff must contain positive delays"
            )
        if timeout_failure_limit < 2:
            raise ValueError("Drive refresh timeout failure limit must be at least two")
        self.refresh = refresh
        self.rip_is_active = rip_is_active
        self.invalidate_bindings = invalidate_bindings
        self.periodic_refresh = periodic_refresh
        self.debounce_seconds = debounce_seconds
        self.poll_seconds = poll_seconds
        self.failure_backoff_seconds = failure_backoff_seconds
        self.timeout_failure_limit = timeout_failure_limit
        self._pending = threading.Event()
        self._media_changed = threading.Event()
        self._lightweight_media_refresh_pending = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._automatic_discovery_pause_reason: str | None = None
        self._consecutive_timeout_failures = 0

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="optical-refresh-coordinator", daemon=True
            )
            self._thread.start()

    def notify_change(self) -> None:
        self._resume_automatic_discovery()
        self._media_changed.set()
        self._lightweight_media_refresh_pending.set()
        self._pending.set()

    def request_refresh(self) -> None:
        """Queue a full refresh without claiming that Windows reported media change."""

        self._resume_automatic_discovery()
        self._pending.set()

    def resume_automatic_discovery(self) -> None:
        """Clear the breaker before one separately requested manual refresh."""

        self._resume_automatic_discovery()

    def _resume_automatic_discovery(self) -> None:
        with self._state_lock:
            self._automatic_discovery_pause_reason = None
            self._consecutive_timeout_failures = 0

    def automatic_discovery_status(self) -> dict[str, object]:
        """Return path-free circuit-breaker state for the cached drive dashboard."""

        with self._state_lock:
            return {
                "paused": self._automatic_discovery_pause_reason is not None,
                "pause_reason": self._automatic_discovery_pause_reason,
                "consecutive_timeout_count": self._consecutive_timeout_failures,
            }

    def refresh_deferred(self) -> bool:
        """Report a queued refresh held behind active optical work."""

        return self._pending.is_set() and self.rip_is_active()

    def stop(self) -> None:
        self._stop.set()
        self._pending.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _run_deferred_lightweight_refresh(self) -> None:
        """Reconcile one media event without invoking MakeMKV discovery."""

        if (
            not self._lightweight_media_refresh_pending.is_set()
            or self.periodic_refresh is None
        ):
            return
        self._lightweight_media_refresh_pending.clear()
        try:
            self.periodic_refresh()
        except Exception:
            # Full discovery remains queued for the idle boundary.
            pass

    def _handle_refresh_failure(
        self,
        exc: Exception,
        *,
        periodic: bool,
        consecutive_failures: int,
    ) -> int:
        """Record one safe failure and return the next generic failure count."""

        if periodic:
            self._pending.clear()
            logger.warning(
                "Lightweight Windows optical-drive reconciliation failed safely; "
                "failure_type={}",
                type(exc).__name__,
            )
            return 0
        timed_out = "timed out" in str(exc).casefold()
        with self._state_lock:
            if timed_out:
                self._consecutive_timeout_failures += 1
            else:
                self._consecutive_timeout_failures = 0
            timeout_count = self._consecutive_timeout_failures
            pause_for_timeouts = (
                timed_out and timeout_count >= self.timeout_failure_limit
            )
            if pause_for_timeouts:
                self._automatic_discovery_pause_reason = "repeated_timeouts"
        if pause_for_timeouts:
            self._pending.clear()
            self._media_changed.clear()
            self._lightweight_media_refresh_pending.clear()
            logger.warning(
                "Automatic MakeMKV drive discovery paused after repeated "
                "timeouts; consecutive_timeout_count={}",
                timeout_count,
            )
            return 0
        retry_delay = self.failure_backoff_seconds[
            min(consecutive_failures, len(self.failure_backoff_seconds) - 1)
        ]
        self._pending.set()
        if timed_out:
            logger.warning(
                "Automatic MakeMKV drive discovery timed out safely; "
                "consecutive_timeout_count={} retry_in_seconds={}",
                timeout_count,
                retry_delay,
            )
        else:
            logger.warning(
                "Automatic MakeMKV drive discovery failed safely; "
                "failure_type={} retry_in_seconds={}",
                type(exc).__name__,
                retry_delay,
            )
        self._stop.wait(retry_delay)
        return consecutive_failures + 1

    def _run(self) -> None:
        consecutive_failures = 0
        while not self._stop.is_set():
            notified = self._pending.wait(self.poll_seconds)
            if self._stop.is_set():
                return
            if not notified and self.poll_seconds is None:
                continue
            # A periodic turn reconciles Windows' cached volume state without
            # invoking MakeMKV. Full discovery is reserved for startup and
            # actual media-change notifications.
            periodic = not notified
            self._pending.set()
            self._stop.wait(self.debounce_seconds)
            if self._stop.is_set():
                return
            if self.rip_is_active():
                # Do one Windows-only reconciliation for each media event so
                # another idle drive can notice an inserted disc. The full
                # MakeMKV discovery and global identity invalidation remain
                # deferred until every optical worker is idle.
                self._run_deferred_lightweight_refresh()
                self._stop.wait(1)
                continue
            media_changed = self._media_changed.is_set()
            self._pending.clear()
            try:
                if periodic and self.periodic_refresh is not None:
                    self.periodic_refresh()
                    self._pending.clear()
                    consecutive_failures = 0
                    continue
                if media_changed and self.invalidate_bindings is not None:
                    self.invalidate_bindings()
                self.refresh()
                if media_changed:
                    self._media_changed.clear()
                    self._lightweight_media_refresh_pending.clear()
                consecutive_failures = 0
                with self._state_lock:
                    self._consecutive_timeout_failures = 0
            except Exception as exc:
                # The DriveWatcher retains typed error state for the UI.
                consecutive_failures = self._handle_refresh_failure(
                    exc,
                    periodic=periodic,
                    consecutive_failures=consecutive_failures,
                )
                continue
