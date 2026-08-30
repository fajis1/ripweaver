import json
from unittest.mock import Mock, call, patch

from mkv_episode_matcher.backend.routers.system import (
    _public_config,
    _validate_executable_paths,
    update_config,
)
from mkv_episode_matcher.core.config_manager import ConfigManager
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
    assert result["retained_source_ttl_days"] == 30
    assert result["short_title_review_seconds"] == 150
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
        "gemini_model": "gemini-custom-preview",
        "gemini_fallback_models": [
            "gemini-backup-one",
            "gemini-backup-two",
        ],
        "retained_source_ttl_days": 45,
        "short_title_review_seconds": 210,
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
    assert saved.gemini_model == "gemini-custom-preview"
    assert saved.gemini_fallback_models == [
        "gemini-backup-one",
        "gemini-backup-two",
    ]
    assert saved.retained_source_ttl_days == 45
    assert saved.short_title_review_seconds == 210


@patch(
    "mkv_episode_matcher.core.credentials.credential_is_configured",
    return_value=True,
)
def test_public_config_includes_gemini_status_without_value(_mock_status):
    result = _public_config(Config())

    assert result["gemini_primary_api_key"] == ""
    assert result["gemini_paid_api_key"] == ""
    assert result["credential_status"]["gemini-primary"]["configured"] is True


def test_public_config_exposes_only_credential_suffix(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "not-for-display-9876")

    result = _public_config(Config())
    serialized = json.dumps(result, default=str)

    assert result["credential_status"]["tmdb"]["configured"] is True
    assert result["credential_status"]["tmdb"]["last4"] == "9876"
    assert "not-for-display-9876" not in serialized


def test_saved_tool_path_overrides_environment_default_but_secret_does_not(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "handbrake_path": "G:/portable/HandBrakeCLI.exe",
            "tmdb_api_key": "must-not-persist",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ConfigManager,
        "_environment_overrides",
        staticmethod(
            lambda: {
                "handbrake_path": "C:/stale/HandBrakeCLI.exe",
                "tmdb_api_key": "environment-secret",
            }
        ),
    )

    loaded = ConfigManager(config_path).load()

    assert loaded.handbrake_path.as_posix() == "G:/portable/HandBrakeCLI.exe"
    assert loaded.tmdb_api_key == "environment-secret"


def test_executable_validation_requires_existing_correctly_named_files(tmp_path):
    handbrake = tmp_path / "HandBrakeCLI.exe"
    handbrake.write_bytes(b"synthetic")
    wrong_name = tmp_path / "not-ffprobe.exe"
    wrong_name.write_bytes(b"synthetic")

    assert _validate_executable_paths(Config(handbrake_path=handbrake)) == {}
    assert _validate_executable_paths(Config(ffprobe_path=wrong_name)) == {
        "ffprobe_path": "The selected file has the wrong executable name."
    }
    assert _validate_executable_paths(
        Config(ffmpeg_path=tmp_path / "missing" / "ffmpeg.exe")
    ) == {"ffmpeg_path": "The selected executable file does not exist."}


@patch("mkv_episode_matcher.core.config_manager.get_config_manager")
def test_invalid_executable_path_prevents_web_config_save(mock_get_manager, tmp_path):
    manager = Mock()
    manager.load.return_value = Config()
    mock_get_manager.return_value = manager

    result = update_config({
        "handbrake_path": str(tmp_path / "missing" / "HandBrakeCLI.exe")
    })

    assert result["status"] == "error"
    assert result["field_errors"] == {
        "handbrake_path": "The selected executable file does not exist."
    }
    manager.save.assert_not_called()
