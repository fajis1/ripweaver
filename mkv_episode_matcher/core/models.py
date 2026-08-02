from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class EpisodeInfo(BaseModel):
    """Data model for episode information."""

    series_name: str
    season: int
    episode: int
    title: str | None = None

    @property
    def s_e_format(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"


class SubtitleFile(BaseModel):
    """Data model for a subtitle file."""

    path: Path
    language: str = "en"
    episode_info: EpisodeInfo | None = None
    content: str | None = None  # Loaded content (optional)


class AudioChunk(BaseModel):
    """Data model for an extracted audio chunk."""

    path: Path
    start_time: float
    duration: float


class MatchResult(BaseModel):
    """Data model for a matching result."""

    episode_info: EpisodeInfo
    confidence: float
    matched_file: Path
    matched_time: float
    chunk_index: int = 0
    model_name: str
    original_file: Path | None = None  # Store original filename for display


class FailedMatch(BaseModel):
    """Data model for a failed match."""

    original_file: Path
    reason: str
    confidence: float = 0.0
    series_name: str | None = None
    season: int | None = None


class MatchCandidate(BaseModel):
    """A candidate match from a single chunk."""

    episode_info: EpisodeInfo
    confidence: float
    reference_file: Path


class Config(BaseModel):
    """Global configuration model."""

    tmdb_api_key: str | None = None
    show_dir: Path | None = None
    cache_dir: Path = Field(
        default_factory=lambda: Path.home() / ".mkv-episode-matcher" / "cache"
    )
    min_confidence: float = 0.7

    # Local pipeline locations and unattended-operation policy. These values
    # are non-secret and may be persisted in the local JSON configuration.
    rip_output_root: Path | None = None
    transcode_output_root: Path | None = None
    deletion_staging_root: Path | None = None
    jellyfin_tv_root: Path | None = None
    jellyfin_movie_root: Path | None = None
    makemkv_path: Path | None = None
    handbrake_path: Path | None = None
    ffmpeg_path: Path | None = None
    ffprobe_path: Path | None = None
    default_handbrake_profile: str = "balanced"
    default_handbrake_profile_480p: str | None = None
    default_handbrake_profile_720p: str | None = None
    default_handbrake_profile_1080p: str | None = None
    default_handbrake_profile_2160p: str | None = None
    remember_last_handbrake_profile: bool = True
    gemini_model: str = "gemini-3.6-flash"
    automatic_processing_enabled: bool = False
    automatic_eject_after_rip: bool = False
    automatic_gemini_ambiguity_fallback: bool = False
    automatic_organization_enabled: bool = False

    # OpenSubtitles settings
    open_subtitles_api_key: str | None = None
    open_subtitles_username: str | None = None
    open_subtitles_password: str | None = None
    open_subtitles_user_agent: str = "Oz 1.0.0"

    # Provider settings
    asr_provider: Literal["whisper", "parakeet"] = (
        "whisper"  # parakeet kept for migration
    )
    asr_model_name: str = "small"
    sub_provider: Literal["opensubtitles", "local"] = "opensubtitles"

    @model_validator(mode="before")
    @classmethod
    def migrate_asr_provider(cls, data: dict) -> dict:
        """Migrate legacy parakeet config to whisper.

        Parakeet model identifiers (e.g. "nvidia/parakeet-tdt-0.6b-v2") are not valid
        whisper model names, so we always reset to the whisper default on provider change.
        """
        if isinstance(data, dict):
            if data.get("asr_provider") == "parakeet":
                data["asr_provider"] = "whisper"
                data["asr_model_name"] = "small"
        return data

    @field_validator("show_dir")
    @classmethod
    def validate_show_dir(cls, v):
        if v and not v.exists():
            raise ValueError(f"Show directory does not exist: {v}")
        return v

    @field_validator(
        "rip_output_root",
        "transcode_output_root",
        "deletion_staging_root",
        "jellyfin_tv_root",
        "jellyfin_movie_root",
        "makemkv_path",
        "handbrake_path",
        "ffmpeg_path",
        "ffprobe_path",
    )
    @classmethod
    def normalize_optional_path(cls, value):
        if value in (None, ""):
            return None
        return Path(value)

    @field_validator(
        "default_handbrake_profile",
        "default_handbrake_profile_480p",
        "default_handbrake_profile_720p",
        "default_handbrake_profile_1080p",
        "default_handbrake_profile_2160p",
    )
    @classmethod
    def validate_profile_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if len(cleaned) > 80:
            raise ValueError("HandBrake profile name is invalid")
        return cleaned

    @field_validator("gemini_model")
    @classmethod
    def validate_gemini_model(cls, value: str) -> str:
        cleaned = value.strip()
        if (
            not cleaned
            or len(cleaned) > 100
            or not all(
                character.isalnum() or character in "._-" for character in cleaned
            )
        ):
            raise ValueError("Gemini model ID is invalid")
        return cleaned
