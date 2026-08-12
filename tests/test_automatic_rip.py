from dataclasses import dataclass
from types import SimpleNamespace

from mkv_episode_matcher.backend.automatic_rip import (
    _AUTOMATIC_UNMATCHED_CODES,
    AutomaticRipCoordinator,
    _automatic_series_name,
    _has_prior_disc_work,
    _recover_bound_automatic_job,
    run_automatic_drive,
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


def test_automatic_series_name_recovers_disc_label_without_using_volume_as_season():
    assert (
        _automatic_series_name(
            "Unmatched",
            "Faerie-Tale-Theatre-1--disc-01-fingerprint-title-000",
        )
        == "Faerie Tale Theatre"
    )
    assert (
        _automatic_series_name(
            "Reviewed Series",
            "Different-Disc-4--disc-01-fingerprint-title-000",
        )
        == "Reviewed Series"
    )


def test_failed_all_season_analysis_requires_explicit_retry():
    assert "unmatched_disc_analysis_required" in _AUTOMATIC_UNMATCHED_CODES
    assert "all_season_analysis_failed" not in _AUTOMATIC_UNMATCHED_CODES
    assert "all_season_analysis_running" not in _AUTOMATIC_UNMATCHED_CODES


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


def test_unmapped_or_ignored_devices_never_launch_automatic_work(monkeypatch):
    launched = []
    coordinator = AutomaticRipCoordinator(launched.append)
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.automatic_rip.threading.Thread.start",
        lambda thread: thread.run(),
    )
    snapshot = DriveStatusSnapshot(
        drives=(
            PublicDriveStatus(0, True, True, "disc", mapping_status="unmapped"),
            PublicDriveStatus(1, True, True, "disc", mapping_status="ignored"),
            PublicDriveStatus(2, True, True, "disc", mapping_status="trusted"),
        ),
        refreshed_at="2026-08-10T00:00:00+00:00",
        status="ready",
    )

    coordinator.observe(snapshot, enabled=True)

    assert launched == [2]


def test_changed_disc_label_launches_without_observed_empty_tray(monkeypatch):
    launched = []
    coordinator = AutomaticRipCoordinator(launched.append)
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.automatic_rip.threading.Thread.start",
        lambda thread: thread.run(),
    )
    first = DriveStatusSnapshot(
        drives=(PublicDriveStatus(0, True, True, "FAERIE_TALE_THEATRE_5"),),
        refreshed_at="2026-08-01T00:00:00+00:00",
        status="ready",
    )
    replacement = DriveStatusSnapshot(
        drives=(PublicDriveStatus(0, True, True, "FAERIE_TALE_THEATRE_6"),),
        refreshed_at="2026-08-01T00:01:00+00:00",
        status="ready",
    )

    coordinator.observe(first, enabled=True)
    coordinator.observe(replacement, enabled=True)

    assert launched == [0, 0]


def test_transient_worker_failure_rearms_unchanged_loaded_disc(monkeypatch):
    attempts = 0

    def worker(_drive_index):
        nonlocal attempts
        attempts += 1
        return attempts > 1

    coordinator = AutomaticRipCoordinator(worker)
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.automatic_rip.threading.Thread.start",
        lambda thread: thread.run(),
    )

    coordinator.observe(_snapshot(1), enabled=True)
    coordinator.observe(_snapshot(1), enabled=True)
    coordinator.observe(_snapshot(1), enabled=True)

    assert attempts == 2


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

    def get_job(self, job_id):
        return next(job for job in self.jobs if job.job_id == job_id)


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


def test_failed_zero_output_attempt_does_not_block_automatic_retry():
    prepared = _job("new", "0123456789abcdef", "awaiting_review")
    failed = _job("failed", "0123456789abcdef", "failed")

    assert _has_prior_disc_work(_Store(prepared, failed), prepared) is False


def test_failed_attempt_with_verified_output_still_blocks_automatic_rerip():
    prepared = _job("new", "0123456789abcdef", "awaiting_review")
    failed = _job("failed", "0123456789abcdef", "failed")
    store = _Store(prepared, failed)
    store.list_events = lambda job_id: (
        SimpleNamespace(event_type="rip_title_completed"),
    )

    assert _has_prior_disc_work(store, prepared) is True


