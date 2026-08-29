"""Path-redacted readiness checks for the local RipWeaver installation."""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

TOOL_DOWNLOAD_URLS = {
    "makemkv_path": "https://www.makemkv.com/download/",
    "handbrake_path": "https://handbrake.fr/downloads2.php",
    "ffmpeg_path": "https://ffmpeg.org/download.html",
    "ffprobe_path": "https://ffmpeg.org/download.html",
    "tesseract_path": "https://tesseract-ocr.github.io/tessdoc/Installation.html",
    "disc_image_creator_path": "https://github.com/saramibreak/DiscImageCreator/releases",
}

_TOOL_SPECS = (
    (
        "makemkv_path",
        "MakeMKV",
        "Disc scanning and ripping",
        False,
    ),
    (
        "handbrake_path",
        "HandBrakeCLI",
        "Video transcoding",
        False,
    ),
    (
        "ffmpeg_path",
        "FFmpeg",
        "Audio evidence and media analysis",
        False,
    ),
    (
        "ffprobe_path",
        "FFprobe",
        "Media inspection and output verification",
        False,
    ),
    (
        "tesseract_path",
        "Tesseract OCR",
        "Optional on-screen text analysis",
        True,
    ),
    (
        "disc_image_creator_path",
        "DiscImageCreator",
        "Optional whole-disc recovery",
        True,
    ),
)


def _tool_item(
    config: Any,
    discovered: Mapping[str, str | None],
    *,
    field: str,
    label: str,
    feature: str,
    optional: bool,
) -> dict[str, Any]:
    configured = getattr(config, field, None)
    if configured is not None:
        path = Path(configured)
        if path.is_file():
            status = "ready"
            message = f"{label} is configured and available."
        else:
            status = "invalid"
            message = (
                f"The saved {label} executable cannot be found. Choose it again in "
                "Settings."
            )
    elif discovered.get(field):
        status = "available"
        message = (
            f"{label} was detected. Use Detect installed tools in Settings to save it."
        )
    else:
        status = "optional" if optional else "missing"
        message = (
            f"{label} is optional and is not configured."
            if optional
            else f"Install {label}, then detect or select its executable in Settings."
        )
    return {
        "id": field.removesuffix("_path"),
        "field": field,
        "category": "tool",
        "label": label,
        "feature": feature,
        "status": status,
        "required": not optional,
        "message": message,
        "download_url": TOOL_DOWNLOAD_URLS[field],
    }


def _directory_item(
    config: Any,
    *,
    field: str,
    item_id: str,
    label: str,
    feature: str,
    required: bool,
) -> dict[str, Any]:
    configured = getattr(config, field, None)
    if configured is None:
        status = "optional" if not required else "missing"
        message = (
            f"{label} is optional and is not configured."
            if not required
            else f"Choose a {label.lower()} in Settings."
        )
    elif Path(configured).is_dir():
        status = "ready"
        message = f"{label} is configured and available."
    else:
        status = "invalid"
        message = (
            f"The saved {label.lower()} cannot be found. Choose it again in Settings."
        )
    return {
        "id": item_id,
        "field": field,
        "category": "storage",
        "label": label,
        "feature": feature,
        "status": status,
        "required": required,
        "message": message,
        "download_url": None,
    }


def build_system_health(
    config: Any,
    *,
    discovered: Mapping[str, str | None] | None = None,
    credential_is_configured: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Describe feature readiness without returning paths or credential values."""

    discovered = discovered or {}
    tools = [
        _tool_item(
            config,
            discovered,
            field=field,
            label=label,
            feature=feature,
            optional=optional,
        )
        for field, label, feature, optional in _TOOL_SPECS
    ]
    tool_status = {item["field"]: item["status"] for item in tools}

    storage = [
        _directory_item(
            config,
            field="rip_output_root",
            item_id="rip_staging",
            label="Rip staging folder",
            feature="Disc ripping",
            required=True,
        ),
        _directory_item(
            config,
            field="transcode_output_root",
            item_id="encoded_staging",
            label="Encoded staging folder",
            feature="Video transcoding",
            required=True,
        ),
    ]

    library_paths = [
        getattr(config, "jellyfin_tv_root", None),
        getattr(config, "jellyfin_movie_root", None),
    ]
    configured_libraries = [Path(path) for path in library_paths if path is not None]
    if not configured_libraries:
        library_status = "missing"
        library_message = (
            "Choose at least one TV or movie media library folder in Settings."
        )
    elif any(path.is_dir() for path in configured_libraries):
        library_status = "ready"
        library_message = (
            "At least one media library folder is configured and available."
        )
    else:
        library_status = "invalid"
        library_message = "The saved media library folders cannot be found. Choose them again in Settings."
    storage.append({
        "id": "media_library",
        "field": "jellyfin_tv_root",
        "category": "storage",
        "label": "Media library folder",
        "feature": "Plex, Jellyfin, Emby, or another media server",
        "status": library_status,
        "required": True,
        "message": library_message,
        "download_url": None,
    })
    storage_status = {item["id"]: item["status"] for item in storage}

    provider_items: list[dict[str, Any]] = []
    provider_ready = True
    if getattr(config, "sub_provider", "opensubtitles") == "opensubtitles":
        provider_ready = bool(
            credential_is_configured and credential_is_configured("opensubtitles-api")
        )
        provider_items.append({
            "id": "opensubtitles_api",
            "field": "open_subtitles_api_key",
            "category": "provider",
            "label": "OpenSubtitles API key",
            "feature": "Subtitle-based episode identification",
            "status": "ready" if provider_ready else "missing",
            "required": True,
            "message": (
                "The OpenSubtitles API key is configured."
                if provider_ready
                else "Add an OpenSubtitles API key in Settings, or select the local provider."
            ),
            "download_url": "https://www.opensubtitles.com/en/consumers",
        })

    ready_for = {
        "launch": True,
        "disc_ripping": (
            tool_status["makemkv_path"] == "ready"
            and storage_status["rip_staging"] == "ready"
        ),
        "transcoding": (
            tool_status["handbrake_path"] == "ready"
            and tool_status["ffprobe_path"] == "ready"
            and storage_status["encoded_staging"] == "ready"
        ),
        "media_analysis": (
            tool_status["ffmpeg_path"] == "ready"
            and tool_status["ffprobe_path"] == "ready"
        ),
        "media_organization": storage_status["media_library"] == "ready",
        "episode_identification": provider_ready,
    }
    ready_for["full_pipeline"] = all(
        ready_for[key]
        for key in (
            "disc_ripping",
            "transcoding",
            "media_analysis",
            "media_organization",
            "episode_identification",
        )
    )
    items = [*tools, *storage, *provider_items]
    action_count = sum(
        item["status"] in {"missing", "invalid", "available"} and item["required"]
        for item in items
    )
    return {
        "status": "ready" if ready_for["full_pipeline"] else "needs_setup",
        "summary": (
            "RipWeaver is ready for the complete workflow."
            if ready_for["full_pipeline"]
            else f"RipWeaver can launch, but {action_count} setup item(s) need attention."
        ),
        "ready_for": ready_for,
        "items": items,
    }
