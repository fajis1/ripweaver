import json
from pathlib import Path
from types import SimpleNamespace

from mkv_episode_matcher.backend import automatic_rip, downstream_worker
from mkv_episode_matcher.backend import unmatched_disc_analysis as analysis
from mkv_episode_matcher.backend.identification_dossier import (
    source_identity,
)
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiMatchResult,
    GeminiReviewPlan,
    UnmatchedFileEvidence,
)
from mkv_episode_matcher.pipeline_adapters import IdentifyStageAdapter
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
from mkv_episode_matcher.tmdb_client import TvShowCandidate


def test_source_identity_supports_non_disc_triage_items(tmp_path: Path):
    """SourceIdentity handles items without disc_fingerprint using media_id."""
    video_file = tmp_path / "triage_episode.mkv"
    video_file.write_bytes(b"dummy mkv contents for triage")
    file_size = video_file.stat().st_size

    payload = {
        "media_id": "triage-12345678abcdef00",
        "source_path": str(video_file),
        "source_size_bytes": file_size,
    }
    identity = source_identity(payload, video_file, "tiny")
    assert identity.disc_fingerprint is None
    assert identity.title_index is None
    assert identity.media_id == "triage-12345678abcdef00"
    digest = identity.digest()
    assert isinstance(digest, str) and len(digest) == 64


def test_pipeline_adapter_matches_assignment_by_media_id(tmp_path: Path):
    """IdentifyStageAdapter matches episode_assignments by media_id when title_index is absent."""
    video_file = tmp_path / "EUREKA 1.3_t07.mkv"
    video_file.write_bytes(b"dummy video data")

    media_id = "triage-d8166ac22151914f"
    contract_path = tmp_path / f"{media_id}.all-season-abc123.json"
    contract_payload = {
        "schema_version": 1,
        "mode": "verified-rip-contract",
        "media_id": media_id,
        "source_path": str(video_file),
        "source_size_bytes": video_file.stat().st_size,
        "media_context": {
            "series_name": "Eureka",
            "season": 1,
            "content_hint": "tv",
            "identification_policy_version": 4,
            "episode_assignment_source": "gemini-two-pass",
            "episode_assignments": [
                {
                    "media_id": media_id,
                    "season": 1,
                    "episode": 11,
                    "title": "H.O.U.S.E. Rules",
                    "episode_id": "S01E11",
                    "confidence": 0.95,
                    "evidence_source": "gemini-two-pass",
                }
            ],
        },
    }
    contract_path.write_text(json.dumps(contract_payload), encoding="utf-8")

    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract_path))
    item = store.get(media_id)

    engine = SimpleNamespace()
    identified_root = tmp_path / "identified_contracts"
    identified_root.mkdir()
    adapter = IdentifyStageAdapter(engine, identified_root)
    artifact = adapter(item)

    assert artifact.stage == "identify"
    identified_data = json.loads(artifact.contract_path.read_text(encoding="utf-8"))
    assert identified_data["mode"] == "identified-episode-contract"
    assert identified_data["episode_id"] == "S01E11"
    assert "H.O.U.S.E. Rules" in identified_data["library_relative"]
    assert identified_data["library_relative"].startswith("Eureka/Season 01/")


def test_automatic_season_tv_context_extracts_series_and_season(tmp_path: Path):
    """_automatic_season_tv_context reconciles TV context without a disc fingerprint."""
    media_id = "triage-test-item-01"
    contract = tmp_path / f"{media_id}.verified-rip.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "media_id": media_id,
            "media_context": {
                "series_name": "Eureka",
                "season": 1,
                "content_hint": "tv",
            },
        }),
        encoding="utf-8",
    )
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    held_item = store.get(media_id)

    series_name, season = automatic_rip._automatic_season_tv_context([held_item])
    assert series_name == "Eureka"
    assert season == 1