def test_abandoned_review_plan_is_not_treated_as_previous_rip_output():
    prepared = _job("new", "0123456789abcdef", "awaiting_review")
    abandoned = _job("old", "0123456789abcdef", "awaiting_review")

    assert _has_prior_disc_work(_Store(prepared, abandoned), prepared) is False


def test_clean_job_bound_before_preparation_error_is_recovered():
    fingerprint = "0123456789abcdef"
    job = _job("rip-clean", fingerprint, "awaiting_review")
    job.preview["requires_review"] = False
    watcher = type(
        "Watcher",
        (),
        {
            "snapshot": lambda _self: DriveStatusSnapshot(
                drives=(
                    PublicDriveStatus(
                        4,
                        True,
                        True,
                        "disc",
                        current_disc_fingerprint=fingerprint,
                        current_job_id=job.job_id,
                    ),
                ),
                refreshed_at="2026-08-12T00:00:00+00:00",
                status="ready",
            )
        },
    )()

    assert _recover_bound_automatic_job(4, watcher, _Store(job)) is job


def test_bound_job_with_real_review_requirement_is_not_recovered():
    fingerprint = "0123456789abcdef"
    job = _job("rip-review", fingerprint, "awaiting_review")
    job.preview["requires_review"] = True
    watcher = type(
        "Watcher",
        (),
        {
            "snapshot": lambda _self: DriveStatusSnapshot(
                drives=(
                    PublicDriveStatus(
                        4,
                        True,
                        True,
                        "disc",
                        current_disc_fingerprint=fingerprint,
                        current_job_id=job.job_id,
                    ),
                ),
                refreshed_at="2026-08-12T00:00:00+00:00",
                status="ready",
            )
        },
    )()

    assert _recover_bound_automatic_job(4, watcher, _Store(job)) is None


def test_worker_resumes_same_clean_job_after_preparation_return_fails(monkeypatch):
    fingerprint = "0123456789abcdef"
    job = SimpleNamespace(
        job_id="rip-clean",
        state="awaiting_review",
        plan_sha256="a" * 64,
        preview={
            "requires_review": False,
            "jobs": [
                {
                    "job_id": "disc-01-title-001",
                    "staging_destination": (
                        f".staging/disc-01/attempt-new/{fingerprint}/title-001"
                    ),
                }
            ],
        },
    )

    class Store:
        def get_job(self, _job_id):
            return job

        def list_jobs(self, *, limit=50):
            return (job,)[:limit]

        def authorize(self, *_args, **_kwargs):
            job.state = "authorized"
            return job

        def queue(self, *_args, **_kwargs):
            job.state = "queued"
            return job

    store = Store()
    watcher = SimpleNamespace(
        snapshot=lambda: DriveStatusSnapshot(
            drives=(
                PublicDriveStatus(
                    4,
                    True,
                    True,
                    "disc",
                    current_disc_fingerprint=fingerprint,
                    current_job_id=job.job_id,
                ),
            ),
            refreshed_at="2026-08-12T00:00:00+00:00",
            status="ready",
        )
    )
    executed = []

    monkeypatch.setattr(
        "mkv_episode_matcher.core.config_manager.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(automatic_processing_enabled=True)
        ),
    )
    dependency_values = {
        "get_drive_watcher": watcher,
        "get_orchestration_store": store,
        "get_private_binding_store": object(),
        "get_pipeline_queue_store": object(),
        "get_disc_inventory_runner": object(),
        "get_rip_execution_registry": object(),
        "get_rip_queue_runner": object(),
        "get_pipeline_contract_root": object(),
    }
    for name, value in dependency_values.items():
        monkeypatch.setattr(
            f"mkv_episode_matcher.backend.dependencies.{name}",
            lambda value=value: value,
        )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.routers.rip.prepare_drive_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("response failed")),
    )

    def execute(job_id, *_args, **_kwargs):
        executed.append(job_id)
        job.state = "completed"
        return {"state": "completed"}

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.routers.rip.execute_rip_job", execute
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.automatic_rip._continue_automatic_downstream",
        lambda *_args, **_kwargs: None,
    )
    store.get_pipeline_settings = lambda _job_id: {"handbrake_profile_id": None}

    assert run_automatic_drive(4) is True
    assert executed == [job.job_id]
    assert job.state == "completed"
