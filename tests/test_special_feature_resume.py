import json
from dataclasses import replace

import pytest

from mkv_episode_matcher.disc.special_feature_binder import (
    SpecialFeatureBindError,
    _normalized_inventory,
    bind_diagnostic_special_feature_manifest,
    file_sha256,
    write_bound_special_feature_manifest,
)
from mkv_episode_matcher.disc.special_feature_resume import (
    build_special_feature_resume_manifest,
)
from tests.test_special_feature_binder import _write_inputs


def _resume_inputs(tmp_path):
    diagnostic, original_inventory = _write_inputs(tmp_path)
    bound = bind_diagnostic_special_feature_manifest(
        diagnostic,
        original_inventory,
        expected_diagnostic_sha256=file_sha256(diagnostic),
    )
    second_job = replace(
        bound.jobs[0],
        job_id="special-fixture-title-001",
        title_index=1,
        relative_output_dir=".staging/special-features/token/title-001",
        output_basename="special-fixture-title-001.mkv",
    )

    inventory = json.loads(original_inventory.read_text(encoding="utf-8"))
    inventory["titles"].append(
        {
            "index": 1,
            "attributes": {
                "8": "1",
                "9": "0:05:00",
                "11": "10000000",
                "27": "title01.mkv",
            },
            "streams": {},
        }
    )
    original_inventory.write_text(json.dumps(inventory), encoding="utf-8")
    bound = replace(
        bound,
        jobs=(bound.jobs[0], second_job),
        inventory_signature_sha256=_normalized_inventory(inventory)[1],
    )
    bound_path = tmp_path / "bound.json"
    write_bound_special_feature_manifest(bound_path, bound)

    fresh_inventory = tmp_path / "relocated.json"
    inventory["drive"]["index"] = 7
    fresh_inventory.write_text(json.dumps(inventory), encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text(
        "\n".join(
            json.dumps(event)
            for event in (
                {"event": "queue_started", "job_count": 2},
                {"event": "job_started", "job_id": bound.jobs[0].job_id},
                {"event": "job_completed", "job_id": bound.jobs[0].job_id},
                {"event": "job_started", "job_id": bound.jobs[1].job_id},
                {"event": "queue_paused"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return bound_path, original_inventory, fresh_inventory, events


def test_resume_excludes_completed_and_rebinds_fresh_drive(tmp_path):
    bound, original, fresh, events = _resume_inputs(tmp_path)

    resumed = build_special_feature_resume_manifest(
        bound,
        original,
        fresh,
        events,
        expected_bound_sha256=file_sha256(bound),
    )

    assert [job.title_index for job in resumed.jobs] == [1]
    assert resumed.jobs[0].drive_index == 7
    assert "/resume-" in resumed.jobs[0].relative_output_dir
    assert resumed.execution_authorized is False


def test_resume_rejects_unknown_completed_job(tmp_path):
    bound, original, fresh, events = _resume_inputs(tmp_path)
    events.write_text(
        '{"event":"queue_started","job_count":2}\n'
        '{"event":"job_started","job_id":"unknown"}\n',
        encoding="utf-8",
    )

    with pytest.raises(SpecialFeatureBindError, match="unknown"):
        build_special_feature_resume_manifest(
            bound,
            original,
            fresh,
            events,
            expected_bound_sha256=file_sha256(bound),
        )
