import pytest

from mkv_episode_matcher.media.episode_catalog import (
    EpisodeCatalogEntry,
    EpisodeCatalogError,
    build_tmdb_aired_catalog,
    rank_catalog_candidates,
    validate_catalog,
    write_episode_catalog,
)


def test_build_tmdb_aired_catalog_ignores_specials_and_converts_runtime():
    responses = {
        "/tv/42": {
            "seasons": [
                {"season_number": 0},
                {"season_number": 1},
                {"season_number": 2},
            ]
        },
        "/tv/42/season/1": {
            "episodes": [
                {
                    "episode_number": 1,
                    "name": "First Story",
                    "overview": "A beginning.",
                    "runtime": 49,
                }
            ]
        },
        "/tv/42/season/2": {
            "episodes": [
                {
                    "episode_number": 3,
                    "name": "Later Story",
                    "overview": "",
                    "runtime": None,
                }
            ]
        },
    }

    catalog = build_tmdb_aired_catalog(42, responses.__getitem__)

    assert [item.episode_id for item in catalog] == ["S01E01", "S02E03"]
    assert catalog[0].runtime_seconds == 49 * 60
    assert catalog[1].runtime_seconds is None


def test_catalog_rejects_mismatched_and_duplicate_ids():
    with pytest.raises(EpisodeCatalogError, match="does not match"):
        validate_catalog([EpisodeCatalogEntry("S01E02", 1, 1, "Story", "")])

    item = EpisodeCatalogEntry("S01E01", 1, 1, "Story", "")
    with pytest.raises(EpisodeCatalogError, match="unique"):
        validate_catalog([item, item])


def test_tmdb_catalog_requires_usable_seasons():
    with pytest.raises(EpisodeCatalogError, match="no aired seasons"):
        build_tmdb_aired_catalog(42, lambda _path: {"seasons": []})


def test_local_catalog_ranking_prefers_named_story_and_runtime():
    catalog = (
        EpisodeCatalogEntry(
            "S03E01",
            3,
            1,
            "Goldilocks and the Three Bears",
            "A girl visits the home of three bears.",
            2880,
        ),
        EpisodeCatalogEntry(
            "S04E02",
            4,
            2,
            "The Snow Queen",
            "A goblin separates two friends.",
            2940,
        ),
    )

    candidates = rank_catalog_candidates(
        ("This is the story of Goldilocks and the three bears.",),
        2890,
        catalog,
    )

    assert candidates[0].episode_id == "S03E01"
    assert candidates[0].title_score == 1.0
    assert candidates[0].combined_score > candidates[1].combined_score


def test_catalog_report_is_path_free_and_refuses_overwrite(tmp_path):
    path = tmp_path / "catalog.json"
    entries = (
        EpisodeCatalogEntry(
            "S01E01",
            1,
            1,
            "First Story",
            "A safe overview.",
            3000,
        ),
    )

    write_episode_catalog(path, entries)

    serialized = path.read_text(encoding="utf-8")
    assert "S01E01" in serialized
    assert "source_path" not in serialized
    with pytest.raises(EpisodeCatalogError, match="refusing overwrite"):
        write_episode_catalog(path, entries)
