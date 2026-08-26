import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.disc.special_features import (
    SpecialFeaturePlanError,
    build_special_feature_plan,
    load_feature_catalog,
    load_feature_catalog_payload,
)


def _title(
    index: int,
    seconds: int,
    *,
    size: int | None = None,
    chapters: int = 1,
    audio_streams: int = 0,
):
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return {
        "index": index,
        "attributes": {
            "8": str(chapters),
            "9": f"{hours}:{minutes:02d}:{seconds:02d}",
            "11": str(size if size is not None else seconds * 100_000),
            "27": f"title_t{index:02d}.mkv",
        },
        "streams": {
            str(stream_index): {
                "attributes": {
                    "1": "Audio",
                    "2": "Stereo",
                    "3": "eng",
                    "4": "English",
                    "5": "Stereo",
                }
            }
            for stream_index in range(audio_streams)
        },
    }


def _catalog(features):
    return load_feature_catalog_payload({
        "mode": "special-feature-catalog",
        "features": features,
    })


def _feature(
    feature_id: str,
    title: str,
    feature_type: str,
    runtime: int,
    **extra,
):
    return {
        "feature_id": feature_id,
        "title": title,
        "feature_type": feature_type,
        "runtime_seconds": runtime,
        **extra,
    }


def test_bonus_disc_matches_parts_and_holds_play_all_and_menu():
    catalogue = _catalog([
        _feature("making", "Making the Movie", "behind the scenes", 1800),
        _feature("deleted", "Deleted Scene", "deleted scene", 420),
        _feature("trailer", "Original Trailer", "trailer", 120),
        _feature(
            "play-all",
            "Play All",
            "other",
            2340,
            play_all=True,
            component_ids=["making", "deleted", "trailer"],
        ),
    ])
    inventory = {
        "titles": [
            _title(0, 1798),
            _title(1, 423),
            _title(2, 119),
            _title(3, 2340),
            _title(4, 14),
        ],
        "warnings": [],
    }

    plan = build_special_feature_plan(inventory, catalogue, report_id="bonus-fixture")
    decisions = {item.title.index: item for item in plan.decisions}

    assert [
        item.title.index for item in plan.decisions if item.recommended_for_rip
    ] == [
        0,
        1,
        2,
    ]
    assert decisions[0].jellyfin_folder == "behind the scenes"
    assert decisions[1].jellyfin_folder == "deleted scenes"
    assert decisions[2].jellyfin_folder == "trailers"
    assert decisions[3].classification == "play-all-candidate"
    assert decisions[4].classification == "menu-candidate"


def test_global_assignment_avoids_greedy_dead_end():
    catalogue = _catalog([
        _feature("a", "Feature A", "featurette", 106),
        _feature("b", "Feature B", "featurette", 120),
    ])
    inventory = {"titles": [_title(0, 100), _title(1, 110)], "warnings": []}

    plan = build_special_feature_plan(
        inventory,
        catalogue,
        report_id="assignment-fixture",
        maximum_runtime_delta=15,
    )

    assert {
        decision.title.index: decision.matched_feature_id for decision in plan.decisions
    } == {0: "a", 1: "b"}


def test_metadata_duplicate_is_not_treated_as_proven_duplicate():
    catalogue = _catalog([])
    inventory = {
        "titles": [
            _title(0, 300, size=50_000_000, chapters=2),
            _title(1, 300, size=50_000_000, chapters=2),
        ],
        "warnings": [],
    }

    plan = build_special_feature_plan(
        inventory, catalogue, report_id="duplicate-fixture"
    )
    held = [decision for decision in plan.decisions if not decision.recommended_for_rip]

    assert len(held) == 2
    assert {item.classification for item in held} == {"duplicate-candidate"}
    assert all("not proof" in item.reasons[1] for item in held)


def test_equal_runtime_candidates_are_held_as_ambiguous():
    catalogue = _catalog([
        _feature("music-video", "Let's Get Together", "other", 95),
        _feature("gallery", "Production Gallery", "other", 97),
    ])
    inventory = {
        "titles": [_title(0, 99), _title(1, 99)],
        "warnings": [],
    }

    plan = build_special_feature_plan(
        inventory, catalogue, report_id="runtime-tie-fixture"
    )

    assert {item.classification for item in plan.decisions} == {"ambiguous-match"}
    assert not any(item.recommended_for_rip for item in plan.decisions)
    assert all(
        set(item.candidate_feature_ids) == {"music-video", "gallery"}
        for item in plan.decisions
    )
    assert set(plan.missing_feature_ids) == {"music-video", "gallery"}


