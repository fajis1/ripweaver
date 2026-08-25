import json

import pytest

from mkv_episode_matcher.backend.transcode_authorization import (
    build_transcode_authorization_plan,
)
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.handbrake_profiles import HandBrakeProfileStore
from mkv_episode_matcher.pipeline_queue import (
    PipelineQueueError,
    PipelineQueueStore,
    build_artifact,
)


def _artifact(tmp_path, media_id, stage):
    path = tmp_path / f"{media_id}-{stage}.json"
    path.write_text(
        json.dumps({"media_id": media_id, "stage": stage}), encoding="utf-8"
    )
    return build_artifact(stage, path)


def test_transcode_authorization_binds_exact_queue_profile_tools_and_root(tmp_path):
    store = PipelineQueueStore(tmp_path / "pipeline.sqlite3")
    store.enqueue_verified_rip("media-1", _artifact(tmp_path, "media-1", "rip"))
    store.claim_next()
    store.complete_stage(
        "media-1", "identify", _artifact(tmp_path, "media-1", "identify")
    )
    output = tmp_path / "encoded"
    output.mkdir()
    handbrake = tmp_path / "HandBrakeCLI.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    handbrake.write_bytes(b"synthetic")
    ffprobe.write_bytes(b"synthetic")
    config = Config(
        cache_dir=tmp_path / "cache",
        transcode_output_root=output,
        handbrake_path=handbrake,
        ffprobe_path=ffprobe,
        default_handbrake_profile="balanced",
    )

    plan = build_transcode_authorization_plan(
        store, HandBrakeProfileStore(tmp_path / "profiles.json"), config
    )
    public = plan.public_dict()

    assert plan.media_ids == ("media-1",)
    assert public["item_count"] == 1
    assert public["profile_display_name"] == "Resolution defaults (fallback: AMD VCN Balanced)"
    assert public["profile_selection"] == "source-resolution"
    assert str(output) not in json.dumps(public)
    assert str(handbrake) not in json.dumps(public)


def test_transcode_authorization_requires_a_ready_queue(tmp_path):
    config = Config(cache_dir=tmp_path / "cache")
    with pytest.raises(PipelineQueueError, match="No queued transcode"):
        build_transcode_authorization_plan(
            PipelineQueueStore(tmp_path / "pipeline.sqlite3"),
            HandBrakeProfileStore(tmp_path / "profiles.json"),
            config,
        )
