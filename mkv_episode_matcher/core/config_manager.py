import json
from pathlib import Path

from loguru import logger

from mkv_episode_matcher.core.environment import load_environment_settings
from mkv_episode_matcher.core.models import Config

SECRET_CONFIG_FIELDS = {
    "tmdb_api_key",
    "open_subtitles_api_key",
    "open_subtitles_username",
    "open_subtitles_password",
    "gemini_primary_api_key",
    "gemini_paid_api_key",
}


class ConfigManager:
    """Enhanced configuration manager with JSON-based storage and validation."""

    def __init__(self, config_path: Path | None = None):
        if config_path is None:
            config_path = Path.home() / ".mkv-episode-matcher" / "config.json"
        self.config_path = config_path

    def load(self) -> Config:
        """Load configuration from JSON file with fallback to defaults."""
        if not self.config_path.exists():
            logger.info("No config file found, using defaults")
            return self._create_default_config()

        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))

            # Handle legacy INI config files
            if "Config" in data:
                logger.info("Migrating legacy INI config to JSON")
                data = self._migrate_legacy_config(data)

            persisted_secrets = SECRET_CONFIG_FIELDS.intersection(data)
            if persisted_secrets:
                for field in persisted_secrets:
                    data.pop(field, None)
                logger.warning(
                    "Ignored credential fields in JSON configuration. "
                    "Move them with 'mkv-match credentials'."
                )

            data.update(self._environment_overrides())

            config = Config(**data)
            logger.debug(f"Config loaded from {self.config_path}")
            return config

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config file: {e}")
            return self._create_default_config()
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            return self._create_default_config()

    def _create_default_config(self) -> Config:
        """Create default configuration."""
        config = Config(**self._environment_overrides())
        # Auto-save default config
        self.save(config)
        logger.info(f"Created default configuration at {self.config_path}")
        return config

    @staticmethod
    def _environment_overrides() -> dict[str, str]:
        environment = load_environment_settings()
        values = {
            "tmdb_api_key": environment.tmdb_api_key,
            "open_subtitles_api_key": environment.opensubtitles_api_key,
            "open_subtitles_username": environment.opensubtitles_username,
            "open_subtitles_password": environment.opensubtitles_password,
            "makemkv_path": environment.makemkv_path,
            "handbrake_path": environment.handbrake_path,
            "ffmpeg_path": environment.ffmpeg_path,
            "ffprobe_path": environment.ffprobe_path,
        }
        return {key: value for key, value in values.items() if value not in (None, "")}

    def _migrate_legacy_config(self, legacy_data: dict) -> dict:
        """Migrate legacy INI-style config to new JSON format."""
        legacy_config = legacy_data.get("Config", {})

        # Only carry the model name forward if the legacy provider was whisper.
        # Parakeet model ids are not valid whisper ids, so reset to default.
        legacy_provider = legacy_config.get("asr_provider", "whisper")
        if legacy_provider == "parakeet":
            asr_model_name = "small"
        else:
            asr_model_name = legacy_config.get("asr_model_name", "small")

        migrated = {
            "show_dir": legacy_config.get("show_dir"),
            "cache_dir": str(Path.home() / ".mkv-episode-matcher" / "cache"),
            "min_confidence": 0.7,
            "asr_provider": "whisper",
            "asr_model_name": asr_model_name,
            "sub_provider": "opensubtitles",
        }

        # Clean up None values
        migrated = {k: v for k, v in migrated.items() if v is not None}

        logger.info("Legacy config migrated successfully")
        return migrated

    def save(self, config: Config):
        """Save configuration to JSON file with validation."""
        try:
            # Ensure parent directory exists
            self.config_path.parent.mkdir(parents=True, exist_ok=True)

            # Serialize config to JSON
            config_data = config.model_dump(
                exclude=SECRET_CONFIG_FIELDS,
                exclude_none=True,  # Don't save None values
                by_alias=True,  # Use field aliases if defined
            )

            # Convert Path objects to strings for JSON serialization
            for key, value in config_data.items():
                if isinstance(value, Path):
                    config_data[key] = str(value)

            # Write to file with pretty formatting
            self.config_path.write_text(
                json.dumps(config_data, indent=2, sort_keys=True), encoding="utf-8"
            )

            logger.info(f"Configuration saved to {self.config_path}")

        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            raise


def get_config_manager() -> ConfigManager:
    """Get the default configuration manager instance."""
    return ConfigManager()
