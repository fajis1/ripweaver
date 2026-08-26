import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from mkv_episode_matcher.backend.jellyfin_cleanup import (
    _disk_metadata,
    apply_cleanup,
    filter_plan,
    plan_cleanup,
)
from mkv_episode_matcher.core.datetime_compat import UTC


def test_disk_label_uses_series_context_from_tv_staging_path():
    key, label = _disk_metadata(
        Path(
            "TV Shows/Faerie Tale Theatre/Unmatched/disc-03-a9efabef47cf0247-title-000.mkv"
        )
    )

    assert key == "disc-03-a9efabef47cf0247"
    assert label == "Faerie Tale Theatre · Disc 03"


def _workspace(tmp_path):
    rip = tmp_path / "rip"
    encoded = tmp_path / "encoded"
    library = tmp_path / "library"
    contracts = tmp_path / "contracts"
    for path in (rip, encoded, library, contracts):
        path.mkdir()
    return rip, encoded, library, contracts


def test_plan_finds_only_encoded_files_with_jellyfin_counterparts(tmp_path):
    rip, encoded, library, contracts = _workspace(tmp_path)
    target = encoded / "encoded-staging" / "Show" / "Season 01" / "Show - S01E01.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"encoded")
    (library / "Show" / "Season 01").mkdir(parents=True)
    (library / "Show" / "Season 01" / target.name).write_bytes(b"library")
    orphan = encoded / "encoded-staging" / "Show" / "Season 01" / "orphan.mkv"
    orphan.write_bytes(b"orphan")

    plan = plan_cleanup(
        rip_root=rip,
        encoded_root=encoded,
        contract_root=contracts,
        library_root=library,
        mode="all",
    )

    assert len(plan.candidates) == 1
    assert plan.candidates[0].library_relative == "Show/Season 01/Show - S01E01.mkv"


def test_rip_requires_verified_transcode_contract_and_age_filter(tmp_path):
    rip, encoded, library, contracts = _workspace(tmp_path)
    source = rip / "TV Shows" / "Show" / "title-000.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"rip")
    encoded_file = (
        encoded / "encoded-staging" / "Show" / "Season 01" / "Show - S01E01.mkv"
    )
    encoded_file.parent.mkdir(parents=True)
    encoded_file.write_bytes(b"encoded")
    library_file = library / "Show" / "Season 01" / encoded_file.name
    library_file.parent.mkdir(parents=True)
    library_file.write_bytes(b"library")
    (contracts / "item.transcode.json").write_text(
        json.dumps({
            "mode": "verified-transcode-contract",
            "original_source_path": str(source),
            "encoded_path": str(encoded_file),
            "library_relative": "Show/Season 01/Show - S01E01.mkv",
        }),
        encoding="utf-8",
    )
    old = datetime.now(UTC) - timedelta(days=8)
    for path in (source, encoded_file):
        timestamp = old.timestamp()
        os.utime(path, (timestamp, timestamp))

    plan = plan_cleanup(
        rip_root=rip,
        encoded_root=encoded,
        contract_root=contracts,
        library_root=library,
        mode="older_than",
        days=7,
        now=datetime.now(UTC),
    )

    assert {item.category for item in plan.candidates} == {"rip", "encoded"}
    apply_cleanup(
        plan=plan,
        rip_root=rip,
        encoded_root=encoded,
        contract_root=contracts,
        library_root=library,
        expected_plan_sha256=plan.plan_sha256,
        authorized_file_count=2,
    )
    assert not source.exists()
    assert not encoded_file.exists()
    assert library_file.exists()


def test_all_staging_includes_unbacked_rip_encoded_and_cleanup_files(tmp_path):
    rip, encoded, _library, contracts = _workspace(tmp_path)
    cleanup = tmp_path / "cleanup"
    cleanup.mkdir()
    (rip / "unbacked-rip.mkv").write_bytes(b"rip")
    (encoded / "unbacked-encoded.mkv").write_bytes(b"encoded")
    (cleanup / "retained-original.mkv").write_bytes(b"cleanup")

    plan = plan_cleanup(
        rip_root=rip,
        encoded_root=encoded,
        contract_root=contracts,
        library_root=None,
        cleanup_root=cleanup,
        mode="all_staging",
    )

    assert {item.category for item in plan.candidates} == {"rip", "encoded", "cleanup"}


def test_selected_group_rechecks_only_selected_candidates(tmp_path):
    rip, encoded, _library, contracts = _workspace(tmp_path)
    first = rip / "disc-01-0123456789abcdef-title-000.mkv"
    second = rip / "disc-02-fedcba9876543210-title-000.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    plan = plan_cleanup(
        rip_root=rip,
        encoded_root=encoded,
        contract_root=contracts,
        library_root=None,
        mode="all_staging",
    )
    selected = filter_plan(plan, (plan.candidates[0],))
    apply_cleanup(
        plan=selected,
        rip_root=rip,
        encoded_root=encoded,
        contract_root=contracts,
        library_root=None,
        expected_plan_sha256=selected.plan_sha256,
        authorized_file_count=1,
        candidate_keys=(
            f"{plan.candidates[0].category}:{plan.candidates[0].relative_path}",
        ),
    )
    assert not first.exists()
    assert second.exists()
