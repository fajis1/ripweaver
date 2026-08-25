import threading
import time

import pytest

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


def test_media_event_uses_one_lightweight_refresh_while_full_scan_is_deferred():
    active = True
    lightweight = threading.Event()
    full = threading.Event()
    lightweight_calls = []
    coordinator = DriveRefreshCoordinator(
        full.set,
        lambda: active,
        periodic_refresh=lambda: (
            lightweight_calls.append("native"),
            lightweight.set(),
        ),
        debounce_seconds=0.01,
    )
    coordinator.start()
    try:
        coordinator.notify_change()
        assert lightweight.wait(1)
        time.sleep(0.05)
        assert lightweight_calls == ["native"]
        assert not full.is_set()
        assert coordinator.refresh_deferred() is True

        active = False
        assert full.wait(2)
    finally:
        coordinator.stop()


def test_manual_refresh_request_is_deferred_without_faking_media_change():
    active = True
    refreshed = threading.Event()
    invalidations = []
    coordinator = DriveRefreshCoordinator(
        refreshed.set,
        lambda: active,
        invalidate_bindings=lambda: invalidations.append("invalidated"),
        debounce_seconds=0.01,
    )
    coordinator.start()
    try:
        coordinator.request_refresh()
        time.sleep(0.05)
        assert coordinator.refresh_deferred() is True
        assert not refreshed.is_set()
        assert invalidations == []
        active = False
        assert refreshed.wait(2)
        assert invalidations == []
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

    coordinator = DriveRefreshCoordinator(
        refresh,
        lambda: False,
        debounce_seconds=0.01,
        failure_backoff_seconds=(0.01,),
    )
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
        failure_backoff_seconds=(0.01,),
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


def test_failure_retry_backoff_is_required_and_positive():
    with pytest.raises(ValueError, match="positive"):
        DriveRefreshCoordinator(
            lambda: None,
            lambda: False,
            failure_backoff_seconds=(),
        )
    with pytest.raises(ValueError, match="positive"):
        DriveRefreshCoordinator(
            lambda: None,
            lambda: False,
            failure_backoff_seconds=(0,),
        )


def test_failed_refresh_does_not_enter_a_tight_retry_loop():
    attempted = threading.Event()
    attempts = 0

    def fail():
        nonlocal attempts
        attempts += 1
        attempted.set()
        raise RuntimeError("synthetic")

    coordinator = DriveRefreshCoordinator(
        fail,
        lambda: False,
        debounce_seconds=0.01,
        failure_backoff_seconds=(0.2,),
    )
    coordinator.start()
    try:
        coordinator.notify_change()
        assert attempted.wait(1)
        time.sleep(0.05)
        assert attempts == 1
    finally:
        coordinator.stop()


def test_repeated_timeouts_pause_automatic_discovery_until_manual_refresh():
    second_attempt = threading.Event()
    resumed = threading.Event()
    attempts = 0

    def refresh():
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            if attempts == 2:
                second_attempt.set()
            raise RuntimeError("synthetic discovery timed out")
        resumed.set()

    coordinator = DriveRefreshCoordinator(
        refresh,
        lambda: False,
        debounce_seconds=0.01,
        failure_backoff_seconds=(0.01,),
    )
    coordinator.start()
    try:
        coordinator.notify_change()
        assert second_attempt.wait(1)
        for _ in range(100):
            if coordinator.automatic_discovery_status()["paused"]:
                break
            time.sleep(0.01)
        assert coordinator.automatic_discovery_status() == {
            "paused": True,
            "pause_reason": "repeated_timeouts",
            "consecutive_timeout_count": 2,
        }
        time.sleep(0.05)
        assert attempts == 2

        coordinator.request_refresh()
        assert resumed.wait(1)
        assert coordinator.automatic_discovery_status() == {
            "paused": False,
            "pause_reason": None,
            "consecutive_timeout_count": 0,
        }
    finally:
        coordinator.stop()


def test_physical_media_event_resumes_paused_automatic_discovery():
    second_attempt = threading.Event()
    resumed = threading.Event()
    attempts = 0

    def refresh():
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            if attempts == 2:
                second_attempt.set()
            raise RuntimeError("synthetic discovery timed out")
        resumed.set()

    coordinator = DriveRefreshCoordinator(
        refresh,
        lambda: False,
        debounce_seconds=0.01,
        failure_backoff_seconds=(0.01,),
    )
    coordinator.start()
    try:
        coordinator.notify_change()
        assert second_attempt.wait(1)
        for _ in range(100):
            if coordinator.automatic_discovery_status()["paused"]:
                break
            time.sleep(0.01)
        assert coordinator.automatic_discovery_status()["paused"] is True

        coordinator.notify_change()
        assert resumed.wait(1)
        assert attempts == 3
    finally:
        coordinator.stop()


def test_timeout_failure_limit_requires_repeated_failures():
    with pytest.raises(ValueError, match="at least two"):
        DriveRefreshCoordinator(
            lambda: None,
            lambda: False,
            timeout_failure_limit=1,
        )