def test_multiple_audio_streams_require_preservation():
    plan = build_special_feature_plan(
        {"titles": [_title(0, 144, audio_streams=4)], "warnings": []},
        _catalog([_feature("audio-archive", "Audio Archives", "other", 144)]),
        report_id="multi-audio-fixture",
    )

    assert plan.decisions[0].audio_policy == "preserve-all"


def test_menu_bound_catalogue_items_are_not_reported_as_missing_titles():
    catalogue = load_feature_catalog_payload({
        "mode": "special-feature-catalog",
        "catalog_id": "release-fixture-v1",
        "release_id": "release-fixture",
        "sources": [
            {
                "source_id": "review",
                "source_type": "review",
                "locator": "https://example.invalid/review",
            }
        ],
        "features": [
            {
                **_feature("video", "Standalone Video", "featurette", 300),
                "source_ids": ["review"],
            },
            {
                "feature_id": "gallery",
                "title": "Interactive Gallery",
                "feature_type": "other",
                "runtime_seconds": None,
                "representation": "menu-bound",
                "source_ids": ["review"],
            },
        ],
    })

    plan = build_special_feature_plan(
        {"titles": [_title(0, 300)], "warnings": []},
        catalogue,
        report_id="representation-fixture",
    )

    assert plan.catalog_id == "release-fixture-v1"
    assert plan.release_id == "release-fixture"
    assert plan.missing_feature_ids == ()


def test_unknown_catalogue_source_is_rejected():
    with pytest.raises(SpecialFeaturePlanError, match="unknown source"):
        load_feature_catalog_payload({
            "mode": "special-feature-catalog",
            "features": [
                {
                    **_feature("video", "Standalone Video", "featurette", 300),
                    "source_ids": ["missing"],
                }
            ],
        })


def test_unmatched_title_has_jellyfin_fallback_policy():
    plan = build_special_feature_plan(
        {"titles": [_title(0, 300)], "warnings": []},
        _catalog([]),
        report_id="fallback-fixture",
    )

    decision = plan.decisions[0]
    assert decision.classification == "review"
    assert decision.jellyfin_fallback_folder == "extras"
    assert decision.fallback_name_policy == "content-fingerprint-required"


def test_reviewed_release_fixture_uses_generic_catalogue_schema():
    catalogue = load_feature_catalog(
        Path(__file__).parent / "fixtures" / "parent_trap_2005_special_features.json"
    )

    assert catalogue.catalog_id == "parent-trap-2005-r1-disc2-v1"
    assert any(
        item.representation == "multi-audio-title" for item in catalogue.features
    )
    assert any(item.representation == "menu-bound" for item in catalogue.features)


def test_catalog_rejects_unknown_play_all_component():
    with pytest.raises(SpecialFeaturePlanError, match="unknown component"):
        _catalog([
            _feature(
                "play-all",
                "Play All",
                "other",
                300,
                play_all=True,
                component_ids=["missing"],
            )
        ])


def test_plan_contains_no_media_action():
    plan = build_special_feature_plan(
        {"titles": [_title(0, 300)], "warnings": []},
        _catalog([_feature("one", "One", "featurette", 300)]),
        report_id="safe-fixture",
    )
    serialized = json.dumps(plan.to_dict()).casefold()

    for forbidden in ("command", "makemkv", "handbrake", "eject", "source_path"):
        assert forbidden not in serialized


def test_cli_json_is_path_redacted(tmp_path):
    inventory_path = tmp_path / "private-parent-trap-label.json"
    catalogue_path = tmp_path / "private-release.json"
    inventory_path.write_text(
        json.dumps({"titles": [_title(0, 300)], "warnings": []}),
        encoding="utf-8",
    )
    catalogue_path.write_text(
        json.dumps({
            "mode": "special-feature-catalog",
            "features": [_feature("one", "One", "featurette", 300)],
        }),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "plan-special-features",
            str(inventory_path),
            str(catalogue_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "special-features-plan-only"
    assert payload["report_id"] == "report-1"
    assert str(inventory_path) not in result.output
    assert str(catalogue_path) not in result.output
