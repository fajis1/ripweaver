import json

import pytest

from mkv_episode_matcher.media.gemini_matcher import (
    GeminiResponseError,
    TransportResponse,
)
from mkv_episode_matcher.media.gemini_series_resolver import (
    GeminiSeriesResolver,
    build_series_resolution_request,
)
from mkv_episode_matcher.tmdb_client import TvShowCandidate


class FakeTransport:
    def __init__(self, response):
        self.response = response

    def send(self, **_kwargs):
        return self.response


def _candidate(tmdb_id=33):
    return TvShowCandidate(
        tmdb_id, "The Flintstones", "The Flintstones", 1960, "Family sitcom"
    )


def test_series_request_is_path_free_and_bounds_tmdb_ids():
    request = build_series_resolution_request(
        "gemini-test", "The Flintstones CSR DIM2", (_candidate(),)
    )
    prompt = json.loads(request["input"])
    schema = request["response_format"]["schema"]

    assert prompt["series_hint"] == "The Flintstones CSR DIM2"
    assert "G:\\" not in json.dumps(request)
    assert schema["properties"]["tmdb_id"]["enum"] == [33, None]


def test_series_resolver_accepts_only_supplied_tmdb_id():
    response = TransportResponse(
        200,
        {"output_text": json.dumps({
            "tmdb_id": 44,
            "series_name": "Invented",
            "confidence": 0.9,
            "evidence": ["Candidate selected."],
        })},
    )
    resolver = GeminiSeriesResolver(
        model="gemini-test", transport=FakeTransport(response), max_retries=0
    )

    with pytest.raises(GeminiResponseError, match="unsupplied"):
        resolver.resolve_with_key(
            "The Flintstones", (_candidate(),),
            api_key="fake-key", credential="gemini-primary"
        )
