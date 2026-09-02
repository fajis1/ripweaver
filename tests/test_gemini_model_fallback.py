import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mkv_episode_matcher.core.credentials import ApiServiceError
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
from mkv_episode_matcher.media.gemini_failover import ordered_gemini_models
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiDescriptiveRanker,
    GeminiEpisodeRanker,
    TransportResponse,
    UnmatchedFileEvidence,
)
from mkv_episode_matcher.media.gemini_series_resolver import GeminiSeriesResolver
from mkv_episode_matcher.tmdb_client import TvShowCandidate


class QueueTransport:
    def __init__(self, *responses: TransportResponse):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _episode_inputs():
    files = (UnmatchedFileEvidence("title-001", 1200, ("bounded evidence",)),)
    catalog = (EpisodeCatalogEntry("S01E01", 1, 1, "Pilot", "Opening story", 1200),)
    payload = {
        "output_text": json.dumps({
            "matches": [
                {
                    "file_id": "title-001",
                    "episode_id": "S01E01",
                    "confidence": 0.9,
                    "evidence": ["The supplied evidence agrees."],
                }
            ]
        })
    }
    return files, catalog, payload


def test_model_order_uses_bounded_defaults_or_explicit_backups():
    assert ordered_gemini_models("gemini-3.5-flash-lite") == (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
    )
    assert ordered_gemini_models(
        "primary", [" backup ", "backup", "last", "ignored"]
    ) == ("primary", "backup", "last")


def test_config_validates_at_most_two_safe_fallback_model_ids():
    assert Config(
        gemini_fallback_models=["one", "one", "two"]
    ).gemini_fallback_models == [
        "one",
        "two",
    ]
    with pytest.raises(ValueError, match="At most two"):
        Config(gemini_fallback_models=["one", "two", "three"])
    with pytest.raises(ValueError, match="model ID is invalid"):
        Config(gemini_fallback_models=["unsafe/model"])


@patch("mkv_episode_matcher.media.gemini_matcher.load_environment_settings")
def test_episode_ranker_advances_models_after_all_keys_hit_capacity(mock_settings):
    files, catalog, payload = _episode_inputs()
    mock_settings.return_value = SimpleNamespace(
        gemini_primary_api_key="primary-placeholder",
        gemini_paid_api_key="backup-placeholder",
    )
    transport = QueueTransport(
        TransportResponse(429, {"error": {"message": "quota exhausted"}}),
        TransportResponse(429, {"error": {"message": "quota exhausted"}}),
        TransportResponse(200, payload),
    )
    ranker = GeminiEpisodeRanker(
        model="gemini-3.5-flash-lite",
        fallback_models=["gemini-3.1-flash-lite"],
        transport=transport,
        max_retries=0,
    )

    plan = ranker.rank_with_configured_keys(files, catalog)

    assert plan.model == "gemini-3.1-flash-lite"
    assert [call["api_key"] for call in transport.calls] == [
        "primary-placeholder",
        "backup-placeholder",
        "primary-placeholder",
    ]
    assert [call["body"]["model"] for call in transport.calls] == [
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]


@patch("mkv_episode_matcher.media.gemini_matcher.load_environment_settings")
def test_unavailable_model_switches_before_retrying_same_model_with_backup_key(
    mock_settings,
):
    files, catalog, payload = _episode_inputs()
    mock_settings.return_value = SimpleNamespace(
        gemini_primary_api_key="primary-placeholder",
        gemini_paid_api_key="backup-placeholder",
    )
    transport = QueueTransport(
        TransportResponse(
            404,
            {"error": {"message": "models/gemini-old is not found"}},
            '{"error":{"message":"models/gemini-old is not found"}}',
        ),
        TransportResponse(200, payload),
    )
    ranker = GeminiEpisodeRanker(
        model="gemini-old",
        fallback_models=["gemini-new"],
        transport=transport,
        max_retries=0,
    )

    plan = ranker.rank_with_configured_keys(files, catalog)

    assert plan.model == "gemini-new"
    assert [call["api_key"] for call in transport.calls] == [
        "primary-placeholder",
        "primary-placeholder",
    ]


@patch("mkv_episode_matcher.media.gemini_matcher.load_environment_settings")
def test_generic_bad_request_does_not_switch_models(mock_settings):
    files, catalog, _payload = _episode_inputs()
    mock_settings.return_value = SimpleNamespace(
        gemini_primary_api_key="primary-placeholder",
        gemini_paid_api_key=None,
    )
    transport = QueueTransport(
        TransportResponse(400, {"error": {"message": "invalid request schema"}})
    )
    ranker = GeminiEpisodeRanker(
        model="gemini-primary",
        fallback_models=["gemini-backup"],
        transport=transport,
        max_retries=0,
    )

    with pytest.raises(ApiServiceError, match="request was not accepted"):
        ranker.rank_with_configured_keys(files, catalog)

    assert [call["body"]["model"] for call in transport.calls] == ["gemini-primary"]


@patch("mkv_episode_matcher.media.gemini_matcher.load_environment_settings")
def test_descriptive_ranker_uses_the_configured_model_chain(mock_settings):
    mock_settings.return_value = SimpleNamespace(
        gemini_primary_api_key="primary-placeholder",
        gemini_paid_api_key=None,
    )
    files = (UnmatchedFileEvidence("title-001", 300, ("production interview",)),)
    payload = {
        "output_text": json.dumps({
            "matches": [
                {
                    "file_id": "title-001",
                    "content_kind": "extra",
                    "suggested_title": "Production Interview",
                    "year": None,
                    "confidence": 0.8,
                    "evidence": ["The supplied dialogue is production-focused."],
                    "summary": "Cast members discuss the production.",
                }
            ]
        })
    }
    transport = QueueTransport(
        TransportResponse(503, {"error": {"message": "overloaded"}}),
        TransportResponse(200, payload),
    )
    ranker = GeminiDescriptiveRanker(
        model="gemini-primary",
        fallback_models=["gemini-backup"],
        transport=transport,
        max_retries=0,
    )

    plan = ranker.describe_with_configured_keys(files, release_hint="Example disc")

    assert plan.model == "gemini-backup"
    assert [call["body"]["model"] for call in transport.calls] == [
        "gemini-primary",
        "gemini-backup",
    ]


@patch("mkv_episode_matcher.media.gemini_series_resolver.load_environment_settings")
def test_series_resolver_uses_same_capacity_fallback_chain(mock_settings):
    mock_settings.return_value = SimpleNamespace(
        gemini_primary_api_key="primary-placeholder",
        gemini_paid_api_key=None,
    )
    response = {
        "output_text": json.dumps({
            "tmdb_id": 33,
            "series_name": "The Flintstones",
            "confidence": 0.9,
            "evidence": ["The supplied candidate matches."],
            "alternative_series_names": [],
        })
    }
    transport = QueueTransport(
        TransportResponse(503, {"error": {"message": "overloaded"}}),
        TransportResponse(200, response),
    )
    resolver = GeminiSeriesResolver(
        model="gemini-3.7-flash",
        fallback_models=["gemini-3.6-flash"],
        transport=transport,
        max_retries=0,
    )
    candidates = (
        TvShowCandidate(
            33, "The Flintstones", "The Flintstones", 1960, "Family sitcom"
        ),
    )

    result = resolver.resolve_with_configured_keys("The Flintstones DIM2", candidates)

    assert result.tmdb_id == 33
    assert [call["body"]["model"] for call in transport.calls] == [
        "gemini-3.7-flash",
        "gemini-3.6-flash",
    ]
