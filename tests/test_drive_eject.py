from types import SimpleNamespace

import pytest

from mkv_episode_matcher.backend.rip_runtime import RipExecutionRegistry
from mkv_episode_matcher.backend.routers import rip
from mkv_episode_matcher.disc import eject
from mkv_episode_matcher.disc.drive_watcher import DriveWatcher
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.preflight import CommandResult, PreflightError


def _drive_result(source: str) -> CommandResult:
    return CommandResult(
        command=("makemkvcon64.exe", "info", source),
        return_code=0,
        stdout='DRV:0,2,999,1,"private hardware","Test Disc","D:"\n',
        stderr="",
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:00:01+00:00",
    )


def _accepted() -> eject.EjectMethodResult:
    return eject.EjectMethodResult(accepted=True)


def _rejected(method: str, stage: str, error_code: int) -> eject.EjectMethodResult:
    return eject.EjectMethodResult(
        accepted=False,
        failures=(eject.EjectFailure(method, stage, error_code),),
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
        lambda device: calls.append(("ioctl", device))
        or _rejected("storage", "device-control-read-write", 32),
    )
    monkeypatch.setattr(
        eject,
        "_eject_with_mci",
        lambda device: calls.append(("mci", device)) or _accepted(),
    )

    eject.eject_optical_drive("D:")

    assert calls == [("ioctl", "D:"), ("mci", "D:")]


def test_eject_still_uses_mci_when_storage_driver_claims_success(monkeypatch):
    calls = []
    monkeypatch.setattr(eject.sys, "platform", "win32")
    monkeypatch.setattr(
        eject,
        "_eject_with_storage_ioctl",
        lambda device: calls.append(("ioctl", device)) or _accepted(),
    )
    monkeypatch.setattr(
        eject,
        "_eject_with_mci",
        lambda device: calls.append(("mci", device)) or _accepted(),
    )

    eject.eject_optical_drive("D:")

    assert calls == [("ioctl", "D:"), ("mci", "D:")]


def test_eject_reports_failure_only_after_both_exact_drive_methods_fail(monkeypatch):
    monkeypatch.setattr(eject.sys, "platform", "win32")
    monkeypatch.setattr(
        eject,
        "_eject_with_storage_ioctl",
        lambda _device: eject.EjectMethodResult(
            accepted=False,
            failures=(
                eject.EjectFailure("storage", "open-read-write", 5),
                eject.EjectFailure("storage", "open-read-only", 32),
                eject.EjectFailure("storage", "open-metadata-only", 21),
            ),
        ),
    )
    monkeypatch.setattr(
        eject,
        "_eject_with_mci",
        lambda _device: _rejected("mci", "door-open", 263),
    )

    with pytest.raises(eject.DiscEjectError, match="close programs") as failure:
        eject.eject_optical_drive("D:")

    message = str(failure.value)
    assert "storage/open-read-write=error-5" in message
    assert "storage/open-read-only=error-32" in message
    assert "storage/open-metadata-only=error-21" in message
    assert "mci/door-open=error-263" in message
    assert "D:" not in message


def test_storage_eject_retains_each_safe_open_failure_code():
    invalid_handle = eject.wintypes.HANDLE(-1).value

    class FakeFunction:
        def __init__(self, responses):
            self.responses = iter(responses)

        def __call__(self, *_args):
            return next(self.responses)

    errors = iter((5, 32, 21))
    kernel32 = SimpleNamespace(
        CreateFileW=FakeFunction((invalid_handle, invalid_handle, invalid_handle))
    )

    result = eject._eject_with_storage_ioctl(
        "D:", kernel32=kernel32, get_last_error=lambda: next(errors)
    )

    assert result == eject.EjectMethodResult(
        accepted=False,
        failures=(
            eject.EjectFailure("storage", "open-read-write", 5),
            eject.EjectFailure("storage", "open-read-only", 32),
            eject.EjectFailure("storage", "open-metadata-only", 21),
        ),
    )


def test_mci_eject_retains_safe_open_failure_code():
    class FakeFunction:
        def __call__(self, *_args):
            return 275

    result = eject._eject_with_mci(
        "D:", winmm=SimpleNamespace(mciSendStringW=FakeFunction())
    )

    assert result == _rejected("mci", "open", 275)


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
    cached = watcher.snapshot().drives[0]
    assert cached.has_disc is False
    assert cached.disc_label is None
    assert cached.current_job_id is None
    assert cached.current_disc_fingerprint is None


