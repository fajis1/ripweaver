import os
from unittest.mock import Mock, patch

import pytest
import requests
from dotenv import dotenv_values
from typer.testing import CliRunner

from mkv_episode_matcher.cli import app
from mkv_episode_matcher.core.credentials import (
    CREDENTIAL_SPECS,
    ApiCredentialError,
    ApiServiceError,
    credential_is_configured,
    looks_like_authentication_error,
    migrate_credentials_from_json,
    request_credential_recovery,
    set_credential_recovery_handler,
    store_credential,
)


@pytest.fixture(autouse=True)
def reset_recovery_handler():
    set_credential_recovery_handler(None)
    yield
    set_credential_recovery_handler(None)


def test_store_credential_writes_dotenv_without_status_exposure(
    tmp_path, monkeypatch
):
    dotenv_path = tmp_path / ".env"
    monkeypatch.delenv("TMDB_API_KEY", raising=False)

    store_credential("tmdb", "fake-test-key", dotenv_path=dotenv_path)

    assert dotenv_values(dotenv_path)["TMDB_API_KEY"] == "fake-test-key"
    assert credential_is_configured("tmdb") is True
    os.environ.pop("TMDB_API_KEY", None)


def test_store_credential_honors_configured_dotenv_file(tmp_path, monkeypatch):
    shared_dotenv = tmp_path / "shared-credentials" / ".env"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MKV_MATCH_ENV_FILE", str(shared_dotenv))
    monkeypatch.delenv("TMDB_API_KEY", raising=False)

    store_credential("tmdb", "fake-shared-key")

    assert dotenv_values(shared_dotenv)["TMDB_API_KEY"] == "fake-shared-key"
    assert not (tmp_path / ".env").exists()
    os.environ.pop("TMDB_API_KEY", None)


def test_store_credential_refuses_explicitly_disabled_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MKV_MATCH_ENV_FILE", "")

    with pytest.raises(RuntimeError, match="storage is disabled"):
        store_credential("tmdb", "fake-disabled-key")

    assert not (tmp_path / ".env").exists()


def test_store_credential_rejects_empty_value(tmp_path):
    with pytest.raises(ValueError, match="cannot be empty"):
        store_credential("tmdb", "   ", dotenv_path=tmp_path / ".env")


