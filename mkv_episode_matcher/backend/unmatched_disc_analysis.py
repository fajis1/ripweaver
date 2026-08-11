"""Disc-level all-season identification for seasonless television rips."""

from __future__ import annotations

import json
import re
from collections import Counter
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


def _subtitle_candidates(files, references, catalog_by_number, asr, *, min_confidence):
    scored = {}
    for item in files:
        candidates = []
        for subtitle in references:
            info = subtitle.episode_info
            if info is None:
                continue
            entry = catalog_by_number.get((info.season, info.episode))
            if entry is None or not _episode_runtime_consistent(
                item.duration_seconds, entry
            ):
                continue
            try:
                content = subtitle.content or SubtitleReader.read_srt_file(
                    subtitle.path
                )
            except (OSError, ValueError):
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


def _subtitle_candidate_rejection_reason(
    best_score: float,
    best_votes: int,
    margin: float,
    min_confidence: float,
) -> str | None:
    if best_score < min_confidence:
        return "below_confidence_threshold"
    if best_votes < 2 and best_score < 0.92:
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
        if (
            best_score >= min_confidence
            and (best_votes >= 2 or best_score >= 0.92)
            and margin >= 0.08
        ):
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


def discover_opensubtitles_season(
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
        scored = _subtitle_candidates(
            representatives,
            references,
            catalog_by_number,
            asr,
            min_confidence=min_confidence,
        )
        accepted, diagnostics = _accept_subtitle_candidates(
            scored, min_confidence, set()
        )
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


def match_opensubtitles_seasons(
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
    for season in seasons:
        if not remaining:
            break
        references = reference_cache.get(season)
        if references is None:
            references = tuple(provider.get_subtitles(series_name, season, [], tmdb_id))
            reference_cache[season] = references
        scored = _subtitle_candidates(
            tuple(remaining.values()),
            references,
            catalog_by_number,
            asr,
            min_confidence=min_confidence,
        )
        season_matches, season_diagnostics = _accept_subtitle_candidates(
            scored,
            min_confidence,
            used,
            diagnostic_details=diagnostic_details,
        )
        accepted.update(season_matches)
        diagnostics.update(season_diagnostics)
        for file_id in season_matches:
            remaining.pop(file_id, None)
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
) -> tuple[EpisodeCatalogEntry, ...]:
    selected: set[str] = set()
    available = tuple(entry for entry in catalog if entry.episode_id not in excluded)
    for item in files:
        ranked = rank_catalog_candidates(
            item.transcript_excerpts,
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
) -> dict[str, object]:
    matches: dict[str, object] = {}
    assigned: set[str] = set()
    for chunk in _gemini_chunks(files):
        chunk_catalog = _shortlist_catalog(chunk, catalog, assigned)
        if not chunk_catalog:
            raise PipelineQueueError("Local Gemini candidate shortlist is empty")
        chunk_ids = {entry.episode_id for entry in chunk_catalog}
        review = ranker.rank_with_configured_keys(
            chunk,
            chunk_catalog,
            prior_attempts={
                item.file_id: dossier.safe_attempts(item.file_id) for item in chunk
            },
            existing_episode_ids=frozenset(
                episode_id
                for episode_id in known_existing_ids
                if episode_id in chunk_ids
            ),
        )
        for match in review.matches:
            matches[match.file_id] = match
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
    validated = search_tv_show_candidates(resolution.series_name)
    if resolution.tmdb_id is not None:
        by_id = next(
            (item for item in validated if item.tmdb_id == resolution.tmdb_id),
            None,
        )
        if by_id is not None:
            return by_id
    validated_exact = _exact_series_candidates(resolution.series_name, validated)
    if len(validated_exact) == 1:
        return validated_exact[0]
    raise GeminiAnalysisError(
        "gemini_series_resolution_uncertain",
        "Gemini's proposed series name could not be validated through TMDb",
    )


def _resolve_series_catalog_details(
    series_name: str,
    config: Config,
    *,
    allow_gemini: bool,
) -> tuple[TvShowCandidate, tuple[EpisodeCatalogEntry, ...]]:
    """Resolve one canonical TV series and require a valid TMDb catalogue."""

    candidates = search_tv_show_candidates(series_name)
    exact = _exact_series_candidates(series_name, candidates)
    exact_is_canonical = len(exact) == 1 and not _has_packaging_series_markers(
        series_name
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
    if "at least two held titles" in message:
        return "selection", "insufficient_held_titles"
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
) -> tuple[str, ...]:
    """Run all-season analysis and retain safe telemetry for failed attempts."""

    started = perf_counter()
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
) -> tuple[str, ...]:
    """Transcribe held titles and compare them to the reviewed episode scope."""

    selected = _selected_analysis_items(store, disc_fingerprint)
    if len(selected) < 2:
        raise PipelineQueueError(
            "At least two held titles are required for disc sequence analysis"
        )

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
    dossier = None
    anchor_started = perf_counter()
    try:
        if not season_scope:
            anchor_evidence, dossier = collect_dossier_evidence(
                anchors, config, asr, contract_root
            )
            if any(item.transcript_excerpts for item in anchor_evidence):
                try:
                    subtitle_provider = OpenSubtitlesProvider()
                    season_scope = discover_opensubtitles_season(
                        anchor_evidence,
                        catalog,
                        series_name,
                        selected_series.tmdb_id,
                        asr,
                        min_confidence=config.min_confidence,
                        provider=subtitle_provider,
                        reference_cache=subtitle_reference_cache,
                        season_order=preferred_season_order(catalog, known_episodes),
                    )
                except Exception as exc:
                    logger.info(
                        "All-season anchor discovery was inconclusive: {}",
                        type(exc).__name__,
                    )
        if len(anchors) == len(selected):
            gemini_evidence = anchor_evidence
        else:
            gemini_evidence, dossier = collect_dossier_evidence(
                tuple(selected), config, asr, contract_root
            )
        if dossier is None:
            gemini_evidence, dossier = collect_dossier_evidence(
                tuple(selected), config, asr, contract_root
            )
    except TranscriptBatchError as exc:
        raise PipelineQueueError(
            "Audio evidence collection failed before episode matching"
        ) from exc
    anchor_elapsed_ms = round((perf_counter() - anchor_started) * 1000)
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
    media_ids = tuple(item.media_id for item, _payload in selected)
    catalog_by_id = {entry.episode_id: entry for entry in candidate_catalog}
    duration_by_id = {item.file_id: item.duration_seconds for item in gemini_evidence}
    provisional: dict[str, float] = {}
    opensubtitles_diagnostics: dict[str, tuple[float, float]] = {}
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
        proposed = {}
        if any(item.transcript_excerpts for item in gemini_evidence):
            try:
                subtitle_provider = subtitle_provider or OpenSubtitlesProvider()
                if not season_scope:
                    season_scope = discover_opensubtitles_season(
                        gemini_evidence,
                        candidate_catalog,
                        series_name,
                        selected_series.tmdb_id,
                        asr,
                        min_confidence=config.min_confidence,
                        provider=subtitle_provider,
                        reference_cache=subtitle_reference_cache,
                        season_order=preferred_season_order(catalog, known_episodes),
                    )
                opensubtitles_details: dict[str, dict[str, object]] = {}
                proposed, opensubtitles_diagnostics = match_opensubtitles_seasons(
                    gemini_evidence,
                    candidate_catalog,
                    series_name,
                    selected_series.tmdb_id,
                    season_scope,
                    asr,
                    min_confidence=config.min_confidence,
                    provider=subtitle_provider,
                    reference_cache=subtitle_reference_cache,
                    diagnostic_details=opensubtitles_details,
                )
                for item in gemini_evidence:
                    score, margin = opensubtitles_diagnostics.get(
                        item.file_id, (0.0, 0.0)
                    )
                    details = opensubtitles_details.get(item.file_id, {})
                    dossier.record_attempt(
                        (item.file_id,),
                        branch="tv-opensubtitles",
                        disposition=(
                            "matched" if item.file_id in proposed else "review"
                        ),
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
                            "candidate_episode_id": (
                                details.get("candidate_episode_id")
                            ),
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
                    summary={"reason": type(exc).__name__},
                )

    logger.info(
        "All-season local matching: titles={} season_scope={} proposed={} "
        "elapsed_ms={}",
        len(selected),
        ",".join(str(value) for value in season_scope) or "unresolved",
        len(proposed),
        round((perf_counter() - analysis_started) * 1000),
    )

    unresolved_for_gemini = tuple(
        item for item in gemini_evidence if item.file_id not in proposed
    )
    # Preserve independent subtitle matches, but let Gemini inspect unresolved
    # titles against only the established season scope and unassigned episodes.
    if unresolved_for_gemini and allow_gemini:
        proposed_episode_ids = {entry.episode_id for entry in proposed.values()}
        assigned_episode_ids = existing_episode_ids | frozenset(proposed_episode_ids)
        bounded_seasons = set(season_scope)
        remaining_gemini_catalog = tuple(
            entry
            for entry in gemini_catalog
            if entry.episode_id not in proposed_episode_ids
            and (not bounded_seasons or entry.season in bounded_seasons)
        )
        effective_gemini_scope = (
            ",".join(f"S{value:02d}" for value in season_scope)
            if season_scope
            else gemini_candidate_scope
        )
        try:
            dossier.record_attempt(
                tuple(item.file_id for item in unresolved_for_gemini),
                branch="tv-gemini",
                disposition="started",
                summary={
                    "candidate_scope": effective_gemini_scope,
                    "candidate_count": len(remaining_gemini_catalog),
                },
            )
            ranker = GeminiEpisodeRanker(model=config.gemini_model)
            initial_matches = _rank_gemini_chunks(
                ranker,
                unresolved_for_gemini,
                remaining_gemini_catalog,
                dossier,
                assigned_episode_ids,
            )
            if set(initial_matches) != {item.file_id for item in unresolved_for_gemini}:
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
            matches = _rank_gemini_chunks(
                ranker,
                unresolved_for_gemini,
                remaining_gemini_catalog,
                dossier,
                assigned_episode_ids,
            )
            if set(matches) != {item.file_id for item in unresolved_for_gemini}:
                raise PipelineQueueError("Gemini did not review every disc title")
            gemini_catalog_by_id = {
                entry.episode_id: entry for entry in remaining_gemini_catalog
            }
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
            proposed.update({
                file_id: gemini_catalog_by_id[str(match.episode_id)]
                for file_id, match in resolved_matches.items()
            })
            provisional.update({
                file_id: min(match.confidence, initial_matches[file_id].confidence)
                for file_id, match in resolved_matches.items()
            })
            for file_id, match in matches.items():
                dossier.record_attempt(
                    (file_id,),
                    branch="tv-gemini",
                    disposition="matched" if file_id in proposed else "review",
                    summary={
                        "phase": "confirmation",
                        "candidate_scope": effective_gemini_scope,
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
    elif not proposed:
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
                if (
                    item.state == "review_required"
                    and item.review_code != "visual_content_review_required"
                ):
                    store.choose_review_path(
                        media_id, "all_season_sequence_review_required"
                    )
    elif unresolved:
        for media_id in unresolved:
            store.choose_review_path(media_id, "all_season_sequence_review_required")
    final_unresolved = tuple(
        media_id
        for media_id in media_ids
        if media_id not in applied and media_id not in play_all_ids
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
