from types import SimpleNamespace

from mkv_episode_matcher.core.models import EpisodeInfo, SubtitleFile
from mkv_episode_matcher.core.providers.subtitles import (
    OpenSubtitlesProvider,
    _complete_cached_season,
    _write_season_manifest,
)


def _cached_subtitle(tmp_path, episode: int) -> SubtitleFile:
    path = tmp_path / f"episode-{episode}.srt"
    path.write_text("Synthetic\n", encoding="utf-8")
    return SubtitleFile(
        path=path,
        episode_info=EpisodeInfo(
            series_name="The Flintstones",
            season=6,
            episode=episode,
        ),
    )


def test_complete_season_manifest_requires_every_expected_episode(tmp_path):
    cache = tmp_path / "data" / "The Flintstones"
    cache.mkdir(parents=True)
    _write_season_manifest(
        cache,
        season=6,
        tmdb_id=1996,
        episode_numbers={1, 2},
        complete=True,
    )

    assert _complete_cached_season(
        cache,
        6,
        [_cached_subtitle(tmp_path, 1), _cached_subtitle(tmp_path, 2)],
        1996,
    )
    assert not _complete_cached_season(
        cache,
        6,
        [_cached_subtitle(tmp_path, 1)],
        1996,
    )
    assert not _complete_cached_season(
        cache,
        6,
        [_cached_subtitle(tmp_path, 1), _cached_subtitle(tmp_path, 2)],
        1234,
    )


def test_opensubtitles_uses_cached_season_without_live_client(tmp_path):
    cache = tmp_path / "data" / "The Flintstones"
    cache.mkdir(parents=True)
    (cache / "The Flintstones - S06E01.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nSynthetic\n",
        encoding="utf-8",
    )
    provider = OpenSubtitlesProvider.__new__(OpenSubtitlesProvider)
    provider.config = SimpleNamespace(cache_dir=tmp_path)
    provider.client = None
    provider._credential_error = None
    provider._auth_error = "offline"

    subtitles = provider.get_subtitles("The Flintstones", 6, [], 1996)

    assert len(subtitles) == 1
    assert subtitles[0].episode_info.season == 6
    assert subtitles[0].episode_info.episode == 1


def test_complete_cache_manifest_bypasses_live_provider(tmp_path):
    cache = tmp_path / "data" / "The Flintstones"
    cache.mkdir(parents=True)
    (cache / "The Flintstones - S06E01.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nSynthetic\n",
        encoding="utf-8",
    )
    _write_season_manifest(
        cache,
        season=6,
        tmdb_id=1996,
        episode_numbers={1},
        complete=True,
    )
    provider = OpenSubtitlesProvider.__new__(OpenSubtitlesProvider)
    provider.config = SimpleNamespace(cache_dir=tmp_path)
    provider.client = object()

    subtitles = provider.get_subtitles("The Flintstones", 6, [], 1996)

    assert len(subtitles) == 1
    assert subtitles[0].episode_info.episode == 1