def test_migrate_json_credentials_moves_values_and_sanitizes_source(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.json"
    dotenv_path = tmp_path / ".env"
    config_path.write_text(
        '{"Config": {"tmdb_api_key": "fake-legacy-key", '
        '"show_dir": "safe-path"}}',
        encoding="utf-8",
    )
    monkeypatch.delenv("TMDB_API_KEY", raising=False)

    migrated = migrate_credentials_from_json(
        config_path,
        dotenv_path=dotenv_path,
    )

    assert migrated == ["tmdb"]
    assert dotenv_values(dotenv_path)["TMDB_API_KEY"] == "fake-legacy-key"
    sanitized = config_path.read_text(encoding="utf-8")
    assert "fake-legacy-key" not in sanitized
    assert "tmdb_api_key" not in sanitized
    assert "safe-path" in sanitized
    os.environ.pop("TMDB_API_KEY", None)


def test_provider_management_links_are_https():
    assert all(
        spec.management_url.startswith("https://")
        for spec in CREDENTIAL_SPECS.values()
    )


@pytest.mark.parametrize(
    "error",
    [
        requests.HTTPError(response=Mock(status_code=401)),
        requests.HTTPError(response=Mock(status_code=403)),
        RuntimeError("invalid API key"),
        RuntimeError("authentication failed"),
    ],
)
def test_authentication_error_classification(error):
    assert looks_like_authentication_error(error) is True


@pytest.mark.parametrize(
    "error",
    [
        requests.HTTPError(response=Mock(status_code=429)),
        RuntimeError("connection timed out"),
        RuntimeError("provider unavailable"),
    ],
)
def test_transient_errors_are_not_classified_as_bad_keys(error):
    assert looks_like_authentication_error(error) is False


def test_recovery_handler_receives_metadata_only():
    seen = []

    def handler(error):
        seen.append((error.credential, error.status_code, error.management_url))
        return True

    set_credential_recovery_handler(handler)
    error = ApiCredentialError("tmdb", "rejected", status_code=401)

    assert request_credential_recovery(error) is True
    assert seen == [
        (
            "tmdb",
            401,
            "https://www.themoviedb.org/settings/api",
        )
    ]
    set_credential_recovery_handler(None)


@patch("mkv_episode_matcher.tmdb_client.get_config_manager")
@patch("mkv_episode_matcher.tmdb_client.requests.get")
def test_tmdb_rejected_key_recovers_once_without_key_in_url(
    mock_get, mock_config_manager
):
    from mkv_episode_matcher.tmdb_client import _tmdb_get_json

    rejected_config = Mock(tmdb_api_key="old-fake-key")
    replacement_config = Mock(tmdb_api_key="new-fake-key")
    mock_config_manager.return_value.load.side_effect = [
        rejected_config,
        replacement_config,
    ]
    mock_get.side_effect = [
        Mock(status_code=401),
        Mock(status_code=200, json=lambda: {"results": []}),
    ]
    recoveries = []
    set_credential_recovery_handler(
        lambda error: recoveries.append(error.credential) or True
    )

    assert _tmdb_get_json("/search/tv", query="Test") == {"results": []}
    assert recoveries == ["tmdb"]
    first_url = mock_get.call_args_list[0].args[0]
    assert "old-fake-key" not in first_url
    assert mock_get.call_args_list[0].kwargs["params"]["api_key"] == "old-fake-key"
    assert mock_get.call_args_list[1].kwargs["params"]["api_key"] == "new-fake-key"
    set_credential_recovery_handler(None)


@patch("mkv_episode_matcher.tmdb_client.get_config_manager")
@patch("mkv_episode_matcher.tmdb_client.requests.get")
def test_tmdb_rate_limit_does_not_request_new_key(
    mock_get, mock_config_manager
):
    from mkv_episode_matcher.tmdb_client import _tmdb_get_json

    mock_config_manager.return_value.load.return_value = Mock(
        tmdb_api_key="fake-key"
    )
    mock_get.return_value = Mock(status_code=429)
    recoveries = []
    set_credential_recovery_handler(
        lambda error: recoveries.append(error.credential) or True
    )

    with pytest.raises(ApiServiceError, match="rate limit"):
        _tmdb_get_json("/search/tv", query="Test")

    assert recoveries == []


@patch("mkv_episode_matcher.tmdb_client.get_config_manager")
@patch("mkv_episode_matcher.tmdb_client.requests.get")
def test_tmdb_network_error_is_redacted(mock_get, mock_config_manager):
    from mkv_episode_matcher.tmdb_client import _tmdb_get_json

    mock_config_manager.return_value.load.return_value = Mock(
        tmdb_api_key="fake-key"
    )
    mock_get.side_effect = requests.ConnectionError(
        "private request and credential detail"
    )

    with pytest.raises(
        ApiServiceError,
        match="network failure: ConnectionError",
    ) as raised:
        _tmdb_get_json("/tv/42")

    assert "private request" not in str(raised.value)
    assert "fake-key" not in str(raised.value)


@patch("mkv_episode_matcher.core.credentials.store_credential")
def test_credentials_command_hides_value_and_shows_management_link(mock_store):
    result = CliRunner().invoke(
        app,
        ["credentials", "tmdb"],
        input="fake-pasted-key\n",
    )

    assert result.exit_code == 0
    assert "fake-pasted-key" not in result.output
    assert "https://www.themoviedb.org/settings/api" in result.output
    mock_store.assert_called_once_with("tmdb", "fake-pasted-key")


@patch("mkv_episode_matcher.core.providers.subtitles.OpenSubtitles")
@patch("mkv_episode_matcher.core.providers.subtitles.get_config_manager")
def test_opensubtitles_missing_key_recovers_after_replacement(
    mock_config_manager, mock_opensubtitles
):
    from mkv_episode_matcher.core.providers.subtitles import (
        OpenSubtitlesProvider,
    )

    missing = Mock(
        open_subtitles_api_key=None,
        open_subtitles_user_agent="test-agent",
        open_subtitles_username=None,
        open_subtitles_password=None,
    )
    replacement = Mock(
        open_subtitles_api_key="replacement-fake-key",
        open_subtitles_user_agent="test-agent",
        open_subtitles_username=None,
        open_subtitles_password=None,
    )
    mock_config_manager.return_value.load.side_effect = [
        missing,
        missing,
        replacement,
    ]
    recoveries = []
    set_credential_recovery_handler(
        lambda error: recoveries.append(error.credential) or True
    )

    provider = OpenSubtitlesProvider()

    assert provider.client is mock_opensubtitles.return_value
    assert recoveries == ["opensubtitles-api"]
    mock_opensubtitles.assert_called_once_with(
        "test-agent", "replacement-fake-key"
    )


@patch("mkv_episode_matcher.core.providers.subtitles.OpenSubtitles")
@patch("mkv_episode_matcher.core.providers.subtitles.get_config_manager")
def test_opensubtitles_rejected_login_prompts_for_password_once(
    mock_config_manager, mock_opensubtitles
):
    from mkv_episode_matcher.core.providers.subtitles import (
        OpenSubtitlesProvider,
    )

    config = Mock(
        open_subtitles_api_key="fake-api-key",
        open_subtitles_user_agent="test-agent",
        open_subtitles_username="test-user",
        open_subtitles_password="fake-password",
    )
    mock_config_manager.return_value.load.side_effect = [config, config, config]
    rejected_client = Mock()
    rejected_client.login.side_effect = requests.HTTPError(
        "invalid credentials",
        response=Mock(status_code=401),
    )
    replacement_client = Mock()
    mock_opensubtitles.side_effect = [rejected_client, replacement_client]
    recoveries = []
    set_credential_recovery_handler(
        lambda error: recoveries.append(error.credential) or True
    )

    provider = OpenSubtitlesProvider()

    assert provider.client is replacement_client
    assert recoveries == ["opensubtitles-password"]
    assert mock_opensubtitles.call_count == 2
