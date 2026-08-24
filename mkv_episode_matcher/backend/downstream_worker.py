"""Bounded background consumer for explicitly enabled downstream stages."""

from __future__ import annotations

import json
import re
import threading

from loguru import logger

from mkv_episode_matcher.backend.automatic_rip import _AUTOMATIC_UNMATCHED_CODES
from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.pipeline_queue import DownstreamDispatcher, PipelineQueueError

_DISC_TITLE_ID = re.compile(
    r"-disc-\d+-([0-9a-f]{16})-title-(\d{3})(?:-|$)", re.IGNORECASE
)
_AUTOMATIC_DISC_COORDINATOR_CODES = _AUTOMATIC_UNMATCHED_CODES | frozenset({
    "all_season_analysis_running",
    "all_season_analysis_failed",
})
_AUTOMATIC_DISC_IMMEDIATE_CODES = _AUTOMATIC_UNMATCHED_CODES - frozenset({
    "all_season_sequence_review_required",
})


def _sequence_only_attempts(attempts: tuple[dict[str, object], ...]) -> bool:
    sequence_matched = any(
        attempt.get("branch") == "tv-local" and attempt.get("disposition") == "matched"
        for attempt in attempts
    )
    independently_matched = any(
        attempt.get("branch") in {"tv-opensubtitles", "tv-gemini"}
        and attempt.get("disposition") == "matched"
        for attempt in attempts
    )
    return sequence_matched and not independently_matched


