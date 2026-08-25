from types import SimpleNamespace

import pytest

from mkv_episode_matcher.backend import related_movie_analysis as analysis
from mkv_episode_matcher.core.models import Config, SubtitleFile
from mkv_episode_matcher.media.gemini_matcher import UnmatchedFileEvidence
from mkv_episode_matcher.tmdb_client import MovieCandidate


class _SubtitleProvider:
    def __init__(self, references):
        self.references = references

    def get_movie_subtitle(self, _title, *, tmdb_id, year=None):
        content = self.references.get(tmdb_id)
        if content is None:
            return None
        return SubtitleFile(path=f"tmdb-{tmdb_id}.srt", content=content)


class _ASR:
    @staticmethod
    def calculate_match_score(transcript, reference):
        return 0.96 if transcript in reference else 0.1


def _movie(tmdb_id, title, runtime_minutes):
    return MovieCandidate(
        tmdb_id=tmdb_id,
        title=title,
        original_title=title,
        release_year=1966,
        overview="",
        runtime_seconds=runtime_minutes * 60,
    )


def test_feature_length_tv_title_matches_related_movie_subtitles(monkeypatch):
    candidates = (
        _movie(1, "The Man Called Flintstone", 89),
        _movie(2, "The Flintstones", 91),
    )
    monkeypatch.setattr(
        analysis, "search_movie_candidates", lambda _name, limit: candidates
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.unmatched_disc_analysis.SubtitleReader.extract_subtitle_chunk",
        lambda *_args, **_kwargs: ["unrelated timestamp dialogue"],
    )
    evidence = (
        UnmatchedFileEvidence(
            "title-020",
            89 * 60,
            (
                "rare rock slag secret agent anchor",
                "inserted restoration dialogue",
                "distinct green goose mission anchor",
            ),
        ),
    )
    provider = _SubtitleProvider({
        1: (
            "rare rock slag secret agent anchor ordinary film dialogue "
            "distinct green goose mission anchor"
        ),
        2: "live action quarry dialogue with no matching anchors",
    })

    matches, diagnostics = analysis.match_related_tv_movies(
        evidence,
        "The Flintstones",
        Config(min_confidence=0.7),
        _ASR(),
        subtitle_provider=provider,
    )

    assert matches["title-020"].candidate.title == "The Man Called Flintstone"
    assert matches["title-020"].qualifying_window_count == 2
    assert diagnostics["title-020"]["reason"] == "accepted"


def test_related_movie_match_requires_feature_length_runtime(monkeypatch):
    monkeypatch.setattr(
        analysis,
        "search_movie_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Short extras must not trigger movie lookup")
        ),
    )

    matches, diagnostics = analysis.match_related_tv_movies(
        (UnmatchedFileEvidence("short-extra", 600, ("short dialogue",)),),
        "Example Show",
        Config(),
        SimpleNamespace(),
    )

    assert matches == {}
    assert diagnostics == {}


def test_related_movie_match_rejects_ambiguous_subtitle_candidates(monkeypatch):
    candidates = (_movie(1, "First Film", 89), _movie(2, "Second Film", 90))
    monkeypatch.setattr(
        analysis, "search_movie_candidates", lambda _name, limit: candidates
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.unmatched_disc_analysis._score_subtitle",
        lambda _asr, _excerpts, content, _duration: (
            (0.9 if content == "first" else 0.86),
            (0.9, 0.9) if content == "first" else (0.86, 0.86),
        ),
    )

    matches, diagnostics = analysis.match_related_tv_movies(
        (UnmatchedFileEvidence("feature", 89 * 60, ("one", "two")),),
        "Example Show",
        Config(min_confidence=0.7),
        SimpleNamespace(),
        subtitle_provider=_SubtitleProvider({1: "first", 2: "second"}),
    )

    assert matches == {}
    assert diagnostics["feature"]["reason"] == "ambiguous_runner_up"
    assert diagnostics["feature"]["margin"] == pytest.approx(0.04)
