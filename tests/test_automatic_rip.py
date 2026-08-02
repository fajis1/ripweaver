from dataclasses import dataclass

from mkv_episode_matcher.backend.automatic_rip import (
    AutomaticRipCoordinator,
    _has_prior_disc_work,
)
from mkv_episode_matcher.disc.drive_watcher import (
    DriveStatusSnapshot,
    PublicDriveStatus,
)


def _snapshot(*loaded: int) -> DriveStatusSnapshot:
    return DriveStatusSnapshot(
        drives=tuple(
            PublicDriveStatus(
                index, True, index in loaded, "disc" if index in loaded else None
            )
            for index in range(3)
        ),
        refreshed_at="2026-08-01T00:00:00+00:00",
        status="ready",
    )


def test_insertions_launch_once_and_removal_rearms_drive(monkeypatch):
    launched = []
    coordinator = AutomaticRipCoordinator(launched.append)
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.automatic_rip.threading.Thread.start",
        lambda thread: thread.run(),
    )

    coordinator.observe(_snapshot(0, 1), enabled=True)
    coordinator.observe(_snapshot(0, 1), enabled=True)
    coordinator.observe(_snapshot(1), enabled=True)
    coordinator.observe(_snapshot(0, 1), enabled=True)

    assert launched == [0, 1, 0]


def test_disabled_automation_tracks_loaded_disc_without_launching(monkeypatch):
    launched = []
    coordinator = AutomaticRipCoordinator(launched.append)
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.automatic_rip.threading.Thread.start",
        lambda thread: thread.run(),
    )

    coordinator.observe(_snapshot(2), enabled=False)
    coordinator.observe(_snapshot(2), enabled=True)

    assert launched == []


@dataclass
class _Job:
    job_id: str
    state: str
    preview: dict[str, object]


class _Store:
    def __init__(self, *jobs):
        self.jobs = jobs

    def list_jobs(self, *, limit=50):
        return self.jobs[:limit]


def _job(job_id: str, fingerprint: str, state: str = "completed") -> _Job:
    return _Job(
        job_id,
        state,
        {
            "jobs": [
                {
                    "staging_destination": (
                        f".staging/disc-01/attempt-new/{fingerprint}/title-000"
                    )
                }
            ]
        },
    )


def test_previously_known_disc_blocks_automatic_rerip():
    prepared = _job("new", "0123456789abcdef", "awaiting_review")
    previous = _job("old", "0123456789abcdef")

    assert _has_prior_disc_work(_Store(prepared, previous), prepared) is True


def test_different_or_cancelled_disc_does_not_block_automatic_rip():
    prepared = _job("new", "0123456789abcdef", "awaiting_review")
    other = _job("other", "fedcba9876543210")
    cancelled = _job("old", "0123456789abcdef", "cancelled")

    assert _has_prior_disc_work(_Store(prepared, other, cancelled), prepared) is False
