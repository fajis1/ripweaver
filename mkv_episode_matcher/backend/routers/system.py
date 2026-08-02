import shutil
import string
from pathlib import Path

from fastapi import APIRouter, HTTPException

from mkv_episode_matcher import __version__

router = APIRouter(prefix="/system", tags=["System"])


def _find_executable(
    names: tuple[str, ...], candidates: tuple[Path, ...] = ()
) -> str | None:
    """Find an installed executable without launching it."""

    for name in names:
        discovered = shutil.which(name)
        if discovered:
            return str(Path(discovered).resolve())
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _find_portable_executable(
    filename: str, roots: tuple[Path, ...], *, max_depth: int = 3, max_dirs: int = 2000
) -> str | None:
    """Find one exact portable executable in bounded download-folder roots."""

    queue = [(root, 0) for root in roots if root.is_dir()]
    visited: set[Path] = set()
    inspected = 0
    while queue and inspected < max_dirs:
        directory, depth = queue.pop(0)
        try:
            resolved = directory.resolve()
            if resolved in visited:
                continue
            visited.add(resolved)
            inspected += 1
            entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.name.casefold() == filename.casefold():
                return str(entry.resolve())
        if depth < max_depth:
            queue.extend(
                (entry, depth + 1)
                for entry in entries
                if entry.is_dir() and not entry.is_symlink()
            )
    return None


def _portable_download_roots() -> tuple[Path, ...]:
    roots = [Path.home() / "Downloads"]
    for letter in string.ascii_uppercase:
        roots.extend((Path(f"{letter}:/downloads"), Path(f"{letter}:/Downloads")))
    unique = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return tuple(unique)


@router.get("/tools/discover")
def discover_tools():
    """Locate supported executables without invoking external programs."""

    program_files = Path("C:/Program Files")
    program_files_x86 = Path("C:/Program Files (x86)")
    return {
        "tools": {
            "makemkv_path": _find_executable(
                ("makemkvcon64.exe", "makemkvcon.exe"),
                (
                    program_files_x86 / "MakeMKV" / "makemkvcon64.exe",
                    program_files / "MakeMKV" / "makemkvcon64.exe",
                ),
            ),
            "handbrake_path": _find_executable(
                ("HandBrakeCLI.exe", "HandBrakeCLI"),
                (program_files / "HandBrake" / "HandBrakeCLI.exe",),
            )
            or _find_portable_executable(
                "HandBrakeCLI.exe", _portable_download_roots()
            ),
            "ffmpeg_path": _find_executable(("ffmpeg.exe", "ffmpeg")),
            "ffprobe_path": _find_executable(("ffprobe.exe", "ffprobe")),
        }
    }


@router.get("/folders")
def browse_folders(path: str | None = None):
    """List local directories for the settings folder chooser, read-only."""

    if path is None or not path.strip():
        if Path("C:/").exists():
            roots = [Path(f"{letter}:/") for letter in string.ascii_uppercase]
            return {
                "current": None,
                "entries": [
                    {"name": root.drive or str(root), "path": str(root)}
                    for root in roots
                    if root.exists()
                ],
            }
        path_obj = Path("/")
    else:
        path_obj = Path(path).expanduser()

    if not path_obj.exists() or not path_obj.is_dir():
        raise HTTPException(status_code=400, detail="Folder does not exist")
    try:
        entries = sorted(
            (
                {"name": item.name or str(item), "path": str(item.resolve())}
                for item in path_obj.iterdir()
                if item.is_dir()
            ),
            key=lambda item: item["name"].casefold(),
        )
    except OSError:
        raise HTTPException(status_code=403, detail="Folder cannot be read") from None
    parent = path_obj.parent if path_obj.parent != path_obj else None
    return {
        "current": str(path_obj.resolve()),
        "parent": str(parent.resolve()) if parent else None,
        "entries": entries,
    }


def _public_config(config):
    """Return configuration and credential status without secret values."""

    from mkv_episode_matcher.core.config_manager import SECRET_CONFIG_FIELDS
    from mkv_episode_matcher.core.credentials import (
        CREDENTIAL_SPECS,
        credential_is_configured,
        credential_last4,
    )

    result = config.model_dump(exclude=SECRET_CONFIG_FIELDS)
    result.update(dict.fromkeys(SECRET_CONFIG_FIELDS, ""))
    result["credential_status"] = {
        name: {
            "configured": credential_is_configured(spec.name),
            "last4": credential_last4(spec.name),
            "management_url": spec.management_url,
        }
        for name, spec in CREDENTIAL_SPECS.items()
        if name in CREDENTIAL_SPECS
    }
    return result


