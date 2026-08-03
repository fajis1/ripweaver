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
    StageOutcome,
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


def test_movie_hint_routes_to_movie_identifier_before_tv_fallback(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    contracts.mkdir()

    class Engine:
        def process_path(self, *args, **kwargs):
            raise AssertionError("TV matcher must not run before the movie strategy")

    item = _queued_item(
        tmp_path,
        {
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "media_context": {
                "series_name": "Possible Movie",
                "season": 1,
                "content_hint": "movie",
            },
        },
    )

    with pytest.raises(
        PipelineReviewRequiredError, match="movie_identification_required"
    ):
        IdentifyStageAdapter(Engine(), contracts)(item)


def test_extras_hint_without_season_routes_to_feature_evidence(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    contracts.mkdir()

    class Engine:
        def process_path(self, *args, **kwargs):
            raise AssertionError("TV matcher must not run for movie extras")

    item = _queued_item(
        tmp_path,
        {
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "media_context": {
                "series_name": "Parent Trap II",
                "season": None,
                "content_hint": "extras",
            },
        },
    )

    with pytest.raises(
        PipelineReviewRequiredError, match="special_feature_evidence_required"
    ):
        IdentifyStageAdapter(Engine(), contracts)(item)


def test_identify_adapter_uses_reviewed_special_feature_assignment(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    contracts.mkdir()

    class Engine:
        def process_path(self, *args, **kwargs):
            raise AssertionError("Episode matcher must not run for a catalogued extra")

    item = _queued_item(
        tmp_path,
        {
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "media_context": {
                "series_name": "Unmatched",
                "content_hint": "extras",
                "special_feature_catalog_id": "reviewed-catalog",
                "special_feature_library_title": "Example Movie",
                "special_feature_library_year": 2001,
                "special_feature_assignments": [
                    {
                        "title_index": 7,
                        "classification": "matched-feature",
                        "matched_title": "Making Of",
                        "jellyfin_folder": "behind the scenes",
                        "fallback_name_policy": "none",
                    }
                ],
            },
        },
    )
    item = SimpleNamespace(**{**item.__dict__, "media_id": "disc-01-title-007"})

    artifact = IdentifyStageAdapter(Engine(), contracts)(item)
    payload = json.loads(artifact.contract_path.read_text(encoding="utf-8"))

    assert payload["episode_id"] is None
    assert payload["library_relative"] == (
        "Example Movie (2001)/behind the scenes/Making Of.mkv"
    )


def test_identify_adapter_uses_provisional_descriptive_movie_assignment(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    item = _queued_item(
        tmp_path,
        {
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "title_index": 0,
            "media_context": {
                "series_name": "Example Double Feature",
                "content_hint": "extras",
                "special_feature_library_title": "The Example Movie",
                "special_feature_library_year": 1961,
                "special_feature_assignments": [
                    {
                        "title_index": 0,
                        "classification": "matched-feature",
                        "matched_title": "The Example Movie",
                        "jellyfin_folder": "other",
                        "fallback_name_policy": "none",
                        "media_kind": "movie",
                        "provisional_match": True,
                        "gemini_confidence": 0.81,
                    }
                ],
            },
        },
    )

    artifact = IdentifyStageAdapter(SimpleNamespace(), contracts)(item)
    payload = json.loads(artifact.contract_path.read_text(encoding="utf-8"))

    assert payload["library_relative"] == (
        "The Example Movie (1961)/The Example Movie (1961).mkv"
    )
    assert payload["identification_order"] == ["gemini-descriptive-movie"]
    assert payload["provisional_match"] is True
    assert payload["confidence"] == 0.81


def test_identify_adapter_allows_provisional_extra_without_release_year(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    item = _queued_item(
        tmp_path,
        {
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "title_index": 3,
            "media_context": {
                "series_name": "Example Release",
                "content_hint": "extras",
                "special_feature_library_title": "Example Release",
                "special_feature_library_year": None,
                "special_feature_assignments": [
                    {
                        "title_index": 3,
                        "classification": "matched-feature",
                        "matched_title": "Production Featurette",
                        "jellyfin_folder": "other",
                        "fallback_name_policy": "none",
                        "media_kind": "extra",
                        "provisional_match": True,
                        "gemini_confidence": 0.65,
                    }
                ],
            },
        },
    )

    artifact = IdentifyStageAdapter(SimpleNamespace(), contracts)(item)
    payload = json.loads(artifact.contract_path.read_text(encoding="utf-8"))

    assert payload["library_relative"] == (
        "Example Release/other/Production Featurette.mkv"
    )


def test_identify_adapter_holds_ambiguous_special_feature_for_evidence(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    item = _queued_item(
        tmp_path,
        {
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "media_context": {
                "series_name": "Unmatched",
                "special_feature_assignments": [
                    {
                        "title_index": 7,
                        "classification": "ambiguous-match",
                        "fallback_name_policy": "content-fingerprint-required",
                    }
                ],
            },
        },
    )
    item = SimpleNamespace(**{**item.__dict__, "media_id": "disc-01-title-007"})

    with pytest.raises(
        PipelineReviewRequiredError, match="special_feature_evidence_required"
    ):
        IdentifyStageAdapter(SimpleNamespace(), contracts)(item)


def test_identify_adapter_uses_reviewed_cross_season_episode_assignment(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    item = _queued_item(
        tmp_path,
        {
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "title_index": 3,
            "media_context": {
                "series_name": "Faerie Tale Theatre",
                "season": None,
                "episode_assignments": [
                    {
                        "title_index": 3,
                        "season": 4,
                        "episode": 1,
                        "title": "The Three Little Pigs",
                    }
                ],
            },
        },
    )

    artifact = IdentifyStageAdapter(SimpleNamespace(), contracts)(item)
    payload = json.loads(artifact.contract_path.read_text(encoding="utf-8"))

    assert payload["episode_id"] == "S04E01"
    assert payload["library_relative"] == (
        "Faerie Tale Theatre/Season 04/"
        "Faerie Tale Theatre - S04E01 - The Three Little Pigs.mkv"
    )
    assert payload["identification_order"] == ["reviewed-release-catalogue"]


def test_identify_adapter_revises_an_obsolete_identification_contract(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    obsolete = contracts / "media-1.identify.json"
    obsolete.write_text(
        json.dumps({
            "mode": "identified-episode-contract",
            "media_id": "media-1",
            "episode_id": "S01E01",
            "library_relative": "Unmatched/Season 01/Unmatched - S01E01.mkv",
        }),
        encoding="utf-8",
    )
    item = _queued_item(
        tmp_path,
        {
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "title_index": 0,
            "media_context": {
                "series_name": "Faerie Tale Theatre",
                "season": None,
                "episode_assignments": [
                    {
                        "title_index": 0,
                        "season": 1,
                        "episode": 1,
                        "title": "The Tale of the Frog Prince",
                    }
                ],
            },
        },
    )

    adapter = IdentifyStageAdapter(SimpleNamespace(), contracts)
    artifact = adapter(item)
    repeated = adapter(item)
    payload = json.loads(artifact.contract_path.read_text(encoding="utf-8"))

    assert artifact.contract_path != obsolete
    assert artifact.contract_path == repeated.contract_path
    assert artifact.contract_path.name.startswith("media-1.identify-")
    assert payload["library_relative"] == (
        "Faerie Tale Theatre/Season 01/"
        "Faerie Tale Theatre - S01E01 - The Tale of the Frog Prince.mkv"
    )
    assert json.loads(obsolete.read_text(encoding="utf-8"))[
        "library_relative"
    ].startswith("Unmatched/")


def test_identify_adapter_refuses_placeholder_episode_assignment(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    item = _queued_item(
        tmp_path,
        {
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "title_index": 0,
            "media_context": {
                "series_name": "Unmatched",
                "season": 1,
                "episode_assignments": [
                    {
                        "title_index": 0,
                        "season": 1,
                        "episode": 1,
                        "title": "Episode 1",
                    }
                ],
            },
        },
    )

    with pytest.raises(
        PipelineReviewRequiredError, match="unmatched_disc_analysis_required"
    ):
        IdentifyStageAdapter(SimpleNamespace(), contracts)(item)


def test_identify_adapter_rechecks_pre_policy_all_season_assignment(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    contract = tmp_path / "media-1.all-season-old.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "title_index": 0,
            "media_context": {
                "series_name": "Faerie Tale Theatre",
                "season": None,
                "episode_assignments": [
                    {
                        "title_index": 0,
                        "season": 3,
                        "episode": 1,
                        "title": "Goldilocks and the Three Bears",
                    }
                ],
            },
        }),
        encoding="utf-8",
    )
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip("media-1", build_artifact("rip", contract))
    item = store.claim_next()

    with pytest.raises(
        PipelineReviewRequiredError, match="unmatched_disc_analysis_required"
    ):
        IdentifyStageAdapter(SimpleNamespace(), contracts)(item)


def test_unmatched_disc_routes_to_analysis_instead_of_missing_season(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    item = _queued_item(
        tmp_path,
        {
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "media_context": {"series_name": "Unmatched", "season": None},
        },
    )

    with pytest.raises(
        PipelineReviewRequiredError, match="unmatched_disc_analysis_required"
    ):
        IdentifyStageAdapter(SimpleNamespace(), contracts)(item)


def test_identification_holds_existing_episode_before_transcode(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contracts = tmp_path / "contracts"
    library = tmp_path / "library"
    season = library / "Test Show" / "Season 01"
    contracts.mkdir()
    season.mkdir(parents=True)
    (season / "Test Show - S01E01 - Existing - 720p.mkv").write_bytes(b"old")
    item = _queued_item(
        tmp_path,
        {
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "title_index": 0,
            "media_context": {
                "series_name": "Test Show",
                "episode_assignments": [
                    {"title_index": 0, "season": 1, "episode": 1, "title": "First"}
                ],
            },
        },
    )

    outcome = IdentifyStageAdapter(
        SimpleNamespace(), contracts, tv_library_root=library
    )(item)

    assert isinstance(outcome, StageOutcome)
    assert outcome.next_review_code == "library_collision"
    identified = json.loads(outcome.artifact.contract_path.read_text())
    assert identified["episode_id"] == "S01E01"


def test_transcode_refuses_old_placeholder_episode_contract(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"raw")
    contracts = tmp_path / "contracts"
    encoded_root = tmp_path / "encoded"
    run_root = tmp_path / "runs"
    for path in (contracts, encoded_root, run_root):
        path.mkdir()
    identify_contract = tmp_path / "identify.json"
    identify_contract.write_text(
        json.dumps({
            "mode": "identified-episode-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "episode_id": "S01E01",
            "library_relative": "Unmatched/Season 01/Unmatched - S01E01 - Episode 1.mkv",
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

    with pytest.raises(
        PipelineReviewRequiredError, match="placeholder_identification_required"
    ):
        TranscodeStageAdapter(
            handbrake=tmp_path / "HandBrakeCLI.exe",
            ffprobe=tmp_path / "ffprobe.exe",
            output_root=encoded_root,
            run_root=run_root,
            contract_root=contracts,
            profile=HandBrakeProfile(),
        )(store.claim_next())


def test_transcode_refuses_placeholder_filename_under_real_series(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"raw")
    contracts = tmp_path / "contracts"
    encoded_root = tmp_path / "encoded"
    run_root = tmp_path / "runs"
    for path in (contracts, encoded_root, run_root):
        path.mkdir()
    identify_contract = tmp_path / "identify.json"
    identify_contract.write_text(
        json.dumps({
            "mode": "identified-episode-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "episode_id": "S01E01",
            "library_relative": (
                "Faerie Tale Theatre/Season 01/Unmatched - S01E01 - Episode 1.mkv"
            ),
        }),
        encoding="utf-8",
    )
    item = QueuedPipelineItem(
        media_id="media-1",
        state="running",
        stage="transcode",
        artifact=build_artifact("identify", identify_contract),
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-08-01T00:00:00+00:00",
        error_type=None,
        review_code=None,
    )

    with pytest.raises(
        PipelineReviewRequiredError, match="placeholder_identification_required"
    ):
        TranscodeStageAdapter(
            handbrake=tmp_path / "HandBrakeCLI.exe",
            ffprobe=tmp_path / "ffprobe.exe",
            output_root=encoded_root,
            run_root=run_root,
            contract_root=contracts,
            profile=HandBrakeProfile(),
        )(item)


def test_organization_refuses_placeholder_episode_contract(tmp_path):
    encoded = tmp_path / "encoded.mkv"
    encoded.write_bytes(b"encoded")
    library = tmp_path / "library"
    contracts = tmp_path / "contracts"
    library.mkdir()
    contracts.mkdir()
    transcode_contract = tmp_path / "transcode.json"
    transcode_contract.write_text(
        json.dumps({
            "mode": "verified-transcode-contract",
            "encoded_path": str(encoded),
            "encoded_size_bytes": encoded.stat().st_size,
            "episode_id": "S01E01",
            "library_relative": (
                "Faerie Tale Theatre/Season 01/Unmatched - S01E01 - Episode 1.mkv"
            ),
        }),
        encoding="utf-8",
    )
    item = QueuedPipelineItem(
        media_id="media-1",
        state="running",
        stage="organize",
        artifact=build_artifact("transcode", transcode_contract),
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-08-01T00:00:00+00:00",
        error_type=None,
        review_code=None,
    )

    with pytest.raises(
        PipelineReviewRequiredError, match="placeholder_identification_required"
    ):
        OrganizeStageAdapter(
            library_root=library,
            contract_root=contracts,
            confirm_organize=True,
        )(item)

    assert encoded.read_bytes() == b"encoded"
    assert not any(library.rglob("*.mkv"))


def test_transcode_and_organization_adapters_link_verified_output(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"raw")
    contracts = tmp_path / "contracts"
    encoded_root = tmp_path / "encoded"
    run_root = tmp_path / "runs"
    library_root = tmp_path / "library"
    deletion_root = tmp_path / "ready-to-delete"
    for path in (contracts, encoded_root, run_root, library_root, deletion_root):
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
        deletion_staging_root=deletion_root,
    )(organize_item)
    final = store.complete_stage("media-1", "organize", organized_artifact)

    destination = (
        library_root / "Test Show/Season 01/Test Show - S01E01 - First - 1080p.mkv"
    )
    assert destination.read_bytes() == b"encoded"
    archived = deletion_root / "media-1" / "source.mkv"
    assert archived.read_bytes() == b"raw"
    assert not source.exists()
    organized_payload = json.loads(organized_artifact.contract_path.read_text())
    assert organized_payload["output_size_bytes"] == 7
    assert organized_payload["archived_source_size_bytes"] == 3
    assert final.state == "completed"


def test_transcode_retry_uses_new_attempt_when_prior_run_directory_exists(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"raw")
    contracts = tmp_path / "contracts"
    encoded_root = tmp_path / "encoded"
    run_root = tmp_path / "runs"
    for path in (contracts, encoded_root, run_root):
        path.mkdir()
    (run_root / "media-1-attempt-001").mkdir()
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
    item = QueuedPipelineItem(
        media_id="media-1",
        state="running",
        stage="transcode",
        artifact=build_artifact("identify", identify_contract),
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-08-01T00:00:00+00:00",
        error_type=None,
        review_code=None,
    )
    observed = {}

    def fake_handbrake(_executable, _ffprobe, job, run_dir, **_kwargs):
        observed["attempt"] = job.attempt_number
        observed["run_dir"] = run_dir.name
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
            width=720,
            height=480,
            field_order="progressive",
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.pipeline_adapters.execute_handbrake_job",
        fake_handbrake,
    )

    artifact = TranscodeStageAdapter(
        handbrake=tmp_path / "HandBrakeCLI.exe",
        ffprobe=tmp_path / "ffprobe.exe",
        output_root=encoded_root,
        run_root=run_root,
        contract_root=contracts,
        profile=HandBrakeProfile(),
    )(item)

    assert observed == {"attempt": 2, "run_dir": "media-1-attempt-002"}
    assert artifact.contract_path.is_file()


@pytest.mark.parametrize(
    ("override", "expected_encoder"),
    [(None, "x265"), ("balanced", "vce_h265")],
)
def test_transcode_uses_reviewed_profile_precedence(
    tmp_path, monkeypatch, override, expected_encoder
):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"raw")
    contracts = tmp_path / "contracts"
    encoded_root = tmp_path / "encoded"
    run_root = tmp_path / "runs"
    for path in (contracts, encoded_root, run_root):
        path.mkdir()
    identify_contract = tmp_path / "identify.json"
    identify_contract.write_text(
        json.dumps({
            "mode": "identified-episode-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "episode_id": "S01E01",
            "library_relative": "Show/Season 01/Show - S01E01 - First.mkv",
            "handbrake_profile_id": "cpu-balanced",
        }),
        encoding="utf-8",
    )
    item = QueuedPipelineItem(
        media_id="media-1",
        state="running",
        stage="transcode",
        artifact=build_artifact("identify", identify_contract),
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
        error_type=None,
        review_code=None,
    )
    observed = {}

    def fake_handbrake(_executable, _ffprobe, job, run_dir, **_kwargs):
        observed["profile"] = job.profile
        job.destination.write_bytes(b"encoded")
        return HandBrakeResult(
            job.media_id,
            job.profile.encoder,
            7,
            1200,
            "hevc",
            1,
            0,
            run_dir / "p",
            run_dir / "e",
            1920,
            1080,
            "progressive",
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.pipeline_adapters.execute_handbrake_job", fake_handbrake
    )
    TranscodeStageAdapter(
        handbrake=tmp_path / "HandBrakeCLI.exe",
        ffprobe=tmp_path / "ffprobe.exe",
        output_root=encoded_root,
        run_root=run_root,
        contract_root=contracts,
        profile=HandBrakeProfile(),
        profiles={
            "cpu-balanced": HandBrakeProfile(
                encoder="x265", encoder_preset="medium", quality=22
            )
        },
        profile_override_id=override,
    )(item)

    assert observed["profile"].encoder == expected_encoder


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


def test_transcode_holds_existing_library_episode_before_handbrake(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"raw")
    contracts = tmp_path / "contracts"
    encoded_root = tmp_path / "encoded"
    run_root = tmp_path / "runs"
    library_root = tmp_path / "library"
    season = library_root / "Test Show" / "Season 01"
    for path in (contracts, encoded_root, run_root, season):
        path.mkdir(parents=True)
    (season / "Test Show - S01E01 - Existing - 720p.mkv").write_bytes(b"existing")
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
    item = QueuedPipelineItem(
        media_id="media-1",
        state="running",
        stage="transcode",
        artifact=build_artifact("identify", identify_contract),
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-08-01T00:00:00+00:00",
        error_type=None,
        review_code=None,
    )

    def unexpected_handbrake(*_args, **_kwargs):
        raise AssertionError("HandBrake must not run for a library collision")

    monkeypatch.setattr(
        "mkv_episode_matcher.pipeline_adapters.execute_handbrake_job",
        unexpected_handbrake,
    )
    with pytest.raises(PipelineReviewRequiredError, match="library_collision"):
        TranscodeStageAdapter(
            handbrake=tmp_path / "HandBrakeCLI.exe",
            ffprobe=tmp_path / "ffprobe.exe",
            output_root=encoded_root,
            run_root=run_root,
            contract_root=contracts,
            profile=HandBrakeProfile(),
            tv_library_root=library_root,
        )(item)

    assert source.read_bytes() == b"raw"
    assert not any(encoded_root.rglob("*.mkv"))
    assert not any(run_root.iterdir())
