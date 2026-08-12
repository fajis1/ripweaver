"""Disc-level all-season identification for seasonless television rips."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from loguru import logger

from mkv_episode_matcher.backend.identification_dossier import (
    IdentificationDossierStore,
    collect_dossier_evidence,
)
from mkv_episode_matcher.core.credentials import ApiCredentialError, ApiServiceError
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.core.providers.subtitles import OpenSubtitlesProvider
from mkv_episode_matcher.core.tv_identification_policy import (
    AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION,
    AUTOMATIC_TV_MIN_CONFIDENCE,
    GEMINI_TWO_PASS_SOURCE,
    OPENSUBTITLES_RESIDUAL_SOURCE,
    OPENSUBTITLES_TWO_WINDOW_SOURCE,
)
from mkv_episode_matcher.core.utils import SubtitleReader, clean_text
from mkv_episode_matcher.media.episode_catalog import (
    EpisodeCatalogEntry,
    rank_catalog_candidates,
)
from mkv_episode_matcher.media.evidence_bundle import (
    SavedFileEvidence,
    SavedTranscriptWindow,
)
from mkv_episode_matcher.media.gemini_matcher import (
    GeminiEpisodeRanker,
    GeminiMatchError,
    GeminiResponseError,
    UnmatchedFileEvidence,
)
from mkv_episode_matcher.media.gemini_series_resolver import GeminiSeriesResolver
from mkv_episode_matcher.media.play_all_detection import (
    MatchedEpisodeEvidence,
    detect_play_all,
)
from mkv_episode_matcher.media.sequence_matcher import (
    SequenceGroup,
    plan_disc_sequences,
)
from mkv_episode_matcher.media.transcript_batch import TranscriptBatchError
from mkv_episode_matcher.pipeline_queue import (
    PipelineQueueError,
    PipelineQueueStore,
    build_artifact,
)
from mkv_episode_matcher.tmdb_client import (
    TvShowCandidate,
    fetch_aired_episode_catalog,
    search_tv_show_candidates,
)


class GeminiAnalysisError(PipelineQueueError):
    """Credential-free provider diagnostic safe for durable events and logs."""

    def __init__(
        self,
        review_code: str,
        diagnostic: str,
        *,
        proposed_series_name: str | None = None,
        proposed_tmdb_id: int | None = None,
        proposed_confidence: float | None = None,
        proposed_series_names: tuple[str, ...] = (),
    ):
        self.review_code = review_code
        self.diagnostic = diagnostic
        self.proposed_series_name = proposed_series_name
        self.proposed_tmdb_id = proposed_tmdb_id
        self.proposed_confidence = proposed_confidence
        self.proposed_series_names = proposed_series_names
        super().__init__(diagnostic)


@dataclass(frozen=True)
class _ReviewSequencePlan:
    disposition: str = "review"
    score: float = 0.0
    global_margin: float = 0.0
    groups: tuple = ()


@dataclass(frozen=True)
class _DiscEpisodeRangeFence:
    """Candidate boundary inferred from independent matches on one disc."""

    season: int
    minimum_episode: int
    maximum_episode: int
    disc_title_count: int
    anchor_episode_ids: tuple[str, ...]

    def permits(self, candidate: EpisodeCatalogEntry) -> bool:
        return (
            candidate.season == self.season
            and self.minimum_episode <= candidate.episode <= self.maximum_episode
        )

    @property
    def scope(self) -> str:
        return (
            f"S{self.season:02d}E{self.minimum_episode:02d}-E{self.maximum_episode:02d}"
        )


_DISC_RANGE_HISTORY_EVIDENCE = frozenset({
    OPENSUBTITLES_TWO_WINDOW_SOURCE,
    OPENSUBTITLES_RESIDUAL_SOURCE,
})
_DISC_RANGE_HISTORY_MATCH_SOURCES = frozenset({
    "deterministic",
    "manual_playback",
    "server_assisted",
})


def _build_disc_episode_range_fence(
    anchors: tuple[EpisodeCatalogEntry, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    disc_title_count: int,
) -> _DiscEpisodeRangeFence | None:
    """Bound ambiguous candidates without treating title order as evidence.

    At least two independently assigned episodes from one season must fit in a
    contiguous window no wider than the number of titles known for the disc.
    The fence admits every window that can contain those anchors, so it cannot
    choose an episode or assume that MakeMKV title order is episode order.
    """

    unique = {entry.episode_id: entry for entry in anchors}
    if len(unique) < 2 or disc_title_count < len(unique):
        return None
    seasons = {entry.season for entry in unique.values()}
    if len(seasons) != 1:
        return None
    season = seasons.pop()
    episodes = tuple(entry.episode for entry in unique.values())
    anchor_minimum = min(episodes)
    anchor_maximum = max(episodes)
    if anchor_maximum - anchor_minimum + 1 > disc_title_count:
        return None
    season_maximum = max(
        (entry.episode for entry in catalog if entry.season == season), default=0
    )
    minimum = max(1, anchor_maximum - disc_title_count + 1)
    maximum = min(season_maximum, anchor_minimum + disc_title_count - 1)
    if season_maximum <= 0 or minimum > maximum:
        return None
    return _DiscEpisodeRangeFence(
        season=season,
        minimum_episode=minimum,
        maximum_episode=maximum,
        disc_title_count=disc_title_count,
        anchor_episode_ids=tuple(
            sorted(
                unique,
                key=lambda episode_id: (
                    unique[episode_id].season,
                    unique[episode_id].episode,
                ),
            )
        ),
    )


def _disc_title_count(
    store: PipelineQueueStore,
    disc_fingerprint: str,
    selected: list[tuple[object, dict]],
) -> int:
    expected = store.expected_title_indexes_for_disc(disc_fingerprint)
    observed = {
        int(payload["title_index"])
        for _item, payload in selected
        if isinstance(payload.get("title_index"), int)
        and not isinstance(payload.get("title_index"), bool)
    }
    observed.update(store.catalogue_title_history(disc_fingerprint))
    return max(len(expected), len(observed), len(selected))


def _history_disc_range_anchors(
    store: PipelineQueueStore,
    disc_fingerprint: str,
    catalog_by_id: Mapping[str, EpisodeCatalogEntry],
) -> tuple[EpisodeCatalogEntry, ...]:
    """Use only provenance-bearing prior outcomes; never legacy sequence history."""

    anchors = []
    for outcome in store.catalogue_title_history(disc_fingerprint).values():
        episode_id = outcome.get("episode_id")
        evidence_source = outcome.get("assignment_evidence_source")
        policy_version = outcome.get("identification_policy_version")
        match_source = outcome.get("match_source")
        if not isinstance(episode_id, str) or (
            not (
                evidence_source in _DISC_RANGE_HISTORY_EVIDENCE
                and policy_version == AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION
            )
            and match_source not in _DISC_RANGE_HISTORY_MATCH_SOURCES
        ):
            continue
        candidate = catalog_by_id.get(episode_id.upper())
        if candidate is not None:
            anchors.append(candidate)
    return tuple(anchors)


def _anchor_titles(
    selected: list[tuple[object, dict]],
) -> tuple[tuple[object, dict], ...]:
    """Choose bounded, spread-out titles for initial season discovery."""

    if len(selected) <= 3:
        return tuple(selected)
    indexes = (0, len(selected) // 2, len(selected) - 1)
    return tuple(selected[index] for index in dict.fromkeys(indexes))


def dominant_season_scope(
    assigned: frozenset[tuple[int, int]],
    catalog: tuple[EpisodeCatalogEntry, ...],
) -> tuple[int, ...]:
    """Prefer a dominant season and order neighbors from episode position."""

    counts = Counter(season for season, _episode in assigned)
    if not counts:
        return ()
    ranked = counts.most_common()
    dominant, count = ranked[0]
    if count < 2 or (len(ranked) > 1 and ranked[1][1] == count):
        return ()
    dominant_episodes = [episode for season, episode in assigned if season == dominant]
    return _ordered_neighbor_scope(dominant, tuple(dominant_episodes), catalog)


def _subtitle_reference_windows(content: str, excerpt: str) -> tuple[str, ...]:
    words = clean_text(re.sub(r"<[^>]+>", " ", content)).split()
    excerpt_words = max(8, len(clean_text(excerpt).split()))
    # Keep enough surrounding reference dialogue to tolerate ASR omissions,
    # without diluting a short anchor inside a window twice its size.  These
    # windows deliberately ignore subtitle timestamps: extended/reconstructed
    # cuts can insert minutes of dialogue before an otherwise unchanged scene.
    width = max(24, excerpt_words + max(8, excerpt_words // 2))
    step = max(8, width // 3)
    if len(words) <= width:
        return (" ".join(words),) if words else ()
    starts = range(0, len(words) - width + 1, step)
    windows = [" ".join(words[start : start + width]) for start in starts]
    windows.append(" ".join(words[-width:]))
    windows = list(dict.fromkeys(windows))
    maximum_windows = 96
    if len(windows) <= maximum_windows:
        return tuple(windows)

    # Keep fuzzy comparisons bounded for malformed or unusually long subtitle
    # files.  Lexical overlap is only a cheap shortlist; the ASR model's fuzzy
    # scorer still makes the actual decision.
    query = clean_text(excerpt).split()
    query_tokens = set(query)
    query_pairs = set(zip(query, query[1:], strict=False))

    def lexical_rank(value: tuple[int, str]) -> tuple[int, int, int]:
        index, window = value
        tokens = window.split()
        pairs = set(zip(tokens, tokens[1:], strict=False))
        return (
            len(query_pairs & pairs),
            len(query_tokens & set(tokens)),
            -index,
        )

    shortlisted = sorted(enumerate(windows), key=lexical_rank, reverse=True)[
        :maximum_windows
    ]
    return tuple(window for _index, window in shortlisted)


def _score_subtitle(
    asr,
    excerpts: tuple[str, ...],
    content: str,
    duration_seconds: float,
) -> tuple[float, tuple[float, ...]]:
    """Score independent dialogue anchors against one regular-edition subtitle.

    Timestamp-near matches remain useful for ordinary cuts, but every excerpt
    also receives a bounded whole-subtitle search.  That makes regular episode
    subtitles valid references for extended editions: unchanged scenes vote
    for the episode wherever they moved, while inserted-scene excerpts simply
    remain below the qualifying threshold and do not lower consensus.
    """

    scores = []
    starts = tuple(
        max(0.0, duration_seconds * position / (len(excerpts) + 1) - 15.0)
        for position in range(1, len(excerpts) + 1)
    )
    for excerpt, start in zip(excerpts, starts, strict=False):
        cleaned = clean_text(excerpt)
        if not cleaned:
            continue
        aligned = []
        for offset in (-60.0, -30.0, 0.0, 30.0, 60.0):
            window_start = max(0.0, start + offset)
            reference = clean_text(
                " ".join(
                    SubtitleReader.extract_subtitle_chunk(
                        content, window_start, window_start + 45.0
                    )
                )
            )
            if reference:
                aligned.append(asr.calculate_match_score(cleaned, reference))
        # Always search the regular subtitle globally as well.  A reconstructed
        # cut may still have valid subtitle text at the calculated timestamp,
        # but it can be a completely different scene after earlier insertions.
        windows = _subtitle_reference_windows(content, cleaned)
        global_score = (
            max(asr.calculate_match_score(cleaned, window) for window in windows)
            if windows
            else 0.0
        )
        if aligned or global_score:
            scores.append(max((*aligned, global_score)))
    if not scores:
        return 0.0, ()
    scores.sort(reverse=True)
    return sum(scores) / len(scores), tuple(scores)


def _subtitle_candidates(
    files,
    references,
    catalog_by_number,
    asr,
    *,
    min_confidence,
    rejected_candidates=None,
    phase="direct",
):
    scored = {}
    for item in files:
        candidates = []
        for subtitle in references:
            info = subtitle.episode_info
            if info is None:
                continue
            entry = catalog_by_number.get((info.season, info.episode))
            if entry is None:
                if rejected_candidates is not None:
                    rejected_candidates.setdefault(item.file_id, []).append({
                        "phase": phase,
                        "candidate_episode_id": f"S{info.season:02d}E{info.episode:02d}",
                        "candidate_episode_title": None,
                        "season": info.season,
                        "episode": info.episode,
                        "disposition": "rejected",
                        "reason": "episode_not_in_aired_catalogue",
                    })
                continue
            if not _episode_runtime_consistent(item.duration_seconds, entry):
                if rejected_candidates is not None:
                    rejected_candidates.setdefault(item.file_id, []).append({
                        "phase": phase,
                        "candidate_episode_id": entry.episode_id,
                        "candidate_episode_title": entry.title,
                        "season": entry.season,
                        "episode": entry.episode,
                        "runtime_consistent": False,
                        "disposition": "rejected",
                        "reason": "runtime_mismatch",
                    })
                continue
            try:
                content = subtitle.content or SubtitleReader.read_srt_file(
                    subtitle.path
                )
            except (OSError, ValueError):
                if rejected_candidates is not None:
                    rejected_candidates.setdefault(item.file_id, []).append({
                        "phase": phase,
                        "candidate_episode_id": entry.episode_id,
                        "candidate_episode_title": entry.title,
                        "season": entry.season,
                        "episode": entry.episode,
                        "runtime_consistent": True,
                        "disposition": "rejected",
                        "reason": "subtitle_reference_unreadable",
                    })
                continue
            average_score, window_scores = _score_subtitle(
                asr,
                item.transcript_excerpts,
                content,
                item.duration_seconds,
            )
            qualifying = tuple(
                score for score in window_scores if score >= min_confidence
            )
            consensus_score = (
                sum(qualifying) / len(qualifying) if qualifying else average_score
            )
            candidates.append((
                consensus_score,
                len(qualifying),
                entry,
            ))
        scored[item.file_id] = sorted(
            candidates, key=lambda value: value[0], reverse=True
        )
    return scored


def _missing_subtitle_reference_audit(
    files,
    references,
    catalog,
    season,
    *,
    phase,
):
    """Record aired options that could not be scored for lack of a reference."""

    referenced = {
        (subtitle.episode_info.season, subtitle.episode_info.episode)
        for subtitle in references
        if subtitle.episode_info is not None
    }
    missing = [
        entry
        for entry in catalog
        if entry.season == season and (entry.season, entry.episode) not in referenced
    ]
    return {
        item.file_id: [
            {
                "phase": phase,
                "candidate_episode_id": entry.episode_id,
                "candidate_episode_title": entry.title,
                "season": entry.season,
                "episode": entry.episode,
                "runtime_consistent": _episode_runtime_consistent(
                    item.duration_seconds, entry
                ),
                "disposition": "rejected",
                "reason": (
                    "subtitle_reference_unavailable"
                    if _episode_runtime_consistent(item.duration_seconds, entry)
                    else "runtime_mismatch"
                ),
            }
            for entry in missing
        ]
        for item in files
        if missing
    }


def _scored_candidate_audit(
    scored,
    accepted,
    diagnostic_details,
    *,
    phase,
):
    """Explain the disposition of every scored episode candidate."""

    audit = {}
    for file_id, ranked in scored.items():
        details = diagnostic_details.get(file_id, {})
        selected = accepted.get(file_id)
        records = []
        for rank, (score, qualifying_windows, entry) in enumerate(ranked, start=1):
            is_selected = (
                selected is not None and entry.episode_id == selected.episode_id
            )
            reason = (
                str(details.get("reason", "accepted"))
                if is_selected
                else str(details.get("reason", "candidate_rejected"))
                if rank == 1
                else "lower_ranked_candidate"
            )
            records.append({
                "phase": phase,
                "rank": rank,
                "candidate_episode_id": entry.episode_id,
                "candidate_episode_title": entry.title,
                "season": entry.season,
                "episode": entry.episode,
                "score": round(float(score), 6),
                "qualifying_window_count": int(qualifying_windows),
                "runtime_consistent": True,
                "disposition": "selected" if is_selected else "rejected",
                "reason": reason,
            })
        audit[file_id] = records
    return audit


def _subtitle_candidate_rejection_reason(
    best_score: float,
    best_votes: int,
    margin: float,
    min_confidence: float,
) -> str | None:
    if best_score < min_confidence:
        return "below_confidence_threshold"
    if best_votes < 2:
        return "insufficient_qualifying_windows"
    if margin < 0.08:
        return "ambiguous_runner_up"
    return None


def _accept_subtitle_candidates(
    scored, min_confidence, used, *, diagnostic_details=None
):
    proposals = []
    diagnostics = {}
    for file_id, ranked in scored.items():
        if not ranked:
            continue
        best_score, best_votes, best = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        margin = best_score - runner_up
        diagnostics[file_id] = (best_score, margin)
        rejection_reason = _subtitle_candidate_rejection_reason(
            best_score, best_votes, margin, min_confidence
        )
        if diagnostic_details is not None:
            diagnostic_details[file_id] = {
                "best_score": best_score,
                "runner_up_score": runner_up,
                "margin": margin,
                "qualifying_window_count": best_votes,
                "candidate_episode_id": best.episode_id,
                "reason": rejection_reason or "candidate_pending",
            }
        if best_score >= min_confidence and best_votes >= 2 and margin >= 0.08:
            proposals.append((best_score, margin, file_id, best))
    accepted = {}
    for _score, _margin, file_id, entry in sorted(proposals, reverse=True):
        if entry.episode_id in used:
            if diagnostic_details is not None:
                diagnostic_details[file_id]["reason"] = "episode_already_assigned"
            continue
        accepted[file_id] = entry
        used.add(entry.episode_id)
        if diagnostic_details is not None:
            diagnostic_details[file_id]["reason"] = "accepted"
    return accepted, diagnostics


def _accept_residual_subtitle_candidates(
    scored: dict[str, list[tuple[float, int, EpisodeCatalogEntry]]],
    min_confidence: float,
    used: set[str],
    *,
    diagnostic_details: dict[str, dict[str, object]] | None = None,
) -> dict[str, EpisodeCatalogEntry]:
    """Resolve strong leftovers only after confident one-to-one assignments."""

    proposals = []
    for file_id, ranked in scored.items():
        remaining = [
            candidate for candidate in ranked if candidate[2].episode_id not in used
        ]
        if not remaining:
            if diagnostic_details is not None:
                diagnostic_details[file_id] = {
                    "best_score": 0.0,
                    "runner_up_score": 0.0,
                    "margin": 0.0,
                    "qualifying_window_count": 0,
                    "candidate_episode_id": None,
                    "reason": "all_candidates_already_assigned",
                }
            continue
        best_score, best_votes, best = remaining[0]
        runner_up = remaining[1][0] if len(remaining) > 1 else 0.0
        margin = best_score - runner_up
        reason = (
            "below_confidence_threshold"
            if best_score < min_confidence
            else "insufficient_qualifying_windows"
            if best_votes < 1
            else "residual_margin_below_threshold"
            if margin < 0.25
            else "candidate_pending"
        )
        if diagnostic_details is not None:
            diagnostic_details[file_id] = {
                "best_score": best_score,
                "runner_up_score": runner_up,
                "margin": margin,
                "qualifying_window_count": best_votes,
                "candidate_episode_id": best.episode_id,
                "reason": reason,
            }
        if reason == "candidate_pending":
            proposals.append((best_score, margin, best_votes, file_id, best))
    accepted = {}
    for score, margin, best_votes, file_id, entry in sorted(proposals, reverse=True):
        if entry.episode_id in used:
            continue
        accepted[file_id] = entry
        used.add(entry.episode_id)
        if diagnostic_details is not None:
            diagnostic_details[file_id] = {
                "best_score": score,
                "runner_up_score": score - margin,
                "margin": margin,
                "qualifying_window_count": best_votes,
                "candidate_episode_id": entry.episode_id,
                "reason": "accepted_after_disc_residual_reduction",
            }
    return accepted


def _ordered_neighbor_scope(
    season: int,
    episodes: tuple[int, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
) -> tuple[int, ...]:
    available = {entry.season for entry in catalog}
    episode_count = max(
        (entry.episode for entry in catalog if entry.season == season), default=0
    )
    position = (
        sum(episodes) / len(episodes) / episode_count
        if episodes and episode_count
        else 0.5
    )
    neighbor = season - 1 if position <= 0.35 else season + 1
    return (season, neighbor) if neighbor in available else (season,)


def discover_opensubtitles_season(  # noqa: C901 - season discovery audit ledger
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    series_name: str,
    tmdb_id: int,
    asr,
    *,
    min_confidence: float,
    provider,
    reference_cache: dict[int, tuple] | None = None,
    season_order: tuple[int, ...] | None = None,
    candidate_evaluations: dict[str, list[dict[str, object]]] | None = None,
    audit_phase: str = "season-discovery",
) -> tuple[int, ...]:
    """Discover a season from independent transcript matches across the series."""

    usable = tuple(item for item in files if item.transcript_excerpts)
    if not usable:
        return ()
    median_duration = sorted(item.duration_seconds for item in usable)[len(usable) // 2]
    representatives = tuple(
        sorted(
            usable,
            key=lambda item: abs(item.duration_seconds - median_duration),
        )[:4]
    )
    catalog_by_number = {(entry.season, entry.episode): entry for entry in catalog}
    season_results = []
    reference_cache = reference_cache if reference_cache is not None else {}
    available_seasons = {entry.season for entry in catalog}
    preferred = season_order or tuple(sorted(available_seasons))
    ordered_seasons = tuple(
        season for season in preferred if season in available_seasons
    ) + tuple(sorted(available_seasons - set(preferred)))
    for season in ordered_seasons:
        references = reference_cache.get(season)
        if references is None:
            references = tuple(provider.get_subtitles(series_name, season, [], tmdb_id))
            reference_cache[season] = references
        rejected_candidates = _missing_subtitle_reference_audit(
            representatives,
            references,
            catalog,
            season,
            phase=audit_phase,
        )
        scored = _subtitle_candidates(
            representatives,
            references,
            catalog_by_number,
            asr,
            min_confidence=min_confidence,
            rejected_candidates=rejected_candidates,
            phase=audit_phase,
        )
        details: dict[str, dict[str, object]] = {}
        accepted, diagnostics = _accept_subtitle_candidates(
            scored, min_confidence, set(), diagnostic_details=details
        )
        if candidate_evaluations is not None:
            scored_audit = _scored_candidate_audit(
                scored, accepted, details, phase=audit_phase
            )
            for records in scored_audit.values():
                for record in records:
                    if record["disposition"] == "selected":
                        record["disposition"] = "review"
                        record["reason"] = "season_discovery_seed_only"
            for file_id in {**rejected_candidates, **scored_audit}:
                candidate_evaluations.setdefault(file_id, []).extend(
                    rejected_candidates.get(file_id, [])
                )
                candidate_evaluations[file_id].extend(scored_audit.get(file_id, []))
        confidence_sum = sum(diagnostics[file_id][0] for file_id in accepted)
        margin_sum = sum(diagnostics[file_id][1] for file_id in accepted)
        season_results.append((
            len(accepted),
            confidence_sum,
            margin_sum,
            season,
            accepted,
        ))
        if len(accepted) >= 2:
            episodes = tuple(entry.episode for entry in accepted.values())
            return _ordered_neighbor_scope(season, episodes, catalog)
    season_results.sort(reverse=True)
    if not season_results or season_results[0][0] == 0:
        return ()
    winner = season_results[0]
    runner_up = season_results[1] if len(season_results) > 1 else None
    if runner_up is not None and winner[:3] == runner_up[:3]:
        return ()
    # A single seed must be exceptionally strong; multiple independent seeds
    # may establish the season at the normal configured threshold.
    if winner[0] == 1 and winner[1] < max(0.82, min_confidence + 0.08):
        return ()
    episodes = tuple(entry.episode for entry in winner[4].values())
    return _ordered_neighbor_scope(winner[3], episodes, catalog)


def match_opensubtitles_seasons(  # noqa: C901 - direct and residual audit ledger
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    series_name: str,
    tmdb_id: int,
    seasons: tuple[int, ...],
    asr,
    *,
    min_confidence: float,
    provider_factory=OpenSubtitlesProvider,
    provider=None,
    reference_cache: dict[int, tuple] | None = None,
    diagnostic_details: dict[str, dict[str, object]] | None = None,
    prior_disc_assignments: bool = False,
    candidate_evaluations: dict[str, list[dict[str, object]]] | None = None,
) -> tuple[dict[str, EpisodeCatalogEntry], dict[str, tuple[float, float]]]:
    """Match cached Whisper excerpts independently against bounded season subtitles."""

    if not seasons or not files:
        return {}, {}
    provider = provider or provider_factory()
    reference_cache = reference_cache if reference_cache is not None else {}
    catalog_by_number = {(entry.season, entry.episode): entry for entry in catalog}
    remaining = {item.file_id: item for item in files}
    accepted = {}
    diagnostics = {}
    used = set()
    residual_scored: dict[str, list[tuple[float, int, EpisodeCatalogEntry]]] = {}
    for season in seasons:
        if not remaining:
            break
        references = reference_cache.get(season)
        if references is None:
            references = tuple(provider.get_subtitles(series_name, season, [], tmdb_id))
            reference_cache[season] = references
        rejected_candidates = _missing_subtitle_reference_audit(
            tuple(remaining.values()),
            references,
            catalog,
            season,
            phase="direct",
        )
        scored = _subtitle_candidates(
            tuple(remaining.values()),
            references,
            catalog_by_number,
            asr,
            min_confidence=min_confidence,
            rejected_candidates=rejected_candidates,
            phase="direct",
        )
        for file_id, candidates in scored.items():
            residual_scored.setdefault(file_id, []).extend(candidates)
            residual_scored[file_id].sort(key=lambda value: value[0], reverse=True)
        season_matches, season_diagnostics = _accept_subtitle_candidates(
            scored,
            min_confidence,
            used,
            diagnostic_details=diagnostic_details,
        )
        accepted.update(season_matches)
        diagnostics.update(season_diagnostics)
        if candidate_evaluations is not None:
            details = diagnostic_details or {}
            scored_audit = _scored_candidate_audit(
                scored, season_matches, details, phase="direct"
            )
            for file_id in {**rejected_candidates, **scored_audit}:
                candidate_evaluations.setdefault(file_id, []).extend(
                    rejected_candidates.get(file_id, [])
                )
                candidate_evaluations[file_id].extend(scored_audit.get(file_id, []))
        for file_id in season_matches:
            remaining.pop(file_id, None)
    residual_details = diagnostic_details if diagnostic_details is not None else {}
    used_before_residual = set(used)
    residual_matches = (
        _accept_residual_subtitle_candidates(
            {file_id: residual_scored.get(file_id, []) for file_id in remaining},
            min_confidence,
            used,
            diagnostic_details=residual_details,
        )
        if used or prior_disc_assignments
        else {}
    )
    accepted.update(residual_matches)
    if candidate_evaluations is not None and (used or prior_disc_assignments):
        residual_candidates = {
            file_id: [
                candidate
                for candidate in residual_scored.get(file_id, [])
                if candidate[2].episode_id not in used_before_residual
            ]
            for file_id in remaining
        }
        residual_audit = _scored_candidate_audit(
            residual_candidates,
            residual_matches,
            residual_details,
            phase="residual-elimination",
        )
        for file_id in remaining:
            eliminated = tuple(
                {
                    "phase": "residual-elimination",
                    "candidate_episode_id": entry.episode_id,
                    "candidate_episode_title": entry.title,
                    "season": entry.season,
                    "episode": entry.episode,
                    "score": round(float(score), 6),
                    "qualifying_window_count": int(votes),
                    "runtime_consistent": True,
                    "disposition": "rejected",
                    "reason": "episode_already_assigned_to_other_title",
                }
                for score, votes, entry in residual_scored.get(file_id, [])
                if entry.episode_id in used_before_residual
            )
            candidate_evaluations.setdefault(file_id, []).extend(
                residual_audit.get(file_id, [])
            )
            candidate_evaluations[file_id].extend(eliminated)
    for file_id in remaining:
        details = residual_details.get(file_id, {})
        score = float(details.get("best_score", 0.0))
        diagnostics[file_id] = (score, float(details.get("margin", 0.0)))
    return accepted, diagnostics


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


def preferred_season_order(
    catalog: tuple[EpisodeCatalogEntry, ...],
    known_episodes: frozenset[tuple[int, int]],
) -> tuple[int, ...]:
    """Try unseen seasons beyond the known frontier before represented seasons."""

    seasons = sorted({entry.season for entry in catalog})
    if not known_episodes:
        return tuple(seasons)
    known_seasons = {season for season, _episode in known_episodes}
    frontier = max(known_seasons)
    return tuple(
        sorted(
            seasons,
            key=lambda season: (
                season in known_seasons,
                abs(season - (frontier + 1)),
                season,
            ),
        )
    )


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


def _gemini_chunks(
    files: tuple[UnmatchedFileEvidence, ...], size: int = 4
) -> tuple[tuple[UnmatchedFileEvidence, ...], ...]:
    return tuple(files[index : index + size] for index in range(0, len(files), size))


def _shortlist_catalog(
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    excluded: set[str],
    *,
    top_k: int = 10,
    reviewer_scene_descriptions: Mapping[str, str] | None = None,
) -> tuple[EpisodeCatalogEntry, ...]:
    selected: set[str] = set()
    available = tuple(entry for entry in catalog if entry.episode_id not in excluded)
    for item in files:
        review_note = (reviewer_scene_descriptions or {}).get(item.file_id)
        excerpts = item.transcript_excerpts + ((review_note,) if review_note else ())
        ranked = rank_catalog_candidates(
            excerpts,
            item.duration_seconds,
            available,
            top_k=min(top_k, len(available)),
        )
        selected.update(candidate.episode_id for candidate in ranked)
    return tuple(entry for entry in catalog if entry.episode_id in selected)


def _rank_gemini_chunks(
    ranker: GeminiEpisodeRanker,
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    dossier: IdentificationDossierStore,
    known_existing_ids: frozenset[str],
    reviewer_scene_descriptions: Mapping[str, str] | None = None,
    *,
    analysis_run_id: str | None = None,
    phase: str = "gemini",
) -> dict[str, object]:
    matches: dict[str, object] = {}
    assigned: set[str] = set()
    for chunk in _gemini_chunks(files):
        chunk_notes = {
            item.file_id: reviewer_scene_descriptions[item.file_id]
            for item in chunk
            if reviewer_scene_descriptions
            and item.file_id in reviewer_scene_descriptions
        }
        chunk_catalog = _shortlist_catalog(
            chunk,
            catalog,
            assigned,
            reviewer_scene_descriptions=chunk_notes,
        )
        if not chunk_catalog:
            raise PipelineQueueError("Local Gemini candidate shortlist is empty")
        chunk_ids = {entry.episode_id for entry in chunk_catalog}
        kwargs = {
            "prior_attempts": {
                item.file_id: dossier.safe_attempts(item.file_id) for item in chunk
            },
            "existing_episode_ids": frozenset(
                episode_id
                for episode_id in known_existing_ids
                if episode_id in chunk_ids
            ),
        }
        if chunk_notes:
            kwargs["reviewer_scene_descriptions"] = chunk_notes
        review = ranker.rank_with_configured_keys(chunk, chunk_catalog, **kwargs)
        for match in review.matches:
            matches[match.file_id] = match
            if analysis_run_id is not None:
                _record_candidate_audit_safely(
                    dossier,
                    match.file_id,
                    analysis_run_id=analysis_run_id,
                    branch="tv-gemini",
                    evaluations=tuple(
                        {
                            "phase": phase,
                            "candidate_ordinal": candidate_ordinal,
                            "candidate_episode_id": entry.episode_id,
                            "candidate_episode_title": entry.title,
                            "season": entry.season,
                            "episode": entry.episode,
                            "disposition": (
                                "selected"
                                if entry.episode_id == match.episode_id
                                else "rejected"
                            ),
                            "reason": (
                                "gemini_selected_candidate"
                                if entry.episode_id == match.episode_id
                                else "gemini_returned_no_episode"
                                if match.episode_id is None
                                else "gemini_selected_different_candidate"
                            ),
                        }
                        for candidate_ordinal, entry in enumerate(
                            chunk_catalog, start=1
                        )
                    ),
                )
            if match.episode_id is not None:
                assigned.add(match.episode_id)
    return matches


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
    if isinstance(exc, GeminiMatchError):
        return GeminiAnalysisError("gemini_analysis_failed", str(exc))
    if isinstance(exc, PipelineQueueError):
        return GeminiAnalysisError("gemini_response_invalid", str(exc))
    return GeminiAnalysisError(
        "gemini_provider_failed",
        f"Gemini provider failure: {type(exc).__name__}",
    )


def _normalized_series(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _exact_series_candidates(
    series_name: str, candidates: tuple[TvShowCandidate, ...]
) -> tuple[TvShowCandidate, ...]:
    expected = _normalized_series(series_name)
    return tuple(
        item
        for item in candidates
        if expected
        in {_normalized_series(item.name), _normalized_series(item.original_name)}
    )


def _has_packaging_series_markers(series_name: str) -> bool:
    """Return whether an otherwise exact name still looks like disc packaging."""

    normalized = re.sub(r"[_-]+", " ", series_name)
    return (
        re.search(
            r"\b(?:disc|disk|dvd|volume|vol|season)\s*\d{1,3}\b"
            r"|\b(?:csr\s+dim|superfan\s+episodes?)\b",
            normalized,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _canonical_series_query(series_name: str) -> str:
    """Remove common optical-release markers before the first TMDb search."""

    value = re.sub(r"[_-]+", " ", series_name)
    value = re.sub(r"(?i)\b(?:the\s+complete\s+series|complete\s+series)\b", " ", value)
    value = re.sub(
        r"(?i)\b(?:superfan(?:\s+episodes?)?|extended\s+episodes?)\b", " ", value
    )
    value = re.sub(r"(?i)\bcsr\s+dim\s*\d+\b", " ", value)
    value = re.sub(
        r"(?i)\b(?:disc|disk|dvd|volume|vol|season)\s*(?:\d{1,3}|[ivxlcdm]+)\b",
        " ",
        value,
    )
    value = re.sub(r"(?i)\bs\d{1,3}\s*(?:d|disc|disk)\s*\d{1,3}\b", " ", value)
    # A standalone S2 is a common season marker. A standalone D2 may be an
    # actual title (for example, "D2: The Mighty Ducks"), so remove D-number
    # shorthand only through the contextual S2 D2 rule above.
    value = re.sub(r"(?i)\bs\d{1,3}\b", " ", value)
    return " ".join(value.split()).strip(" .-_–—")


def _initial_series_candidates(series_name: str) -> tuple[TvShowCandidate, ...]:
    """Search cleaned and full labels, preserving a bounded unique result set."""

    cleaned = _canonical_series_query(series_name)
    queries = tuple(dict.fromkeys(value for value in (cleaned, series_name) if value))
    candidates: list[TvShowCandidate] = []
    seen_ids: set[int] = set()
    for query in queries:
        for candidate in search_tv_show_candidates(query):
            if candidate.tmdb_id in seen_ids:
                continue
            seen_ids.add(candidate.tmdb_id)
            candidates.append(candidate)
            if len(candidates) == 12:
                return tuple(candidates)
    return tuple(candidates)


def _resolve_series_with_gemini(
    series_name: str,
    candidates: tuple[TvShowCandidate, ...],
    config: Config,
) -> TvShowCandidate:
    """Resolve a packaging label and validate the answer back through TMDb."""

    try:
        resolution = GeminiSeriesResolver(
            model=config.gemini_model
        ).resolve_with_configured_keys(series_name, candidates)
    except Exception as exc:
        raise classify_gemini_failure(exc) from exc
    if resolution.confidence < config.min_confidence:
        raise GeminiAnalysisError(
            "gemini_series_resolution_uncertain",
            "Gemini series resolution did not meet the confidence threshold",
            proposed_series_name=resolution.series_name,
            proposed_tmdb_id=resolution.tmdb_id,
            proposed_confidence=resolution.confidence,
            proposed_series_names=(
                resolution.series_name,
                *resolution.alternative_series_names,
            ),
        )
    if resolution.tmdb_id is not None:
        selected = next(
            (item for item in candidates if item.tmdb_id == resolution.tmdb_id),
            None,
        )
        if selected is not None:
            return selected

    # A disc label such as "THE OFFICE SUPERFAN EPISODES S1" may return one
    # plausible but catalogue-less search result.  Validate Gemini's canonical
    # series name with a fresh TMDb search instead of trusting invented metadata.
    ranked_names = (resolution.series_name, *resolution.alternative_series_names)
    for rank, proposed_name in enumerate(ranked_names):
        validated = search_tv_show_candidates(proposed_name)
        if rank == 0 and resolution.tmdb_id is not None:
            by_id = next(
                (item for item in validated if item.tmdb_id == resolution.tmdb_id),
                None,
            )
            if by_id is not None:
                return by_id
        validated_exact = _exact_series_candidates(proposed_name, validated)
        if len(validated_exact) == 1:
            return validated_exact[0]
    raise GeminiAnalysisError(
        "gemini_series_resolution_uncertain",
        "Gemini's proposed series name could not be validated through TMDb",
        proposed_series_name=resolution.series_name,
        proposed_tmdb_id=resolution.tmdb_id,
        proposed_confidence=resolution.confidence,
        proposed_series_names=ranked_names,
    )


def _resolve_series_catalog_details(
    series_name: str,
    config: Config,
    *,
    allow_gemini: bool,
) -> tuple[TvShowCandidate, tuple[EpisodeCatalogEntry, ...]]:
    """Resolve one canonical TV series and require a valid TMDb catalogue."""

    candidates = _initial_series_candidates(series_name)
    canonical_query = _canonical_series_query(series_name)
    exact = _exact_series_candidates(canonical_query or series_name, candidates)
    exact_is_canonical = len(exact) == 1 and not _has_packaging_series_markers(
        canonical_query or series_name
    )
    selected = exact[0] if exact_is_canonical else None
    if selected is None and allow_gemini:
        selected = _resolve_series_with_gemini(series_name, candidates, config)
    if selected is None and len(candidates) == 1:
        # When external fallback is disabled, preserve the prior conservative
        # single-result behavior. Automatic Gemini-enabled runs always review
        # this inexact label before reaching this branch.
        selected = candidates[0]
    if selected is None:
        raise PipelineQueueError("No TV series matched the reviewed series name")
    catalog = tuple(fetch_aired_episode_catalog(selected.tmdb_id) or ())
    if not catalog and allow_gemini:
        canonical = _resolve_series_with_gemini(series_name, candidates, config)
        if canonical is not None and canonical.tmdb_id != selected.tmdb_id:
            selected = canonical
            catalog = tuple(fetch_aired_episode_catalog(selected.tmdb_id) or ())
    if not catalog:
        raise PipelineQueueError(
            "TMDb returned no aired episodes for the resolved TV series"
        )
    return selected, catalog


def resolve_series_catalog(
    series_name: str,
    config: Config,
    *,
    allow_gemini: bool,
) -> tuple[str, tuple[EpisodeCatalogEntry, ...]]:
    selected, catalog = _resolve_series_catalog_details(
        series_name, config, allow_gemini=allow_gemini
    )
    return selected.name, catalog


def _selected_analysis_items(
    store: PipelineQueueStore, disc_fingerprint: str
) -> list[tuple[object, dict]]:
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
    return [selected_by_title[index] for index in sorted(selected_by_title)]


def _matching_failure_diagnostic(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, GeminiAnalysisError):
        return "gemini", exc.review_code
    message = str(exc).lower()
    if "at least one held title" in message:
        return "selection", "insufficient_held_titles"
    if "independent episode evidence" in message:
        return "analysis", "independent_episode_evidence_required"
    if "catalogue" in message or "tv series" in message or "tmdb" in message:
        return "catalogue", "catalogue_unavailable"
    if "transcript" in message or "audio evidence" in message or "source" in message:
        return "evidence", "evidence_unavailable"
    if "opensubtitles" in message:
        return "opensubtitles", "opensubtitles_failed"
    if "sequence" in message:
        return "local-sequence", "sequence_analysis_failed"
    return "analysis", type(exc).__name__


def _record_matching_performance_safely(
    store: PipelineQueueStore, **record: object
) -> None:
    try:
        store.record_matching_performance(**record)
    except Exception as exc:  # Telemetry must never stop media processing.
        logger.warning(
            "Matching telemetry was not persisted safely: {}", type(exc).__name__
        )


def _record_candidate_audit_safely(
    dossier: IdentificationDossierStore,
    media_id: str,
    *,
    analysis_run_id: str,
    branch: str,
    evaluations: tuple[dict[str, object], ...],
) -> None:
    try:
        dossier.record_candidate_evaluations(
            media_id,
            analysis_run_id=analysis_run_id,
            branch=branch,
            evaluations=evaluations,
        )
    except Exception as exc:  # Audit persistence must never stop identification.
        logger.warning(
            "Candidate audit was not persisted safely: {}", type(exc).__name__
        )


def _record_workflow_audit_safely(
    dossier: IdentificationDossierStore,
    media_ids: tuple[str, ...],
    *,
    analysis_run_id: str,
    phase: str,
    disposition: str,
    summary: dict[str, object],
) -> None:
    try:
        dossier.record_workflow_event(
            media_ids,
            analysis_run_id=analysis_run_id,
            phase=phase,
            disposition=disposition,
            summary=summary,
        )
    except Exception as exc:  # Audit persistence must never stop identification.
        logger.warning(
            "Workflow audit was not persisted safely: {}", type(exc).__name__
        )


def _matching_provider_branches(
    dossier: IdentificationDossierStore, media_ids: tuple[str, ...]
) -> tuple[str, ...]:
    branches = []
    for media_id in media_ids:
        try:
            attempts = dossier.safe_attempts(media_id)
        except PipelineQueueError:
            continue
        branches.extend(str(attempt["branch"]) for attempt in attempts)
    return tuple(dict.fromkeys(branches))


def execute_unmatched_disc_analysis(
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
    reviewer_scene_descriptions: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Run all-season analysis and retain safe telemetry for failed attempts."""

    started = perf_counter()
    analysis_run_id = uuid4().hex
    try:
        return _execute_unmatched_disc_analysis(
            store,
            disc_fingerprint,
            series_name,
            config,
            asr,
            contract_root,
            season=season,
            allow_gemini=allow_gemini,
            allow_content_fallback=allow_content_fallback,
            reviewer_scene_descriptions=reviewer_scene_descriptions,
            analysis_run_id=analysis_run_id,
        )
    except Exception as exc:
        try:
            selected = _selected_analysis_items(store, disc_fingerprint)
        except PipelineQueueError:
            selected = []
        media_ids = tuple(item.media_id for item, _payload in selected)
        dossier = IdentificationDossierStore(
            contract_root.parent / "identification-evidence"
        )
        failure_stage, failure_code = _matching_failure_diagnostic(exc)
        if media_ids:
            try:
                dossier.record_workflow_event(
                    media_ids,
                    analysis_run_id=analysis_run_id,
                    phase="analysis-finished",
                    disposition="failed",
                    summary={
                        "failure_stage": failure_stage,
                        "reason": failure_code,
                    },
                )
            except Exception as audit_exc:
                logger.warning(
                    "Failure audit was not persisted safely: {}",
                    type(audit_exc).__name__,
                )
        _record_matching_performance_safely(
            store,
            disc_fingerprint=disc_fingerprint,
            series_name=series_name.strip() or "Unresolved series",
            title_count=len(selected),
            anchor_count=min(3, len(selected)),
            season_scope=(season,) if season is not None else (),
            proposed_count=0,
            applied_count=0,
            unresolved_count=len(selected),
            anchor_elapsed_ms=0,
            total_elapsed_ms=round((perf_counter() - started) * 1000),
            outcome="failed",
            failure_stage=failure_stage,
            failure_code=failure_code,
            provider_branches=_matching_provider_branches(dossier, media_ids),
        )
        raise


