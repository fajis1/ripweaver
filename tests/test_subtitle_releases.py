from pathlib import Path
from types import SimpleNamespace

import pytest

from mkv_episode_matcher.core.matcher import MultiSegmentMatcher
from mkv_episode_matcher.core.models import EpisodeInfo, MatchCandidate, SubtitleFile
from mkv_episode_matcher.core.providers.subtitles import OpenSubtitlesProvider
from mkv_episode_matcher.core.subtitle_releases import (
    classify_subtitle_release,
    infer_subtitle_release_profile,
    select_subtitle_release_options,
)


def _candidate(name: str, episode: int = 10):
    return SimpleNamespace(
        file_name=name,
        files=[{"file_name": name}],
        season_number=5,
        episode_number=episode,
    )


def _provider(tmp_path, candidates):
    provider = OpenSubtitlesProvider.__new__(OpenSubtitlesProvider)
    provider.config = SimpleNamespace(cache_dir=tmp_path)
    provider.client = object()
    provider._credential_error = None
    provider._auth_error = None
    provider._credential_retry_attempted = False
    searches = []

    def search(**kwargs):
        searches.append(kwargs)
        return SimpleNamespace(data=list(candidates))

    counter = iter(range(100))

    def download(_candidate):
        target = tmp_path / f"provider-{next(counter)}.srt"
        target.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nSynthetic\n",
            encoding="utf-8",
        )
        return target

    provider._search_with_retry = search
    provider._download_with_retry = download
    return provider, searches


def test_superfan_profile_keeps_canonical_series_and_classifies_releases():
    profile = infer_subtitle_release_profile("The Office Superfan Episodes")

    assert profile.key == "superfan"
    assert profile.canonical_series_name == "The Office"
    assert (
        classify_subtitle_release(
            "The.Office.Superfan.Episodes.S05E10.Extended.Cut.PCOK", profile
        )
        == "exact"
    )
    assert classify_subtitle_release("The.Office.S05E10.Extended", profile) == (
        "compatible"
    )
    assert classify_subtitle_release("The.Office.S05E10.HDTV", profile) == "generic"


def test_release_ranking_keeps_two_options_and_prefers_exact_metadata():
    profile = infer_subtitle_release_profile("The Office Superfan Episodes")
    regular = _candidate("The.Office.S05E10.HDTV.srt")
    compatible = _candidate("The.Office.S05E10.Extended.Cut.srt")
    exact = _candidate("The.Office.Superfan.Episodes.S05E10.PCOK.srt")

    selected = select_subtitle_release_options(
        (regular, compatible, exact), profile, maximum=2
    )

    assert [item.release_match for item in selected] == ["exact", "compatible"]


def test_unknown_release_retains_multiple_provider_options():
    profile = infer_subtitle_release_profile("Example Show")
    selected = select_subtitle_release_options(
        (
            _candidate("Example.Show.S05E10.WEB.srt"),
            _candidate("Example.Show.S05E10.BLURAY.srt"),
            _candidate("Example.Show.S05E10.HDTV.srt"),
        ),
        profile,
        maximum=2,
    )

    assert profile.key is None
    assert len(selected) == 2
    assert {item.release_match for item in selected} == {"unresolved"}


def test_provider_downloads_bounded_superfan_variants_and_restores_metadata(tmp_path):
    provider, searches = _provider(
        tmp_path,
        (
            _candidate("The.Office.S05E10.HDTV.srt"),
            _candidate("The.Office.S05E10.Extended.Cut.srt"),
            _candidate("The.Office.Superfan.Episodes.S05E10.PCOK.srt"),
        ),
    )

    subtitles = provider.get_subtitles("The Office Superfan Episodes", 5, [])

    assert len(subtitles) == 2
    assert [item.release_match for item in subtitles] == ["exact", "compatible"]
    assert all(item.release_profile == "superfan" for item in subtitles)
    assert searches[0]["query"] == "The Office Superfan Episodes S05"
    assert searches[1]["query"] == "The Office S05"

    provider._search_with_retry = lambda **_kwargs: pytest.fail(
        "A complete release-aware cache should avoid another provider search"
    )
    cached = provider.get_subtitles("The Office Superfan Episodes", 5, [])
    assert len(cached) == 2
    assert {item.release_match for item in cached} == {"exact", "compatible"}


