from pathlib import Path

import pytest

from mkv_episode_matcher.disc.image_acquisition_bindings import (
    PrivateAcquisitionBindingStore,
)
from mkv_episode_matcher.disc.ripper import RipError


def test_private_binding_is_immutable(tmp_path: Path):
    executable = tmp_path / "tool.exe"
    executable.write_bytes(b"tool")
    root = tmp_path / "images"
    root.mkdir()
    store = PrivateAcquisitionBindingStore(tmp_path / "private.sqlite3")
    first = store.bind(
        job_id="acq-1",
        plan_sha256="a" * 64,
        executable=executable,
        image_root=root,
        drive_letter="e:",
    )
    assert first.drive_letter == "E"
    assert (
        store.bind(
            job_id="acq-1",
            plan_sha256="a" * 64,
            executable=executable,
            image_root=root,
            drive_letter="E",
        )
        == first
    )
    with pytest.raises(RipError, match="immutable"):
        store.bind(
            job_id="acq-1",
            plan_sha256="b" * 64,
            executable=executable,
            image_root=root,
            drive_letter="E",
        )
