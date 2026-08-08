from pathlib import Path

import pytest

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

    watcher.clear_current_job(
        0, expected_disc_fingerprint="0123456789abcdef"
    )

    drive = watcher.snapshot().drives[0]
    assert drive.current_job_id is None
    assert drive.current_disc_fingerprint is None
    assert drive.has_disc is True


def test_snapshot_does_not_invoke_discovery():
    def runner(*args, **kwargs):
        raise AssertionError("snapshot must not access hardware")

    snapshot = DriveWatcher(runner, native_discovery=lambda: ()).snapshot()

    assert snapshot.status == "not_scanned"
    assert snapshot.drives == ()


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
