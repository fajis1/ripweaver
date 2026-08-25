from pathlib import Path

import pytest

from mkv_episode_matcher.disc.image_acquisition import plan_disc_image
from mkv_episode_matcher.disc.image_acquisition_store import ImageAcquisitionStore
from mkv_episode_matcher.disc.ripper import RipError


def test_store_is_idempotent_and_path_redacted(tmp_path: Path):
    store = ImageAcquisitionStore(tmp_path / "control.sqlite3")
    plan = plan_disc_image(drive_index=2, media_kind="dvd", estimated_bytes=2048)
    first = store.create(plan, idempotency_key="create-1")
    second = store.create(plan, idempotency_key="create-1")
    assert first == second
    serialized = str(first.plan)
    assert "DiscImageCreator" not in serialized
    assert ":\\" not in serialized


def test_exact_digest_and_ordered_transitions(tmp_path: Path):
    store = ImageAcquisitionStore(tmp_path / "control.sqlite3")
    job = store.create(
        plan_disc_image(drive_index=0, media_kind="bluray", estimated_bytes=4096),
        idempotency_key="create",
    )
    with pytest.raises(RipError, match="digest"):
        store.transition(
            job.job_id,
            "authorized",
            idempotency_key="authorize",
            expected_plan_sha256="0" * 64,
        )
    authorized = store.transition(
        job.job_id,
        "authorized",
        idempotency_key="authorize",
        expected_plan_sha256=job.plan_sha256,
    )
    assert authorized.state == "authorized"
    assert (
        store.transition(
            job.job_id,
            "queued",
            idempotency_key="queue",
            expected_plan_sha256=job.plan_sha256,
        ).state
        == "queued"
    )
    assert store.events(job.job_id) == ("created", "authorized", "queued")


def test_invalid_transition_fails_closed(tmp_path: Path):
    store = ImageAcquisitionStore(tmp_path / "control.sqlite3")
    job = store.create(
        plan_disc_image(drive_index=0, media_kind="dvd", estimated_bytes=2048),
        idempotency_key="create",
    )
    with pytest.raises(RipError, match="transition"):
        store.transition(
            job.job_id,
            "running",
            idempotency_key="run",
            expected_plan_sha256=job.plan_sha256,
        )
