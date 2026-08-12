import threading
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mkv_episode_matcher.backend.control_access import require_local_control
from mkv_episode_matcher.backend.dependencies import (
    get_engine,
    get_library_episode_repair_store,
    get_pipeline_contract_root,
    get_pipeline_queue_store,
)
from mkv_episode_matcher.backend.library_episode_repair import (
    LibraryEpisodeRepairError,
    LibraryEpisodeRepairStore,
    apply_generic_repairs,
    discover_episode_claims,
    execute_library_episode_audit,
    sequence_derived_episode_keys,
)
from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore

router = APIRouter(prefix="/scan", tags=["scan"])


class FileEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int | None = None


@router.get("/browse", response_model=list[FileEntry])
async def browse_directory(path: str | None = None):  # noqa: C901
    """
    List contents of a directory.
    If path is not provided, returns root drives (Windows) or logical roots.
    """
    if not path:
        drives = []
        import sys

        if sys.platform == "win32":
            import string
            from ctypes import windll

            bitmask = windll.kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if bitmask & 1:
                    drive_path = f"{letter}:\\"
                    drives.append(
                        FileEntry(name=drive_path, path=drive_path, is_dir=True)
                    )
                bitmask >>= 1
        else:
            common_paths = [
                ("/", "Root (/)"),
                ("/home", "Home"),
                ("/mnt", "Mount points"),
                ("/media", "Removable media"),
                ("/opt", "Optional software"),
                ("/usr", "User programs"),
                ("/var", "Variable data"),
            ]
            for path_str, display_name in common_paths:
                path_obj = Path(path_str)
                if path_obj.exists() and path_obj.is_dir():
                    try:
                        next(path_obj.iterdir(), None)
                        drives.append(
                            FileEntry(name=display_name, path=path_str, is_dir=True)
                        )
                    except (PermissionError, OSError):
                        continue

        return drives

    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    entries = []
    try:
        for item in p.iterdir():
            try:
                # Basic filtering so we don't crash on permission errors for individual files
                is_dir = item.is_dir()
                size = item.stat().st_size if not is_dir else None
                entries.append(
                    FileEntry(name=item.name, path=str(item), is_dir=is_dir, size=size)
                )
            except PermissionError:
                continue
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Permission denied") from exc

    # Sort: directories first, then files
    entries.sort(key=lambda x: (not x.is_dir, x.name.lower()))
    return entries


@router.get("/test-subtitles")
async def test_subtitle_connection():
    """Test OpenSubtitles API connection and credentials."""
    from mkv_episode_matcher.core.config_manager import get_config_manager
    from mkv_episode_matcher.core.credentials import (
        CREDENTIAL_SPECS,
        looks_like_authentication_error,
    )

    cm = get_config_manager()
    config = cm.load()

    result = {
        "api_key_configured": bool(config.open_subtitles_api_key),
        "username_configured": bool(config.open_subtitles_username),
        "password_configured": bool(config.open_subtitles_password),
        "connection_ok": False,
        "login_ok": False,
        "error": None,
        "error_type": None,
        "credential_url": CREDENTIAL_SPECS["opensubtitles-api"].management_url,
    }

    if not config.open_subtitles_api_key:
        result["error"] = "OpenSubtitles API key not configured"
        result["error_type"] = "credential"
        return result

    try:
        from opensubtitlescom import OpenSubtitles

        client = OpenSubtitles(
            config.open_subtitles_user_agent,
            config.open_subtitles_api_key,
        )
        result["connection_ok"] = True

        if config.open_subtitles_username and config.open_subtitles_password:
            client.login(config.open_subtitles_username, config.open_subtitles_password)
            result["login_ok"] = True
    except Exception as e:
        if looks_like_authentication_error(e):
            result["error"] = "OpenSubtitles credentials were rejected"
            result["error_type"] = "credential"
        else:
            result["error"] = f"OpenSubtitles connection failed: {type(e).__name__}"
            result["error_type"] = "service"

    return result


class AnalyzeRequest(BaseModel):
    path: str


@router.post("/analyze")
def analyze_path(req: AnalyzeRequest, engine: Annotated[object, Depends(get_engine)]):
    """
    Scan a directory for MKV files and perform initial context detection (Series/Season)
    without running the full ASR matching process.
    """
    path_obj = Path(req.path)
    if not path_obj.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    # Use engine's scan logic
    files = engine.scan_for_mkv(path_obj)

    results = []
    for f in files:
        # Detect context (Series, Season) using filename heuristics
        series, season = engine._detect_context(f)

        # Check if already processed (Scene Release format)
        is_processed = engine._is_already_processed(f)

        results.append({
            "path": str(f),
            "name": f.name,
            "series": series,
            "season": season,
            "is_processed": is_processed,
        })

    return {"base_path": str(path_obj), "files": results, "count": len(results)}


