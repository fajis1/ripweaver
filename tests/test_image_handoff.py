from pathlib import Path

import pytest

from mkv_episode_matcher.disc.image_acquisition import plan_disc_image
from mkv_episode_matcher.disc.image_acquisition_bindings import (
    PrivateAcquisitionBinding,
)
from mkv_episode_matcher.disc.image_acquisition_store import AcquisitionJob
from mkv_episode_matcher.disc.image_handoff import load_verified_local_source
from mkv_episode_matcher.disc.ripper import RipError


def test_handoff_revalidates_iso_and_never_returns_disc_source(tmp_path: Path):
    plan = plan_disc_image(drive_index=0, media_kind="dvd", estimated_bytes=2048)
    destination = tmp_path / plan.relative_destination
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"x" * 2048)
    job = AcquisitionJob("acq-1", "a" * 64, plan.to_dict(), "verified", "now", "now")
    binding = PrivateAcquisitionBinding(
        "acq-1", "a" * 64, tmp_path / "tool.exe", tmp_path, "E"
    )
    source = load_verified_local_source(job, binding)
    assert source.source_specifier.startswith("iso:")
    assert not source.source_specifier.startswith(("disc:", "dev:"))


def test_handoff_refuses_nonverified_job(tmp_path: Path):
    plan = plan_disc_image(drive_index=0, media_kind="dvd", estimated_bytes=2048)
    job = AcquisitionJob("acq-1", "a" * 64, plan.to_dict(), "running", "now", "now")
    binding = PrivateAcquisitionBinding(
        "acq-1", "a" * 64, tmp_path / "tool.exe", tmp_path, "E"
    )
    with pytest.raises(RipError, match="not verified"):
        load_verified_local_source(job, binding)
