"""Disc-level all-season identification for seasonless television rips."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from mkv_episode_matcher.backend.identification_dossier import collect_dossier_evidence
from mkv_episode_matcher.core.credentials import ApiCredentialError, ApiServiceError
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.media.episode_catalog import EpisodeCatalogEntry
from mkv_episode_matcher.media.evidence_bundle import (
    SavedFileEvidence,
    SavedTranscriptWindow,
)
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiEpisodeRanker,
    GeminiResponseError,
)
from mkv_episode_matcher.media.play_all_detection import (
    MatchedEpisodeEvidence,
    detect_play_all,
)
from mkv_episode_matcher.media.sequence_matcher import (
    SequenceGroup,
    plan_disc_sequences,
)
from mkv_episode_matcher.pipeline_queue import (
    PipelineQueueError,
    PipelineQueueStore,
    build_artifact,
)
from mkv_episode_matcher.tmdb_client import fetch_aired_episode_catalog_for_show


class GeminiAnalysisError(PipelineQueueError):
    """Credential-free provider diagnostic safe for durable events and logs."""

    def __init__(self, review_code: str, diagnostic: str):
        self.review_code = review_code
        self.diagnostic = diagnostic
        super().__init__(diagnostic)


@dataclass(frozen=True)
class _ReviewSequencePlan:
    disposition: str = "review"
    score: float = 0.0
    global_margin: float = 0.0
    groups: tuple = ()


def _episode_runtime_consistent(
    source_duration: float, candidate: EpisodeCatalogEntry
) -> bool:
    """Reject obvious play-all/compilation titles from one-episode assignment."""

    candidate_duration = candidate.runtime_seconds
    if candidate_duration is None or candidate_duration <= 0:
        return True
    maximum = max(candidate_duration * 1.75, candidate_duration + 1800)
    return source_duration <= maximum


_EPISODE_FILE = re.compile(r"(?i)(?<![A-Z0-9])S(\d{1,3})E(\d{1,3})(?!\d)")


def existing_library_episodes(
    library_root: Path | None, series_name: str
) -> frozenset[tuple[int, int]]:
    """Inspect filenames only and return episode numbers already in Jellyfin."""

    if library_root is None or not library_root.is_dir():
        return frozenset()
    normalized = re.sub(r"[^a-z0-9]+", "", series_name.casefold())
    series_directories = tuple(
        child
        for child in library_root.iterdir()
        if child.is_dir()
        and re.sub(r"[^a-z0-9]+", "", child.name.casefold()) == normalized
    )
    found: set[tuple[int, int]] = set()
    for directory in series_directories:
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in {
                ".mkv",
                ".mp4",
                ".m4v",
                ".avi",
                ".mov",
                ".ts",
            }:
                continue
            match = _EPISODE_FILE.search(path.name)
            if match is not None:
                found.add((int(match.group(1)), int(match.group(2))))
    return frozenset(found)


def prioritize_missing_catalog(
    catalog: tuple[EpisodeCatalogEntry, ...],
    library_episodes: frozenset[tuple[int, int]],
    file_count: int,
) -> tuple[tuple[EpisodeCatalogEntry, ...], str]:
    """Retain every aired candidate and annotate whether Jellyfin can assist."""

    del file_count
    if library_episodes:
        return catalog, "all-library-aware"
    return catalog, "all"


def assigned_disc_episodes(
    store: PipelineQueueStore, disc_fingerprint: str
) -> frozenset[tuple[int, int]]:
    """Return episode numbers already identified for this exact disc."""

    assigned: set[tuple[int, int]] = set()
    for outcome in store.title_history(disc_fingerprint).values():
        episode_id = outcome.get("episode_id")
        if not isinstance(episode_id, str):
            continue
        match = _EPISODE_FILE.fullmatch(episode_id)
        if match is not None:
            assigned.add((int(match.group(1)), int(match.group(2))))
    return frozenset(assigned)


def classify_gemini_failure(exc: Exception) -> GeminiAnalysisError:
    if isinstance(exc, ApiCredentialError):
        status = f" (HTTP {exc.status_code})" if exc.status_code else ""
        return GeminiAnalysisError(
            "gemini_credential_rejected",
            f"Gemini credential {exc.credential} was not accepted{status}: {exc.reason}",
        )
    if isinstance(exc, ApiServiceError):
        status = exc.status_code
        if status == 429:
            code = "gemini_rate_limited"
        elif status is not None and status >= 500:
            code = "gemini_provider_unavailable"
        elif status is not None and status >= 400:
            code = "gemini_request_rejected"
        else:
            code = "gemini_network_failed"
        suffix = f" (HTTP {status})" if status is not None else ""
        return GeminiAnalysisError(
            code, f"Gemini provider failure{suffix}: {exc.reason}"
        )
    if isinstance(exc, GeminiResponseError):
        return GeminiAnalysisError("gemini_response_invalid", str(exc))
    if isinstance(exc, PipelineQueueError):
        return GeminiAnalysisError("gemini_response_invalid", str(exc))
    return GeminiAnalysisError(
        "gemini_provider_failed",
        f"Gemini provider failure: {type(exc).__name__}",
    )


def execute_unmatched_disc_analysis(  # noqa: C901 - guarded disc-level workflow
    store: PipelineQueueStore,
    disc_fingerprint: str,
    series_name: str,
    config: Config,
    asr,
    contract_root: Path,
    *,
    season: int | None = None,
    allow_gemini: bool = False,
    allow_content_fallback: bool = True,
) -> tuple[str, ...]:
    """Transcribe held titles and compare them to the reviewed episode scope."""

    selected_by_title = {}
    for item in store.list_items():
        if item.stage != "identify" or item.state != "review_required":
            continue
        try:
            payload = json.loads(
                item.artifact.contract_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineQueueError("Verified rip contract is unavailable") from exc
        if payload.get("disc_fingerprint") != disc_fingerprint:
            continue
        title_index = payload.get("title_index")
        if not isinstance(title_index, int) or isinstance(title_index, bool):
            continue
        previous = selected_by_title.get(title_index)
        if previous is None or item.updated_at >= previous[0].updated_at:
            selected_by_title[title_index] = (item, payload)
    selected = [selected_by_title[index] for index in sorted(selected_by_title)]
    if len(selected) < 2:
        raise PipelineQueueError(
            "At least two held titles are required for disc sequence analysis"
        )

    catalog = fetch_aired_episode_catalog_for_show(series_name)
    if season is not None:
        catalog = tuple(item for item in catalog if item.season == season)
    if not catalog:
        raise PipelineQueueError(
            "Episode catalogue is unavailable for the reviewed scope"
        )
    gemini_evidence, dossier = collect_dossier_evidence(
        tuple(selected), config, asr, contract_root
    )
    evidence = tuple(
        SavedFileEvidence(
            file_id=item.file_id,
            duration_seconds=item.duration_seconds,
            windows=tuple(
                SavedTranscriptWindow(float(index), excerpt)
                for index, excerpt in enumerate(item.transcript_excerpts)
            ),
        )
        for item in gemini_evidence
    )
    if len(evidence) != len(selected):
        raise PipelineQueueError("All-season analysis omitted a reviewed title")
    library_episodes = existing_library_episodes(
        getattr(config, "jellyfin_tv_root", None), series_name
    )
    disc_episodes = assigned_disc_episodes(store, disc_fingerprint)
    candidate_catalog = tuple(
        entry
        for entry in catalog
        if (entry.season, entry.episode) not in disc_episodes
    )
    if len(candidate_catalog) < len(selected):
        raise PipelineQueueError(
            "Episode catalogue has too few unassigned entries for this disc"
        )
    known_episodes = library_episodes | disc_episodes
    gemini_catalog, gemini_candidate_scope = prioritize_missing_catalog(
        candidate_catalog, known_episodes, len(selected)
    )
    existing_episode_ids = frozenset(
        entry.episode_id
        for entry in catalog
        if (entry.season, entry.episode) in known_episodes
    )
    local_evidence_available = all(item.windows for item in evidence)
    plan = (
        plan_disc_sequences(
            evidence,
            candidate_catalog,
            (
                SequenceGroup(
                    f"disc-{disc_fingerprint}",
                    tuple(item.file_id for item in evidence),
                ),
            ),
        )
        if local_evidence_available
        else _ReviewSequencePlan()
    )
    store.record_sequence_diagnostic(
        tuple(item.media_id for item, _payload in selected),
        catalog_episode_count=len(candidate_catalog),
        file_count=len(selected),
        best_score=plan.score,
        runner_up_score=max(0.0, plan.score - plan.global_margin),
        global_margin=plan.global_margin,
        disposition=plan.disposition,
        library_episode_count=len(library_episodes),
        candidate_scope="all",
    )
    media_ids = tuple(item.media_id for item, _payload in selected)
    catalog_by_id = {entry.episode_id: entry for entry in candidate_catalog}
    duration_by_id = {item.file_id: item.duration_seconds for item in gemini_evidence}
    provisional: dict[str, float] = {}
    if plan.disposition == "proposed":
        raw_proposed = {
            item.file_id: catalog_by_id[item.proposed_episode]
            for group in plan.groups
            for item in group.items
        }
        proposed = {
            file_id: candidate
            for file_id, candidate in raw_proposed.items()
            if _episode_runtime_consistent(duration_by_id[file_id], candidate)
        }
        for media_id in media_ids:
            candidate = raw_proposed.get(media_id)
            runtime_consistent = media_id in proposed
            dossier.record_attempt(
                (media_id,),
                branch="tv-local",
                disposition="matched" if runtime_consistent else "review",
                summary={
                    "best_score": round(plan.score, 6),
                    "runner_up_score": round(
                        max(0.0, plan.score - plan.global_margin), 6
                    ),
                    "margin": round(plan.global_margin, 6),
                    "candidate_scope": "all",
                    "candidate_count": len(catalog),
                    "candidate_episode_id": (
                        candidate.episode_id if candidate is not None else None
                    ),
                    "runtime_consistent": runtime_consistent,
                    "reason": (
                        "scored"
                        if runtime_consistent
                        else "probable_play_all_or_compilation"
                    ),
                },
            )
    elif allow_gemini:
        dossier.record_attempt(
            media_ids,
            branch="tv-local",
            disposition="review",
            summary={
                "best_score": round(plan.score, 6),
                "runner_up_score": round(max(0.0, plan.score - plan.global_margin), 6),
                "margin": round(plan.global_margin, 6),
                "candidate_scope": "all",
                "candidate_count": len(catalog),
                "reason": (
                    "scored"
                    if local_evidence_available
                    else "usable_transcript_unavailable"
                ),
            },
        )
        try:
            dossier.record_attempt(
                media_ids,
                branch="tv-gemini",
                disposition="started",
                summary={
                    "candidate_scope": gemini_candidate_scope,
                    "candidate_count": len(gemini_catalog),
                },
            )
            ranker = GeminiEpisodeRanker(model=config.gemini_model)
            initial_review = ranker.rank_with_configured_keys(
                gemini_evidence,
                gemini_catalog,
                prior_attempts={
                    media_id: dossier.safe_attempts(media_id) for media_id in media_ids
                },
                existing_episode_ids=existing_episode_ids,
            )
            initial_matches = {item.file_id: item for item in initial_review.matches}
            if set(initial_matches) != {item.file_id for item in evidence}:
                raise PipelineQueueError("Gemini did not review every disc title")
            for file_id, match in initial_matches.items():
                dossier.record_attempt(
                    (file_id,),
                    branch="tv-gemini",
                    disposition="review",
                    summary={
                        "phase": "initial",
                        "candidate_episode_id": match.episode_id,
                        "confidence": round(match.confidence, 6),
                    },
                )
            confirmation_review = ranker.rank_with_configured_keys(
                gemini_evidence,
                gemini_catalog,
                prior_attempts={
                    media_id: dossier.safe_attempts(media_id) for media_id in media_ids
                },
                existing_episode_ids=existing_episode_ids,
            )
            matches = {item.file_id: item for item in confirmation_review.matches}
            if set(matches) != {item.file_id for item in evidence}:
                raise PipelineQueueError("Gemini did not review every disc title")
            gemini_catalog_by_id = {entry.episode_id: entry for entry in gemini_catalog}
            duration_by_id = {
                item.file_id: item.duration_seconds for item in gemini_evidence
            }
            resolved_matches = {
                file_id: match
                for file_id, match in matches.items()
                if match.episode_id is not None
                and match.episode_id == initial_matches[file_id].episode_id
                and match.confidence >= config.min_confidence
                and initial_matches[file_id].confidence >= config.min_confidence
                and _episode_runtime_consistent(
                    duration_by_id[file_id],
                    gemini_catalog_by_id[str(match.episode_id)],
                )
            }
            if not resolved_matches:
                raise PipelineQueueError(
                    "Gemini did not identify any disc title confidently"
                )
            proposed = {
                file_id: gemini_catalog_by_id[str(match.episode_id)]
                for file_id, match in resolved_matches.items()
            }
            provisional = {
                file_id: min(match.confidence, initial_matches[file_id].confidence)
                for file_id, match in resolved_matches.items()
            }
            for file_id, match in matches.items():
                dossier.record_attempt(
                    (file_id,),
                    branch="tv-gemini",
                    disposition="matched" if file_id in proposed else "review",
                    summary={
                        "phase": "confirmation",
                        "candidate_scope": gemini_candidate_scope,
                        "candidate_episode_id": match.episode_id,
                        "confidence": round(match.confidence, 6),
                        "consistent_with_initial": (
                            match.episode_id == initial_matches[file_id].episode_id
                        ),
                        "runtime_consistent": (
                            match.episode_id is None
                            or _episode_runtime_consistent(
                                duration_by_id[file_id],
                                gemini_catalog_by_id[str(match.episode_id)],
                            )
                        ),
                    },
                )
        except Exception as exc:
            failure = classify_gemini_failure(exc)
            dossier.record_attempt(
                media_ids,
                branch="tv-gemini",
                disposition="failed",
                summary={"reason": failure.review_code},
            )
            # One confirmed run may try each content family once. Reuse the
            # exact cached evidence for a movie/TV-movie/bonus classification
            # before returning the combined failure for review.
            try:
                from mkv_episode_matcher.backend.gemini_fallback import (
                    execute_gemini_fallback,
                )

                if not allow_content_fallback:
                    raise failure
                for media_id in media_ids:
                    store.choose_review_path(media_id, "gemini_analysis_running")
                applied = execute_gemini_fallback(
                    store, media_ids, config, asr, contract_root
                )
                if set(applied) == set(media_ids):
                    return applied
            except Exception as fallback_exc:
                dossier.record_attempt(
                    media_ids,
                    branch="gemini-synthesis",
                    disposition="failed",
                    summary={"reason": type(fallback_exc).__name__},
                )
            raise failure from exc
    else:
        dossier.record_attempt(
            media_ids,
            branch="tv-local",
            disposition="review",
            summary={
                "best_score": round(plan.score, 6),
                "runner_up_score": round(max(0.0, plan.score - plan.global_margin), 6),
                "margin": round(plan.global_margin, 6),
                "candidate_scope": "all",
                "candidate_count": len(catalog),
                "reason": (
                    "scored"
                    if local_evidence_available
                    else "usable_transcript_unavailable"
                ),
            },
        )
        raise PipelineQueueError("All-season sequence result requires review")
    matched_evidence = tuple(
        MatchedEpisodeEvidence(
            file_id=file_id,
            season=entry.season,
            episode=entry.episode,
            duration_seconds=duration_by_id[file_id],
            size_bytes=next(
                (
                    payload.get("source_size_bytes")
                    for item, payload in selected
                    if item.media_id == file_id
                ),
                None,
            ),
        )
        for file_id, entry in proposed.items()
    )
    play_all_ids: set[str] = set()
    for item, payload in selected:
        if item.media_id in proposed:
            continue
        evidence = detect_play_all(
            candidate_file_id=item.media_id,
            candidate_duration_seconds=duration_by_id[item.media_id],
            candidate_size_bytes=payload.get("source_size_bytes"),
            matched_episodes=matched_evidence,
        )
        if evidence is None:
            continue
        play_all_ids.add(item.media_id)
        dossier.record_attempt(
            (item.media_id,),
            branch="tv-play-all",
            disposition="matched",
            summary={
                "component_episode_ids": list(evidence.component_episode_ids),
                "duration_ratio": round(evidence.duration_ratio, 6),
                "size_ratio": (
                    round(evidence.size_ratio, 6)
                    if evidence.size_ratio is not None
                    else None
                ),
                "reason": "matched_episode_coverage_supports_play_all",
            },
        )
        store.choose_review_path(item.media_id, "play_all_aggregate_detected")
    assignments = [
        {
            "title_index": title_index,
            "season": proposed[item.media_id].season,
            "episode": proposed[item.media_id].episode,
            "title": proposed[item.media_id].title,
            "provisional_match": item.media_id in provisional,
            "gemini_confidence": provisional.get(item.media_id),
        }
        for title_index, (item, _payload) in sorted(selected_by_title.items())
        if item.media_id in proposed
    ]
    contract_root.mkdir(parents=True, exist_ok=True)
    applied = []
    for item, payload in selected:
        if item.media_id not in proposed:
            continue
        revised = dict(payload)
        context = dict(revised.get("media_context", {}))
        context.update(
            series_name=series_name.strip(),
            season=season,
            episode_assignments=assignments,
            episode_assignment_source="all-season-analysis",
            identification_policy_version=2,
        )
        revised["media_context"] = context
        path = contract_root / f"{item.media_id}.all-season-{uuid4().hex[:12]}.json"
        path.write_text(
            json.dumps(revised, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        store.apply_reviewed_identification_input(
            item.media_id, build_artifact("rip", path)
        )
        applied.append(item.media_id)
    unresolved = tuple(
        media_id
        for media_id in media_ids
        if media_id not in applied and media_id not in play_all_ids
    )
    if unresolved and allow_content_fallback:
        try:
            from mkv_episode_matcher.backend.gemini_fallback import (
                execute_gemini_fallback,
            )

            for media_id in unresolved:
                store.choose_review_path(media_id, "gemini_analysis_running")
            fallback_applied = execute_gemini_fallback(
                store, unresolved, config, asr, contract_root
            )
            applied.extend(fallback_applied)
        except Exception as exc:
            dossier.record_attempt(
                unresolved,
                branch="gemini-synthesis",
                disposition="failed",
                summary={"reason": type(exc).__name__},
            )
            for media_id in unresolved:
                item = store.get(media_id)
                if item.state == "review_required":
                    store.choose_review_path(
                        media_id, "all_season_sequence_review_required"
                    )
    elif unresolved:
        for media_id in unresolved:
            store.choose_review_path(media_id, "all_season_sequence_review_required")
    return tuple(applied)
