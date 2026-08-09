# tmdb_client.py
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from threading import Lock
from typing import Any, TypeVar

import requests
from loguru import logger

from mkv_episode_matcher.core.credentials import (
    ApiCredentialError,
    ApiServiceError,
    request_credential_recovery,
)

F = TypeVar("F", bound=Callable[..., Any])


def retry_network_operation(
    max_retries: int = 3, base_delay: float = 1.0
) -> Callable[[F], F]:
    """Decorator for retrying network operations."""

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception = None
            delay = base_delay

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (requests.RequestException, ConnectionError, TimeoutError) as e:
                    last_exception = e
                    safe_error = type(e).__name__
                    if attempt == max_retries:
                        logger.error(
                            f"Max retries ({max_retries}) exceeded for "
                            f"{func.__name__}: {safe_error}"
                        )
                        raise e

                    logger.warning(
                        f"Network retry {attempt + 1}/{max_retries + 1} for "
                        f"{func.__name__}: {safe_error}"
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 30)  # Cap at 30 seconds

            if last_exception is not None:
                raise last_exception
            raise RuntimeError("Network retry loop ended without an exception")

        return wrapper  # type: ignore

    return decorator


from mkv_episode_matcher.core.config_manager import get_config_manager

BASE_IMAGE_URL = "https://image.tmdb.org/t/p/original"
BASE_API_URL = "https://api.themoviedb.org/3"


@dataclass(frozen=True)
class TvShowCandidate:
    tmdb_id: int
    name: str
    original_name: str
    first_air_year: int | None
    overview: str


class RateLimitedRequest:
    """
    A class that represents a rate-limited request object.

    Attributes:
        rate_limit (int): Maximum number of requests allowed per period.
        period (int): Period in seconds.
        requests_made (int): Counter for requests made.
        start_time (float): Start time of the current period.
        lock (Lock): Lock for synchronization.
    """

    def __init__(self, rate_limit=30, period=1):
        self.rate_limit = rate_limit
        self.period = period
        self.requests_made = 0
        self.start_time = time.time()
        self.lock = Lock()

    def get(self, url):
        """
        Sends a rate-limited GET request to the specified URL.

        Args:
            url (str): The URL to send the request to.

        Returns:
            Response: The response object returned by the request.
        """
        with self.lock:
            if self.requests_made >= self.rate_limit:
                sleep_time = self.period - (time.time() - self.start_time)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                self.requests_made = 0
                self.start_time = time.time()

            self.requests_made += 1

        response = requests.get(url, timeout=30)
        return response


# Initialize rate-limited request
rate_limited_request = RateLimitedRequest(rate_limit=30, period=1)


