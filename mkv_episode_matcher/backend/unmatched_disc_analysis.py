"""Disc-level all-season identification for seasonless television rips."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from inspect import signature
from pathlib import Path
from time import perf_counter, sleep
from uuid import uuid4

from loguru import logger

from mkv_episode_matcher.backend.identification_dossier import (
    IdentificationDossierStore,
    collect_dossier_evidence,
    collect_supplemental_dossier_evidence,
)
from mkv_episode_matcher.core.credentials import ApiCredentialError, ApiServiceError
from mkv_episode_matcher.core.models import Config
from mkv_episode_matcher.core.providers.subtitles import OpenSubtitlesProvider
from mkv_episode_matcher.core.subtitle_releases import (
    infer_subtitle_release_profile,
    release_match_priority,
)
from mkv_episode_matcher.core.tv_identification_policy import (
    AUTOMATIC_TV_IDENTIFICATION_POLICY_VERSION,
    AUTOMATIC_TV_MIN_CONFIDENCE,
    GEMINI_TWO_PASS_SOURCE,
    LOCAL_DIALOGUE_TWO_WINDOW_SOURCE,
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
    GeminiSubtitleComparisonEvidence,
    UnmatchedFileEvidence,
    gemini_request_digest,
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


class DiscCoherenceError(GeminiAnalysisError):
    """Stop every proposed assignment when the completed disc set is implausible."""

    def __init__(self) -> None:
        super().__init__(
            "whole_disc_coherence_review_required",
            "The proposed episode assignments do not form a plausible whole-disc set",
        )


_SUBTITLE_YIELD_EVERY_COMPARISONS = 32
_SUBTITLE_YIELD_SECONDS = 0.001


class _SubtitleAnalysisProgress:
    """Cooperatively yield during finite fuzzy work without a time deadline."""

    def __init__(
        self,
        *,
        yield_control: Callable[[float], None] = sleep,
        yield_every: int = _SUBTITLE_YIELD_EVERY_COMPARISONS,
    ) -> None:
        if yield_every <= 0:
            raise ValueError("Subtitle analysis progress interval is invalid")
        self._yield_control = yield_control
        self._yield_every = yield_every
        self._checkpoints = 0

    def checkpoint(self) -> None:
        """Yield periodically while deterministic work keys keep the pass finite."""

        self._checkpoints += 1
        if self._checkpoints % self._yield_every == 0:
            self._yield_control(_SUBTITLE_YIELD_SECONDS)


def _checkpoint_subtitle_progress(
    analysis_progress: _SubtitleAnalysisProgress | None,
) -> None:
    if analysis_progress is not None:
        analysis_progress.checkpoint()


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
    LOCAL_DIALOGUE_TWO_WINDOW_SOURCE,
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


def _disc_number_hint(selected: list[tuple[object, dict]]) -> int | None:
    hints = {
        context.get("disc_number")
        for _item, payload in selected
        if isinstance((context := payload.get("media_context")), dict)
        and isinstance(context.get("disc_number"), int)
        and not isinstance(context.get("disc_number"), bool)
        and context.get("disc_number") > 0
    }
    return hints.pop() if len(hints) == 1 else None


def _subtitle_lookup_series_name(
    canonical_series_name: str,
    selected: list[tuple[object, dict]],
    *,
    reviewed_series_name: str | None = None,
) -> tuple[str, str | None]:
    """Retain a saved edition hint without changing canonical TV metadata."""

    contextual_names = [canonical_series_name]
    if reviewed_series_name and reviewed_series_name.strip():
        contextual_names.append(reviewed_series_name)
    contextual_names.extend(
        str(context["series_name"])
        for _item, payload in selected
        if isinstance((context := payload.get("media_context")), dict)
        and isinstance(context.get("series_name"), str)
        and str(context["series_name"]).strip()
    )
    profile = infer_subtitle_release_profile(" ".join(contextual_names))
    canonical_profile = infer_subtitle_release_profile(canonical_series_name)
    lookup_base = canonical_profile.canonical_series_name
    release_suffix = {
        "superfan": "Superfan Episodes",
        "extended": "Extended",
        "unrated": "Unrated",
        "supercut": "Supercut",
        "directors_cut": "Director's Cut",
    }.get(profile.key)
    if release_suffix is not None:
        return f"{lookup_base} {release_suffix}", profile.key
    return canonical_series_name, None


def _reviewed_episode_catalog(
    catalog: tuple[EpisodeCatalogEntry, ...],
    season: int | None,
    episode_range: tuple[int, int] | None,
) -> tuple[EpisodeCatalogEntry, ...]:
    """Apply a reviewer-supplied candidate boundary without assigning a title."""

    scoped = (
        tuple(item for item in catalog if item.season == season)
        if season is not None
        else catalog
    )
    if episode_range is None:
        return scoped
    if season is None:
        raise PipelineQueueError("A reviewed episode range requires a season")
    start, end = episode_range
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 1
        or end < start
        or end - start > 49
    ):
        raise PipelineQueueError("Reviewed episode range is invalid")
    return tuple(item for item in scoped if start <= item.episode <= end)


def _reviewed_candidate_scope_label(
    season: int | None, episode_range: tuple[int, int] | None
) -> str:
    if season is None:
        return "all"
    if episode_range is None:
        return f"S{season:02d}"
    start, end = episode_range
    return f"S{season:02d}E{start:02d}-E{end:02d}"


def _prioritize_catalog_for_disc_number(
    catalog: tuple[EpisodeCatalogEntry, ...],
    disc_number: int | None,
    disc_title_count: int,
) -> tuple[EpisodeCatalogEntry, ...]:
    """Use a disc ordinal only to order candidates, never to remove or name one.

    A disc with ``N`` episode-like titles gives disc ``D`` an expected window
    of ``((D - 1) * N) + 1`` through ``D * N``. The assumption is deliberately
    advisory because releases can distribute episodes unevenly or include
    extras. Every aired candidate remains available for dialogue matching.
    """

    if disc_number is None or disc_number < 1 or disc_title_count < 1 or not catalog:
        return catalog
    seasons = {entry.season for entry in catalog}
    if len(seasons) != 1:
        return catalog
    maximum = max(entry.episode for entry in catalog)
    expected_minimum = ((disc_number - 1) * disc_title_count) + 1
    if expected_minimum > maximum:
        return catalog
    expected_maximum = min(maximum, disc_number * disc_title_count)

    def priority(entry: EpisodeCatalogEntry) -> tuple[int, int, int]:
        if expected_minimum <= entry.episode <= expected_maximum:
            return (0, 0, entry.episode)
        distance = (
            expected_minimum - entry.episode
            if entry.episode < expected_minimum
            else entry.episode - expected_maximum
        )
        return (1, distance, entry.episode)

    return tuple(sorted(catalog, key=priority))


def _disc_assignments_coherent(
    entries: tuple[EpisodeCatalogEntry, ...],
    *,
    disc_title_count: int,
    required_season: int | None,
) -> bool:
    """Reject an assignment set that cannot plausibly belong to one disc."""

    if not entries:
        return True
    seasons = {entry.season for entry in entries}
    if len(seasons) != 1 or (
        required_season is not None and seasons != {required_season}
    ):
        return False
    unique_episodes = {entry.episode for entry in entries}
    if len(unique_episodes) != len(entries):
        return False
    if len(unique_episodes) < 2:
        return True
    return max(unique_episodes) - min(unique_episodes) + 1 <= disc_title_count


# Retain the old private name for callers that used the narrower provider guard.
_gemini_disc_assignments_coherent = _disc_assignments_coherent


def _combined_disc_assignments(
    store: PipelineQueueStore,
    disc_fingerprint: str,
    selected: list[tuple[object, dict]],
    proposed: Mapping[str, EpisodeCatalogEntry],
    catalog_by_id: Mapping[str, EpisodeCatalogEntry],
) -> tuple[EpisodeCatalogEntry, ...]:
    """Combine durable sibling outcomes with this run's proposals by title index."""

    expected = store.expected_title_indexes_for_disc(disc_fingerprint)
    by_title: dict[int, EpisodeCatalogEntry] = {}
    for title_index, outcome in store.catalogue_title_history(disc_fingerprint).items():
        if expected and title_index not in expected:
            continue
        episode_id = outcome.get("episode_id")
        if isinstance(episode_id, str):
            entry = catalog_by_id.get(episode_id.upper())
            if entry is not None:
                by_title[title_index] = entry
    for item, payload in selected:
        entry = proposed.get(item.media_id)
        title_index = payload.get("title_index")
        if (
            entry is not None
            and isinstance(title_index, int)
            and not isinstance(title_index, bool)
        ):
            by_title[title_index] = entry
    return tuple(by_title[index] for index in sorted(by_title))


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


