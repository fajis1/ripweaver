import threading
from pathlib import Path

from mkv_episode_matcher.backend.library_episode_repair import (
    LibraryEpisodeRepairStore,
)
from mkv_episode_matcher.backend.rip_runtime import RipExecutionRegistry
from mkv_episode_matcher.core.config_manager import ConfigManager
from mkv_episode_matcher.core.engine import MatchEngineV2
from mkv_episode_matcher.disc.catalogue_contributions import (
    CatalogueContributionStore,
)
from mkv_episode_matcher.disc.drive_mapping import (
    DriveMappingStore,
    discover_windows_optical_devices,
)
from mkv_episode_matcher.disc.drive_watcher import DriveWatcher
from mkv_episode_matcher.disc.image_acquisition import execute_disc_image
from mkv_episode_matcher.disc.image_acquisition_bindings import (
    PrivateAcquisitionBindingStore,
)
from mkv_episode_matcher.disc.image_acquisition_store import ImageAcquisitionStore
from mkv_episode_matcher.disc.makemkv_process_control import (
    ExclusiveMakeMKVStartupControl,
)
from mkv_episode_matcher.disc.makemkv_process_control import (
    get_makemkv_startup_control as get_process_makemkv_startup_control,
)
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.preflight import resolve_makemkv_path, run_info_command
from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.disc.rip_orchestrator import run_parallel_auto_rip_queue
from mkv_episode_matcher.disc.ripper import run_rip_queue
from mkv_episode_matcher.disc.windows_drive_events import (
    DriveRefreshCoordinator,
    WindowsVolumeEventListener,
)
from mkv_episode_matcher.media.ffprobe_runner import inspect_mkv
from mkv_episode_matcher.media.handbrake_profiles import HandBrakeProfileStore
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore

# Global singleton instance
_engine_instance: MatchEngineV2 | None = None
_engine_lock = threading.Lock()
_parsing_status = "idle"  # idle, loading, ready, error
_orchestration_store: OrchestrationStore | None = None
_orchestration_lock = threading.Lock()
_private_binding_store: PrivateBindingStore | None = None
_rip_execution_registry = RipExecutionRegistry()
_pipeline_queue_store: PipelineQueueStore | None = None
_catalogue_contribution_store: CatalogueContributionStore | None = None
_drive_watcher: DriveWatcher | None = None
_handbrake_profile_store: HandBrakeProfileStore | None = None
_drive_refresh_coordinator: DriveRefreshCoordinator | None = None
_windows_volume_listener: WindowsVolumeEventListener | None = None
_library_episode_repair_store: LibraryEpisodeRepairStore | None = None
_image_acquisition_store: ImageAcquisitionStore | None = None
_private_acquisition_binding_store: PrivateAcquisitionBindingStore | None = None


def get_config_manager():
    # Helper to get config, no caching needed here as ConfigManager handles it or is lightweight
    return ConfigManager()


def get_engine() -> MatchEngineV2:
    """
    Get the singleton instance of the MatchEngine.
    Initializes it if it hasn't been created yet.
    This call blocks until initialization is complete.
    """
    global _engine_instance, _parsing_status

    with _engine_lock:
        if _engine_instance is None:
            try:
                _parsing_status = "loading"
                manager = get_config_manager()
                config = manager.load()
                _engine_instance = MatchEngineV2(config)
                _parsing_status = "ready"
            except Exception as e:
                _parsing_status = "error"
                raise e

    return _engine_instance


def get_engine_status() -> dict:
    """Non-blocking status check."""
    return {"status": _parsing_status, "loaded": _engine_instance is not None}


def get_orchestration_store() -> OrchestrationStore:
    """Return the durable local, path-redacted orchestration store."""

    global _orchestration_store
    with _orchestration_lock:
        if _orchestration_store is None:
            config = get_config_manager().load()
            database_path = config.cache_dir.parent / "orchestration" / "jobs.sqlite3"
            _orchestration_store = OrchestrationStore(database_path)
    return _orchestration_store


