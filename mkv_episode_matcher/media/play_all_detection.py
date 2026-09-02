"""Metadata-only detection of joined/play-all episode representations."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class MatchedEpisodeEvidence:
    file_id: str
    season: int
    episode: int
    duration_seconds: float
    size_bytes: int | None


@dataclass(frozen=True)
class PlayAllEvidence:
    component_file_ids: tuple[str, ...]
    component_episode_ids: tuple[str, ...]
    duration_ratio: float
    size_ratio: float | None


def detect_play_all(
    *,
    candidate_file_id: str,
    candidate_duration_seconds: float,
    candidate_size_bytes: int | None,
    matched_episodes: tuple[MatchedEpisodeEvidence, ...],
) -> PlayAllEvidence | None:
    """Detect one unmatched title containing a contiguous matched episode run."""

    if candidate_duration_seconds <= 0 or len(matched_episodes) < 2:
        return None
    ordered = sorted(matched_episodes, key=lambda item: (item.season, item.episode))
    median_duration = median(item.duration_seconds for item in ordered)
    if candidate_duration_seconds < median_duration * 1.6:
        return None

    candidates: list[tuple[float, PlayAllEvidence]] = []
    for start in range(len(ordered)):
        for end in range(start + 2, len(ordered) + 1):
            group = ordered[start:end]
            if any(
                (left.season, left.episode + 1) != (right.season, right.episode)
                for left, right in zip(group, group[1:], strict=False)
            ):
                break
            expected_duration = sum(item.duration_seconds for item in group)
            duration_ratio = candidate_duration_seconds / expected_duration
            duration_error = abs(duration_ratio - 1.0)
            if duration_error > 0.05:
                continue
            component_sizes = [
                item.size_bytes
                for item in group
                if item.size_bytes is not None and item.size_bytes > 0
            ]
            size_ratio = None
            size_error = 0.0
            if candidate_size_bytes and len(component_sizes) == len(group):
                expected_size = sum(component_sizes)
                size_ratio = candidate_size_bytes / expected_size
                size_error = abs(size_ratio - 1.0)
                if size_error > 0.35:
                    continue
            evidence = PlayAllEvidence(
                component_file_ids=tuple(item.file_id for item in group),
                component_episode_ids=tuple(
                    f"S{item.season:02d}E{item.episode:02d}" for item in group
                ),
                duration_ratio=duration_ratio,
                size_ratio=size_ratio,
            )
            candidates.append((duration_error + size_error, evidence))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]
