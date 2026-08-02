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

    watcher = DriveWatcher(runner)
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


def test_snapshot_does_not_invoke_discovery():
    def runner(*args, **kwargs):
        raise AssertionError("snapshot must not access hardware")

    snapshot = DriveWatcher(runner).snapshot()

    assert snapshot.status == "not_scanned"
    assert snapshot.drives == ()


def test_failed_refresh_preserves_last_known_slots():
    results = iter([
        _result('DRV:0,2,999,1,"hardware","disc","D:"\n'),
        _result(""),
    ])
    watcher = DriveWatcher(lambda *args, **kwargs: next(results))
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
    watcher = DriveWatcher(lambda *args, **kwargs: next(results))

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


@pytest.mark.parametrize("timeout", [0, 4, 121])
def test_refresh_rejects_unsafe_timeout(timeout):
    watcher = DriveWatcher(lambda *args, **kwargs: _result(""))

    with pytest.raises(PreflightError, match="timeout"):
        watcher.refresh(Path("makemkvcon64.exe"), timeout_seconds=timeout)
