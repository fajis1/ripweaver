"""Environment-backed configuration for secrets and external tools."""

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvironmentSettings(BaseSettings):
    """Values that should not be embedded in source code or committed JSON."""

    tmdb_api_key: str | None = None
    opensubtitles_api_key: str | None = None
    opensubtitles_username: str | None = None
    opensubtitles_password: str | None = None

    makemkv_path: Path | None = None
    makemkv_key: str | None = None
    handbrake_path: Path | None = None
    ffprobe_path: Path | None = None
    ffmpeg_path: Path | None = None
    tesseract_path: Path | None = None

    gemini_primary_api_key: str | None = None
    gemini_paid_api_key: str | None = None
    gemini_model: str = "gemini-3.6-flash"
    ripweaver_catalogue_token: str | None = None
    media_triage_folder: Path | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


def load_environment_settings() -> EnvironmentSettings:
    """Load process environment and the local, Git-ignored ``.env`` file."""

    configured_path = os.environ.get("MKV_MATCH_ENV_FILE")
    env_file = None if configured_path == "" else configured_path or ".env"
    return EnvironmentSettings(_env_file=env_file)
