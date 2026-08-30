import json
from types import SimpleNamespace

import pytest

from mkv_episode_matcher.backend import unmatched_disc_analysis as analysis
from mkv_episode_matcher.backend.identification_dossier import (
    IdentificationDossierStore,
)
from mkv_episode_matcher.backend.routers import rip as rip_router
from mkv_episode_matcher.core.credentials import ApiCredentialError, ApiServiceError
from mkv_episode_matcher.core.models import EpisodeInfo, SubtitleFile
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiMatchResult,
    GeminiResponseError,
    GeminiReviewPlan,
    UnmatchedFileEvidence,
)
from mkv_episode_matcher.media.gemini_series_resolver import GeminiSeriesResolution
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact
from mkv_episode_matcher.tmdb_client import TvShowCandidate


def test_disc_identification_audit_returns_complete_trace_only_on_demand(
    tmp_path, monkeypatch
):
    fingerprint = "0123456789abcdef"
    media_id = f"disc-01-{fingerprint}-title-001"
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    contract = contracts / f"{media_id}.verified-rip.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "media_id": media_id,
            "disc_fingerprint": fingerprint,
            "title_index": 1,
        }),
        encoding="utf-8",
    )
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    dossier = IdentificationDossierStore(tmp_path / "identification-evidence")
    dossier.record_workflow_event(
        (media_id,),
        analysis_run_id="b" * 32,
        phase="analysis-started",
        disposition="started",
        summary={"title_count": 1},
    )
    monkeypatch.setattr(rip_router, "get_pipeline_contract_root", lambda: contracts)
    monkeypatch.setattr(
        rip_router,
        "_pipeline_item_response",
        lambda _item: {
            "disc_fingerprint": fingerprint,
            "title_index": 1,
            "display_name": "Synthetic title",
            "match_summary": None,
        },
    )

    report = rip_router.get_disc_identification_audit(
        fingerprint,
        store,
        OrchestrationStore(tmp_path / "orchestration.sqlite3"),
    )

    assert report["path_redacted"] is True
    assert report["dialogue_redacted"] is True
    assert report["rip_jobs"] == []
    assert len(report["titles"]) == 1
    assert report["titles"][0]["media_id"] == media_id
    assert report["titles"][0]["pipeline_events"][0]["event_type"] == (
        "verified_rip_queued"
    )
    assert report["titles"][0]["identification_audit"][0]["phase"] == (
        "analysis-started"
    )


def test_residual_subtitle_pass_selects_strong_remaining_episode():
    episode_12 = EpisodeCatalogEntry("S02E12", 2, 12, "The Injury", "", 1200)
    episode_15 = EpisodeCatalogEntry("S02E15", 2, 15, "Boys and Girls", "", 1200)
    details = {}

    accepted = analysis._accept_residual_subtitle_candidates(
        {"title-004": [(0.76, 1, episode_12), (0.20, 0, episode_15)]},
        0.70,
        set(),
        diagnostic_details=details,
    )

    assert accepted == {"title-004": episode_12}
    assert details["title-004"]["reason"] == ("accepted_after_disc_residual_reduction")


def test_gemini_disc_batch_sends_five_unresolved_titles_in_one_call():
    files = tuple(
        UnmatchedFileEvidence(f"title-{index}", 1200, (f"dialogue {index}",))
        for index in range(5)
    )
    catalog = tuple(
        EpisodeCatalogEntry(
            f"S04E{index + 1:02d}", 4, index + 1, f"Episode {index + 1}", "", 1200
        )
        for index in range(5)
    )

    class Dossier:
        @staticmethod
        def safe_attempts(_file_id):
            return ()

    class Ranker:
        model = "gemini-test"

        def __init__(self):
            self.calls = []

        def rank_with_configured_keys(self, supplied_files, supplied_catalog, **kwargs):
            self.calls.append((supplied_files, supplied_catalog, kwargs))
            return SimpleNamespace(
                matches=tuple(
                    SimpleNamespace(
                        file_id=item.file_id,
                        episode_id=entry.episode_id,
                        confidence=0.9,
                    )
                    for item, entry in zip(
                        supplied_files, supplied_catalog, strict=True
                    )
                )
            )

    ranker = Ranker()
    matches = analysis._rank_gemini_chunks(
        ranker,
        files,
        catalog,
        Dossier(),
        frozenset(),
    )

    assert set(matches) == {item.file_id for item in files}
    assert len(ranker.calls) == 1
    assert len(ranker.calls[0][0]) == 5


def test_gemini_disc_batch_reuses_private_exact_request_cache(tmp_path):
    files = (UnmatchedFileEvidence("title-1", 1200, ("dialogue",)),)
    catalog = (EpisodeCatalogEntry("S04E11", 4, 11, "Eleven", "", 1200),)
    dossier = IdentificationDossierStore(tmp_path / "private")

    class Ranker:
        model = "gemini-test"

        def __init__(self):
            self.calls = 0

        def rank_with_configured_keys(
            self,
            supplied_files,
            _catalog,
            *,
            prior_attempts,
            existing_episode_ids,
            review_phase,
            proposed_assignments=None,
            subtitle_comparisons=None,
        ):
            self.calls += 1
            return GeminiReviewPlan(
                "gemini-unmatched-review-plan",
                self.model,
                (
                    GeminiMatchResult(
                        supplied_files[0].file_id,
                        "S04E11",
                        0.9,
                        ("paired scenes",),
                    ),
                ),
            )

    ranker = Ranker()
    first = analysis._rank_gemini_chunks(ranker, files, catalog, dossier, frozenset())
    second = analysis._rank_gemini_chunks(ranker, files, catalog, dossier, frozenset())

    assert first == second
    assert ranker.calls == 1


def test_gemini_disc_batch_reuses_cache_from_configured_fallback_model(tmp_path):
    files = (UnmatchedFileEvidence("title-1", 1200, ("dialogue",)),)
    catalog = (EpisodeCatalogEntry("S04E11", 4, 11, "Eleven", "", 1200),)
    dossier = IdentificationDossierStore(tmp_path / "private")

    class Ranker:
        model = "gemini-primary"
        models = ("gemini-primary", "gemini-fallback")

        def __init__(self):
            self.calls = 0

        def rank_with_configured_keys(
            self,
            supplied_files,
            _catalog,
            *,
            prior_attempts,
            existing_episode_ids,
            review_phase,
            proposed_assignments=None,
            subtitle_comparisons=None,
        ):
            self.calls += 1
            return GeminiReviewPlan(
                "gemini-unmatched-review-plan",
                "gemini-fallback",
                (
                    GeminiMatchResult(
                        supplied_files[0].file_id,
                        "S04E11",
                        0.9,
                        ("paired scenes",),
                    ),
                ),
            )

    ranker = Ranker()
    first = analysis._rank_gemini_chunks(ranker, files, catalog, dossier, frozenset())
    second = analysis._rank_gemini_chunks(ranker, files, catalog, dossier, frozenset())

    assert first == second
    assert ranker.calls == 1


def test_gemini_provider_recorder_is_not_part_of_request_digest(tmp_path):
    files = (UnmatchedFileEvidence("title-1", 1200, ("dialogue",)),)
    catalog = (EpisodeCatalogEntry("S07E20", 7, 20, "Twenty", "", 1200),)
    dossier = IdentificationDossierStore(tmp_path / "private")

    class Ranker:
        model = "gemini-test"

        def __init__(self):
            self.recorder = None

        def rank_with_configured_keys(
            self,
            supplied_files,
            _catalog,
            *,
            prior_attempts,
            existing_episode_ids,
            review_phase,
            transaction_recorder,
        ):
            self.recorder = transaction_recorder
            return GeminiReviewPlan(
                "gemini-unmatched-review-plan",
                self.model,
                (
                    GeminiMatchResult(
                        supplied_files[0].file_id,
                        "S07E20",
                        0.9,
                        ("paired scenes",),
                    ),
                ),
            )

    ranker = Ranker()
    matches = analysis._rank_gemini_chunks(
        ranker,
        files,
        catalog,
        dossier,
        frozenset(),
        analysis_run_id="0123456789abcdef0123456789abcdef",
    )

    assert matches["title-1"].episode_id == "S07E20"
    assert callable(ranker.recorder)


def test_matching_telemetry_omits_non_provider_audit_branches():
    dossier = SimpleNamespace(
        safe_attempts=lambda _media_id: (
            {"branch": "tv-local"},
            {"branch": "tv-disc-range"},
            {"branch": "visual-ocr"},
            {"branch": "tv-opensubtitles"},
        )
    )

    assert analysis._matching_provider_branches(dossier, ("title-1",)) == (
        "tv-local",
        "tv-opensubtitles",
    )


def test_gemini_uses_six_diverse_excerpts_and_paired_subtitle_passages():
    excerpts = tuple(
        f"speaker{index} topic{index} location{index} action{index}"
        for index in range(12)
    )
    files = (UnmatchedFileEvidence("title-1", 1200, excerpts),)
    reference = SimpleNamespace(
        episode_info=SimpleNamespace(season=4, episode=11),
        content=" ".join(
            f"speaker{index} topic{index} location{index} action{index} continues"
            for index in range(12)
        ),
        path=None,
    )
    evaluations = {
        "title-1": [
            {
                "phase": "offset-six-window-retry",
                "rank": 1,
                "candidate_episode_id": "S04E11",
            }
        ]
    }

    class ASR:
        @staticmethod
        def calculate_match_score(query, candidate):
            return 0.9 if query in candidate else 0.1

    comparisons = analysis._gemini_subtitle_comparisons(
        files, (reference,), evaluations, ASR()
    )
    selected = analysis._select_gemini_file_evidence(files, comparisons)

    assert len(comparisons["title-1"]) == 2
    assert all(pair.candidate_episode_id == "S04E11" for pair in comparisons["title-1"])
    assert len(selected[0].transcript_excerpts) == 6


def test_gemini_subtitle_comparisons_follow_final_unassigned_catalog():
    comparisons = {
        "title-1": (
            analysis.GeminiSubtitleComparisonEvidence(
                "S07E21", "assigned dialogue", "assigned reference", 0.9
            ),
            analysis.GeminiSubtitleComparisonEvidence(
                "S07E22", "remaining dialogue", "remaining reference", 0.8
            ),
        )
    }
    remaining_catalog = (EpisodeCatalogEntry("S07E22", 7, 22, "Episode 22", "", 1200),)

    filtered = analysis._filter_gemini_subtitle_comparisons(
        comparisons, remaining_catalog
    )

    assert tuple(pair.candidate_episode_id for pair in filtered["title-1"]) == (
        "S07E22",
    )


