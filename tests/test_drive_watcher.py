import threading
from pathlib import Path

import pytest

from mkv_episode_matcher.disc.drive_mapping import (
    DriveMappingStore,
    NativeOpticalDevice,
)
from mkv_episode_matcher.disc.drive_watcher import DriveWatcher
from mkv_episode_matcher.disc.preflight import CommandResult, PreflightError


def _result(stdout: str) -> CommandResult:
    return CommandResult(
        command=("makemkvcon64.exe", "info", "disc:9999"),
        return_code=0,
        stdout=stdout,
        stderr="",
        started_at="2026-07-31T00:00:00+00:00",
        finished_at="2026-07-31T00:00:01+00:00",
    )


def test_watcher_exposes_only_redacted_slot_state():
    calls = []

    def runner(executable, source, **kwargs):
        calls.append((executable, source, kwargs))
        return _result(
            'DRV:0,2,999,1,"private hardware","private disc","D:"\n'
            'DRV:1,2,999,1,"","","E:"\n'
            'DRV:14,2,999,0,"","",""\n'
            'DRV:15,0,0,0,"","",""\n'
        )

    watcher = DriveWatcher(runner, native_discovery=lambda: ())
    snapshot = watcher.refresh(Path("makemkvcon64.exe"), timeout_seconds=12)

    assert calls == [
        (
            Path("makemkvcon64.exe"),
            "disc:9999",
            {"timeout_seconds": 12},
        )
    ]
    assert [(drive.drive_index, drive.has_disc) for drive in snapshot.drives] == [
        (0, True),
        (1, False),
    ]
    assert snapshot.drives[0].disc_label == "private disc"
    assert snapshot.drives[1].disc_label is None
    assert "private hardware" not in repr(snapshot)
    assert "D:" not in repr(snapshot)
    assert watcher.device_name(0) == "D:"
    assert watcher.device_name(1) == "E:"
    assert watcher.device_name(2) is None


def test_clear_current_job_detaches_only_expected_disc_identity():
    watcher = DriveWatcher(
        lambda _executable, _source, **_kwargs: _result(
            'DRV:0,2,999,1,"private hardware","Test Disc","D:"\n'
        ),
        native_discovery=lambda: (),
    )
    watcher.refresh(Path("makemkvcon64.exe"))
    watcher.bind_current_job(
        0, "rip-0123456789abcdef0123456789abcdef", "0123456789abcdef"
    )

    watcher.clear_current_job(0, expected_disc_fingerprint="0123456789abcdef")

    drive = watcher.snapshot().drives[0]
    assert drive.current_job_id is None
    assert drive.current_disc_fingerprint is None
    assert drive.has_disc is True


def test_successful_eject_clears_identity_before_same_label_disc_arrives():
    watcher = DriveWatcher(
        lambda _executable, _source, **_kwargs: _result(
            'DRV:0,2,999,1,"private hardware","same label","D:"\n'
        ),
        native_discovery=lambda: (),
        native_media_discovery=lambda: {"D:": (True, "same label")},
    )
    watcher.refresh(Path("makemkvcon64.exe"))
    watcher.bind_current_job(
        0,
        "rip-0123456789abcdef0123456789abcdef",
        "0123456789abcdef",
    )

    ejected = watcher.record_successful_eject(0)

    empty = ejected.drives[0]
    assert empty.has_disc is False
    assert empty.disc_label is None
    assert empty.current_job_id is None
    assert empty.current_disc_fingerprint is None

    inserted = watcher.refresh_native_media().drives[0]
    assert inserted.has_disc is True
    assert inserted.disc_label == "same label"
    assert inserted.current_job_id is None
    assert inserted.current_disc_fingerprint is None


def test_snapshot_does_not_invoke_discovery():
    def runner(*args, **kwargs):
        raise AssertionError("snapshot must not access hardware")

    snapshot = DriveWatcher(runner, native_discovery=lambda: ()).snapshot()

    assert snapshot.status == "not_scanned"
    assert snapshot.drives == ()


def test_snapshot_remains_available_and_duplicate_refresh_is_rejected():
    started = threading.Event()
    release = threading.Event()

    def runner(*_args, **_kwargs):
        started.set()
        assert release.wait(timeout=2)
        return _result('DRV:0,2,999,1,"hardware","disc","D:"\n')

    watcher = DriveWatcher(runner, native_discovery=lambda: ())
    worker = threading.Thread(
        target=watcher.refresh,
        args=(Path("makemkvcon64.exe"),),
    )
    worker.start()
    assert started.wait(timeout=1)

    assert watcher.refresh_in_progress() is True
    assert watcher.snapshot().status == "not_scanned"
    with pytest.raises(PreflightError, match="already in progress"):
        watcher.refresh(Path("makemkvcon64.exe"))

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert watcher.refresh_in_progress() is False
    assert watcher.snapshot().status == "ready"


