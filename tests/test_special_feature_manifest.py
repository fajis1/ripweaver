import json

import pytest
from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.disc.special_feature_manifest import (
    SpecialFeatureManifestError,
    build_diagnostic_special_feature_manifest,
    write_diagnostic_special_feature_manifest,
)
from mkv_episode_matcher.disc.special_features import (
    SpecialFeatureDecision,
    SpecialFeaturePlan,
)
from mkv_episode_matcher.disc.title_selector import NormalizedTitle


def _decision(
    index,
    classification,
    *,
    candidates=(),
    audio_policy="review",
    fallback=False,
):
    return SpecialFeatureDecision(
        title=NormalizedTitle(
            index=index,
            duration_seconds=300,
            size_bytes=10_000_000 + index,
            chapters=1,
            output_name=None,
            audio_streams=(),
        ),
        classification=classification,
        recommended_for_rip=classification == "matched-feature",
        matched_feature_id=candidates[0] if candidates else None,
        candidate_feature_ids=tuple(candidates),
        matched_title=None,
        feature_type=None,
        jellyfin_folder=None,
        runtime_delta_seconds=None,
        diagnostic_audio_stream=None,
        alternate_audio_streams=(),
        audio_policy=audio_policy,
        jellyfin_fallback_folder="extras" if fallback else None,
        fallback_name_policy=("content-fingerprint-required" if fallback else "none"),
        reasons=("fixture",),
    )


def _plan(decisions):
    return SpecialFeaturePlan(
        report_id="report-1",
        mode="special-features-plan-only",
        catalog_id="catalog-v1",
        release_id="release-v1",
        catalogue_entry_count=2,
        warning_count=0,
        planning_notes=(),
        decisions=tuple(decisions),
        missing_feature_ids=(),
    )


def test_manifest_includes_plausible_titles_and_holds_menu_and_play_all():
    manifest = build_diagnostic_special_feature_manifest(
        _plan([
            _decision(0, "matched-feature", candidates=("known",)),
            _decision(
                1,
                "ambiguous-match",
                candidates=("candidate-a", "candidate-b"),
                fallback=True,
            ),
            _decision(2, "review", fallback=True),
            _decision(3, "duplicate-candidate", fallback=True),
            _decision(4, "menu-candidate"),
            _decision(5, "play-all-candidate"),
        ])
    )

    assert [job.title_index for job in manifest.jobs] == [0, 1, 2, 3]
    assert {(item.title_index, item.reason) for item in manifest.excluded_titles} == {
        (4, "probable-menu-held"),
        (5, "play-all-held"),
    }
    assert manifest.execution_authorized is False
    assert manifest.jobs[1].candidate_feature_ids == (
        "candidate-a",
        "candidate-b",
    )
    assert manifest.jobs[1].jellyfin_fallback_folder == "extras"


def test_multi_audio_job_requires_stream_inventory():
    manifest = build_diagnostic_special_feature_manifest(
        _plan([
            _decision(
                0,
                "matched-feature",
                candidates=("sound-studio",),
                audio_policy="preserve-all",
            )
        ])
    )

    job = manifest.jobs[0]
    assert job.audio_policy == "preserve-all"
    assert "audio-stream-inventory" in job.evidence_after_rip


def test_manifest_is_path_redacted_and_has_no_execution_command():
    manifest = build_diagnostic_special_feature_manifest(
        _plan([_decision(0, "review", fallback=True)])
    )
    serialized = json.dumps(manifest.to_dict()).casefold()

    assert manifest.mode == "special-feature-diagnostic-rip-plan-only"
    assert ".staging/special-features/" in serialized
    for forbidden in (
        "drive_index",
        "source_path",
        "library_root",
        "makemkv",
        "command",
        "confirm-rip",
    ):
        assert forbidden not in serialized


def test_manifest_requires_at_least_one_plausible_title():
    with pytest.raises(SpecialFeatureManifestError, match="No plausible"):
        build_diagnostic_special_feature_manifest(
            _plan([_decision(0, "menu-candidate")])
        )


def test_writer_refuses_overwrite_and_missing_parent(tmp_path):
    manifest = build_diagnostic_special_feature_manifest(
        _plan([_decision(0, "matched-feature", candidates=("known",))])
    )
    output = tmp_path / "manifest.json"

    write_diagnostic_special_feature_manifest(output, manifest)
    with pytest.raises(SpecialFeatureManifestError, match="refusing overwrite"):
        write_diagnostic_special_feature_manifest(output, manifest)
    with pytest.raises(SpecialFeatureManifestError, match="parent directory"):
        write_diagnostic_special_feature_manifest(
            tmp_path / "missing" / "manifest.json",
            manifest,
        )


def test_cli_writes_non_executable_manifest_from_saved_data(tmp_path):
    inventory = tmp_path / "inventory.json"
    catalogue = tmp_path / "catalogue.json"
    output = tmp_path / "diagnostic.json"
    inventory.write_text(
        json.dumps({
            "titles": [
                {
                    "index": 0,
                    "attributes": {
                        "8": "1",
                        "9": "0:05:00",
                        "11": "10000000",
                        "27": "title.mkv",
                    },
                    "streams": {},
                },
                {
                    "index": 1,
                    "attributes": {
                        "8": "1",
                        "9": "0:00:07",
                        "11": "100000",
                        "27": "menu.mkv",
                    },
                    "streams": {},
                },
            ],
            "warnings": [],
        }),
        encoding="utf-8",
    )
    catalogue.write_text(
        json.dumps({
            "mode": "special-feature-catalog",
            "catalog_id": "catalog-v1",
            "release_id": "release-v1",
            "features": [
                {
                    "feature_id": "known",
                    "title": "Known Feature",
                    "feature_type": "featurette",
                    "runtime_seconds": 300,
                }
            ],
        }),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "plan-special-feature-rip",
            str(inventory),
            str(catalogue),
            "--manifest-out",
            str(output),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == "special-feature-diagnostic-rip-plan-only"
    assert payload["execution_authorized"] is False
    assert [job["title_index"] for job in payload["jobs"]] == [0]
    assert payload["excluded_titles"][0]["reason"] == "probable-menu-held"
