import json
import re
from types import SimpleNamespace

from mkv_episode_matcher.backend.gemini_fallback import (
    _descriptive_release_hint,
    execute_gemini_fallback,
)
from mkv_episode_matcher.backend.related_movie_analysis import RelatedMovieMatch
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiDescriptivePlan,
    GeminiDescriptiveResult,
    UnmatchedFileEvidence,
)
from mkv_episode_matcher.tmdb_client import MovieCandidate


class FakeStore:
    def __init__(self, items):
        self.items = {item.media_id: item for item in items}
        self.applied = {}
        self.visual_reviews = {}

    def get(self, media_id):
        return self.items[media_id]

    def apply_reviewed_identification_input(self, media_id, artifact):
        self.applied[media_id] = artifact

    def choose_review_path(self, media_id, code):
        self.items[media_id].review_code = code

    def record_silent_video_review(self, media_id, category):
        self.visual_reviews[media_id] = category


class FakeDossier:
    def __init__(self):
        self.attempts = []

    def record_attempt(self, media_ids, **details):
        self.attempts.append((media_ids, details))

    def safe_attempts(self, _media_id):
        return ()

    def attempted(self, _media_id, _branch):
        return False


def test_descriptive_release_hint_prefers_real_contract_series():
    assert (
        _descriptive_release_hint(
            {"media_context": {"series_name": "FAERIE TALE THEATRE"}},
            "Faerie-Tale-Theatre-5--disc-01-deadbeef-title-000",
        )
        == "FAERIE TALE THEATRE"
    )


def test_descriptive_release_hint_recovers_legacy_semipretty_media_id():
    assert (
        _descriptive_release_hint(
            {"media_context": {"series_name": "Unmatched"}},
            "Faerie-Tale-Theatre-5--disc-01-deadbeef-title-000",
        )
        == "Faerie Tale Theatre 5"
    )


def _item(tmp_path, media_id, duration):
    source = tmp_path / f"{media_id}.mkv"
    source.write_bytes(b"synthetic")
    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "source_path": str(source),
            "source_size_bytes": source.stat().st_size,
            "disc_fingerprint": "0123456789abcdef",
            "title_index": int(re.search(r"-title-(\d{3})", media_id).group(1)),
            "media_context": {
                "series_name": "Example Double Feature",
                "content_hint": "extras",
                "special_feature_catalog_id": None,
                "special_feature_assignments": [],
            },
            "test_duration": duration,
        }),
        encoding="utf-8",
    )
    return SimpleNamespace(
        media_id=media_id,
        review_code="gemini_analysis_running",
        artifact=SimpleNamespace(contract_path=contract),
    )


def test_catalogue_free_fallback_applies_mixed_provisional_results(
    tmp_path, monkeypatch
):
    items = [
        _item(tmp_path, "disc-title-000", 7200),
        _item(tmp_path, "disc-title-003-recovery-abcd", 300),
    ]
    store = FakeStore(items)
    durations = {
        item.media_id: 7200 if "title-000" in item.media_id else 300 for item in items
    }

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.collect_dossier_evidence",
        lambda selected, *_args: (
            tuple(
                UnmatchedFileEvidence(
                    item.media_id, durations[item.media_id], ("bounded evidence",)
                )
                for item, _payload in selected
            ),
            FakeDossier(),
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.GeminiDescriptiveRanker.describe_with_configured_keys",
        lambda self, evidence, release_hint, prior_attempts=None: GeminiDescriptivePlan(
            mode="gemini-descriptive-review-plan",
            model="test",
            matches=(
                GeminiDescriptiveResult(
                    "disc-title-000",
                    "movie",
                    "Example Movie",
                    2001,
                    0.9,
                    ("Feature-length evidence.",),
                ),
                GeminiDescriptiveResult(
                    "disc-title-003-recovery-abcd",
                    "extra",
                    "Making Example Movie",
                    None,
                    0.7,
                    ("Short production evidence.",),
                ),
            ),
        ),
    )

    applied = execute_gemini_fallback(
        store,
        tuple(item.media_id for item in items),
        Config(gemini_model="test"),
        SimpleNamespace(),
        tmp_path / "contracts",
    )

    assert applied == ("disc-title-000", "disc-title-003-recovery-abcd")
    movie = json.loads(
        store.applied["disc-title-000"].contract_path.read_text(encoding="utf-8")
    )
    extra = json.loads(
        store.applied["disc-title-003-recovery-abcd"].contract_path.read_text(
            encoding="utf-8"
        )
    )
    assert (
        movie["media_context"]["special_feature_assignments"][0]["media_kind"]
        == "movie"
    )
    assert movie["media_context"]["special_feature_library_year"] == 2001
    assert (
        extra["media_context"]["special_feature_assignments"][0]["media_kind"]
        == "extra"
    )


