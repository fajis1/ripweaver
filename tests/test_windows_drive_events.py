import threading
import time

from mkv_episode_matcher.disc.windows_drive_events import DriveRefreshCoordinator


def test_coordinator_debounces_events_into_one_refresh():
    refreshed = threading.Event()
    calls = []
    coordinator = DriveRefreshCoordinator(
        lambda: (calls.append("refresh"), refreshed.set()),
        lambda: False,
        debounce_seconds=0.01,
    )
    coordinator.start()
    try:
        coordinator.notify_change()
        coordinator.notify_change()
        coordinator.notify_change()
        assert refreshed.wait(1)
        assert calls == ["refresh"]
    finally:
        coordinator.stop()


def test_coordinator_defers_refresh_while_rip_is_active():
    active = True
    refreshed = threading.Event()
    coordinator = DriveRefreshCoordinator(
        refreshed.set,
        lambda: active,
        debounce_seconds=0.01,
    )
    coordinator.start()
    try:
        coordinator.notify_change()
        time.sleep(0.05)
        assert not refreshed.is_set()
        active = False
        assert refreshed.wait(2)
    finally:
        coordinator.stop()


def test_coordinator_preserves_worker_after_refresh_failure():
    succeeded = threading.Event()
    attempts = 0

    def refresh():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic")
        succeeded.set()

    coordinator = DriveRefreshCoordinator(refresh, lambda: False, debounce_seconds=0.01)
    coordinator.start()
    try:
        coordinator.notify_change()
        time.sleep(0.05)
        coordinator.notify_change()
        assert succeeded.wait(1)
        assert attempts == 2
    finally:
        coordinator.stop()