def _subtitle_cues(content: str) -> tuple[tuple[float, float, str], ...]:
    """Parse bounded SRT timing without retaining dialogue outside the caller."""

    cues = []
    for block in re.split(r"\r?\n\r?\n", content.strip()):
        lines = block.splitlines()
        timestamp_index = next(
            (index for index, line in enumerate(lines[:3]) if "-->" in line), None
        )
        if timestamp_index is None or timestamp_index + 1 >= len(lines):
            continue
        try:
            start_text, end_text = lines[timestamp_index].split("-->", maxsplit=1)
            start = SubtitleReader.parse_timestamp(start_text.strip())
            end = SubtitleReader.parse_timestamp(end_text.strip().split()[0])
        except (TypeError, ValueError):
            continue
        text = " ".join(lines[timestamp_index + 1 :]).strip()
        if text and end >= start:
            cues.append((start, end, text))
    return tuple(cues)


def _subtitle_chunk_from_cues(
    cues: tuple[tuple[float, float, str], ...], start: float, end: float
) -> str:
    return " ".join(
        text
        for cue_start, cue_end, text in cues
        if cue_end >= start and cue_start <= end
    )


def _subtitle_reference_windows(content: str, excerpt: str) -> tuple[str, ...]:
    cues = _subtitle_cues(content)
    dialogue = " ".join(text for _start, _end, text in cues) if cues else content
    words = clean_text(re.sub(r"<[^>]+>", " ", dialogue)).split()
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


def _best_global_subtitle_pair(
    asr,
    excerpt: str,
    content: str,
    *,
    analysis_progress: _SubtitleAnalysisProgress | None = None,
) -> tuple[float, str]:
    cleaned = clean_text(excerpt)
    windows = _subtitle_reference_windows(content, cleaned)
    if not cleaned or not windows:
        return 0.0, ""
    scored = []
    for window in windows:
        if analysis_progress is not None:
            analysis_progress.checkpoint()
        scored.append((asr.calculate_match_score(cleaned, window), window))
    return max(scored, key=lambda value: value[0])


def _gemini_subtitle_comparisons(  # noqa: C901 - bounded evidence selection
    files: tuple[UnmatchedFileEvidence, ...],
    references: tuple,
    candidate_evaluations: Mapping[str, list[dict[str, object]]],
    asr,
    *,
    analysis_progress: _SubtitleAnalysisProgress | None = None,
) -> dict[str, tuple[GeminiSubtitleComparisonEvidence, ...]]:
    """Build bounded paired evidence for only the strongest local candidates."""

    references_by_id: dict[str, list] = {}
    for subtitle in references:
        info = subtitle.episode_info
        if info is not None:
            references_by_id.setdefault(
                f"S{info.season:02d}E{info.episode:02d}", []
            ).append(subtitle)
    comparisons = {}
    for item in files:
        evaluations = candidate_evaluations.get(item.file_id, [])
        latest_phase = (
            "offset-six-window-retry"
            if any(
                value.get("phase") == "offset-six-window-retry" for value in evaluations
            )
            else "direct"
        )
        ranked_ids = []
        for evaluation in sorted(
            (value for value in evaluations if value.get("phase") == latest_phase),
            key=lambda value: int(value.get("rank", 10_000)),
        ):
            episode_id = evaluation.get("candidate_episode_id")
            if (
                isinstance(episode_id, str)
                and episode_id in references_by_id
                and episode_id not in ranked_ids
            ):
                ranked_ids.append(episode_id)
            if len(ranked_ids) == 3:
                break
        pairs = []
        for episode_id in ranked_ids:
            scored_pairs = []
            for subtitle in references_by_id[episode_id]:
                try:
                    content = subtitle.content or SubtitleReader.read_srt_file(
                        subtitle.path
                    )
                except (OSError, ValueError):
                    continue
                for excerpt in item.transcript_excerpts:
                    score, reference = _best_global_subtitle_pair(
                        asr,
                        excerpt,
                        content,
                        analysis_progress=analysis_progress,
                    )
                    if reference:
                        scored_pairs.append((
                            score,
                            release_match_priority(
                                getattr(subtitle, "release_match", "unresolved")
                            ),
                            excerpt,
                            reference,
                        ))
            for score, _release_priority, excerpt, reference in sorted(
                scored_pairs, key=lambda value: (value[0], value[1]), reverse=True
            )[:2]:
                pairs.append(
                    GeminiSubtitleComparisonEvidence(
                        candidate_episode_id=episode_id,
                        whisper_excerpt=" ".join(excerpt.split())[:400],
                        subtitle_excerpt=" ".join(reference.split())[:400],
                        local_score=max(0.0, min(1.0, float(score))),
                    )
                )
        comparisons[item.file_id] = tuple(pairs[:6])
    return comparisons


def _select_gemini_file_evidence(
    files: tuple[UnmatchedFileEvidence, ...],
    comparisons: Mapping[str, tuple[GeminiSubtitleComparisonEvidence, ...]],
) -> tuple[UnmatchedFileEvidence, ...]:
    """Choose at most six strong, non-duplicate excerpts from twelve local windows."""

    selected_files = []
    for item in files:
        start_by_excerpt = (
            dict(
                zip(
                    item.transcript_excerpts,
                    item.transcript_start_seconds,
                    strict=True,
                )
            )
            if len(item.transcript_start_seconds) == len(item.transcript_excerpts)
            else {}
        )
        strength = {
            pair.whisper_excerpt: pair.local_score
            for pair in comparisons.get(item.file_id, ())
        }
        ranked = sorted(
            item.transcript_excerpts,
            key=lambda excerpt: strength.get(" ".join(excerpt.split())[:400], 0.0),
            reverse=True,
        )
        chosen: list[str] = []
        chosen_tokens: list[set[str]] = []
        for excerpt in ranked:
            tokens = set(clean_text(excerpt).split())
            if not tokens:
                continue
            if any(
                len(tokens & prior) / max(1, len(tokens | prior)) >= 0.75
                for prior in chosen_tokens
            ):
                continue
            chosen.append(excerpt)
            chosen_tokens.append(tokens)
            if len(chosen) == 6:
                break
        selected_files.append(
            UnmatchedFileEvidence(
                item.file_id,
                item.duration_seconds,
                tuple(chosen[:6]),
                (
                    tuple(start_by_excerpt[excerpt] for excerpt in chosen[:6])
                    if start_by_excerpt
                    and all(excerpt in start_by_excerpt for excerpt in chosen[:6])
                    else ()
                ),
            )
        )
    return tuple(selected_files)