def get_private_binding_store() -> PrivateBindingStore:
    """Return the private local path-binding store; never expose its contents."""

    global _private_binding_store
    with _orchestration_lock:
        if _private_binding_store is None:
            config = get_config_manager().load()
            database_path = (
                config.cache_dir.parent / "orchestration" / "private-bindings.sqlite3"
            )
            _private_binding_store = PrivateBindingStore(database_path)
    return _private_binding_store


def get_image_acquisition_store() -> ImageAcquisitionStore:
    global _image_acquisition_store
    with _orchestration_lock:
        if _image_acquisition_store is None:
            config = get_config_manager().load()
            path = config.cache_dir.parent / "orchestration" / "acquisitions.sqlite3"
            _image_acquisition_store = ImageAcquisitionStore(path)
    return _image_acquisition_store


def get_private_acquisition_binding_store() -> PrivateAcquisitionBindingStore:
    global _private_acquisition_binding_store
    with _orchestration_lock:
        if _private_acquisition_binding_store is None:
            config = get_config_manager().load()
            path = (
                config.cache_dir.parent
                / "orchestration"
                / "private-acquisitions.sqlite3"
            )
            _private_acquisition_binding_store = PrivateAcquisitionBindingStore(path)
    return _private_acquisition_binding_store


def get_image_acquisition_executor():
    """Return the physical boundary; API tests must override this dependency."""

    return execute_disc_image


def get_rip_execution_registry() -> RipExecutionRegistry:
    """Return process-local active-executor controls."""

    return _rip_execution_registry


def get_makemkv_startup_control() -> ExclusiveMakeMKVStartupControl:
    """Return the process-wide exclusive MakeMKV startup safety gate."""

    return get_process_makemkv_startup_control()


def get_rip_queue_runner():
    """Return the production queue runner; tests must override this dependency."""

    return run_parallel_auto_rip_queue


def get_disc_inventory_runner():
    """Return the read-only MakeMKV info runner; tests override this dependency."""

    return run_info_command


def get_ffprobe_inspector():
    """Return constrained read-only FFprobe inspection; tests inject a fake."""

    return inspect_mkv


def get_special_feature_queue_runner():
    """Return the sequential special-feature runner; tests override it."""

    return run_rip_queue


def get_pipeline_queue_store() -> PipelineQueueStore:
    """Return the private durable downstream item queue."""

    global _pipeline_queue_store
    with _orchestration_lock:
        if _pipeline_queue_store is None:
            config = get_config_manager().load()
            database_path = (
                config.cache_dir.parent / "orchestration" / "pipeline.sqlite3"
            )
            _pipeline_queue_store = PipelineQueueStore(database_path)
    return _pipeline_queue_store


def get_catalogue_contribution_store() -> CatalogueContributionStore:
    """Return the private durable community-contribution outbox."""

    global _catalogue_contribution_store
    with _orchestration_lock:
        if _catalogue_contribution_store is None:
            config = get_config_manager().load()
            database_path = (
                config.cache_dir.parent
                / "orchestration"
                / "catalogue-contributions.sqlite3"
            )
            _catalogue_contribution_store = CatalogueContributionStore(database_path)
    return _catalogue_contribution_store


def get_pipeline_contract_root() -> Path:
    """Return the private root for immutable inter-stage JSON contracts."""

    config = get_config_manager().load()
    return config.cache_dir.parent / "orchestration" / "pipeline-contracts"


def get_library_episode_repair_store() -> LibraryEpisodeRepairStore:
    """Return the private restart-safe Jellyfin episode-repair store."""

    global _library_episode_repair_store
    with _orchestration_lock:
        if _library_episode_repair_store is None:
            config = get_config_manager().load()
            root = config.cache_dir.parent / "orchestration" / "library-repairs"
            _library_episode_repair_store = LibraryEpisodeRepairStore(root)
    return _library_episode_repair_store


def get_drive_watcher() -> DriveWatcher:
    """Return the read-only process-local drive status cache."""

    global _drive_watcher
    with _orchestration_lock:
        if _drive_watcher is None:
            config = get_config_manager().load()
            mapping_path = config.cache_dir.parent / "orchestration" / "drive-map.json"
            _drive_watcher = DriveWatcher(
                native_identity_discovery=discover_windows_optical_devices,
                mapping_store=DriveMappingStore(mapping_path),
            )
    return _drive_watcher


