"""Saved-data-only contiguous disc-sequence matching.

The caller explicitly supplies both title order within each group and the
chronological order of groups. This module does not infer disc membership,
access media, contact providers, or mutate media.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from mkv_episode_matcher.media.episode_catalog import (
    EpisodeCatalogEntry,
    rank_catalog_candidates,
)
from mkv_episode_matcher.media.evidence_bundle import (
    SavedFileEvidence,
    select_transcript_excerpts,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class SequenceMatchError(RuntimeError):
    """Raised when a sequence plan cannot be produced safely."""


@dataclass(frozen=True)
class SequenceGroup:
    group_id: str
    file_ids: tuple[str, ...]


@dataclass(frozen=True)
class SequenceItemPlan:
    file_id: str
    proposed_episode: str
    lexical_score: float
    independent_top_episode: str
    independent_top_score: float


@dataclass(frozen=True)
class SequenceAlternativePlan:
    episode_ids: tuple[str, ...]
    mean_score: float


@dataclass(frozen=True)
class SequenceGroupPlan:
    group_id: str
    score: float
    local_margin: float
    disposition: str
    items: tuple[SequenceItemPlan, ...]
    local_alternatives: tuple[SequenceAlternativePlan, ...]


@dataclass(frozen=True)
class DiscSequencePlan:
    mode: str
    catalog_episode_count: int
    group_count: int
    file_count: int
    score: float
    global_margin: float
    disposition: str
    groups: tuple[SequenceGroupPlan, ...]

    def safe_report(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _Window:
    start: int
    end: int
    episode_ids: tuple[str, ...]
    item_scores: tuple[float, ...]

    @property
    def total_score(self) -> float:
        return sum(self.item_scores)

    @property
    def mean_score(self) -> float:
        return self.total_score / len(self.item_scores)


@dataclass(frozen=True)
class _Assignment:
    total_score: float
    windows: tuple[_Window, ...]


def parse_sequence_group_specs(specs: tuple[str, ...]) -> tuple[SequenceGroup, ...]:
    """Parse ordered ``GROUP=FILE,FILE`` declarations."""

    if not specs or len(specs) > 20:
        raise SequenceMatchError("Provide 1-20 explicit sequence groups")
    groups: list[SequenceGroup] = []
    for spec in specs:
        group_id, separator, raw_ids = spec.partition("=")
        file_ids = tuple(value.strip() for value in raw_ids.split(",") if value.strip())
        if (
            not separator
            or _SAFE_ID.fullmatch(group_id) is None
            or len(file_ids) < 2
            or len(file_ids) > 20
            or any(_SAFE_ID.fullmatch(file_id) is None for file_id in file_ids)
            or len(set(file_ids)) != len(file_ids)
        ):
            raise SequenceMatchError(
                "Sequence groups must use GROUP=FILE,FILE with 2-20 safe IDs"
            )
        groups.append(SequenceGroup(group_id, file_ids))
    if len({group.group_id for group in groups}) != len(groups):
        raise SequenceMatchError("Sequence group IDs must be unique")
    all_ids = [file_id for group in groups for file_id in group.file_ids]
    if len(set(all_ids)) != len(all_ids):
        raise SequenceMatchError("A file ID may appear in only one sequence group")
    return tuple(groups)


def _validate_inputs(
    files: tuple[SavedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    groups: tuple[SequenceGroup, ...],
) -> dict[str, SavedFileEvidence]:
    if not files or not catalog or not groups:
        raise SequenceMatchError(
            "Evidence, catalogue, and sequence groups are required"
        )
    by_id = {item.file_id: item for item in files}
    if len(by_id) != len(files):
        raise SequenceMatchError("Saved evidence file IDs must be unique")
    grouped_ids = tuple(file_id for group in groups for file_id in group.file_ids)
    if set(grouped_ids) != set(by_id):
        raise SequenceMatchError(
            "Explicit sequence groups must cover every saved evidence file exactly once"
        )
    if any(len(group.file_ids) > len(catalog) for group in groups):
        raise SequenceMatchError("A sequence group is longer than the catalogue")
    return by_id


def _score_maps(
    files: tuple[SavedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
) -> dict[str, dict[str, float]]:
    scores: dict[str, dict[str, float]] = {}
    for item in files:
        candidates = rank_catalog_candidates(
            select_transcript_excerpts(item.windows),
            item.duration_seconds,
            catalog,
            top_k=len(catalog),
        )
        if len(candidates) != len(catalog):
            raise SequenceMatchError("Local ranking did not cover the full catalogue")
        scores[item.file_id] = {
            candidate.episode_id: candidate.combined_score for candidate in candidates
        }
    return scores


def _group_windows(
    group: SequenceGroup,
    catalog: tuple[EpisodeCatalogEntry, ...],
    scores: dict[str, dict[str, float]],
) -> tuple[_Window, ...]:
    length = len(group.file_ids)
    windows: list[_Window] = []
    for start in range(len(catalog) - length + 1):
        entries = catalog[start : start + length]
        windows.append(
            _Window(
                start=start,
                end=start + length - 1,
                episode_ids=tuple(entry.episode_id for entry in entries),
                item_scores=tuple(
                    scores[file_id][entry.episode_id]
                    for file_id, entry in zip(group.file_ids, entries, strict=True)
                ),
            )
        )
    return tuple(windows)


def _top_assignments(
    windows_by_group: tuple[tuple[_Window, ...], ...],
) -> tuple[_Assignment, ...]:
    """Return the best two ordered, non-overlapping group assignments."""

    states = [
        _Assignment(window.total_score, (window,)) for window in windows_by_group[0]
    ]
    for windows in windows_by_group[1:]:
        next_states: list[_Assignment] = []
        for window in windows:
            compatible = [
                state for state in states if state.windows[-1].end < window.start
            ]
            compatible.sort(key=lambda state: state.total_score, reverse=True)
            next_states.extend(
                _Assignment(
                    state.total_score + window.total_score,
                    (*state.windows, window),
                )
                for state in compatible[:2]
            )
        if not next_states:
            raise SequenceMatchError(
                "No ordered one-to-one assignment fits the explicit groups"
            )
        states = next_states
    states.sort(
        key=lambda state: (
            state.total_score,
            tuple(-window.start for window in state.windows),
        ),
        reverse=True,
    )
    unique: list[_Assignment] = []
    seen: set[tuple[int, ...]] = set()
    for state in states:
        key = tuple(window.start for window in state.windows)
        if key in seen:
            continue
        seen.add(key)
        unique.append(state)
        if len(unique) == 2:
            break
    return tuple(unique)


def plan_disc_sequences(
    files: tuple[SavedFileEvidence, ...],
    catalog: tuple[EpisodeCatalogEntry, ...],
    groups: tuple[SequenceGroup, ...],
    *,
    automatic_score: float = 0.55,
    automatic_margin: float = 0.02,
    alternative_count: int = 3,
) -> DiscSequencePlan:
    """Plan contiguous sequences without media, provider, or mutation access."""

    if (
        not 0 <= automatic_score <= 1
        or not 0 <= automatic_margin <= 1
        or not 1 <= alternative_count <= 10
    ):
        raise SequenceMatchError("Sequence scoring thresholds are invalid")
    _validate_inputs(files, catalog, groups)
    scores = _score_maps(files, catalog)
    windows_by_group = tuple(_group_windows(group, catalog, scores) for group in groups)
    assignments = _top_assignments(windows_by_group)
    selected = assignments[0]
    total_files = sum(len(group.file_ids) for group in groups)
    plan_score = selected.total_score / total_files
    second_score = (
        assignments[1].total_score / total_files if len(assignments) > 1 else 0.0
    )
    global_margin = plan_score - second_score

    group_plans: list[SequenceGroupPlan] = []
    for group, chosen, all_windows in zip(
        groups,
        selected.windows,
        windows_by_group,
        strict=True,
    ):
        ranked_windows = sorted(
            all_windows,
            key=lambda window: (window.mean_score, -window.start),
            reverse=True,
        )
        alternatives = tuple(
            SequenceAlternativePlan(
                window.episode_ids,
                round(window.mean_score, 6),
            )
            for window in ranked_windows[:alternative_count]
        )
        other_scores = [
            window.mean_score
            for window in ranked_windows
            if window.start != chosen.start
        ]
        local_margin = chosen.mean_score - max(other_scores, default=0.0)
        items: list[SequenceItemPlan] = []
        for file_id, episode_id, lexical_score in zip(
            group.file_ids,
            chosen.episode_ids,
            chosen.item_scores,
            strict=True,
        ):
            independent_episode, independent_score = max(
                scores[file_id].items(),
                key=lambda item: (item[1], item[0]),
            )
            items.append(
                SequenceItemPlan(
                    file_id=file_id,
                    proposed_episode=episode_id,
                    lexical_score=round(lexical_score, 6),
                    independent_top_episode=independent_episode,
                    independent_top_score=round(independent_score, 6),
                )
            )
        group_disposition = (
            "proposed"
            if chosen.mean_score >= automatic_score
            and local_margin >= automatic_margin
            and global_margin >= automatic_margin
            else "review-ambiguous"
        )
        group_plans.append(
            SequenceGroupPlan(
                group_id=group.group_id,
                score=round(chosen.mean_score, 6),
                local_margin=round(local_margin, 6),
                disposition=group_disposition,
                items=tuple(items),
                local_alternatives=alternatives,
            )
        )

    disposition = (
        "proposed"
        if all(group.disposition == "proposed" for group in group_plans)
        else "review-ambiguous"
    )
    return DiscSequencePlan(
        mode="saved-disc-sequence-plan",
        catalog_episode_count=len(catalog),
        group_count=len(groups),
        file_count=total_files,
        score=round(plan_score, 6),
        global_margin=round(global_margin, 6),
        disposition=disposition,
        groups=tuple(group_plans),
    )


def write_safe_sequence_plan(path: Path, plan: DiscSequencePlan) -> Path:
    """Write a dialogue- and path-free plan without overwriting."""

    if path.exists():
        raise SequenceMatchError("Sequence plan exists; refusing overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(plan.safe_report(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path
