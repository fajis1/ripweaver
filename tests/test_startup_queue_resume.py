import threading

from mkv_episode_matcher.backend import startup_queue_resume
from mkv_episode_matcher.backend.routers import rip
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore


def test_startup_queue_resume_runs_once_after_grace_period():
    resumed = threading.Event()
    try:
        startup_queue_resume.schedule_startup_queue_resume(
            resumed.set, delay_seconds=0.01
        )
        assert startup_queue_resume.startup_queue_resume_pending() is True
        assert startup_queue_resume.startup_queue_resume_seconds() == 1
        assert resumed.wait(1)
        assert startup_queue_resume.startup_queue_resume_pending() is False
        assert startup_queue_resume.startup_queue_resume_seconds() is None
    finally:
        startup_queue_resume.cancel_startup_queue_resume()


def test_explicit_pause_cancels_pending_startup_resume(tmp_path, monkeypatch):
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    cancelled = []
    monkeypatch.setattr(
        rip,
        "cancel_startup_queue_resume",
        lambda: cancelled.append("cancelled"),
    )

    response = rip.pause_pipeline(
        rip.PipelineControlRequest(confirm_control=True), store
    )

    assert cancelled == ["cancelled"]
    assert response["paused"] is True
    assert store.is_paused() is True


def test_fresh_pause_cancels_startup_queue_resume():
    resumed = threading.Event()
    try:
        startup_queue_resume.schedule_startup_queue_resume(
            resumed.set, delay_seconds=0.1
        )
        assert startup_queue_resume.cancel_startup_queue_resume() is True
        assert resumed.wait(0.2) is False
        assert startup_queue_resume.startup_queue_resume_pending() is False
    finally:
        startup_queue_resume.cancel_startup_queue_resume()
