import json
from types import SimpleNamespace

import pytest

from mkv_episode_matcher.backend import unmatched_disc_analysis as analysis
from mkv_episode_matcher.core.credentials import ApiCredentialError, ApiServiceError
from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiResponseError,
    UnmatchedFileEvidence,
)
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore, build_artifact


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
                "media_context": {"series_name": "Unmatched", "season": None},
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
        analysis, "fetch_aired_episode_catalog_for_show", lambda _name: catalog
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

    analysis.execute_unmatched_disc_analysis(
        store,
        fingerprint,
        "Example Series",
        SimpleNamespace(
            ffprobe_path=None,
            ffmpeg_path=None,
            asr_model_name="small",
            gemini_model="gemini-test",
            jellyfin_tv_root=tmp_path,
        ),
        SimpleNamespace(),
        contracts,
    )

    assert seen_catalogs == [catalog]
    diagnostic = next(
        event
        for event in store.list_events()
        if event.event_type == "sequence_match_scored"
    )
    assert diagnostic.details["candidate_scope"] == "all"
    assert diagnostic.details["catalog_episode_count"] == 4


@pytest.mark.parametrize(
    "mode",
    [
        "local",
        "local-runtime-mismatch",
        "gemini",
        "partial-gemini",
        "runtime-mismatch",
        "unstable-gemini",
    ],
)
def test_all_season_analysis_applies_sequence_without_saved_release_map(
    tmp_path, monkeypatch, mode
):
    use_gemini = mode not in {"local", "local-runtime-mismatch"}
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

    catalog = tuple(
        EpisodeCatalogEntry(f"S0{index + 1}E01", index + 1, 1, title, "overview", 1200)
        for index, title in enumerate(("First", "Second"))
    )
    monkeypatch.setattr(
        analysis, "fetch_aired_episode_catalog_for_show", lambda _name: catalog
    )

    class FakeDossier:
        attempts = []

        def record_attempt(self, media_ids, **details):
            self.attempts.append((media_ids, details))

        def safe_attempts(self, _media_id):
            return ()

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
            FakeDossier(),
        ),
    )
    monkeypatch.setattr(
        analysis,
        "plan_disc_sequences",
        lambda *_args, **_kwargs: SimpleNamespace(
            disposition="review" if use_gemini else "proposed",
            score=0.72,
            global_margin=0.03,
            groups=(
                SimpleNamespace(
                    items=tuple(
                        SimpleNamespace(
                            file_id=media_id, proposed_episode=entry.episode_id
                        )
                        for media_id, entry in zip(media_ids, catalog, strict=True)
                    )
                ),
            ),
        ),
    )
    if use_gemini:

        class FakeRanker:
            def __init__(self, *, model):
                assert model == "gemini-test"
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
                            zip(files, catalog, strict=True)
                        )
                    )
                )

        monkeypatch.setattr(analysis, "GeminiEpisodeRanker", FakeRanker)
    for media_id in media_ids:
        store.choose_review_path(media_id, "all_season_analysis_running")

    applied = analysis.execute_unmatched_disc_analysis(
        store,
        fingerprint,
        "Example Series",
        SimpleNamespace(
            ffprobe_path=None,
            ffmpeg_path=None,
            asr_model_name="small",
            automatic_gemini_ambiguity_fallback=False,
            gemini_model="gemini-test",
            min_confidence=0.7,
        ),
        SimpleNamespace(),
        contracts,
        allow_gemini=use_gemini,
        allow_content_fallback=False,
    )

    expected_applied = (
        tuple(media_ids[:1])
        if mode in {"local-runtime-mismatch", "partial-gemini", "runtime-mismatch"}
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
        "disposition": "review" if use_gemini else "proposed",
        "file_count": 2,
        "global_margin": 0.03,
        "library_episode_count": 0,
        "runner_up_score": 0.69,
    }
    for item in store.list_items():
        if item.media_id not in expected_applied:
            assert item.state == "review_required"
            assert item.review_code == "all_season_sequence_review_required"
            continue
        assert item.state == "queued"
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
        assert payload["media_context"]["series_name"] == "Example Series"
        assert payload["media_context"]["episode_assignment_source"] == (
            "all-season-analysis"
        )
        assert payload["media_context"]["identification_policy_version"] == 2
        assert len(payload["media_context"]["episode_assignments"]) == len(
            expected_applied
        )
        assert all(
            assignment["provisional_match"] is use_gemini
            for assignment in payload["media_context"]["episode_assignments"]
        )
