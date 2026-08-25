import abc
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

from loguru import logger
from opensubtitlescom import OpenSubtitles

F = TypeVar("F", bound=Callable[..., Any])

from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.core.credentials import (
    ApiCredentialError,
    looks_like_authentication_error,
    request_credential_recovery,
    status_code_from_exception,
)
from mkv_episode_matcher.core.models import EpisodeInfo, SubtitleFile
from mkv_episode_matcher.core.subtitle_releases import (
    SubtitleReleaseProfile,
    alternate_release_queries,
    infer_subtitle_release_profile,
    release_profile_query,
    select_subtitle_release_options,
    subtitle_candidate_release_name,
    subtitle_release_family,
)
from mkv_episode_matcher.core.utils import safe_cache_component

_CACHE_MANIFEST_SCHEMA = 2
_CACHE_RELEASE_SUFFIX = re.compile(
    r" - (exact|compatible|generic|unresolved)-[0-9a-f]{10}\.srt$",
    re.IGNORECASE,
)


def _season_manifest_path(cache_dir: Path, season: int) -> Path:
    return cache_dir / f".season-{season:02d}.cache.json"


def _complete_cached_season(
    cache_dir: Path,
    season: int,
    cached_subtitles: list[SubtitleFile],
    tmdb_id: int | None,
    release_profile_key: str | None = None,
) -> bool:
    path = _season_manifest_path(cache_dir, season)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = payload.get("episode_numbers")
    if (
        payload.get("schema_version") != _CACHE_MANIFEST_SCHEMA
        or payload.get("season") != season
        or payload.get("tmdb_id") != tmdb_id
        or payload.get("release_profile") != release_profile_key
        or payload.get("complete") is not True
        or not isinstance(expected, list)
        or any(not isinstance(value, int) or value <= 0 for value in expected)
    ):
        return False
    cached = {
        item.episode_info.episode
        for item in cached_subtitles
        if item.episode_info is not None
    }
    return bool(expected) and set(expected) <= cached


def _write_season_manifest(
    cache_dir: Path,
    *,
    season: int,
    tmdb_id: int | None,
    episode_numbers: set[int],
    complete: bool,
    release_profile_key: str | None = None,
    release_variant_counts: dict[str, int] | None = None,
) -> None:
    target = _season_manifest_path(cache_dir, season)
    temporary = cache_dir / f".{target.name}.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(
            {
                "schema_version": _CACHE_MANIFEST_SCHEMA,
                "season": season,
                "tmdb_id": tmdb_id,
                "release_profile": release_profile_key,
                "episode_numbers": sorted(episode_numbers),
                "complete": complete,
                "release_variant_counts": release_variant_counts or {},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _cached_release_match(filename: str) -> str:
    match = _CACHE_RELEASE_SUFFIX.search(filename)
    return match.group(1).casefold() if match else "unresolved"


def _candidate_episode(candidate: object, season: int) -> int | None:
    api_season = getattr(candidate, "season_number", None)
    api_episode = getattr(candidate, "episode_number", None)
    if isinstance(api_season, int) and isinstance(api_episode, int):
        return api_episode if api_season == season and api_episode > 0 else None
    info = parse_season_episode(subtitle_candidate_release_name(candidate))
    if info is None or info.season != season:
        return None
    return info.episode


def _rank_episode_release_options(
    candidates: list[object],
    season: int,
    profile: SubtitleReleaseProfile,
    *,
    maximum_per_episode: int = 2,
) -> tuple[tuple[int, object, str, str], ...]:
    """Select a bounded number of provider releases for each episode."""

    by_episode: dict[int, list[object]] = {}
    for candidate in candidates:
        episode = _candidate_episode(candidate, season)
        if episode is not None:
            by_episode.setdefault(episode, []).append(candidate)
    selected = []
    for episode in sorted(by_episode):
        for option in select_subtitle_release_options(
            by_episode[episode], profile, maximum=maximum_per_episode
        ):
            selected.append((
                episode,
                option.candidate,
                option.release_name,
                option.release_match,
            ))
    return tuple(selected)


def _subtitle_cache_target(
    cache_dir: Path,
    show_name: str,
    season: int,
    episode: int,
    release_name: str,
    release_match: str,
) -> Path:
    digest = hashlib.sha256(release_name.encode("utf-8")).hexdigest()[:10]
    cache_name = safe_cache_component(show_name)
    return cache_dir / (
        f"{cache_name} - S{season:02d}E{episode:02d} - {release_match}-{digest}.srt"
    )


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
) -> Callable[[F], F]:
    """Decorator for retrying operations with exponential backoff."""

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception = None
            delay = base_delay

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except ApiCredentialError:
                    raise
                except Exception as e:
                    if looks_like_authentication_error(e):
                        raise ApiCredentialError(
                            "opensubtitles-api",
                            "rejected by the provider",
                            status_code=status_code_from_exception(e),
                        ) from e
                    last_exception = e
                    if attempt == max_retries:
                        logger.error(
                            f"Max retries ({max_retries}) exceeded for "
                            f"{func.__name__}: {type(e).__name__}"
                        )
                        raise e

                    logger.warning(
                        f"Attempt {attempt + 1}/{max_retries + 1} failed for "
                        f"{func.__name__}: {type(e).__name__}, retrying in "
                        f"{delay:.1f}s..."
                    )
                    time.sleep(delay)
                    delay = min(delay * backoff_factor, max_delay)

            if last_exception is not None:
                raise last_exception
            raise RuntimeError("Retry loop ended without an exception")

        return wrapper  # type: ignore

    return decorator


