import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mkv_episode_matcher.backend.routers.rip import (
    DriveMappingDecision,
    DriveMappingPlanRequest,
    DriveMappingRequest,
    save_drive_mapping_plan,
    update_drive_mapping,
)
from mkv_episode_matcher.disc import drive_mapping
from mkv_episode_matcher.disc.drive_mapping import (
    DriveMappingError,
    DriveMappingStore,
    NativeOpticalDevice,
    discover_windows_optical_devices,
    parse_windows_optical_devices,
)
from mkv_episode_matcher.disc.drive_watcher import DriveWatcher
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.preflight import CommandResult


def _result(output: str) -> CommandResult:
    return CommandResult(
        command=("makemkvcon64.exe", "info", "disc:9999"),
        return_code=0,
        stdout=output,
        stderr="",
        started_at="2026-08-10T00:00:00+00:00",
        finished_at="2026-08-10T00:00:01+00:00",
    )


def _native(letter: str, key_character: str, name: str) -> NativeOpticalDevice:
    return NativeOpticalDevice(
        device_name=letter,
        device_key=key_character * 64,
        display_name=name,
        connection_type="usb" if "USB" in name else "sata",
    )


def test_windows_identity_parser_redacts_generic_usb_identifier():
    payload = json.dumps([
        {
            "device_name": "J:",
            "device_key": "a" * 64,
            "display_name": "000000 9999999999999999 USB Device",
            "connection_type": "usb",
        }
    ])

    devices = parse_windows_optical_devices(payload)

    assert devices == (
        NativeOpticalDevice(
            device_name="J:",
            device_key="a" * 64,
            display_name="Generic USB optical device",
            connection_type="usb",
        ),
    )
    assert "9999999999999999" not in devices[0].display_name


def test_windows_identity_parser_refuses_ambiguous_hardware_keys():
    payload = json.dumps([
        {
            "device_name": "D:",
            "device_key": "a" * 64,
            "display_name": "Drive one",
            "connection_type": "sata",
        },
        {
            "device_name": "F:",
            "device_key": "a" * 64,
            "display_name": "Drive two",
            "connection_type": "sata",
        },
    ])

    with pytest.raises(DriveMappingError, match="ambiguous"):
        parse_windows_optical_devices(payload)


def test_windows_identity_discovery_allows_slow_multi_drive_metadata(monkeypatch):
    observed: dict[str, int] = {}

    def run(*_args, **kwargs):
        observed["timeout_seconds"] = kwargs["timeout"]
        return SimpleNamespace(returncode=0, stdout="[]")

    monkeypatch.setattr(drive_mapping.sys, "platform", "win32")
    monkeypatch.setattr(drive_mapping.subprocess, "run", run)

    assert discover_windows_optical_devices() == ()
    assert observed == {"timeout_seconds": 30}


def test_drive_mapping_store_persists_only_hashed_decisions(tmp_path):
    path = tmp_path / "drive-map.json"
    store = DriveMappingStore(path)

    assert store.status("a" * 64) == "unmapped"
    store.set_status("a" * 64, "trusted")
    store.set_status("b" * 64, "ignored")

    assert DriveMappingStore(path).status("a" * 64) == "trusted"
    assert DriveMappingStore(path).status("b" * 64) == "ignored"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 2,
        "devices": {
            "a" * 64: {
                "connection_type": None,
                "display_name": None,
                "status": "trusted",
            },
            "b" * 64: {
                "connection_type": None,
                "display_name": None,
                "status": "ignored",
            },
        },
    }