def _validate_executable_paths(config) -> dict[str, str]:
    """Validate configured tool files without launching them or exposing paths."""

    expected_names = {
        "makemkv_path": {"makemkvcon64.exe", "makemkvcon.exe", "makemkvcon"},
        "handbrake_path": {"handbrakecli.exe", "handbrakecli"},
        "ffmpeg_path": {"ffmpeg.exe", "ffmpeg"},
        "ffprobe_path": {"ffprobe.exe", "ffprobe"},
    }
    errors = {}
    for field, names in expected_names.items():
        path = getattr(config, field)
        if path is None:
            continue
        if not path.is_file():
            errors[field] = "The selected executable file does not exist."
        elif path.name.casefold() not in names:
            errors[field] = "The selected file has the wrong executable name."
    return errors


@router.get("/status")
def get_system_status():
    """
    Get current system status.
    Checks the singleton engine status without blocking.
    """
    from mkv_episode_matcher.backend.dependencies import get_engine_status

    status = get_engine_status()

    return {
        "status": status["status"],
        "model_loaded": status["loaded"],
        "version": __version__,
    }


@router.get("/config")
def get_config():
    """Get current configuration."""
    from mkv_episode_matcher.core.config_manager import get_config_manager

    manager = get_config_manager()
    return _public_config(manager.load())


@router.post("/config")
def update_config(config_data: dict):
    """Update non-secret configuration and locally store submitted credentials."""
    from mkv_episode_matcher.core.config_manager import (
        SECRET_CONFIG_FIELDS,
        get_config_manager,
    )
    from mkv_episode_matcher.core.credentials import store_credential
    from mkv_episode_matcher.core.models import Config

    credential_fields = {
        "tmdb_api_key": "tmdb",
        "open_subtitles_api_key": "opensubtitles-api",
        "open_subtitles_username": "opensubtitles-username",
        "open_subtitles_password": "opensubtitles-password",
        "gemini_primary_api_key": "gemini-primary",
        "gemini_paid_api_key": "gemini-paid",
    }
    manager = get_config_manager()
    try:
        submitted = dict(config_data)
        submitted.pop("credential_status", None)
        credential_updates = {
            credential_fields[field]: submitted.pop(field)
            for field in SECRET_CONFIG_FIELDS
            if isinstance(submitted.get(field), str) and submitted[field]
        }
        for field in SECRET_CONFIG_FIELDS:
            submitted.pop(field, None)

        current = manager.load().model_dump(exclude=SECRET_CONFIG_FIELDS)
        current.update(submitted)
        new_config = Config(**current)
        field_errors = _validate_executable_paths(new_config)
        if field_errors:
            return {
                "status": "error",
                "message": "Configuration was not saved. Fix the highlighted executable path.",
                "field_errors": field_errors,
            }

        for credential, value in credential_updates.items():
            store_credential(credential, value)
        manager.save(new_config)
        return {
            "status": "success",
            "config": _public_config(manager.load()),
        }
    except Exception as error:
        return {
            "status": "error",
            "message": (f"Configuration was not saved ({type(error).__name__})."),
        }


@router.get("/config/validate")
def validate_config():
    """Check if required credentials are configured."""
    from mkv_episode_matcher.core.config_manager import get_config_manager

    manager = get_config_manager()
    config = manager.load()

    missing = []

    # Check OpenSubtitles credentials (required unless using local provider)
    if config.sub_provider == "opensubtitles":
        if not config.open_subtitles_api_key:
            missing.append("open_subtitles_api_key")

    return {
        "valid": len(missing) == 0,
        "missing": missing,
        "needs_onboarding": len(missing) > 0,
    }


@router.post("/shutdown")
def shutdown_server():
    """Shutdown the application server."""
    import os
    import signal
    import threading
    import time

    def kill_server():
        time.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)

    # Schedule shutdown in a separate thread to allow response to return
    threading.Thread(target=kill_server).start()
    return {"status": "shutting_down"}