def test_matcher_collapses_release_variants_to_one_vote_per_episode(
    tmp_path, monkeypatch
):
    class _Asr:
        def transcribe(self, _path):
            return "recognizable dialogue"

        def calculate_match_score(self, _transcript, reference):
            if "exact scene" in reference:
                return 0.80
            if "generic scene" in reference:
                return 0.70
            return 0.75

    monkeypatch.setattr(
        "mkv_episode_matcher.core.matcher.extract_audio_chunk",
        lambda *_args, **_kwargs: None,
    )
    references = [
        SubtitleFile(
            path=tmp_path / "generic.srt",
            content="1\n00:00:00,000 --> 00:01:00,000\ngeneric scene\n",
            episode_info=EpisodeInfo(series_name="The Office", season=5, episode=10),
            release_match="generic",
        ),
        SubtitleFile(
            path=tmp_path / "exact.srt",
            content="1\n00:00:00,000 --> 00:01:00,000\nexact scene\n",
            episode_info=EpisodeInfo(series_name="The Office", season=5, episode=10),
            release_match="exact",
        ),
        SubtitleFile(
            path=tmp_path / "other.srt",
            content="1\n00:00:00,000 --> 00:01:00,000\nother scene\n",
            episode_info=EpisodeInfo(series_name="The Office", season=5, episode=11),
            release_match="generic",
        ),
    ]
    matcher = MultiSegmentMatcher(_Asr(), temp_dir=tmp_path)

    candidates = matcher._process_chunk(Path("synthetic.mkv"), 0.0, references)

    assert len(candidates) == 2
    episode_ten = next(item for item in candidates if item.episode_info.episode == 10)
    assert episode_ten.confidence == 0.80
    assert episode_ten.subtitle_release_match == "exact"
    evaluations = matcher._last_segment_trace["candidate_evaluations"]
    assert [item["candidate_episode_id"] for item in evaluations] == [
        "S05E10",
        "S05E11",
    ]
    assert evaluations[0]["subtitle_release_match"] == "exact"
    assert evaluations[0]["score"] == 0.80
    assert matcher._last_segment_trace["reference_variant_count"] == 3


def test_matcher_result_retains_window_votes_and_runner_up(tmp_path, monkeypatch):
    class _Asr:
        def transcribe(self, _path):
            return "recognizable dialogue"

        def calculate_match_score(self, _transcript, reference):
            return 0.80 if "winner" in reference else 0.65

    monkeypatch.setattr(
        "mkv_episode_matcher.core.matcher.get_video_duration", lambda _path: 1200
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.core.matcher.extract_audio_chunk",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "mkv_episode_matcher.core.matcher.SubtitleReader.extract_subtitle_chunk",
        lambda content, *_args: [content],
    )
    references = [
        SubtitleFile(
            path=tmp_path / "winner.srt",
            content="winner scene",
            episode_info=EpisodeInfo(
                series_name="The Office", season=5, episode=8, title="Winner"
            ),
            release_match="exact",
        ),
        SubtitleFile(
            path=tmp_path / "runner-up.srt",
            content="runner up scene",
            episode_info=EpisodeInfo(
                series_name="The Office", season=5, episode=9, title="Runner Up"
            ),
            release_match="generic",
        ),
    ]

    result = MultiSegmentMatcher(_Asr(), temp_dir=tmp_path).match(
        Path("synthetic.mkv"), references
    )

    assert result is not None
    assert result.episode_info.s_e_format == "S05E08"
    assert result.decision_trace["selected_vote_count"] == 6
    assert result.decision_trace["runner_up_episode_id"] == "S05E09"
    assert result.decision_trace["runner_up_score"] == 0.65
    assert len(result.decision_trace["segments"]) == 6


