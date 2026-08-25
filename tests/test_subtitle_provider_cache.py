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


def test_movie_subtitle_search_uses_tmdb_id_and_then_cache(tmp_path):
    downloaded = tmp_path / "downloaded.srt"
    downloaded.write_text("Synthetic movie subtitle\n", encoding="utf-8")
    provider = OpenSubtitlesProvider.__new__(OpenSubtitlesProvider)
    provider.config = SimpleNamespace(cache_dir=tmp_path)
    provider.client = object()
    provider._credential_error = None
    provider._auth_error = None
    calls = []
    provider._search_with_retry = lambda **kwargs: (
        calls.append(kwargs) or SimpleNamespace(data=[object()])
    )
    provider._download_with_retry = lambda _subtitle: downloaded

    first = provider.get_movie_subtitle(
        "The Man Called Flintstone", tmdb_id=123, year=1966
    )

    assert first is not None
    assert first.path.is_file()
    assert calls == [{"languages": "en", "tmdb_id": 123, "type": "movie"}]

    provider._search_with_retry = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("Cached movie subtitle should avoid another search")
    )
    second = provider.get_movie_subtitle(
        "The Man Called Flintstone", tmdb_id=123, year=1966
    )

    assert second is not None
    assert second.path == first.path
