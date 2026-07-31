import json
from types import SimpleNamespace

import pytest

from mkv_episode_matcher.media.handbrake import HandBrakeProfile, HandBrakeResult
from mkv_episode_matcher.pipeline_adapters import (
    IdentifyStageAdapter,
    OrganizeStageAdapter,
    TranscodeStageAdapter,
)
from mkv_episode_matcher.pipeline_queue import (
    PipelineQueueStore,
    PipelineReviewRequiredError,
    QueuedPipelineItem,
    build_artifact,
)


def _queued_item(tmp_path, payload):
    contract = tmp_path / "verified-rip.json"
    contract.write_text(json.dumps(payload), encoding="utf-8")
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip("media-1", build_artifact("rip", contract))
    return store.claim_next()


def test_identify_adapter_runs_engine_in_dry_run_and_writes_handoff(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    observed = {}

    class Engine:
        def process_path(self, path, **kwargs):
            observed.update(kwargs)
            match = SimpleNamespace(
                confidence=0.91,
                episode_info=SimpleNamespace(
                    series_name="Test Show",
                    season=1,
                    episode=2,
                    title="Second",
                ),
            )
            return [match], []

    item = _queued_item(
        tmp_path,
        {
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "media_context": {"series_name": "Test Show", "season": 1, "tmdb_id": 7},
        },
    )
    artifact = IdentifyStageAdapter(Engine(), contracts)(item)

    assert observed["dry_run"] is True
    assert observed["files_override"] == [source]
    payload = json.loads(artifact.contract_path.read_text())
    assert payload["episode_id"] == "S01E02"
    assert payload["library_relative"].endswith("Test Show - S01E02 - Second.mkv")


def test_transcode_and_organization_adapters_link_verified_output(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"raw")
    contracts = tmp_path / "contracts"
    encoded_root = tmp_path / "encoded"
    run_root = tmp_path / "runs"
    library_root = tmp_path / "library"
    for path in (contracts, encoded_root, run_root, library_root):
        path.mkdir()
    identify_contract = tmp_path / "identify.json"
    identify_contract.write_text(
        json.dumps({
            "mode": "identified-episode-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "episode_id": "S01E01",
            "library_relative": "Test Show/Season 01/Test Show - S01E01 - First.mkv",
        }),
        encoding="utf-8",
    )
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    rip = tmp_path / "rip.json"
    rip.write_text("{}", encoding="utf-8")
    store.enqueue_verified_rip("media-1", build_artifact("rip", rip))
    store.claim_next()
    store.complete_stage(
        "media-1", "identify", build_artifact("identify", identify_contract)
    )
    transcode_item = store.claim_next()

    def fake_handbrake(_executable, _ffprobe, job, run_dir, **_kwargs):
        job.destination.write_bytes(b"encoded")
        return HandBrakeResult(
            media_id=job.media_id,
            encoder=job.profile.encoder,
            output_bytes=7,
            duration_seconds=1200,
            video_codec="hevc",
            audio_streams=2,
            subtitle_streams=0,
            process_log=run_dir / "process.log",
            event_log=run_dir / "events.jsonl",
            width=1920,
            height=1080,
            field_order="progressive",
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.pipeline_adapters.execute_handbrake_job",
        fake_handbrake,
    )
    transcode_artifact = TranscodeStageAdapter(
        handbrake=tmp_path / "HandBrakeCLI.exe",
        ffprobe=tmp_path / "ffprobe.exe",
        output_root=encoded_root,
        run_root=run_root,
        contract_root=contracts,
        profile=HandBrakeProfile(),
    )(transcode_item)
    store.complete_stage("media-1", "transcode", transcode_artifact)
    organize_item = store.claim_next()

    organized_artifact = OrganizeStageAdapter(
        library_root=library_root,
        contract_root=contracts,
        confirm_organize=True,
    )(organize_item)
    final = store.complete_stage("media-1", "organize", organized_artifact)

    destination = (
        library_root / "Test Show/Season 01/Test Show - S01E01 - First - 1080p.mkv"
    )
    assert destination.read_bytes() == b"encoded"
    assert final.state == "completed"


def test_organization_holds_existing_episode_until_keep_both_is_explicit(tmp_path):
    encoded = tmp_path / "encoded.mkv"
    encoded.write_bytes(b"new-version")
    contracts = tmp_path / "contracts"
    library = tmp_path / "library"
    season = library / "Test Show" / "Season 01"
    contracts.mkdir()
    season.mkdir(parents=True)
    (season / "Test Show - S01E01 - First - 720p.mkv").write_bytes(b"existing")
    contract = tmp_path / "transcode.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-transcode-contract",
            "media_id": "media-1",
            "encoded_path": str(encoded),
            "encoded_size_bytes": encoded.stat().st_size,
            "episode_id": "S01E01",
            "encoded_height": 1080,
            "encoded_field_order": "progressive",
            "library_relative": "Test Show/Season 01/Test Show - S01E01 - First.mkv",
        }),
        encoding="utf-8",
    )
    item = QueuedPipelineItem(
        media_id="media-1",
        state="running",
        stage="organize",
        artifact=build_artifact("transcode", contract),
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
        error_type=None,
        review_code=None,
    )

    with pytest.raises(PipelineReviewRequiredError, match="library_collision"):
        OrganizeStageAdapter(
            library_root=library,
            contract_root=contracts,
            confirm_organize=True,
        )(item)
    assert encoded.is_file()

    OrganizeStageAdapter(
        library_root=library,
        contract_root=contracts,
        confirm_organize=True,
        allow_version_coexistence=True,
    )(item)
    assert (
        season / "Test Show - S01E01 - First - 1080p.mkv"
    ).read_bytes() == b"new-version"
