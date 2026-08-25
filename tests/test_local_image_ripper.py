from datetime import datetime
from pathlib import Path

import pytest

from mkv_episode_matcher.core.datetime_compat import UTC
from mkv_episode_matcher.disc.image_acquisition import VerifiedLocalSource
from mkv_episode_matcher.disc.local_image_ripper import run_local_image_titles
from mkv_episode_matcher.disc.ripper import (
    RipError,
    RipJob,
    RipResult,
    build_rip_command,
)


def test_local_command_refuses_physical_source(tmp_path: Path):
    job = RipJob("job-1", 0, 1, "staging/one")
    with pytest.raises(RipError, match="ISO or file"):
        build_rip_command(
            Path("makemkvcon.exe"),
            job,
            tmp_path,
            source_specifier="disc:0",
        )


def test_local_titles_are_sequential_and_use_only_iso(tmp_path: Path):
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"x" * 2048)
    source = VerifiedLocalSource("acq", iso, f"iso:{iso.resolve()}", 2048)
    output = tmp_path / "output"
    output.mkdir()
    observed = []

    def runner(_exe, _root, job, _log, **kwargs):
        observed.append((job.job_id, kwargs["source_specifier"]))
        now = datetime.now(UTC).isoformat()
        return RipResult(job.job_id, 0, 1, 1, 0, now, now)

    jobs = (
        RipJob("job-1", 0, 1, "staging/one"),
        RipJob("job-2", 0, 2, "staging/two"),
    )
    results = run_local_image_titles(
        tmp_path / "makemkvcon.exe",
        output,
        source,
        jobs,
        tmp_path / "run",
        title_runner=runner,
    )
    assert [item.job_id for item in results] == ["job-1", "job-2"]
    assert observed == [
        ("job-1", f"iso:{iso.resolve()}"),
        ("job-2", f"iso:{iso.resolve()}"),
    ]
