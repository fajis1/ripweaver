"""Identify feature-length TV-disc supplements as related movies.

This stage uses bounded TMDb candidates and ordinary movie subtitles.  It is
deliberately independent of disc filenames and never treats runtime alone as a
match: at least two strong Whisper dialogue anchors (or one exceptional anchor)
must distinguish the movie from its runner-up.
"""

from __future__ import annotations

from dataclasses import dataclass

from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.core.providers.subtitles import OpenSubtitlesProvider
from mkv_episode_matcher.core.utils import SubtitleReader
from mkv_episode_matcher.media.gemini_matcher import UnmatchedFileEvidence
from mkv_episode_matcher.tmdb_client import MovieCandidate, search_movie_candidates

_FEATURE_LENGTH_SECONDS = 45 * 60


@dataclass(frozen=True)
class RelatedMovieMatch:
    """One subtitle-validated movie assignment."""

    candidate: MovieCandidate
    confidence: float
    qualifying_window_count: int
    margin: float


def _runtime_consistent(source_seconds: float, candidate: MovieCandidate) -> bool:
    runtime = candidate.runtime_seconds
    if runtime is None or runtime <= 0:
        return False
    tolerance = max(5 * 60.0, runtime * 0.15)
    return abs(source_seconds - runtime) <= tolerance


def _movie_references(
    candidates: tuple[MovieCandidate, ...], provider: OpenSubtitlesProvider
) -> dict[int, str]:
    references = {}
    for candidate in candidates:
        subtitle = provider.get_movie_subtitle(
            candidate.title,
            tmdb_id=candidate.tmdb_id,
            year=candidate.release_year,
        )
        if subtitle is None:
            continue
        try:
            content = subtitle.content or SubtitleReader.read_srt_file(subtitle.path)
        except (OSError, ValueError):
            continue
        if content.strip():
            references[candidate.tmdb_id] = content
    return references


def _rank_item(
    item: UnmatchedFileEvidence,
    candidates: tuple[MovieCandidate, ...],
    references: dict[int, str],
    asr,
    min_confidence: float,
) -> list[tuple[float, int, MovieCandidate]]:
    from mkv_episode_matcher.backend.unmatched_disc_analysis import _score_subtitle

    ranked = []
    for candidate in candidates:
        content = references.get(candidate.tmdb_id)
        if content is None or not _runtime_consistent(item.duration_seconds, candidate):
            continue
        average_score, window_scores = _score_subtitle(
            asr,
            item.transcript_excerpts,
            content,
            item.duration_seconds,
        )
        qualifying = tuple(score for score in window_scores if score >= min_confidence)
        consensus = sum(qualifying) / len(qualifying) if qualifying else average_score
        ranked.append((consensus, len(qualifying), candidate))
    return sorted(ranked, key=lambda value: value[0], reverse=True)


def match_related_tv_movies(
    evidence: tuple[UnmatchedFileEvidence, ...],
    series_name: str,
    config: Config,
    asr,
    *,
    excluded_tmdb_ids: frozenset[int] = frozenset(),
    subtitle_provider: OpenSubtitlesProvider | None = None,
) -> tuple[dict[str, RelatedMovieMatch], dict[str, dict[str, object]]]:
    """Match feature-length TV-disc items against related movie subtitles."""

    feature_items = tuple(
        item
        for item in evidence
        if item.duration_seconds >= _FEATURE_LENGTH_SECONDS
        and any(excerpt.strip() for excerpt in item.transcript_excerpts)
    )
    if not feature_items:
        return {}, {}

    candidates = tuple(
        candidate
        for candidate in search_movie_candidates(series_name, limit=8)
        if candidate.tmdb_id not in excluded_tmdb_ids
        and any(
            _runtime_consistent(item.duration_seconds, candidate)
            for item in feature_items
        )
    )
    if not candidates:
        return {}, {
            item.file_id: {
                "candidate_count": 0,
                "reason": "no_runtime_compatible_movies",
            }
            for item in feature_items
        }

    provider = subtitle_provider or OpenSubtitlesProvider()
    references = _movie_references(candidates, provider)

    # Import lazily to avoid coupling module initialization to the larger
    # all-season analyzer.  Both paths intentionally share the same extended-
    # cut, timestamp-independent anchor scorer and acceptance thresholds.
    from mkv_episode_matcher.backend.unmatched_disc_analysis import (
        _subtitle_candidate_rejection_reason,
    )

    proposals = []
    diagnostics: dict[str, dict[str, object]] = {}
    for item in feature_items:
        ranked = _rank_item(
            item,
            candidates,
            references,
            asr,
            config.min_confidence,
        )
        if not ranked:
            diagnostics[item.file_id] = {
                "candidate_count": len(candidates),
                "subtitle_reference_count": len(references),
                "reason": "no_usable_movie_subtitles",
            }
            continue
        best_score, best_votes, best = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        margin = best_score - runner_up
        reason = _subtitle_candidate_rejection_reason(
            best_score,
            best_votes,
            margin,
            config.min_confidence,
        )
        diagnostics[item.file_id] = {
            "candidate_count": len(candidates),
            "subtitle_reference_count": len(references),
            "candidate_tmdb_id": best.tmdb_id,
            "best_score": round(best_score, 6),
            "runner_up_score": round(runner_up, 6),
            "margin": round(margin, 6),
            "qualifying_window_count": best_votes,
            "reason": reason or "candidate_pending",
        }
        if reason is None:
            proposals.append((best_score, margin, item.file_id, best, best_votes))

    matches: dict[str, RelatedMovieMatch] = {}
    used = set(excluded_tmdb_ids)
    for score, margin, file_id, candidate, votes in sorted(proposals, reverse=True):
        if candidate.tmdb_id in used:
            diagnostics[file_id]["reason"] = "movie_already_assigned"
            continue
        matches[file_id] = RelatedMovieMatch(candidate, score, votes, margin)
        diagnostics[file_id]["reason"] = "accepted"
        used.add(candidate.tmdb_id)
    return matches, diagnostics
