import hashlib
import json
import shutil
import string
from pathlib import Path

from fastapi import APIRouter, HTTPException
from loguru import logger

from mkv_episode_matcher import __version__

router = APIRouter(prefix="/system", tags=["System"])


def _shutdown_activity() -> dict[str, object]:
    """Return path-free activity that would be interrupted by shutdown."""

    from mkv_episode_matcher.backend.dependencies import (
        get_pipeline_queue_store,
        get_rip_execution_registry,
    )

    items = get_pipeline_queue_store().list_items()
    running_items = [item for item in items if item.state == "running"]
    running_analyses = [
        item
        for item in items
        if item.state == "review_required"
        and (item.review_code or "").endswith("_running")
    ]
    physical_rip_active = get_rip_execution_registry().has_active_executor()
    active_count = len(running_items) + len(running_analyses) + int(physical_rip_active)
    return {
        "safe_to_shutdown": active_count == 0,
        "active_count": active_count,
        "physical_rip_active": physical_rip_active,
        "downstream_items_active": len(running_items),
        "evidence_analyses_active": len(running_analyses),
        "restart_recovery": (
            "Interrupted downstream work is requeued at startup; interrupted physical "
            "rips return paused for review."
        ),
    }


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


def _cleanup_context(*, require_library: bool = True):
    from mkv_episode_matcher.backend.dependencies import get_pipeline_contract_root
    from mkv_episode_matcher.backend.jellyfin_cleanup import CleanupError, plan_cleanup
    from mkv_episode_matcher.core.config_manager import get_config_manager

    config = get_config_manager().load()
    if (
        config.rip_output_root is None
        or config.transcode_output_root is None
        or (require_library and not (config.jellyfin_tv_root or config.jellyfin_movie_root))
    ):
        raise CleanupError(
            "Configure rip, encoded, and at least one Jellyfin library root first"
        )
    return config, get_pipeline_contract_root(), plan_cleanup


def _cleanup_plans(payload: dict):  # noqa: C901
    from mkv_episode_matcher.backend.jellyfin_cleanup import CleanupError, filter_plan

    mode = payload.get("mode", "older_than")
    days = payload.get("days")
    if mode not in {"older_than", "all", "all_staging"}:
        raise CleanupError("Cleanup mode is invalid")
    if mode == "older_than" and days not in {7, 14}:
        raise CleanupError("Cleanup age must be 7 or 14 days")
    config, contract_root, planner = _cleanup_context(require_library=mode != "all_staging")
    plans = []
    library_roots = []
    if mode == "all_staging":
        protected_roots = [
            root.resolve()
            for root in (config.jellyfin_tv_root, config.jellyfin_movie_root)
            if root is not None
        ]
        plans.append(
            planner(
                rip_root=config.rip_output_root,
                encoded_root=config.transcode_output_root,
                contract_root=contract_root,
                library_root=None,
                cleanup_root=config.deletion_staging_root,
                protected_roots=tuple(protected_roots),
                mode=mode,
            )
        )
        library_roots.append(None)
    else:
        for library_root in (config.jellyfin_tv_root, config.jellyfin_movie_root):
            if library_root is None:
                continue
            plans.append(
                planner(
                    rip_root=config.rip_output_root,
                    encoded_root=config.transcode_output_root,
                    contract_root=contract_root,
                    library_root=library_root,
                    mode=mode,
                    days=days,
                )
            )
            library_roots.append(library_root)
    seen: set[tuple[str, str]] = set()
    unique_plans = []
    unique_roots = []
    for plan, library_root in zip(plans, library_roots, strict=True):
        unique_candidates_list = []
        for candidate in plan.candidates:
            identity = (candidate.category, candidate.relative_path)
            if identity in seen:
                continue
            seen.add(identity)
            unique_candidates_list.append(candidate)
        unique_candidates = tuple(unique_candidates_list)
        unique_plans.append(filter_plan(plan, unique_candidates))
        unique_roots.append(library_root)
    selected_keys = payload.get("candidate_keys")
    if selected_keys is not None:
        if not isinstance(selected_keys, list) or not all(
            isinstance(key, str) for key in selected_keys
        ):
            raise CleanupError("Cleanup candidate selection is invalid")
        selected = set(selected_keys)
        unique_plans = [
            filter_plan(
                plan,
                tuple(
                    candidate
                    for candidate in plan.candidates
                    if f"{candidate.category}:{candidate.relative_path}" in selected
                ),
            )
            for plan in unique_plans
        ]
    combined = hashlib.sha256(
        json.dumps([plan.plan_sha256 for plan in unique_plans], separators=(",", ":")).encode()
    ).hexdigest()
    return config, list(zip(unique_plans, unique_roots, strict=True)), combined


