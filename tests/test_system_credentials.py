import json
from unittest.mock import Mock, call, patch

from mkv_episode_matcher.backend.routers.system import (
    _public_config,
    update_config,
)
from mkv_episode_matcher.core.models import Config


@patch(
    "mkv_episode_matcher.core.credentials.credential_is_configured",
    return_value=True,
)
def test_public_config_never_returns_credential_values(_mock_status):
    config = Config(
        tmdb_api_key="fake-tmdb-secret",
        open_subtitles_api_key="fake-os-secret",
        open_subtitles_username="fake-personal-user",
        open_subtitles_password="fake-password",
    )

    result = _public_config(config)
    serialized = json.dumps(result, default=str)

    assert "fake-tmdb-secret" not in serialized
    assert "fake-os-secret" not in serialized
    assert "fake-personal-user" not in serialized
    assert "fake-password" not in serialized
    assert result["tmdb_api_key"] == ""
    assert result["credential_status"]["tmdb"]["configured"] is True


@patch(
    "mkv_episode_matcher.core.credentials.credential_is_configured",
    return_value=True,
)
@patch("mkv_episode_matcher.core.credentials.store_credential")
@patch("mkv_episode_matcher.core.config_manager.get_config_manager")
def test_web_config_stores_submitted_secrets_without_returning_them(
    mock_get_manager, mock_store, _mock_status
):
    manager = Mock()
    manager.load.return_value = Config(min_confidence=0.7)
    mock_get_manager.return_value = manager

    result = update_config({
        "min_confidence": 0.8,
        "tmdb_api_key": "replacement-fake-tmdb",
        "open_subtitles_api_key": "replacement-fake-os",
        "gemini_primary_api_key": "replacement-fake-gemini",
        "rip_output_root": "D:/staging/rips",
        "transcode_output_root": "D:/staging/encoded",
        "jellyfin_tv_root": "D:/library/tv",
        "jellyfin_movie_root": "D:/library/movies",
        "automatic_processing_enabled": True,
    })
    serialized = json.dumps(result, default=str)

    assert result["status"] == "success"
    assert "replacement-fake-tmdb" not in serialized
    assert "replacement-fake-os" not in serialized
    assert "replacement-fake-gemini" not in serialized
    mock_store.assert_has_calls(
        [
            call("tmdb", "replacement-fake-tmdb"),
            call("opensubtitles-api", "replacement-fake-os"),
            call("gemini-primary", "replacement-fake-gemini"),
        ],
        any_order=True,
    )
    assert mock_store.call_count == 3
    manager.save.assert_called_once()
    saved = manager.save.call_args.args[0]
    assert saved.automatic_processing_enabled is True
    assert saved.rip_output_root.as_posix() == "D:/staging/rips"


@patch(
    "mkv_episode_matcher.core.credentials.credential_is_configured",
    return_value=True,
)
def test_public_config_includes_gemini_status_without_value(_mock_status):
    result = _public_config(Config())

    assert result["gemini_primary_api_key"] == ""
    assert result["gemini_paid_api_key"] == ""
    assert result["credential_status"]["gemini-primary"]["configured"] is True
