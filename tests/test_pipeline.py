import hashlib
import json

import pytest

from mkv_episode_matcher.pipeline import (
    PIPELINE_STAGES,
    PipelineArtifact,
    PipelineError,
    run_checkpointed_pipeline,
)


def _runners(tmp_path, calls, fail_stage=None):
    runners = {}
    for stage in PIPELINE_STAGES:

        def runner(context, current=stage):
            calls.append(current)
            if current == fail_stage:
                raise RuntimeError("private synthetic failure")
            assert context.stage == current
            if current != "rip":
                assert context.previous is not None
            contract = tmp_path / f"{current}.json"
            contract.write_text(json.dumps({"stage": current}), encoding="utf-8")
            return PipelineArtifact(
                stage=current,
                contract_path=contract,
                contract_sha256=hashlib.sha256(contract.read_bytes()).hexdigest(),
                item_count=2,
            )

        runners[stage] = runner
    return runners


def test_pipeline_links_all_stages_without_manual_mapping(tmp_path):
    calls = []
    final = run_checkpointed_pipeline(
        pipeline_id="synthetic-pipeline",
        plan_sha256="a" * 64,
        checkpoint_path=tmp_path / "checkpoint.json",
        runners=_runners(tmp_path, calls),
    )

    assert calls == list(PIPELINE_STAGES)
    assert final.stage == "organize"
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
    assert checkpoint["status"] == "completed"
    assert checkpoint["completed"] == list(PIPELINE_STAGES)


def test_pipeline_resumes_after_failure_at_first_unfinished_stage(tmp_path):
    calls = []
    with pytest.raises(PipelineError, match="transcode"):
        run_checkpointed_pipeline(
            pipeline_id="synthetic-pipeline",
            plan_sha256="b" * 64,
            checkpoint_path=tmp_path / "checkpoint.json",
            runners=_runners(tmp_path, calls, fail_stage="transcode"),
        )
    assert calls == ["rip", "identify", "transcode"]

    resumed_calls = []
    final = run_checkpointed_pipeline(
        pipeline_id="synthetic-pipeline",
        plan_sha256="b" * 64,
        checkpoint_path=tmp_path / "checkpoint.json",
        runners=_runners(tmp_path, resumed_calls),
    )
    assert resumed_calls == ["transcode", "organize"]
    assert final.stage == "organize"


def test_pipeline_refuses_changed_completed_contract(tmp_path):
    calls = []
    run_checkpointed_pipeline(
        pipeline_id="synthetic-pipeline",
        plan_sha256="c" * 64,
        checkpoint_path=tmp_path / "checkpoint.json",
        runners=_runners(tmp_path, calls),
    )
    (tmp_path / "identify.json").write_text("changed", encoding="utf-8")

    with pytest.raises(PipelineError, match="changed"):
        run_checkpointed_pipeline(
            pipeline_id="synthetic-pipeline",
            plan_sha256="c" * 64,
            checkpoint_path=tmp_path / "checkpoint.json",
            runners=_runners(tmp_path, []),
        )