def get_handbrake_profile_store() -> HandBrakeProfileStore:
    """Return the local non-secret HandBrake profile library."""

    global _handbrake_profile_store
    with _orchestration_lock:
        if _handbrake_profile_store is None:
            config = get_config_manager().load()
            path = config.cache_dir.parent / "orchestration" / "handbrake-profiles.json"
            _handbrake_profile_store = HandBrakeProfileStore(path)
    return _handbrake_profile_store


def start_windows_drive_events() -> bool:
    """Attach event-driven cached drive refresh on Windows."""

    global _drive_refresh_coordinator, _windows_volume_listener
    if _drive_refresh_coordinator is not None:
        return True

    def refresh() -> None:
        config = get_config_manager().load()
        watcher = get_drive_watcher()
        with _rip_execution_registry.claim_all_drive_discovery():
            executable = resolve_makemkv_path(config.makemkv_path)
            snapshot = watcher.refresh(executable, timeout_seconds=120)
        from mkv_episode_matcher.backend.automatic_rip import observe_automatic_drives

        observe_automatic_drives(
            snapshot,
            enabled=config.automatic_processing_enabled,
            processing_paused=get_pipeline_queue_store().is_paused(),
        )

    def periodic_refresh() -> None:
        config = get_config_manager().load()
        watcher = get_drive_watcher()
        snapshot = watcher.refresh_native_media()
        from mkv_episode_matcher.backend.automatic_rip import observe_automatic_drives

        observe_automatic_drives(
            snapshot,
            enabled=config.automatic_processing_enabled,
            processing_paused=get_pipeline_queue_store().is_paused(),
        )

    coordinator = DriveRefreshCoordinator(
        refresh,
        _rip_execution_registry.has_active_optical_work,
        invalidate_bindings=get_drive_watcher().invalidate_current_disc_bindings,
        periodic_refresh=periodic_refresh,
        # Windows volume-label reads wake idle optical drives. Keep the native
        # reconciliation callback for real media events received during active
        # optical work, but do not poll every drive while the system is idle.
        poll_seconds=None,
    )
    listener = WindowsVolumeEventListener(coordinator.notify_change)
    coordinator.start()
    try:
        listener.start()
    except Exception:
        coordinator.stop()
        return False
    _drive_refresh_coordinator = coordinator
    _windows_volume_listener = listener
    # Reconcile discs that were already inserted before the server started.
    # The coordinator debounces this read-only MakeMKV info refresh and still
    # defers it while an authorized rip executor is active.
    coordinator.notify_change()
    return True


def request_windows_drive_refresh() -> bool:
    """Queue one full discovery turn without fabricating a media-change event."""

    if _drive_refresh_coordinator is None:
        return False
    _drive_refresh_coordinator.request_refresh()
    return True


def windows_drive_refresh_deferred() -> bool:
    """Return whether full discovery is queued behind active optical work."""

    return bool(
        _drive_refresh_coordinator is not None
        and _drive_refresh_coordinator.refresh_deferred()
    )


def windows_drive_automatic_discovery_status() -> dict[str, object]:
    """Return path-free automatic-discovery circuit-breaker state."""

    if _drive_refresh_coordinator is None:
        return {
            "paused": False,
            "pause_reason": None,
            "consecutive_timeout_count": 0,
        }
    return _drive_refresh_coordinator.automatic_discovery_status()


def resume_windows_drive_automatic_discovery() -> None:
    """Clear the automatic breaker before a confirmed synchronous refresh."""

    if _drive_refresh_coordinator is not None:
        _drive_refresh_coordinator.resume_automatic_discovery()


def stop_windows_drive_events() -> None:
    """Stop native volume notifications and their refresh worker."""

    global _drive_refresh_coordinator, _windows_volume_listener
    if _windows_volume_listener is not None:
        _windows_volume_listener.stop()
    if _drive_refresh_coordinator is not None:
        _drive_refresh_coordinator.stop()
    _windows_volume_listener = None
    _drive_refresh_coordinator = None
