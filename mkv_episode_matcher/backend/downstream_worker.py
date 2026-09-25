"""Bounded background consumer for explicitly enabled downstream stages."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass

from loguru import logger

from mkv_episode_matcher.backend.automatic_rip import (
    _AUTOMATIC_DISC_RETRY_CODES,
    _AUTOMATIC_UNMATCHED_CODES,
)
from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.pipeline_queue import DownstreamDispatcher, PipelineQueueError

_DISC_TITLE_ID = re.compile(
    r"-disc-\d+-([0-9a-f]{16})-title-(\d{3})(?:-|$)", re.IGNORECASE
)
_AUTOMATIC_DISC_COORDINATOR_CODES = (
    _AUTOMATIC_UNMATCHED_CODES
    | frozenset({
        "all_season_analysis_running",
    })
    | _AUTOMATIC_DISC_RETRY_CODES
)
_AUTOMATIC_DISC_IMMEDIATE_CODES = (
    _AUTOMATIC_UNMATCHED_CODES - frozenset({"all_season_sequence_review_required"})
    | _AUTOMATIC_DISC_RETRY_CODES
)


def downstream_processing_enabled(config: object) -> bool:
    """Keep approved downstream work independent from unattended disc ripping."""

    return bool(getattr(config, "downstream_processing_enabled", True))


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


def _contract_disc_title_identity(
    item: object, store: object | None = None
) -> tuple[str, int] | None:
    """Read exact physical title identity from the immutable rip contract.

    Recovered MakeMKV outputs intentionally keep their original basename and
    therefore do not necessarily have a canonical queue media ID.  The rip
    contract is the authoritative lineage boundary for those items.
    """

    artifacts = []
    rip_artifact_reader = getattr(store, "rip_artifact", None)
    if callable(rip_artifact_reader):
        try:
            artifacts.append(rip_artifact_reader(item.media_id))
        except (PipelineQueueError, OSError):
            pass
    artifact = getattr(item, "artifact", None)
    if artifact is not None:
        artifacts.append(artifact)
    seen_paths = set()
    for candidate in artifacts:
        contract_path = getattr(candidate, "contract_path", None)
        if contract_path is None or contract_path in seen_paths:
            continue
        seen_paths.add(contract_path)
        try:
            payload = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        fingerprint = payload.get("disc_fingerprint")
        title_index = payload.get("title_index")
        if (
            isinstance(fingerprint, str)
            and re.fullmatch(r"[0-9a-f]{16}", fingerprint, re.IGNORECASE)
            and isinstance(title_index, int)
            and not isinstance(title_index, bool)
            and title_index >= 0
        ):
            return fingerprint.lower(), title_index

    # Retain compatibility with older identified contracts that did not copy
    # physical lineage forward but still used the canonical queue ID.
    media_match = _DISC_TITLE_ID.search(item.media_id)
    if media_match is None:
        return None
    return media_match.group(1).lower(), int(media_match.group(2))


@dataclass(frozen=True)
class _GeminiRouteAssignmentDecision:
    role_accepted: bool
    identity_status: str

    def __post_init__(self) -> None:
        if self.identity_status not in {
            "invalid",
            "exact_verified",
            "exact_pending",
            "descriptive_pending",
            "descriptive_accepted",
        }:
            raise ValueError("Gemini route identity status is invalid")
        if self.role_accepted != (self.identity_status != "invalid"):
            raise ValueError("Gemini route decision is inconsistent")


def _gemini_route_assignment_decision(
    item: object, title_index: int, accepted_role: str | None
) -> _GeminiRouteAssignmentDecision:
    """Separate an accepted content role from its final identity confidence."""

    if accepted_role not in {"movie", "extra"}:
        return _GeminiRouteAssignmentDecision(False, "invalid")
    try:
        payload = json.loads(item.artifact.contract_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return _GeminiRouteAssignmentDecision(False, "invalid")
    context = payload.get("media_context")
    if not isinstance(context, dict):
        return _GeminiRouteAssignmentDecision(False, "invalid")
    assignments = context.get("special_feature_assignments")
    if not isinstance(assignments, list):
        return _GeminiRouteAssignmentDecision(False, "invalid")
    assignment = next(
        (
            assignment
            for assignment in assignments
            if isinstance(assignment, dict)
            and assignment.get("title_index") == title_index
            and assignment.get("classification") == "matched-feature"
            and assignment.get("media_kind") == accepted_role
            and type(assignment.get("provisional_match")) is bool
        ),
        None,
    )
    if assignment is None:
        return _GeminiRouteAssignmentDecision(False, "invalid")
    if assignment["provisional_match"] is False:
        if accepted_role == "movie" and not (
            type(assignment.get("tmdb_movie_id")) is int
            and assignment["tmdb_movie_id"] > 0
            and isinstance(assignment.get("matched_title"), str)
            and bool(assignment["matched_title"].strip())
            and assignment.get("identity_verification_status") == "exact_verified"
            and assignment.get("identification_method")
            in {"movie-opensubtitles", "tv-related-movie-opensubtitles"}
        ):
            return _GeminiRouteAssignmentDecision(False, "invalid")
        if accepted_role == "extra" and assignment.get(
            "identity_verification_status"
        ) == ("descriptive_accepted"):
            if not (
                assignment.get("descriptive_identity_accepted") is True
                and type(assignment.get("related_tmdb_movie_id")) is int
                and assignment["related_tmdb_movie_id"] > 0
                and assignment.get("identification_method")
                == "gemini-descriptive-extra"
            ):
                return _GeminiRouteAssignmentDecision(False, "invalid")
            return _GeminiRouteAssignmentDecision(True, "descriptive_accepted")
        return _GeminiRouteAssignmentDecision(True, "exact_verified")
    return _GeminiRouteAssignmentDecision(
        True,
        "exact_pending" if accepted_role == "movie" else "descriptive_pending",
    )


def _verified_gemini_route_assignment(
    item: object, title_index: int, accepted_role: str | None
) -> bool:
    """Compatibility predicate for callers that require a final exact identity."""

    return (
        _gemini_route_assignment_decision(
            item, title_index, accepted_role
        ).identity_status
        == "exact_verified"
    )


def _current_disc_title_lineages(
    items: tuple[object, ...], store: object | None = None
) -> tuple[object, ...]:
    """Keep only the newest queue record for each physical disc title."""

    newest: dict[tuple[str, int], object] = {}
    identities: dict[int, tuple[str, int] | None] = {}
    for item in items:
        identity = _contract_disc_title_identity(item, store)
        identities[id(item)] = identity
        if identity is None:
            continue
        key = identity
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
        identity = identities[id(item)]
        if identity is None:
            selected.append(item)
            continue
        if newest[identity] is item:
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
        self._automatic_triage_attempts: set[
            tuple[tuple[str, int | None], tuple[str, ...]]
        ] = set()
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
            and (
                config := get_config_manager().load()
            ).automatic_gemini_ambiguity_fallback
            and getattr(config, "automatic_ai_extra_titles", False)
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
        handled = self._apply_automatic_disc_analysis()
        if not handled:
            handled = self._apply_automatic_triage_analysis()
        if not handled:
            handled = self._apply_movie_extra_dependents()
        if not handled:
            handled = self._settle_terminal_tv_route()
        if not handled:
            handled = self._apply_automatic_assessed_gemini_route()
        return handled

    def _apply_movie_extra_dependents(self) -> bool:  # noqa: C901
        """Requeue descriptive extras only after their main movie is exact."""

        config = get_config_manager().load()
        store = self.dispatcher.store
        paused = getattr(store, "is_paused", None)
        if (
            not downstream_processing_enabled(config)
            or not getattr(config, "automatic_ai_extra_titles", False)
            or self._stop.is_set()
            or (callable(paused) and paused())
        ):
            return False

        from uuid import uuid4

        from mkv_episode_matcher.backend.automatic_rip import _downstream_lock
        from mkv_episode_matcher.backend.dependencies import get_pipeline_contract_root
        from mkv_episode_matcher.disc.movie_extras_identity import (
            MovieExtrasIdentityError,
            accept_descriptive_extra_identities,
            verified_movie_identity,
        )
        from mkv_episode_matcher.disc.routing import assessment_from_contract
        from mkv_episode_matcher.pipeline_queue import build_artifact

        with _downstream_lock:
            if self._stop.is_set() or (callable(paused) and paused()):
                return False
            items = _current_disc_title_lineages(tuple(store.list_items()), store)
            contract_root = get_pipeline_contract_root()
            loaded: list[tuple[object, dict, object]] = []
            for item in items:
                if item.stage != "identify":
                    identity = _contract_disc_title_identity(item, store)
                    if identity is None:
                        continue
                    latest = store.routing_latest(identity[0])
                    if latest is None:
                        continue
                    for path in sorted(
                        contract_root.glob(f"{item.media_id}*.verified-rip.json"),
                        reverse=True,
                    ):
                        try:
                            payload = json.loads(path.read_text(encoding="utf-8"))
                            context = payload["media_context"]
                            payload["media_context"] = dict(context)
                            payload["media_context"].update(
                                routing_assessment=latest.to_dict(),
                                routing_assessment_digest=latest.digest,
                                routing_assessment_revision=latest.revision,
                            )
                            verified_movie_identity(payload)
                        except (
                            OSError,
                            ValueError,
                            TypeError,
                            MovieExtrasIdentityError,
                        ):
                            continue
                        loaded.append((item, payload, latest))
                        break
                    continue
                try:
                    payload = json.loads(
                        item.artifact.contract_path.read_text(encoding="utf-8")
                    )
                    assessment = assessment_from_contract(payload)
                except (OSError, ValueError, TypeError):
                    continue
                if assessment is None:
                    continue
                latest = store.routing_latest(assessment.inventory_fingerprint)
                if latest is None or latest.title_indexes != assessment.title_indexes:
                    continue
                context = payload["media_context"]
                payload = dict(payload)
                payload["media_context"] = dict(context)
                payload["media_context"].update(
                    routing_assessment=latest.to_dict(),
                    routing_assessment_digest=latest.digest,
                    routing_assessment_revision=latest.revision,
                )
                loaded.append((item, payload, latest))

            for main_item, main_payload, assessment in loaded:
                if main_item.state != "queued":
                    continue
                try:
                    verified_movie_identity(main_payload)
                except MovieExtrasIdentityError:
                    continue
                fingerprint = assessment.inventory_fingerprint
                candidates = [
                    (item, payload)
                    for item, payload, candidate_assessment in loaded
                    if candidate_assessment.inventory_fingerprint == fingerprint
                    and item.stage == "identify"
                    and item.state == "review_required"
                    and item.review_code
                    == "provisional_content_identity_review_required"
                ]
                valid: list[tuple[object, dict]] = []
                for item, payload in candidates:
                    try:
                        accept_descriptive_extra_identities(main_payload, (payload,))
                    except MovieExtrasIdentityError:
                        store.choose_review_path(
                            item.media_id,
                            "descriptive_extra_identity_review_required",
                        )
                    else:
                        valid.append((item, payload))
                if not valid:
                    continue
                accepted = accept_descriptive_extra_identities(
                    main_payload, tuple(payload for _item, payload in valid)
                )
                contract_root.mkdir(parents=True, exist_ok=True)
                changed = False
                for (item, _payload), accepted_payload in zip(
                    valid, accepted, strict=True
                ):
                    if self._stop.is_set() or (callable(paused) and paused()):
                        return changed
                    path = (
                        contract_root
                        / f"{item.media_id}.movie-extra-{uuid4().hex[:12]}.verified-rip.json"
                    )
                    try:
                        with path.open("x", encoding="utf-8") as handle:
                            json.dump(
                                accepted_payload, handle, indent=2, sort_keys=True
                            )
                            handle.write("\n")
                        store.apply_reviewed_identification_input(
                            item.media_id, build_artifact("rip", path)
                        )
                    except (OSError, PipelineQueueError):
                        store.choose_review_path(
                            item.media_id,
                            "descriptive_extra_identity_review_required",
                        )
                        continue
                    changed = True
                return changed
        return False

    def _settle_terminal_tv_route(self) -> bool:  # noqa: C901 - guarded saved-result handoff
        """Record a final TV result without treating a service outage as no-match."""

        config = get_config_manager().load()
        store = self.dispatcher.store
        paused = getattr(store, "is_paused", None)
        if not downstream_processing_enabled(config) or (callable(paused) and paused()):
            return False
        from mkv_episode_matcher.disc.routing import assessment_from_contract
        from mkv_episode_matcher.disc.routing_controller import (
            next_route,
            terminal_tv_review_outcome,
        )

        for item in store.list_items():
            if item.stage != "identify" or item.state != "review_required":
                continue
            outcome = terminal_tv_review_outcome(item.review_code)
            if outcome is None:
                continue
            try:
                payload = json.loads(
                    item.artifact.contract_path.read_text(encoding="utf-8")
                )
                assessment = assessment_from_contract(payload)
            except (OSError, ValueError, TypeError):
                continue
            if assessment is None:
                continue
            title_index = payload["title_index"]
            title_role = next(
                role.role
                for role in assessment.title_roles()
                if role.title_index == title_index
            )
            if title_role != "tv":
                continue
            latest = store.routing_latest(assessment.inventory_fingerprint)
            if latest is None or latest.digest != assessment.digest:
                continue
            attempts = store.routing_attempts(
                assessment.inventory_fingerprint, title_index
            )
            if (
                next_route(assessment, title_index=title_index, attempts=attempts)
                != "tv"
            ):
                continue
            claimed = store.routing_claim_next(
                assessment,
                title_index,
                media_id=item.media_id,
                expected_review_code=item.review_code,
            )
            if claimed != "tv":
                continue
            store.routing_settle(assessment, title_index, "tv", outcome)
            return True
        return False

    def _apply_automatic_assessed_gemini_route(self) -> bool:  # noqa: C901
        """Try one opted-in, assessment-bound title without a detached task."""

        config = get_config_manager().load()
        store = self.dispatcher.store
        if (
            not downstream_processing_enabled(config)
            or not config.automatic_gemini_ambiguity_fallback
            or self._stop.is_set()
            or store.is_paused()
        ):
            return False
        from mkv_episode_matcher.backend.automatic_rip import _downstream_lock
        from mkv_episode_matcher.backend.dependencies import (
            get_engine,
            get_pipeline_contract_root,
        )
        from mkv_episode_matcher.backend.gemini_fallback import (
            GeminiFallbackOutcome,
            execute_gemini_fallback,
        )
        from mkv_episode_matcher.disc.routing import assessment_from_contract
        from mkv_episode_matcher.disc.routing_controller import next_route

        eligible_codes = {
            "content_classification_required",
            "movie_identification_required",
            "special_feature_evidence_required",
            "gemini_evidence_required",
            "gemini_descriptive_review_required",
            "tv_title_no_match",
        }
        for item in store.list_items():
            if (
                item.stage != "identify"
                or item.state != "review_required"
                or item.review_code not in eligible_codes
            ):
                continue
            try:
                payload = json.loads(
                    item.artifact.contract_path.read_text(encoding="utf-8")
                )
                assessment = assessment_from_contract(payload)
            except (OSError, ValueError, TypeError):
                continue
            if assessment is None:
                continue
            title_index = payload["title_index"]
            latest = store.routing_latest(assessment.inventory_fingerprint)
            if latest is None or latest.digest != assessment.digest:
                continue
            attempts = store.routing_attempts(
                assessment.inventory_fingerprint, title_index
            )
            route = next_route(assessment, title_index=title_index, attempts=attempts)
            if route not in {"classify", "movie", "extra"}:
                continue
            if route == "extra" and not getattr(
                config, "automatic_ai_extra_titles", False
            ):
                continue
            with _downstream_lock:
                if self._stop.is_set() or store.is_paused():
                    return False
                claimed = store.routing_claim_next(
                    assessment,
                    title_index,
                    media_id=item.media_id,
                    expected_review_code=item.review_code,
                )
                if claimed != route:
                    if claimed is not None:
                        store.routing_settle(
                            assessment, title_index, claimed, "interrupted"
                        )
                    continue
                settled = False
                try:
                    store.choose_review_path(item.media_id, "gemini_evidence_required")
                    if self._stop.is_set() or store.is_paused():
                        store.routing_settle(
                            assessment, title_index, route, "interrupted"
                        )
                        return True
                    report = execute_gemini_fallback(
                        store,
                        (item.media_id,),
                        config,
                        get_engine().asr,
                        get_pipeline_contract_root(),
                        return_outcomes=True,
                    )
                    if (
                        not isinstance(report, GeminiFallbackOutcome)
                        or len(report.titles) != 1
                    ):
                        raise ValueError("Gemini route report is incomplete")
                    result = report.titles[0]
                    if result.media_id != item.media_id or result.disposition not in {
                        "matched",
                        "no_match",
                        "review",
                        "service_failed",
                    }:
                        raise ValueError("Gemini route report is invalid")
                    current = store.get(item.media_id)
                    outcome = result.disposition
                    assignment_decision = _GeminiRouteAssignmentDecision(
                        False, "invalid"
                    )
                    if outcome == "matched" and (
                        current.state != "queued"
                        or current.stage != "identify"
                        or current.artifact.contract_path == item.artifact.contract_path
                    ):
                        outcome = "review"
                    elif outcome == "matched":
                        assignment_decision = _gemini_route_assignment_decision(
                            current, title_index, result.accepted_role
                        )
                        if not assignment_decision.role_accepted:
                            outcome = "review"
                        elif assignment_decision.identity_status != "exact_verified":
                            store.hold_for_review(
                                item.media_id,
                                "provisional_content_identity_review_required",
                            )
                    elif outcome != "matched" and current.state != "review_required":
                        outcome = "review"
                    if outcome == "service_failed":
                        store.hold_for_review(item.media_id, "gemini_provider_failed")
                    if outcome == "matched":
                        from dataclasses import replace

                        from mkv_episode_matcher.disc.routing import TitleEvidence

                        store.routing_settle(assessment, title_index, route, outcome)
                        settled = True

                        current_latest = assessment
                        for _ in range(10):
                            new_item = TitleEvidence(
                                title_index,
                                "content",
                                "supported",
                                result.accepted_role,
                            )
                            if new_item in current_latest.evidence:
                                break
                            new_evidence = current_latest.evidence + (new_item,)
                            new_assessment = replace(
                                current_latest,
                                evidence=new_evidence,
                                revision=current_latest.revision + 1,
                            )
                            try:
                                store.routing_append(
                                    new_assessment,
                                    expected_revision=current_latest.revision,
                                )
                                break
                            except Exception as exc:
                                from mkv_episode_matcher.disc.routing import (
                                    RoutingError,
                                )

                                if not isinstance(exc, RoutingError):
                                    raise
                                current_latest = store.routing_latest(
                                    assessment.inventory_fingerprint
                                )
                                if (
                                    current_latest is None
                                    or current_latest.revision <= assessment.revision
                                ):
                                    raise ValueError(
                                        "Could not resolve sibling revision race during append"
                                    ) from exc
                        else:
                            raise ValueError(
                                "Exceeded maximum retries for sibling revision race"
                            )
                    else:
                        store.routing_settle(assessment, title_index, route, outcome)
                        settled = True
                except Exception:
                    logger.exception("Automatic Gemini title route held safely")
                    if settled:
                        continue
                    current = store.get(item.media_id)
                    if (
                        current.stage == "identify"
                        and current.state == "queued"
                        and current.artifact.contract_path
                        != item.artifact.contract_path
                    ):
                        # A provider/contract failure after one applied result
                        # must not let an unverified partial outcome advance.
                        store.hold_for_review(item.media_id, "gemini_analysis_failed")
                        store.routing_settle(assessment, title_index, route, "review")
                    else:
                        store.routing_settle(
                            assessment, title_index, route, "service_failed"
                        )
                    if (
                        current.stage == "identify"
                        and current.state == "review_required"
                    ):
                        store.choose_review_path(
                            item.media_id, "gemini_provider_failed"
                        )
                return True
        return False

    def _resume_version_coexistence_reviews(self) -> bool:
        """Requeue old broad episode collisions under the exact-version rule."""

        if self._version_coexistence_reconciled:
            return False
        config = get_config_manager().load()
        if not (
            downstream_processing_enabled(config)
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
            identity = _contract_disc_title_identity(item, self.dispatcher.store)
            try:
                if item.stage == "transcode" and identity is not None:
                    fingerprint, title_index = identity
                    self.dispatcher.store.restart_identification(
                        item.media_id,
                        expected_disc_fingerprint=fingerprint,
                        expected_title_index=title_index,
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
        if not downstream_processing_enabled(config):
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
        items = _current_disc_title_lineages(
            tuple(self.dispatcher.store.list_items()), self.dispatcher.store
        )
        visual_reviews = self.dispatcher.store.silent_video_review_flags()
        for item in items:
            identity = _contract_disc_title_identity(item, self.dispatcher.store)
            if identity is not None:
                fingerprint, title_index = identity
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

    def _apply_automatic_triage_analysis(self) -> bool:  # noqa: C901
        """Recover automatic TV triage / loose-file items without a physical disc fingerprint."""

        config = get_config_manager().load()
        if not downstream_processing_enabled(config):
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

        groups: dict[tuple[str, int | None], list[str]] = {}
        pending: set[tuple[str, int | None]] = set()

        items = tuple(self.dispatcher.store.list_items())
        for item in items:
            if item.stage != "identify":
                continue
            try:
                payload = json.loads(
                    item.artifact.contract_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("disc_fingerprint") is not None:
                continue
            context = payload.get("media_context")
            if not isinstance(context, dict):
                continue
            if context.get("content_hint") not in {None, "tv"}:
                continue
            series_name = context.get("series_name")
            if not isinstance(series_name, str) or not series_name.strip():
                continue
            season = context.get("season")
            if season is not None and (
                isinstance(season, bool) or not isinstance(season, int) or season < 0
            ):
                season = None
            key = (series_name.strip().casefold(), season)
            if item.state in {"queued", "running"}:
                pending.add(key)
                continue
            if (
                item.state != "review_required"
                or item.review_code not in _AUTOMATIC_DISC_COORDINATOR_CODES
            ):
                continue
            groups.setdefault(key, []).append(item.media_id)

        ready = next(
            (
                (key, tuple(ids))
                for key, ids in groups.items()
                if key not in pending
                and (key, tuple(sorted(ids))) not in self._automatic_triage_attempts
            ),
            None,
        )
        if ready is None:
            return False

        ready_key, media_ids = ready
        self._automatic_triage_attempts.add((ready_key, tuple(sorted(media_ids))))
        from mkv_episode_matcher.backend.automatic_rip import (
            _downstream_lock,
            _resolve_automatic_unmatched_season,
        )
        from mkv_episode_matcher.backend.dependencies import get_pipeline_contract_root

        with _downstream_lock:
            _resolve_automatic_unmatched_season(
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
                self._apply_movie_extra_dependents()
            except Exception as exc:
                logger.error(
                    "Movie-extra dependency reconciliation could not run: {}",
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
                        handled = self._apply_automatic_triage_analysis()
                    except Exception as exc:
                        logger.error(
                            "Automatic triage analysis could not run: {}",
                            type(exc).__name__,
                        )
                        handled = False
                if not handled:
                    try:
                        handled = self._settle_terminal_tv_route()
                    except Exception as exc:
                        logger.error(
                            "Automatic TV route settlement could not run: {}",
                            type(exc).__name__,
                        )
                        handled = False
                if not handled:
                    try:
                        handled = self._apply_automatic_assessed_gemini_route()
                    except Exception as exc:
                        logger.error(
                            "Automatic assessed Gemini route could not run: {}",
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
