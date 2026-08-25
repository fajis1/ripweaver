from pathlib import Path

import pytest

from mkv_episode_matcher.backend.rip_runtime import (
    AllDriveDiscoveryDeferredError,
    AllDriveDiscoveryInProgressError,
    RipExecutionRegistry,
)
from mkv_episode_matcher.disc.ripper import RipError


def test_registry_allows_parallel_different_drives(tmp_path: Path):
    registry = RipExecutionRegistry()

    registry.attach("rip-one", tmp_path / "one", frozenset({2}))
    registry.attach("rip-two", tmp_path / "two", frozenset({4}))

    assert registry.active_drive_indexes() == (2, 4)


def test_registry_refuses_two_jobs_for_same_physical_drive(tmp_path: Path):
    registry = RipExecutionRegistry()
    registry.attach("rip-one", tmp_path / "one", frozenset({2}))

    with pytest.raises(
        RipError,
        match="MakeMKV rip is already active on optical drive 3; a second MakeMKV rip was not started",
    ):
        registry.attach("rip-two", tmp_path / "two", frozenset({2}))

    assert registry.is_job_active("rip-two") is False
    assert registry.active_drive_indexes() == (2,)


def test_registry_releases_drive_claim_on_detach(tmp_path: Path):
    registry = RipExecutionRegistry()
    registry.attach("rip-one", tmp_path / "one", frozenset({2}))
    registry.detach("rip-one")

    registry.attach("rip-two", tmp_path / "two", frozenset({2}))

    assert registry.is_job_active("rip-two") is True


def test_all_drive_discovery_is_deferred_while_any_rip_is_active(tmp_path: Path):
    registry = RipExecutionRegistry()
    registry.attach("rip-one", tmp_path / "one", frozenset({2}))

    with pytest.raises(AllDriveDiscoveryDeferredError, match="deferred"):
        registry.claim_all_drive_discovery()

    assert registry.all_drive_discovery_active() is False


def test_all_drive_discovery_is_deferred_during_drive_preparation():
    registry = RipExecutionRegistry()
    preparation = registry.claim_drive_preparation(2)
    try:
        with pytest.raises(AllDriveDiscoveryDeferredError, match="deferred"):
            registry.claim_all_drive_discovery()
        assert registry.busy_drive_indexes() == (2,)
    finally:
        preparation.release()


def test_registry_reports_exact_safe_drive_operation(tmp_path: Path):
    registry = RipExecutionRegistry()
    preparation = registry.claim_drive_preparation(
        2,
        operation="manual eject check",
    )
    try:
        assert registry.physical_drive_operations() == {2: "manual eject check"}
        with pytest.raises(
            RipError,
            match=(
                "Manual eject check is already active on optical drive 3; "
                "a second MakeMKV rip was not started"
            ),
        ):
            registry.attach("rip-one", tmp_path / "one", frozenset({2}))
    finally:
        preparation.release()

    registry.attach("rip-one", tmp_path / "one", frozenset({2}))
    assert registry.physical_drive_operations() == {2: "MakeMKV rip"}


def test_execution_inventories_promote_atomically_to_one_rip_claim(tmp_path: Path):
    registry = RipExecutionRegistry()
    leases = tuple(
        registry.claim_drive_preparation(
            drive_index,
            operation="execution inventory",
        )
        for drive_index in (2, 4)
    )

    registry.promote_drive_preparations(
        "rip-one",
        tmp_path / "run",
        leases,
    )

    assert registry.active_drive_indexes() == (2, 4)
    assert registry.physical_drive_operations() == {
        2: "MakeMKV rip",
        4: "MakeMKV rip",
    }
    for lease in leases:
        lease.release()
    assert registry.active_drive_indexes() == (2, 4)

    registry.detach("rip-one")
    assert registry.busy_drive_indexes() == ()


def test_execution_inventory_promotion_refuses_a_changed_lease(tmp_path: Path):
    registry = RipExecutionRegistry()
    lease = registry.claim_drive_preparation(
        2,
        operation="execution inventory",
    )
    lease.release()

    with pytest.raises(RipError, match="changed before rip"):
        registry.promote_drive_preparations(
            "rip-one",
            tmp_path / "run",
            (lease,),
        )

    assert registry.has_active_optical_work() is False


def test_all_drive_discovery_atomically_blocks_new_physical_work(tmp_path: Path):
    registry = RipExecutionRegistry()
    discovery = registry.claim_all_drive_discovery()
    try:
        with pytest.raises(AllDriveDiscoveryInProgressError, match="already active"):
            registry.claim_all_drive_discovery()
        with pytest.raises(RipError, match="all-drive discovery is active"):
            registry.claim_drive_preparation(2)
        with pytest.raises(RipError, match="all-drive discovery is active"):
            registry.attach("rip-one", tmp_path / "one", frozenset({2}))
    finally:
        discovery.release()

    registry.attach("rip-one", tmp_path / "one", frozenset({2}))
    assert registry.active_drive_indexes() == (2,)