@router.post("/cleanup/preview")
def preview_jellyfin_cleanup(payload: dict):
    """Scan staging roots and return a digest-bound, non-destructive cleanup plan."""

    from mkv_episode_matcher.backend.jellyfin_cleanup import CleanupError

    try:
        _config, planned, combined = _cleanup_plans(payload)
    except CleanupError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    candidates = [candidate for plan, _library in planned for candidate in plan.candidates]
    logger.info(
        "Cleanup preview completed: mode={}, candidate_count={}, group_selection={}",
        payload.get("mode", "older_than"),
        len(candidates),
        payload.get("candidate_keys") is not None,
    )
    return {
        "plan_sha256": combined,
        "mode": payload.get("mode", "older_than"),
        "days": payload.get("days"),
        "file_count": len(candidates),
        "total_size_bytes": sum(item.size_bytes for item in candidates),
        "candidates": [
            {
                "category": item.category,
                "relative_path": item.relative_path,
                "library_relative": item.library_relative,
                "size_bytes": item.size_bytes,
                "modified_at": item.modified_at,
                "backed_up": item.backed_up,
                "disk_key": item.disk_key,
                "disk_label": item.disk_label,
                "candidate_key": f"{item.category}:{item.relative_path}",
            }
            for item in candidates
        ],
    }


@router.post("/cleanup/delete")
def delete_jellyfin_cleanup(payload: dict):
    """Apply only the exact cleanup plan explicitly confirmed by the caller."""

    from mkv_episode_matcher.backend.jellyfin_cleanup import CleanupError, apply_cleanup

    if payload.get("confirm_delete") is not True:
        raise HTTPException(status_code=400, detail="Explicit cleanup confirmation is required")
    expected = payload.get("expected_plan_sha256")
    authorized_count = payload.get("authorized_file_count")
    if not isinstance(expected, str) or not isinstance(authorized_count, int):
        raise HTTPException(status_code=400, detail="Cleanup authorization is incomplete")
    try:
        config, planned, combined = _cleanup_plans(payload)
        if combined != expected or sum(len(plan.candidates) for plan, _library in planned) != authorized_count:
            raise CleanupError("Cleanup plan changed; review a fresh scan")
        contract_root = _cleanup_context(require_library=payload.get("mode") != "all_staging")[1]
        deleted = 0
        for plan, library_root in planned:
            deleted += apply_cleanup(
                plan=plan,
                rip_root=config.rip_output_root,
                encoded_root=config.transcode_output_root,
                contract_root=contract_root,
                library_root=library_root,
                cleanup_root=config.deletion_staging_root,
                protected_roots=tuple(
                    root.resolve()
                    for root in (config.jellyfin_tv_root, config.jellyfin_movie_root)
                    if root is not None
                ),
                candidate_keys=(
                    tuple(payload["candidate_keys"])
                    if isinstance(payload.get("candidate_keys"), list)
                    else None
                ),
                expected_plan_sha256=plan.plan_sha256,
                authorized_file_count=len(plan.candidates),
            )
    except CleanupError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    logger.info(
        "Cleanup deletion completed: mode={}, deleted_file_count={}, group_selection={}",
        payload.get("mode", "older_than"),
        deleted,
        payload.get("candidate_keys") is not None,
    )
    return {"deleted_file_count": deleted, "jellyfin_files_affected": 0}


@router.get("/shutdown/status")
def shutdown_status():
    """Report whether shutdown would interrupt active work."""

    return _shutdown_activity()


@router.post("/shutdown")
def shutdown_server(payload: dict | None = None):
    """Shutdown the application server."""
    import os
    import signal
    import threading
    import time

    activity = _shutdown_activity()
    if not activity["safe_to_shutdown"] and not (payload or {}).get(
        "confirm_interrupt"
    ):
        raise HTTPException(
            status_code=409,
            detail={
                **activity,
                "message": "Active work would be interrupted; explicit confirmation is required",
            },
        )

    def kill_server():
        time.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)

    # Schedule shutdown in a separate thread to allow response to return
    threading.Thread(target=kill_server).start()
    return {"status": "shutting_down", "interrupted_work": activity["active_count"]}
