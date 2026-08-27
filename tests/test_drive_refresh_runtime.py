from pathlib import Path

import pytest

from mkv_episode_matcher.backend import dependencies
from mkv_episode_matcher.backend.rip_runtime import RipExecutionRegistry
from mkv_episode_matcher.backend.routers import rip
from mkv_episode_matcher.disc.drive_watcher import DriveWatcher


def test_manual_full_refresh_defers_without_starting_makemkv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = []
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: calls.append("makemkv"),
        native_discovery=lambda: (),
    )
    registry = RipExecutionRegistry()
    registry.attach("active-rip", tmp_path / "run", frozenset({0}))
    queued = []
    monkeypatch.setattr(
        rip,
        "request_windows_drive_refresh",
        lambda: (queued.append("deferred"), True)[1],
    )
    monkeypatch.setattr(rip, "windows_drive_refresh_deferred", lambda: True)

    response = rip.refresh_drive_status(
        rip.RefreshDrivesRequest(confirm_read=True), watcher, registry
    )

    assert calls == []
    assert queued == ["deferred"]
    assert response["refresh_deferred"] is True
    assert response["busy_drive_indexes"] == [0]
    assert response["physical_drive_operations"] == {0: "MakeMKV rip"}


def test_drive_status_surfaces_automatic_discovery_circuit_breaker(
    monkeypatch: pytest.MonkeyPatch,
):
    watcher = DriveWatcher(lambda *_args, **_kwargs: None, native_discovery=lambda: ())
    monkeypatch.setattr(rip, "windows_drive_refresh_deferred", lambda: False)
    monkeypatch.setattr(
        rip,
        "windows_drive_automatic_discovery_status",
        lambda: {
            "paused": True,
            "pause_reason": "repeated_timeouts",
            "consecutive_timeout_count": 2,
        },
    )

    response = rip._drive_status_response(watcher, RipExecutionRegistry())

    assert response["automatic_discovery_paused"] is True
    assert response["automatic_discovery_pause_reason"] == "repeated_timeouts"
    assert response["automatic_discovery_timeout_count"] == 2


def test_production_drive_events_do_not_poll_idle_optical_drives(monkeypatch):
    captured = {}

    class Coordinator:
        def __init__(self, *_args, **kwargs):
            captured["coordinator_kwargs"] = kwargs

        def start(self):
            captured["coordinator_started"] = True

        def notify_change(self):
            captured["startup_refresh_queued"] = True

        def stop(self):
            pass

    class Listener:
        def __init__(self, _on_change):
            pass

        def start(self):
            captured["listener_started"] = True

        def stop(self):
            pass

    watcher = DriveWatcher(lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dependencies, "_drive_refresh_coordinator", None)
    monkeypatch.setattr(dependencies, "_windows_volume_listener", None)
    monkeypatch.setattr(dependencies, "DriveRefreshCoordinator", Coordinator)
    monkeypatch.setattr(dependencies, "WindowsVolumeEventListener", Listener)
    monkeypatch.setattr(dependencies, "get_drive_watcher", lambda: watcher)

    assert dependencies.start_windows_drive_events() is True
    assert captured["coordinator_kwargs"]["poll_seconds"] is None
    assert callable(captured["coordinator_kwargs"]["periodic_refresh"])
    assert captured["coordinator_started"] is True
    assert captured["listener_started"] is True
    assert captured["startup_refresh_queued"] is True
