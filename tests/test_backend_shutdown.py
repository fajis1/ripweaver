import asyncio
import sys
import threading
from types import SimpleNamespace

from mkv_episode_matcher.backend import automatic_rip, main
from mkv_episode_matcher.cli import serve
from mkv_episode_matcher.disc.makemkv_process_control import (
    SupervisedShutdownResult,
)


def test_backend_shutdown_settles_makemkv_after_stopping_drive_events(monkeypatch):
    events = []
    monkeypatch.setattr(main, "_catalogue_contribution_worker", None)
    monkeypatch.setattr(main, "_downstream_worker", None)
    monkeypatch.setattr(
        main,
        "stop_windows_drive_events",
        lambda: events.append("stop-drive-events"),
    )

    def settle_children():
        events.append("settle-makemkv-children")
        return SupervisedShutdownResult(tracked_count=3, settled_count=3)

    monkeypatch.setattr(main, "shutdown_makemkv_process_control", settle_children)

    asyncio.run(main.shutdown_event())

    assert events == ["stop-drive-events", "settle-makemkv-children"]


def test_held_review_server_suppresses_automatic_downstream_worker(monkeypatch):
    config = SimpleNamespace(automatic_processing_enabled=True)

    monkeypatch.setattr(main, "automatic_rip_startup_held", lambda: True)
    assert main._automatic_downstream_enabled(config) is False

    monkeypatch.setattr(main, "automatic_rip_startup_held", lambda: False)
    assert main._automatic_downstream_enabled(config) is True


def test_normal_startup_arms_one_minute_queue_resume(monkeypatch):
    pauses = []
    scheduled = {}
    observed = []
    snapshot = object()
    store = SimpleNamespace(
        is_paused=lambda: False,
        set_paused=lambda value: pauses.append(value),
    )
    config = SimpleNamespace(automatic_processing_enabled=True)

    monkeypatch.setattr(main, "cancel_startup_queue_resume", lambda: False)
    monkeypatch.setattr(main, "automatic_rip_startup_held", lambda: False)
    monkeypatch.setattr(
        main,
        "schedule_startup_queue_resume",
        lambda callback, *, delay_seconds: scheduled.update(
            callback=callback, delay_seconds=delay_seconds
        ),
    )
    monkeypatch.setattr(
        main, "get_drive_watcher", lambda: SimpleNamespace(snapshot=lambda: snapshot)
    )
    monkeypatch.setattr(
        automatic_rip,
        "observe_automatic_drives",
        lambda value, **kwargs: observed.append((value, kwargs)),
    )

    assert main._arm_startup_queue_resume(config, store) is True
    assert pauses == [True]
    assert scheduled["delay_seconds"] == 60.0

    scheduled["callback"]()

    assert pauses == [True, False]
    assert observed == [
        (
            snapshot,
            {"enabled": True, "processing_paused": False},
        )
    ]


def test_durable_queue_pause_survives_backend_restart(monkeypatch):
    scheduled = []
    pauses = []
    store = SimpleNamespace(
        is_paused=lambda: True,
        set_paused=lambda value: pauses.append(value),
    )
    config = SimpleNamespace(automatic_processing_enabled=True)

    monkeypatch.setattr(main, "cancel_startup_queue_resume", lambda: False)
    monkeypatch.setattr(main, "automatic_rip_startup_held", lambda: False)
    monkeypatch.setattr(
        main,
        "schedule_startup_queue_resume",
        lambda *_args, **_kwargs: scheduled.append(True),
    )

    assert main._arm_startup_queue_resume(config, store) is False
    assert pauses == []
    assert scheduled == []


def test_held_review_startup_skips_unattended_workers(tmp_path, monkeypatch):
    config = SimpleNamespace(
        automatic_processing_enabled=True,
        cache_dir=tmp_path / "cache",
    )
    public_store = SimpleNamespace(reconcile_incomplete=lambda: ())
    pipeline_store = SimpleNamespace(reconcile_incomplete=lambda **_kwargs: ())

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("held startup launched unattended work")

    silent_logger = SimpleNamespace(
        remove=lambda *_args, **_kwargs: None,
        add=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        critical=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(main, "logger", silent_logger)
    monkeypatch.setattr(
        main,
        "get_config_manager",
        lambda: SimpleNamespace(load=lambda: config),
    )
    monkeypatch.setattr(
        main,
        "start_makemkv_process_control",
        lambda: SimpleNamespace(cleared_count=0),
    )
    monkeypatch.setattr(main, "get_orchestration_store", lambda: public_store)
    monkeypatch.setattr(main, "get_pipeline_queue_store", lambda: pipeline_store)
    monkeypatch.setattr(main, "start_windows_drive_events", lambda: False)
    monkeypatch.setattr(main, "automatic_rip_startup_held", lambda: True)
    monkeypatch.setattr(main, "get_catalogue_contribution_store", unexpected_call)
    monkeypatch.setattr(main, "get_engine", unexpected_call)
    monkeypatch.setattr(threading, "Thread", ImmediateThread)
    monkeypatch.setattr(main, "_catalogue_contribution_worker", None)
    monkeypatch.setattr(main, "_downstream_worker", None)

    asyncio.run(main.startup_event())

    assert main._catalogue_contribution_worker is None
    assert main._downstream_worker is None


def test_cli_serve_bounds_graceful_shutdown_before_child_settlement(monkeypatch):
    calls = []

    def run(application, **kwargs):
        calls.append((application, kwargs))

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run))

    serve(
        port=8765,
        host="127.0.0.1",
        no_browser=True,
        hold_automatic_rips=False,
    )

    assert calls == [
        (
            main.app,
            {
                "host": "127.0.0.1",
                "port": 8765,
                "timeout_graceful_shutdown": 15,
            },
        )
    ]
