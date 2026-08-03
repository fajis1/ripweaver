"""Bounded background consumer for explicitly enabled downstream stages."""

from __future__ import annotations

import json
import threading

from loguru import logger

from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.pipeline_queue import DownstreamDispatcher


class DownstreamWorker:
    """Poll one durable queue while preserving its global single-worker lock."""

    def __init__(
        self,
        dispatcher: DownstreamDispatcher,
        *,
        allowed_stages: tuple[str, ...],
        poll_seconds: float = 1.0,
    ):
        self.dispatcher = dispatcher
        self.allowed_stages = allowed_stages
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_automatic_transcode_plan: str | None = None
        self._automatic_transcode_media_ids: tuple[str, ...] = ()
        self._automatic_analysis_attempts: set[str] = set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="mkv-identify-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_seconds * 2))
        self._thread = None

    def _apply_automatic_fallback(self, item) -> None:
        if (
            item.review_code == "special_feature_evidence_required"
            and get_config_manager().load().automatic_gemini_ambiguity_fallback
        ):
            # This records the opted-in fallback path only. Evidence
            # preparation and every external provider call remain separate
            # guarded operations.
            self.dispatcher.store.choose_review_path(
                item.media_id, "gemini_evidence_required"
            )

    def _apply_automatic_disc_analysis(self) -> bool:
        """Recover automatic TV batches that settled into a sequence hold."""

        config = get_config_manager().load()
        if not config.automatic_processing_enabled:
            return False
        if self._automatic_transcode_media_ids:
            active = tuple(
                self.dispatcher.store.get(media_id)
                for media_id in self._automatic_transcode_media_ids
            )
            if any(
                item.stage == "transcode" and item.state in {"queued", "running"}
                for item in active
            ):
                return False
            self._automatic_transcode_media_ids = ()
        groups: dict[str, list[str]] = {}
        triggered: set[str] = set()
        for item in self.dispatcher.store.list_items():
            if (
                item.stage != "identify"
                or item.state != "review_required"
                or item.review_code
                not in {
                    "missing_season_context",
                    "unmatched_disc_analysis_required",
                    "all_season_analysis_running",
                    "all_season_analysis_failed",
                    "all_season_sequence_review_required",
                }
            ):
                continue
            try:
                payload = json.loads(
                    item.artifact.contract_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            fingerprint = payload.get("disc_fingerprint")
            if isinstance(fingerprint, str):
                groups.setdefault(fingerprint, []).append(item.media_id)
                if item.review_code in {
                    "missing_season_context",
                    "unmatched_disc_analysis_required",
                    "all_season_analysis_running",
                }:
                    triggered.add(fingerprint)
        ready = next(
            (
                (fingerprint, tuple(ids))
                for fingerprint, ids in groups.items()
                if fingerprint in triggered
                and fingerprint not in self._automatic_analysis_attempts
                and len(ids) >= 2
            ),
            None,
        )
        if ready is None:
            return False
        fingerprint, media_ids = ready
        self._automatic_analysis_attempts.add(fingerprint)
        from mkv_episode_matcher.backend.automatic_rip import (
            _downstream_lock,
            _resolve_automatic_unmatched_disc,
        )
        from mkv_episode_matcher.backend.dependencies import get_pipeline_contract_root

        with _downstream_lock:
            _resolve_automatic_unmatched_disc(
                media_ids,
                self.dispatcher.store,
                config,
                get_pipeline_contract_root(),
            )
        return True

    def _start_automatic_transcode_if_ready(self) -> bool:
        """Start one newly discovered, resolution-aware transcode batch."""

        config = get_config_manager().load()
        if not config.automatic_processing_enabled:
            return False
        from mkv_episode_matcher.backend.automatic_rip import _downstream_lock
        from mkv_episode_matcher.backend.dependencies import (
            get_handbrake_profile_store,
            get_pipeline_contract_root,
        )
        from mkv_episode_matcher.backend.routers.rip import (
            AuthorizeTranscodeRequest,
            authorize_transcode_batch,
        )
        from mkv_episode_matcher.backend.transcode_authorization import (
            build_transcode_authorization_plan,
        )

        with _downstream_lock:
            profiles = get_handbrake_profile_store()
            try:
                plan = build_transcode_authorization_plan(
                    self.dispatcher.store,
                    profiles,
                    config,
                    profile_id=None,
                )
            except Exception:
                return False
            if plan.plan_sha256 == self._last_automatic_transcode_plan:
                return False
            authorize_transcode_batch(
                AuthorizeTranscodeRequest(
                    expected_plan_sha256=plan.plan_sha256,
                    authorized_item_count=len(plan.media_ids),
                    profile_id=None,
                    confirm_transcode=True,
                ),
                self.dispatcher.store,
                profiles,
                get_pipeline_contract_root(),
            )
            self._last_automatic_transcode_plan = plan.plan_sha256
            self._automatic_transcode_media_ids = plan.media_ids
        logger.info(
            "Automatic resolution-aware transcode batch started for {} item(s)",
            len(plan.media_ids),
        )
        return True

    def _run(self) -> None:
        logger.info("Downstream identification worker started")
        while not self._stop.is_set():
            try:
                item = self.dispatcher.run_one(allowed_stages=self.allowed_stages)
            except Exception as exc:
                logger.error(
                    "Downstream identification worker paused after queue error: {}",
                    type(exc).__name__,
                )
                self._stop.wait(self.poll_seconds)
                continue
            if item is None:
                try:
                    handled = self._apply_automatic_disc_analysis()
                except Exception as exc:
                    logger.error(
                        "Automatic disc-sequence fallback could not run: {}",
                        type(exc).__name__,
                    )
                    handled = False
                if not handled:
                    try:
                        handled = self._start_automatic_transcode_if_ready()
                    except Exception as exc:
                        logger.error(
                            "Automatic transcode batch could not start: {}",
                            type(exc).__name__,
                        )
                if not handled:
                    self._stop.wait(self.poll_seconds)
            else:
                try:
                    self._apply_automatic_fallback(item)
                except Exception as exc:
                    logger.error(
                        "Automatic ambiguity fallback could not be recorded: {}",
                        type(exc).__name__,
                    )
        logger.info("Downstream identification worker stopped")