def test_execute_unmatched_season_analysis_with_gemini(tmp_path: Path, monkeypatch):
    """execute_unmatched_season_analysis resolves a triage item directly with Gemini within season."""
    video_file = tmp_path / "EUREKA 1.3_t07.mkv"
    video_file.write_bytes(b"dummy video data")

    media_id = "triage-d8166ac22151914f"
    contract_root = tmp_path / "contracts"
    contract_root.mkdir()
    contract_path = contract_root / f"{media_id}.verified-identify.json"
    contract_payload = {
        "schema_version": 1,
        "mode": "verified-rip-contract",
        "media_id": media_id,
        "source_path": str(video_file),
        "source_size_bytes": video_file.stat().st_size,
        "media_context": {
            "series_name": "Eureka",
            "season": 1,
            "content_hint": "tv",
        },
    }
    contract_path.write_text(json.dumps(contract_payload), encoding="utf-8")

    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract_path))
    store.hold_for_review(media_id, "episode_match_review")

    catalog = tuple(
        EpisodeCatalogEntry(
            season=1,
            episode=i,
            episode_id=f"S01E{i:02d}",
            title=f"Episode {i}" if i != 11 else "H.O.U.S.E. Rules",
            overview="Overview",
            runtime_seconds=2600.0,
        )
        for i in range(1, 13)
    )

    candidate = TvShowCandidate(
        tmdb_id=4620,
        name="Eureka",
        original_name="Eureka",
        first_air_year=2006,
        overview="Overview",
    )

    monkeypatch.setattr(
        analysis,
        "_resolve_series_catalog_details",
        lambda series, cfg, allow_gemini=True: (candidate, catalog),
    )
    monkeypatch.setattr(
        analysis,
        "collect_dossier_evidence",
        lambda items, cfg, asr, root: (
            (
                UnmatchedFileEvidence(
                    file_id=media_id,
                    duration_seconds=2600.0,
                    transcript_excerpts=("9-1-1 gotta fly Joe dropped a clip",),
                ),
            ),
            None,
        ),
    )

    fake_plan = GeminiReviewPlan(
        mode="gemini-unmatched-review-plan",
        model="gemini-2.5-flash",
        matches=(
            GeminiMatchResult(
                file_id=media_id,
                episode_id="S01E11",
                confidence=0.98,
                evidence=("dialogue matches episode 11",),
            ),
        ),
    )

    class FakeRanker:
        def __init__(self, *args, **kwargs):
            self.model = "gemini-2.5-flash"
            self.models = ("gemini-2.5-flash",)

        def rank_with_configured_keys(self, files, catalog, **kwargs):
            return fake_plan

    monkeypatch.setattr(analysis, "GeminiEpisodeRanker", FakeRanker)

    config = Config(
        min_confidence=0.70,
        gemini_model="gemini-2.5-flash",
        automatic_gemini_ambiguity_fallback=True,
    )

    applied = analysis.execute_unmatched_season_analysis(
        store,
        (media_id,),
        "Eureka",
        config,
        asr=None,
        contract_root=contract_root,
        season=1,
        allow_gemini=True,
    )

    assert applied == (media_id,)
    updated_item = store.get(media_id)
    assert updated_item.state == "queued"
    assert updated_item.stage == "identify"

    revised_contract = json.loads(
        updated_item.artifact.contract_path.read_text(encoding="utf-8")
    )
    assignments = revised_contract["media_context"]["episode_assignments"]
    assert len(assignments) == 1
    assert assignments[0]["media_id"] == media_id
    assert assignments[0]["episode_id"] == "S01E11"
    assert assignments[0]["season"] == 1
    assert assignments[0]["episode"] == 11
    assert assignments[0]["title"] == "H.O.U.S.E. Rules"


def test_downstream_worker_triggers_triage_analysis(tmp_path: Path, monkeypatch):
    """DownstreamWorker._apply_automatic_triage_analysis discovers stuck triage items and calls recovery."""
    media_id = "triage-stuck-001"
    contract_root = tmp_path / "contracts"
    contract_root.mkdir()
    contract_path = contract_root / f"{media_id}.verified-identify.json"
    contract_path.write_text(
        json.dumps({
            "schema_version": 1,
            "mode": "verified-rip-contract",
            "media_id": media_id,
            "source_path": str(tmp_path / "ep.mkv"),
            "source_size_bytes": 1000,
            "media_context": {
                "series_name": "Eureka",
                "season": 1,
                "content_hint": "tv",
            },
        }),
        encoding="utf-8",
    )

    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract_path))
    store.hold_for_review(media_id, "episode_match_review")

    dispatcher = SimpleNamespace(store=store)
    worker = downstream_worker.DownstreamWorker(
        dispatcher,
        allowed_stages=("identify", "transcode", "organize"),
    )

    called = []

    def fake_resolve(media_ids, st, cfg, cr):
        called.append(media_ids)

    monkeypatch.setattr(
        automatic_rip, "_resolve_automatic_unmatched_season", fake_resolve
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: contract_root,
    )

    config = Config(automatic_processing_enabled=True)
    monkeypatch.setattr(
        downstream_worker,
        "get_config_manager",
        lambda: SimpleNamespace(load=lambda: config),
    )

    handled = worker._apply_automatic_triage_analysis()
    assert handled is True
    assert len(called) == 1
    assert called[0] == (media_id,)

    # Second run should be skipped due to _automatic_triage_attempts
    handled_again = worker._apply_automatic_triage_analysis()
    assert handled_again is False