def test_matcher_uses_six_offset_windows_to_confirm_a_single_vote(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "mkv_episode_matcher.core.matcher.get_video_duration", lambda _path: 1800
    )
    matcher = MultiSegmentMatcher(SimpleNamespace(), temp_dir=tmp_path)
    candidate = MatchCandidate(
        episode_info=EpisodeInfo(
            series_name="The Office", season=6, episode=11, title="Eleventh"
        ),
        confidence=0.89,
        reference_file=tmp_path / "reference.srt",
        subtitle_release_match="generic",
    )
    checked = []

    def process_chunk(_video, start, _references, *, chunk_index, **_kwargs):
        checked.append((chunk_index, start))
        matcher._last_segment_trace = {
            "segment_index": chunk_index,
            "sample_start_seconds": start,
            "status": "matched" if chunk_index in {0, 6} else "below_threshold",
        }
        return [candidate] if chunk_index in {0, 6} else []

    monkeypatch.setattr(matcher, "_process_chunk", process_chunk)

    result = matcher.match(Path("synthetic.mkv"), [])

    assert result is not None
    assert len(checked) == 12
    assert result.decision_trace["selected_episode_id"] == "S06E11"
    assert result.decision_trace["selected_vote_count"] == 2
    assert result.decision_trace["supplemental_attempted"] is True
    assert result.decision_trace["supplemental_segment_count"] == 6
    assert len(result.decision_trace["segments"]) == 12
    assert result.decision_trace["segments"][6]["phase"] == ("offset-six-window-retry")


def test_matcher_uses_offset_windows_after_zero_initial_candidates(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "mkv_episode_matcher.core.matcher.get_video_duration", lambda _path: 1800
    )
    matcher = MultiSegmentMatcher(SimpleNamespace(), temp_dir=tmp_path)
    candidate = MatchCandidate(
        episode_info=EpisodeInfo(
            series_name="The Office", season=6, episode=12, title="Twelfth"
        ),
        confidence=0.82,
        reference_file=tmp_path / "reference.srt",
        subtitle_release_match="generic",
    )
    checked = []

    def process_chunk(_video, start, _references, *, chunk_index, **_kwargs):
        checked.append((chunk_index, start))
        matched = chunk_index in {6, 7}
        matcher._last_segment_trace = {
            "segment_index": chunk_index,
            "sample_start_seconds": start,
            "status": "matched" if matched else "below_threshold",
        }
        return [candidate] if matched else []

    monkeypatch.setattr(matcher, "_process_chunk", process_chunk)

    result = matcher.match(Path("synthetic.mkv"), [], acceptance_threshold=0.7)

    assert result is not None
    assert len(checked) == 12
    assert result.decision_trace["selected_episode_id"] == "S06E12"
    assert result.decision_trace["selected_vote_count"] == 2
    assert result.decision_trace["supplemental_attempted"] is True
    assert result.decision_trace["supplemental_segment_count"] == 6
    assert result.decision_trace["supplemental_reason"] == (
        "no_initial_qualifying_candidate"
    )


def test_matcher_uses_offset_windows_when_initial_winner_is_below_engine_threshold(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "mkv_episode_matcher.core.matcher.get_video_duration", lambda _path: 1800
    )
    matcher = MultiSegmentMatcher(SimpleNamespace(), temp_dir=tmp_path)
    initial = MatchCandidate(
        episode_info=EpisodeInfo(
            series_name="The Office", season=6, episode=12, title="Twelfth"
        ),
        confidence=0.65,
        reference_file=tmp_path / "reference.srt",
        subtitle_release_match="generic",
    )
    supplemental = initial.model_copy(update={"confidence": 0.76})
    checked = []

    def process_chunk(_video, start, _references, *, chunk_index, **_kwargs):
        checked.append((chunk_index, start))
        matcher._last_segment_trace = {
            "segment_index": chunk_index,
            "sample_start_seconds": start,
            "status": "matched",
        }
        if chunk_index in {0, 1}:
            return [initial]
        if chunk_index == 6:
            return [supplemental]
        return []

    monkeypatch.setattr(matcher, "_process_chunk", process_chunk)

    result = matcher.match(Path("synthetic.mkv"), [], acceptance_threshold=0.7)

    assert result is not None
    assert len(checked) == 12
    assert result.confidence == 0.76
    assert result.decision_trace["supplemental_attempted"] is True
    assert result.decision_trace["supplemental_reason"] == (
        "initial_winner_below_engine_threshold"
    )
