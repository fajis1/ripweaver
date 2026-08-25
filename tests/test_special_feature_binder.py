import json

import pytest

from mkv_episode_matcher.disc.rip_manifest import load_rip_manifest
from mkv_episode_matcher.disc.ripper import RipError
from mkv_episode_matcher.disc.special_feature_binder import (
    SpecialFeatureBindError,
    bind_diagnostic_special_feature_manifest,
    file_sha256,
    load_bound_special_feature_manifest,
    write_bound_special_feature_manifest,
)
from mkv_episode_matcher.disc.special_feature_manifest import (
    build_diagnostic_special_feature_manifest,
    write_diagnostic_special_feature_manifest,
)
from mkv_episode_matcher.disc.special_features import (
    SpecialFeatureDecision,
    SpecialFeaturePlan,
)
from mkv_episode_matcher.disc.title_selector import NormalizedTitle


def _diagnostic_plan():
    decision = SpecialFeatureDecision(
        title=NormalizedTitle(
            index=0,
            duration_seconds=300,
            size_bytes=10_000_000,
            chapters=1,
            output_name=None,
            audio_streams=(),
        ),
        classification="matched-feature",
        recommended_for_rip=True,
        matched_feature_id="known",
        candidate_feature_ids=("known",),
        matched_title="Known Feature",
        feature_type="featurette",
        jellyfin_folder="featurettes",
        runtime_delta_seconds=0,
        diagnostic_audio_stream=None,
        alternate_audio_streams=(),
        audio_policy="preserve-source",
        jellyfin_fallback_folder=None,
        fallback_name_policy="none",
        reasons=("fixture",),
    )
    return build_diagnostic_special_feature_manifest(
        SpecialFeaturePlan(
            report_id="report-1",
            mode="special-features-plan-only",
            catalog_id="catalog-v1",
            release_id="release-v1",
            catalogue_entry_count=1,
            warning_count=0,
            planning_notes=(),
            decisions=(decision,),
            missing_feature_ids=(),
        )
    )


def _inventory(size=10_000_000):
    return {
        "minimum_length_seconds": 0,
        "drive": {"index": 2},
        "titles": [
            {
                "index": 0,
                "attributes": {
                    "8": "1",
                    "9": "0:05:00",
                    "11": str(size),
                    "27": "title.mkv",
                },
                "streams": {},
            }
        ],
        "warnings": [],
    }


def test_binder_rejects_inventory_that_can_shift_title_indexes(tmp_path):
    diagnostic_path, inventory_path = _write_inputs(tmp_path)
    payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    payload["minimum_length_seconds"] = 120
    inventory_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SpecialFeatureBindError, match="title indexes cannot shift"):
        bind_diagnostic_special_feature_manifest(
            diagnostic_path,
            inventory_path,
            expected_diagnostic_sha256=file_sha256(diagnostic_path),
        )


def _write_inputs(tmp_path, *, size=10_000_000):
    diagnostic_path = tmp_path / "diagnostic.json"
    inventory_path = tmp_path / "fresh.json"
    write_diagnostic_special_feature_manifest(
        diagnostic_path,
        _diagnostic_plan(),
    )
    inventory_path.write_text(json.dumps(_inventory(size)), encoding="utf-8")
    return diagnostic_path, inventory_path


def test_binder_requires_digest_and_identical_inventory(tmp_path):
    diagnostic_path, inventory_path = _write_inputs(tmp_path)
    digest = file_sha256(diagnostic_path)

    bound = bind_diagnostic_special_feature_manifest(
        diagnostic_path,
        inventory_path,
        expected_diagnostic_sha256=digest,
    )

    assert bound.execution_authorized is False
    assert bound.diagnostic_manifest_sha256 == digest
    assert len(bound.jobs) == 1
    assert bound.jobs[0].drive_index == 2
    assert bound.jobs[0].title_index == 0


def test_binder_rejects_wrong_diagnostic_digest(tmp_path):
    diagnostic_path, inventory_path = _write_inputs(tmp_path)

    with pytest.raises(SpecialFeatureBindError, match="does not match"):
        bind_diagnostic_special_feature_manifest(
            diagnostic_path,
            inventory_path,
            expected_diagnostic_sha256="0" * 64,
        )


def test_binder_rejects_changed_fresh_inventory(tmp_path):
    diagnostic_path, inventory_path = _write_inputs(tmp_path, size=10_000_001)

    with pytest.raises(SpecialFeatureBindError, match="does not match"):
        bind_diagnostic_special_feature_manifest(
            diagnostic_path,
            inventory_path,
            expected_diagnostic_sha256=file_sha256(diagnostic_path),
        )


def test_episode_rip_loader_rejects_bound_special_feature_manifest(tmp_path):
    diagnostic_path, inventory_path = _write_inputs(tmp_path)
    bound = bind_diagnostic_special_feature_manifest(
        diagnostic_path,
        inventory_path,
        expected_diagnostic_sha256=file_sha256(diagnostic_path),
    )
    bound_path = tmp_path / "bound.json"
    write_bound_special_feature_manifest(bound_path, bound)

    with pytest.raises(RipError, match="not an approved rip manifest"):
        load_rip_manifest(bound_path)

    loaded = load_bound_special_feature_manifest(
        bound_path,
        inventory_path,
        expected_bound_sha256=file_sha256(bound_path),
    )
    assert loaded.jobs == bound.jobs
