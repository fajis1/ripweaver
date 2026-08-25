"""One-shot processing-queue grace period for normal backend startup."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

STARTUP_QUEUE_RESUME_DELAY_SECONDS = 60.0

_lock = threading.Lock()
_timer: threading.Timer | None = None
_deadline: float | None = None


def schedule_startup_queue_resume(
    callback: Callable[[], None],
    *,
    delay_seconds: float = STARTUP_QUEUE_RESUME_DELAY_SECONDS,
) -> None:
    """Run one queue activation after a cancellable startup grace period."""

    if delay_seconds <= 0:
        raise ValueError("Startup queue resume delay must be positive")

    global _deadline, _timer
    with _lock:
        if _timer is not None:
            _timer.cancel()

        def run() -> None:
            global _deadline, _timer
            # Hold the lock through the callback so a simultaneous explicit
            # Pause always runs afterward and therefore wins the race.
            with _lock:
                if _timer is not timer:
                    return
                _timer = None
                _deadline = None
                callback()

        timer = threading.Timer(delay_seconds, run)
        timer.daemon = True
        _timer = timer
        _deadline = time.monotonic() + delay_seconds
        timer.start()


def cancel_startup_queue_resume() -> bool:
    """Cancel the one-shot activation after a fresh explicit queue decision."""

    global _deadline, _timer
    with _lock:
        timer = _timer
        _timer = None
        _deadline = None
        if timer is None:
            return False
        timer.cancel()
        return True


def startup_queue_resume_pending() -> bool:
    """Return whether the one-shot startup activation is still armed."""

    with _lock:
        return _timer is not None


def startup_queue_resume_seconds() -> int | None:
    """Return a rounded-up countdown for the public queue dashboard."""

    with _lock:
        if _timer is None or _deadline is None:
            return None
        remaining = max(0.0, _deadline - time.monotonic())
        return max(1, int(remaining + 0.999))
