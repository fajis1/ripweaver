from types import SimpleNamespace

import pytest

from mkv_episode_matcher.backend.routers import rip
from mkv_episode_matcher.disc import eject
from mkv_episode_matcher.disc.drive_watcher import DriveWatcher
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.preflight import CommandResult


def _drive_result(source: str) -> CommandResult:
    return CommandResult(
        command=("makemkvcon64.exe", "info", source),
        return_code=0,
        stdout='DRV:0,2,999,1,"private hardware","Test Disc","D:"\n',
        stderr="",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:00:01+00:00",
    )


def test_eject_rejects_non_drive_letter_without_opening_device(monkeypatch):
    monkeypatch.setattr(eject.sys, "platform", "win32")

    with pytest.raises(eject.DiscEjectError, match="device name is invalid"):
        eject.eject_optical_drive("private-device-path")


def test_eject_uses_mci_fallback_when_storage_control_is_rejected(monkeypatch):
    calls = []
    monkeypatch.setattr(eject.sys, "platform", "win32")
    monkeypatch.setattr(
        eject,
        "_eject_with_storage_ioctl",
        lambda device: calls.append(("ioctl", device)) or False,
    )
    monkeypatch.setattr(
        eject, "_eject_with_mci", lambda device: calls.append(("mci", device)) or True
    )

    eject.eject_optical_drive("D:")

    assert calls == [("ioctl", "D:"), ("mci", "D:")]


def test_eject_still_uses_mci_when_storage_driver_claims_success(monkeypatch):
    calls = []
    monkeypatch.setattr(eject.sys, "platform", "win32")
    monkeypatch.setattr(
        eject,
        "_eject_with_storage_ioctl",
        lambda device: calls.append(("ioctl", device)) or True,
    )
    monkeypatch.setattr(
        eject, "_eject_with_mci", lambda device: calls.append(("mci", device)) or True
    )

    eject.eject_optical_drive("D:")

    assert calls == [("ioctl", "D:"), ("mci", "D:")]


def test_eject_reports_failure_only_after_both_exact_drive_methods_fail(monkeypatch):
    monkeypatch.setattr(eject.sys, "platform", "win32")
    monkeypatch.setattr(eject, "_eject_with_storage_ioctl", lambda _device: False)
    monkeypatch.setattr(eject, "_eject_with_mci", lambda _device: False)

    with pytest.raises(eject.DiscEjectError, match="close programs"):
        eject.eject_optical_drive("D:")


def test_eject_endpoint_resolves_exact_drive_and_calls_guarded_adapter(
    tmp_path, monkeypatch
):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(load=lambda: SimpleNamespace(makemkv_path=executable)),
    )
    ejected = []
    monkeypatch.setattr(rip, "eject_optical_drive", ejected.append)

    result = rip.eject_drive(
        0,
        rip.EjectDriveRequest(confirm_eject=True),
        OrchestrationStore(tmp_path / "jobs.sqlite3"),
        lambda _executable, source, **_kwargs: _drive_result(source),
        DriveWatcher(),
        rip.RipExecutionRegistry(),
    )

    assert result == {"status": "ejected", "drive_index": 0}
    assert ejected == ["D:"]


def test_eject_endpoint_uses_primed_drive_cache_without_another_scan(
    tmp_path, monkeypatch
):
    watcher = DriveWatcher(lambda *_args, **_kwargs: _drive_result("disc:9999"))
    watcher.refresh(tmp_path / "makemkvcon64.exe")
    ejected = []
    monkeypatch.setattr(rip, "eject_optical_drive", ejected.append)

    result = rip.eject_drive(
        0,
        rip.EjectDriveRequest(confirm_eject=True),
        OrchestrationStore(tmp_path / "jobs.sqlite3"),
        lambda *_args, **_kwargs: pytest.fail("primed eject must not rescan"),
        watcher,
        rip.RipExecutionRegistry(),
    )

    assert result == {"status": "ejected", "drive_index": 0}
    assert ejected == ["D:"]


def test_eject_reconciles_stale_running_job_without_live_executor(
    tmp_path, monkeypatch
):
    executable = tmp_path / "makemkvcon64.exe"
    executable.write_bytes(b"synthetic")
    monkeypatch.setattr(
        rip,
        "get_config_manager",
        lambda: SimpleNamespace(load=lambda: SimpleNamespace(makemkv_path=executable)),
    )
    job = SimpleNamespace(
        job_id="stale-job",
        state="running",
        preview={"drives": [{"drive_index": 0}]},
    )

    class FakeStore:
        def list_jobs(self):
            return [job]

        def reconcile_incomplete(self):
            job.state = "paused"

        def get_job(self, _job_id):
            return job

        def return_to_review(self, *_args, **_kwargs):
            raise AssertionError("paused stale jobs do not return to review")

    store = FakeStore()
    ejected = []
    monkeypatch.setattr(rip, "eject_optical_drive", ejected.append)

    result = rip.eject_drive(
        0,
        rip.EjectDriveRequest(confirm_eject=True),
        store,
        lambda _executable, source, **_kwargs: _drive_result(source),
        DriveWatcher(),
        rip.RipExecutionRegistry(),
    )

    assert result == {"status": "ejected", "drive_index": 0}
    assert job.state == "paused"
    assert ejected == ["D:"]


def test_automatic_eject_runs_only_for_completed_drive_without_competing_work(
    tmp_path, monkeypatch
):
    ejected = []
    monkeypatch.setattr(rip, "eject_optical_drive", ejected.append)
    completed = SimpleNamespace(
        job_id="completed-job",
        preview={"drives": [{"drive_index": 0}]},
    )
    store = SimpleNamespace(list_jobs=lambda: [completed])

    rip._auto_eject_completed_job_drives(
        completed,
        store,
        lambda _executable, source, **_kwargs: _drive_result(source),
        tmp_path / "makemkvcon64.exe",
        600,
    )

    assert ejected == ["D:"]


def test_automatic_eject_skips_drive_with_other_queued_work(tmp_path, monkeypatch):
    ejected = []
    monkeypatch.setattr(rip, "eject_optical_drive", ejected.append)
    completed = SimpleNamespace(
        job_id="completed-job",
        preview={"drives": [{"drive_index": 0}]},
    )
    competing = SimpleNamespace(
        job_id="queued-job",
        state="queued",
        preview={"drives": [{"drive_index": 0}]},
    )
    store = SimpleNamespace(list_jobs=lambda: [completed, competing])

    rip._auto_eject_completed_job_drives(
        completed,
        store,
        lambda *_args, **_kwargs: pytest.fail("drive must not be read"),
        tmp_path / "makemkvcon64.exe",
        600,
    )

    assert ejected == []


def test_automatic_eject_ignores_reconciled_paused_job(tmp_path, monkeypatch):
    ejected = []
    monkeypatch.setattr(rip, "eject_optical_drive", ejected.append)
    completed = SimpleNamespace(
        job_id="completed-job",
        preview={"drives": [{"drive_index": 0}]},
    )
    reconciled = SimpleNamespace(
        job_id="older-job",
        state="paused",
        preview={"drives": [{"drive_index": 0}]},
    )
    store = SimpleNamespace(list_jobs=lambda: [completed, reconciled])

    rip._auto_eject_completed_job_drives(
        completed,
        store,
        lambda _executable, source, **_kwargs: _drive_result(source),
        tmp_path / "makemkvcon64.exe",
        600,
    )

    assert ejected == ["D:"]
