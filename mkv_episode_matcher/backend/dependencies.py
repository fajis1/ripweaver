import threading
from pathlib import Path

from mkv_episode_matcher.backend.rip_runtime import RipExecutionRegistry
from mkv_episode_matcher.core.config_manager import ConfigManager
from mkv_episode_matcher.core.engine import MatchEngineV2
from mkv_episode_matcher.disc.orchestration_store import OrchestrationStore
from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.disc.rip_orchestrator import run_parallel_auto_rip_queue
from mkv_episode_matcher.disc.ripper import run_rip_queue
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


def get_rip_execution_registry() -> RipExecutionRegistry:
    """Return process-local active-executor controls."""

    return _rip_execution_registry


def get_rip_queue_runner():
    """Return the production queue runner; tests must override this dependency."""

    return run_parallel_auto_rip_queue


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


def get_pipeline_contract_root() -> Path:
    """Return the private root for immutable inter-stage JSON contracts."""

    config = get_config_manager().load()
    return config.cache_dir.parent / "orchestration" / "pipeline-contracts"
