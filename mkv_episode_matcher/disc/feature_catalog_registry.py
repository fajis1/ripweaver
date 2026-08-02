"""Saved-data-only release catalogue selection for bonus-disc inventories."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from pathlib import Path

from mkv_episode_matcher.disc.special_features import (
    SpecialFeaturePlan,
    SpecialFeaturePlanError,
    load_special_feature_plan,
)


@dataclass(frozen=True)
class CatalogSelection:
    plan: SpecialFeaturePlan
    catalog_path: Path
    strong_match_count: int
    runtime_error_seconds: int


def select_feature_catalog(
    inventory_path: Path,
    catalogue_directories: tuple[Path, ...],
    *,
    report_id: str,
) -> CatalogSelection | None:
    """Select one uniquely best reviewed catalogue without provider access."""

    candidates: list[CatalogSelection] = []
    seen: set[Path] = set()
    for directory in catalogue_directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                plan = load_special_feature_plan(
                    inventory_path,
                    resolved,
                    report_id=report_id,
                )
            except SpecialFeaturePlanError:
                continue
            matched = [
                item
                for item in plan.decisions
                if item.classification == "matched-feature"
            ]
            required_matches = max(2, min(5, ceil(plan.catalogue_entry_count / 2)))
            if len(matched) < required_matches:
                continue
            candidates.append(
                CatalogSelection(
                    plan=plan,
                    catalog_path=resolved,
                    strong_match_count=len(matched),
                    runtime_error_seconds=sum(
                        item.runtime_delta_seconds or 0 for item in matched
                    ),
                )
            )
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda item: (-item.strong_match_count, item.runtime_error_seconds),
    )
    if len(ranked) > 1 and (
        ranked[0].strong_match_count,
        ranked[0].runtime_error_seconds,
    ) == (
        ranked[1].strong_match_count,
        ranked[1].runtime_error_seconds,
    ):
        return None
    return ranked[0]
