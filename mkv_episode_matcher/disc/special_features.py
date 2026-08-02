"""Plan-only matching for movie and television disc special features.

The planner combines a saved MakeMKV inventory with a reviewed, path-free
feature catalogue.  It has no disc, subprocess, provider, or media mutation
capability.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from mkv_episode_matcher.disc.title_selector import (
    NormalizedTitle,
    normalize_title,
    rank_diagnostic_audio,
)

FeatureClassification = Literal[
    "matched-feature",
    "ambiguous-match",
    "play-all-candidate",
    "duplicate-candidate",
    "menu-candidate",
    "review",
]
FeatureRepresentation = Literal[
    "standalone-title",
    "multi-audio-title",
    "menu-bound",
    "audio-only",
    "still-gallery",
    "unknown",
]

_JELLYFIN_FOLDERS = {
    "behind the scenes": "behind the scenes",
    "behind-the-scenes": "behind the scenes",
    "deleted scene": "deleted scenes",
    "deleted scenes": "deleted scenes",
    "interview": "interviews",
    "interviews": "interviews",
    "scene": "scenes",
    "scenes": "scenes",
    "sample": "samples",
    "samples": "samples",
    "short": "shorts",
    "shorts": "shorts",
    "featurette": "featurettes",
    "featurettes": "featurettes",
    "clip": "clips",
    "clips": "clips",
    "trailer": "trailers",
    "trailers": "trailers",
    "other": "other",
    "extra": "extras",
    "extras": "extras",
}


class SpecialFeaturePlanError(RuntimeError):
    """Raised when saved special-feature evidence is invalid."""


@dataclass(frozen=True)
class FeatureCatalogEntry:
    feature_id: str
    title: str
    feature_type: str
    runtime_seconds: int | None
    representation: FeatureRepresentation = "standalone-title"
    source_ids: tuple[str, ...] = ()
    play_all: bool = False
    component_ids: tuple[str, ...] = ()

    @property
    def jellyfin_folder(self) -> str:
        return _JELLYFIN_FOLDERS.get(self.feature_type.casefold(), "other")


@dataclass(frozen=True)
class FeatureCatalog:
    catalog_id: str
    release_id: str
    features: tuple[FeatureCatalogEntry, ...]
    library_title: str | None = None
    library_year: int | None = None


@dataclass(frozen=True)
class SpecialFeatureDecision:
    title: NormalizedTitle
    classification: FeatureClassification
    recommended_for_rip: bool
    matched_feature_id: str | None
    candidate_feature_ids: tuple[str, ...]
    matched_title: str | None
    feature_type: str | None
    jellyfin_folder: str | None
    runtime_delta_seconds: int | None
    diagnostic_audio_stream: int | None
    alternate_audio_streams: tuple[int, ...]
    audio_policy: Literal["preserve-source", "preserve-all", "review"]
    jellyfin_fallback_folder: str | None
    fallback_name_policy: Literal["none", "content-fingerprint-required"]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SpecialFeaturePlan:
    report_id: str
    mode: Literal["special-features-plan-only"]
    catalog_id: str
    release_id: str
    catalogue_entry_count: int
    warning_count: int
    planning_notes: tuple[str, ...]
    decisions: tuple[SpecialFeatureDecision, ...]
    missing_feature_ids: tuple[str, ...]
    library_title: str | None = None
    library_year: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise SpecialFeaturePlanError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SpecialFeaturePlanError(f"{field} must be a positive integer") from exc
    if parsed < 1:
        raise SpecialFeaturePlanError(f"{field} must be a positive integer")
    return parsed


def _parse_catalog_entry(  # noqa: C901
    raw: Any,
    seen: set[str],
    source_ids: set[str],
) -> FeatureCatalogEntry:
    if not isinstance(raw, dict):
        raise SpecialFeaturePlanError("Feature entries must be objects")
    allowed = {
        "feature_id",
        "title",
        "feature_type",
        "runtime_seconds",
        "representation",
        "source_ids",
        "play_all",
        "component_ids",
    }
    if set(raw) - allowed:
        raise SpecialFeaturePlanError("Feature entry contains unsupported fields")
    feature_id = raw.get("feature_id")
    title = raw.get("title")
    feature_type = raw.get("feature_type", "other")
    if not isinstance(feature_id, str) or not feature_id.strip():
        raise SpecialFeaturePlanError("Feature ID must be a non-empty string")
    if feature_id in seen:
        raise SpecialFeaturePlanError("Feature IDs must be unique")
    if not isinstance(title, str) or not title.strip():
        raise SpecialFeaturePlanError("Feature title must be a non-empty string")
    if not isinstance(feature_type, str) or not feature_type.strip():
        raise SpecialFeaturePlanError("Feature type must be a non-empty string")
    representation = raw.get("representation", "standalone-title")
    allowed_representations = {
        "standalone-title",
        "multi-audio-title",
        "menu-bound",
        "audio-only",
        "still-gallery",
        "unknown",
    }
    if representation not in allowed_representations:
        raise SpecialFeaturePlanError("Feature representation is unsupported")
    runtime_raw = raw.get("runtime_seconds")
    runtime = (
        None if runtime_raw is None else _positive_int(runtime_raw, "runtime_seconds")
    )
    if representation in {"standalone-title", "multi-audio-title"} and runtime is None:
        raise SpecialFeaturePlanError(
            "Standalone feature entries require runtime_seconds"
        )
    raw_source_ids = raw.get("source_ids", [])
    if not isinstance(raw_source_ids, list) or not all(
        isinstance(item, str) and item for item in raw_source_ids
    ):
        raise SpecialFeaturePlanError("source_ids must be a list of source IDs")
    if set(raw_source_ids) - source_ids:
        raise SpecialFeaturePlanError("Feature references an unknown source ID")
    play_all = raw.get("play_all", False)
    if not isinstance(play_all, bool):
        raise SpecialFeaturePlanError("play_all must be true or false")
    raw_components = raw.get("component_ids", [])
    if not isinstance(raw_components, list) or not all(
        isinstance(item, str) and item for item in raw_components
    ):
        raise SpecialFeaturePlanError("component_ids must be a list of feature IDs")
    seen.add(feature_id)
    return FeatureCatalogEntry(
        feature_id=feature_id,
        title=title.strip(),
        feature_type=feature_type.strip(),
        runtime_seconds=runtime,
        representation=representation,
        source_ids=tuple(raw_source_ids),
        play_all=play_all,
        component_ids=tuple(raw_components),
    )


def load_feature_catalog_payload(payload: Any) -> FeatureCatalog:  # noqa: C901
    """Validate a reviewed provider-neutral feature catalogue."""

    if not isinstance(payload, dict):
        raise SpecialFeaturePlanError("Feature catalogue must be a JSON object")
    if payload.get("mode") != "special-feature-catalog":
        raise SpecialFeaturePlanError("File is not a special-feature catalogue")
    allowed_top_level = {
        "mode",
        "catalog_id",
        "release_id",
        "release",
        "sources",
        "features",
    }
    if set(payload) - allowed_top_level:
        raise SpecialFeaturePlanError("Feature catalogue contains unsupported fields")
    catalog_id = payload.get("catalog_id", "unspecified-catalog")
    release_id = payload.get("release_id", "unspecified-release")
    if not isinstance(catalog_id, str) or not catalog_id.strip():
        raise SpecialFeaturePlanError("catalog_id must be a non-empty string")
    if not isinstance(release_id, str) or not release_id.strip():
        raise SpecialFeaturePlanError("release_id must be a non-empty string")
    release = payload.get("release")
    if release is not None and not isinstance(release, dict):
        raise SpecialFeaturePlanError("release must be an object")
    library_title = release.get("library_title") if release else None
    library_year = release.get("library_year") if release else None
    if library_title is not None and (
        not isinstance(library_title, str) or not library_title.strip()
    ):
        raise SpecialFeaturePlanError("release library_title is invalid")
    if library_year is not None and (
        not isinstance(library_year, int)
        or isinstance(library_year, bool)
        or not 1870 <= library_year <= 2200
    ):
        raise SpecialFeaturePlanError("release library_year is invalid")
    raw_sources = payload.get("sources", [])
    if not isinstance(raw_sources, list):
        raise SpecialFeaturePlanError("sources must be a list")
    source_ids: set[str] = set()
    for source in raw_sources:
        if not isinstance(source, dict):
            raise SpecialFeaturePlanError("Catalogue sources must be objects")
        if set(source) != {"source_id", "source_type", "locator"}:
            raise SpecialFeaturePlanError("Catalogue source fields are invalid")
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise SpecialFeaturePlanError("Source ID must be a non-empty string")
        if source_id in source_ids:
            raise SpecialFeaturePlanError("Source IDs must be unique")
        if source.get("source_type") not in {
            "publisher",
            "review",
            "database",
            "manual",
        }:
            raise SpecialFeaturePlanError("Source type is unsupported")
        locator = source.get("locator")
        if not isinstance(locator, str) or not locator.strip():
            raise SpecialFeaturePlanError("Source locator must be a non-empty string")
        source_ids.add(source_id)
    raw_features = payload.get("features")
    if not isinstance(raw_features, list):
        raise SpecialFeaturePlanError("Feature catalogue has no feature list")

    entries: list[FeatureCatalogEntry] = []
    seen: set[str] = set()
    for raw in raw_features:
        entries.append(_parse_catalog_entry(raw, seen, source_ids))

    for entry in entries:
        unknown = set(entry.component_ids) - seen
        if unknown:
            raise SpecialFeaturePlanError(
                "Play-all entry references an unknown component ID"
            )
        if entry.component_ids and not entry.play_all:
            raise SpecialFeaturePlanError(
                "Only play-all entries may declare component IDs"
            )
    return FeatureCatalog(
        catalog_id=catalog_id.strip(),
        release_id=release_id.strip(),
        features=tuple(entries),
        library_title=library_title.strip() if library_title else None,
        library_year=library_year,
    )


def load_feature_catalog(path: Path) -> FeatureCatalog:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecialFeaturePlanError(
            f"Could not read feature catalogue: {type(exc).__name__}"
        ) from exc
    return load_feature_catalog_payload(payload)


def _runtime_tolerance(runtime_seconds: int, maximum: int) -> int:
    return max(15, min(maximum, round(runtime_seconds * 0.08)))


def _hungarian(cost: list[list[int]]) -> list[int]:  # noqa: C901
    """Return minimum-cost column assignments for a rectangular matrix."""

    if not cost:
        return []
    row_count = len(cost)
    column_count = len(cost[0])
    if row_count > column_count or any(len(row) != column_count for row in cost):
        raise SpecialFeaturePlanError("Invalid assignment matrix")

    u = [0] * (row_count + 1)
    v = [0] * (column_count + 1)
    p = [0] * (column_count + 1)
    way = [0] * (column_count + 1)

    for row in range(1, row_count + 1):
        p[0] = row
        column0 = 0
        minimum = [10**12] * (column_count + 1)
        used = [False] * (column_count + 1)
        while True:
            used[column0] = True
            row0 = p[column0]
            delta = 10**12
            column1 = 0
            for column in range(1, column_count + 1):
                if used[column]:
                    continue
                current = cost[row0 - 1][column - 1] - u[row0] - v[column]
                if current < minimum[column]:
                    minimum[column] = current
                    way[column] = column0
                if minimum[column] < delta:
                    delta = minimum[column]
                    column1 = column
            for column in range(column_count + 1):
                if used[column]:
                    u[p[column]] += delta
                    v[column] -= delta
                else:
                    minimum[column] -= delta
            column0 = column1
            if p[column0] == 0:
                break
        while True:
            column1 = way[column0]
            p[column0] = p[column1]
            column0 = column1
            if column0 == 0:
                break

    assignment = [-1] * row_count
    for column in range(1, column_count + 1):
        if p[column]:
            assignment[p[column] - 1] = column - 1
    return assignment


def _match_features(
    titles: list[NormalizedTitle],
    features: list[FeatureCatalogEntry],
    maximum_runtime_delta: int,
) -> dict[int, FeatureCatalogEntry]:
    if not titles or not features:
        return {}
    forbidden = 10**9
    unmatched = 10**6
    costs: list[list[int]] = []
    for title in titles:
        row: list[int] = []
        for feature in features:
            if title.duration_seconds is None or feature.runtime_seconds is None:
                row.append(forbidden)
                continue
            delta = abs(title.duration_seconds - feature.runtime_seconds)
            tolerance = _runtime_tolerance(
                feature.runtime_seconds, maximum_runtime_delta
            )
            row.append(delta if delta <= tolerance else forbidden)
        row.extend(unmatched + index for index in range(len(titles)))
        costs.append(row)

    assignment = _hungarian(costs)
    matched: dict[int, FeatureCatalogEntry] = {}
    for row_index, (title, column) in enumerate(zip(titles, assignment, strict=True)):
        if 0 <= column < len(features) and costs[row_index][column] < forbidden:
            matched[title.index] = features[column]
    return matched


def _runtime_ambiguities(  # noqa: C901
    titles: list[NormalizedTitle],
    features: list[FeatureCatalogEntry],
    maximum_runtime_delta: int,
) -> dict[int, tuple[FeatureCatalogEntry, ...]]:
    """Find equal-best runtime choices that metadata cannot safely resolve."""

    choices: dict[int, set[FeatureCatalogEntry]] = {}
    eligible: list[tuple[NormalizedTitle, FeatureCatalogEntry, int]] = []
    for title in titles:
        if title.duration_seconds is None:
            continue
        for feature in features:
            if feature.runtime_seconds is None:
                continue
            delta = abs(title.duration_seconds - feature.runtime_seconds)
            if delta <= _runtime_tolerance(
                feature.runtime_seconds, maximum_runtime_delta
            ):
                eligible.append((title, feature, delta))

    for title in titles:
        candidates = [
            (feature, delta) for item, feature, delta in eligible if item == title
        ]
        if not candidates:
            continue
        best = min(delta for _, delta in candidates)
        tied = {feature for feature, delta in candidates if delta == best}
        if len(tied) > 1:
            choices.setdefault(title.index, set()).update(tied)

    for feature in features:
        candidates = [
            (title, delta) for title, item, delta in eligible if item == feature
        ]
        if not candidates:
            continue
        best = min(delta for _, delta in candidates)
        tied = {title.index for title, delta in candidates if delta == best}
        if len(tied) > 1:
            for title_index in tied:
                choices.setdefault(title_index, set()).add(feature)

    return {
        title_index: tuple(sorted(entries, key=lambda item: item.feature_id))
        for title_index, entries in choices.items()
    }


def _metadata_signature(title: NormalizedTitle) -> tuple[int, int, int] | None:
    if (
        title.duration_seconds is None
        or title.size_bytes is None
        or title.chapters is None
    ):
        return None
    return title.duration_seconds, title.size_bytes, title.chapters


def _play_all_runtime_targets(
    catalogue: tuple[FeatureCatalogEntry, ...],
    *,
    maximum_title_runtime: int,
    maximum_runtime_delta: int,
) -> tuple[int, ...]:
    by_id = {entry.feature_id: entry for entry in catalogue}
    runtimes = {
        entry.runtime_seconds
        for entry in catalogue
        if entry.play_all and entry.runtime_seconds is not None
    }
    for entry in catalogue:
        if entry.play_all and len(entry.component_ids) >= 2:
            components = [by_id[item].runtime_seconds for item in entry.component_ids]
            if all(runtime is not None for runtime in components):
                runtimes.add(
                    sum(runtime for runtime in components if runtime is not None)
                )

    parts = [
        entry
        for entry in catalogue
        if not entry.play_all and entry.runtime_seconds is not None
    ]
    limit = maximum_title_runtime + maximum_runtime_delta
    sums_by_count: list[set[int]] = [set() for _ in range(7)]
    sums_by_count[0].add(0)
    for entry in parts:
        for count in range(5, -1, -1):
            for subtotal in tuple(sums_by_count[count]):
                combined = subtotal + (entry.runtime_seconds or 0)
                if combined <= limit:
                    sums_by_count[count + 1].add(combined)
    for count in range(2, 7):
        runtimes.update(sums_by_count[count])
    return tuple(sorted(runtimes))


def build_special_feature_plan(  # noqa: C901
    inventory: dict[str, Any],
    catalogue: FeatureCatalog,
    *,
    report_id: str,
    maximum_runtime_delta: int = 120,
) -> SpecialFeaturePlan:
    """Build a safe special-feature recommendation from saved metadata."""

    if maximum_runtime_delta < 15:
        raise SpecialFeaturePlanError(
            "Maximum runtime delta must be at least 15 seconds"
        )
    raw_titles = inventory.get("titles")
    if not isinstance(raw_titles, list):
        raise SpecialFeaturePlanError("Inventory does not contain a title list")
    titles = [normalize_title(raw) for raw in raw_titles if isinstance(raw, dict)]
    title_features = [
        entry
        for entry in catalogue.features
        if not entry.play_all
        and entry.representation in {"standalone-title", "multi-audio-title"}
    ]
    matches = _match_features(titles, title_features, maximum_runtime_delta)
    ambiguities = _runtime_ambiguities(titles, title_features, maximum_runtime_delta)
    ambiguous_feature_ids = {
        entry.feature_id for entries in ambiguities.values() for entry in entries
    }
    matched_ids = {
        entry.feature_id
        for title_index, entry in matches.items()
        if title_index not in ambiguities
        and entry.feature_id not in ambiguous_feature_ids
    }
    play_all_runtimes = _play_all_runtime_targets(
        catalogue.features,
        maximum_title_runtime=max(
            (title.duration_seconds or 0 for title in titles),
            default=0,
        ),
        maximum_runtime_delta=maximum_runtime_delta,
    )

    signature_counts: dict[tuple[int, int, int], int] = {}
    for title in titles:
        signature = _metadata_signature(title)
        if signature is not None:
            signature_counts[signature] = signature_counts.get(signature, 0) + 1

    decisions: list[SpecialFeatureDecision] = []
    for title in sorted(titles, key=lambda item: item.index):
        diagnostic, alternates = rank_diagnostic_audio(title)
        feature = matches.get(title.index)
        duration = title.duration_seconds
        signature = _metadata_signature(title)
        ambiguous = ambiguities.get(title.index, ())
        if feature is not None and feature.feature_id in ambiguous_feature_ids:
            ambiguous = tuple(
                sorted(
                    {*ambiguous, feature},
                    key=lambda item: item.feature_id,
                )
            )
        if ambiguous:
            classification: FeatureClassification = "ambiguous-match"
            recommended = False
            display_feature = feature if feature in ambiguous else ambiguous[0]
            feature = display_feature
            delta = abs((duration or 0) - display_feature.runtime_seconds)
            reasons = (
                "Runtime has an equal-best title or catalogue assignment.",
                "Held for fingerprint, thumbnail, OCR, or transcript review.",
            )
        elif feature is not None:
            classification: FeatureClassification = "matched-feature"
            recommended = True
            delta = abs((duration or 0) - feature.runtime_seconds)
            reasons = (
                "Globally assigned one-to-one using reviewed feature runtime.",
                "Recommendation still requires manifest review before ripping.",
            )
        elif duration is not None and any(
            abs(duration - target) <= _runtime_tolerance(target, maximum_runtime_delta)
            for target in play_all_runtimes
        ):
            classification = "play-all-candidate"
            recommended = False
            delta = None
            reasons = (
                "Runtime resembles a play-all or sum of individual features.",
                "Held to avoid ripping both a compilation and its components.",
            )
        elif signature is not None and signature_counts.get(signature, 0) > 1:
            classification = "duplicate-candidate"
            recommended = False
            delta = None
            reasons = (
                "Runtime, size, and chapter metadata duplicate another title.",
                "Metadata is not proof of identical content; review is required.",
            )
        elif duration is not None and duration < 30:
            classification = "menu-candidate"
            recommended = False
            delta = None
            reasons = (
                "Very short runtime resembles a menu, logo, or navigation clip.",
                "Short duration alone is not proof; review is required.",
            )
        else:
            classification = "review"
            recommended = False
            delta = None
            reasons = ("No reviewed catalogue entry is close enough in runtime.",)

        decisions.append(
            SpecialFeatureDecision(
                title=title,
                classification=classification,
                recommended_for_rip=recommended,
                matched_feature_id=feature.feature_id if feature else None,
                candidate_feature_ids=(
                    tuple(item.feature_id for item in ambiguous)
                    if ambiguous
                    else (feature.feature_id,)
                    if feature
                    else ()
                ),
                matched_title=feature.title if feature else None,
                feature_type=feature.feature_type if feature else None,
                jellyfin_folder=feature.jellyfin_folder if feature else None,
                runtime_delta_seconds=delta,
                diagnostic_audio_stream=diagnostic,
                alternate_audio_streams=alternates,
                audio_policy=(
                    "preserve-all"
                    if len(title.audio_streams) > 1
                    else "preserve-source"
                    if recommended
                    else "review"
                ),
                jellyfin_fallback_folder=(
                    "extras"
                    if classification
                    in {"ambiguous-match", "duplicate-candidate", "review"}
                    else None
                ),
                fallback_name_policy=(
                    "content-fingerprint-required"
                    if classification
                    in {"ambiguous-match", "duplicate-candidate", "review"}
                    else "none"
                ),
                reasons=reasons,
            )
        )

    warnings = inventory.get("warnings")
    return SpecialFeaturePlan(
        report_id=report_id,
        mode="special-features-plan-only",
        catalog_id=catalogue.catalog_id,
        release_id=catalogue.release_id,
        library_title=catalogue.library_title,
        library_year=catalogue.library_year,
        catalogue_entry_count=len(catalogue.features),
        warning_count=len(warnings) if isinstance(warnings, list) else 0,
        planning_notes=(
            "Play-all catalogue entries are never recommended when individual parts exist.",
            "Runtime ties, unmatched titles, and metadata duplicates remain held for review.",
            "Titles with multiple audio streams require preservation of every source stream.",
            "Unidentified plausible extras require a content fingerprint before a neutral Jellyfin fallback name is proposed.",
            "Jellyfin folders are proposed only; no directory or media action exists.",
        ),
        decisions=tuple(decisions),
        missing_feature_ids=tuple(
            entry.feature_id
            for entry in title_features
            if entry.feature_id not in matched_ids
        ),
    )


def load_special_feature_plan(
    inventory_path: Path,
    catalogue_path: Path,
    *,
    report_id: str,
    maximum_runtime_delta: int = 120,
) -> SpecialFeaturePlan:
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecialFeaturePlanError(
            f"Could not read saved inventory: {type(exc).__name__}"
        ) from exc
    if not isinstance(inventory, dict):
        raise SpecialFeaturePlanError("Inventory must be a JSON object")
    return build_special_feature_plan(
        inventory,
        load_feature_catalog(catalogue_path),
        report_id=report_id,
        maximum_runtime_delta=maximum_runtime_delta,
    )