def test_usb_identity_change_is_blocked_and_old_trust_is_retired(tmp_path):
    path = tmp_path / "drive-map.json"
    store = DriveMappingStore(path)
    old = _native("J:", "a", "Generic USB optical device")
    current = _native("J:", "b", "Generic USB optical device")
    store.set_status(
        old.device_key,
        "trusted",
        display_name=old.display_name,
        connection_type=old.connection_type,
    )
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: _result('DRV:4,2,999,0,"private hardware","","J:"\n'),
        native_discovery=lambda: ("J:",),
        native_identity_discovery=lambda: (current,),
        mapping_store=store,
    )

    snapshot = watcher.refresh(Path("makemkvcon64.exe"))

    assert snapshot.drives[0].mapping_status == "unmapped"
    assert snapshot.drives[0].mapping_warning == "possible_identity_change"
    assert snapshot.drives[0].prior_similar_mapping_count == 1
    plan_sha256 = watcher.mapping_plan_sha256()
    assert plan_sha256 is not None

    updated, retired = watcher.apply_mapping_plan(
        plan_sha256,
        {current.mapping_id: "trusted"},
    )

    assert retired == 1
    assert updated.drives[0].mapping_status == "trusted"
    assert store.status(old.device_key) == "unmapped"
    assert store.status(current.device_key) == "trusted"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["devices"][old.device_key]["status"] == "retired"


def test_mapping_plan_refuses_a_changed_exact_snapshot(tmp_path):
    first = _native("D:", "a", "Drive one")
    second = _native("F:", "b", "Drive two")
    identities = iter([(first,), (first, second)])
    results = iter([
        _result('DRV:0,2,999,0,"one","","D:"\n'),
        _result('DRV:0,2,999,0,"one","","D:"\nDRV:1,2,999,0,"two","","F:"\n'),
    ])
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: next(results),
        native_discovery=lambda: (),
        native_identity_discovery=lambda: next(identities),
        mapping_store=DriveMappingStore(tmp_path / "drive-map.json"),
    )
    watcher.refresh(Path("makemkvcon64.exe"))
    stale_digest = watcher.mapping_plan_sha256()
    watcher.refresh(Path("makemkvcon64.exe"))

    with pytest.raises(DriveMappingError, match="changed"):
        watcher.apply_mapping_plan(
            stale_digest or "",
            {first.mapping_id: "trusted", second.mapping_id: "trusted"},
        )


def test_watcher_blocks_unmapped_devices_and_follows_identity_across_index_change(
    tmp_path,
):
    results = iter([
        _result('DRV:1,2,999,1,"private hardware","same disc","D:"\n'),
        _result('DRV:4,2,999,1,"private hardware","same disc","D:"\n'),
    ])
    native = _native("D:", "a", "Trusted SATA drive")
    store = DriveMappingStore(tmp_path / "drive-map.json")
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: next(results),
        native_discovery=lambda: ("D:",),
        native_identity_discovery=lambda: (native,),
        mapping_store=store,
    )

    first = watcher.refresh(Path("makemkvcon64.exe"))
    assert first.drives[0].drive_index == 1
    assert first.drives[0].mapping_status == "unmapped"
    with pytest.raises(ValueError, match="untrusted"):
        watcher.bind_current_job(
            1,
            "rip-0123456789abcdef0123456789abcdef",
            "fedcba9876543210",
        )

    watcher.set_mapping_status(native.mapping_id, "trusted")
    watcher.bind_current_job(
        1,
        "rip-0123456789abcdef0123456789abcdef",
        "fedcba9876543210",
    )
    second = watcher.refresh(Path("makemkvcon64.exe"))

    assert [(item.drive_index, item.mapping_status) for item in second.drives] == [
        (4, "trusted")
    ]
    assert second.drives[0].current_job_id == "rip-0123456789abcdef0123456789abcdef"
    assert watcher.device_name(1) is None
    assert watcher.device_name(4) == "D:"


def test_authoritative_native_inventory_removes_disconnected_cached_slot(tmp_path):
    results = iter([
        _result(
            'DRV:0,2,999,1,"hardware one","","D:"\n'
            'DRV:1,2,999,1,"hardware two","","F:"\n'
        ),
        _result('DRV:0,2,999,1,"hardware one","","D:"\n'),
    ])
    identities = iter([
        (
            _native("D:", "a", "Drive one"),
            _native("F:", "b", "Drive two"),
        ),
        (_native("D:", "a", "Drive one"),),
    ])
    native_names = iter([("D:", "F:"), ("D:",)])
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: next(results),
        native_discovery=lambda: next(native_names),
        native_identity_discovery=lambda: next(identities),
        mapping_store=DriveMappingStore(tmp_path / "drive-map.json"),
    )

    assert len(watcher.refresh(Path("makemkvcon64.exe")).drives) == 2
    refreshed = watcher.refresh(Path("makemkvcon64.exe"))

    assert [drive.drive_index for drive in refreshed.drives] == [0]
    assert watcher.device_name(1) is None


