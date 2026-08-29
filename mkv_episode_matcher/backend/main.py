import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from mkv_episode_matcher import __version__
from mkv_episode_matcher.backend.automatic_rip import automatic_rip_startup_held
from mkv_episode_matcher.backend.catalogue_worker import CatalogueContributionWorker
from mkv_episode_matcher.backend.dependencies import (
    get_catalogue_contribution_store,
    get_drive_watcher,
    get_engine,
    get_orchestration_store,
    get_pipeline_contract_root,
    get_pipeline_queue_store,
    start_windows_drive_events,
    stop_windows_drive_events,
)
from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker
from mkv_episode_matcher.backend.routers import (
    acquisition,
    catalogue,
    match,
    rip,
    scan,
    system,
)
from mkv_episode_matcher.backend.startup_queue_resume import (
    STARTUP_QUEUE_RESUME_DELAY_SECONDS,
    cancel_startup_queue_resume,
    schedule_startup_queue_resume,
)
from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.disc.makemkv_process_control import (
    MakeMKVProcessControlError,
    shutdown_makemkv_process_control,
    start_makemkv_process_control,
)
from mkv_episode_matcher.pipeline_adapters import (
    IdentifyStageAdapter,
    OrganizeStageAdapter,
)
from mkv_episode_matcher.pipeline_queue import (
    DownstreamDispatcher,
    PipelineReviewRequiredError,
)

_downstream_worker: DownstreamWorker | None = None
_catalogue_contribution_worker: CatalogueContributionWorker | None = None
UVICORN_GRACEFUL_SHUTDOWN_SECONDS = 15


def run_uvicorn_server(*, host: str, port: int) -> None:
    """Run Uvicorn with an application-visible graceful shutdown callback."""

    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        timeout_graceful_shutdown=UVICORN_GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = uvicorn.Server(config)

    def request_shutdown() -> None:
        server.should_exit = True

    app.state.request_server_shutdown = request_shutdown
    try:
        server.run()
    finally:
        if getattr(app.state, "request_server_shutdown", None) is request_shutdown:
            del app.state.request_server_shutdown


def _authorization_required(_item):
    raise PipelineReviewRequiredError("combined_pipeline_authorization_required")


def _automatic_downstream_enabled(config) -> bool:
    """Keep exact-plan review sessions free of unattended media work."""

    return bool(
        config.automatic_processing_enabled and not automatic_rip_startup_held()
    )


def _start_catalogue_contribution_worker() -> None:
    """Start catalogue contributions only outside exact-plan review sessions."""

    global _catalogue_contribution_worker
    if automatic_rip_startup_held():
        logger.info("Automatic catalogue contribution worker remained held")
        return
    _catalogue_contribution_worker = CatalogueContributionWorker(
        get_catalogue_contribution_store(), get_pipeline_queue_store()
    )
    _catalogue_contribution_worker.start()


def _start_exclusive_makemkv_control() -> None:
    """Acquire process-wide MakeMKV control before backend reconciliation."""

    try:
        cleanup = start_makemkv_process_control()
    except MakeMKVProcessControlError as exc:
        logger.critical(
            "Exclusive MakeMKV startup cleanup failed safely: {}", type(exc).__name__
        )
        raise RuntimeError(
            "RipWeaver could not establish exclusive MakeMKV process control"
        ) from None
    logger.info(
        "Exclusive MakeMKV startup cleanup verified; cleared_process_count={}",
        cleanup.cleared_count,
    )


def _arm_startup_queue_resume(config, pipeline_store) -> bool:
    """Pause normal startup briefly, then reactivate cached authorized work."""

    cancel_startup_queue_resume()
    if automatic_rip_startup_held():
        logger.info("Startup queue activation remained held for exact-plan review")
        return False
    if pipeline_store.is_paused():
        logger.info(
            "Processing queue remained durably paused; explicit resume is required"
        )
        return False

    pipeline_store.set_paused(True)

    def activate() -> None:
        try:
            pipeline_store.set_paused(False)
            if config.automatic_processing_enabled:
                from mkv_episode_matcher.backend.automatic_rip import (
                    observe_automatic_drives,
                )

                observe_automatic_drives(
                    get_drive_watcher().snapshot(),
                    enabled=True,
                    processing_paused=False,
                )
            logger.info("Startup queue grace period ended; processing queue active")
        except Exception as exc:
            logger.warning(
                "Startup queue activation failed safely; failure_type={}",
                type(exc).__name__,
            )

    schedule_startup_queue_resume(
        activate, delay_seconds=STARTUP_QUEUE_RESUME_DELAY_SECONDS
    )
    logger.info(
        "Processing queue held for startup grace period; resume_in_seconds={}",
        int(STARTUP_QUEUE_RESUME_DELAY_SECONDS),
    )
    return True


