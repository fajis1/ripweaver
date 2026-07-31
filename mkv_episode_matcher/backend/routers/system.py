from fastapi import APIRouter

from mkv_episode_matcher import __version__

router = APIRouter(prefix="/system", tags=["System"])


def _public_config(config):
    """Return configuration and credential status without secret values."""

    from mkv_episode_matcher.core.config_manager import SECRET_CONFIG_FIELDS
    from mkv_episode_matcher.core.credentials import (
        CREDENTIAL_SPECS,
        credential_is_configured,
    )

    result = config.model_dump(exclude=SECRET_CONFIG_FIELDS)
    result.update(dict.fromkeys(SECRET_CONFIG_FIELDS, ""))
    result["credential_status"] = {
        name: {
            "configured": credential_is_configured(spec.name),
            "management_url": spec.management_url,
        }
        for name, spec in CREDENTIAL_SPECS.items()
        if name in CREDENTIAL_SPECS
    }
    return result


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