def _filter_gemini_subtitle_comparisons(
    comparisons: Mapping[str, tuple[GeminiSubtitleComparisonEvidence, ...]],
    catalog: tuple[EpisodeCatalogEntry, ...],
) -> dict[str, tuple[GeminiSubtitleComparisonEvidence, ...]]:
    """Keep paired subtitle evidence inside Gemini's final candidate set."""

    allowed_episode_ids = {entry.episode_id for entry in catalog}
    return {
        file_id: filtered
        for file_id, pairs in comparisons.items()
        if (
            filtered := tuple(
                pair
                for pair in pairs
                if pair.candidate_episode_id in allowed_episode_ids
            )
        )
    }


def _subtitle_alignment_scores(  # noqa: C901 - explicit finite clock grid
    asr,
    cleaned_excerpts: tuple[str, ...],
    starts: tuple[float, ...],
    content: str,
    duration_seconds: float,
    *,
    analysis_progress: _SubtitleAnalysisProgress | None = None,
) -> tuple[float, ...]:
    """Choose one scale/offset transform that best supports multiple anchors."""

    cues = _subtitle_cues(content)
    subtitle_duration = max((end for _start, end, _text in cues), default=0.0)
    scales = [1.0]
    if duration_seconds > 0 and subtitle_duration > 0:
        normalized_scale = subtitle_duration / duration_seconds
        if 0.5 <= normalized_scale <= 1.5 and abs(normalized_scale - 1.0) >= 0.01:
            scales.append(normalized_scale)
    offsets = (
        -300.0,
        -180.0,
        -120.0,
        -60.0,
        -30.0,
        0.0,
        30.0,
        60.0,
        120.0,
        180.0,
        300.0,
    )
    best_aligned: tuple[float, ...] = tuple(0.0 for _excerpt in cleaned_excerpts)
    best_alignment_rank = (-1.0, -1.0)
    for scale in scales:
        for offset in offsets:
            transform_scores = []
            for cleaned, start in zip(cleaned_excerpts, starts, strict=True):
                _checkpoint_subtitle_progress(analysis_progress)
                if not cleaned:
                    transform_scores.append(0.0)
                    continue
                predicted = max(0.0, start * scale + offset)
                if cues:
                    reference = clean_text(
                        _subtitle_chunk_from_cues(
                            cues,
                            max(0.0, predicted - 30.0),
                            predicted + 60.0,
                        )
                    )
                else:
                    reference = clean_text(
                        " ".join(
                            SubtitleReader.extract_subtitle_chunk(
                                content,
                                max(0.0, predicted - 30.0),
                                predicted + 60.0,
                            )
                        )
                    )
                transform_scores.append(
                    asr.calculate_match_score(cleaned, reference) if reference else 0.0
                )
            strongest = sorted(transform_scores, reverse=True)[:2]
            alignment_rank = (sum(strongest), sum(transform_scores))
            if alignment_rank > best_alignment_rank:
                best_alignment_rank = alignment_rank
                best_aligned = tuple(transform_scores)
    return best_aligned


def _score_subtitle(
    asr,
    excerpts: tuple[str, ...],
    content: str,
    duration_seconds: float,
    *,
    sample_start_seconds: tuple[float, ...] = (),
    analysis_progress: _SubtitleAnalysisProgress | None = None,
) -> tuple[float, tuple[float, ...]]:
    """Score dialogue using one expanded clock plus global scene recovery.

    The clock grid admits a constant offset and duration drift across multiple
    anchors.  Every excerpt also receives a bounded timestamp-free search, so
    added Superfan scenes may introduce discontinuous jumps without hiding the
    unchanged broadcast dialogue on either side.
    """

    starts = (
        sample_start_seconds
        if len(sample_start_seconds) == len(excerpts)
        else tuple(
            max(0.0, duration_seconds * position / (len(excerpts) + 1) - 15.0)
            for position in range(1, len(excerpts) + 1)
        )
    )
    cleaned_excerpts = tuple(clean_text(excerpt) for excerpt in excerpts)
    best_aligned = _subtitle_alignment_scores(
        asr,
        cleaned_excerpts,
        starts,
        content,
        duration_seconds,
        analysis_progress=analysis_progress,
    )

    scores = []
    for index, cleaned in enumerate(cleaned_excerpts):
        _checkpoint_subtitle_progress(analysis_progress)
        if not cleaned:
            continue
        # Always search the regular subtitle globally as well.  A reconstructed
        # cut may still have valid subtitle text at the calculated timestamp,
        # but it can be a completely different scene after earlier insertions.
        windows = _subtitle_reference_windows(content, cleaned)
        global_scores = []
        for window in windows:
            _checkpoint_subtitle_progress(analysis_progress)
            global_scores.append(asr.calculate_match_score(cleaned, window))
        global_score = max(global_scores) if global_scores else 0.0
        aligned_score = best_aligned[index]
        if aligned_score or global_score:
            scores.append(max(aligned_score, global_score))
    if not scores:
        return 0.0, ()
    scores.sort(reverse=True)
    return sum(scores) / len(scores), tuple(scores)


_ALTERED_RELEASE_PROFILES = frozenset({
    "superfan",
    "extended",
    "unrated",
    "supercut",
    "directors_cut",
})


def _references_use_altered_cut(references: tuple) -> bool:
    return any(
        getattr(reference, "release_profile", None) in _ALTERED_RELEASE_PROFILES
        for reference in references
    )


def _release_ladder_references(references: tuple, tier: str) -> tuple:
    if tier == "altered":
        return tuple(
            reference
            for reference in references
            if getattr(reference, "release_match", "unresolved")
            in {"exact", "compatible"}
        )
    if tier == "generic":
        return tuple(
            reference
            for reference in references
            if getattr(reference, "release_match", "unresolved")
            in {"generic", "unresolved"}
        )
    raise ValueError("Subtitle release ladder tier is invalid")


