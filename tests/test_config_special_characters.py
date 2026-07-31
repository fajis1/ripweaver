"""Test cases for environment-based handling of credential characters."""

import tempfile
from pathlib import Path

import pytest

from mkv_episode_matcher.core.config_manager import ConfigManager
from mkv_episode_matcher.core.models import Config


class TestConfigSpecialCharacters:
    """Test that config handling works correctly with special characters in passwords."""

    @pytest.fixture
    def temp_config_file(self):
        """Create a temporary config file for testing."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            yield Path(f.name)
        Path(f.name).unlink(missing_ok=True)

    @pytest.fixture
    def mock_config_data(self, tmp_path):
        """Mock configuration data for testing."""
        show_dir = tmp_path / "test_path"
        show_dir.mkdir()
        return {
            "tmdb_api_key": "test_tmdb_api_key",
            "open_subtitles_api_key": "test_os_api_key",
            "open_subtitles_user_agent": "test_user_agent",
            "open_subtitles_username": "test_username",
            "show_dir": show_dir,
        }

    def _test_password_loading(
        self, password, temp_config_file, mock_config_data, monkeypatch
    ):
        """Verify environment secrets load without being written to JSON."""
        cm = ConfigManager(config_path=temp_config_file)
        monkeypatch.setenv("OPENSUBTITLES_PASSWORD", password)

        config_data = mock_config_data.copy()
        config_data["open_subtitles_password"] = "must-not-be-persisted"

        config = Config(**config_data)
        cm.save(config)

        loaded = cm.load()
        expected = password or None
        assert loaded.open_subtitles_password == expected
        assert "must-not-be-persisted" not in temp_config_file.read_text(
            encoding="utf-8"
        )

    def test_password_with_percent_symbol(
        self, temp_config_file, mock_config_data, monkeypatch
    ):
        """Test that passwords containing % symbol don't cause interpolation errors."""
        password = "H7z*X$X29JdJ^#%Q"  # gitguardian:ignore
        self._test_password_loading(
            password, temp_config_file, mock_config_data, monkeypatch
        )

    def test_password_with_multiple_percent_symbols(
        self, temp_config_file, mock_config_data, monkeypatch
    ):
        """Test that passwords with multiple % symbols work correctly."""
        password = "password%with%multiple%percent%signs"
        self._test_password_loading(
            password, temp_config_file, mock_config_data, monkeypatch
        )

    def test_password_with_interpolation_like_syntax(
        self, temp_config_file, mock_config_data, monkeypatch
    ):
        """Test that passwords resembling interpolation syntax are handled correctly."""
        password = "%(section)s_password_%(option)s"
        self._test_password_loading(
            password, temp_config_file, mock_config_data, monkeypatch
        )

    def test_password_with_various_special_characters(
        self, temp_config_file, mock_config_data, monkeypatch
    ):
        """Test that passwords with various special characters work correctly."""
        special_passwords = [
            "pass@word!123",
            "my$ecret#key*",
            "complex&password^with()brackets[]",
            "unicode_测试_password",
            "spaces in password",
            "tabs\tand\nnewlines",
        ]

        for password in special_passwords:
            self._test_password_loading(
                password, temp_config_file, mock_config_data, monkeypatch
            )

    def test_empty_password(self, temp_config_file, mock_config_data, monkeypatch):
        """Test that empty passwords are handled correctly."""
        self._test_password_loading(
            "", temp_config_file, mock_config_data, monkeypatch
        )

    def test_environment_persistence(
        self, temp_config_file, mock_config_data, monkeypatch
    ):
        """Test that an environment secret remains available across loads."""
        password = "persistent%password#123"
        monkeypatch.setenv("OPENSUBTITLES_PASSWORD", password)

        cm = ConfigManager(config_path=temp_config_file)
        config_data = mock_config_data.copy()
        config_data["open_subtitles_password"] = "must-not-be-persisted"
        config = Config(**config_data)
        cm.save(config)

        for _ in range(3):
            loaded = cm.load()
            assert loaded.open_subtitles_password == password