def parse_season_episode(filename: str) -> EpisodeInfo | None:
    """Parse season and episode from filename using regex."""
    # S01E01
    match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", filename)
    if match:
        return EpisodeInfo(
            series_name="",  # Placeholder
            season=int(match.group(1)),
            episode=int(match.group(2)),
        )
    # 1x01
    match = re.search(r"(\d{1,2})x(\d{1,2})", filename)
    if match:
        return EpisodeInfo(
            series_name="", season=int(match.group(1)), episode=int(match.group(2))
        )
    return None


class SubtitleProvider(abc.ABC):
    @abc.abstractmethod
    def get_subtitles(
        self,
        show_name: str,
        season: int,
        video_files: list[Path] = None,
        tmdb_id: int | None = None,
    ) -> list[SubtitleFile]:
        pass


class LocalSubtitleProvider(SubtitleProvider):
    """Provider that scans a local directory for subtitle files."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir / "data"

    def get_subtitles(
        self,
        show_name: str,
        season: int,
        video_files: list[Path] = None,
        tmdb_id: int | None = None,
    ) -> list[SubtitleFile]:
        """Get all subtitle files for a specific show and season."""
        release_profile = infer_subtitle_release_profile(show_name, video_files)
        show_dir = self.cache_dir / safe_cache_component(show_name)
        if not show_dir.exists():
            # logger.warning(f"No subtitle cache found at {show_dir}")
            return []

        subtitles = []
        # Case insensitive glob
        files = list(show_dir.glob("*.srt")) + list(show_dir.glob("*.SRT"))

        for f in files:
            info = parse_season_episode(f.name)
            if info:
                if info.season == season:
                    info.series_name = show_name
                    release_match = _cached_release_match(f.name)
                    subtitles.append(
                        SubtitleFile(
                            path=f,
                            episode_info=info,
                            release_name=(
                                f"Cached {release_match} subtitle variant"
                                if release_match != "unresolved"
                                else None
                            ),
                            release_match=release_match,
                            release_profile=release_profile.key,
                        )
                    )

        # Deduplicate by path
        seen = set()
        unique_subs = []
        for sub in subtitles:
            if sub.path not in seen:
                seen.add(sub.path)
                unique_subs.append(sub)

        return unique_subs


class OpenSubtitlesProvider(SubtitleProvider):
    """Provider that downloads subtitles using OpenSubtitles.com."""

    def __init__(self):
        cm = get_config_manager()
        self.config = cm.load()
        self.client = None
        self._auth_error: str | None = None
        self._credential_error: ApiCredentialError | None = None
        self._credential_retry_attempted = False
        self.network_timeout = 30  # seconds
        self._authenticate()

    def _authenticate(self):
        for attempt in range(2):
            self.config = get_config_manager().load()
            if not self.config.open_subtitles_api_key:
                error = ApiCredentialError("opensubtitles-api", "not configured")
                if attempt == 0 and request_credential_recovery(error):
                    continue
                self._credential_error = error
                self._auth_error = str(error)
                logger.warning(self._auth_error)
                return

            try:
                self.client = OpenSubtitles(
                    self.config.open_subtitles_user_agent,
                    self.config.open_subtitles_api_key,
                )
                if (
                    self.config.open_subtitles_username
                    and self.config.open_subtitles_password
                ):
                    self.client.login(
                        self.config.open_subtitles_username,
                        self.config.open_subtitles_password,
                    )
                    logger.debug("Logged in to OpenSubtitles")
                else:
                    logger.debug("Initialized OpenSubtitles (no login)")
                self._auth_error = None
                return
            except Exception as e:
                if looks_like_authentication_error(e):
                    message = str(e).lower()
                    credential = (
                        "opensubtitles-api"
                        if "api key" in message
                        else "opensubtitles-password"
                    )
                    error = ApiCredentialError(
                        credential,
                        "rejected by the provider",
                        status_code=status_code_from_exception(e),
                    )
                    if attempt == 0 and request_credential_recovery(error):
                        continue
                    self._credential_error = error
                    self._auth_error = str(error)
                else:
                    self._auth_error = (
                        f"Failed to initialize OpenSubtitles: {type(e).__name__}"
                    )
                logger.error(self._auth_error)
                self.client = None
                return

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def _search_with_retry(
        self,
        query: str | None = None,
        languages: str = "en",
        parent_tmdb_id: int | None = None,
        tmdb_id: int | None = None,
        season_number: int | None = None,
        type: str | None = None,
    ):
        """Search for subtitles with retry logic."""
        if not self.client:
            raise RuntimeError("OpenSubtitles client not initialized")

        import signal

        def timeout_handler(signum, frame):
            raise TimeoutError(
                f"Search operation timed out after {self.network_timeout}s"
            )

        # Set timeout for search operation (Unix-like systems only)
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(self.network_timeout)

        try:
            return self.client.search(
                query=query,
                languages=languages,
                parent_tmdb_id=parent_tmdb_id,
                tmdb_id=tmdb_id,
                season_number=season_number,
                type=type,
            )
        finally:
            if hasattr(signal, "SIGALRM"):
                signal.alarm(0)  # Cancel the alarm

    @retry_with_backoff(max_retries=5, base_delay=3.0)
    def _download_with_retry(self, subtitle):
        """Download subtitle file with retry logic.

        Uses 5 retries with 3s base delay to handle rate limit issues,
        as the OpenSubtitles download quota can be buggy.
        """
        if not self.client:
            raise RuntimeError("OpenSubtitles client not initialized")

        import signal

        def timeout_handler(signum, frame):
            raise TimeoutError(
                f"Download operation timed out after {self.network_timeout}s"
            )

        # Set timeout for download operation (Unix-like systems only)
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(self.network_timeout)

        try:
            return self.client.download_and_save(subtitle)
        finally:
            if hasattr(signal, "SIGALRM"):
                signal.alarm(0)  # Cancel the alarm

    def get_subtitles(
        self,
        show_name: str,
        season: int,
        video_files: list[Path] = None,
        tmdb_id: int | None = None,
    ) -> list[SubtitleFile]:
        """Get bounded, release-aware subtitle variants for a show/season."""

        return self._get_subtitles(
            show_name,
            season,
            video_files,
            tmdb_id,
            alternate_only=False,
        )

    def get_alternate_subtitles(
        self,
        show_name: str,
        season: int,
        video_files: list[Path] = None,
        tmdb_id: int | None = None,
    ) -> list[SubtitleFile]:
        """Fetch only untested release variants after an evidence failure."""

        return self._get_subtitles(
            show_name,
            season,
            video_files,
            tmdb_id,
            alternate_only=True,
        )

    def _get_subtitles(  # noqa: C901 - provider metadata and retry guards
        self,
        show_name: str,
        season: int,
        video_files: list[Path] | None,
        tmdb_id: int | None,
        *,
        alternate_only: bool,
    ) -> list[SubtitleFile]:
        release_profile = infer_subtitle_release_profile(show_name, video_files)
        cache_dir = self.config.cache_dir / "data" / safe_cache_component(show_name)
        cached_subtitles = LocalSubtitleProvider(self.config.cache_dir).get_subtitles(
            show_name, season, video_files, tmdb_id
        )
        if not alternate_only and _complete_cached_season(
            cache_dir,
            season,
            cached_subtitles,
            tmdb_id,
            release_profile.key,
        ):
            logger.info(
                "Using complete cached subtitle season for {} S{:02d} "
                "({} variants; release_profile={})",
                show_name,
                season,
                len(cached_subtitles),
                release_profile.key or "unresolved",
            )
            return cached_subtitles
        if not self.client:
            if alternate_only:
                logger.info(
                    "Alternate subtitle release lookup is unavailable for {} S{:02d}",
                    show_name,
                    season,
                )
                return []
            if cached_subtitles:
                logger.info(
                    "Using {} cached subtitle references for {} S{:02d}",
                    len(cached_subtitles),
                    show_name,
                    season,
                )
                return cached_subtitles
            if self._credential_error is not None:
                raise self._credential_error
            reason = self._auth_error or "unknown error during initialization"
            logger.error(f"OpenSubtitles client not available: {reason}")
            return []

        # Check for manual TMDB ID first and get correct show name
        search_show_name = show_name
        if tmdb_id and not alternate_only:
            logger.info(
                f"Using manual TMDB ID: {tmdb_id} for {show_name} S{season:02d}"
            )
            try:
                from mkv_episode_matcher.tmdb_client import fetch_show_details

                show_data = fetch_show_details(tmdb_id)
                if show_data:
                    search_show_name = show_data.get("name", show_name)
                    logger.info(
                        f"TMDB lookup: Using '{search_show_name}' instead of '{show_name}'"
                    )
                else:
                    logger.warning(f"Failed to lookup TMDB ID {tmdb_id}")
            except ApiCredentialError:
                raise
            except Exception as e:
                logger.error(f"Error looking up TMDB ID {tmdb_id}: {type(e).__name__}")

        logger.info(
            "Searching OpenSubtitles for {} S{:02d} with release profile {} ({})",
            search_show_name,
            season,
            release_profile.key or "unresolved",
            "alternate-release failover" if alternate_only else "initial pass",
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        downloaded_subtitles = [] if alternate_only else list(cached_subtitles)

        try:
            search_specs: list[dict[str, object]] = []
            if alternate_only:
                search_specs.extend(
                    {
                        "query": query,
                        "season_number": season,
                        "type": "episode",
                    }
                    for query in alternate_release_queries(release_profile, season)
                )
            elif release_profile.understood:
                # The edition-bearing query is intentionally first.  A TMDb
                # series ID normally describes the broadcast series and can
                # otherwise hide Superfan/extended releases.
                search_specs.append({
                    "query": f"{show_name} S{season:02d}",
                    "season_number": season,
                    "type": "episode",
                })
                if tmdb_id:
                    search_specs.append({
                        "query": None,
                        "parent_tmdb_id": tmdb_id,
                        "season_number": season,
                        "type": "episode",
                    })
                elif (
                    release_profile.canonical_series_name.casefold()
                    != show_name.casefold()
                ):
                    search_specs.append({
                        "query": release_profile_query(release_profile, season),
                        "season_number": season,
                        "type": "episode",
                    })
            elif tmdb_id:
                search_specs.append({
                    "query": None,
                    "parent_tmdb_id": tmdb_id,
                    "season_number": season,
                    "type": "episode",
                })
            else:
                search_specs.append({
                    "query": f"{search_show_name} S{season:02d}",
                    "season_number": season,
                    "type": "episode",
                })

            provider_candidates: list[object] = []
            seen_provider_options: set[tuple[int | None, str]] = set()
            for search_index, spec in enumerate(search_specs, start=1):
                response = self._search_with_retry(**spec)
                response_data = getattr(response, "data", None)
                if not isinstance(response_data, list | tuple):
                    continue
                logger.info(
                    "OpenSubtitles release search {}/{} returned {} options",
                    search_index,
                    len(search_specs),
                    len(response_data),
                )
                for candidate in response_data:
                    signature = (
                        _candidate_episode(candidate, season),
                        re.sub(
                            r"[^a-z0-9]+",
                            "",
                            subtitle_candidate_release_name(candidate).casefold(),
                        ),
                    )
                    if signature in seen_provider_options:
                        continue
                    seen_provider_options.add(signature)
                    provider_candidates.append(candidate)

            if not provider_candidates:
                logger.warning("No subtitle release options were returned")
                if not alternate_only:
                    _write_season_manifest(
                        cache_dir,
                        season=season,
                        tmdb_id=tmdb_id,
                        episode_numbers=set(),
                        complete=False,
                        release_profile_key=release_profile.key,
                    )
                return downloaded_subtitles

            selected_options = _rank_episode_release_options(
                provider_candidates,
                season,
                release_profile,
                maximum_per_episode=8 if alternate_only else 2,
            )
            if alternate_only:
                untested_options = []
                selected_families: dict[int, set[str]] = {}
                for option in selected_options:
                    ep_num, _subtitle, release_name, release_match = option
                    target_path = _subtitle_cache_target(
                        cache_dir,
                        show_name,
                        season,
                        ep_num,
                        release_name,
                        release_match,
                    )
                    if target_path.is_file():
                        continue
                    family = subtitle_release_family(release_name)
                    episode_families = selected_families.setdefault(ep_num, set())
                    if family in episode_families or len(episode_families) >= 6:
                        continue
                    episode_families.add(family)
                    untested_options.append(option)
                selected_options = tuple(untested_options)
            available_episodes = {
                episode
                for candidate in provider_candidates
                if (episode := _candidate_episode(candidate, season)) is not None
            }
            expected_targets: set[Path] = set()
            for ep_num, subtitle, release_name, release_match in selected_options:
                target_path = _subtitle_cache_target(
                    cache_dir,
                    show_name,
                    season,
                    ep_num,
                    release_name,
                    release_match,
                )
                expected_targets.add(target_path)
                if target_path.is_file():
                    if not any(
                        item.path == target_path for item in downloaded_subtitles
                    ):
                        downloaded_subtitles.append(
                            SubtitleFile(
                                path=target_path,
                                language="en",
                                episode_info=EpisodeInfo(
                                    series_name=show_name,
                                    season=season,
                                    episode=ep_num,
                                ),
                                release_name=release_name,
                                release_match=release_match,
                                release_profile=release_profile.key,
                            )
                        )
                    continue
                try:
                    logger.info(
                        "Downloading subtitle for S{:02d}E{:02d} (release_match={})",
                        season,
                        ep_num,
                        release_match,
                    )
                    srt_file = Path(self._download_with_retry(subtitle))
                    if not srt_file.is_file() or srt_file.stat().st_size <= 0:
                        raise OSError("Downloaded subtitle is unavailable")
                    shutil.move(str(srt_file), str(target_path))
                    downloaded_subtitles.append(
                        SubtitleFile(
                            path=target_path,
                            language="en",
                            episode_info=EpisodeInfo(
                                series_name=show_name, season=season, episode=ep_num
                            ),
                            release_name=release_name,
                            release_match=release_match,
                            release_profile=release_profile.key,
                        )
                    )
                except Exception as e:
                    logger.error(
                        "Failed to download/save one subtitle variant: {}",
                        type(e).__name__,
                    )

            release_variant_counts: dict[str, int] = {}
            for item in downloaded_subtitles:
                release_variant_counts[item.release_match] = (
                    release_variant_counts.get(item.release_match, 0) + 1
                )
            logger.info(
                "{} subtitle selection retained {} variants across {} episodes: {}",
                "Alternate-release" if alternate_only else "Release-aware",
                len(downloaded_subtitles),
                len({
                    item.episode_info.episode
                    for item in downloaded_subtitles
                    if item.episode_info is not None
                }),
                ", ".join(
                    f"{key}={release_variant_counts[key]}"
                    for key in sorted(release_variant_counts)
                ),
            )
            if not alternate_only:
                _write_season_manifest(
                    cache_dir,
                    season=season,
                    tmdb_id=tmdb_id,
                    episode_numbers=available_episodes,
                    complete=bool(available_episodes)
                    and bool(expected_targets)
                    and all(path.is_file() for path in expected_targets),
                    release_profile_key=release_profile.key,
                    release_variant_counts=release_variant_counts,
                )
            return downloaded_subtitles

        except ApiCredentialError as error:
            if not self._credential_retry_attempted and request_credential_recovery(
                error
            ):
                self._credential_retry_attempted = True
                self.client = None
                self._authenticate()
                if self.client:
                    return self._get_subtitles(
                        show_name,
                        season,
                        video_files,
                        tmdb_id,
                        alternate_only=alternate_only,
                    )
            logger.error(str(error))
            return downloaded_subtitles
        except Exception as e:
            logger.error(f"OpenSubtitles search failed: {type(e).__name__}")
            return downloaded_subtitles

    def get_movie_subtitle(
        self,
        movie_title: str,
        *,
        tmdb_id: int,
        year: int | None = None,
    ) -> SubtitleFile | None:
        """Return one cached or downloaded English movie subtitle reference.

        Movie references live in an ID-scoped cache so similarly named films
        cannot contaminate each other.  This method is intentionally bounded to
        one download: callers use it only after TMDb runtime filtering has
        reduced a TV-disc feature to a small related-film candidate set.
        """

        if tmdb_id <= 0:
            raise ValueError("TMDb movie ID is invalid")
        cache_dir = self.config.cache_dir / "data" / "movies" / f"tmdb-{tmdb_id}"
        cached = tuple(sorted(cache_dir.glob("*.srt"))) if cache_dir.is_dir() else ()
        if cached:
            return SubtitleFile(path=cached[0], language="en")
        if not self.client:
            if self._credential_error is not None:
                raise self._credential_error
            return None

        try:
            response = self._search_with_retry(
                languages="en",
                tmdb_id=tmdb_id,
                type="movie",
            )
            candidates = getattr(response, "data", None)
            if not isinstance(candidates, list) or not candidates:
                return None
            downloaded = Path(self._download_with_retry(candidates[0]))
            if not downloaded.is_file() or downloaded.stat().st_size <= 0:
                return None
            cache_dir.mkdir(parents=True, exist_ok=True)
            safe_title = safe_cache_component(movie_title)
            year_suffix = f"-{year}" if year is not None else ""
            target = cache_dir / f"{safe_title}{year_suffix}.srt"
            if target.exists():
                return SubtitleFile(path=target, language="en")
            shutil.move(str(downloaded), str(target))
            return SubtitleFile(path=target, language="en")
        except ApiCredentialError:
            raise
        except Exception as exc:
            logger.error("OpenSubtitles movie reference failed: {}", type(exc).__name__)
            return None


class CompositeSubtitleProvider(SubtitleProvider):
    def __init__(self, providers: list[SubtitleProvider]):
        self.providers = providers

    def get_subtitles(
        self,
        show_name: str,
        season: int,
        video_files: list[Path] = None,
        tmdb_id: int | None = None,
    ) -> list[SubtitleFile]:
        results = []

        # Try each provider in order, but prioritize cached results
        for i, provider in enumerate(self.providers):
            provider_results = provider.get_subtitles(
                show_name, season, video_files, tmdb_id
            )

            # If this is the local provider and we have results, prefer them
            if isinstance(provider, LocalSubtitleProvider) and provider_results:
                logger.info(
                    f"Found {len(provider_results)} cached subtitles for {show_name} S{season:02d}"
                )
                results.extend(provider_results)
                # Return early if we have enough cached subtitles
                if (
                    len(provider_results) >= 3
                ):  # Arbitrary threshold for "enough" episodes
                    logger.info("Using cached subtitles, skipping download")
                    return results
            else:
                # For non-local providers, only use if we don't have cached results
                if not results:
                    logger.info(f"No cached subtitles found, trying provider {i + 1}")
                    results.extend(provider_results)
                else:
                    logger.info(
                        "Skipping additional providers since cached subtitles are available"
                    )
                    break

        return results

    def get_alternate_subtitles(
        self,
        show_name: str,
        season: int,
        video_files: list[Path] = None,
        tmdb_id: int | None = None,
    ) -> list[SubtitleFile]:
        """Collect bounded untested releases from capable providers only."""

        results: list[SubtitleFile] = []
        seen: set[tuple[str, int | None, int | None]] = set()
        for provider in self.providers:
            getter = getattr(provider, "get_alternate_subtitles", None)
            if not callable(getter):
                continue
            for subtitle in getter(show_name, season, video_files, tmdb_id):
                episode = subtitle.episode_info
                signature = (
                    str(subtitle.path),
                    episode.season if episode is not None else None,
                    episode.episode if episode is not None else None,
                )
                if signature in seen:
                    continue
                seen.add(signature)
                results.append(subtitle)
        return results