def test_descriptive_tv_result_reuses_evidence_in_all_season_matcher(
    tmp_path, monkeypatch
):
    items = [
        _item(tmp_path, "disc-title-000", 1200),
        _item(tmp_path, "disc-title-001", 1200),
    ]
    store = FakeStore(items)
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.collect_dossier_evidence",
        lambda selected, *_args: (
            tuple(
                UnmatchedFileEvidence(item.media_id, 1200, ("episode dialogue",))
                for item, _payload in selected
            ),
            FakeDossier(),
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.GeminiDescriptiveRanker.describe_with_configured_keys",
        lambda self, evidence, release_hint, prior_attempts=None: GeminiDescriptivePlan(
            mode="gemini-descriptive-review-plan",
            model="test",
            matches=tuple(
                GeminiDescriptiveResult(
                    item.file_id,
                    "tv_episode",
                    "Unnumbered television episode",
                    None,
                    0.7,
                    ("Television evidence.",),
                )
                for item in evidence
            ),
        ),
    )
    captured = {}

    def fake_all_season(
        _store,
        fingerprint,
        series_name,
        _config,
        _asr,
        _contract_root,
        **options,
    ):
        captured.update(
            fingerprint=fingerprint, series_name=series_name, options=options
        )
        return tuple(item.media_id for item in items)

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.unmatched_disc_analysis.execute_unmatched_disc_analysis",
        fake_all_season,
    )

    applied = execute_gemini_fallback(
        store,
        tuple(item.media_id for item in items),
        Config(gemini_model="test", automatic_gemini_ambiguity_fallback=True),
        SimpleNamespace(),
        tmp_path / "contracts",
    )

    assert applied == tuple(item.media_id for item in items)
    assert captured["fingerprint"] == "0123456789abcdef"
    assert captured["series_name"] == "Example Double Feature"
    assert captured["options"]["allow_content_fallback"] is False


def test_descriptive_tv_extra_stays_with_canonical_series(tmp_path, monkeypatch):
    item = _item(tmp_path, "disc-title-003", 300)
    payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
    payload["media_context"].update(series_name="The Flintstones", content_hint=None)
    item.artifact.contract_path.write_text(json.dumps(payload), encoding="utf-8")
    store = FakeStore([item])
    dossier = FakeDossier()
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.collect_dossier_evidence",
        lambda selected, *_args: (
            (UnmatchedFileEvidence(item.media_id, 300, ("bounded evidence",)),),
            dossier,
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.GeminiDescriptiveRanker.describe_with_configured_keys",
        lambda self, evidence, release_hint, prior_attempts=None: GeminiDescriptivePlan(
            mode="gemini-descriptive-review-plan",
            model="test",
            matches=(
                GeminiDescriptiveResult(
                    item.media_id,
                    "extra",
                    "Carved in Stone",
                    None,
                    0.8,
                    ("Bonus feature evidence.",),
                ),
            ),
        ),
    )

    applied = execute_gemini_fallback(
        store,
        (item.media_id,),
        Config(gemini_model="test"),
        SimpleNamespace(),
        tmp_path / "contracts",
    )

    assert applied == (item.media_id,)
    revised = json.loads(
        store.applied[item.media_id].contract_path.read_text(encoding="utf-8")
    )
    context = revised["media_context"]
    assignment = context["special_feature_assignments"][0]
    assert context["special_feature_library_title"] == "The Flintstones"
    assert assignment["library_kind"] == "tv"
    assert assignment["jellyfin_folder"] == "Extras"
    assert assignment["match_summary"] == "Bonus feature evidence."
    assert any(details["branch"] == "tv-bonus" for _, details in dossier.attempts)


def test_tv_disc_related_movie_is_validated_before_generic_bonus(tmp_path, monkeypatch):
    item = _item(tmp_path, "disc-title-020", 89 * 60)
    payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
    payload["media_context"].update(series_name="The Flintstones", content_hint=None)
    item.artifact.contract_path.write_text(json.dumps(payload), encoding="utf-8")
    store = FakeStore([item])
    dossier = FakeDossier()
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.collect_dossier_evidence",
        lambda selected, *_args: (
            (
                UnmatchedFileEvidence(
                    item.media_id,
                    89 * 60,
                    ("first movie anchor", "second movie anchor"),
                ),
            ),
            dossier,
        ),
    )
    candidate = MovieCandidate(
        tmdb_id=123,
        title="The Man Called Flintstone",
        original_title="The Man Called Flintstone",
        release_year=1966,
        overview="",
        runtime_seconds=89 * 60,
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.related_movie_analysis.match_related_tv_movies",
        lambda *_args, **_kwargs: (
            {
                item.media_id: RelatedMovieMatch(
                    candidate=candidate,
                    confidence=0.94,
                    qualifying_window_count=2,
                    margin=0.31,
                )
            },
            {item.media_id: {"reason": "accepted", "candidate_tmdb_id": 123}},
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.GeminiDescriptiveRanker.describe_with_configured_keys",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Validated movie must bypass generic Gemini naming")
        ),
    )

    applied = execute_gemini_fallback(
        store,
        (item.media_id,),
        Config(gemini_model="test"),
        SimpleNamespace(),
        tmp_path / "contracts",
    )

    assert applied == (item.media_id,)
    revised = json.loads(
        store.applied[item.media_id].contract_path.read_text(encoding="utf-8")
    )
    context = revised["media_context"]
    assignment = context["special_feature_assignments"][0]
    assert context["special_feature_library_title"] == "The Man Called Flintstone"
    assert context["special_feature_library_year"] == 1966
    assert assignment["media_kind"] == "movie"
    assert assignment["library_kind"] == "movie"
    assert assignment["provisional_match"] is False
    assert assignment["candidate_feature_ids"] == ["tmdb-movie-123"]
    assert assignment["identification_method"] == "tv-related-movie-opensubtitles"
    assert any(details["branch"] == "tv-movie" for _, details in dossier.attempts)


