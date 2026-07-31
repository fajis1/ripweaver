"""Plan-only matching primitives for media whose season is not yet known.

This module deliberately accepts redacted evidence rather than media paths. Audio
extraction, transcription, rename, and movement remain separate operations.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from mkv_episode_matcher.media.subtitle_ocr import parse_srt_text


@dataclass(frozen=True, order=True)
class EpisodeRef:
    season: int
    episode: int
    title: str | None = None
    runtime_seconds: float | None = None

    @property
    def key(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"


@dataclass(frozen=True)
class SubtitleWindow:
    episode: EpisodeRef
    text: str


@dataclass(frozen=True)
class ReferenceRetrievalCandidate:
    episode_key: str
    retrieval_score: float
    bm25_score: float
    query_term_coverage: float
    overlapping_terms: int


@dataclass(frozen=True)
class FileEvidence:
    """Redacted evidence for one input, ordered as it appeared on the disc."""

    file_id: str
    duration_seconds: float
    text_scores: dict[str, float]


@dataclass(frozen=True)
class CandidateScore:
    episode: EpisodeRef
    text_score: float
    runtime_score: float
    combined_score: float


@dataclass(frozen=True)
class PlannedUnmatchedItem:
    file_id: str
    proposed_episode: str | None
    confidence: float
    margin: float
    disposition: str
    candidates: tuple[CandidateScore, ...]


@dataclass(frozen=True)
class UnmatchedPlan:
    mode: str
    created_at: str
    series_name: str
    items: tuple[PlannedUnmatchedItem, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "created_at": self.created_at,
            "series_name": self.series_name,
            "items": [asdict(item) for item in self.items],
        }


_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "in",
    "is",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "with",
}


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9']+", text.lower())
        if token not in _STOP_WORDS
    ]


def bm25_episode_scores(
    query: str,
    windows: list[SubtitleWindow],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> dict[str, float]:
    """Return normalized per-episode BM25 retrieval scores.

    Each subtitle window is one document. An episode receives the score of its
    best matching window, which lets one transcript retrieve a short candidate
    list without comparing it to every full subtitle using fuzzy matching.
    """

    query_terms = set(_tokens(query))
    if not query_terms or not windows:
        return {}
    documents = [_tokens(window.text) for window in windows]
    average_length = sum(map(len, documents)) / len(documents)
    if average_length <= 0:
        return {}

    document_frequency: Counter[str] = Counter()
    for document in documents:
        document_frequency.update(set(document))

    raw_by_episode: dict[str, float] = defaultdict(float)
    total_documents = len(documents)
    for window, document in zip(windows, documents, strict=True):
        frequencies = Counter(document)
        document_length = len(document)
        score = 0.0
        for term in query_terms:
            frequency = frequencies[term]
            if not frequency:
                continue
            frequency_in_documents = document_frequency[term]
            inverse_document_frequency = math.log(
                1
                + (total_documents - frequency_in_documents + 0.5)
                / (frequency_in_documents + 0.5)
            )
            denominator = frequency + k1 * (
                1 - b + b * document_length / average_length
            )
            score += inverse_document_frequency * (frequency * (k1 + 1) / denominator)
        raw_by_episode[window.episode.key] = max(
            raw_by_episode[window.episode.key],
            score,
        )

    maximum = max(raw_by_episode.values(), default=0.0)
    if maximum <= 0:
        return {}
    return {
        episode_key: score / maximum
        for episode_key, score in raw_by_episode.items()
        if score > 0
    }


_EPISODE_KEY = re.compile(r"(?i)\bS(\d{1,2})E(\d{1,3})\b")


def load_srt_reference_windows(
    root: Path,
    *,
    window_words: int = 120,
    stride_words: int = 60,
) -> list[SubtitleWindow]:
    """Load path-free episode windows from an explicit subtitle-cache root."""

    if not root.is_dir():
        raise ValueError("Reference root must be an existing directory")
    if window_words <= 0 or stride_words <= 0 or stride_words > window_words:
        raise ValueError("Reference window and stride must be positive and ordered")

    windows: list[SubtitleWindow] = []
    for path in sorted(root.rglob("*.srt")):
        match = _EPISODE_KEY.search(path.stem)
        if match is None:
            continue
        episode = EpisodeRef(int(match.group(1)), int(match.group(2)))
        content = path.read_text(encoding="utf-8-sig", errors="replace")
        text, _ = parse_srt_text(content)
        words = text.split()
        if not words:
            continue
        if len(words) <= window_words:
            windows.append(SubtitleWindow(episode, text))
            continue
        for start in range(0, len(words), stride_words):
            chunk = words[start : start + window_words]
            if not chunk:
                break
            windows.append(SubtitleWindow(episode, " ".join(chunk)))
            if start + window_words >= len(words):
                break
    return windows


def rank_reference_query(
    query: str,
    windows: list[SubtitleWindow],
    *,
    top_k: int = 10,
) -> tuple[ReferenceRetrievalCandidate, ...]:
    """Rank cached episode references using BM25 plus query-term coverage."""

    if top_k <= 0:
        raise ValueError("top_k must be positive")
    query_terms = set(_tokens(query))
    if not query_terms:
        return ()

    bm25_scores = bm25_episode_scores(query, windows)
    episode_terms: dict[str, set[str]] = defaultdict(set)
    for window in windows:
        episode_terms[window.episode.key].update(_tokens(window.text))

    candidates: list[ReferenceRetrievalCandidate] = []
    for episode_key, bm25_score in bm25_scores.items():
        overlapping = query_terms & episode_terms[episode_key]
        coverage = len(overlapping) / len(query_terms)
        candidates.append(
            ReferenceRetrievalCandidate(
                episode_key=episode_key,
                retrieval_score=0.70 * bm25_score + 0.30 * coverage,
                bm25_score=bm25_score,
                query_term_coverage=coverage,
                overlapping_terms=len(overlapping),
            )
        )
    candidates.sort(
        key=lambda candidate: (
            candidate.retrieval_score,
            candidate.query_term_coverage,
            candidate.bm25_score,
            candidate.episode_key,
        ),
        reverse=True,
    )
    return tuple(candidates[:top_k])


def runtime_similarity(
    media_seconds: float,
    episode_seconds: float | None,
    *,
    sigma_seconds: float = 180.0,
) -> float:
    """Score runtime proximity with a Gaussian curve."""

    if episode_seconds is None:
        return 0.5
    delta = media_seconds - episode_seconds
    return math.exp(-0.5 * (delta / sigma_seconds) ** 2)


def rank_candidates(
    evidence: FileEvidence,
    episodes: list[EpisodeRef],
    *,
    top_k: int = 5,
    text_weight: float = 0.85,
    runtime_weight: float = 0.15,
    maximum_runtime_delta: float = 900.0,
) -> tuple[CandidateScore, ...]:
    """Prune and rank episode candidates using text and runtime evidence."""

    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if not math.isclose(text_weight + runtime_weight, 1.0):
        raise ValueError("candidate weights must sum to one")

    ranked: list[CandidateScore] = []
    for episode in episodes:
        if (
            episode.runtime_seconds is not None
            and abs(evidence.duration_seconds - episode.runtime_seconds)
            > maximum_runtime_delta
        ):
            continue
        text_score = max(0.0, min(1.0, evidence.text_scores.get(episode.key, 0.0)))
        runtime_score = runtime_similarity(
            evidence.duration_seconds,
            episode.runtime_seconds,
        )
        combined_score = text_weight * text_score + runtime_weight * runtime_score
        ranked.append(
            CandidateScore(
                episode=episode,
                text_score=text_score,
                runtime_score=runtime_score,
                combined_score=combined_score,
            )
        )
    ranked.sort(
        key=lambda candidate: (
            candidate.combined_score,
            candidate.text_score,
            -candidate.episode.season,
            -candidate.episode.episode,
        ),
        reverse=True,
    )
    return tuple(ranked[:top_k])


def _ordered_assignment(
    candidates: list[tuple[CandidateScore, ...]],
    episodes: list[EpisodeRef],
    *,
    minimum_score: float,
) -> list[str | None]:
    """Find the highest-scoring one-to-one assignment preserving episode order."""

    episode_keys = [episode.key for episode in sorted(episodes)]
    rows = len(candidates)
    columns = len(episode_keys)
    score_maps = [
        {candidate.episode.key: candidate.combined_score for candidate in row}
        for row in candidates
    ]
    scores = [[0.0] * (columns + 1) for _ in range(rows + 1)]
    matched = [[0] * (columns + 1) for _ in range(rows + 1)]
    operation = [[""] * (columns + 1) for _ in range(rows + 1)]

    for row in range(1, rows + 1):
        operation[row][0] = "unmatched"
    for column in range(1, columns + 1):
        operation[0][column] = "skip_episode"

    for row in range(1, rows + 1):
        for column in range(1, columns + 1):
            options = [
                (
                    scores[row][column - 1],
                    matched[row][column - 1],
                    "skip_episode",
                ),
                (
                    scores[row - 1][column],
                    matched[row - 1][column],
                    "unmatched",
                ),
            ]
            candidate_score = score_maps[row - 1].get(episode_keys[column - 1])
            if candidate_score is not None and candidate_score >= minimum_score:
                options.append((
                    scores[row - 1][column - 1] + candidate_score,
                    matched[row - 1][column - 1] + 1,
                    "match",
                ))
            best_score, best_count, best_operation = max(
                options,
                key=lambda item: (item[1], item[0], item[2] == "match"),
            )
            scores[row][column] = best_score
            matched[row][column] = best_count
            operation[row][column] = best_operation

    assignments: list[str | None] = [None] * rows
    row, column = rows, columns
    while row > 0 or column > 0:
        current = operation[row][column]
        if current == "match":
            assignments[row - 1] = episode_keys[column - 1]
            row -= 1
            column -= 1
        elif current == "skip_episode":
            column -= 1
        else:
            row -= 1
    return assignments


def plan_unmatched(
    series_name: str,
    files: list[FileEvidence],
    episodes: list[EpisodeRef],
    *,
    top_k: int = 5,
    minimum_score: float = 0.60,
    automatic_score: float = 0.75,
    automatic_margin: float = 0.08,
) -> UnmatchedPlan:
    """Create a non-mutating, disc-wide review plan."""

    if not series_name.strip():
        raise ValueError("series_name is required")
    if len({item.file_id for item in files}) != len(files):
        raise ValueError("file IDs must be unique")
    if len({episode.key for episode in episodes}) != len(episodes):
        raise ValueError("episode references must be unique")

    ranked = [rank_candidates(item, episodes, top_k=top_k) for item in files]
    assignments = _ordered_assignment(
        ranked,
        episodes,
        minimum_score=minimum_score,
    )
    items: list[PlannedUnmatchedItem] = []
    for evidence, candidates, assignment in zip(
        files,
        ranked,
        assignments,
        strict=True,
    ):
        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate.episode.key == assignment
            ),
            None,
        )
        if selected is None:
            confidence = 0.0
            margin = 0.0
            disposition = "review-unmatched"
        else:
            alternatives = [
                candidate.combined_score
                for candidate in candidates
                if candidate.episode.key != assignment
            ]
            confidence = selected.combined_score
            margin = confidence - max(alternatives, default=0.0)
            disposition = (
                "proposed"
                if confidence >= automatic_score and margin >= automatic_margin
                else "review-ambiguous"
            )
        items.append(
            PlannedUnmatchedItem(
                file_id=evidence.file_id,
                proposed_episode=assignment,
                confidence=confidence,
                margin=margin,
                disposition=disposition,
                candidates=candidates,
            )
        )

    return UnmatchedPlan(
        mode="unmatched-review-plan",
        created_at=datetime.now(UTC).isoformat(),
        series_name=series_name.strip(),
        items=tuple(items),
    )