def test_failed_refresh_preserves_last_known_slots():
    results = iter([
        _result('DRV:0,2,999,1,"hardware","disc","D:"\n'),
        _result(""),
    ])
    watcher = DriveWatcher(
        lambda *args, **kwargs: next(results), native_discovery=lambda: ()
    )
    first = watcher.refresh(Path("makemkvcon64.exe"))

    with pytest.raises(PreflightError, match="no optical-drive records"):
        watcher.refresh(Path("makemkvcon64.exe"))

    failed = watcher.snapshot()
    assert failed.status == "error"
    assert failed.drives == first.drives
    assert failed.error_type == "PreflightError"
    assert failed.error_code == "no_drives"


def test_failed_refresh_classifies_timeout_for_actionable_recovery():
    def timeout_runner(*_args, **_kwargs):
        raise PreflightError("MakeMKV info timed out after 30s for disc:9999")

    watcher = DriveWatcher(timeout_runner, native_discovery=lambda: ())

    with pytest.raises(PreflightError, match="timed out"):
        watcher.refresh(Path("makemkvcon64.exe"))

    failed = watcher.snapshot()
    assert failed.status == "error"
    assert failed.error_type == "PreflightError"
    assert failed.error_code == "timeout"


def test_first_failed_refresh_keeps_native_windows_slots_visible():
    def timeout_runner(*_args, **_kwargs):
        raise PreflightError("MakeMKV info timed out after 30s for disc:9999")

    watcher = DriveWatcher(
        timeout_runner,
        native_discovery=lambda: ("D:", "F:", "H:", "I:"),
    )

    with pytest.raises(PreflightError, match="timed out"):
        watcher.refresh(Path("makemkvcon64.exe"))

    failed = watcher.snapshot()
    assert failed.status == "error"
    assert failed.error_code == "timeout"
    assert [drive.drive_index for drive in failed.drives] == [0, 1, 2, 3]
    assert all(drive.available for drive in failed.drives)
    assert all(not drive.has_disc for drive in failed.drives)
    assert all(not drive.makemkv_confirmed for drive in failed.drives)
    assert [watcher.device_name(index) for index in range(4)] == [
        "D:",
        "F:",
        "H:",
        "I:",
    ]


def test_first_failed_refresh_keeps_native_windows_identities(tmp_path):
    def timeout_runner(*_args, **_kwargs):
        raise PreflightError("MakeMKV info timed out after 30s for disc:9999")

    native = NativeOpticalDevice(
        device_name="D:",
        device_key="a" * 64,
        display_name="External optical drive",
        connection_type="USB",
    )
    watcher = DriveWatcher(
        timeout_runner,
        native_discovery=lambda: ("D:",),
        native_media_discovery=lambda: {"D:": (True, "Inserted disc")},
        native_identity_discovery=lambda: (native,),
        mapping_store=DriveMappingStore(tmp_path / "drive-map.json"),
    )

    with pytest.raises(PreflightError, match="timed out"):
        watcher.refresh(Path("makemkvcon64.exe"), timeout_seconds=30)

    drive = watcher.snapshot().drives[0]
    assert drive.mapping_id == native.mapping_id
    assert drive.display_name == "External optical drive"
    assert drive.connection_type == "USB"
    assert drive.mapping_status == "unmapped"
    assert drive.mapping_warning == "new_device"
    assert drive.has_disc is True
    assert drive.disc_label == "Inserted disc"
    assert drive.makemkv_confirmed is False
    assert watcher.mapping_plan_sha256() is not None


def test_native_media_populates_a_confirmed_noscan_slot():
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: _result('DRV:0,2,999,0,"hardware","","D:"\n'),
        native_discovery=lambda: ("D:",),
        native_media_discovery=lambda: {"D:": (True, "Inserted disc")},
    )

    drive = watcher.refresh(Path("makemkvcon64.exe")).drives[0]

    assert drive.has_disc is True
    assert drive.disc_label == "Inserted disc"
    assert drive.makemkv_confirmed is True