def test_tv_disc_accepts_provisional_gemini_movie_when_subtitles_are_unavailable(
    tmp_path, monkeypatch
):
    item = _item(tmp_path, "disc-title-020", 89 * 60)
    payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
    payload["media_context"].update(series_name="The Flintstones", content_hint=None)
    item.artifact.contract_path.write_text(json.dumps(payload), encoding="utf-8")
    store = FakeStore([item])
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.collect_dossier_evidence",
        lambda selected, *_args: (
            (UnmatchedFileEvidence(item.media_id, 89 * 60, ("movie dialogue",)),),
            FakeDossier(),
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.related_movie_analysis.match_related_tv_movies",
        lambda *_args, **_kwargs: (
            {},
            {item.media_id: {"reason": "no_usable_movie_subtitles"}},
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.GeminiDescriptiveRanker.describe_with_configured_keys",
        lambda self, evidence, release_hint, prior_attempts=None: GeminiDescriptivePlan(
            mode="gemini-descriptive-review-plan",
            model="test",
            matches=(
                GeminiDescriptiveResult(
                    item.media_id,
                    "movie",
                    "The Man Called Flintstone",
                    1966,
                    0.88,
                    ("Feature-length animated movie evidence.",),
                ),
            ),
        ),
    )

    applied = execute_gemini_fallback(
        store,
        (item.media_id,),
        Config(gemini_model="test"),
        SimpleNamespace(),
        tmp_path / "contracts",
    )

    assert applied == (item.media_id,)
    revised = json.loads(
        store.applied[item.media_id].contract_path.read_text(encoding="utf-8")
    )
    assignment = revised["media_context"]["special_feature_assignments"][0]
    assert assignment["media_kind"] == "movie"
    assert assignment["library_kind"] == "movie"
    assert assignment["provisional_match"] is True


def test_unresolved_descriptive_result_stops_showing_running(tmp_path, monkeypatch):
    item = _item(tmp_path, "disc-title-000", 1200)
    store = FakeStore([item])
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.collect_dossier_evidence",
        lambda selected, *_args: (
            (UnmatchedFileEvidence(item.media_id, 1200, ("episode dialogue",)),),
            FakeDossier(),
        ),
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.GeminiDescriptiveRanker.describe_with_configured_keys",
        lambda self, evidence, release_hint, prior_attempts=None: GeminiDescriptivePlan(
            mode="gemini-descriptive-review-plan",
            model="test",
            matches=(
                GeminiDescriptiveResult(
                    item.media_id,
                    "unknown",
                    "Unresolved title",
                    None,
                    0.4,
                    ("Insufficient evidence.",),
                ),
            ),
        ),
    )

    applied = execute_gemini_fallback(
        store,
        (item.media_id,),
        Config(gemini_model="test"),
        SimpleNamespace(),
        tmp_path / "contracts",
    )

    assert applied == ()
    assert item.review_code == "gemini_descriptive_review_required"


def test_warning_screen_is_held_before_gemini_matching(tmp_path, monkeypatch):
    item = _item(tmp_path, "disc-title-001", 20)
    store = FakeStore([item])

    def fake_collect(selected, _config, _asr, _root, _visual, recorder):
        recorder(item.media_id, "likely_warning_screen")
        return (
            (UnmatchedFileEvidence(item.media_id, 20, ("copyright warning",)),),
            FakeDossier(),
        )

    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.collect_dossier_evidence",
        fake_collect,
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.gemini_fallback.GeminiDescriptiveRanker.describe_with_configured_keys",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Warning screens must not be sent to Gemini matching")
        ),
    )

    handled = execute_gemini_fallback(
        store,
        (item.media_id,),
        Config(gemini_model="test"),
        SimpleNamespace(),
        tmp_path / "contracts",
    )

    assert handled == (item.media_id,)
    assert store.visual_reviews == {item.media_id: "likely_warning_screen"}
    assert item.review_code == "visual_content_review_required"
    assert store.applied == {}