def _subtitle_candidates(  # noqa: C901 - per-reference rejection audit
    files,
    references,
    catalog_by_number,
    asr,
    *,
    min_confidence,
    rejected_candidates=None,
    phase="direct",
    analysis_progress: _SubtitleAnalysisProgress | None = None,
):
    scored = {}
    altered_cut = _references_use_altered_cut(tuple(references))
    for item in files:
        if analysis_progress is not None:
            analysis_progress.checkpoint()
        candidates_by_episode = {}
        for subtitle in references:
            if analysis_progress is not None:
                analysis_progress.checkpoint()
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
            if not _episode_runtime_consistent(
                item.duration_seconds,
                entry,
                altered_cut=altered_cut,
            ):
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
                sample_start_seconds=item.transcript_start_seconds,
                analysis_progress=analysis_progress,
            )
            qualifying = tuple(
                score for score in window_scores if score >= min_confidence
            )
            consensus_score = (
                sum(qualifying) / len(qualifying) if qualifying else average_score
            )
            candidate = (
                consensus_score,
                len(qualifying),
                entry,
            )
            episode_key = (entry.season, entry.episode)
            prior = candidates_by_episode.get(episode_key)
            candidate_rank = (
                consensus_score,
                len(qualifying),
                release_match_priority(
                    getattr(subtitle, "release_match", "unresolved")
                ),
            )
            if prior is None or candidate_rank > prior[0]:
                candidates_by_episode[episode_key] = (candidate_rank, candidate)
        scored[item.file_id] = sorted(
            (value[1] for value in candidates_by_episode.values()),
            key=lambda value: value[0],
            reverse=True,
        )
    return scored


def _subtitle_release_summary(references: tuple) -> dict[str, object]:
    counts = Counter(
        str(getattr(reference, "release_match", "unresolved"))
        for reference in references
    )
    episode_ids = {
        (reference.episode_info.season, reference.episode_info.episode)
        for reference in references
        if reference.episode_info is not None
    }
    profiles = sorted({
        str(reference.release_profile)
        for reference in references
        if getattr(reference, "release_profile", None)
    })
    return {
        "subtitle_reference_variant_count": len(references),
        "subtitle_reference_episode_count": len(episode_ids),
        "subtitle_release_profile": ",".join(profiles) or "unresolved",
        "subtitle_exact_reference_count": counts.get("exact", 0),
        "subtitle_compatible_reference_count": counts.get("compatible", 0),
        "subtitle_generic_reference_count": counts.get("generic", 0),
        "subtitle_unresolved_reference_count": counts.get("unresolved", 0),
    }


