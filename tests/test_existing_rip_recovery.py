from mkv_episode_matcher.disc.existing_rip_recovery import (
    discover_existing_rips,
    recovered_jobs,
)
from mkv_episode_matcher.disc.ripper import RipJob


def _job(index: int) -> RipJob:
    return RipJob(
        job_id=f"disc-01-title-{index:03d}",
        drive_index=0,
        title_index=index,
        relative_output_dir=f".staging/disc-01/new/title-{index:03d}",
        output_basename=f"disc-01-0123456789abcdef-title-{index:03d}.mkv",
    )


def test_recovery_discovers_exact_unique_basenames_without_exposing_parent(tmp_path):
    old = tmp_path / ".staging" / "disc-01" / "old-attempt" / "title-000"
    old.mkdir(parents=True)
    source = old / "disc-01-0123456789abcdef-title-000.mkv"
    source.write_bytes(b"synthetic-mkv")

    plan = discover_existing_rips(tmp_path, (_job(0), _job(1)))

    assert plan.public_dict()["candidates"] == [
        {
            "job_id": "disc-01-title-000",
            "title_index": 0,
            "basename": source.name,
            "size_bytes": len(b"synthetic-mkv"),
            "candidate_id": plan.candidates[0].candidate_id,
        }
    ]
    assert plan.missing_title_indexes == (1,)
    rebound = recovered_jobs((_job(0), _job(1)), plan)
    assert len(rebound) == 1
    assert rebound[0].relative_output_dir.endswith("old-attempt/title-000")
    assert rebound[0].final_relative_dir is None


def test_recovery_refuses_to_choose_between_duplicate_candidates(tmp_path):
    for attempt in ("one", "two"):
        parent = tmp_path / attempt
        parent.mkdir()
        (parent / "disc-01-0123456789abcdef-title-000.mkv").write_bytes(b"mkv")

    plan = discover_existing_rips(tmp_path, (_job(0),))

    assert len(plan.candidates) == 2
    assert len({candidate.candidate_id for candidate in plan.candidates}) == 2
    assert plan.ambiguous_title_indexes == (0,)


def test_recovery_accepts_changed_session_disc_ordinal(tmp_path):
    parent = tmp_path / "older-attempt"
    parent.mkdir()
    source = parent / "disc-04-0123456789abcdef-title-000.mkv"
    source.write_bytes(b"older-rip")

    plan = discover_existing_rips(tmp_path, (_job(0),))
    rebound = recovered_jobs((_job(0),), plan)

    assert [candidate.basename for candidate in plan.candidates] == [source.name]
    assert rebound[0].output_basename == source.name


def test_recovery_prefers_one_exact_current_ordinal_over_older_ordinals(tmp_path):
    current = tmp_path / "current"
    older = tmp_path / "older"
    current.mkdir()
    older.mkdir()
    expected = current / "disc-01-0123456789abcdef-title-000.mkv"
    expected.write_bytes(b"current-rip")
    (older / "disc-04-0123456789abcdef-title-000.mkv").write_bytes(b"older-rip")

    plan = discover_existing_rips(tmp_path, (_job(0),))

    assert [candidate.basename for candidate in plan.candidates] == [expected.name]
    assert plan.ambiguous_title_indexes == ()


def test_recovery_keeps_multiple_exact_current_ordinal_copies_ambiguous(tmp_path):
    for attempt in ("one", "two"):
        parent = tmp_path / attempt
        parent.mkdir()
        (parent / "disc-01-0123456789abcdef-title-000.mkv").write_bytes(b"mkv")

    plan = discover_existing_rips(tmp_path, (_job(0),))

    assert len(plan.candidates) == 2
    assert plan.ambiguous_title_indexes == (0,)


def test_recovery_does_not_accept_different_fingerprint_or_title(tmp_path):
    parent = tmp_path / "unrelated"
    parent.mkdir()
    (parent / "disc-04-ffffffffffffffff-title-000.mkv").write_bytes(b"other-disc")
    (parent / "disc-04-0123456789abcdef-title-001.mkv").write_bytes(b"other-title")

    plan = discover_existing_rips(tmp_path, (_job(0),))

    assert plan.candidates == ()
    assert plan.missing_title_indexes == (0,)


def test_recovery_accepts_one_complete_size_bound_special_feature_cohort(tmp_path):
    jobs = (_job(0), _job(1))
    jobs = tuple(
        RipJob(**{**job.__dict__, "estimated_bytes": 10 + job.title_index})
        for job in jobs
    )
    complete = tmp_path / "complete"
    incomplete = tmp_path / "incomplete"
    complete.mkdir()
    incomplete.mkdir()
    for job in jobs:
        (
            complete / f"special-1111111111111111-title-{job.title_index:03d}.mkv"
        ).write_bytes(b"x" * job.estimated_bytes)
    (incomplete / "special-2222222222222222-title-000.mkv").write_bytes(b"x" * 10)

    plan = discover_existing_rips(tmp_path, jobs)

    assert [candidate.title_index for candidate in plan.candidates] == [0, 1]
    assert all(
        candidate.basename.startswith("special-1111") for candidate in plan.candidates
    )


def test_recovery_holds_competing_complete_special_feature_cohorts(tmp_path):
    job = RipJob(**{**_job(0).__dict__, "estimated_bytes": 10})
    for token in ("1111111111111111", "2222222222222222"):
        parent = tmp_path / token
        parent.mkdir()
        (parent / f"special-{token}-title-000.mkv").write_bytes(b"x" * 10)

    plan = discover_existing_rips(tmp_path, (job,))

    assert len(plan.candidates) == 2
    assert len({candidate.candidate_id for candidate in plan.candidates}) == 2
    assert plan.ambiguous_title_indexes == (0,)


def test_recovery_allows_bounded_makemkv_inventory_size_variance(tmp_path):
    job = RipJob(**{**_job(0).__dict__, "estimated_bytes": 100_000_000})
    parent = tmp_path / "complete"
    parent.mkdir()
    source = parent / "special-1111111111111111-title-000.mkv"
    source.write_bytes(b"x")
    with source.open("r+b") as stream:
        stream.truncate(97_500_000)

    plan = discover_existing_rips(tmp_path, (job,))

    assert [candidate.title_index for candidate in plan.candidates] == [0]


def test_recovery_rejects_large_special_feature_size_difference(tmp_path):
    job = RipJob(**{**_job(0).__dict__, "estimated_bytes": 100_000_000})
    parent = tmp_path / "wrong-size"
    parent.mkdir()
    source = parent / "special-1111111111111111-title-000.mkv"
    source.write_bytes(b"x")
    with source.open("r+b") as stream:
        stream.truncate(80_000_000)

    plan = discover_existing_rips(tmp_path, (job,))

    assert plan.candidates == ()
    assert plan.missing_title_indexes == (0,)