def test_blocked_native_media_does_not_starve_makemkv_slot_listing(tmp_path):
    media_started = threading.Event()
    release_media = threading.Event()
    runner_started = threading.Event()
    errors: list[Exception] = []
    native = NativeOpticalDevice(
        device_name="D:",
        device_key="b" * 64,
        display_name="External optical drive",
        connection_type="USB",
    )

    def native_media():
        media_started.set()
        release_media.wait(timeout=5)
        return {"D:": (True, "Inserted disc")}

    def runner(*_args, **_kwargs):
        runner_started.set()
        return _result('DRV:0,2,999,0,"hardware","","D:"\n')

    watcher = DriveWatcher(
        runner,
        native_discovery=lambda: ("D:",),
        native_media_discovery=native_media,
        native_identity_discovery=lambda: (native,),
        mapping_store=DriveMappingStore(tmp_path / "drive-map.json"),
    )

    def refresh() -> None:
        try:
            watcher.refresh(Path("makemkvcon64.exe"))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    refresh_worker = threading.Thread(target=refresh)
    refresh_worker.start()
    assert media_started.wait(timeout=1)
    provisional = watcher.snapshot().drives[0]
    assert provisional.mapping_id == native.mapping_id
    assert provisional.makemkv_confirmed is False
    assert runner_started.wait(timeout=2)
    release_media.set()
    refresh_worker.join(timeout=2)

    assert not refresh_worker.is_alive()
    assert errors == []
    assert watcher.snapshot().drives[0].makemkv_confirmed is True


def test_partial_refresh_preserves_previously_discovered_empty_slots():
    results = iter([
        _result(
            'DRV:0,2,999,1,"hardware","disc","D:"\n'
            'DRV:1,2,999,1,"","","E:"\n'
            'DRV:2,2,999,1,"","","F:"\n'
        ),
        _result('DRV:1,2,999,1,"","new disc","E:"\n'),
    ])
    watcher = DriveWatcher(
        lambda *args, **kwargs: next(results), native_discovery=lambda: ()
    )

    watcher.refresh(Path("makemkvcon64.exe"))
    refreshed = watcher.refresh(Path("makemkvcon64.exe"))

    assert [(drive.drive_index, drive.has_disc) for drive in refreshed.drives] == [
        (0, False),
        (1, True),
        (2, False),
    ]
    assert watcher.device_name(0) == "D:"
    assert watcher.device_name(1) == "E:"
    assert watcher.device_name(2) == "F:"


def test_refresh_preserves_bound_disc_when_makemkv_temporarily_omits_drive():
    results = iter([
        _result(
            'DRV:0,2,999,1,"hardware","","D:"\n'
            'DRV:1,2,999,1,"hardware","same disc","E:"\n'
        ),
        _result('DRV:0,2,999,1,"hardware","new disc","D:"\n'),
    ])
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: next(results),
        native_discovery=lambda: ("D:", "E:"),
    )
    watcher.refresh(Path("makemkvcon64.exe"))
    watcher.bind_current_job(
        1,
        "rip-0123456789abcdef0123456789abcdef",
        "0123456789abcdef",
    )

    refreshed = watcher.refresh(Path("makemkvcon64.exe"))

    omitted = next(drive for drive in refreshed.drives if drive.drive_index == 1)
    assert omitted.available is True
    assert omitted.has_disc is True
    assert omitted.disc_label == "same disc"
    assert omitted.current_job_id == "rip-0123456789abcdef0123456789abcdef"
    assert omitted.current_disc_fingerprint == "0123456789abcdef"
    assert omitted.makemkv_confirmed is True


def test_refresh_retains_temporarily_missing_known_drive_as_unavailable():
    results = iter([
        _result(
            'DRV:0,2,999,1,"hardware","","D:"\n'
            'DRV:1,2,999,1,"hardware","same disc","E:"\n'
        ),
        _result('DRV:0,2,999,1,"hardware","new disc","D:"\n'),
    ])
    native_names = iter([("D:", "E:"), ("D:",)])
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: next(results),
        native_discovery=lambda: next(native_names),
    )
    watcher.refresh(Path("makemkvcon64.exe"))
    watcher.bind_current_job(
        1,
        "rip-0123456789abcdef0123456789abcdef",
        "0123456789abcdef",
    )

    refreshed = watcher.refresh(Path("makemkvcon64.exe"))

    omitted = next(drive for drive in refreshed.drives if drive.drive_index == 1)
    assert omitted.available is False
    assert omitted.has_disc is True
    assert omitted.current_job_id == "rip-0123456789abcdef0123456789abcdef"
    assert omitted.current_disc_fingerprint == "0123456789abcdef"