def _missing_subtitle_reference_audit(
    files,
    references,
    catalog,
    season,
    *,
    phase,
):
    """Record aired options that could not be scored for lack of a reference."""

    altered_cut = _references_use_altered_cut(tuple(references))
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
                    item.duration_seconds,
                    entry,
                    altered_cut=altered_cut,
                ),
                "disposition": "rejected",
                "reason": (
                    "subtitle_reference_unavailable"
                    if _episode_runtime_consistent(
                        item.duration_seconds,
                        entry,
                        altered_cut=altered_cut,
                    )
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


def _merge_subtitle_candidate_rankings(
    *rankings: list[tuple[float, int, EpisodeCatalogEntry]],
) -> list[tuple[float, int, EpisodeCatalogEntry]]:
    """Keep the strongest independently scored reference for each episode."""

    by_episode: dict[str, tuple[float, int, EpisodeCatalogEntry]] = {}
    for ranking in rankings:
        for candidate in ranking:
            prior = by_episode.get(candidate[2].episode_id)
            if prior is None or candidate[:2] > prior[:2]:
                by_episode[candidate[2].episode_id] = candidate
    return sorted(by_episode.values(), key=lambda value: value[0], reverse=True)


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
    analysis_progress: _SubtitleAnalysisProgress | None = None,
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
        if analysis_progress is not None:
            analysis_progress.checkpoint()
        references = reference_cache.get(season)
        if references is None:
            references = tuple(provider.get_subtitles(series_name, season, [], tmdb_id))
            reference_cache[season] = references
        if analysis_progress is not None:
            analysis_progress.checkpoint()
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
            analysis_progress=analysis_progress,
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
    release_failover_attempted: set[int] | None = None,
    allow_release_failover: bool = True,
    analysis_progress: _SubtitleAnalysisProgress | None = None,
) -> tuple[dict[str, EpisodeCatalogEntry], dict[str, tuple[float, float]]]:
    """Match cached Whisper excerpts independently against bounded season subtitles."""

    if not seasons or not files:
        return {}, {}
    provider = provider or provider_factory()
    reference_cache = reference_cache if reference_cache is not None else {}
    release_failover_attempted = (
        release_failover_attempted if release_failover_attempted is not None else set()
    )
    catalog_by_number = {(entry.season, entry.episode): entry for entry in catalog}
    remaining = {item.file_id: item for item in files}
    accepted = {}
    diagnostics = {}
    used = set()
    residual_scored: dict[str, list[tuple[float, int, EpisodeCatalogEntry]]] = {}
    initial_scored_by_season: dict[
        int, dict[str, list[tuple[float, int, EpisodeCatalogEntry]]]
    ] = {}
    release_ladder_seasons: set[int] = set()
    for season in seasons:
        if not remaining:
            break
        if analysis_progress is not None:
            analysis_progress.checkpoint()
        references = reference_cache.get(season)
        if references is None:
            references = tuple(provider.get_subtitles(series_name, season, [], tmdb_id))
            reference_cache[season] = references
        if len(seasons) == 1 and _references_use_altered_cut(references):
            release_ladder_seasons.add(season)
        scoring_references = (
            _release_ladder_references(references, "altered")
            if season in release_ladder_seasons
            else references
        )
        direct_phase = (
            "season-release-altered" if season in release_ladder_seasons else "direct"
        )
        if analysis_progress is not None:
            analysis_progress.checkpoint()
        rejected_candidates = _missing_subtitle_reference_audit(
            tuple(remaining.values()),
            scoring_references,
            catalog,
            season,
            phase=direct_phase,
        )
        scored = _subtitle_candidates(
            tuple(remaining.values()),
            scoring_references,
            catalog_by_number,
            asr,
            min_confidence=min_confidence,
            rejected_candidates=rejected_candidates,
            phase=direct_phase,
            analysis_progress=analysis_progress,
        )
        initial_scored_by_season[season] = scored
        for file_id, candidates in scored.items():
            residual_scored[file_id] = _merge_subtitle_candidate_rankings(
                residual_scored.get(file_id, []), candidates
            )
        season_matches, season_diagnostics = _accept_subtitle_candidates(
            scored,
            min_confidence,
            used,
            diagnostic_details=diagnostic_details,
        )
        if season in release_ladder_seasons and diagnostic_details is not None:
            for file_id in scored:
                if file_id in diagnostic_details:
                    diagnostic_details[file_id]["subtitle_reference_pass"] = (
                        "season-release-altered"
                    )
        accepted.update(season_matches)
        diagnostics.update(season_diagnostics)
        if candidate_evaluations is not None:
            details = diagnostic_details or {}
            scored_audit = _scored_candidate_audit(
                scored, season_matches, details, phase=direct_phase
            )
            for file_id in {**rejected_candidates, **scored_audit}:
                candidate_evaluations.setdefault(file_id, []).extend(
                    rejected_candidates.get(file_id, [])
                )
                candidate_evaluations[file_id].extend(scored_audit.get(file_id, []))
        for file_id in season_matches:
            remaining.pop(file_id, None)
    # Only broaden provider releases after every normal season reference has
    # failed for the remaining titles. This prevents an alternate cut from one
    # season pre-empting a valid normal reference in the neighboring season.
    for season in seasons:
        if not remaining:
            break
        if analysis_progress is not None:
            analysis_progress.checkpoint()
        alternate_getter = getattr(provider, "get_alternate_subtitles", None)
        should_try_alternates = (
            allow_release_failover
            and bool(remaining)
            and season not in release_failover_attempted
            and tmdb_id > 0
            and callable(alternate_getter)
            and any(item.transcript_excerpts for item in remaining.values())
        )
        if should_try_alternates:
            release_failover_attempted.add(season)
            alternate_failure_reason = None
            try:
                alternate_references = tuple(
                    alternate_getter(series_name, season, [], tmdb_id)
                )
            except Exception as exc:
                logger.info(
                    "Alternate subtitle release lookup failed safely: {}",
                    type(exc).__name__,
                )
                alternate_failure_reason = "alternate_release_lookup_failed"
                alternate_references = ()
            if analysis_progress is not None:
                analysis_progress.checkpoint()
            if diagnostic_details is not None:
                for file_id in remaining:
                    diagnostic_details.setdefault(file_id, {})[
                        "subtitle_reference_pass"
                    ] = "alternate-release-failover"
            if not alternate_references and candidate_evaluations is not None:
                for file_id in remaining:
                    candidate_evaluations.setdefault(file_id, []).append({
                        "phase": "alternate-release-failover",
                        "candidate_episode_id": None,
                        "candidate_episode_title": None,
                        "season": season,
                        "episode": None,
                        "disposition": "review",
                        "reason": alternate_failure_reason
                        or "no_untested_subtitle_references",
                    })
            if alternate_references:
                if len(seasons) == 1 and _references_use_altered_cut(
                    alternate_references
                ):
                    release_ladder_seasons.add(season)
                references = (
                    tuple(reference_cache.get(season, ())) + alternate_references
                )
                reference_cache[season] = references
                alternate_scoring_references = (
                    _release_ladder_references(alternate_references, "altered")
                    if season in release_ladder_seasons
                    else alternate_references
                )
                failover_rejected = _missing_subtitle_reference_audit(
                    tuple(remaining.values()),
                    alternate_scoring_references,
                    catalog,
                    season,
                    phase="alternate-release-failover",
                )
                failover_scored = _subtitle_candidates(
                    tuple(remaining.values()),
                    alternate_scoring_references,
                    catalog_by_number,
                    asr,
                    min_confidence=min_confidence,
                    rejected_candidates=failover_rejected,
                    phase="alternate-release-failover",
                    analysis_progress=analysis_progress,
                )
                combined_scored = {}
                for file_id in remaining:
                    combined_scored[file_id] = _merge_subtitle_candidate_rankings(
                        initial_scored_by_season.get(season, {}).get(file_id, []),
                        failover_scored.get(file_id, []),
                    )
                    residual_scored[file_id] = _merge_subtitle_candidate_rankings(
                        residual_scored.get(file_id, []),
                        failover_scored.get(file_id, []),
                    )
                failover_matches, failover_diagnostics = _accept_subtitle_candidates(
                    combined_scored,
                    min_confidence,
                    used,
                    diagnostic_details=diagnostic_details,
                )
                if diagnostic_details is not None:
                    for file_id in combined_scored:
                        if file_id in diagnostic_details:
                            diagnostic_details[file_id]["subtitle_reference_pass"] = (
                                "alternate-release-failover"
                            )
                accepted.update(failover_matches)
                diagnostics.update(failover_diagnostics)
                if candidate_evaluations is not None:
                    details = diagnostic_details or {}
                    failover_audit = _scored_candidate_audit(
                        combined_scored,
                        failover_matches,
                        details,
                        phase="alternate-release-failover",
                    )
                    for file_id in {**failover_rejected, **failover_audit}:
                        candidate_evaluations.setdefault(file_id, []).extend(
                            failover_rejected.get(file_id, [])
                        )
                        candidate_evaluations[file_id].extend(
                            failover_audit.get(file_id, [])
                        )
                for file_id in failover_matches:
                    remaining.pop(file_id, None)
    # A confidently known altered-cut season exhausts exact/compatible release
    # families before generic broadcast subtitles.  This keeps a regular cut
    # from pre-empting the intended Superfan/extended evidence while retaining
    # the generic season as a final same-season fallback.
    for season in seasons:
        if not remaining or season not in release_ladder_seasons:
            continue
        generic_references = _release_ladder_references(
            tuple(reference_cache.get(season, ())), "generic"
        )
        generic_rejected = _missing_subtitle_reference_audit(
            tuple(remaining.values()),
            generic_references,
            catalog,
            season,
            phase="season-release-generic",
        )
        generic_scored = _subtitle_candidates(
            tuple(remaining.values()),
            generic_references,
            catalog_by_number,
            asr,
            min_confidence=min_confidence,
            rejected_candidates=generic_rejected,
            phase="season-release-generic",
            analysis_progress=analysis_progress,
        )
        combined_scored = {}
        for file_id in remaining:
            combined_scored[file_id] = _merge_subtitle_candidate_rankings(
                initial_scored_by_season.get(season, {}).get(file_id, []),
                residual_scored.get(file_id, []),
                generic_scored.get(file_id, []),
            )
            residual_scored[file_id] = _merge_subtitle_candidate_rankings(
                residual_scored.get(file_id, []), generic_scored.get(file_id, [])
            )
        generic_matches, generic_diagnostics = _accept_subtitle_candidates(
            combined_scored,
            min_confidence,
            used,
            diagnostic_details=diagnostic_details,
        )
        if diagnostic_details is not None:
            for file_id in combined_scored:
                if file_id in diagnostic_details:
                    diagnostic_details[file_id]["subtitle_reference_pass"] = (
                        "season-release-generic"
                    )
        accepted.update(generic_matches)
        diagnostics.update(generic_diagnostics)
        if candidate_evaluations is not None:
            details = diagnostic_details or {}
            generic_audit = _scored_candidate_audit(
                combined_scored,
                generic_matches,
                details,
                phase="season-release-generic",
            )
            for file_id in {**generic_rejected, **generic_audit}:
                candidate_evaluations.setdefault(file_id, []).extend(
                    generic_rejected.get(file_id, [])
                )
                candidate_evaluations[file_id].extend(generic_audit.get(file_id, []))
        for file_id in generic_matches:
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
    source_duration: float,
    candidate: EpisodeCatalogEntry,
    *,
    altered_cut: bool = False,
) -> bool:
    """Reject obvious play-all titles while admitting known extended editions."""

    candidate_duration = candidate.runtime_seconds
    if candidate_duration is None or candidate_duration <= 0:
        return True
    maximum = (
        max(candidate_duration * 3.0, candidate_duration + 3600)
        if altered_cut
        else max(candidate_duration * 1.75, candidate_duration + 1800)
    )
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


