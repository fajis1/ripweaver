from pathlib import Path

import pytest

from mkv_episode_matcher.backend.rip_runtime import RipExecutionRegistry
from mkv_episode_matcher.disc.ripper import RipError


def test_registry_allows_parallel_different_drives(tmp_path: Path):
    registry = RipExecutionRegistry()

    registry.attach("rip-one", tmp_path / "one", frozenset({2}))
    registry.attach("rip-two", tmp_path / "two", frozenset({4}))

    assert registry.active_drive_indexes() == (2, 4)


def test_registry_refuses_two_jobs_for_same_physical_drive(tmp_path: Path):
    registry = RipExecutionRegistry()
    registry.attach("rip-one", tmp_path / "one", frozenset({2}))

    with pytest.raises(RipError, match="optical drive 3"):
        registry.attach("rip-two", tmp_path / "two", frozenset({2}))

    assert registry.is_job_active("rip-two") is False
    assert registry.active_drive_indexes() == (2,)


def test_registry_releases_drive_claim_on_detach(tmp_path: Path):
    registry = RipExecutionRegistry()
    registry.attach("rip-one", tmp_path / "one", frozenset({2}))
    registry.detach("rip-one")

    registry.attach("rip-two", tmp_path / "two", frozenset({2}))

    assert registry.is_job_active("rip-two") is True
