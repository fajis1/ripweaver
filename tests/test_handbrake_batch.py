from types import SimpleNamespace

import pytest

from mkv_episode_matcher.media.handbrake import (
    HandBrakeCapabilities,
    HandBrakeProfile,
)
from mkv_episode_matcher.media.handbrake_batch import (
    HandBrakeBatchError,
    OrganizationTarget,
    plan_handbrake_batch,
    write_handbrake_batch_manifest,
)


def _inputs(tmp_path):
    source_root = tmp_path / "sources"
    output_root = tmp_path / "output"
    source_root.mkdir()
    output_root.mkdir()
    first = source_root / "disc-title000.mkv"
    second = source_root / "disc-title001.mkv"
    first.write_bytes(b"a" * 100)
    second.write_bytes(b"b" * 200)
    targets = (
        OrganizationTarget(
            "disc-title000",
            "Test Series/Season 03/Test Series - S03E01 - First.mkv",
        ),
        OrganizationTarget(
            "disc-title001",
            "Test Series/Season 03/Test Series - S03E02 - Second.mkv",
        ),
    )
    sources = {"disc-title000": first, "disc-title001": second}
    return targets, sources, output_root


def _capabilities():
    return HandBrakeCapabilities(True, ("vce_h265",))


def _space(free=10_000):
    return lambda _path: SimpleNamespace(free=free)


def test_batch_manifest_is_relative_complete_and_does_not_create_directories(tmp_path):
    targets, sources, output_root = _inputs(tmp_path)

    manifest = plan_handbrake_batch(
        targets,
        sources,
        output_root=output_root,
        staging_prefix="encoded-staging",
        profile=HandBrakeProfile(),
        capabilities=_capabilities(),
        reserve_bytes=1000,
        disk_usage=_space(),
    )

    assert manifest.status == "ready-after-directory-creation"
    assert manifest.job_count == 2
    assert manifest.total_source_bytes == 300
    assert manifest.jobs[0].destination_relative.startswith("encoded-staging/")
    assert not (output_root / "encoded-staging").exists()


def test_source_mappings_require_exact_coverage(tmp_path):
    targets, sources, output_root = _inputs(tmp_path)
    sources.pop("disc-title001")

    with pytest.raises(HandBrakeBatchError, match="exactly cover"):
        plan_handbrake_batch(
            targets,
            sources,
            output_root=output_root,
            staging_prefix="encoded-staging",
            profile=HandBrakeProfile(),
            capabilities=_capabilities(),
            disk_usage=_space(),
        )


def test_existing_encoded_destination_refuses_manifest(tmp_path):
    targets, sources, output_root = _inputs(tmp_path)
    destination = (
        output_root
        / "encoded-staging"
        / "Test Series"
        / "Season 03"
        / "Test Series - S03E01 - First.mkv"
    )
    destination.parent.mkdir(parents=True)
    destination.touch()

    with pytest.raises(HandBrakeBatchError, match="refusing overwrite"):
        plan_handbrake_batch(
            targets,
            sources,
            output_root=output_root,
            staging_prefix="encoded-staging",
            profile=HandBrakeProfile(),
            capabilities=_capabilities(),
            reserve_bytes=0,
            disk_usage=_space(),
        )


def test_insufficient_space_and_missing_vcn_stop_planning(tmp_path):
    targets, sources, output_root = _inputs(tmp_path)

    with pytest.raises(HandBrakeBatchError, match="insufficient"):
        plan_handbrake_batch(
            targets,
            sources,
            output_root=output_root,
            staging_prefix="encoded-staging",
            profile=HandBrakeProfile(),
            capabilities=_capabilities(),
            reserve_bytes=1000,
            disk_usage=_space(100),
        )
    with pytest.raises(HandBrakeBatchError, match="not available"):
        plan_handbrake_batch(
            targets,
            sources,
            output_root=output_root,
            staging_prefix="encoded-staging",
            profile=HandBrakeProfile(),
            capabilities=HandBrakeCapabilities(False, ()),
            disk_usage=_space(),
        )


def test_saved_manifest_contains_no_absolute_roots_and_refuses_overwrite(tmp_path):
    targets, sources, output_root = _inputs(tmp_path)
    manifest = plan_handbrake_batch(
        targets,
        sources,
        output_root=output_root,
        staging_prefix="encoded-staging",
        profile=HandBrakeProfile(),
        capabilities=_capabilities(),
        reserve_bytes=0,
        disk_usage=_space(),
    )
    report = tmp_path / "manifest.json"

    write_handbrake_batch_manifest(report, manifest)

    serialized = report.read_text(encoding="utf-8")
    assert str(output_root) not in serialized
    assert str(next(iter(sources.values())).parent) not in serialized
    with pytest.raises(HandBrakeBatchError, match="refusing overwrite"):
        write_handbrake_batch_manifest(report, manifest)