def _current_disc_title_lineages(items: tuple[object, ...]) -> tuple[object, ...]:
    """Keep only the newest queue record for each physical disc title."""

    newest: dict[tuple[str, int], object] = {}
    for item in items:
        media_match = _DISC_TITLE_ID.search(item.media_id)
        if media_match is None:
            continue
        key = (media_match.group(1).lower(), int(media_match.group(2)))
        previous = newest.get(key)
        if previous is None or (
            getattr(item, "updated_at", ""),
            getattr(item, "created_at", ""),
            item.media_id,
        ) >= (
            getattr(previous, "updated_at", ""),
            getattr(previous, "created_at", ""),
            previous.media_id,
        ):
            newest[key] = item

    selected = []
    for item in items:
        media_match = _DISC_TITLE_ID.search(item.media_id)
        if media_match is None:
            selected.append(item)
            continue
        key = (media_match.group(1).lower(), int(media_match.group(2)))
        if newest[key] is item:
            selected.append(item)
    return tuple(selected)


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
        self._automatic_analysis_attempts: set[tuple[str, tuple[int, ...]]] = set()
        self._version_coexistence_reconciled = False
        self._legacy_sequence_assignments_reconciled = False

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

    def _apply_post_item_automation(self, item) -> bool:
        """Run item and completed-disc fallbacks without waiting for queue idle."""

        self._apply_automatic_fallback(item)
        return self._apply_automatic_disc_analysis()

    def _resume_version_coexistence_reviews(self) -> bool:
        """Requeue old broad episode collisions under the exact-version rule."""

        if self._version_coexistence_reconciled:
            return False
        config = get_config_manager().load()
        if not (
            config.automatic_processing_enabled
            and config.automatic_organization_enabled
        ):
            return False
        from mkv_episode_matcher.backend.organization_authorization import (
            organization_item_has_collision,
        )

        resumed = 0
        for item in self.dispatcher.store.list_items():
            if (
                item.state != "review_required"
                or item.review_code != "library_collision"
            ):
                continue
            if item.stage in {"identify", "transcode"}:
                self.dispatcher.store.retry(item.media_id)
                resumed += 1
                continue
            if item.stage != "organize":
                continue
            try:
                payload = json.loads(
                    item.artifact.contract_path.read_text(encoding="utf-8")
                )
                collision = organization_item_has_collision(payload, config)
            except (OSError, json.JSONDecodeError, PipelineQueueError):
                continue
            if not collision:
                self.dispatcher.store.retry(item.media_id)
                resumed += 1
        self._version_coexistence_reconciled = True
        if resumed:
            logger.info(
                "Resumed {} prior broad episode-collision review(s) under exact-version checks",
                resumed,
            )
        return resumed > 0

    def _reconcile_legacy_sequence_assignments(  # noqa: C901 - guarded migration
        self,
    ) -> bool:
        """Stop policy-v2 sequence-only assignments before downstream work."""

        if self._legacy_sequence_assignments_reconciled:
            return False
        from mkv_episode_matcher.backend.dependencies import (
            get_pipeline_contract_root,
        )
        from mkv_episode_matcher.backend.identification_dossier import (
            IdentificationDossierStore,
        )
        from mkv_episode_matcher.core.tv_identification_policy import (
            AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION,
        )

        dossier = IdentificationDossierStore(
            get_pipeline_contract_root().parent / "identification-evidence"
        )
        restarted = 0
        held = 0
        for item in self.dispatcher.store.list_items():
            if item.stage not in {"transcode", "organize"} or item.state not in {
                "queued",
                "review_required",
                "failed",
            }:
                continue
            try:
                payload = json.loads(
                    item.artifact.contract_path.read_text(encoding="utf-8")
                )
                attempts = dossier.safe_attempts(item.media_id)
            except (OSError, json.JSONDecodeError, PipelineQueueError):
                continue
            if payload.get("identification_policy_version") == (
                AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION
            ):
                continue
            if not _sequence_only_attempts(attempts):
                continue
            media_match = _DISC_TITLE_ID.search(item.media_id)
            try:
                if item.stage == "transcode" and media_match is not None:
                    self.dispatcher.store.restart_identification(
                        item.media_id,
                        expected_disc_fingerprint=media_match.group(1).lower(),
                        expected_title_index=int(media_match.group(2)),
                    )
                    restarted += 1
                elif item.stage == "organize" and item.state == "queued":
                    self.dispatcher.store.hold_for_review(
                        item.media_id, "legacy_sequence_assignment_review_required"
                    )
                    held += 1
            except PipelineQueueError:
                continue
        self._legacy_sequence_assignments_reconciled = True
        if restarted or held:
            logger.warning(
                "Quarantined legacy sequence-only assignments: restarted={} held={}",
                restarted,
                held,
            )
        return bool(restarted or held)

    def _apply_automatic_disc_analysis(self) -> bool:  # noqa: C901
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
        pending: set[str] = set()
        expected: dict[str, set[int]] = {}
        observed: dict[str, set[int]] = {}
        resolved: dict[str, set[int]] = {}
        items = _current_disc_title_lineages(tuple(self.dispatcher.store.list_items()))
        visual_reviews = self.dispatcher.store.silent_video_review_flags()
        for item in items:
            media_match = _DISC_TITLE_ID.search(item.media_id)
            if media_match is not None:
                fingerprint = media_match.group(1).lower()
                title_index = int(media_match.group(2))
                observed.setdefault(fingerprint, set()).add(title_index)
                if item.stage != "identify":
                    resolved.setdefault(fingerprint, set()).add(title_index)
        for item in items:
            if item.stage != "identify":
                continue
            try:
                payload = json.loads(
                    item.artifact.contract_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            fingerprint_value = payload.get("disc_fingerprint")
            if isinstance(fingerprint_value, str):
                fingerprint = fingerprint_value.lower()
                expected_indexes = payload.get("disc_expected_title_indexes")
                if isinstance(expected_indexes, list) and all(
                    isinstance(index, int)
                    and not isinstance(index, bool)
                    and index >= 0
                    for index in expected_indexes
                ):
                    expected.setdefault(fingerprint, set()).update(expected_indexes)
                if item.state in {"queued", "running"}:
                    pending.add(fingerprint)
                    continue
                if (
                    item.state != "review_required"
                    or item.review_code not in _AUTOMATIC_DISC_COORDINATOR_CODES
                ):
                    continue
                groups.setdefault(fingerprint, []).append(item.media_id)
                if (
                    item.review_code in _AUTOMATIC_DISC_IMMEDIATE_CODES
                    or item.review_code == "all_season_analysis_running"
                ):
                    triggered.add(fingerprint)
                elif (
                    item.review_code
                    in {
                        "all_season_sequence_review_required",
                    }
                    and config.automatic_gemini_ambiguity_fallback
                    and item.media_id not in visual_reviews
                ):
                    # Older automatic fallbacks could silently skip OCR because
                    # their evidence directory was absent. Retry such a disc
                    # once after restart; a durable visual flag prevents loops.
                    triggered.add(fingerprint)
        # Contracts created before acquisition and matching scopes were split
        # may still carry whole-disc expectations. Durable automatic/manual
        # non-episode dispositions are authoritative for matching readiness.
        disposition_reader = getattr(self.dispatcher.store, "title_dispositions", None)
        if callable(disposition_reader):
            for fingerprint, expected_indexes in expected.items():
                dispositions = disposition_reader(fingerprint)
                expected_indexes.difference_update(
                    title_index
                    for title_index, disposition in dispositions.items()
                    if disposition.get("disposition") == "skip"
                )
        matching_scope_reader = getattr(
            self.dispatcher.store, "disc_matching_scope", None
        )
        if callable(matching_scope_reader):
            for fingerprint in tuple(expected):
                prepared_scope = matching_scope_reader(fingerprint)
                if prepared_scope is not None:
                    expected[fingerprint] = set(prepared_scope)
                    if callable(disposition_reader):
                        dispositions = disposition_reader(fingerprint)
                        expected[fingerprint].difference_update(
                            title_index
                            for title_index, disposition in dispositions.items()
                            if disposition.get("disposition") == "skip"
                        )
        ready = next(
            (
                (
                    fingerprint,
                    tuple(ids),
                    (fingerprint, tuple(sorted(resolved.get(fingerprint, set())))),
                )
                for fingerprint, ids in groups.items()
                if fingerprint in triggered
                and fingerprint not in pending
                and (
                    fingerprint,
                    tuple(sorted(resolved.get(fingerprint, set()))),
                )
                not in self._automatic_analysis_attempts
                and (
                    not expected.get(fingerprint)
                    or expected[fingerprint].issubset(observed.get(fingerprint, set()))
                )
            ),
            None,
        )
        if ready is None:
            return False
        fingerprint, media_ids, attempt_key = ready
        self._automatic_analysis_attempts.add(attempt_key)
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

    def _run(self) -> None:  # noqa: C901 - isolated worker recovery boundaries
        logger.info("Downstream identification worker started")
        while not self._stop.is_set():
            try:
                if self._reconcile_legacy_sequence_assignments():
                    continue
            except Exception as exc:
                logger.error(
                    "Legacy sequence-assignment reconciliation could not run: {}",
                    type(exc).__name__,
                )
            try:
                if self._resume_version_coexistence_reviews():
                    continue
            except Exception as exc:
                logger.error(
                    "Version-coexistence review reconciliation could not run: {}",
                    type(exc).__name__,
                )
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
                    self._apply_post_item_automation(item)
                except Exception as exc:
                    logger.error(
                        "Automatic post-identification fallback could not run: {}",
                        type(exc).__name__,
                    )
        logger.info("Downstream identification worker stopped")