def test_mapping_api_requires_confirmation_and_updates_only_private_decision(tmp_path):
    native = _native("D:", "a", "Reviewed drive")
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: _result('DRV:0,2,999,1,"private hardware","","D:"\n'),
        native_discovery=lambda: ("D:",),
        native_identity_discovery=lambda: (native,),
        mapping_store=DriveMappingStore(tmp_path / "drive-map.json"),
    )
    watcher.refresh(Path("makemkvcon64.exe"))
    jobs = OrchestrationStore(tmp_path / "jobs.sqlite3")

    with pytest.raises(HTTPException) as unconfirmed:
        update_drive_mapping(
            native.mapping_id,
            DriveMappingRequest(status="trusted"),
            watcher,
            jobs,
        )
    assert unconfirmed.value.status_code == 400

    response = update_drive_mapping(
        native.mapping_id,
        DriveMappingRequest(status="trusted", confirm_mapping=True),
        watcher,
        jobs,
    )

    assert response["drives"][0]["mapping_status"] == "trusted"
    assert DriveMappingStore(tmp_path / "drive-map.json").status(native.device_key) == (
        "trusted"
    )


def test_mapping_wizard_saves_every_current_device_in_one_plan(tmp_path):
    first = _native("D:", "a", "Reviewed drive")
    second = _native("F:", "b", "USB reviewed drive")
    watcher = DriveWatcher(
        lambda *_args, **_kwargs: _result(
            'DRV:1,2,999,0,"one","","D:"\nDRV:4,2,999,0,"two","","F:"\n'
        ),
        native_discovery=lambda: ("D:", "F:"),
        native_identity_discovery=lambda: (first, second),
        mapping_store=DriveMappingStore(tmp_path / "drive-map.json"),
    )
    watcher.refresh(Path("makemkvcon64.exe"))
    plan_sha256 = watcher.mapping_plan_sha256()
    assert plan_sha256 is not None

    response = save_drive_mapping_plan(
        DriveMappingPlanRequest(
            expected_mapping_plan_sha256=plan_sha256,
            mappings=[
                DriveMappingDecision(mapping_id=first.mapping_id, status="trusted"),
                DriveMappingDecision(mapping_id=second.mapping_id, status="trusted"),
            ],
            confirm_mapping=True,
        ),
        watcher,
        OrchestrationStore(tmp_path / "jobs.sqlite3"),
    )

    assert response["mapping_summary"] == {
        "trusted": 2,
        "ignored": 0,
        "unmapped": 0,
    }
    assert response["automatic_processing_requested"] is False
    assert all(drive["mapping_status"] == "trusted" for drive in response["drives"])


def test_transient_native_identity_failure_preserves_prior_trusted_mapping(tmp_path):
    native = _native("D:", "a", "Reviewed drive")
    mapping_store = DriveMappingStore(tmp_path / "drive-map.json")
    mapping_store.set_status(
        native.device_key,
        "trusted",
        display_name=native.display_name,
        connection_type=native.connection_type,
    )
    attempts = 0

    def identities():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return (native,)
        raise DriveMappingError("Windows optical-device discovery failed safely")

    watcher = DriveWatcher(
        lambda *_args, **_kwargs: _result(
            'DRV:0,2,999,1,"private hardware","disc","D:"\n'
        ),
        native_discovery=lambda: ("D:",),
        native_identity_discovery=identities,
        mapping_store=mapping_store,
    )

    assert watcher.refresh(Path("makemkvcon64.exe")).drives[0].mapping_status == (
        "trusted"
    )
    assert watcher.refresh(Path("makemkvcon64.exe")).drives[0].mapping_status == (
        "trusted"
    )