def test_native_windows_inventory_adds_makemkv_omitted_empty_trays():
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: _result(
            'DRV:1,2,999,1,"hardware","disc one","D:"\n'
            'DRV:2,2,999,1,"hardware","disc two","F:"\n'
            'DRV:3,2,999,0,"hardware","","H:"\n'
        ),
        native_discovery=lambda: ("D:", "F:", "H:", "I:", "J:"),
    )

    snapshot = watcher.refresh(Path("makemkvcon64.exe"))

    assert [(drive.drive_index, drive.has_disc) for drive in snapshot.drives] == [
        (1, True),
        (2, True),
        (3, False),
        (4, False),
        (5, False),
    ]
    assert watcher.device_name(4) == "I:"
    assert watcher.device_name(5) == "J:"
    assert snapshot.drives[0].makemkv_confirmed is True
    assert snapshot.drives[-2].makemkv_confirmed is False
    assert snapshot.drives[-1].makemkv_confirmed is False


def test_native_loaded_disc_does_not_disappear_when_makemkv_omits_usb_drive():
    results = iter([
        _result(
            'DRV:4,2,999,1,"usb hardware","same disc","J:"\n'
            'DRV:5,2,999,1,"usb hardware","","K:"\n'
        ),
        _result('DRV:5,2,999,1,"usb hardware","","K:"\n'),
    ])
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: next(results),
        native_discovery=lambda: ("J:", "K:"),
        native_media_discovery=lambda: {
            "J:": (True, "same disc"),
            "K:": (False, None),
        },
    )
    watcher.refresh(Path("makemkvcon64.exe"))

    refreshed = watcher.refresh(Path("makemkvcon64.exe"))

    drive = next(
        item
        for item in refreshed.drives
        if watcher.device_name(item.drive_index) == "J:"
    )
    assert drive.drive_index == 4
    assert drive.available is True
    assert drive.has_disc is True
    assert drive.disc_label == "same disc"
    assert drive.makemkv_confirmed is False


def test_windows_only_slot_cannot_bind_disc_work_before_makemkv_confirmation():
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: _result('DRV:0,2,999,0,"hardware","","D:"\n'),
        native_discovery=lambda: ("D:", "F:"),
        native_media_discovery=lambda: {"F:": (True, "disc")},
    )
    watcher.refresh(Path("makemkvcon64.exe"))
    refreshed = watcher.refresh_native_media()
    provisional = refreshed.drives[-1]
    assert provisional.has_disc is True
    assert provisional.makemkv_confirmed is False

    with pytest.raises(ValueError, match="untrusted, unconfirmed, or empty"):
        watcher.bind_current_job(
            provisional.drive_index,
            "rip-0123456789abcdef0123456789abcdef",
            "0123456789abcdef",
        )


def test_current_job_binding_is_cleared_when_tray_disc_changes():
    results = iter([
        _result('DRV:0,2,999,1,"hardware","disc one","D:"\n'),
        _result('DRV:0,2,999,1,"hardware","disc two","D:"\n'),
    ])
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: next(results), native_discovery=lambda: ()
    )
    watcher.refresh(Path("makemkvcon64.exe"))
    watcher.bind_current_job(
        0,
        "rip-0123456789abcdef0123456789abcdef",
        "fedcba9876543210",
    )

    assert watcher.snapshot().drives[0].current_job_id is not None
    assert watcher.snapshot().drives[0].current_disc_fingerprint == "fedcba9876543210"

    watcher.refresh(Path("makemkvcon64.exe"))

    assert watcher.snapshot().drives[0].current_job_id is None
    assert watcher.snapshot().drives[0].current_disc_fingerprint is None


def test_volume_change_invalidates_disc_identity_even_when_label_is_reused():
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: _result(
            'DRV:0,2,999,1,"hardware","same label","D:"\n'
        ),
        native_discovery=lambda: (),
    )
    watcher.refresh(Path("makemkvcon64.exe"))
    watcher.bind_current_job(
        0,
        "rip-0123456789abcdef0123456789abcdef",
        "fedcba9876543210",
    )

    watcher.invalidate_current_disc_bindings()
    watcher.refresh(Path("makemkvcon64.exe"))

    drive = watcher.snapshot().drives[0]
    assert drive.disc_label == "same label"
    assert drive.current_job_id is None
    assert drive.current_disc_fingerprint is None


@pytest.mark.parametrize("timeout", [0, 4, 121])
def test_refresh_rejects_unsafe_timeout(timeout):
    watcher = DriveWatcher(lambda *args, **kwargs: _result(""))

    with pytest.raises(PreflightError, match="timeout"):
        watcher.refresh(Path("makemkvcon64.exe"), timeout_seconds=timeout)