def test_eject_endpoint_rejects_windows_only_provisional_drive(tmp_path, monkeypatch):
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: CommandResult(
            command=("makemkvcon64.exe", "info", "disc:9999"),
            return_code=0,
            stdout="",
            stderr="",
            started_at="2026-08-01T00:00:00+00:00",
            finished_at="2026-08-01T00:00:01+00:00",
        ),
        native_discovery=lambda: ("D:",),
        native_media_discovery=lambda: {"D:": (True, "disc")},
    )
    with pytest.raises(PreflightError, match="provisional"):
        watcher.refresh(tmp_path / "makemkvcon64.exe")
    monkeypatch.setattr(
        rip,
        "eject_optical_drive",
        lambda _device_name: pytest.fail("a provisional drive must not be ejected"),
    )

    with pytest.raises(rip.HTTPException) as response:
        rip.eject_drive(
            0,
            rip.EjectDriveRequest(confirm_eject=True),
            OrchestrationStore(tmp_path / "jobs.sqlite3"),
            lambda *_args, **_kwargs: pytest.fail(
                "a provisional drive must not be inventoried"
            ),
            watcher,
            rip.RipExecutionRegistry(),
        )

    assert response.value.status_code == 409
    assert "MakeMKV has not confirmed" in response.value.detail


def test_eject_endpoint_returns_safe_windows_diagnostics(tmp_path, monkeypatch):
    watcher = DriveWatcher(lambda *_args, **_kwargs: _drive_result("disc:9999"))
    watcher.refresh(tmp_path / "makemkvcon64.exe")

    def fail(_device_name: str) -> None:
        raise eject.DiscEjectError(
            "Optical tray could not be ejected. Windows diagnostics: "
            "storage/device-control-read-write=error-32; mci/open=error-275"
        )

    monkeypatch.setattr(rip, "eject_optical_drive", fail)

    with pytest.raises(rip.HTTPException) as response:
        rip.eject_drive(
            0,
            rip.EjectDriveRequest(confirm_eject=True),
            OrchestrationStore(tmp_path / "jobs.sqlite3"),
            lambda *_args, **_kwargs: pytest.fail("primed eject must not rescan"),
            watcher,
            rip.RipExecutionRegistry(),
        )

    assert response.value.status_code == 409
    assert response.value.detail.endswith(
        "storage/device-control-read-write=error-32; mci/open=error-275"
    )
    assert "D:" not in response.value.detail


def test_automatic_eject_reports_discovery_conflict_as_retryable(tmp_path, monkeypatch):
    watcher = DriveWatcher(lambda *_args, **_kwargs: _drive_result("disc:9999"))
    watcher.refresh(tmp_path / "makemkvcon64.exe")
    registry = RipExecutionRegistry()
    discovery = registry.claim_all_drive_discovery()
    monkeypatch.setattr(
        rip,
        "eject_optical_drive",
        lambda _device_name: pytest.fail("a blocked eject must not touch the tray"),
    )

    try:
        with pytest.raises(rip.HTTPException) as response:
            rip.eject_drive(
                0,
                rip.EjectDriveRequest(
                    confirm_eject=True,
                    automatic_completion=True,
                ),
                OrchestrationStore(tmp_path / "jobs.sqlite3"),
                lambda *_args, **_kwargs: pytest.fail("a blocked eject must not scan"),
                watcher,
                registry,
            )
    finally:
        discovery.release()

    assert response.value.status_code == 409
    assert response.value.detail == {
        "code": "all_drive_discovery_active",
        "message": (
            "Read-only all-drive discovery is active; eject verification was deferred"
        ),
        "retryable": True,
        "retry_after_seconds": 5,
    }


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
        RipExecutionRegistry(),
        DriveWatcher(),
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
        RipExecutionRegistry(),
        DriveWatcher(),
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
        RipExecutionRegistry(),
        DriveWatcher(),
    )

    assert ejected == ["D:"]


def test_automatic_eject_skips_drive_claimed_by_active_optical_work(
    tmp_path, monkeypatch
):
    ejected = []
    monkeypatch.setattr(rip, "eject_optical_drive", ejected.append)
    completed = SimpleNamespace(
        job_id="completed-job",
        preview={"drives": [{"drive_index": 0}]},
    )
    store = SimpleNamespace(list_jobs=lambda: [completed])
    registry = RipExecutionRegistry()
    registry.attach("active-job", tmp_path / "active-run", frozenset({0}))

    rip._auto_eject_completed_job_drives(
        completed,
        store,
        lambda *_args, **_kwargs: pytest.fail("drive must not be read"),
        tmp_path / "makemkvcon64.exe",
        600,
        registry,
        DriveWatcher(),
    )

    assert ejected == []
