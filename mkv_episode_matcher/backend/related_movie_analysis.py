"""Identify feature-length TV-disc supplements as related movies.

This stage uses bounded TMDb candidates and ordinary movie subtitles.  It is
deliberately independent of disc filenames and never treats runtime alone as a
match: at least two strong Whisper dialogue anchors (or one exceptional anchor)
must distinguish the movie from its runner-up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.core.providers.subtitles import OpenSubtitlesProvider
from mkv_episode_matcher.core.utils import SubtitleReader, clean_text
from mkv_episode_matcher.media.gemini_matcher import UnmatchedFileEvidence
from mkv_episode_matcher.tmdb_client import MovieCandidate, search_movie_candidates

_FEATURE_LENGTH_SECONDS = 35 * 60


@dataclass(frozen=True)
class RelatedMovieMatch:
    """One subtitle-validated movie assignment."""

    candidate: MovieCandidate
    confidence: float
    qualifying_window_count: int
    margin: float
    part_number: int | None = None
    total_parts: int | None = None


def _is_multipart_split(source_seconds: float, candidate: MovieCandidate) -> bool:
    """Check if source duration matches a ~50% two-part movie split."""
    runtime = candidate.runtime_seconds
    if runtime is None or runtime <= 0:
        return False
    half_runtime = runtime / 2.0
    half_tolerance = max(5 * 60.0, half_runtime * 0.15)
    return abs(source_seconds - half_runtime) <= half_tolerance


def _runtime_consistent(source_seconds: float, candidate: MovieCandidate) -> bool:
    runtime = candidate.runtime_seconds
    if runtime is None or runtime <= 0:
        return False
    tolerance = max(5 * 60.0, runtime * 0.15)
    if abs(source_seconds - runtime) <= tolerance:
        return True
    return _is_multipart_split(source_seconds, candidate)


def _clean_movie_search_query(query: str) -> str:
    """Strip disc volume, side, and format tags to improve TMDb candidate recall."""
    cleaned = re.sub(
        r"\b(?:disc|disk|side|part|pt|vol|volume|season|s)\s*[-_]?\s*\d+[a-z]?\b",
        " ",
        query,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:side[ab]|d\d+|cd\d+|dvd\d*|bd|bluray|blu-ray|16x9|4x3|ws|fs|widescreen|fullscreen|revised|remastered|special edition|bonus|extras?)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[-_.]+", " ", cleaned)
    return " ".join(cleaned.split()).strip()


def _part_from_label(file_id: str) -> int | None:
    id_lower = file_id.lower()
    if (
        re.search(r"(?:side[-_ ]?a\b|part[-_ ]?0*1\b|pt[-_ ]?0*1\b)", id_lower)
        or "sidea" in id_lower
    ):
        return 1
    if (
        re.search(r"(?:side[-_ ]?b\b|part[-_ ]?0*2\b|pt[-_ ]?0*2\b)", id_lower)
        or "sideb" in id_lower
    ):
        return 2
    return None


def _best_cue_start(
    cleaned_excerpt: str,
    cues: tuple[tuple[float, float, str], ...],
    asr,
) -> float | None:
    best_start = None
    best_score = 0.0
    for start, _end, cue_text in cues:
        cleaned_cue = clean_text(cue_text)
        if not cleaned_cue:
            continue
        if asr is not None and hasattr(asr, "calculate_match_score"):
            score = asr.calculate_match_score(cleaned_excerpt, cleaned_cue)
        else:
            score = (
                1.0
                if cleaned_cue in cleaned_excerpt or cleaned_excerpt in cleaned_cue
                else 0.0
            )
        if score > best_score:
            best_score = score
            best_start = start
    return best_start if best_score >= 0.4 else None


def _part_from_subtitle_cues(
    excerpts: tuple[str, ...],
    runtime: float | None,
    content: str | None,
    asr=None,
) -> int | None:
    if not content or not runtime or runtime <= 0:
        return None
    from mkv_episode_matcher.backend.unmatched_disc_analysis import _subtitle_cues

    cues = _subtitle_cues(content)
    if not cues:
        return None
    matched_starts = [
        start
        for excerpt in excerpts
        if (cleaned := clean_text(excerpt))
        and (start := _best_cue_start(cleaned, cues, asr)) is not None
    ]
    if not matched_starts:
        return None
    avg_time = sum(matched_starts) / len(matched_starts)
    return 1 if avg_time < (runtime / 2.0) else 2


def _infer_part_number(
    item: UnmatchedFileEvidence,
    candidate: MovieCandidate,
    content: str | None,
    asr=None,
) -> int:
    """Infer whether a multi-part title is Part 1 or Part 2."""
    from_label = _part_from_label(item.file_id)
    if from_label is not None:
        return from_label
    from_cues = _part_from_subtitle_cues(
        item.transcript_excerpts, candidate.runtime_seconds, content, asr
    )
    return from_cues if from_cues is not None else 1


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


def _search_candidates(
    series_name: str,
    excluded_tmdb_ids: frozenset[int],
    feature_items: tuple[UnmatchedFileEvidence, ...],
) -> tuple[MovieCandidate, ...]:
    searched = list(search_movie_candidates(series_name, limit=8))
    clean_query = _clean_movie_search_query(series_name)
    if clean_query and clean_query.casefold() != series_name.casefold():
        existing_ids = {c.tmdb_id for c in searched}
        for extra in search_movie_candidates(clean_query, limit=8):
            if extra.tmdb_id not in existing_ids:
                searched.append(extra)
                existing_ids.add(extra.tmdb_id)

    return tuple(
        candidate
        for candidate in searched
        if candidate.tmdb_id not in excluded_tmdb_ids
        and any(
            _runtime_consistent(item.duration_seconds, candidate)
            for item in feature_items
        )
    )


def match_related_tv_movies(  # noqa: C901 - linear guarded workflow
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

    candidates = _search_candidates(series_name, excluded_tmdb_ids, feature_items)
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
        is_multipart = _is_multipart_split(item.duration_seconds, best)
        part_number = (
            _infer_part_number(item, best, references.get(best.tmdb_id), asr)
            if is_multipart
            else None
        )
        total_parts = 2 if is_multipart else None
        diagnostics[item.file_id] = {
            "candidate_count": len(candidates),
            "subtitle_reference_count": len(references),
            "candidate_tmdb_id": best.tmdb_id,
            "best_score": round(best_score, 6),
            "runner_up_score": round(runner_up, 6),
            "margin": round(margin, 6),
            "qualifying_window_count": best_votes,
            "is_multipart": is_multipart,
            "part_number": part_number,
            "reason": reason or "candidate_pending",
        }
        if reason is None:
            proposals.append((
                best_score,
                margin,
                item.file_id,
                best,
                best_votes,
                part_number,
                total_parts,
            ))

    matches: dict[str, RelatedMovieMatch] = {}
    used: set[tuple[int, int | None]] = {
        (tmdb_id, None) for tmdb_id in excluded_tmdb_ids
    }
    for score, margin, file_id, candidate, votes, part_number, total_parts in sorted(
        proposals, reverse=True
    ):
        key = (candidate.tmdb_id, part_number)
        if key in used:
            diagnostics[file_id]["reason"] = "movie_already_assigned"
            continue
        matches[file_id] = RelatedMovieMatch(
            candidate,
            score,
            votes,
            margin,
            part_number=part_number,
            total_parts=total_parts,
        )
        diagnostics[file_id]["reason"] = "accepted"
        used.add(key)
    return matches, diagnostics