def test_residual_subtitle_pass_preserves_close_or_weak_choices_for_review():
    episode_12 = EpisodeCatalogEntry("S02E12", 2, 12, "The Injury", "", 1200)
    episode_15 = EpisodeCatalogEntry("S02E15", 2, 15, "Boys and Girls", "", 1200)

    assert (
        analysis._accept_residual_subtitle_candidates(
            {"close": [(0.76, 1, episode_12), (0.60, 1, episode_15)]},
            0.70,
            set(),
        )
        == {}
    )
    assert (
        analysis._accept_residual_subtitle_candidates(
            {"weak": [(0.45, 1, episode_12), (0.10, 0, episode_15)]},
            0.70,
            set(),
        )
        == {}
    )


def test_disc_episode_range_fence_uses_anchors_without_title_order():
    catalog = tuple(
        EpisodeCatalogEntry(
            f"S04E{episode:02d}", 4, episode, f"Episode {episode}", "", 1200
        )
        for episode in range(1, 15)
    )
    anchors = tuple(catalog[episode - 1] for episode in range(7, 13))

    fence = analysis._build_disc_episode_range_fence(anchors, catalog, 7)

    assert fence is not None
    assert fence.scope == "S04E06-E13"
    assert fence.permits(catalog[12]) is True
    assert fence.permits(catalog[3]) is False
    assert fence.permits(catalog[13]) is False
    assert analysis._build_disc_episode_range_fence(anchors[:1], catalog, 7) is None


def test_disc_number_prioritizes_expected_title_count_window_without_exclusion():
    catalog = tuple(
        EpisodeCatalogEntry(
            f"S05E{episode:02d}", 5, episode, f"Episode {episode}", "", 1200
        )
        for episode in range(1, 25)
    )

    disc_two = analysis._prioritize_catalog_for_disc_number(catalog, 2, 6)
    disc_four = analysis._prioritize_catalog_for_disc_number(catalog, 4, 6)

    assert {entry.episode_id for entry in disc_two} == {
        entry.episode_id for entry in catalog
    }
    assert tuple(entry.episode for entry in disc_two[:6]) == tuple(range(7, 13))
    assert tuple(entry.episode for entry in disc_four[:6]) == tuple(range(19, 25))
    assert disc_two.index(catalog[0]) > disc_two.index(catalog[6])


def test_saved_superfan_context_is_retained_only_for_subtitle_lookup():
    selected = [
        (
            SimpleNamespace(media_id="title-1"),
            {"media_context": {"series_name": "The Office Superfan Episodes"}},
        )
    ]

    lookup_name, release_profile = analysis._subtitle_lookup_series_name(
        "The Office", selected
    )

    assert lookup_name == "The Office Superfan Episodes"
    assert release_profile == "superfan"


def test_reviewed_release_context_survives_canonical_series_resolution():
    selected = [
        (
            SimpleNamespace(media_id="title-1"),
            {"media_context": {"series_name": "The Office"}},
        )
    ]

    lookup_name, release_profile = analysis._subtitle_lookup_series_name(
        "The Office",
        selected,
        reviewed_series_name="The Office Superfan Episodes",
    )

    assert lookup_name == "The Office Superfan Episodes"
    assert release_profile == "superfan"


@pytest.mark.parametrize(
    ("reviewed_name", "expected_name", "expected_profile"),
    (
        ("Example Extended", "Example Extended", "extended"),
        ("Example Unrated", "Example Unrated", "unrated"),
        ("Example Supercut", "Example Supercut", "supercut"),
        ("Example Director's Cut", "Example Director's Cut", "directors_cut"),
    ),
)
def test_reviewed_altered_release_profiles_are_retained_for_subtitle_lookup(
    reviewed_name: str,
    expected_name: str,
    expected_profile: str,
):
    lookup_name, release_profile = analysis._subtitle_lookup_series_name(
        "Example",
        [],
        reviewed_series_name=reviewed_name,
    )

    assert lookup_name == expected_name
    assert release_profile == expected_profile


def test_release_lookup_does_not_duplicate_existing_edition_suffix():
    lookup_name, release_profile = analysis._subtitle_lookup_series_name(
        "The Office Superfan Episodes",
        [],
    )

    assert lookup_name == "The Office Superfan Episodes"
    assert release_profile == "superfan"


def test_reviewed_episode_range_only_narrows_candidate_catalog():
    catalog = tuple(
        EpisodeCatalogEntry(
            f"S07E{episode:02d}", 7, episode, f"Episode {episode}", "", 1200
        )
        for episode in range(18, 27)
    ) + (EpisodeCatalogEntry("S08E20", 8, 20, "Other season", "", 1200),)

    scoped = analysis._reviewed_episode_catalog(catalog, 7, (20, 24))

    assert tuple(entry.episode_id for entry in scoped) == (
        "S07E20",
        "S07E21",
        "S07E22",
        "S07E23",
        "S07E24",
    )
    with pytest.raises(analysis.PipelineQueueError, match="requires a season"):
        analysis._reviewed_episode_catalog(catalog, None, (20, 24))


def test_gemini_disc_coherence_rejects_split_s4_d3_assignments():
    entries = tuple(
        EpisodeCatalogEntry(
            f"S04E{episode:02d}", 4, episode, f"Episode {episode}", "", 1200
        )
        for episode in (12, 11, 1, 2, 3, 4)
    )

    assert not analysis._gemini_disc_assignments_coherent(
        entries, disc_title_count=6, required_season=4
    )
    assert analysis._gemini_disc_assignments_coherent(
        tuple(entries[:2]), disc_title_count=6, required_season=4
    )


def test_gemini_disc_coherence_is_required_without_saved_season_context():
    scattered = tuple(
        EpisodeCatalogEntry(
            f"S08E{episode:02d}", 8, episode, f"Episode {episode}", "", 1200
        )
        for episode in (4, 7, 8, 12, 14, 16, 17, 19)
    )

    assert not analysis._gemini_disc_assignments_coherent(
        scattered, disc_title_count=8, required_season=None
    )
    assert analysis._gemini_disc_assignments_coherent(
        scattered[:3], disc_title_count=8, required_season=None
    )


