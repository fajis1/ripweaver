from pathlib import Path

import pytest

from mkv_episode_matcher.disc.failed_rip_cleanup import (
    apply_failed_rip_cleanup,
    plan_failed_rip_cleanup,
)
from mkv_episode_matcher.disc.ripper import RipError, RipJob


def _job() -> RipJob:
    return RipJob(
        job_id="disc-01-title-000",
        drive_index=0,
        title_index=0,
        relative_output_dir=".staging/disc-01/new/0123456789abcdef/title-000",
        output_basename="disc-01-0123456789abcdef-title-000.mkv",
        final_relative_dir="TV Shows/Series/Season 01",
    )


def test_cleanup_requires_unchanged_exact_plan(tmp_path: Path):
    failed = tmp_path / ".staging/disc-04/old/0123456789abcdef/title-000"
    failed.mkdir(parents=True)
    partial = failed / "title_t00.mkv"
    partial.write_bytes(b"partial")

    plan = plan_failed_rip_cleanup(tmp_path, (_job(),))
    assert plan.file_count == 1
    assert plan.total_bytes == 7

    applied = apply_failed_rip_cleanup(
        tmp_path, (_job(),), expected_plan_sha256=plan.plan_sha256
    )
    assert applied == plan
    assert not failed.exists()


def test_cleanup_refuses_changed_plan_and_preserves_partial(tmp_path: Path):
    failed = tmp_path / ".staging/disc-01/old/0123456789abcdef/title-000"
    failed.mkdir(parents=True)
    partial = failed / "title_t00.mkv"
    partial.write_bytes(b"partial")
    plan = plan_failed_rip_cleanup(tmp_path, (_job(),))
    partial.write_bytes(b"changed")

    with pytest.raises(RipError, match="changed"):
        apply_failed_rip_cleanup(
            tmp_path, (_job(),), expected_plan_sha256=plan.plan_sha256
        )
    assert partial.read_bytes() == b"changed"


def test_cleanup_refuses_unknown_files(tmp_path: Path):
    failed = tmp_path / ".staging/disc-01/old/0123456789abcdef/title-000"
    failed.mkdir(parents=True)
    (failed / "notes.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(RipError, match="unknown"):
        plan_failed_rip_cleanup(tmp_path, (_job(),))
    assert (failed / "notes.txt").is_file()


def test_verified_final_prevents_partial_cleanup(tmp_path: Path):
    failed = tmp_path / ".staging/disc-01/old/0123456789abcdef/title-000"
    failed.mkdir(parents=True)
    (failed / "title_t00.mkv").write_bytes(b"diagnostic")
    final = (
        tmp_path / "TV Shows/Series/Season 01/disc-01-0123456789abcdef-title-000.mkv"
    )
    final.parent.mkdir(parents=True)
    final.write_bytes(b"verified")

    plan = plan_failed_rip_cleanup(tmp_path, (_job(),))
    assert plan.file_count == 0
    assert failed.is_dir()
