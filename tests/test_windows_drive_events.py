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


def test_coordinator_retries_failed_refresh_without_another_external_event():
    succeeded = threading.Event()
    attempts = 0

    def refresh():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic")
        succeeded.set()

    coordinator = DriveRefreshCoordinator(
        refresh,
        lambda: False,
        debounce_seconds=0.01,
    )
    coordinator.start()
    try:
        coordinator.notify_change()
        assert succeeded.wait(1)
        assert attempts == 2
    finally:
        coordinator.stop()


def test_coordinator_periodically_reconciles_without_browser_activity():
    refreshed = threading.Event()
    coordinator = DriveRefreshCoordinator(
        refreshed.set,
        lambda: False,
        debounce_seconds=0.01,
        poll_seconds=0.01,
    )
    coordinator.start()
    try:
        assert refreshed.wait(1)
    finally:
        coordinator.stop()


def test_periodic_reconciliation_does_not_invalidate_disc_bindings():
    refreshed = threading.Event()
    invalidations = []
    coordinator = DriveRefreshCoordinator(
        refreshed.set,
        lambda: False,
        invalidate_bindings=lambda: invalidations.append("invalidated"),
        debounce_seconds=0.01,
        poll_seconds=0.01,
    )
    coordinator.start()
    try:
        assert refreshed.wait(1)
        assert invalidations == []
    finally:
        coordinator.stop()


def test_periodic_reconciliation_uses_lightweight_refresh():
    refreshed = threading.Event()
    full_refreshes = []
    coordinator = DriveRefreshCoordinator(
        lambda: full_refreshes.append("full"),
        lambda: False,
        periodic_refresh=refreshed.set,
        debounce_seconds=0.01,
        poll_seconds=0.01,
    )
    coordinator.start()
    try:
        assert refreshed.wait(1)
        assert full_refreshes == []
    finally:
        coordinator.stop()


def test_media_change_invalidates_bindings_before_refresh():
    refreshed = threading.Event()
    order = []
    coordinator = DriveRefreshCoordinator(
        lambda: (order.append("refresh"), refreshed.set()),
        lambda: False,
        invalidate_bindings=lambda: order.append("invalidate"),
        debounce_seconds=0.01,
    )
    coordinator.start()
    try:
        coordinator.notify_change()
        assert refreshed.wait(1)
        assert order == ["invalidate", "refresh"]
    finally:
        coordinator.stop()