def _rank_gemini_chunks(  # noqa: C901 - batched request/cache/audit boundary
    ranker: GeminiEpisodeRanker,
    files: tuple[UnmatchedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    dossier: IdentificationDossierStore,
    known_existing_ids: frozenset[str],
    reviewer_scene_descriptions: Mapping[str, str] | None = None,
    *,
    analysis_run_id: str | None = None,
    phase: str = "gemini",
    subtitle_comparisons: Mapping[str, tuple[GeminiSubtitleComparisonEvidence, ...]]
    | None = None,
    proposed_assignments: Mapping[str, str | None] | None = None,
) -> dict[str, object]:
    """Rank one complete unresolved-disc batch in a single provider call."""

    matches: dict[str, object] = {}
    chunk_notes = {
        item.file_id: reviewer_scene_descriptions[item.file_id]
        for item in files
        if reviewer_scene_descriptions and item.file_id in reviewer_scene_descriptions
    }
    chunk_catalog = _shortlist_catalog(
        files,
        catalog,
        set(),
        reviewer_scene_descriptions=chunk_notes,
    )
    if not chunk_catalog:
        raise PipelineQueueError("Local Gemini candidate shortlist is empty")
    chunk_ids = {entry.episode_id for entry in chunk_catalog}
    stable_attempts = {}
    for item in files:
        deduplicated = {}
        for attempt in dossier.safe_attempts(item.file_id):
            if attempt.get("branch") in {"tv-gemini", "gemini-synthesis"}:
                continue
            identity = json.dumps(
                {
                    "branch": attempt.get("branch"),
                    "disposition": attempt.get("disposition"),
                    "summary": attempt.get("summary"),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            deduplicated[identity] = attempt
        stable_attempts[item.file_id] = tuple(deduplicated.values())
    kwargs = {
        "prior_attempts": stable_attempts,
        "existing_episode_ids": frozenset(
            episode_id for episode_id in known_existing_ids if episode_id in chunk_ids
        ),
    }
    extended_parameters = signature(ranker.rank_with_configured_keys).parameters
    if "review_phase" in extended_parameters:
        kwargs["review_phase"] = (
            "confirmation" if phase == "gemini-confirmation" else "initial"
        )
    if chunk_notes:
        kwargs["reviewer_scene_descriptions"] = chunk_notes
    if subtitle_comparisons and "subtitle_comparisons" in extended_parameters:
        kwargs["subtitle_comparisons"] = {
            file_id: pairs
            for file_id, pairs in subtitle_comparisons.items()
            if file_id in {item.file_id for item in files}
        }
    if (
        proposed_assignments is not None
        and "proposed_assignments" in extended_parameters
    ):
        kwargs["proposed_assignments"] = proposed_assignments
    if analysis_run_id is not None and "transaction_recorder" in extended_parameters:
        kwargs["transaction_recorder"] = (
            lambda transaction: dossier.record_gemini_provider_transaction(
                analysis_run_id, transaction
            )
        )
    review = None
    request_digest = None
    if isinstance(dossier, IdentificationDossierStore):
        # The recorder is an execution-side diagnostic callback, not part of
        # the provider request. Passing it into the request builder both makes
        # an otherwise deterministic digest impossible and raises before the
        # provider can be called.
        digest_kwargs = {
            key: value for key, value in kwargs.items() if key != "transaction_recorder"
        }
        request_digest = gemini_request_digest(
            ranker.model, files, chunk_catalog, **digest_kwargs
        )
        review = dossier.load_gemini_review(request_digest, model=ranker.model)
        if review is not None:
            cached_file_ids = [match.file_id for match in review.matches]
            cached_episode_ids = [
                match.episode_id
                for match in review.matches
                if match.episode_id is not None
            ]
            if (
                set(cached_file_ids) != {item.file_id for item in files}
                or len(cached_file_ids) != len(files)
                or not set(cached_episode_ids).issubset(chunk_ids)
                or len(cached_episode_ids) != len(set(cached_episode_ids))
                or any(not 0 <= match.confidence <= 1 for match in review.matches)
            ):
                raise PipelineQueueError("Private Gemini cache is inconsistent")
    if review is None:
        review = ranker.rank_with_configured_keys(files, chunk_catalog, **kwargs)
        if request_digest is not None:
            dossier.save_gemini_review(request_digest, review)
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
                    for candidate_ordinal, entry in enumerate(chunk_catalog, start=1)
                ),
            )
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
    if isinstance(exc, GeminiAnalysisError):
        return exc
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


def _latest_disc_title_items(
    store: PipelineQueueStore, disc_fingerprint: str
) -> list[tuple[object, dict]]:
    latest_by_title = {}
    for item in store.list_items():
        try:
            payload = json.loads(
                store.rip_artifact(item.media_id).contract_path.read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError, PipelineQueueError) as exc:
            raise PipelineQueueError("Verified rip contract is unavailable") from exc
        if payload.get("disc_fingerprint") != disc_fingerprint:
            continue
        title_index = payload.get("title_index")
        if not isinstance(title_index, int) or isinstance(title_index, bool):
            continue
        previous = latest_by_title.get(title_index)
        if previous is None or (item.created_at, item.updated_at, item.media_id) >= (
            previous[0].created_at,
            previous[0].updated_at,
            previous[0].media_id,
        ):
            latest_by_title[title_index] = (item, payload)
    return [latest_by_title[index] for index in sorted(latest_by_title)]


def _selected_analysis_items(
    store: PipelineQueueStore, disc_fingerprint: str
) -> list[tuple[object, dict]]:
    return [
        entry
        for entry in _latest_disc_title_items(store, disc_fingerprint)
        if entry[0].stage == "identify" and entry[0].state == "review_required"
    ]


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
    provider_branches = {
        "tv-local",
        "tv-opensubtitles",
        "tv-gemini",
        "tv-play-all",
        "tv-movie",
        "movie-bonus",
        "tv-bonus",
        "gemini-synthesis",
    }
    return tuple(
        dict.fromkeys(branch for branch in branches if branch in provider_branches)
    )


def execute_unmatched_disc_analysis(
    store: PipelineQueueStore,
    disc_fingerprint: str,
    series_name: str,
    config: Config,
    asr,
    contract_root: Path,
    *,
    season: int | None = None,
    episode_range: tuple[int, int] | None = None,
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
            episode_range=episode_range,
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
    episode_range: tuple[int, int] | None = None,
    allow_gemini: bool = False,
    allow_content_fallback: bool = True,
    reviewer_scene_descriptions: Mapping[str, str] | None = None,
    analysis_run_id: str,
) -> tuple[str, ...]:
    """Transcribe held titles and compare them to the reviewed episode scope."""

    disc_context_items = _latest_disc_title_items(store, disc_fingerprint)
    selected = [
        entry
        for entry in disc_context_items
        if entry[0].stage == "identify" and entry[0].state == "review_required"
    ]
    if not selected:
        raise PipelineQueueError(
            "At least one held title is required for disc sequence analysis"
        )
    selected_ids = {item.media_id for item, _payload in selected}
    media_ids = tuple(item.media_id for item, _payload in selected)
    candidate_scope_label = _reviewed_candidate_scope_label(season, episode_range)
    reviewed_series_name = series_name
    subtitle_series_name, subtitle_release_profile = _subtitle_lookup_series_name(
        series_name,
        disc_context_items,
        reviewed_series_name=reviewed_series_name,
    )
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
            "reviewed_candidate_scope": candidate_scope_label,
            "subtitle_release_profile": subtitle_release_profile or "unresolved",
            "subtitle_analysis_mode": "finite-progress-no-total-deadline",
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
    subtitle_series_name, subtitle_release_profile = _subtitle_lookup_series_name(
        series_name,
        disc_context_items,
        reviewed_series_name=reviewed_series_name,
    )
    all_series_catalog = catalog
    catalog = _reviewed_episode_catalog(catalog, season, episode_range)
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
    subtitle_analysis_progress = _SubtitleAnalysisProgress()
    subtitle_provider = None
    subtitle_reference_cache: dict[int, tuple] = {}
    subtitle_release_failover_attempted: set[int] = set()
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
                        subtitle_series_name,
                        selected_series.tmdb_id,
                        asr,
                        min_confidence=automatic_min_confidence,
                        provider=subtitle_provider,
                        reference_cache=subtitle_reference_cache,
                        season_order=preferred_season_order(catalog, known_episodes),
                        candidate_evaluations=anchor_candidate_evaluations,
                        audit_phase="anchor-season-discovery",
                        analysis_progress=subtitle_analysis_progress,
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
    disc_number = _disc_number_hint(selected)
    disc_episode_title_count = _disc_title_count(store, disc_fingerprint, selected)
    prioritized_candidate_catalog = _prioritize_catalog_for_disc_number(
        candidate_catalog, disc_number, disc_episode_title_count
    )
    gemini_catalog, gemini_candidate_scope = prioritize_missing_catalog(
        prioritized_candidate_catalog, known_episodes, len(selected)
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
        candidate_scope=candidate_scope_label,
    )
    catalog_by_id = {entry.episode_id: entry for entry in candidate_catalog}
    full_catalog_by_id = {entry.episode_id: entry for entry in catalog}
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
            and _episode_runtime_consistent(
                duration_by_id[media_id],
                candidate,
                altered_cut=subtitle_release_profile is not None,
            )
        )
        dossier.record_attempt(
            (media_id,),
            branch="tv-local",
            disposition="review",
            analysis_run_id=analysis_run_id,
            summary={
                "candidate_scope": candidate_scope_label,
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
    opensubtitles_candidate_evaluations: dict[str, list[dict[str, object]]] = {}
    if any(item.transcript_excerpts for item in gemini_evidence):
        try:
            subtitle_provider = subtitle_provider or OpenSubtitlesProvider()
            if not season_scope:
                season_scope = discover_opensubtitles_season(
                    gemini_evidence,
                    candidate_catalog,
                    subtitle_series_name,
                    selected_series.tmdb_id,
                    asr,
                    min_confidence=automatic_min_confidence,
                    provider=subtitle_provider,
                    reference_cache=subtitle_reference_cache,
                    season_order=preferred_season_order(catalog, known_episodes),
                    candidate_evaluations=opensubtitles_candidate_evaluations,
                    audit_phase="full-season-discovery",
                    analysis_progress=subtitle_analysis_progress,
                )
            opensubtitles_details: dict[str, dict[str, object]] = {}
            proposed, opensubtitles_diagnostics = match_opensubtitles_seasons(
                gemini_evidence,
                candidate_catalog,
                subtitle_series_name,
                selected_series.tmdb_id,
                season_scope,
                asr,
                min_confidence=automatic_min_confidence,
                provider=subtitle_provider,
                reference_cache=subtitle_reference_cache,
                diagnostic_details=opensubtitles_details,
                prior_disc_assignments=bool(disc_episodes),
                candidate_evaluations=opensubtitles_candidate_evaluations,
                release_failover_attempted=subtitle_release_failover_attempted,
                analysis_progress=subtitle_analysis_progress,
            )
            unresolved_ids = {
                item.file_id for item in gemini_evidence if item.file_id not in proposed
            }
            first_pass_window_counts = {
                item.file_id: len(item.transcript_excerpts) for item in gemini_evidence
            }
            if (
                unresolved_ids
                and isinstance(dossier, IdentificationDossierStore)
                and any(
                    1 <= len(item.transcript_excerpts) < 12
                    for item in gemini_evidence
                    if item.file_id in unresolved_ids
                )
            ):
                supplemental_inputs = tuple(
                    pair
                    for pair in selected
                    if getattr(pair[0], "media_id", None) in unresolved_ids
                )
                supplemental_existing = tuple(
                    item for item in gemini_evidence if item.file_id in unresolved_ids
                )
                supplemental_evidence, dossier = collect_supplemental_dossier_evidence(
                    supplemental_inputs,
                    supplemental_existing,
                    config,
                    asr,
                    contract_root,
                )
                if any(
                    len(item.transcript_excerpts)
                    > first_pass_window_counts.get(item.file_id, 0)
                    for item in supplemental_evidence
                ):
                    proposed_episode_ids = {
                        entry.episode_id for entry in proposed.values()
                    }
                    narrowed_supplemental_catalog = tuple(
                        entry
                        for entry in candidate_catalog
                        if entry.episode_id not in proposed_episode_ids
                    )
                    supplemental_details: dict[str, dict[str, object]] = {}
                    supplemental_evaluations: dict[str, list[dict[str, object]]] = {}
                    supplemental_matches, supplemental_diagnostics = (
                        match_opensubtitles_seasons(
                            supplemental_evidence,
                            narrowed_supplemental_catalog,
                            subtitle_series_name,
                            selected_series.tmdb_id,
                            season_scope,
                            asr,
                            min_confidence=automatic_min_confidence,
                            provider=subtitle_provider,
                            reference_cache=subtitle_reference_cache,
                            diagnostic_details=supplemental_details,
                            prior_disc_assignments=bool(disc_episodes or proposed),
                            candidate_evaluations=supplemental_evaluations,
                            release_failover_attempted=(
                                subtitle_release_failover_attempted
                            ),
                            analysis_progress=subtitle_analysis_progress,
                        )
                    )
                    proposed.update(supplemental_matches)
                    opensubtitles_diagnostics.update(supplemental_diagnostics)
                    opensubtitles_details.update(supplemental_details)
                    for file_id, evaluations in supplemental_evaluations.items():
                        for evaluation in evaluations:
                            evaluation["phase"] = "offset-six-window-retry"
                        opensubtitles_candidate_evaluations.setdefault(
                            file_id, []
                        ).extend(evaluations)
                    gemini_evidence = tuple(
                        next(
                            (
                                retry
                                for retry in supplemental_evidence
                                if retry.file_id == item.file_id
                            ),
                            item,
                        )
                        for item in gemini_evidence
                    )
            for file_id in proposed:
                assignment_evidence[file_id] = (
                    OPENSUBTITLES_RESIDUAL_SOURCE
                    if opensubtitles_details.get(file_id, {}).get("reason")
                    == "accepted_after_disc_residual_reduction"
                    else OPENSUBTITLES_TWO_WINDOW_SOURCE
                )
            subtitle_release_summary = _subtitle_release_summary(
                tuple(
                    reference
                    for scoped_season in season_scope
                    for reference in subtitle_reference_cache.get(scoped_season, ())
                )
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
                        "candidate_scope": candidate_scope_label,
                        "best_score": round(score, 6),
                        "runner_up_score": round(
                            float(details.get("runner_up_score", 0.0)), 6
                        ),
                        "margin": round(margin, 6),
                        "qualifying_window_count": int(
                            details.get("qualifying_window_count", 0)
                        ),
                        "transcript_window_count": len(item.transcript_excerpts),
                        "subtitle_pass": (
                            details.get("subtitle_reference_pass")
                            or (
                                "offset-six-window-retry"
                                if len(item.transcript_excerpts)
                                > first_pass_window_counts.get(item.file_id, 0)
                                else "initial-six-window"
                            )
                        ),
                        "candidate_episode_id": details.get("candidate_episode_id"),
                        "candidate_episode_title": (
                            subtitle_candidate.title
                            if subtitle_candidate is not None
                            else None
                        ),
                        "candidate_series_name": series_name,
                        **subtitle_release_summary,
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
    subtitle_references = tuple(
        reference
        for scoped_season in season_scope
        for reference in subtitle_reference_cache.get(scoped_season, ())
    )
    gemini_subtitle_comparisons = _gemini_subtitle_comparisons(
        unresolved_for_gemini,
        subtitle_references,
        opensubtitles_candidate_evaluations,
        asr,
        analysis_progress=subtitle_analysis_progress,
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
        gemini_subtitle_comparisons = _filter_gemini_subtitle_comparisons(
            gemini_subtitle_comparisons, remaining_gemini_catalog
        )
        unresolved_for_gemini = _select_gemini_file_evidence(
            unresolved_for_gemini, gemini_subtitle_comparisons
        )
        effective_gemini_scope = (
            disc_range_fence.scope
            if disc_range_fence is not None
            else candidate_scope_label
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
                subtitle_comparisons=gemini_subtitle_comparisons,
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
                subtitle_comparisons=gemini_subtitle_comparisons,
                proposed_assignments={
                    file_id: match.episode_id
                    for file_id, match in initial_matches.items()
                },
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
                    altered_cut=subtitle_release_profile is not None,
                )
            }
            gemini_entries = tuple(
                gemini_catalog_by_id[str(match.episode_id)]
                for match in resolved_matches.values()
            )
            # Gemini evaluates each title independently. Before any of those
            # choices may advance, validate the complete proposed disc set,
            # including independently established sibling assignments. This
            # guard must also run when the saved context has no explicit
            # season: a missing season is not permission for one disc to span
            # multiple seasons or a wider episode range than its title count.
            combined_disc_entries = tuple(proposed.values()) + gemini_entries
            if not _disc_assignments_coherent(
                combined_disc_entries,
                disc_title_count=disc_episode_title_count,
                required_season=season,
            ):
                dossier.record_attempt(
                    tuple(resolved_matches),
                    branch="tv-disc-range",
                    disposition="review",
                    analysis_run_id=analysis_run_id,
                    summary={
                        "candidate_scope": effective_gemini_scope,
                        "candidate_count": len(combined_disc_entries),
                        "gemini_candidate_count": len(gemini_entries),
                        "disc_title_count": disc_episode_title_count,
                        "requested_season": season,
                        "disc_number": disc_number,
                        "reason": "gemini_disc_assignments_incoherent",
                    },
                )
                resolved_matches = {}
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
                        altered_cut=subtitle_release_profile is not None,
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
                if gemini_entries:
                    raise DiscCoherenceError()
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
                # Leaving an explicit season is a local, review-only escape hatch.
                # It must not spend another Gemini call or become an automatic
                # assignment.
                if season is not None:
                    outside_catalog = tuple(
                        entry
                        for entry in all_series_catalog
                        if entry.season != season
                        and (entry.season, entry.episode) not in known_episodes
                    )
                    if outside_catalog:
                        try:
                            outside_seasons = tuple(
                                sorted({entry.season for entry in outside_catalog})
                            )
                            outside_details: dict[str, dict[str, object]] = {}
                            outside_evaluations: dict[str, list[dict[str, object]]] = {}
                            outside_source = tuple(
                                item
                                for item in gemini_evidence
                                if item.file_id
                                in {value.file_id for value in unresolved_for_gemini}
                            )
                            _outside_matches, outside_diagnostics = (
                                match_opensubtitles_seasons(
                                    outside_source,
                                    outside_catalog,
                                    series_name,
                                    selected_series.tmdb_id,
                                    outside_seasons,
                                    asr,
                                    min_confidence=automatic_min_confidence,
                                    provider=subtitle_provider,
                                    reference_cache=subtitle_reference_cache,
                                    diagnostic_details=outside_details,
                                    candidate_evaluations=outside_evaluations,
                                    allow_release_failover=False,
                                    analysis_progress=subtitle_analysis_progress,
                                )
                            )
                            outside_by_id = {
                                entry.episode_id: entry for entry in outside_catalog
                            }
                            for item in outside_source:
                                details = outside_details.get(item.file_id, {})
                                candidate_id = details.get("candidate_episode_id")
                                candidate = outside_by_id.get(str(candidate_id))
                                score, margin = outside_diagnostics.get(
                                    item.file_id, (0.0, 0.0)
                                )
                                dossier.record_attempt(
                                    (item.file_id,),
                                    branch="tv-opensubtitles",
                                    disposition="review",
                                    analysis_run_id=analysis_run_id,
                                    summary={
                                        "phase": "outside-season-review",
                                        "candidate_scope": (
                                            f"outside-S{season:02d}-review-only"
                                        ),
                                        "candidate_episode_id": candidate_id,
                                        "candidate_episode_title": (
                                            candidate.title if candidate else None
                                        ),
                                        "candidate_series_name": series_name,
                                        "best_score": round(score, 6),
                                        "margin": round(margin, 6),
                                        "reason": ("outside_explicit_season_boundary"),
                                    },
                                )
                                _record_candidate_audit_safely(
                                    dossier,
                                    item.file_id,
                                    analysis_run_id=analysis_run_id,
                                    branch="tv-opensubtitles",
                                    evaluations=tuple(
                                        {
                                            **evaluation,
                                            "phase": "outside-season-review",
                                            "disposition": "rejected",
                                            "reason": (
                                                "outside_explicit_season_boundary"
                                            ),
                                        }
                                        for evaluation in outside_evaluations.get(
                                            item.file_id, []
                                        )
                                    ),
                                )
                        except Exception as outside_exc:
                            dossier.record_attempt(
                                tuple(item.file_id for item in unresolved_for_gemini),
                                branch="tv-opensubtitles",
                                disposition="failed",
                                analysis_run_id=analysis_run_id,
                                summary={
                                    "reason": (
                                        "outside_season_review_failed:"
                                        f"{type(outside_exc).__name__}"
                                    )
                                },
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
    combined_disc_entries = _combined_disc_assignments(
        store,
        disc_fingerprint,
        selected,
        proposed,
        full_catalog_by_id,
    )
    if not _disc_assignments_coherent(
        combined_disc_entries,
        disc_title_count=disc_episode_title_count,
        required_season=season,
    ):
        dossier.record_attempt(
            tuple(proposed),
            branch="tv-disc-range",
            disposition="review",
            analysis_run_id=analysis_run_id,
            summary={
                "candidate_count": len(combined_disc_entries),
                "disc_title_count": disc_episode_title_count,
                "requested_season": season,
                "disc_number": disc_number,
                "reason": "whole_disc_assignments_incoherent",
            },
        )
        raise DiscCoherenceError()
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