def _execute_unmatched_disc_analysis(  # noqa: C901 - guarded disc-level workflow
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
    reviewer_scene_descriptions: Mapping[str, str] | None = None,
    analysis_run_id: str,
) -> tuple[str, ...]:
    """Transcribe held titles and compare them to the reviewed episode scope."""

    selected = _selected_analysis_items(store, disc_fingerprint)
    if not selected:
        raise PipelineQueueError(
            "At least one held title is required for disc sequence analysis"
        )
    selected_ids = {item.media_id for item, _payload in selected}
    media_ids = tuple(item.media_id for item, _payload in selected)
    dossier = IdentificationDossierStore(
        contract_root.parent / "identification-evidence"
    )
    _record_workflow_audit_safely(
        dossier,
        media_ids,
        analysis_run_id=analysis_run_id,
        phase="analysis-started",
        disposition="started",
        summary={
            "title_count": len(selected),
            "requested_series_name": series_name.strip()[:240],
            "requested_season": season,
            "gemini_enabled": allow_gemini,
        },
    )
    reviewer_scene_descriptions = dict(reviewer_scene_descriptions or {})
    if set(reviewer_scene_descriptions) - selected_ids:
        raise PipelineQueueError("Reviewer scene description does not match this disc")
    if reviewer_scene_descriptions and not allow_gemini:
        raise PipelineQueueError("Reviewer scene description requires Gemini approval")

    selected_series, catalog = _resolve_series_catalog_details(
        series_name, config, allow_gemini=allow_gemini
    )
    series_name = selected_series.name
    if season is not None:
        catalog = tuple(item for item in catalog if item.season == season)
    if not catalog:
        raise PipelineQueueError(
            "Episode catalogue is unavailable for the reviewed scope"
        )
    analysis_started = perf_counter()
    automatic_min_confidence = max(
        AUTOMATIC_TV_MIN_CONFIDENCE, float(config.min_confidence)
    )
    _record_workflow_audit_safely(
        dossier,
        media_ids,
        analysis_run_id=analysis_run_id,
        phase="catalogue-resolved",
        disposition="completed",
        summary={
            "candidate_series_name": series_name,
            "candidate_count": len(catalog),
            "minimum_confidence": round(automatic_min_confidence, 6),
        },
    )
    library_episodes = frozenset().union(
        *(
            existing_library_episodes(root, series_name)
            for root in (
                getattr(config, "jellyfin_tv_root", None),
                getattr(config, "transcode_output_root", None),
                getattr(config, "rip_output_root", None),
            )
        )
    )
    recorded_episodes = store.assigned_series_episodes(series_name)
    disc_episodes = assigned_disc_episodes(store, disc_fingerprint)
    known_episodes = library_episodes | recorded_episodes | disc_episodes
    season_scope = (
        (season,)
        if season is not None
        else dominant_season_scope(disc_episodes, catalog)
    )
    subtitle_provider = None
    subtitle_reference_cache: dict[int, tuple] = {}
    anchors = _anchor_titles(selected)
    anchor_evidence: tuple[UnmatchedFileEvidence, ...] = ()
    collected_dossier = None
    anchor_started = perf_counter()
    try:
        if not season_scope:
            anchor_evidence, collected_dossier = collect_dossier_evidence(
                anchors, config, asr, contract_root
            )
            if any(item.transcript_excerpts for item in anchor_evidence):
                anchor_candidate_evaluations: dict[str, list[dict[str, object]]] = {}
                try:
                    subtitle_provider = OpenSubtitlesProvider()
                    season_scope = discover_opensubtitles_season(
                        anchor_evidence,
                        catalog,
                        series_name,
                        selected_series.tmdb_id,
                        asr,
                        min_confidence=automatic_min_confidence,
                        provider=subtitle_provider,
                        reference_cache=subtitle_reference_cache,
                        season_order=preferred_season_order(catalog, known_episodes),
                        candidate_evaluations=anchor_candidate_evaluations,
                        audit_phase="anchor-season-discovery",
                    )
                except Exception as exc:
                    logger.info(
                        "All-season anchor discovery was inconclusive: {}",
                        type(exc).__name__,
                    )
                finally:
                    for file_id, evaluations in anchor_candidate_evaluations.items():
                        _record_candidate_audit_safely(
                            dossier,
                            file_id,
                            analysis_run_id=analysis_run_id,
                            branch="tv-opensubtitles",
                            evaluations=tuple(evaluations),
                        )
        if len(anchors) == len(selected):
            gemini_evidence = anchor_evidence
        else:
            gemini_evidence, collected_dossier = collect_dossier_evidence(
                tuple(selected), config, asr, contract_root
            )
        if collected_dossier is None:
            gemini_evidence, collected_dossier = collect_dossier_evidence(
                tuple(selected), config, asr, contract_root
            )
    except TranscriptBatchError as exc:
        raise PipelineQueueError(
            "Audio evidence collection failed before episode matching"
        ) from exc
    if collected_dossier is not None:
        dossier = collected_dossier
    anchor_elapsed_ms = round((perf_counter() - anchor_started) * 1000)
    for item in gemini_evidence:
        _record_workflow_audit_safely(
            dossier,
            (item.file_id,),
            analysis_run_id=analysis_run_id,
            phase="evidence-collected",
            disposition="completed",
            summary={
                "duration_seconds": round(item.duration_seconds, 3),
                "transcript_window_count": len(item.transcript_excerpts),
                "usable_transcript_available": bool(item.transcript_excerpts),
            },
        )
    logger.info(
        "All-season anchor pass: titles={} total_titles={} season_scope={} "
        "elapsed_ms={}",
        len(anchors),
        len(selected),
        ",".join(str(value) for value in season_scope) or "unresolved",
        anchor_elapsed_ms,
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
    candidate_catalog = tuple(
        entry for entry in catalog if (entry.season, entry.episode) not in disc_episodes
    )
    if len(candidate_catalog) < len(selected):
        raise PipelineQueueError(
            "Episode catalogue has too few unassigned entries for this disc"
        )
    gemini_catalog, gemini_candidate_scope = prioritize_missing_catalog(
        candidate_catalog, known_episodes, len(selected)
    )
    gemini_catalog_ids = frozenset(entry.episode_id for entry in gemini_catalog)
    existing_episode_ids = frozenset(
        entry.episode_id
        for entry in catalog
        if (entry.season, entry.episode) in known_episodes
        and entry.episode_id in gemini_catalog_ids
    )
    local_evidence_available = bool(evidence) and all(item.windows for item in evidence)
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
    catalog_by_id = {entry.episode_id: entry for entry in candidate_catalog}
    full_catalog_by_id = {entry.episode_id: entry for entry in catalog}
    disc_episode_title_count = _disc_title_count(store, disc_fingerprint, selected)
    history_range_anchors = _history_disc_range_anchors(
        store, disc_fingerprint, full_catalog_by_id
    )
    duration_by_id = {item.file_id: item.duration_seconds for item in gemini_evidence}
    provisional: dict[str, float] = {}
    assignment_evidence: dict[str, str] = {}
    opensubtitles_diagnostics: dict[str, tuple[float, float]] = {}
    sequence_candidates = {
        sequence_item.file_id: catalog_by_id.get(sequence_item.proposed_episode)
        for group in plan.groups
        for sequence_item in group.items
    }
    for media_id in media_ids:
        candidate = sequence_candidates.get(media_id)
        runtime_consistent = bool(
            candidate is not None
            and _episode_runtime_consistent(duration_by_id[media_id], candidate)
        )
        dossier.record_attempt(
            (media_id,),
            branch="tv-local",
            disposition="review",
            analysis_run_id=analysis_run_id,
            summary={
                "candidate_scope": "all",
                "candidate_count": len(catalog),
                "candidate_episode_id": (
                    candidate.episode_id if candidate is not None else None
                ),
                "candidate_episode_title": (
                    candidate.title if candidate is not None else None
                ),
                "candidate_series_name": series_name,
                "runtime_consistent": runtime_consistent,
                "reason": (
                    "advisory_sequence_candidate"
                    if runtime_consistent
                    else "advisory_sequence_runtime_mismatch"
                    if candidate is not None
                    else "advisory_sequence_unavailable"
                    if local_evidence_available
                    else "usable_transcript_unavailable"
                ),
            },
        )
    proposed: dict[str, EpisodeCatalogEntry] = {}
    if any(item.transcript_excerpts for item in gemini_evidence):
        opensubtitles_candidate_evaluations: dict[str, list[dict[str, object]]] = {}
        try:
            subtitle_provider = subtitle_provider or OpenSubtitlesProvider()
            if not season_scope:
                season_scope = discover_opensubtitles_season(
                    gemini_evidence,
                    candidate_catalog,
                    series_name,
                    selected_series.tmdb_id,
                    asr,
                    min_confidence=automatic_min_confidence,
                    provider=subtitle_provider,
                    reference_cache=subtitle_reference_cache,
                    season_order=preferred_season_order(catalog, known_episodes),
                    candidate_evaluations=opensubtitles_candidate_evaluations,
                    audit_phase="full-season-discovery",
                )
            opensubtitles_details: dict[str, dict[str, object]] = {}
            proposed, opensubtitles_diagnostics = match_opensubtitles_seasons(
                gemini_evidence,
                candidate_catalog,
                series_name,
                selected_series.tmdb_id,
                season_scope,
                asr,
                min_confidence=automatic_min_confidence,
                provider=subtitle_provider,
                reference_cache=subtitle_reference_cache,
                diagnostic_details=opensubtitles_details,
                prior_disc_assignments=bool(disc_episodes),
                candidate_evaluations=opensubtitles_candidate_evaluations,
            )
            for file_id in proposed:
                assignment_evidence[file_id] = (
                    OPENSUBTITLES_RESIDUAL_SOURCE
                    if opensubtitles_details.get(file_id, {}).get("reason")
                    == "accepted_after_disc_residual_reduction"
                    else OPENSUBTITLES_TWO_WINDOW_SOURCE
                )
            for item in gemini_evidence:
                score, margin = opensubtitles_diagnostics.get(item.file_id, (0.0, 0.0))
                details = opensubtitles_details.get(item.file_id, {})
                subtitle_candidate = catalog_by_id.get(
                    str(details.get("candidate_episode_id"))
                )
                dossier.record_attempt(
                    (item.file_id,),
                    branch="tv-opensubtitles",
                    disposition=("matched" if item.file_id in proposed else "review"),
                    analysis_run_id=analysis_run_id,
                    summary={
                        "candidate_scope": ",".join(
                            f"S{value:02d}" for value in season_scope
                        ),
                        "best_score": round(score, 6),
                        "runner_up_score": round(
                            float(details.get("runner_up_score", 0.0)), 6
                        ),
                        "margin": round(margin, 6),
                        "qualifying_window_count": int(
                            details.get("qualifying_window_count", 0)
                        ),
                        "candidate_episode_id": details.get("candidate_episode_id"),
                        "candidate_episode_title": (
                            subtitle_candidate.title
                            if subtitle_candidate is not None
                            else None
                        ),
                        "candidate_series_name": series_name,
                        "reason": details.get(
                            "reason", "subtitle_candidate_unavailable"
                        ),
                    },
                )
        except Exception as exc:
            dossier.record_attempt(
                media_ids,
                branch="tv-opensubtitles",
                disposition="failed",
                analysis_run_id=analysis_run_id,
                summary={"reason": type(exc).__name__},
            )
        finally:
            for file_id, evaluations in opensubtitles_candidate_evaluations.items():
                _record_candidate_audit_safely(
                    dossier,
                    file_id,
                    analysis_run_id=analysis_run_id,
                    branch="tv-opensubtitles",
                    evaluations=tuple(evaluations),
                )

    direct_range_anchors = tuple(
        entry
        for file_id, entry in proposed.items()
        if assignment_evidence.get(file_id) == OPENSUBTITLES_TWO_WINDOW_SOURCE
    )
    disc_range_fence = _build_disc_episode_range_fence(
        history_range_anchors + direct_range_anchors,
        catalog,
        disc_episode_title_count,
    )
    if disc_range_fence is not None:
        for file_id, entry in tuple(proposed.items()):
            if assignment_evidence.get(
                file_id
            ) != OPENSUBTITLES_RESIDUAL_SOURCE or disc_range_fence.permits(entry):
                continue
            proposed.pop(file_id, None)
            provisional.pop(file_id, None)
            assignment_evidence.pop(file_id, None)
            dossier.record_attempt(
                (file_id,),
                branch="tv-disc-range",
                disposition="review",
                analysis_run_id=analysis_run_id,
                summary={
                    "candidate_scope": disc_range_fence.scope,
                    "candidate_episode_id": entry.episode_id,
                    "candidate_episode_title": entry.title,
                    "candidate_series_name": series_name,
                    "anchor_count": len(disc_range_fence.anchor_episode_ids),
                    "disc_title_count": disc_range_fence.disc_title_count,
                    "reason": "residual_candidate_outside_disc_range",
                },
            )
    retained_range_anchors = tuple(
        entry
        for file_id, entry in proposed.items()
        if assignment_evidence.get(file_id) in _DISC_RANGE_HISTORY_EVIDENCE
    )
    disc_range_fence = _build_disc_episode_range_fence(
        history_range_anchors + retained_range_anchors,
        catalog,
        disc_episode_title_count,
    )

    logger.info(
        "All-season local matching: titles={} season_scope={} proposed={} "
        "elapsed_ms={}",
        len(selected),
        ",".join(str(value) for value in season_scope) or "unresolved",
        len(proposed),
        round((perf_counter() - analysis_started) * 1000),
    )

    # An explicit reviewer note requests a fresh Gemini episode decision for
    # that exact title. Keep the local/subtitle result in the safe dossier as
    # evidence, but do not let it bypass the confirmed external review.
    for file_id in reviewer_scene_descriptions:
        proposed.pop(file_id, None)
        provisional.pop(file_id, None)
        assignment_evidence.pop(file_id, None)

    unresolved_for_gemini = tuple(
        item for item in gemini_evidence if item.file_id not in proposed
    )
    # Preserve independent subtitle matches, but let Gemini inspect unresolved
    # titles against only the established season scope and unassigned episodes.
    if unresolved_for_gemini and allow_gemini:
        proposed_episode_ids = {entry.episode_id for entry in proposed.values()}
        assigned_episode_ids = existing_episode_ids | frozenset(proposed_episode_ids)
        bounded_seasons = set(season_scope)
        season_bounded_gemini_catalog = tuple(
            entry
            for entry in gemini_catalog
            if entry.episode_id not in proposed_episode_ids
            and (not bounded_seasons or entry.season in bounded_seasons)
        )
        remaining_gemini_catalog = (
            tuple(
                entry
                for entry in season_bounded_gemini_catalog
                if disc_range_fence.permits(entry)
            )
            if disc_range_fence is not None
            else season_bounded_gemini_catalog
        )
        effective_gemini_scope = (
            disc_range_fence.scope
            if disc_range_fence is not None
            else ",".join(f"S{value:02d}" for value in season_scope)
            if season_scope
            else gemini_candidate_scope
        )
        if disc_range_fence is not None:
            rejected_by_range = tuple(
                entry
                for entry in season_bounded_gemini_catalog
                if not disc_range_fence.permits(entry)
            )
            dossier.record_attempt(
                tuple(item.file_id for item in unresolved_for_gemini),
                branch="tv-disc-range",
                disposition="review",
                analysis_run_id=analysis_run_id,
                summary={
                    "candidate_scope": disc_range_fence.scope,
                    "candidate_count_before": len(season_bounded_gemini_catalog),
                    "candidate_count_after": len(remaining_gemini_catalog),
                    "anchor_count": len(disc_range_fence.anchor_episode_ids),
                    "disc_title_count": disc_range_fence.disc_title_count,
                    "reason": "evidence_anchored_candidate_fence_applied",
                },
            )
            for item in unresolved_for_gemini:
                _record_candidate_audit_safely(
                    dossier,
                    item.file_id,
                    analysis_run_id=analysis_run_id,
                    branch="tv-disc-range",
                    evaluations=tuple(
                        {
                            "phase": "gemini-candidate-filter",
                            "candidate_episode_id": entry.episode_id,
                            "candidate_episode_title": entry.title,
                            "season": entry.season,
                            "episode": entry.episode,
                            "disposition": "rejected",
                            "reason": "outside_evidence_anchored_disc_range",
                        }
                        for entry in rejected_by_range
                    ),
                )
        try:
            dossier.record_attempt(
                tuple(item.file_id for item in unresolved_for_gemini),
                branch="tv-gemini",
                disposition="started",
                analysis_run_id=analysis_run_id,
                summary={
                    "candidate_scope": effective_gemini_scope,
                    "candidate_count": len(remaining_gemini_catalog),
                    "reviewer_scene_description_supplied": any(
                        item.file_id in reviewer_scene_descriptions
                        for item in unresolved_for_gemini
                    ),
                },
            )
            ranker = GeminiEpisodeRanker(model=config.gemini_model)
            initial_matches = _rank_gemini_chunks(
                ranker,
                unresolved_for_gemini,
                remaining_gemini_catalog,
                dossier,
                assigned_episode_ids,
                reviewer_scene_descriptions,
                analysis_run_id=analysis_run_id,
                phase="gemini-initial",
            )
            if set(initial_matches) != {item.file_id for item in unresolved_for_gemini}:
                raise PipelineQueueError("Gemini did not review every disc title")
            gemini_catalog_by_id = {
                entry.episode_id: entry for entry in remaining_gemini_catalog
            }
            for file_id, match in initial_matches.items():
                candidate = gemini_catalog_by_id.get(str(match.episode_id))
                dossier.record_attempt(
                    (file_id,),
                    branch="tv-gemini",
                    disposition="review",
                    analysis_run_id=analysis_run_id,
                    summary={
                        "phase": "initial",
                        "candidate_episode_id": match.episode_id,
                        "candidate_episode_title": candidate.title
                        if candidate
                        else None,
                        "candidate_series_name": series_name,
                        "confidence": round(match.confidence, 6),
                        "reason": (
                            "gemini_initial_candidate"
                            if match.episode_id is not None
                            else "gemini_returned_no_episode"
                        ),
                    },
                )
            matches = _rank_gemini_chunks(
                ranker,
                unresolved_for_gemini,
                remaining_gemini_catalog,
                dossier,
                assigned_episode_ids,
                reviewer_scene_descriptions,
                analysis_run_id=analysis_run_id,
                phase="gemini-confirmation",
            )
            if set(matches) != {item.file_id for item in unresolved_for_gemini}:
                raise PipelineQueueError("Gemini did not review every disc title")
            duration_by_id = {
                item.file_id: item.duration_seconds for item in gemini_evidence
            }
            resolved_matches = {
                file_id: match
                for file_id, match in matches.items()
                if match.episode_id is not None
                and match.episode_id == initial_matches[file_id].episode_id
                and match.confidence >= automatic_min_confidence
                and initial_matches[file_id].confidence >= automatic_min_confidence
                and _episode_runtime_consistent(
                    duration_by_id[file_id],
                    gemini_catalog_by_id[str(match.episode_id)],
                )
            }
            proposed.update({
                file_id: gemini_catalog_by_id[str(match.episode_id)]
                for file_id, match in resolved_matches.items()
            })
            provisional.update({
                file_id: min(match.confidence, initial_matches[file_id].confidence)
                for file_id, match in resolved_matches.items()
            })
            assignment_evidence.update(
                dict.fromkeys(resolved_matches, GEMINI_TWO_PASS_SOURCE)
            )
            for file_id, match in matches.items():
                candidate = gemini_catalog_by_id.get(str(match.episode_id))
                initial_match = initial_matches[file_id]
                runtime_consistent = bool(
                    match.episode_id is None
                    or _episode_runtime_consistent(
                        duration_by_id[file_id],
                        gemini_catalog_by_id[str(match.episode_id)],
                    )
                )
                confirmation_reason = (
                    "accepted"
                    if file_id in resolved_matches
                    else "gemini_returned_no_episode"
                    if match.episode_id is None
                    else "gemini_passes_disagreed"
                    if match.episode_id != initial_match.episode_id
                    else "gemini_initial_below_confidence_threshold"
                    if initial_match.confidence < automatic_min_confidence
                    else "gemini_confirmation_below_confidence_threshold"
                    if match.confidence < automatic_min_confidence
                    else "runtime_mismatch"
                    if not runtime_consistent
                    else "gemini_candidate_rejected"
                )
                dossier.record_attempt(
                    (file_id,),
                    branch="tv-gemini",
                    disposition="matched" if file_id in proposed else "review",
                    analysis_run_id=analysis_run_id,
                    summary={
                        "phase": "confirmation",
                        "candidate_scope": effective_gemini_scope,
                        "candidate_episode_id": match.episode_id,
                        "candidate_episode_title": candidate.title
                        if candidate
                        else None,
                        "candidate_series_name": series_name,
                        "confidence": round(match.confidence, 6),
                        "consistent_with_initial": (
                            match.episode_id == initial_match.episode_id
                        ),
                        "runtime_consistent": runtime_consistent,
                        "reason": confirmation_reason,
                    },
                )
            if not resolved_matches and not proposed:
                raise PipelineQueueError(
                    "Gemini did not identify any disc title confidently"
                )
        except Exception as exc:
            failure = classify_gemini_failure(exc)
            dossier.record_attempt(
                tuple(item.file_id for item in unresolved_for_gemini),
                branch="tv-gemini",
                disposition="failed",
                analysis_run_id=analysis_run_id,
                summary={"reason": failure.review_code},
            )
            if proposed:
                logger.warning(
                    "Bounded Gemini fallback failed for unresolved titles; "
                    "preserving {} prior episode match(es): {}",
                    len(proposed),
                    failure.review_code,
                )
            else:
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
                        store,
                        media_ids,
                        config,
                        asr,
                        contract_root,
                        analysis_run_id=analysis_run_id,
                    )
                    if set(applied) == set(media_ids):
                        return applied
                except Exception as fallback_exc:
                    dossier.record_attempt(
                        media_ids,
                        branch="gemini-synthesis",
                        disposition="failed",
                        analysis_run_id=analysis_run_id,
                        summary={"reason": type(fallback_exc).__name__},
                    )
                raise failure from exc
    elif not proposed:
        raise PipelineQueueError("Independent episode evidence requires review")
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
            analysis_run_id=analysis_run_id,
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
            "evidence_source": assignment_evidence[item.media_id],
        }
        for item, payload in selected
        for title_index in (payload["title_index"],)
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
            episode_assignment_source="all-season-independent-evidence",
            identification_policy_version=(AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION),
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
                store,
                unresolved,
                config,
                asr,
                contract_root,
                analysis_run_id=analysis_run_id,
            )
            applied.extend(fallback_applied)
        except Exception as exc:
            dossier.record_attempt(
                unresolved,
                branch="gemini-synthesis",
                disposition="failed",
                analysis_run_id=analysis_run_id,
                summary={"reason": type(exc).__name__},
            )
            for media_id in unresolved:
                item = store.get(media_id)
                if (
                    item.state == "review_required"
                    and item.review_code != "visual_content_review_required"
                ):
                    store.choose_review_path(
                        media_id, "independent_episode_evidence_required"
                    )
    elif unresolved:
        for media_id in unresolved:
            store.choose_review_path(media_id, "independent_episode_evidence_required")
    final_unresolved = tuple(
        media_id
        for media_id in media_ids
        if media_id not in applied and media_id not in play_all_ids
    )
    for media_id in media_ids:
        if media_id in proposed:
            entry = proposed[media_id]
            disposition = "matched"
            summary = {
                "candidate_episode_id": entry.episode_id,
                "candidate_episode_title": entry.title,
                "candidate_series_name": series_name,
                "evidence_source": assignment_evidence[media_id],
                "reason": "episode_assignment_applied",
            }
        elif media_id in play_all_ids:
            disposition = "matched"
            summary = {"reason": "play_all_aggregate_detected"}
        elif media_id in applied:
            disposition = "matched"
            summary = {"reason": "content_fallback_applied"}
        else:
            disposition = "review"
            held = store.get(media_id)
            summary = {"reason": held.review_code or "identification_unresolved"}
        _record_workflow_audit_safely(
            dossier,
            (media_id,),
            analysis_run_id=analysis_run_id,
            phase="analysis-finished",
            disposition=disposition,
            summary=summary,
        )
    total_elapsed_ms = round((perf_counter() - analysis_started) * 1000)
    _record_matching_performance_safely(
        store,
        disc_fingerprint=disc_fingerprint,
        series_name=series_name,
        title_count=len(selected),
        anchor_count=len(anchors),
        season_scope=tuple(season_scope),
        proposed_count=len(proposed),
        applied_count=len(applied),
        unresolved_count=len(final_unresolved),
        anchor_elapsed_ms=anchor_elapsed_ms,
        total_elapsed_ms=total_elapsed_ms,
        outcome="completed",
        provider_branches=_matching_provider_branches(dossier, media_ids),
    )
    return tuple(applied)
