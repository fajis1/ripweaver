import json
from pathlib import PurePosixPath

import pytest

from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
from mkv_episode_matcher.media.organizer import (
    OrganizationPlanError,
    SequenceAssignment,
    add_jellyfin_version_label,
    build_episode_filename,
    jellyfin_resolution_label,
    load_sequence_assignments,
    plan_tv_organization,
    write_safe_organization_plan,
)


def _catalog():
    return (
        EpisodeCatalogEntry(
            "S03E01",
            3,
            1,
            "A Story: The Beginning?",
            "",
            1800,
        ),
        EpisodeCatalogEntry("S04E01", 4, 1, "Another Story", "", 1800),
    )


def test_filename_and_layout_stop_at_season_folder(tmp_path):
    plan = plan_tv_organization(
        (SequenceAssignment("disc-title000", "S03E01"),),
        _catalog(),
        library_root=tmp_path,
        series_name="Test Series",
    )

    item = plan.items[0]
    assert item.relative_destination == (
        "Test Series/Season 03/Test Series - S03E01 - A Story The Beginning.mkv"
    )
    assert item.status == "proposed"
    assert plan.missing_directories == ("Test Series/Season 03",)


def test_jellyfin_resolution_version_uses_required_suffix():
    canonical = build_episode_filename(
        "Test Series", "S03E01", "A Story", version_label="1080p"
    )
    assert canonical == "Test Series - S03E01 - A Story - 1080p.mkv"
    assert jellyfin_resolution_label(576, "tt") == "576i"
    assert jellyfin_resolution_label(2160, "progressive") == "2160p"
    assert (
        add_jellyfin_version_label(
            PurePosixPath("Test Series/Season 03/Test Series - S03E01 - A Story.mkv"),
            "720p",
        )
        .as_posix()
        .endswith("Test Series - S03E01 - A Story - 720p.mkv")
    )


def test_exact_destination_collision_routes_to_review(tmp_path):
    season = tmp_path / "Test Series" / "Season 03"
    season.mkdir(parents=True)
    filename = build_episode_filename(
        "Test Series",
        "S03E01",
        "A Story: The Beginning?",
    )
    (season / filename.upper()).touch()

    plan = plan_tv_organization(
        (SequenceAssignment("disc-title000", "S03E01"),),
        _catalog(),
        library_root=tmp_path,
        series_name="Test Series",
    )

    assert plan.items[0].status == "review-existing-destination"
    assert plan.review_count == 1


def test_same_episode_with_different_title_routes_to_dedup_review(tmp_path):
    season = tmp_path / "Test Series" / "Season 03"
    season.mkdir(parents=True)
    (season / "Old Name - S03E01 - Different Title.mkv").touch()

    plan = plan_tv_organization(
        (SequenceAssignment("disc-title000", "S03E01"),),
        _catalog(),
        library_root=tmp_path,
        series_name="Test Series",
    )

    assert plan.items[0].status == "review-existing-episode"
    assert plan.items[0].conflicts == ("Old Name - S03E01 - Different Title.mkv",)


def test_unsafe_series_component_is_rejected(tmp_path):
    with pytest.raises(OrganizationPlanError, match="one safe path component"):
        plan_tv_organization(
            (SequenceAssignment("disc-title000", "S03E01"),),
            _catalog(),
            library_root=tmp_path,
            series_name="../Test Series",
        )


def test_loader_requires_fully_proposed_unique_sequence(tmp_path):
    report = tmp_path / "sequence.json"
    report.write_text(
        json.dumps({
            "mode": "saved-disc-sequence-plan",
            "disposition": "review-ambiguous",
            "groups": [],
        }),
        encoding="utf-8",
    )

    with pytest.raises(OrganizationPlanError, match="requires review"):
        load_sequence_assignments(report)


def test_report_refuses_overwrite_and_omits_library_root(tmp_path):
    plan = plan_tv_organization(
        (SequenceAssignment("disc-title000", "S03E01"),),
        _catalog(),
        library_root=tmp_path,
        series_name="Test Series",
    )
    report = tmp_path / "plan.json"

    write_safe_organization_plan(report, plan)

    serialized = report.read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized
    with pytest.raises(OrganizationPlanError, match="refusing overwrite"):
        write_safe_organization_plan(report, plan)