class EpisodeAuditDiscoverRequest(BaseModel):
    scope: str = "sequence-derived"


class EpisodeAuditStartRequest(BaseModel):
    candidate_digest: str
    confirm_media_read: bool = False
    confirm_provider_lookup: bool = False


class EpisodeAuditApplyRequest(BaseModel):
    result_digest: str
    file_ids: list[str]
    confirm_generic_rename: bool = False


def _jellyfin_audit_root() -> Path:
    configured = get_config_manager().load().jellyfin_tv_root
    if configured is None:
        raise LibraryEpisodeRepairError(
            "Configure the Jellyfin TV library root before using episode repair"
        )
    library_root = configured.expanduser().resolve()
    if not library_root.is_dir():
        raise LibraryEpisodeRepairError(
            "The configured Jellyfin TV root is unavailable"
        )
    return library_root


@router.post(
    "/episode-audit/discover",
    dependencies=[Depends(require_local_control)],
)
def discover_episode_audit(
    request: EpisodeAuditDiscoverRequest,
    store: Annotated[
        LibraryEpisodeRepairStore, Depends(get_library_episode_repair_store)
    ],
    pipeline_store: Annotated[PipelineQueueStore, Depends(get_pipeline_queue_store)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict:
    """Inventory named episodes only; do not open media or contact providers."""

    try:
        if request.scope not in {"sequence-derived", "all-named"}:
            raise LibraryEpisodeRepairError("The episode-repair scope is invalid")
        root = _jellyfin_audit_root()
        episode_keys = (
            sequence_derived_episode_keys(
                pipeline_store.list_items(),
                contract_root.parent / "identification-evidence",
            )
            if request.scope == "sequence-derived"
            else None
        )
        return store.create(
            root,
            discover_episode_claims(root, episode_keys=episode_keys),
            scope=request.scope,
        )
    except LibraryEpisodeRepairError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/episode-audit/{job_id}/start",
    dependencies=[Depends(require_local_control)],
)
def start_episode_audit(
    job_id: str,
    request: EpisodeAuditStartRequest,
    store: Annotated[
        LibraryEpisodeRepairStore, Depends(get_library_episode_repair_store)
    ],
    engine: Annotated[object, Depends(get_engine)],
    contract_root: Annotated[Path, Depends(get_pipeline_contract_root)],
) -> dict:
    """Start exact-file Whisper/subtitle verification in a background worker."""

    if not request.confirm_media_read or not request.confirm_provider_lookup:
        raise HTTPException(
            status_code=400,
            detail="Media-read and subtitle-provider confirmations are required",
        )
    try:
        view = store.start(job_id, request.candidate_digest)
    except LibraryEpisodeRepairError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    config = get_config_manager().load()

    def run() -> None:
        execute_library_episode_audit(
            store,
            job_id,
            config,
            engine.asr,
            contract_root,
        )

    threading.Thread(
        target=run,
        name=f"library-episode-audit-{job_id[:8]}",
        daemon=True,
    ).start()
    return view


@router.get(
    "/episode-audit/{job_id}",
    dependencies=[Depends(require_local_control)],
)
def get_episode_audit(
    job_id: str,
    store: Annotated[
        LibraryEpisodeRepairStore, Depends(get_library_episode_repair_store)
    ],
) -> dict:
    try:
        return store.public(job_id)
    except LibraryEpisodeRepairError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/episode-audit/{job_id}/apply",
    dependencies=[Depends(require_local_control)],
)
def apply_episode_audit(
    job_id: str,
    request: EpisodeAuditApplyRequest,
    store: Annotated[
        LibraryEpisodeRepairStore, Depends(get_library_episode_repair_store)
    ],
) -> dict:
    """Apply an exact reviewed generic-name set; never overwrite a destination."""

    if not request.confirm_generic_rename:
        raise HTTPException(
            status_code=400, detail="Explicit generic-rename confirmation is required"
        )
    try:
        return apply_generic_repairs(
            store,
            job_id,
            result_digest=request.result_digest,
            file_ids=tuple(request.file_ids),
        )
    except LibraryEpisodeRepairError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