def test_disc_range_restart_anchors_require_independent_provenance(tmp_path):
    fingerprint = "0123456789abcdef"
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    with store._connect() as connection:
        connection.executemany(
            """
            INSERT INTO disc_title_history (
                disc_fingerprint, title_index, outcome_name, library_relative,
                episode_id, classification, match_source,
                assignment_evidence_source, identification_policy_version,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    fingerprint,
                    1,
                    "Example - S04E07.mkv",
                    "Example/Season 04/Example - S04E07.mkv",
                    "S04E07",
                    "episode",
                    "local_evidence",
                    "opensubtitles-two-window",
                    4,
                    "2026-08-12T00:00:00+00:00",
                ),
                (
                    fingerprint,
                    2,
                    "Example - S04E08.mkv",
                    "Example/Season 04/Example - S04E08.mkv",
                    "S04E08",
                    "episode",
                    "local_evidence",
                    "opensubtitles-residual-elimination",
                    4,
                    "2026-08-12T00:00:00+00:00",
                ),
                (
                    fingerprint,
                    3,
                    "Example - S01E01.mkv",
                    "Example/Season 01/Example - S01E01.mkv",
                    "S01E01",
                    "episode",
                    "local_evidence",
                    "opensubtitles-two-window",
                    3,
                    "2026-08-12T00:00:00+00:00",
                ),
            ),
        )
    catalog = tuple(
        EpisodeCatalogEntry(episode_id, season, episode, episode_id, "", 1200)
        for episode_id, season, episode in (
            ("S01E01", 1, 1),
            ("S04E07", 4, 7),
            ("S04E08", 4, 8),
        )
    )

    anchors = analysis._history_disc_range_anchors(
        store, fingerprint, {entry.episode_id: entry for entry in catalog}
    )

    assert tuple(entry.episode_id for entry in anchors) == ("S04E07", "S04E08")


def test_residual_matching_requires_prior_or_current_confident_assignment(tmp_path):
    episode_12 = EpisodeCatalogEntry("S02E12", 2, 12, "The Injury", "", 1200)
    episode_15 = EpisodeCatalogEntry("S02E15", 2, 15, "Boys and Girls", "", 1200)
    catalog = (episode_12, episode_15)
    files = (UnmatchedFileEvidence("title-004", 1200, ("grill dialogue",)),)

    class Provider:
        def get_subtitles(self, _series, _season, _files, _tmdb_id):
            results = []
            for episode, content in ((12, "grill dialogue"), (15, "other dialogue")):
                path = tmp_path / f"episode-{episode}.srt"
                path.write_text("", encoding="utf-8")
                results.append(
                    SubtitleFile(
                        path=path,
                        content=content,
                        episode_info=EpisodeInfo(
                            series_name="Example", season=2, episode=episode
                        ),
                    )
                )
            return results

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.76 if transcript in reference else 0.20

    without_reduction, _diagnostics = analysis.match_opensubtitles_seasons(
        files,
        catalog,
        "Example",
        1,
        (2,),
        ASR(),
        min_confidence=0.7,
        provider=Provider(),
    )
    details = {}
    with_reduction, _diagnostics = analysis.match_opensubtitles_seasons(
        files,
        catalog,
        "Example",
        1,
        (2,),
        ASR(),
        min_confidence=0.7,
        provider=Provider(),
        diagnostic_details=details,
        prior_disc_assignments=True,
    )

    assert without_reduction == {}
    assert with_reduction == {"title-004": episode_12}
    assert details["title-004"]["reason"] == ("accepted_after_disc_residual_reduction")


def test_scene_description_is_used_for_episode_shortlist_and_gemini():
    files = (UnmatchedFileEvidence("title-005", 1200, ("unhelpful dialogue",)),)
    catalog = (
        EpisodeCatalogEntry(
            "S02E12",
            2,
            12,
            "The Injury",
            "Michael burns his foot on a grill and Dwight rushes to help.",
            1200,
        ),
        EpisodeCatalogEntry(
            "S02E19",
            2,
            19,
            "Michael's Birthday",
            "The staff tries to distract Kevin while awaiting medical results.",
            1200,
        ),
    )
    notes = {"title-005": "Michael burns his foot on a grill."}

    shortlisted = analysis._shortlist_catalog(
        files,
        catalog,
        set(),
        top_k=1,
        reviewer_scene_descriptions=notes,
    )

    assert tuple(item.episode_id for item in shortlisted) == ("S02E12",)

    class Ranker:
        def rank_with_configured_keys(self, ranked_files, ranked_catalog, **kwargs):
            assert ranked_files == files
            assert tuple(item.episode_id for item in ranked_catalog) == (
                "S02E12",
                "S02E19",
            )
            assert kwargs["reviewer_scene_descriptions"] == notes
            return SimpleNamespace(
                matches=(
                    SimpleNamespace(
                        file_id="title-005", episode_id="S02E12", confidence=0.95
                    ),
                )
            )

    class Dossier:
        @staticmethod
        def safe_attempts(_media_id):
            return ()

    matched = analysis._rank_gemini_chunks(
        Ranker(), files, catalog, Dossier(), frozenset(), notes
    )

    assert matched["title-005"].episode_id == "S02E12"


def test_failed_analysis_records_safe_failure_telemetry(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")

    with pytest.raises(analysis.PipelineQueueError, match="At least one"):
        analysis.execute_unmatched_disc_analysis(
            store,
            "0123456789abcdef",
            "Example Series",
            SimpleNamespace(),
            SimpleNamespace(),
            tmp_path / "contracts",
        )

    record = store.matching_performance()[0]
    assert record["outcome"] == "failed"
    assert record["failure_stage"] == "selection"
    assert record["failure_code"] == "insufficient_held_titles"
    assert record["title_count"] == 0


@pytest.mark.parametrize(
    ("scene_guided", "expected_content_fallback"), ((False, True), (True, False))
)
def test_disc_route_scopes_content_fallback_for_scene_guided_review(
    tmp_path, monkeypatch, scene_guided, expected_content_fallback
):
    fingerprint = "0123456789abcdef"
    media_id = f"Example--disc-01-{fingerprint}-title-000"
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contract_root = tmp_path / "contracts"
    contract_root.mkdir()
    contract = contract_root / "rip.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "media_id": media_id,
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "disc_fingerprint": fingerprint,
            "title_index": 0,
            "media_context": {
                "series_name": "The Office Superfan Episodes S1",
                "season": None,
            },
        }),
        encoding="utf-8",
    )
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.claim_next()
    store.require_review(media_id, "all_season_sequence_review_required")
    captured = {}

    def fake_analysis(*_args, **kwargs):
        captured["series_name"] = _args[2]
        captured.update(kwargs)
        return ()

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(rip_router, "execute_unmatched_disc_analysis", fake_analysis)
    monkeypatch.setattr(rip_router.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        rip_router,
        "_pipeline_item_response",
        lambda _item: {"disc_fingerprint": fingerprint},
    )
    monkeypatch.setattr(
        rip_router,
        "get_config_manager",
        lambda: SimpleNamespace(load=lambda: SimpleNamespace()),
    )
    monkeypatch.setattr(
        rip_router, "get_engine", lambda: SimpleNamespace(asr=SimpleNamespace())
    )

    result = rip_router.analyze_unmatched_disc(
        rip_router.UnmatchedDiscAnalysisRequest(
            disc_fingerprint=fingerprint,
            series_name="The Office",
            season=7,
            episode_start=20,
            episode_end=24,
            confirm_media_read=True,
            confirm_provider_lookup=True,
            confirm_external_fallback=True,
            reviewer_scene_descriptions=(
                {media_id: "Michael burns his foot on a countertop grill."}
                if scene_guided
                else {}
            ),
        ),
        store,
        contract_root,
    )

    assert result["started"] is True
    assert result["series_name"] == "The Office"
    assert captured["series_name"] == "The Office"
    assert captured["episode_range"] == (20, 24)
    assert captured["allow_content_fallback"] is expected_content_fallback
    assert captured["reviewer_scene_descriptions"] == (
        {media_id: "Michael burns his foot on a countertop grill."}
        if scene_guided
        else {}
    )
    assert result["reviewed_episode_range"] == [20, 24]
    assert result["reviewer_scene_description_count"] == int(scene_guided)


def test_disc_route_does_not_select_stale_review_lineage(tmp_path):
    fingerprint = "0123456789abcdef"
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")

    for position, media_id in enumerate(("older-title", "newer-title")):
        source = tmp_path / f"source-{position}.mkv"
        source.write_bytes(b"synthetic")
        contract = contracts / f"rip-{position}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "media_id": media_id,
                "source_path": str(source),
                "source_size_bytes": source.stat().st_size,
                "disc_fingerprint": fingerprint,
                "title_index": 4,
                "media_context": {"series_name": "Example", "season": 7},
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        if position == 0:
            store.claim_next()
            store.require_review(media_id, "all_season_sequence_review_required")

    with pytest.raises(rip_router.HTTPException) as exc_info:
        rip_router.analyze_unmatched_disc(
            rip_router.UnmatchedDiscAnalysisRequest(
                disc_fingerprint=fingerprint,
                series_name="Example",
                season=7,
                episode_start=20,
                episode_end=20,
                confirm_media_read=True,
                confirm_provider_lookup=True,
            ),
            store,
            contracts,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "No unmatched disc titles are ready"


def test_analyzer_does_not_select_stale_review_lineage(tmp_path):
    fingerprint = "0123456789abcdef"
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")

    for position, media_id in enumerate(("older-title", "newer-title")):
        source = tmp_path / f"source-{position}.mkv"
        source.write_bytes(b"synthetic")
        contract = contracts / f"rip-{position}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "media_id": media_id,
                "source_path": str(source),
                "source_size_bytes": source.stat().st_size,
                "disc_fingerprint": fingerprint,
                "title_index": 4,
                "media_context": {"series_name": "Example", "season": 7},
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        if position == 0:
            store.claim_next()
            store.require_review(media_id, "all_season_sequence_review_required")

    assert analysis._selected_analysis_items(store, fingerprint) == []

    store.claim_next()
    store.require_review("newer-title", "all_season_sequence_review_required")
    selected = analysis._selected_analysis_items(store, fingerprint)
    assert [item.media_id for item, _payload in selected] == ["newer-title"]


def test_disc_release_profile_uses_latest_sibling_context(tmp_path):
    fingerprint = "0123456789abcdef"
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")

    for title_index, series_name in ((3, "The Office"), (5, "The Office Superfan")):
        media_id = f"title-{title_index}"
        source = tmp_path / f"source-{title_index}.mkv"
        source.write_bytes(b"synthetic")
        contract = contracts / f"rip-{title_index}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "media_id": media_id,
                "source_path": str(source),
                "source_size_bytes": source.stat().st_size,
                "disc_fingerprint": fingerprint,
                "title_index": title_index,
                "media_context": {"series_name": series_name, "season": 7},
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))

    lookup_name, profile = analysis._subtitle_lookup_series_name(
        "The Office", analysis._latest_disc_title_items(store, fingerprint)
    )

    assert lookup_name == "The Office Superfan Episodes"
    assert profile == "superfan"


def test_last_held_title_can_use_prior_disc_assignments(tmp_path, monkeypatch):
    fingerprint = "0123456789abcdef"
    media_id = f"Example--disc-01-{fingerprint}-title-005"
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic")
    contract = contracts / "rip.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "media_id": media_id,
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "disc_fingerprint": fingerprint,
            "title_index": 5,
            "media_context": {"series_name": "Example", "season": 2},
        }),
        encoding="utf-8",
    )
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.claim_next()
    store.require_review(media_id, "all_season_sequence_review_required")
    store.choose_review_path(media_id, "all_season_analysis_running")
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO disc_title_history (
                disc_fingerprint, title_index, outcome_name,
                library_relative, episode_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                4,
                "Example - S02E11 - Prior.mkv",
                "Example/Season 02/Example - S02E11 - Prior.mkv",
                "S02E11",
                "2026-08-11T00:00:00+00:00",
            ),
        )
    catalog = (
        EpisodeCatalogEntry("S02E11", 2, 11, "Prior", "", 1200),
        EpisodeCatalogEntry("S02E12", 2, 12, "The Injury", "", 1200),
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

    class Dossier:
        @staticmethod
        def record_attempt(*_args, **_kwargs):
            return None

        @staticmethod
        def safe_attempts(_media_id):
            return ()

    monkeypatch.setattr(
        analysis,
        "collect_dossier_evidence",
        lambda items, *_args: (
            tuple(
                UnmatchedFileEvidence(item.media_id, 1200, ("matching dialogue",))
                for item, _payload in items
            ),
            Dossier(),
        ),
    )
    monkeypatch.setattr(
        analysis,
        "plan_disc_sequences",
        lambda _evidence, candidate_catalog, _groups: SimpleNamespace(
            disposition="proposed",
            score=0.9,
            global_margin=0.5,
            groups=(
                SimpleNamespace(
                    items=(
                        SimpleNamespace(
                            file_id=media_id,
                            proposed_episode=candidate_catalog[0].episode_id,
                        ),
                    )
                ),
            ),
        ),
    )
    gemini_calls = []

    class Ranker:
        def __init__(self, *, model, fallback_models=()):
            assert model == "gemini-test"
            assert tuple(fallback_models) == ()

        def rank_with_configured_keys(self, files, candidates, **kwargs):
            gemini_calls.append(kwargs)
            assert tuple(item.file_id for item in files) == (media_id,)
            assert tuple(item.episode_id for item in candidates) == ("S02E12",)
            assert kwargs["reviewer_scene_descriptions"] == {
                media_id: "Michael burns his foot on a grill."
            }
            return SimpleNamespace(
                matches=(
                    SimpleNamespace(
                        file_id=media_id, episode_id="S02E12", confidence=0.95
                    ),
                )
            )

    monkeypatch.setattr(analysis, "GeminiEpisodeRanker", Ranker)

    applied = analysis.execute_unmatched_disc_analysis(
        store,
        fingerprint,
        "Example",
        SimpleNamespace(min_confidence=0.7, gemini_model="gemini-test"),
        SimpleNamespace(),
        contracts,
        season=2,
        allow_gemini=True,
        allow_content_fallback=False,
        reviewer_scene_descriptions={media_id: "Michael burns his foot on a grill."},
    )

    assert applied == (media_id,)
    assert len(gemini_calls) == 2
    assert store.get(media_id).state == "queued"


def test_ambiguous_gemini_title_is_fenced_by_other_disc_matches(tmp_path, monkeypatch):
    fingerprint = "fedcba9876543210"
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    media_ids = []
    for title_index in range(1, 8):
        media_id = f"Example--disc-01-{fingerprint}-title-{title_index:03d}"
        media_ids.append(media_id)
        source = tmp_path / f"source-{title_index}.mkv"
        source.write_bytes(b"synthetic")
        contract = contracts / f"rip-{title_index}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "media_id": media_id,
                "source_path": str(source),
                "source_size_bytes": source.stat().st_size,
                "disc_fingerprint": fingerprint,
                "title_index": title_index,
                "disc_expected_title_indexes": list(range(1, 8)),
                "media_context": {"series_name": "Example", "season": 4},
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        store.claim_next()
        store.require_review(media_id, "unmatched_disc_analysis_required")
        store.choose_review_path(media_id, "all_season_analysis_running")

    catalog = tuple(
        EpisodeCatalogEntry(
            f"S04E{episode:02d}", 4, episode, f"Episode {episode}", "", 1200
        )
        for episode in range(1, 15)
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

    class Dossier:
        def __init__(self):
            self.attempts = []

        def record_attempt(self, media_ids, **details):
            self.attempts.append((media_ids, details))

        @staticmethod
        def safe_attempts(_media_id):
            return ()

    dossier = Dossier()
    monkeypatch.setattr(
        analysis,
        "collect_dossier_evidence",
        lambda items, *_args: (
            tuple(
                UnmatchedFileEvidence(item.media_id, 1200, ("episode dialogue",))
                for item, _payload in items
            ),
            dossier,
        ),
    )
    monkeypatch.setattr(
        analysis,
        "plan_disc_sequences",
        lambda *_args, **_kwargs: SimpleNamespace(
            disposition="review",
            score=0.4,
            global_margin=0.01,
            groups=(),
        ),
    )

    def match_six(files, *_args, diagnostic_details=None, **_kwargs):
        matched = {
            file.file_id: catalog[episode - 1]
            for file, episode in zip(files[:6], range(7, 13), strict=True)
        }
        if diagnostic_details is not None:
            diagnostic_details.update({
                file_id: {
                    "best_score": 0.81,
                    "runner_up_score": 0.4,
                    "margin": 0.41,
                    "qualifying_window_count": 4,
                    "candidate_episode_id": entry.episode_id,
                    "reason": "accepted",
                }
                for file_id, entry in matched.items()
            })
            diagnostic_details[media_ids[6]] = {
                "best_score": 0.55,
                "runner_up_score": 0.50,
                "margin": 0.05,
                "qualifying_window_count": 0,
                "candidate_episode_id": "S04E13",
                "reason": "below_confidence_threshold",
            }
        diagnostics = dict.fromkeys(matched, (0.81, 0.41))
        diagnostics[media_ids[6]] = (0.55, 0.05)
        return matched, diagnostics

    monkeypatch.setattr(analysis, "match_opensubtitles_seasons", match_six)
    gemini_catalogs = []

    class Ranker:
        def __init__(self, *, model, fallback_models=()):
            assert model == "gemini-test"
            assert tuple(fallback_models) == ()

        def rank_with_configured_keys(self, files, candidates, **_kwargs):
            candidate_ids = tuple(item.episode_id for item in candidates)
            gemini_catalogs.append(candidate_ids)
            assert "S04E04" not in candidate_ids
            assert "S04E13" in candidate_ids
            return SimpleNamespace(
                matches=(
                    SimpleNamespace(
                        file_id=files[0].file_id,
                        episode_id="S04E13",
                        confidence=0.95,
                    ),
                )
            )

    monkeypatch.setattr(analysis, "GeminiEpisodeRanker", Ranker)

    applied = analysis.execute_unmatched_disc_analysis(
        store,
        fingerprint,
        "Example",
        SimpleNamespace(min_confidence=0.7, gemini_model="gemini-test"),
        SimpleNamespace(),
        contracts,
        season=4,
        allow_gemini=True,
        allow_content_fallback=False,
    )

    assert applied == tuple(media_ids)
    assert len(gemini_catalogs) == 2
    assert any(
        details["branch"] == "tv-disc-range"
        and details["summary"]["candidate_scope"] == "S04E06-E13"
        for _media_ids, details in dossier.attempts
    )
    title_seven = json.loads(
        store.get(media_ids[6]).artifact.contract_path.read_text(encoding="utf-8")
    )
    assignment = next(
        item
        for item in title_seven["media_context"]["episode_assignments"]
        if item["title_index"] == 7
    )
    assert (assignment["season"], assignment["episode"]) == (4, 13)


def _show(tmdb_id: int, name: str) -> TvShowCandidate:
    return TvShowCandidate(tmdb_id, name, name, 1960, "Animated family sitcom")


def _episode_catalog() -> tuple[EpisodeCatalogEntry, ...]:
    return (EpisodeCatalogEntry("S01E01", 1, 1, "Pilot", "", 1500),)


def test_series_catalog_uses_one_exact_tmdb_name_without_gemini(monkeypatch):
    monkeypatch.setattr(
        analysis,
        "search_tv_show_candidates",
        lambda _name: (_show(33, "The Flintstones"), _show(44, "Flintstone Kids")),
    )
    monkeypatch.setattr(
        analysis, "fetch_aired_episode_catalog", lambda show_id: _episode_catalog()
    )

    name, catalog = analysis.resolve_series_catalog(
        "The Flintstones",
        SimpleNamespace(gemini_model="test", min_confidence=0.7),
        allow_gemini=True,
    )

    assert name == "The Flintstones"
    assert catalog == _episode_catalog()


def test_series_catalog_uses_gemini_to_choose_ambiguous_tmdb_candidate(monkeypatch):
    candidates = (_show(33, "The Flintstones"), _show(44, "Flintstone Kids"))
    monkeypatch.setattr(analysis, "search_tv_show_candidates", lambda _name: candidates)
    monkeypatch.setattr(
        analysis, "fetch_aired_episode_catalog", lambda show_id: _episode_catalog()
    )
    resolver = SimpleNamespace(
        resolve_with_configured_keys=lambda _hint, _candidates: GeminiSeriesResolution(
            33, "The Flintstones", 0.95, ("Packaging marker removed.",)
        )
    )
    monkeypatch.setattr(analysis, "GeminiSeriesResolver", lambda **_kwargs: resolver)

    name, _catalog = analysis.resolve_series_catalog(
        "Flintstones complete collection",
        SimpleNamespace(gemini_model="test", min_confidence=0.7),
        allow_gemini=True,
    )

    assert name == "The Flintstones"


def test_series_catalog_uses_gemini_before_trusting_one_inexact_candidate(
    monkeypatch,
):
    unrelated = _show(900, "The Office UK")
    canonical = _show(2316, "The Office")
    searches = []

    def search(name):
        searches.append(name)
        return (unrelated,) if len(searches) == 1 else (canonical,)

    monkeypatch.setattr(analysis, "search_tv_show_candidates", search)
    monkeypatch.setattr(
        analysis, "fetch_aired_episode_catalog", lambda _show_id: _episode_catalog()
    )
    resolver = SimpleNamespace(
        resolve_with_configured_keys=lambda _hint, _candidates: GeminiSeriesResolution(
            None,
            "The Office",
            0.98,
            ("Removed release-specific packaging text.",),
        )
    )
    monkeypatch.setattr(analysis, "GeminiSeriesResolver", lambda **_kwargs: resolver)

    name, catalog = analysis.resolve_series_catalog(
        "Office Superfan Extended Set One",
        SimpleNamespace(gemini_model="test", min_confidence=0.7),
        allow_gemini=True,
    )

    assert searches == [
        "Office Extended Set One",
        "Office Superfan Extended Set One",
        "The Office",
    ]
    assert name == "The Office"
    assert catalog == _episode_catalog()


def test_unvalidated_gemini_series_requires_review_only_after_gemini(monkeypatch):
    monkeypatch.setattr(analysis, "search_tv_show_candidates", lambda _name: ())
    resolver = SimpleNamespace(
        resolve_with_configured_keys=lambda _hint, _candidates: GeminiSeriesResolution(
            None,
            "Possible Show",
            0.95,
            ("The label resembles a release title.",),
        )
    )
    monkeypatch.setattr(analysis, "GeminiSeriesResolver", lambda **_kwargs: resolver)

    with pytest.raises(
        analysis.GeminiAnalysisError, match="could not be validated through TMDb"
    ) as caught:
        analysis.resolve_series_catalog(
            "Oddly Named Disc",
            SimpleNamespace(gemini_model="test", min_confidence=0.7),
            allow_gemini=True,
        )

    assert caught.value.review_code == "gemini_series_resolution_uncertain"
    assert caught.value.proposed_series_name == "Possible Show"
    assert caught.value.proposed_tmdb_id is None
    assert caught.value.proposed_confidence == 0.95
    assert caught.value.proposed_series_names == ("Possible Show",)


def test_series_catalog_validates_gemini_proposed_name_through_tmdb(monkeypatch):
    calls = []

    def search(name):
        calls.append(name)
        return () if len(calls) == 1 else (_show(33, "The Flintstones"),)

    monkeypatch.setattr(analysis, "search_tv_show_candidates", search)
    monkeypatch.setattr(
        analysis, "fetch_aired_episode_catalog", lambda show_id: _episode_catalog()
    )
    resolver = SimpleNamespace(
        resolve_with_configured_keys=lambda _hint, _candidates: GeminiSeriesResolution(
            None, "The Flintstones", 0.95, ("Canonical title inferred.",)
        )
    )
    monkeypatch.setattr(analysis, "GeminiSeriesResolver", lambda **_kwargs: resolver)

    name, _catalog = analysis.resolve_series_catalog(
        "Flintstones Collector Set",
        SimpleNamespace(gemini_model="test", min_confidence=0.7),
        allow_gemini=True,
    )

    assert calls == ["Flintstones Collector Set", "The Flintstones"]
    assert name == "The Flintstones"


def test_series_catalog_validates_ranked_gemini_alternative(monkeypatch):
    calls = []

    def search(name):
        calls.append(name)
        return (_show(2316, "The Office"),) if name == "The Office" else ()

    monkeypatch.setattr(analysis, "search_tv_show_candidates", search)
    monkeypatch.setattr(
        analysis, "fetch_aired_episode_catalog", lambda _show_id: _episode_catalog()
    )
    resolver = SimpleNamespace(
        resolve_with_configured_keys=lambda _hint, _candidates: GeminiSeriesResolution(
            None,
            "The Office Superfan Episodes",
            0.95,
            ("Packaging label is ambiguous.",),
            ("The Office", "Office Ladies"),
        )
    )
    monkeypatch.setattr(analysis, "GeminiSeriesResolver", lambda **_kwargs: resolver)

    name, _catalog = analysis.resolve_series_catalog(
        "OFFICE SUPERFAN S2 D2",
        SimpleNamespace(gemini_model="test", min_confidence=0.7),
        allow_gemini=True,
    )

    assert calls == [
        "OFFICE",
        "OFFICE SUPERFAN S2 D2",
        "The Office Superfan Episodes",
        "The Office",
    ]
    assert name == "The Office"


def test_series_query_preserves_standalone_d_number_movie_title():
    assert analysis._canonical_series_query("D2: The Mighty Ducks") == (
        "D2: The Mighty Ducks"
    )
    assert analysis._canonical_series_query("THE OFFICE SUPERFAN S2 D2") == "THE OFFICE"


def test_series_catalog_retries_canonical_name_after_catalogueless_label_match(
    monkeypatch,
):
    packaging_result = _show(900, "The Office Superfan Episodes S1")
    canonical = _show(2316, "The Office")
    searches = []

    def search(name):
        searches.append(name)
        return (packaging_result,) if len(searches) == 1 else (canonical,)

    monkeypatch.setattr(analysis, "search_tv_show_candidates", search)
    monkeypatch.setattr(
        analysis,
        "fetch_aired_episode_catalog",
        lambda show_id: ()
        if show_id == packaging_result.tmdb_id
        else _episode_catalog(),
    )
    resolver = SimpleNamespace(
        resolve_with_configured_keys=lambda _hint, _candidates: GeminiSeriesResolution(
            canonical.tmdb_id,
            canonical.name,
            0.98,
            ("Edition and season packaging markers removed.",),
        )
    )
    monkeypatch.setattr(analysis, "GeminiSeriesResolver", lambda **_kwargs: resolver)

    name, catalog = analysis.resolve_series_catalog(
        "THE OFFICE SUPERFAN EPISODES S1",
        SimpleNamespace(gemini_model="test", min_confidence=0.7),
        allow_gemini=True,
    )

    assert searches == ["THE OFFICE", "THE OFFICE SUPERFAN EPISODES S1"]
    assert name == "The Office"
    assert catalog == _episode_catalog()


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_text"),
    [
        (
            ApiCredentialError("gemini-paid", "rejected", status_code=403),
            "gemini_credential_rejected",
            "gemini-paid",
        ),
        (
            ApiServiceError("Gemini", 429, "rate or quota limit reached"),
            "gemini_rate_limited",
            "HTTP 429",
        ),
        (
            ApiServiceError("Gemini", 503, "temporarily unavailable"),
            "gemini_provider_unavailable",
            "HTTP 503",
        ),
        (
            ApiServiceError("Gemini", 400, "request was not accepted"),
            "gemini_request_rejected",
            "HTTP 400",
        ),
        (
            ApiServiceError("Gemini", None, "network failure: Timeout"),
            "gemini_network_failed",
            "network failure",
        ),
        (
            GeminiResponseError("invalid structured output"),
            "gemini_response_invalid",
            "structured output",
        ),
    ],
)
def test_gemini_failures_keep_safe_actionable_diagnostics(
    error, expected_code, expected_text
):
    classified = analysis.classify_gemini_failure(error)

    assert classified.review_code == expected_code
    assert expected_text in classified.diagnostic


def test_existing_library_episodes_reads_names_without_media(tmp_path):
    series = tmp_path / "FAERIE TALE THEATRE" / "Season 03"
    series.mkdir(parents=True)
    (series / "FAERIE TALE THEATRE - S03E05 - Snow White.mkv").write_bytes(b"")
    (series / "notes.txt").write_text("S99E99", encoding="utf-8")

    assert analysis.existing_library_episodes(
        tmp_path, "Faerie Tale Theatre"
    ) == frozenset({(3, 5)})


def test_library_episode_status_never_removes_aired_candidates():
    catalog = tuple(
        EpisodeCatalogEntry(f"S01E{index:02d}", 1, index, str(index), "", 1200)
        for index in range(1, 5)
    )

    preferred, scope = analysis.prioritize_missing_catalog(
        catalog, frozenset({(1, 1), (1, 2)}), 2
    )
    fallback, fallback_scope = analysis.prioritize_missing_catalog(
        catalog, frozenset({(1, 1), (1, 2), (1, 3)}), 2
    )

    assert preferred == catalog
    assert scope == "all-library-aware"
    assert fallback == catalog
    assert fallback_scope == "all-library-aware"


def test_preferred_season_order_starts_after_known_series_frontier():
    catalog = tuple(
        EpisodeCatalogEntry(f"S{season:02d}E01", season, 1, "Episode", "", 1200)
        for season in range(1, 7)
    )

    order = analysis.preferred_season_order(
        catalog,
        frozenset((season, 1) for season in range(1, 6)),
    )

    assert order == (6, 5, 4, 3, 2, 1)


def test_assigned_disc_episodes_reads_identification_history(tmp_path):
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    fingerprint = "0123456789abcdef"
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO disc_title_history (
                disc_fingerprint, title_index, outcome_name,
                library_relative, episode_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                1,
                "Example - S01E04 - Fourth.mkv",
                "Example/Season 01/Example - S01E04 - Fourth.mkv",
                "S01E04",
                "2026-08-08T00:00:00+00:00",
            ),
        )

    assert analysis.assigned_disc_episodes(store, fingerprint) == frozenset({(1, 4)})


def test_dominant_season_scope_tries_previous_near_season_start():
    catalog = tuple(
        EpisodeCatalogEntry(
            f"S{season:02d}E{episode:02d}", season, episode, "", "", 1200
        )
        for season in (1, 2, 3)
        for episode in range(1, 11)
    )

    assert analysis.dominant_season_scope(
        frozenset({(2, 1), (2, 2), (2, 3)}), catalog
    ) == (2, 1)
    assert analysis.dominant_season_scope(
        frozenset({(2, 8), (2, 9), (2, 10)}), catalog
    ) == (2, 3)


def test_opensubtitles_matches_independently_and_only_falls_forward(tmp_path):
    catalog = tuple(
        EpisodeCatalogEntry(f"S0{season}E01", season, 1, str(season), "", 1200)
        for season in (1, 2)
    )
    files = (
        UnmatchedFileEvidence("late-title", 1200, ("alpha dialogue", "alpha dialogue")),
        UnmatchedFileEvidence("early-title", 1200, ("beta dialogue", "beta dialogue")),
    )
    calls = []

    class Provider:
        def get_subtitles(self, _series, season, _files, _tmdb_id):
            calls.append(season)
            text = "alpha dialogue" if season == 1 else "beta dialogue"
            path = tmp_path / f"season-{season}.srt"
            path.write_text("", encoding="utf-8")
            return [
                SubtitleFile(
                    path=path,
                    content=text,
                    episode_info=EpisodeInfo(
                        series_name="Example", season=season, episode=1
                    ),
                )
            ]

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.95 if transcript in reference else 0.1

    diagnostic_details = {}
    matched, _diagnostics = analysis.match_opensubtitles_seasons(
        files,
        catalog,
        "Example",
        1,
        (1, 2),
        ASR(),
        min_confidence=0.7,
        provider_factory=Provider,
        diagnostic_details=diagnostic_details,
    )

    assert calls == [1, 2]
    assert matched["late-title"].episode_id == "S01E01"
    assert matched["early-title"].episode_id == "S02E01"
    assert diagnostic_details["late-title"] == {
        "best_score": 0.95,
        "runner_up_score": 0.0,
        "margin": 0.95,
        "qualifying_window_count": 2,
        "candidate_episode_id": "S01E01",
        "reason": "accepted",
    }


def test_opensubtitles_tries_untested_release_after_normal_references_fail(tmp_path):
    catalog = (
        EpisodeCatalogEntry("S07E20", 7, 20, "First", "", 1200),
        EpisodeCatalogEntry("S07E21", 7, 21, "Second", "", 1200),
    )
    files = (
        UnmatchedFileEvidence(
            "extended-title",
            1200,
            ("alternate cut dialogue", "alternate cut dialogue"),
        ),
    )
    calls = []

    class Provider:
        def get_subtitles(self, _series, season, _files, _tmdb_id):
            calls.append(("initial", season))
            return [
                SubtitleFile(
                    path=tmp_path / "regular-20.srt",
                    content="unrelated regular dialogue",
                    episode_info=EpisodeInfo(
                        series_name="Example", season=season, episode=20
                    ),
                    release_match="generic",
                ),
                SubtitleFile(
                    path=tmp_path / "regular-21.srt",
                    content="different regular dialogue",
                    episode_info=EpisodeInfo(
                        series_name="Example", season=season, episode=21
                    ),
                    release_match="generic",
                ),
            ]

        def get_alternate_subtitles(self, _series, season, _files, _tmdb_id):
            calls.append(("alternate", season))
            return [
                SubtitleFile(
                    path=tmp_path / "supercut-20.srt",
                    content="alternate cut dialogue",
                    episode_info=EpisodeInfo(
                        series_name="Example", season=season, episode=20
                    ),
                    release_match="compatible",
                )
            ]

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.95 if transcript in reference else 0.1

    details = {}
    evaluations = {}
    matched, _diagnostics = analysis.match_opensubtitles_seasons(
        files,
        catalog,
        "Example",
        123,
        (7,),
        ASR(),
        min_confidence=0.7,
        provider=Provider(),
        diagnostic_details=details,
        candidate_evaluations=evaluations,
    )

    assert calls == [("initial", 7), ("alternate", 7)]
    assert matched["extended-title"].episode_id == "S07E20"
    assert details["extended-title"]["subtitle_reference_pass"] == (
        "alternate-release-failover"
    )
    assert any(
        record["phase"] == "alternate-release-failover"
        and record["disposition"] == "selected"
        for record in evaluations["extended-title"]
    )


def test_known_altered_season_tries_release_ladder_before_generic(tmp_path):
    catalog = (
        EpisodeCatalogEntry("S07E20", 7, 20, "First", "", 1200),
        EpisodeCatalogEntry("S07E21", 7, 21, "Second", "", 1200),
    )
    files = (
        UnmatchedFileEvidence(
            "superfan-title",
            1800,
            ("distinct superfan dialogue", "distinct superfan dialogue"),
        ),
    )
    provider_calls = []
    scored_references = []

    class Provider:
        def get_subtitles(self, _series, season, _files, _tmdb_id):
            provider_calls.append(("initial", season))
            return [
                SubtitleFile(
                    path=tmp_path / "generic-20.srt",
                    content="distinct superfan dialogue generic would match",
                    episode_info=EpisodeInfo(
                        series_name="Example", season=season, episode=20
                    ),
                    release_match="generic",
                    release_profile="superfan",
                ),
                SubtitleFile(
                    path=tmp_path / "compatible-21.srt",
                    content="unrelated compatible dialogue",
                    episode_info=EpisodeInfo(
                        series_name="Example", season=season, episode=21
                    ),
                    release_match="compatible",
                    release_profile="superfan",
                ),
            ]

        def get_alternate_subtitles(self, _series, season, _files, _tmdb_id):
            provider_calls.append(("alternate", season))
            return [
                SubtitleFile(
                    path=tmp_path / "extended-20.srt",
                    content="distinct superfan dialogue alternate cut",
                    episode_info=EpisodeInfo(
                        series_name="Example", season=season, episode=20
                    ),
                    release_match="compatible",
                    release_profile="superfan",
                )
            ]

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            scored_references.append(reference)
            return 0.95 if transcript in reference else 0.1

    details = {}
    matched, _diagnostics = analysis.match_opensubtitles_seasons(
        files,
        catalog,
        "Example Superfan Episodes",
        123,
        (7,),
        ASR(),
        min_confidence=0.7,
        provider=Provider(),
        diagnostic_details=details,
    )

    assert provider_calls == [("initial", 7), ("alternate", 7)]
    assert matched["superfan-title"].episode_id == "S07E20"
    assert details["superfan-title"]["subtitle_reference_pass"] == (
        "alternate-release-failover"
    )
    assert any("alternate cut" in reference for reference in scored_references)
    assert not any(
        "generic would match" in reference for reference in scored_references
    )


def test_opensubtitles_does_not_fail_over_before_normal_neighbor_season_succeeds(
    tmp_path,
):
    catalog = tuple(
        EpisodeCatalogEntry(f"S0{season}E01", season, 1, "", "", 1200)
        for season in (1, 2)
    )
    files = (UnmatchedFileEvidence("title", 1200, ("season two", "season two")),)
    alternate_calls = []

    class Provider:
        def get_subtitles(self, _series, season, _files, _tmdb_id):
            return [
                SubtitleFile(
                    path=tmp_path / f"season-{season}.srt",
                    content="season two" if season == 2 else "season one",
                    episode_info=EpisodeInfo(
                        series_name="Example", season=season, episode=1
                    ),
                )
            ]

        def get_alternate_subtitles(self, _series, season, _files, _tmdb_id):
            alternate_calls.append(season)
            return []

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.95 if transcript in reference else 0.1

    matched, _diagnostics = analysis.match_opensubtitles_seasons(
        files,
        catalog,
        "Example",
        123,
        (1, 2),
        ASR(),
        min_confidence=0.7,
        provider=Provider(),
    )

    assert matched["title"].episode_id == "S02E01"
    assert alternate_calls == []


def test_alternate_release_failover_does_not_lower_two_window_requirement(tmp_path):
    catalog = (EpisodeCatalogEntry("S01E01", 1, 1, "First", "", 1200),)
    files = (UnmatchedFileEvidence("title", 1200, ("matching dialogue",)),)

    class Provider:
        def get_subtitles(self, _series, season, _files, _tmdb_id):
            return [
                SubtitleFile(
                    path=tmp_path / "regular.srt",
                    content="different dialogue",
                    episode_info=EpisodeInfo(
                        series_name="Example", season=season, episode=1
                    ),
                )
            ]

        def get_alternate_subtitles(self, _series, season, _files, _tmdb_id):
            return [
                SubtitleFile(
                    path=tmp_path / "alternate.srt",
                    content="matching dialogue",
                    episode_info=EpisodeInfo(
                        series_name="Example", season=season, episode=1
                    ),
                    release_match="compatible",
                )
            ]

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.95 if transcript in reference else 0.1

    details = {}
    matched, _diagnostics = analysis.match_opensubtitles_seasons(
        files,
        catalog,
        "Example",
        123,
        (1,),
        ASR(),
        min_confidence=0.7,
        provider=Provider(),
        diagnostic_details=details,
    )

    assert matched == {}
    assert details["title"]["reason"] == "insufficient_qualifying_windows"
    assert details["title"]["subtitle_reference_pass"] == ("alternate-release-failover")


def test_opensubtitles_records_exact_review_reason():
    best = EpisodeCatalogEntry("S01E01", 1, 1, "First", "", 1200)
    runner_up = EpisodeCatalogEntry("S01E02", 1, 2, "Second", "", 1200)
    details = {}

    accepted, diagnostics = analysis._accept_subtitle_candidates(
        {"title": [(0.87, 1, best), (0.84, 1, runner_up)]},
        0.7,
        set(),
        diagnostic_details=details,
    )

    assert accepted == {}
    assert diagnostics == {"title": (0.87, pytest.approx(0.03))}
    assert details["title"] == {
        "best_score": 0.87,
        "runner_up_score": 0.84,
        "margin": pytest.approx(0.03),
        "qualifying_window_count": 1,
        "candidate_episode_id": "S01E01",
        "reason": "insufficient_qualifying_windows",
    }


def test_opensubtitles_audit_retains_every_scored_and_runtime_rejected_candidate(
    tmp_path,
):
    catalog = (
        EpisodeCatalogEntry("S01E01", 1, 1, "First", "", 1200),
        EpisodeCatalogEntry("S01E02", 1, 2, "Second", "", 1200),
        EpisodeCatalogEntry("S01E03", 1, 3, "Compilation", "", 300),
        EpisodeCatalogEntry("S01E04", 1, 4, "No subtitle", "", 1200),
    )
    evidence = (
        UnmatchedFileEvidence("title", 2500, ("strong anchor", "strong anchor")),
    )

    class Provider:
        @staticmethod
        def get_subtitles(_series, _season, _files, _tmdb_id):
            return tuple(
                SubtitleFile(
                    path=tmp_path / f"{entry.episode_id}.srt",
                    content=(
                        "strong anchor"
                        if entry.episode == 1
                        else "weak unrelated dialogue"
                    ),
                    episode_info=EpisodeInfo(
                        series_name="Example", season=1, episode=entry.episode
                    ),
                )
                for entry in catalog
                if entry.episode != 4
            )

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.95 if transcript in reference else 0.2

    candidate_evaluations = {}
    matched, _diagnostics = analysis.match_opensubtitles_seasons(
        evidence,
        catalog,
        "Example",
        1,
        (1,),
        ASR(),
        min_confidence=0.7,
        provider=Provider(),
        diagnostic_details={},
        candidate_evaluations=candidate_evaluations,
    )

    assert matched == {"title": catalog[0]}
    direct = [
        record
        for record in candidate_evaluations["title"]
        if record["phase"] == "direct"
    ]
    assert {
        (record["candidate_episode_id"], record["disposition"], record["reason"])
        for record in direct
    } == {
        ("S01E01", "selected", "accepted"),
        ("S01E02", "rejected", "lower_ranked_candidate"),
        ("S01E03", "rejected", "runtime_mismatch"),
        ("S01E04", "rejected", "subtitle_reference_unavailable"),
    }


def test_single_exceptionally_high_subtitle_window_cannot_name_episode():
    best = EpisodeCatalogEntry("S01E01", 1, 1, "First", "", 1200)

    accepted, _diagnostics = analysis._accept_subtitle_candidates(
        {"title": [(0.99, 1, best)]},
        0.7,
        set(),
    )

    assert accepted == {}


def test_known_altered_cut_runtime_is_scored_before_play_all_review(tmp_path):
    catalog = (EpisodeCatalogEntry("S07E20", 7, 20, "First", "", 1200),)
    files = (
        UnmatchedFileEvidence(
            "long-superfan-title",
            4000,
            ("two strong anchors", "two strong anchors"),
        ),
    )

    class Provider:
        @staticmethod
        def get_subtitles(_series, season, _files, _tmdb_id):
            return [
                SubtitleFile(
                    path=tmp_path / "superfan-20.srt",
                    content="two strong anchors",
                    episode_info=EpisodeInfo(
                        series_name="Example", season=season, episode=20
                    ),
                    release_match="compatible",
                    release_profile="superfan",
                )
            ]

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.95 if transcript in reference else 0.1

    assert not analysis._episode_runtime_consistent(4000, catalog[0])
    assert analysis._episode_runtime_consistent(4000, catalog[0], altered_cut=True)
    matched, _diagnostics = analysis.match_opensubtitles_seasons(
        files,
        catalog,
        "Example Superfan Episodes",
        123,
        (7,),
        ASR(),
        min_confidence=0.7,
        provider=Provider(),
    )

    assert matched["long-superfan-title"].episode_id == "S07E20"


def test_regular_subtitles_match_extended_cut_after_large_timeline_insertions(
    monkeypatch,
):
    """Unchanged dialogue remains useful even after added scenes shift timing."""

    monkeypatch.setattr(
        analysis.SubtitleReader,
        "extract_subtitle_chunk",
        lambda *_args, **_kwargs: ["different dialogue near the old timestamp"],
    )

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            if transcript in reference:
                return 0.96
            return 0.1

    average, window_scores = analysis._score_subtitle(
        ASR(),
        (
            "rare warehouse anchor alpha",
            "superfan only inserted scene",
            "distinct conference room anchor omega",
        ),
        (
            "opening regular dialogue rare warehouse anchor alpha then many words "
            "from the broadcast episode before distinct conference room anchor omega"
        ),
        2400,
    )

    assert window_scores == pytest.approx((0.96, 0.96, 0.1))
    assert average == pytest.approx((0.96 + 0.96 + 0.1) / 3)


def test_timestamped_evidence_finds_consistent_clock_offset_and_drift(monkeypatch):
    monkeypatch.setattr(analysis, "_subtitle_reference_windows", lambda *_args: ())
    content = """1
00:07:00,000 --> 00:07:20,000
first rare anchor dialogue

2
00:22:00,000 --> 00:22:20,000
second rare anchor dialogue

3
00:24:59,000 --> 00:25:00,000
closing words
"""

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.96 if transcript in reference else 0.1

    average, window_scores = analysis._score_subtitle(
        ASR(),
        ("first rare anchor dialogue", "second rare anchor dialogue"),
        content,
        3000,
        sample_start_seconds=(600.0, 2400.0),
    )

    assert window_scores == pytest.approx((0.96, 0.96))
    assert average == pytest.approx(0.96)


def test_focused_alignment_confirms_nonlinear_shifted_dialogue_anchors():
    def anchor(label):
        return " ".join([label, *(f"{label}word{index}" for index in range(30))])

    alpha = anchor("alpha")
    beta = anchor("beta")
    omega = anchor("omega")
    content = (
        f"1\n00:01:40,000 --> 00:02:00,000\n{alpha}\n\n"
        f"2\n00:08:20,000 --> 00:08:40,000\n{beta}\n\n"
        f"3\n00:15:00,000 --> 00:15:20,000\n{omega}\n"
    )

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.96 if transcript in reference else 0.1

    result = analysis._focused_subtitle_alignment(
        ASR(),
        (alpha, beta, omega),
        content,
        1320,
        min_confidence=0.7,
        sample_start_seconds=(120.0, 660.0, 1200.0),
    )

    assert result.qualifying_anchor_count == 3
    assert result.coherent_anchor_count == 3
    assert result.source_coverage_seconds == 1080.0
    assert result.reference_coverage_seconds == 800.0
    assert result.spread_confirmed is True
    assert result.score == pytest.approx(0.96)


def test_focused_alignment_rejects_time_reversed_false_confirmation():
    def anchor(label):
        return " ".join([label, *(f"{label}word{index}" for index in range(30))])

    first = anchor("first")
    second = anchor("second")
    content = (
        f"1\n00:01:40,000 --> 00:02:00,000\n{second}\n\n"
        f"2\n00:15:00,000 --> 00:15:20,000\n{first}\n"
    )

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.96 if transcript in reference else 0.1

    result = analysis._focused_subtitle_alignment(
        ASR(),
        (first, second),
        content,
        1320,
        min_confidence=0.7,
        sample_start_seconds=(120.0, 1200.0),
    )

    assert result.qualifying_anchor_count == 1
    assert result.spread_confirmed is False


def test_seeded_candidate_uses_deeper_focused_alignment_before_review(
    tmp_path, monkeypatch
):
    def anchor(label):
        return " ".join([label, *(f"{label}word{index}" for index in range(30))])

    alpha = anchor("alpha")
    beta = anchor("beta")
    omega = anchor("omega")
    matching_content = (
        f"1\n00:01:40,000 --> 00:02:00,000\n{alpha}\n\n"
        f"2\n00:08:20,000 --> 00:08:40,000\n{beta}\n\n"
        f"3\n00:15:00,000 --> 00:15:20,000\n{omega}\n"
    )
    other_content = (
        "1\n00:01:40,000 --> 00:02:00,000\n"
        "unrelated dialogue from a different episode\n"
    )
    catalog = (
        EpisodeCatalogEntry("S01E01", 1, 1, "Matched", "", 1320),
        EpisodeCatalogEntry("S01E02", 1, 2, "Other", "", 1320),
    )
    references = []
    for episode, content in ((1, matching_content), (2, other_content)):
        path = tmp_path / f"s01e{episode:02d}.srt"
        path.write_text("", encoding="utf-8")
        references.append(
            SubtitleFile(
                path=path,
                content=content,
                episode_info=EpisodeInfo(
                    series_name="Example", season=1, episode=episode
                ),
            )
        )
    item = UnmatchedFileEvidence(
        "shifted-title",
        1320,
        (alpha, beta, omega),
        (120.0, 660.0, 1200.0),
    )

    def broad_score(_asr, _excerpts, content, _duration, **_kwargs):
        return (
            (0.96, (0.96, 0.1, 0.1))
            if content == matching_content
            else (
                0.2,
                (0.2, 0.1, 0.1),
            )
        )

    monkeypatch.setattr(analysis, "_score_subtitle", broad_score)

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.96 if transcript in reference else 0.1

    details = {}
    scored = analysis._subtitle_candidates(
        (item,),
        references,
        {(entry.season, entry.episode): entry for entry in catalog},
        ASR(),
        min_confidence=0.7,
        enable_focused_refinement=True,
        focused_alignment_details=details,
    )
    accepted, _diagnostics = analysis._accept_subtitle_candidates(
        scored,
        0.7,
        set(),
        diagnostic_details=details,
    )

    assert scored["shifted-title"][0][1] == 3
    assert accepted["shifted-title"].episode_id == "S01E01"
    assert details["shifted-title"]["focused_alignment_attempted"] is True
    assert details["shifted-title"]["focused_alignment_confirmed"] is True
    assert details["shifted-title"]["focused_qualifying_anchor_count"] == 3
    assert details["shifted-title"]["reason"] == "accepted"


def test_extended_anchor_global_search_is_bounded_and_keeps_rare_dialogue():
    decoy = "ordinary office conversation repeats every day for many people "
    content = (decoy * 900) + "rare pretzel day anchor with stanley at the end"

    windows = analysis._subtitle_reference_windows(
        content,
        "rare pretzel day anchor with stanley",
    )

    assert len(windows) <= 96
    assert any("rare pretzel day anchor with stanley" in window for window in windows)


def test_extended_only_whisper_windows_are_neutral_when_two_regular_anchors_match(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        analysis.SubtitleReader,
        "extract_subtitle_chunk",
        lambda *_args, **_kwargs: ["unrelated timestamp dialogue"],
    )
    catalog = (
        EpisodeCatalogEntry("S01E01", 1, 1, "Pilot", "", 1320),
        EpisodeCatalogEntry("S01E02", 1, 2, "Second", "", 1320),
    )
    files = (
        UnmatchedFileEvidence(
            "extended-title",
            2280,
            (
                "rare warehouse anchor alpha",
                "new deleted scene dialogue",
                "distinct conference room anchor omega",
            ),
        ),
    )
    references = []
    for episode, content in (
        (
            1,
            "rare warehouse anchor alpha ordinary dialogue "
            "distinct conference room anchor omega",
        ),
        (2, "different episode dialogue with no matching anchors"),
    ):
        path = tmp_path / f"s01e{episode:02d}.srt"
        path.write_text("", encoding="utf-8")
        references.append(
            SubtitleFile(
                path=path,
                content=content,
                episode_info=EpisodeInfo(
                    series_name="Example", season=1, episode=episode
                ),
            )
        )

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.96 if transcript in reference else 0.1

    scored = analysis._subtitle_candidates(
        files,
        references,
        {(entry.season, entry.episode): entry for entry in catalog},
        ASR(),
        min_confidence=0.7,
    )
    details = {}
    accepted, _diagnostics = analysis._accept_subtitle_candidates(
        scored,
        0.7,
        set(),
        diagnostic_details=details,
    )

    assert accepted["extended-title"].episode_id == "S01E01"
    assert details["extended-title"]["qualifying_window_count"] == 2
    assert details["extended-title"]["reason"] == "accepted"


def test_opensubtitles_bootstraps_season_without_existing_matches(tmp_path):
    catalog = tuple(
        EpisodeCatalogEntry(
            f"S{season:02d}E{episode:02d}", season, episode, "", "", 1200
        )
        for season in (1, 2, 3)
        for episode in (1, 2)
    )
    files = (
        UnmatchedFileEvidence("one", 1200, ("season 2 first", "season 2 first")),
        UnmatchedFileEvidence("two", 1201, ("season 2 second", "season 2 second")),
    )

    class Provider:
        def get_subtitles(self, _series, season, _files, _tmdb_id):
            results = []
            for episode in (1, 2):
                path = tmp_path / f"s{season}e{episode}.srt"
                path.write_text("", encoding="utf-8")
                results.append(
                    SubtitleFile(
                        path=path,
                        content=f"season {season} {'first' if episode == 1 else 'second'}",
                        episode_info=EpisodeInfo(
                            series_name="Example", season=season, episode=episode
                        ),
                    )
                )
            return results

    class ASR:
        @staticmethod
        def calculate_match_score(transcript, reference):
            return 0.95 if transcript == reference else 0.2

    scope = analysis.discover_opensubtitles_season(
        files,
        catalog,
        "Example",
        1,
        ASR(),
        min_confidence=0.7,
        provider=Provider(),
    )

    assert scope == (2, 3)


def test_subtitle_analysis_yields_without_a_total_time_deadline():
    yielded = []

    def yield_control(seconds):
        yielded.append(seconds)

    progress = analysis._SubtitleAnalysisProgress(
        yield_control=yield_control,
        yield_every=1,
    )

    average, scores = analysis._score_subtitle(
        SimpleNamespace(calculate_match_score=lambda *_args: 0.5),
        ("synthetic dialogue evidence",),
        "synthetic reference dialogue evidence",
        1200,
        analysis_progress=progress,
    )

    assert average == pytest.approx(0.5)
    assert scores == pytest.approx((0.5,))
    assert len(yielded) > 2
    assert set(yielded) == {analysis._SUBTITLE_YIELD_SECONDS}


def test_local_sequence_keeps_full_aired_order_when_library_has_gaps(
    tmp_path, monkeypatch
):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    fingerprint = "fedcba9876543210"
    media_ids = []
    for title_index in range(2):
        media_id = f"disc-01-{fingerprint}-title-{title_index:03d}"
        media_ids.append(media_id)
        source = tmp_path / f"source-{title_index}.mkv"
        source.write_bytes(b"synthetic")
        contract = contracts / f"{media_id}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "media_id": media_id,
                "source_path": str(source),
                "source_size_bytes": source.stat().st_size,
                "disc_fingerprint": fingerprint,
                "title_index": title_index,
                "media_context": {
                    "series_name": "Example Series Superfan Episodes",
                    "season": None,
                },
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        store.claim_next()
        store.require_review(media_id, "unmatched_disc_analysis_required")
        store.choose_review_path(media_id, "all_season_analysis_running")

    catalog = tuple(
        EpisodeCatalogEntry(f"S01E{index:02d}", 1, index, str(index), "", 1200)
        for index in range(1, 5)
    )
    monkeypatch.setattr(
        analysis,
        "search_tv_show_candidates",
        lambda _name: (TvShowCandidate(1, "Example Series", "", None, ""),),
    )
    monkeypatch.setattr(
        analysis, "fetch_aired_episode_catalog", lambda _show_id: catalog
    )
    monkeypatch.setattr(
        analysis, "existing_library_episodes", lambda *_args: frozenset({(1, 2)})
    )

    class FakeDossier:
        def record_attempt(self, *_args, **_kwargs):
            return None

        def safe_attempts(self, _media_id):
            return ()

    monkeypatch.setattr(
        analysis,
        "collect_dossier_evidence",
        lambda items, *_args: (
            tuple(
                UnmatchedFileEvidence(item.media_id, 1200, ("useful dialogue",))
                for item, _payload in items
            ),
            FakeDossier(),
        ),
    )
    seen_catalogs = []
    seen_subtitle_series = []

    def fake_plan(_evidence, candidate_catalog, _groups):
        seen_catalogs.append(candidate_catalog)
        return SimpleNamespace(
            disposition="proposed",
            score=0.8,
            global_margin=0.2,
            groups=(
                SimpleNamespace(
                    items=tuple(
                        SimpleNamespace(
                            file_id=media_id,
                            proposed_episode=catalog[index].episode_id,
                        )
                        for index, media_id in enumerate(media_ids)
                    )
                ),
            ),
        )

    monkeypatch.setattr(analysis, "plan_disc_sequences", fake_plan)

    def fake_subtitle_matches(
        _files, candidate_catalog, *_args, diagnostic_details=None, **_kwargs
    ):
        seen_subtitle_series.append(_args[0])
        matches = {
            media_ids[0]: candidate_catalog[2],
            media_ids[1]: candidate_catalog[3],
        }
        if diagnostic_details is not None:
            for file_id, entry in matches.items():
                diagnostic_details[file_id] = {
                    "best_score": 0.91,
                    "runner_up_score": 0.2,
                    "margin": 0.71,
                    "qualifying_window_count": 2,
                    "candidate_episode_id": entry.episode_id,
                    "reason": "accepted",
                }
        return matches, dict.fromkeys(matches, (0.91, 0.71))

    monkeypatch.setattr(analysis, "match_opensubtitles_seasons", fake_subtitle_matches)

    applied = analysis.execute_unmatched_disc_analysis(
        store,
        fingerprint,
        "Example Series",
        SimpleNamespace(
            ffprobe_path=None,
            ffmpeg_path=None,
            asr_model_name="small",
            gemini_model="gemini-test",
            min_confidence=0.7,
            jellyfin_tv_root=tmp_path,
        ),
        SimpleNamespace(),
        contracts,
        season=1,
    )

    assert applied == tuple(media_ids)
    assert seen_catalogs == [catalog]
    assert seen_subtitle_series == ["Example Series Superfan Episodes"]
    diagnostic = next(
        event
        for event in store.list_events()
        if event.event_type == "sequence_match_scored"
    )
    assert diagnostic.details["candidate_scope"] == "S01"
    assert diagnostic.details["catalog_episode_count"] == 4
    payload = json.loads(store.get(media_ids[0]).artifact.contract_path.read_text())
    assert [
        assignment["episode"]
        for assignment in payload["media_context"]["episode_assignments"]
    ] == [3, 4]


def test_anchor_titles_bounds_and_spreads_initial_season_evidence():
    selected = [(SimpleNamespace(media_id=f"title-{index}"), {}) for index in range(9)]

    anchors = analysis._anchor_titles(selected)

    assert [item.media_id for item, _payload in anchors] == [
        "title-0",
        "title-4",
        "title-8",
    ]


@pytest.mark.parametrize(
    "mode",
    [
        "local",
        "local-incoherent",
        "local-runtime-mismatch",
        "gemini",
        "gemini-incoherent",
        "partial-gemini",
        "subtitle-partial-gemini",
        "subtitle-partial-gemini-failed",
        "runtime-mismatch",
        "unstable-gemini",
    ],
)
def test_all_season_analysis_uses_independent_evidence_not_sequence(  # noqa: C901
    tmp_path, monkeypatch, mode
):
    use_gemini = mode not in {
        "local",
        "local-incoherent",
        "local-runtime-mismatch",
    }
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    fingerprint = "0123456789abcdef"
    media_ids = []
    for title_index in range(2):
        media_id = f"disc-04-{fingerprint}-title-{title_index:03d}"
        media_ids.append(media_id)
        source = tmp_path / f"source-{title_index}.mkv"
        source.write_bytes(b"synthetic")
        contract = contracts / f"{media_id}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "media_id": media_id,
                "source_path": str(source),
                "source_size_bytes": source.stat().st_size,
                "disc_fingerprint": fingerprint,
                "title_index": title_index,
                "media_context": {"series_name": "Unmatched", "season": None},
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        store.claim_next()
        store.require_review(media_id, "unmatched_disc_analysis_required")

    catalog_episodes = (
        (1, 12) if mode in {"gemini-incoherent", "local-incoherent"} else (1, 2)
    )
    catalog = tuple(
        EpisodeCatalogEntry(f"S01E{episode:02d}", 1, episode, title, "overview", 1200)
        for episode, title in zip(catalog_episodes, ("First", "Second"), strict=True)
    )
    monkeypatch.setattr(
        analysis,
        "search_tv_show_candidates",
        lambda _name: (TvShowCandidate(1, "Example Series", "", None, ""),),
    )
    monkeypatch.setattr(
        analysis, "fetch_aired_episode_catalog", lambda _show_id: catalog
    )

    class FakeDossier:
        def __init__(self):
            self.attempts = []

        def record_attempt(self, media_ids, **details):
            self.attempts.append((media_ids, details))

        def safe_attempts(self, _media_id):
            return ()

    dossier = FakeDossier()
    monkeypatch.setattr(
        analysis,
        "collect_dossier_evidence",
        lambda items, *_args: (
            tuple(
                UnmatchedFileEvidence(
                    item.media_id,
                    4000
                    if mode in {"local-runtime-mismatch", "runtime-mismatch"}
                    and index == 1
                    else 1200,
                    ("useful dialogue",),
                )
                for index, (item, _payload) in enumerate(items)
            ),
            dossier,
        ),
    )
    monkeypatch.setattr(
        analysis,
        "plan_disc_sequences",
        lambda *_args, **_kwargs: SimpleNamespace(
            disposition="proposed",
            score=0.72,
            global_margin=0.03,
            groups=(
                SimpleNamespace(
                    items=tuple(
                        SimpleNamespace(
                            file_id=media_id, proposed_episode=entry.episode_id
                        )
                        for media_id, entry in zip(
                            media_ids, reversed(catalog), strict=True
                        )
                    )
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        analysis,
        "discover_opensubtitles_season",
        lambda *_args, **_kwargs: (1, 2),
    )
    if mode.startswith("subtitle-partial-gemini"):

        def match_one_subtitle(files, *_args, diagnostic_details=None, **_kwargs):
            assert tuple(item.file_id for item in files) == tuple(media_ids)
            if diagnostic_details is not None:
                diagnostic_details.update({
                    media_ids[0]: {
                        "best_score": 0.94,
                        "runner_up_score": 0.2,
                        "margin": 0.74,
                        "qualifying_window_count": 2,
                        "candidate_episode_id": catalog[0].episode_id,
                        "reason": "accepted",
                    },
                    media_ids[1]: {
                        "best_score": 0.65,
                        "runner_up_score": 0.61,
                        "margin": 0.04,
                        "qualifying_window_count": 0,
                        "candidate_episode_id": catalog[1].episode_id,
                        "reason": "below_confidence_threshold",
                    },
                })
            return {media_ids[0]: catalog[0]}, {
                media_ids[0]: (0.94, 0.74),
                media_ids[1]: (0.65, 0.04),
            }

        monkeypatch.setattr(analysis, "match_opensubtitles_seasons", match_one_subtitle)
    elif mode in {"local", "local-incoherent", "local-runtime-mismatch"}:

        def match_local(files, *_args, diagnostic_details=None, **_kwargs):
            assert _kwargs["min_confidence"] == 0.7
            count = 1 if mode == "local-runtime-mismatch" else 2
            matched = {
                item.file_id: catalog[index] for index, item in enumerate(files[:count])
            }
            if diagnostic_details is not None:
                diagnostic_details.update({
                    file_id: {
                        "best_score": 0.94,
                        "runner_up_score": 0.2,
                        "margin": 0.74,
                        "qualifying_window_count": 2,
                        "candidate_episode_id": entry.episode_id,
                        "reason": "accepted",
                    }
                    for file_id, entry in matched.items()
                })
            return matched, dict.fromkeys(matched, (0.94, 0.74))

        monkeypatch.setattr(analysis, "match_opensubtitles_seasons", match_local)
    else:
        monkeypatch.setattr(
            analysis,
            "match_opensubtitles_seasons",
            lambda *_args, **_kwargs: ({}, {}),
        )
    if use_gemini:

        class FakeRanker:
            def __init__(self, *, model, fallback_models=()):
                assert model == "gemini-test"
                assert tuple(fallback_models) == ()
                self.calls = 0

            def rank_with_configured_keys(
                self,
                files,
                _catalog,
                *,
                prior_attempts=None,
                existing_episode_ids=None,
            ):
                assert prior_attempts is not None
                assert existing_episode_ids == frozenset()
                if mode.startswith("subtitle-partial-gemini"):
                    assert tuple(item.file_id for item in files) == (media_ids[1],)
                    assert tuple(entry.episode_id for entry in _catalog) == (
                        catalog[1].episode_id,
                    )
                if mode == "subtitle-partial-gemini-failed":
                    raise RuntimeError("synthetic provider failure")
                self.calls += 1
                return SimpleNamespace(
                    matches=tuple(
                        SimpleNamespace(
                            file_id=item.file_id,
                            episode_id=(
                                None
                                if mode == "partial-gemini" and index == 1
                                else (
                                    catalog[1].episode_id
                                    if mode == "unstable-gemini"
                                    and self.calls == 2
                                    and index == 0
                                    else entry.episode_id
                                )
                            ),
                            confidence=(
                                0.4 if mode == "partial-gemini" and index == 1 else 0.8
                            ),
                        )
                        for index, (item, entry) in enumerate(
                            zip(files, _catalog, strict=True)
                        )
                    )
                )

        monkeypatch.setattr(analysis, "GeminiEpisodeRanker", FakeRanker)
    for media_id in media_ids:
        store.choose_review_path(media_id, "all_season_analysis_running")

    execute = lambda: analysis.execute_unmatched_disc_analysis(  # noqa: E731
        store,
        fingerprint,
        "Example Series",
        SimpleNamespace(
            ffprobe_path=None,
            ffmpeg_path=None,
            asr_model_name="small",
            automatic_gemini_ambiguity_fallback=False,
            gemini_model="gemini-test",
            min_confidence=0.5 if mode == "local" else 0.7,
        ),
        SimpleNamespace(),
        contracts,
        allow_gemini=use_gemini,
        allow_content_fallback=False,
    )
    if mode in {"gemini-incoherent", "local-incoherent"}:
        with pytest.raises(analysis.DiscCoherenceError) as exc_info:
            execute()
        assert exc_info.value.review_code == "whole_disc_coherence_review_required"
        assert all(item.state == "review_required" for item in store.list_items())
        assert any(
            details.get("summary", {}).get("reason")
            == (
                "gemini_disc_assignments_incoherent"
                if mode == "gemini-incoherent"
                else "whole_disc_assignments_incoherent"
            )
            for _media_ids, details in dossier.attempts
        )
        return
    applied = execute()

    expected_applied = (
        tuple(media_ids[:1])
        if mode
        in {
            "local-runtime-mismatch",
            "partial-gemini",
            "runtime-mismatch",
            "subtitle-partial-gemini-failed",
        }
        else (tuple(media_ids[1:]) if mode == "unstable-gemini" else tuple(media_ids))
    )
    assert applied == expected_applied
    scored = [
        event
        for event in store.list_events()
        if event.event_type == "sequence_match_scored"
    ]
    assert len(scored) == 2
    assert scored[0].details == {
        "best_score": 0.72,
        "candidate_scope": "all",
        "catalog_episode_count": 2,
        "disposition": "proposed",
        "file_count": 2,
        "global_margin": 0.03,
        "library_episode_count": 0,
        "runner_up_score": 0.69,
    }
    for item in store.list_items():
        if item.media_id not in expected_applied:
            assert item.state == "review_required"
            assert item.review_code == "independent_episode_evidence_required"
            continue
        assert item.state == "queued"
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
        assert payload["media_context"]["series_name"] == "Example Series"
        assert payload["media_context"]["episode_assignment_source"] == (
            "all-season-independent-evidence"
        )
        assert payload["media_context"]["identification_policy_version"] == 4
        assert len(payload["media_context"]["episode_assignments"]) == len(
            expected_applied
        )
        assert [
            assignment["season"]
            for assignment in payload["media_context"]["episode_assignments"]
        ] == [
            catalog[media_ids.index(media_id)].season for media_id in expected_applied
        ]
        if mode.startswith("subtitle-partial-gemini"):
            assert [
                assignment["provisional_match"]
                for assignment in payload["media_context"]["episode_assignments"]
            ] == ([False, True] if mode == "subtitle-partial-gemini" else [False])
        else:
            assert all(
                assignment["provisional_match"] is use_gemini
                for assignment in payload["media_context"]["episode_assignments"]
            )
        expected_sources = (
            ["opensubtitles-two-window", "gemini-two-pass"]
            if mode == "subtitle-partial-gemini"
            else ["opensubtitles-two-window"]
            if mode
            in {
                "local-runtime-mismatch",
                "subtitle-partial-gemini-failed",
            }
            else ["opensubtitles-two-window", "opensubtitles-two-window"]
            if mode == "local"
            else ["gemini-two-pass"] * len(expected_applied)
        )
        assert [
            assignment["evidence_source"]
            for assignment in payload["media_context"]["episode_assignments"]
        ] == expected_sources
    sequence_attempts = [
        details
        for _media_ids, details in dossier.attempts
        if details["branch"] == "tv-local"
    ]
    assert len(sequence_attempts) == 2
    assert {attempt["disposition"] for attempt in sequence_attempts} == {"review"}
