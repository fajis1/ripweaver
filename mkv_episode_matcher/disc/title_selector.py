"""Deterministic, plan-only title selection from saved MakeMKV inventories.

This module reads metadata dictionaries and produces recommendations. It has no
subprocess, disc, media-file, or filesystem mutation capability.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path
from statistics import median
from typing import Any, Literal

MIN_EPISODE_SECONDS = 15 * 60
MIN_HINTED_TITLE_SECONDS = 5 * 60
MIN_BONUS_FEATURE_SECONDS = 3 * 60
MIN_RIPPABLE_TITLE_SECONDS = 8
CLUSTER_RELATIVE_TOLERANCE = 0.12
CLUSTER_ABSOLUTE_TOLERANCE = 120
COMBINED_RELATIVE_TOLERANCE = 0.02
COMBINED_ABSOLUTE_TOLERANCE = 90
EXTRA_MAXIMUM_RATIO = 0.65

TitleClassification = Literal["episode", "combined", "extra", "review"]


class TitlePlanError(RuntimeError):
    """Raised when a saved inventory cannot produce a safe plan."""


@dataclass(frozen=True)
class NormalizedAudioStream:
    stream_id: int
    language: str | None
    name: str | None
    codec: str | None
    channels: int | None
    channel_layout: str | None
    is_default: bool


@dataclass(frozen=True)
class NormalizedTitle:
    index: int
    duration_seconds: int | None
    size_bytes: int | None
    chapters: int | None
    output_name: str | None
    audio_streams: tuple[NormalizedAudioStream, ...]


@dataclass(frozen=True)
class TitleDecision:
    title: NormalizedTitle
    classification: TitleClassification
    selected: bool
    reasons: tuple[str, ...]
    diagnostic_audio_stream: int | None
    alternate_audio_streams: tuple[int, ...]
    combined_from_titles: tuple[int, ...] = ()


@dataclass(frozen=True)
class DiscTitlePlan:
    report_id: str
    mode: Literal["plan-only"]
    episode_runtime_seconds: int | None
    expected_episode_count: int | None
    expected_runtime_seconds: int | None
    runtime_tolerance_seconds: int | None
    warning_count: int
    planning_notes: tuple[str, ...]
    decisions: tuple[TitleDecision, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_duration_seconds(value: Any) -> int | None:
    """Parse MakeMKV ``H:MM:SS`` duration metadata."""

    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return None
    if min(hours, minutes, seconds) < 0 or minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _attribute(attributes: dict[Any, Any], code: int) -> Any:
    return attributes.get(code, attributes.get(str(code)))


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _stream_records(streams: Any) -> list[dict[str, Any]]:
    if isinstance(streams, dict):
        return [value for value in streams.values() if isinstance(value, dict)]
    if isinstance(streams, list):
        return [value for value in streams if isinstance(value, dict)]
    return []


def normalize_title(raw_title: dict[str, Any]) -> NormalizedTitle:
    """Normalize one title from an ``inventory_to_dict`` JSON record."""

    attributes = raw_title.get("attributes", {})
    if not isinstance(attributes, dict):
        attributes = {}

    audio_streams: list[NormalizedAudioStream] = []
    for raw_stream in _stream_records(raw_title.get("streams", {})):
        stream_attributes = raw_stream.get("attributes", {})
        if not isinstance(stream_attributes, dict):
            continue
        if str(_attribute(stream_attributes, 1) or "").lower() != "audio":
            continue
        audio_streams.append(
            NormalizedAudioStream(
                stream_id=int(raw_stream.get("stream_id", -1)),
                language=_attribute(stream_attributes, 3),
                name=_attribute(stream_attributes, 2),
                codec=_attribute(stream_attributes, 6),
                channels=_optional_int(_attribute(stream_attributes, 14)),
                channel_layout=_attribute(stream_attributes, 40),
                is_default="d" in str(_attribute(stream_attributes, 38) or ""),
            )
        )

    return NormalizedTitle(
        index=int(raw_title.get("index", -1)),
        duration_seconds=parse_duration_seconds(_attribute(attributes, 9)),
        size_bytes=_optional_int(_attribute(attributes, 11)),
        chapters=_optional_int(_attribute(attributes, 8)),
        output_name=(
            _attribute(attributes, 27)
            or _attribute(attributes, 16)
            or _attribute(attributes, 32)
        ),
        audio_streams=tuple(sorted(audio_streams, key=lambda stream: stream.stream_id)),
    )


def _audio_rank(stream: NormalizedAudioStream) -> tuple[int, int, int, int, int]:
    text = f"{stream.name or ''} {stream.codec or ''}".lower()
    commentary = any(marker in text for marker in ("commentary", "director"))
    language = (stream.language or "").lower()
    english = language in {"eng", "en", "english"}
    stereo = stream.channels == 2
    return (
        1 if commentary else 0,
        0 if english else 1,
        0 if stereo else 1,
        0 if stream.is_default else 1,
        stream.stream_id,
    )


def rank_diagnostic_audio(
    title: NormalizedTitle,
) -> tuple[int | None, tuple[int, ...]]:
    """Prefer English non-commentary stereo, retaining other tracks as alternates."""

    ranked = sorted(title.audio_streams, key=_audio_rank)
    if not ranked:
        return None, ()
    return ranked[0].stream_id, tuple(stream.stream_id for stream in ranked[1:])


def _episode_cluster(titles: list[NormalizedTitle]) -> list[NormalizedTitle]:
    eligible = [
        title
        for title in titles
        if title.duration_seconds is not None
        and title.duration_seconds >= MIN_EPISODE_SECONDS
    ]
    best: list[NormalizedTitle] = []
    best_score: tuple[int, float, float] | None = None
    for seed in eligible:
        assert seed.duration_seconds is not None
        tolerance = max(
            CLUSTER_ABSOLUTE_TOLERANCE,
            seed.duration_seconds * CLUSTER_RELATIVE_TOLERANCE,
        )
        group = [
            title
            for title in eligible
            if title.duration_seconds is not None
            and abs(title.duration_seconds - seed.duration_seconds) <= tolerance
        ]
        durations = [title.duration_seconds for title in group]
        spread = max(durations) - min(durations)
        center = float(median(durations))
        score = (len(group), -(spread / center), -center)
        if best_score is None or score > best_score:
            best = group
            best_score = score
    return best if len(best) >= 2 else []


def _apply_selection_hints(
    titles: list[NormalizedTitle],
    automatic_cluster: list[NormalizedTitle],
    *,
    expected_episode_count: int | None,
    expected_runtime_seconds: int | None,
    runtime_tolerance_seconds: int,
) -> tuple[list[NormalizedTitle], tuple[str, ...]]:
    if expected_episode_count is not None and expected_episode_count < 1:
        raise TitlePlanError("Expected episode count must be at least 1")
    if expected_runtime_seconds is not None and expected_runtime_seconds < 1:
        raise TitlePlanError("Expected runtime must be positive")
    if runtime_tolerance_seconds < 1:
        raise TitlePlanError("Runtime tolerance must be positive")

    if expected_episode_count is None and expected_runtime_seconds is None:
        return automatic_cluster, (
            "Selected the dominant runtime cluster automatically.",
        )

    eligible = [
        title
        for title in titles
        if title.duration_seconds is not None
        and title.duration_seconds >= MIN_HINTED_TITLE_SECONDS
    ]
    if expected_runtime_seconds is not None:
        pool = [
            title
            for title in eligible
            if title.duration_seconds is not None
            and abs(title.duration_seconds - expected_runtime_seconds)
            <= runtime_tolerance_seconds
        ]
        center = expected_runtime_seconds
    elif automatic_cluster:
        center = int(
            median([
                title.duration_seconds
                for title in automatic_cluster
                if title.duration_seconds is not None
            ])
        )
        pool = eligible
    else:
        raise TitlePlanError(
            "Expected episode count requires an expected runtime when no "
            "automatic runtime cluster exists"
        )

    ranked = sorted(
        pool,
        key=lambda title: (
            abs((title.duration_seconds or 0) - center),
            title.index,
        ),
    )
    selected = (
        ranked[:expected_episode_count]
        if expected_episode_count is not None
        else ranked
    )
    selected = sorted(selected, key=lambda title: title.index)

    notes = [
        "Applied explicit title-selection hints.",
        f"Runtime target: {center} seconds.",
    ]
    if expected_episode_count is not None:
        notes.append(f"Expected episode count: {expected_episode_count}.")
        if len(selected) < expected_episode_count:
            notes.append(
                f"Only {len(selected)} title(s) satisfied the runtime evidence; "
                "the expected count was not forced."
            )
    return selected, tuple(notes)


def _combined_components(
    duration_seconds: int,
    episodes: list[NormalizedTitle],
) -> tuple[int, ...]:
    maximum_size = min(6, len(episodes))
    tolerance = max(
        COMBINED_ABSOLUTE_TOLERANCE,
        duration_seconds * COMBINED_RELATIVE_TOLERANCE,
    )
    for component_count in range(2, maximum_size + 1):
        for group in combinations(episodes, component_count):
            component_duration = sum(title.duration_seconds or 0 for title in group)
            if abs(component_duration - duration_seconds) <= tolerance:
                return tuple(title.index for title in group)
    return ()


def select_bonus_titles(plan: DiscTitlePlan) -> tuple[TitleDecision, ...]:
    """Return conservative bonus-disc candidates from a saved title plan.

    This is intentionally metadata-only. Very short navigation clips and titles
    that resemble a play-all sum of other plausible features remain held for
    review instead of being added to the rip proposal.
    """

    plausible = [
        decision.title
        for decision in plan.decisions
        if decision.classification != "combined"
        and decision.title.duration_seconds is not None
        and decision.title.duration_seconds >= MIN_BONUS_FEATURE_SECONDS
    ]
    selected_indexes: set[int] = set()
    for title in plausible:
        duration = title.duration_seconds
        assert duration is not None
        other_titles = [item for item in plausible if item.index != title.index]
        if _combined_components(duration, other_titles):
            continue
        selected_indexes.add(title.index)
    return tuple(
        decision
        for decision in plan.decisions
        if decision.title.index in selected_indexes
    )


def select_rippable_titles(plan: DiscTitlePlan) -> tuple[TitleDecision, ...]:
    """Return every nonempty MakeMKV title that is not trivial navigation.

    Whether a title is an episode, movie, extra, duplicate/play-all item, or
    unknown is intentionally decided after ripping. Metadata ambiguity must not
    prevent automatic ingestion.
    """

    return tuple(
        decision
        for decision in plan.decisions
        if decision.title.duration_seconds is not None
        and decision.title.duration_seconds >= MIN_RIPPABLE_TITLE_SECONDS
        and decision.title.size_bytes is not None
        and decision.title.size_bytes > 0
    )


def build_title_plan(
    inventory: dict[str, Any],
    *,
    report_id: str,
    expected_episode_count: int | None = None,
    expected_runtime_seconds: int | None = None,
    runtime_tolerance_seconds: int = 5 * 60,
) -> DiscTitlePlan:
    """Build a deterministic recommendation without producing execution data."""

    raw_titles = inventory.get("titles")
    if not isinstance(raw_titles, list):
        raise TitlePlanError("Inventory does not contain a title list")

    titles = [
        normalize_title(raw_title)
        for raw_title in raw_titles
        if isinstance(raw_title, dict)
    ]
    automatic_cluster = _episode_cluster(titles)
    cluster, planning_notes = _apply_selection_hints(
        titles,
        automatic_cluster,
        expected_episode_count=expected_episode_count,
        expected_runtime_seconds=expected_runtime_seconds,
        runtime_tolerance_seconds=runtime_tolerance_seconds,
    )
    cluster_indexes = {title.index for title in cluster}
    episode_runtime = (
        int(median([title.duration_seconds for title in cluster])) if cluster else None
    )

    decisions: list[TitleDecision] = []
    for title in sorted(titles, key=lambda item: item.index):
        diagnostic, alternates = rank_diagnostic_audio(title)
        duration = title.duration_seconds
        combined_from = (
            _combined_components(duration, cluster)
            if duration is not None and title.index not in cluster_indexes
            else ()
        )

        if title.index in cluster_indexes:
            classification: TitleClassification = "episode"
            selected = True
            if (
                expected_episode_count is not None
                or expected_runtime_seconds is not None
            ):
                reasons = (
                    "Selected by the explicit expected-count/runtime hints "
                    f"(selected median {episode_runtime} seconds).",
                )
            else:
                reasons = (
                    f"Runtime is within the dominant episode cluster "
                    f"(median {episode_runtime} seconds).",
                )
        elif combined_from:
            classification = "combined"
            selected = False
            reasons = (
                "Runtime matches the sum of selected individual titles "
                f"{', '.join(str(index) for index in combined_from)}.",
                "Excluded to avoid planning both a combined title and its components.",
            )
        elif (
            duration is not None
            and episode_runtime is not None
            and duration < episode_runtime * EXTRA_MAXIMUM_RATIO
        ):
            classification = "extra"
            selected = False
            reasons = ("Runtime is below 65% of the dominant episode runtime.",)
        else:
            classification = "review"
            selected = False
            reasons = (
                "Metadata does not provide enough deterministic evidence for selection.",
            )

        decisions.append(
            TitleDecision(
                title=title,
                classification=classification,
                selected=selected,
                reasons=reasons,
                diagnostic_audio_stream=diagnostic,
                alternate_audio_streams=alternates,
                combined_from_titles=combined_from,
            )
        )

    warnings = inventory.get("warnings", [])
    return DiscTitlePlan(
        report_id=report_id,
        mode="plan-only",
        episode_runtime_seconds=episode_runtime,
        expected_episode_count=expected_episode_count,
        expected_runtime_seconds=expected_runtime_seconds,
        runtime_tolerance_seconds=(
            runtime_tolerance_seconds if expected_runtime_seconds is not None else None
        ),
        warning_count=len(warnings) if isinstance(warnings, list) else 0,
        planning_notes=planning_notes,
        decisions=tuple(decisions),
    )


def load_title_plan(
    report_path: Path,
    *,
    report_id: str,
    expected_episode_count: int | None = None,
    expected_runtime_seconds: int | None = None,
    runtime_tolerance_seconds: int = 5 * 60,
) -> DiscTitlePlan:
    """Load one saved JSON inventory and produce a plan without changing it."""

    try:
        inventory = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TitlePlanError(
            f"Could not read saved inventory report: {type(exc).__name__}"
        ) from exc
    if not isinstance(inventory, dict):
        raise TitlePlanError("Inventory report must contain a JSON object")
    return build_title_plan(
        inventory,
        report_id=report_id,
        expected_episode_count=expected_episode_count,
        expected_runtime_seconds=expected_runtime_seconds,
        runtime_tolerance_seconds=runtime_tolerance_seconds,
    )