app = FastAPI(
    title="RipWeaver",
    description="Backend API for RipWeaver",
    version=__version__,
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, this should be restricted
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from mkv_episode_matcher.backend.routers import websocket

app.include_router(websocket.router)

app.include_router(scan.router)
app.include_router(match.router)
app.include_router(system.router)
app.include_router(catalogue.router)
app.include_router(rip.router)
app.include_router(acquisition.router)


# Fix MIME types on Windows - Validated Middleware Approach
@app.middleware("http")
async def fix_mime_type_middleware(request, call_next):
    response = await call_next(request)
    if request.url.path.endswith(".js"):
        response.headers["content-type"] = "application/javascript"
    elif request.url.path.endswith(".css"):
        response.headers["content-type"] = "text/css"
    return response


# Mount static files (Frontend)
# In development, use ../frontend/dist
# In production (bundled), use ./frontend
static_dir = Path(__file__).parent.parent / "frontend" / "dist"
if not static_dir.exists():
    # Fallback to local 'frontend' dir if bundled flat
    static_dir = Path(__file__).parent / "frontend"

if static_dir.exists():
    assets_dir = static_dir / "assets"
    if assets_dir.exists():
        try:
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
        except Exception as e:
            logger.warning(f"Failed to mount assets: {e}")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        # Check if file exists
        file_path = static_dir / full_path
        if file_path.exists() and file_path.is_file():
            # Manually handle root files MIME types if needed
            if file_path.name.endswith(".js"):
                return FileResponse(file_path, media_type="application/javascript")
            return FileResponse(file_path)

        # SPA Fallback
        return FileResponse(static_dir / "index.html")


@app.on_event("startup")
async def startup_event():
    global _catalogue_contribution_worker
    import threading

    # Configure logging
    config = get_config_manager().load()
    try:
        log_dir = config.cache_dir.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "mkv-match.log"

        logger.remove()
        logger.add(sys.stderr, level="INFO")
        logger.add(
            str(log_file),
            rotation="10 MB",
            retention="1 week",
            level="INFO",
            encoding="utf-8",
        )
        logger.info(f"Logging configured to {log_file}")
    except Exception as e:
        print(f"Failed to configure logging: {e}")

    logger.info("Starting RipWeaver API")
    _start_exclusive_makemkv_control()
    reconciled = get_orchestration_store().reconcile_incomplete()
    if reconciled:
        logger.warning(
            "Reconciled {} interrupted rip job(s) to paused review state",
            len(reconciled),
        )
    # Interrupted running items return to their stage queue. Normal startup
    # then applies a one-minute safety grace period before processing resumes.
    pipeline_store = get_pipeline_queue_store()
    reconciled_pipeline = pipeline_store.reconcile_incomplete()
    if reconciled_pipeline:
        logger.warning(
            "Requeued {} interrupted downstream item(s) at their current stage",
            len(reconciled_pipeline),
        )
    _arm_startup_queue_resume(config, pipeline_store)
    if start_windows_drive_events():
        logger.info("Windows optical-drive event watcher attached")
    else:
        logger.warning("Windows optical-drive event watcher unavailable")

    _start_catalogue_contribution_worker()

    def warm_up_engine():
        global _downstream_worker
        if not _automatic_downstream_enabled(config):
            logger.info(
                "Automatic match-engine warmup and downstream worker remained held"
            )
            return
        logger.info(
            "Background thread: Warming up Match Engine (loading Parakeet model)..."
        )
        engine = get_engine()
        logger.info("Background thread: Match Engine ready!")
        if _automatic_downstream_enabled(config):
            contract_root = get_pipeline_contract_root()
            contract_root.mkdir(parents=True, exist_ok=True)

            def organize(item):
                import json

                payload = json.loads(
                    item.artifact.contract_path.read_text(encoding="utf-8")
                )
                root = (
                    config.jellyfin_tv_root
                    if payload.get("episode_id") or payload.get("library_kind") == "tv"
                    else config.jellyfin_movie_root
                )
                if root is None:
                    raise PipelineReviewRequiredError("missing_library_root")
                return OrganizeStageAdapter(
                    library_root=root,
                    contract_root=contract_root,
                    confirm_organize=True,
                    allow_version_coexistence=config.automatic_organization_enabled,
                    deletion_staging_root=config.deletion_staging_root,
                )(item)

            pipeline_queue_store = get_pipeline_queue_store()
            dispatcher = DownstreamDispatcher(
                pipeline_queue_store,
                {
                    "identify": IdentifyStageAdapter(
                        engine,
                        contract_root,
                        tv_library_root=config.jellyfin_tv_root,
                        movie_library_root=config.jellyfin_movie_root,
                        allow_version_coexistence=(
                            config.automatic_organization_enabled
                        ),
                        disc_match_history=pipeline_queue_store,
                    ),
                    "transcode": _authorization_required,
                    "organize": (
                        organize
                        if config.automatic_organization_enabled
                        else _authorization_required
                    ),
                },
            )
            _downstream_worker = DownstreamWorker(
                dispatcher,
                allowed_stages=(
                    ("identify", "organize")
                    if config.automatic_organization_enabled
                    else ("identify",)
                ),
            )
            _downstream_worker.start()

    threading.Thread(target=warm_up_engine, daemon=True).start()


@app.on_event("shutdown")
async def shutdown_event():
    global _catalogue_contribution_worker, _downstream_worker
    cancel_startup_queue_resume()
    if _catalogue_contribution_worker is not None:
        _catalogue_contribution_worker.stop()
        _catalogue_contribution_worker = None
    if _downstream_worker is not None:
        _downstream_worker.stop()
        _downstream_worker = None
    stop_windows_drive_events()
    try:
        shutdown = shutdown_makemkv_process_control()
    except MakeMKVProcessControlError as exc:
        logger.critical(
            "MakeMKV shutdown settlement failed safely: {}", type(exc).__name__
        )
    else:
        logger.info(
            "MakeMKV shutdown settlement verified; tracked_child_count={} "
            "settled_child_count={}",
            shutdown.tracked_count,
            shutdown.settled_count,
        )


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": __version__}


if __name__ == "__main__":
    run_uvicorn_server(host="127.0.0.1", port=8000)
