"""Plan-only, path-redacted HandBrake batch manifests."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mkv_episode_matcher.media.handbrake import (
    HandBrakeCapabilities,
    HandBrakeError,
    HandBrakeProfile,
    validate_handbrake_profile,
)
from mkv_episode_matcher.media.organizer import sanitize_media_component


class HandBrakeBatchError(RuntimeError):
    """Raised when a batch manifest cannot be planned safely."""


@dataclass(frozen=True)
class OrganizationTarget:
    media_id: str
    relative_destination: str


@dataclass(frozen=True)
class HandBrakeBatchJob:
    media_id: str
    source_name: str
    source_size_bytes: int
    destination_relative: str


@dataclass(frozen=True)
class HandBrakeBatchManifest:
    schema_version: int
    mode: str
    status: str
    profile: dict[str, object]
    job_count: int
    total_source_bytes: int
    required_free_bytes: int
    available_free_bytes: int
    missing_directories: tuple[str, ...]
    jobs: tuple[HandBrakeBatchJob, ...]

    def safe_report(self) -> dict[str, object]:
        return asdict(self)


DiskUsage = Callable[[Path], Any]


def load_organization_targets(path: Path) -> tuple[OrganizationTarget, ...]:
    """Load a conflict-free relative organization plan."""

    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload["mode"] != "tv-organization-plan"
            or int(payload["review_count"]) != 0
            or int(payload["proposed_count"]) != int(payload["item_count"])
        ):
            raise TypeError
        raw_items = payload["items"]
        if not isinstance(raw_items, list):
            raise TypeError
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise HandBrakeBatchError(
            "Organization plan is invalid or contains review items"
        ) from exc

    targets: list[OrganizationTarget] = []
    for item in raw_items:
        try:
            media_id = str(item["file_id"])
            relative = str(item["relative_destination"])
            status = str(item["status"])
        except (KeyError, TypeError) as exc:
            raise HandBrakeBatchError("Organization target fields are invalid") from exc
        relative_path = Path(relative)
        if (
            status != "proposed"
            or not media_id
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or len(relative_path.parts) != 3
            or relative_path.suffix.lower() != ".mkv"
        ):
            raise HandBrakeBatchError("Organization target is unsafe")
        targets.append(OrganizationTarget(media_id, relative_path.as_posix()))
    if not targets:
        raise HandBrakeBatchError("Organization plan contains no targets")
    if len({item.media_id for item in targets}) != len(targets):
        raise HandBrakeBatchError("Organization media IDs must be unique")
    if len({item.relative_destination.casefold() for item in targets}) != len(targets):
        raise HandBrakeBatchError("Organization destinations must be unique")
    return tuple(targets)


def _validate_sources(
    targets: tuple[OrganizationTarget, ...],
    sources: dict[str, Path],
) -> tuple[Path, dict[str, Path]]:
    target_ids = {item.media_id for item in targets}
    if set(sources) != target_ids:
        raise HandBrakeBatchError(
            "Explicit source mappings must exactly cover organization media IDs"
        )
    resolved: dict[str, Path] = {}
    for media_id, source in sources.items():
        candidate = source.resolve()
        if not candidate.is_file() or candidate.suffix.lower() != ".mkv":
            raise HandBrakeBatchError("Each batch source must be one existing MKV")
        if candidate.stat().st_size <= 0:
            raise HandBrakeBatchError("Batch source MKVs must not be empty")
        resolved[media_id] = candidate
    if len(set(resolved.values())) != len(resolved):
        raise HandBrakeBatchError("Each source MKV may appear only once")
    parents = {path.parent for path in resolved.values()}
    if len(parents) != 1:
        raise HandBrakeBatchError(
            "Path-redacted batch manifests require one explicit source root"
        )
    return next(iter(parents)), resolved


def _casefold_names(directory: Path) -> set[str]:
    if not directory.exists():
        return set()
    if not directory.is_dir():
        raise HandBrakeBatchError("A planned encoded destination is not a directory")
    try:
        return {entry.name.casefold() for entry in directory.iterdir()}
    except OSError as exc:
        raise HandBrakeBatchError(
            f"Encoded destination names could not be inspected: {type(exc).__name__}"
        ) from exc


def _build_jobs(
    targets: tuple[OrganizationTarget, ...],
    resolved_sources: dict[str, Path],
    *,
    source_root: Path,
    output_root: Path,
    prefix: str,
) -> tuple[list[HandBrakeBatchJob], set[str]]:
    missing_directories: set[str] = set()
    jobs: list[HandBrakeBatchJob] = []
    planned: set[str] = set()
    for target in targets:
        destination_relative = Path(prefix) / Path(target.relative_destination)
        destination = (output_root / destination_relative).resolve()
        try:
            destination.relative_to(output_root)
        except ValueError as exc:
            raise HandBrakeBatchError(
                "Encoded destination escapes the output root"
            ) from exc
        if (
            destination.parent == source_root
            or destination == resolved_sources[target.media_id]
        ):
            raise HandBrakeBatchError(
                "Encoded staging must be separate from source files"
            )
        destination_key = str(destination).casefold()
        if destination_key in planned:
            raise HandBrakeBatchError("Batch destinations must be unique")
        planned.add(destination_key)
        existing_names = _casefold_names(destination.parent)
        partial_name = (
            f"{destination.stem}.{target.media_id}.partial{destination.suffix}"
        ).casefold()
        if (
            destination.name.casefold() in existing_names
            or partial_name in existing_names
        ):
            raise HandBrakeBatchError(
                "Encoded destination or partial output exists; refusing overwrite"
            )
        if not destination.parent.exists():
            missing_directories.add(destination_relative.parent.as_posix())
        source = resolved_sources[target.media_id]
        jobs.append(
            HandBrakeBatchJob(
                media_id=target.media_id,
                source_name=source.name,
                source_size_bytes=source.stat().st_size,
                destination_relative=destination_relative.as_posix(),
            )
        )
    return jobs, missing_directories


def plan_handbrake_batch(
    targets: tuple[OrganizationTarget, ...],
    sources: dict[str, Path],
    *,
    output_root: Path,
    staging_prefix: str,
    profile: HandBrakeProfile,
    capabilities: HandBrakeCapabilities,
    reserve_bytes: int = 10 * 1024**3,
    disk_usage: DiskUsage = shutil.disk_usage,
) -> HandBrakeBatchManifest:
    """Plan an execution-ready relative manifest without media-content reads."""

    if not output_root.is_dir():
        raise HandBrakeBatchError("Encoded output root must be an existing directory")
    if (
        "/" in staging_prefix
        or "\\" in staging_prefix
        or staging_prefix.strip() in {"", ".", ".."}
    ):
        raise HandBrakeBatchError("Staging prefix must be one safe path component")
    prefix = sanitize_media_component(staging_prefix)
    if reserve_bytes < 0:
        raise HandBrakeBatchError("Free-space reserve must not be negative")
    try:
        validate_handbrake_profile(profile)
    except HandBrakeError as exc:
        raise HandBrakeBatchError(str(exc)) from exc
    if not capabilities.vcn_available or profile.encoder not in capabilities.encoders:
        raise HandBrakeBatchError("Requested AMD VCN encoder is not available")

    source_root, resolved_sources = _validate_sources(targets, sources)
    resolved_output_root = output_root.resolve()
    jobs, missing_directories = _build_jobs(
        targets,
        resolved_sources,
        source_root=source_root,
        output_root=resolved_output_root,
        prefix=prefix,
    )

    total_source_bytes = sum(job.source_size_bytes for job in jobs)
    required_free_bytes = total_source_bytes + reserve_bytes
    try:
        available_free_bytes = int(disk_usage(resolved_output_root).free)
    except OSError as exc:
        raise HandBrakeBatchError(
            f"Encoded staging free space could not be checked: {type(exc).__name__}"
        ) from exc
    if available_free_bytes < required_free_bytes:
        raise HandBrakeBatchError("Encoded staging has insufficient free space")
    status = "ready-after-directory-creation" if missing_directories else "ready"
    return HandBrakeBatchManifest(
        schema_version=2,
        mode="handbrake-batch-manifest",
        status=status,
        profile=asdict(profile),
        job_count=len(jobs),
        total_source_bytes=total_source_bytes,
        required_free_bytes=required_free_bytes,
        available_free_bytes=available_free_bytes,
        missing_directories=tuple(sorted(missing_directories)),
        jobs=tuple(jobs),
    )


def write_handbrake_batch_manifest(
    path: Path,
    manifest: HandBrakeBatchManifest,
) -> Path:
    """Write a new path-redacted manifest without overwriting."""

    if path.exists():
        raise HandBrakeBatchError("HandBrake batch manifest exists; refusing overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.safe_report(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path
