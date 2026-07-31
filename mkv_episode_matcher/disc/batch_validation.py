"""Saved-data-only planning for a physical single-open MakeMKV validation.

This module never invokes MakeMKV or accesses media.  It finds the smallest
set of at least two titles that a single ``--minlength`` cutoff can select
exactly from a complete saved inventory.  The resulting manifest is explicitly
unauthorized and contains no absolute paths or executable command.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mkv_episode_matcher.disc.batch_ripper import (
    BatchInventoryTitle,
    _safe_mkv_basename,
)
from mkv_episode_matcher.disc.title_selector import normalize_title


class BatchValidationPlanError(RuntimeError):
    """Raised when a safe physical-validation plan cannot be produced."""


@dataclass(frozen=True)
class BatchValidationOutput:
    """One output expected from the future single-open validation."""

    title_index: int
    duration_seconds: int
    estimated_bytes: int
    expected_output_name: str


@dataclass(frozen=True)
class BatchValidationManifest:
    """Immutable, non-executable proposal for one future physical validation."""

    mode: str
    source_inventory_sha256: str
    inventory_signature_sha256: str
    drive_index: int
    selector: str
    minimum_length_seconds: int
    relative_staging_dir: str
    estimated_bytes: int
    execution_authorized: bool
    expected_outputs: tuple[BatchValidationOutput, ...]
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_inventory(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BatchValidationPlanError(
            "Saved inventory could not be read as JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise BatchValidationPlanError("Saved inventory root must be an object")
    return payload


def _drive_index(payload: dict[str, Any]) -> int:
    drive = payload.get("drive")
    try:
        index = int(drive["index"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchValidationPlanError(
            "Saved inventory has no valid ordinal drive index"
        ) from exc
    if not 0 <= index <= 99:
        raise BatchValidationPlanError("Saved inventory ordinal drive index is invalid")
    return index


def _normalized_titles(
    payload: dict[str, Any],
) -> tuple[BatchInventoryTitle, ...]:
    raw_titles = payload.get("titles")
    if not isinstance(raw_titles, list) or not raw_titles:
        raise BatchValidationPlanError(
            "Saved inventory must contain a complete nonempty title list"
        )

    titles: list[BatchInventoryTitle] = []
    indexes: set[int] = set()
    output_names: set[str] = set()
    for raw in raw_titles:
        if not isinstance(raw, dict):
            raise BatchValidationPlanError("Saved inventory contains a malformed title")
        normalized = normalize_title(raw)
        if (
            normalized.index < 0
            or normalized.duration_seconds is None
            or normalized.duration_seconds < 0
            or normalized.size_bytes is None
            or normalized.size_bytes <= 0
            or not isinstance(normalized.output_name, str)
            or not _safe_mkv_basename(normalized.output_name)
        ):
            raise BatchValidationPlanError(
                "Saved inventory lacks safe runtime, size, or output metadata"
            )
        if normalized.index in indexes:
            raise BatchValidationPlanError(
                "Saved inventory contains duplicate title indexes"
            )
        folded_name = normalized.output_name.casefold()
        if folded_name in output_names:
            raise BatchValidationPlanError(
                "Saved inventory contains duplicate output names"
            )
        indexes.add(normalized.index)
        output_names.add(folded_name)
        titles.append(
            BatchInventoryTitle(
                title_index=normalized.index,
                duration_seconds=normalized.duration_seconds,
                output_name=normalized.output_name,
            )
        )
    return tuple(titles)


def _smallest_exact_cutoff(
    titles: tuple[BatchInventoryTitle, ...],
) -> tuple[int, tuple[BatchInventoryTitle, ...]]:
    """Return the highest cutoff whose exact selected set has at least 2 titles."""

    if len(titles) < 2:
        raise BatchValidationPlanError(
            "Single-open validation requires at least two inventory titles"
        )
    durations = sorted(
        {title.duration_seconds for title in titles},
        reverse=True,
    )
    for selected_floor in durations:
        selected = tuple(
            sorted(
                (title for title in titles if title.duration_seconds >= selected_floor),
                key=lambda item: item.title_index,
            )
        )
        if len(selected) < 2:
            continue
        excluded = [
            title.duration_seconds
            for title in titles
            if title.duration_seconds < selected_floor
        ]
        cutoff = max(excluded) + 1 if excluded else 0
        selected_by_cutoff = tuple(
            sorted(
                (title for title in titles if title.duration_seconds >= cutoff),
                key=lambda item: item.title_index,
            )
        )
        if selected_by_cutoff == selected:
            return cutoff, selected
    raise BatchValidationPlanError(
        "No exact minimum-runtime cutoff selects at least two titles"
    )


def plan_batch_physical_validation(
    inventory_path: Path,
) -> BatchValidationManifest:
    """Plan the smallest useful single-open test from one saved inventory."""

    payload = _load_inventory(inventory_path)
    drive_index = _drive_index(payload)
    titles = _normalized_titles(payload)
    minimum_length, selected = _smallest_exact_cutoff(titles)

    raw_titles = payload["titles"]
    normalized_for_sizes = [normalize_title(raw) for raw in raw_titles]
    sizes = {title.index: title.size_bytes for title in normalized_for_sizes}
    inventory_identity = [
        {
            "title_index": title.title_index,
            "duration_seconds": title.duration_seconds,
            "estimated_bytes": sizes[title.title_index],
            "output_name": title.output_name,
        }
        for title in sorted(titles, key=lambda item: item.title_index)
    ]
    inventory_signature = _canonical_sha256(inventory_identity)
    selection_identity = {
        "inventory_signature_sha256": inventory_signature,
        "minimum_length_seconds": minimum_length,
        "title_indexes": [title.title_index for title in selected],
    }
    staging_token = _canonical_sha256(selection_identity)[:16]

    expected_outputs = tuple(
        BatchValidationOutput(
            title_index=title.title_index,
            duration_seconds=title.duration_seconds,
            estimated_bytes=int(sizes[title.title_index]),
            expected_output_name=title.output_name,
        )
        for title in selected
    )
    return BatchValidationManifest(
        mode="single-open-makemkv-physical-validation-plan",
        source_inventory_sha256=_file_sha256(inventory_path),
        inventory_signature_sha256=inventory_signature,
        drive_index=drive_index,
        selector="all",
        minimum_length_seconds=minimum_length,
        relative_staging_dir=f"batch-validation/{staging_token}",
        estimated_bytes=sum(item.estimated_bytes for item in expected_outputs),
        execution_authorized=False,
        expected_outputs=expected_outputs,
        limitations=(
            "This saved-data plan does not access a disc or invoke MakeMKV.",
            "This manifest contains no executable command or execution authority.",
            "The installed MakeMKV version's undocumented all-selector behavior "
            "still requires separately authorized physical validation.",
            "A future executor must revalidate a fresh metadata-identical inventory, "
            "the exact manifest digest, collisions, and free space.",
            "No output may be promoted, renamed into a library, transcoded, deleted, "
            "or ejected by a validation run.",
        ),
    )


def write_batch_validation_manifest(
    path: Path,
    manifest: BatchValidationManifest,
) -> tuple[Path, str]:
    """Write one new immutable proposal and return its exact file digest."""

    if path.exists():
        raise BatchValidationPlanError(
            "Batch validation manifest exists; refusing overwrite"
        )
    if not path.parent.is_dir():
        raise BatchValidationPlanError(
            "Batch validation manifest parent directory does not exist"
        )
    try:
        path.write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        raise BatchValidationPlanError(
            "Batch validation manifest could not be written"
        ) from exc
    return path, _file_sha256(path)