def _tmdb_get_json(path: str, **parameters: Any) -> dict:
    """Request TMDb JSON, recovering once from a missing or rejected key."""

    for attempt in range(2):
        config = get_config_manager().load()
        api_key = config.tmdb_api_key
        if not api_key:
            error = ApiCredentialError("tmdb", "not configured")
            if attempt == 0 and request_credential_recovery(error):
                continue
            raise error

        try:
            response = requests.get(
                f"{BASE_API_URL}{path}",
                params={**parameters, "api_key": api_key},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ApiServiceError(
                "TMDb",
                None,
                f"network failure: {type(exc).__name__}",
            ) from exc
        if response.status_code in (401, 403):
            error = ApiCredentialError(
                "tmdb",
                "rejected by the provider",
                status_code=response.status_code,
            )
            if attempt == 0 and request_credential_recovery(error):
                continue
            raise error
        if response.status_code == 429:
            raise ApiServiceError("TMDb", 429, "rate limit reached; retry later")
        if response.status_code >= 500:
            raise ApiServiceError(
                "TMDb", response.status_code, "provider is temporarily unavailable"
            )
        if response.status_code >= 400:
            raise ApiServiceError(
                "TMDb", response.status_code, "request was not accepted"
            )
        return response.json()

    raise ApiCredentialError("tmdb", "replacement key was not accepted")


@retry_network_operation(max_retries=3, base_delay=1.0)
def fetch_show_id(show_name: str) -> str | None:
    """
    Fetch the TMDb ID for a given show name.

    Args:
        show_name (str): The name of the show.

    Returns:
        str: The TMDb ID of the show, or None if not found.
    """
    results = _tmdb_get_json("/search/tv", query=show_name).get("results", [])
    if results:
        return str(results[0]["id"])
    return None


def search_tv_show_candidates(
    show_name: str, *, limit: int = 8
) -> tuple[TvShowCandidate, ...]:
    """Return a bounded, path-free TMDb TV candidate set."""

    if limit < 1 or limit > 20:
        raise ValueError("TMDb TV candidate limit is invalid")
    results = _tmdb_get_json("/search/tv", query=show_name).get("results", [])
    candidates = []
    for item in results[:limit] if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        tmdb_id = item.get("id")
        name = item.get("name")
        if (
            not isinstance(tmdb_id, int)
            or not isinstance(name, str)
            or not name.strip()
        ):
            continue
        first_air_date = item.get("first_air_date")
        year = (
            int(first_air_date[:4])
            if isinstance(first_air_date, str)
            and len(first_air_date) >= 4
            and first_air_date[:4].isdigit()
            else None
        )
        original_name = item.get("original_name")
        overview = item.get("overview")
        candidates.append(
            TvShowCandidate(
                tmdb_id=tmdb_id,
                name=" ".join(name.split())[:160],
                original_name=(
                    " ".join(original_name.split())[:160]
                    if isinstance(original_name, str)
                    else ""
                ),
                first_air_year=year,
                overview=(
                    " ".join(overview.split())[:500]
                    if isinstance(overview, str)
                    else ""
                ),
            )
        )
    return tuple(candidates)


def fetch_aired_episode_catalog(show_id: int):
    """Return the validated aired catalogue for one reviewed TMDb TV ID."""

    from mkv_episode_matcher.media.episode_catalog import build_tmdb_aired_catalog

    return build_tmdb_aired_catalog(show_id, _tmdb_get_json)


def fetch_aired_episode_catalog_for_show(show_name: str):
    """Return one validated, all-season aired catalogue for a reviewed TV name."""

    show_id = fetch_show_id(show_name)
    if show_id is None:
        return None
    return fetch_aired_episode_catalog(int(show_id))


@retry_network_operation(max_retries=3, base_delay=1.0)
def fetch_show_details(show_id: int) -> dict | None:
    """
    Fetch show details from TMDB by ID.

    Args:
        show_id: The TMDB show ID

    Returns:
        dict: Show details including 'name', 'number_of_seasons', etc.
        None: If request fails or API key not configured
    """
    try:
        return _tmdb_get_json(f"/tv/{show_id}")
    except ApiCredentialError:
        raise
    except requests.exceptions.RequestException as e:
        logger.error(
            f"Failed to fetch show details for ID {show_id}: {type(e).__name__}"
        )
        return None
    except ApiServiceError as e:
        logger.error(str(e))
        return None


@retry_network_operation(max_retries=3, base_delay=1.0)
def fetch_season_details(show_id: str, season_number: int) -> int:
    """
    Fetch the total number of episodes for a given show and season from the TMDb API.

    Args:
        show_id (str): The ID of the show on TMDb.
        season_number (int): The season number to fetch details for.

    Returns:
        int: The total number of episodes in the season, or 0 if the API request failed.
    """
    logger.info(f"Fetching season details for Season {season_number}...")
    try:
        season_data = _tmdb_get_json(f"/tv/{show_id}/season/{season_number}")
        total_episodes = len(season_data.get("episodes", []))
        return total_episodes
    except ApiCredentialError:
        raise
    except requests.exceptions.RequestException as e:
        logger.error(
            f"Failed to fetch season details for Season {season_number}: "
            f"{type(e).__name__}"
        )
        return 0
    except ApiServiceError as e:
        logger.error(str(e))
        return 0
    except KeyError:
        logger.error(
            f"Missing 'episodes' key in response JSON data for Season {season_number}"
        )
        return 0


def fetch_episode_details(
    show_id: str, season_number: int, episode_number: int
) -> dict:
    """Fetch one episode without putting the TMDb key in a URL string."""

    return _tmdb_get_json(
        f"/tv/{show_id}/season/{season_number}/episode/{episode_number}"
    )


@retry_network_operation(max_retries=3, base_delay=1.0)
def get_number_of_seasons(show_id: str) -> int:
    """
    Retrieves the number of seasons for a given TV show from the TMDB API.

    Parameters:
    - show_id (int): The ID of the TV show.

    Returns:
    - num_seasons (int): The number of seasons for the TV show.

    Raises:
    - requests.HTTPError: If there is an error while making the API request.
    """
    show_data = _tmdb_get_json(f"/tv/{show_id}")
    num_seasons = show_data.get("number_of_seasons", 0)
    logger.info(f"Found {num_seasons} seasons")
    return num_seasons
