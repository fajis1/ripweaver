"""Bounded background sender for consented catalogue contributions."""

from __future__ import annotations

import threading

from loguru import logger

from mkv_episode_matcher import __version__
from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.core.credentials import store_credential
from mkv_episode_matcher.core.environment import load_environment_settings
from mkv_episode_matcher.disc.catalogue_contributions import (
    CatalogueContributionError,
    CatalogueContributionStore,
)
from mkv_episode_matcher.disc.ripweaver_catalogue import (
    RipWeaverCatalogueClient,
    RipWeaverCatalogueError,
)
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore


class CatalogueContributionWorker:
    """Prepare and send at most one retryable contribution per polling turn."""

    def __init__(
        self,
        store: CatalogueContributionStore,
        pipeline_store: PipelineQueueStore,
        *,
        poll_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.pipeline_store = pipeline_store
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="catalogue-contribution-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_seconds * 2))
        self._thread = None

    def run_once(self) -> bool:
        config = get_config_manager().load()
        if not (
            config.ripweaver_catalogue_enabled
            and config.ripweaver_catalogue_contributions_enabled
        ):
            return False
        self.store.prepare_ready(self.pipeline_store)
        pending = self.store.pending(limit=1)
        if not pending:
            return False
        contribution = pending[0]
        try:
            client = RipWeaverCatalogueClient(base_url=config.ripweaver_catalogue_url)
            token = load_environment_settings().ripweaver_catalogue_token
            if not token:
                registration = client.register()
                store_credential("ripweaver-catalogue", registration.access_token)
                token = registration.access_token
            elif not client.capabilities().compatible:
                raise RipWeaverCatalogueError(
                    "RipWeaver Catalogue protocol is not compatible with this desktop"
                )
            receipt = client.contribute(
                contribution.payload,
                token=token,
                idempotency_key=(f"disc-contribution-{contribution.payload_sha256}"),
                client_version=__version__,
            )
            if receipt.payload_sha256 != contribution.payload_sha256:
                raise CatalogueContributionError(
                    "Catalogue contribution digest did not round trip"
                )
            self.store.mark_sent(contribution.payload_sha256)
            logger.info(
                "Submitted one path-free matched-disc layout to pending catalogue quarantine"
            )
            return True
        except (RipWeaverCatalogueError, CatalogueContributionError, OSError) as exc:
            self.store.mark_failed(
                contribution.payload_sha256, error_type=type(exc).__name__
            )
            logger.warning(
                "Catalogue contribution will retry safely after {}",
                type(exc).__name__,
            )
            return False

    def _run(self) -> None:
        logger.info("Catalogue contribution worker started")
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # pragma: no cover - final thread boundary
                logger.warning(
                    "Catalogue contribution worker recovered after {}",
                    type(exc).__name__,
                )
            self._stop.wait(self.poll_seconds)
        logger.info("Catalogue contribution worker stopped")