def test_execute_unmatched_season_analysis_falls_back_to_all_season(
    tmp_path: Path, monkeypatch
):
    """execute_unmatched_season_analysis falls back to all_series_catalog when within-season fails."""
    video_file = tmp_path / "EUREKA_misfiled.mkv"
    video_file.write_bytes(b"dummy video data")

    media_id = "triage-misfiled-001"
    contract_root = tmp_path / "contracts"
    contract_root.mkdir()
    contract_path = contract_root / f"{media_id}.verified-identify.json"
    contract_payload = {
        "schema_version": 1,
        "mode": "verified-rip-contract",
        "media_id": media_id,
        "source_path": str(video_file),
        "source_size_bytes": video_file.stat().st_size,
        "media_context": {
            "series_name": "Eureka",
            "season": 1,
            "content_hint": "tv",
        },
    }
    contract_path.write_text(json.dumps(contract_payload), encoding="utf-8")

    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract_path))
    store.hold_for_review(media_id, "episode_match_review")

    s1_catalog = [
        EpisodeCatalogEntry(
            season=1,
            episode=i,
            episode_id=f"S01E{i:02d}",
            title=f"S1 Episode {i}",
            overview="Overview",
            runtime_seconds=2600.0,
        )
        for i in range(1, 13)
    ]
    s2_catalog = [
        EpisodeCatalogEntry(
            season=2,
            episode=i,
            episode_id=f"S02E{i:02d}",
            title=f"S2 Episode {i}",
            overview="Overview",
            runtime_seconds=2600.0,
        )
        for i in range(1, 13)
    ]
    full_catalog = tuple(s1_catalog + s2_catalog)

    candidate = TvShowCandidate(
        tmdb_id=4620,
        name="Eureka",
        original_name="Eureka",
        first_air_year=2006,
        overview="Overview",
    )

    monkeypatch.setattr(
        analysis,
        "_resolve_series_catalog_details",
        lambda series, cfg, allow_gemini=True: (candidate, full_catalog),
    )
    monkeypatch.setattr(
        analysis,
        "collect_dossier_evidence",
        lambda items, cfg, asr, root: (
            (
                UnmatchedFileEvidence(
                    file_id=media_id,
                    duration_seconds=2600.0,
                    transcript_excerpts=("dialogue from season 2 episode 3",),
                ),
            ),
            None,
        ),
    )

    class FakeRanker:
        def __init__(self, *args, **kwargs):
            self.model = "gemini-2.5-flash"
            self.models = ("gemini-2.5-flash",)

        def rank_with_configured_keys(self, files, catalog, **kwargs):
            catalog_seasons = {entry.season for entry in catalog}
            if catalog_seasons == {1}:
                return GeminiReviewPlan(
                    mode="gemini-unmatched-review-plan",
                    model="gemini-2.5-flash",
                    matches=(
                        GeminiMatchResult(
                            file_id=media_id,
                            episode_id="S01E01",
                            confidence=0.30,
                            evidence=(),
                        ),
                    ),
                )
            return GeminiReviewPlan(
                mode="gemini-unmatched-review-plan",
                model="gemini-2.5-flash",
                matches=(
                    GeminiMatchResult(
                        file_id=media_id,
                        episode_id="S02E03",
                        confidence=0.96,
                        evidence=("dialogue matches S02E03",),
                    ),
                ),
            )

    monkeypatch.setattr(analysis, "GeminiEpisodeRanker", FakeRanker)

    config = Config(
        min_confidence=0.70,
        gemini_model="gemini-2.5-flash",
        automatic_gemini_ambiguity_fallback=True,
    )

    applied = analysis.execute_unmatched_season_analysis(
        store,
        (media_id,),
        "Eureka",
        config,
        asr=None,
        contract_root=contract_root,
        season=1,
        allow_gemini=True,
    )

    assert applied == (media_id,)
    updated_item = store.get(media_id)
    assert updated_item.state == "queued"
    assert updated_item.stage == "identify"

    revised_contract = json.loads(
        updated_item.artifact.contract_path.read_text(encoding="utf-8")
    )
    assignments = revised_contract["media_context"]["episode_assignments"]
    assert len(assignments) == 1
    assert assignments[0]["media_id"] == media_id
    assert assignments[0]["episode_id"] == "S02E03"
    assert assignments[0]["season"] == 2
    assert assignments[0]["episode"] == 3
