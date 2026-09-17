with open('tests/test_downstream_worker.py', 'a', encoding='utf-8') as f:
    f.write('''
def test_m5_end_to_end_tv_no_match_to_movie_route(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from mkv_episode_matcher.backend import unmatched_disc_analysis as analysis
    from mkv_episode_matcher.backend.automatic_rip import _resolve_automatic_unmatched_disc
    from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
    from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence
    from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry, TvShowCandidate
    from mkv_episode_matcher.backend.identification_dossier import UnmatchedFileEvidence
    from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
    from mkv_episode_matcher.backend.gemini_fallback import GeminiFallbackOutcome, GeminiTitleOutcome

    contracts = tmp_path / "contracts"
    contracts.mkdir(exist_ok=True)
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    fingerprint = "0123456789abcdef"

    assessment = store.routing_append(
        DiscAssessment(
            fingerprint,
            (0,),
            user_hint="movie",
            evidence=(TitleEvidence(0, "database", "supported", "tv"),),
        ),
        expected_revision=0,
    )

    media_id = f"disc-01-title-000"
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contract = contracts / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "media_id": media_id,
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "disc_fingerprint": fingerprint,
            "title_index": 0,
            "media_context": {
                "series_name": "Unmatched", 
                "season": None,
                "routing_assessment": assessment.to_dict(),
                "routing_assessment_digest": assessment.digest,
                "routing_assessment_revision": assessment.revision,
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "unmatched_disc_analysis_required")

    catalog = tuple(
        EpisodeCatalogEntry(
            f"S01E{episode:02d}", 1, episode, f"Title {episode}", "", 1200
        )
        for episode in range(1, 4)
    )

    monkeypatch.setattr(
        analysis,
        "search_tv_show_candidates",
        lambda _name: (TvShowCandidate(1, "Example", "", None, ""),),
    )
    monkeypatch.setattr(
        analysis, "fetch_aired_episode_catalog", lambda _show_id: catalog
    )
    monkeypatch.setattr(
        analysis, "existing_library_episodes", lambda *_args: frozenset()
    )

    class FakeDossier:
        def record_attempt(self, *_args, **_kwargs):
            pass

        def safe_attempts(self, _media_id):
            return ()

    monkeypatch.setattr(
        analysis,
        "collect_dossier_evidence",
        lambda items, *_args: (
            (UnmatchedFileEvidence(media_id, 1200, ("dialogue",)),),
            FakeDossier(),
        ),
    )

    def fake_rank(*_args, **_kwargs):
        return {media_id: SimpleNamespace(episode_id=None, confidence=0.9)}

    monkeypatch.setattr(analysis, "_rank_gemini_chunks", fake_rank)
    monkeypatch.setattr(
        analysis, "match_opensubtitles_seasons", lambda *_args, **_kwargs: ({}, {})
    )
    monkeypatch.setattr(
        analysis,
        "plan_disc_sequences",
        lambda *_args, **_kwargs: analysis._ReviewSequencePlan(),
    )
    monkeypatch.setattr(
        analysis, "discover_opensubtitles_season", lambda *_args, **_kwargs: ()
    )

    config = SimpleNamespace(
        gemini_model="gemini-test",
        automatic_processing_enabled=True,
        automatic_gemini_ambiguity_fallback=True,
        min_confidence=0.8,
    )
    
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(load=lambda: config),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_engine",
        lambda: SimpleNamespace(asr=object()),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root",
        lambda: contracts,
    )

    # 1. Run the automatic disc analysis entry point - REAL coordinator (which raises) 
    # caught by REAL exception handler (which preserves tv_title_no_match)
    _resolve_automatic_unmatched_disc((media_id,), store, config, contracts)

    # Verify that the tv_title_no_match result survived the outer exception handler
    assert store.get(media_id).review_code == "tv_title_no_match"

    def fake_gemini(_store, ids, _config, _asr, _root, *, return_outcomes):
        assert return_outcomes is True
        revised = tmp_path / "revised.json"
        revised_payload = json.loads(contract.read_text(encoding="utf-8"))
        revised_payload["media_context"]["special_feature_assignments"] = [
            {
                "title_index": 0,
                "classification": "matched-feature",
                "media_kind": "movie",
                "provisional_match": False,
            }
        ]
        revised.write_text(json.dumps(revised_payload), encoding="utf-8")
        _store.apply_reviewed_identification_input(
            media_id, build_artifact("rip", revised)
        )
        return GeminiFallbackOutcome(
            (media_id,), (GeminiTitleOutcome(media_id, "matched", "movie"),)
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback",
        fake_gemini,
    )

    # 2. Run the routing worker
    worker = DownstreamWorker(SimpleNamespace(store=store), allowed_stages=("identify",))
    assert worker._settle_terminal_tv_route() is True
    worker._apply_automatic_assessed_gemini_route()

    # Verify it routed to the movie route!
    final_item = store.get(media_id)
    assert final_item.state == "queued"
    assert final_item.review_code is None
    
    # Reload assessment to verify it transitioned
    attempts = store.routing_attempts(fingerprint, 0)
    assert len(attempts) == 2
    assert {(a.route, a.outcome) for a in attempts} == {
        ("tv", "no_match"),
        ("movie", "matched"),
    }
'''
    )
