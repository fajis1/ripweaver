"""Bind a diagnostic special-feature plan to one fresh saved preflight."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from mkv_episode_matcher.core.datetime_compat import UTC
from mkv_episode_matcher.disc.ripper import RipError, RipJob, validate_job
from mkv_episode_matcher.disc.special_feature_manifest import (
    DiagnosticSpecialFeatureJob,
)
from mkv_episode_matcher.disc.title_selector import normalize_title


class SpecialFeatureBindError(RuntimeError):
    """Raised when a diagnostic plan cannot safely bind to a fresh inventory."""


@dataclass(frozen=True)
class BoundSpecialFeatureJob:
    job_id: str
    drive_index: int
    title_index: int
    relative_output_dir: str
    estimated_bytes: int | None
    output_basename: str
    classification: str
    candidate_feature_ids: tuple[str, ...]
    audio_policy: str
    evidence_after_rip: tuple[str, ...]
    jellyfin_fallback_folder: str | None
    fallback_name_policy: str


@dataclass(frozen=True)
class BoundSpecialFeatureManifest:
    mode: Literal["special-feature-rip-binding-plan"]
    created_at: str
    diagnostic_manifest_sha256: str
    source_plan_sha256: str
    inventory_signature_sha256: str
    report_id: str
    catalog_id: str
    release_id: str
    execution_authorized: Literal[False]
    jobs: tuple[BoundSpecialFeatureJob, ...]
    planning_notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _LoadedDiagnostic:
    source_plan_sha256: str
    source_inventory_signature_sha256: str
    report_id: str
    catalog_id: str
    release_id: str
    jobs: tuple[DiagnosticSpecialFeatureJob, ...]


def file_sha256(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SpecialFeatureBindError(
            f"Could not read diagnostic manifest: {type(exc).__name__}"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def _hex_digest(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SpecialFeatureBindError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _load_diagnostic(path: Path) -> _LoadedDiagnostic:  # noqa: C901
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecialFeatureBindError(
            f"Could not parse diagnostic manifest: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise SpecialFeatureBindError("Diagnostic manifest must be an object")
    if payload.get("mode") != "special-feature-diagnostic-rip-plan-only":
        raise SpecialFeatureBindError("File is not a diagnostic special-feature plan")
    if payload.get("execution_authorized") is not False:
        raise SpecialFeatureBindError("Diagnostic manifest authority flag is invalid")

    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise SpecialFeatureBindError("Diagnostic manifest contains no jobs")
    jobs: list[DiagnosticSpecialFeatureJob] = []
    seen_titles: set[int] = set()
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            raise SpecialFeatureBindError("Diagnostic jobs must be objects")
        try:
            job = DiagnosticSpecialFeatureJob(
                job_id=raw["job_id"],
                title_index=int(raw["title_index"]),
                classification=raw["classification"],
                candidate_feature_ids=tuple(raw["candidate_feature_ids"]),
                estimated_bytes=raw.get("estimated_bytes"),
                expected_duration_seconds=raw.get("expected_duration_seconds"),
                expected_audio_stream_count=int(raw["expected_audio_stream_count"]),
                relative_staging_dir=raw["relative_staging_dir"],
                output_basename=raw["output_basename"],
                audio_policy=raw["audio_policy"],
                evidence_after_rip=tuple(raw["evidence_after_rip"]),
                jellyfin_fallback_folder=raw.get("jellyfin_fallback_folder"),
                fallback_name_policy=raw["fallback_name_policy"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SpecialFeatureBindError(
                "Diagnostic job structure is invalid"
            ) from exc
        if job.title_index in seen_titles:
            raise SpecialFeatureBindError("Diagnostic title indexes must be unique")
        if job.expected_audio_stream_count < 0:
            raise SpecialFeatureBindError("Expected audio stream count is invalid")
        try:
            validate_job(
                RipJob(
                    job_id=job.job_id,
                    drive_index=0,
                    title_index=job.title_index,
                    relative_output_dir=job.relative_staging_dir,
                    estimated_bytes=job.estimated_bytes,
                    output_basename=job.output_basename,
                )
            )
        except RipError as exc:
            raise SpecialFeatureBindError(
                f"Diagnostic job failed path validation: {type(exc).__name__}"
            ) from exc
        seen_titles.add(job.title_index)
        jobs.append(job)

    identifiers = {}
    for field in ("report_id", "catalog_id", "release_id"):
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise SpecialFeatureBindError(f"Diagnostic {field} is invalid")
        identifiers[field] = value
    return _LoadedDiagnostic(
        source_plan_sha256=_hex_digest(
            payload.get("source_plan_sha256"),
            "source_plan_sha256",
        ),
        source_inventory_signature_sha256=_hex_digest(
            payload.get("source_inventory_signature_sha256"),
            "source_inventory_signature_sha256",
        ),
        jobs=tuple(jobs),
        **identifiers,
    )


def _load_inventory(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecialFeatureBindError(
            f"Could not read fresh inventory: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise SpecialFeatureBindError("Fresh inventory must be an object")
    if payload.get("minimum_length_seconds") != 0:
        raise SpecialFeatureBindError(
            "Executable special-feature binding requires a zero-minimum "
            "inventory so MakeMKV title indexes cannot shift"
        )
    return payload


def _normalized_inventory(
    payload: dict[str, Any],
) -> tuple[dict[int, Any], str]:
    raw_titles = payload.get("titles")
    if not isinstance(raw_titles, list):
        raise SpecialFeatureBindError("Fresh inventory has no title list")
    titles = [normalize_title(raw) for raw in raw_titles if isinstance(raw, dict)]
    by_index = {title.index: title for title in titles}
    if len(by_index) != len(titles) or any(index < 0 for index in by_index):
        raise SpecialFeatureBindError("Fresh inventory title indexes are invalid")
    identity = [
        {
            "index": title.index,
            "duration_seconds": title.duration_seconds,
            "size_bytes": title.size_bytes,
            "chapters": title.chapters,
            "audio_stream_count": len(title.audio_streams),
        }
        for title in sorted(titles, key=lambda item: item.index)
    ]
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return by_index, hashlib.sha256(encoded).hexdigest()


def _drive_index(payload: dict[str, Any]) -> int:
    drive = payload.get("drive")
    try:
        index = int(drive["index"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecialFeatureBindError(
            "Fresh inventory has no valid drive index"
        ) from exc
    if not 0 <= index <= 99:
        raise SpecialFeatureBindError("Fresh inventory drive index is invalid")
    return index


def bind_diagnostic_special_feature_manifest(
    diagnostic_path: Path,
    fresh_inventory_path: Path,
    *,
    expected_diagnostic_sha256: str,
) -> BoundSpecialFeatureManifest:
    """Bind an immutable diagnostic plan to a metadata-identical fresh scan."""

    expected_digest = _hex_digest(
        expected_diagnostic_sha256,
        "expected diagnostic SHA-256",
    )
    actual_digest = file_sha256(diagnostic_path)
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise SpecialFeatureBindError("Diagnostic manifest SHA-256 does not match")

    diagnostic = _load_diagnostic(diagnostic_path)
    inventory = _load_inventory(fresh_inventory_path)
    titles, inventory_signature = _normalized_inventory(inventory)
    if not hmac.compare_digest(
        inventory_signature,
        diagnostic.source_inventory_signature_sha256,
    ):
        raise SpecialFeatureBindError(
            "Fresh inventory does not match the diagnostic source inventory"
        )
    drive_index = _drive_index(inventory)

    jobs: list[BoundSpecialFeatureJob] = []
    for diagnostic_job in diagnostic.jobs:
        title = titles.get(diagnostic_job.title_index)
        if title is None:
            raise SpecialFeatureBindError("A selected title is missing from fresh scan")
        if (
            title.duration_seconds != diagnostic_job.expected_duration_seconds
            or title.size_bytes != diagnostic_job.estimated_bytes
            or len(title.audio_streams) != diagnostic_job.expected_audio_stream_count
        ):
            raise SpecialFeatureBindError(
                "Selected title metadata changed since diagnostic planning"
            )
        rip_job = RipJob(
            job_id=diagnostic_job.job_id,
            drive_index=drive_index,
            title_index=diagnostic_job.title_index,
            relative_output_dir=diagnostic_job.relative_staging_dir,
            estimated_bytes=diagnostic_job.estimated_bytes,
            output_basename=diagnostic_job.output_basename,
        )
        try:
            validate_job(rip_job)
        except RipError as exc:
            raise SpecialFeatureBindError(
                f"Bound job failed validation: {type(exc).__name__}"
            ) from exc
        jobs.append(
            BoundSpecialFeatureJob(
                job_id=rip_job.job_id,
                drive_index=rip_job.drive_index,
                title_index=rip_job.title_index,
                relative_output_dir=rip_job.relative_output_dir,
                estimated_bytes=rip_job.estimated_bytes,
                output_basename=str(rip_job.output_basename),
                classification=diagnostic_job.classification,
                candidate_feature_ids=diagnostic_job.candidate_feature_ids,
                audio_policy=diagnostic_job.audio_policy,
                evidence_after_rip=diagnostic_job.evidence_after_rip,
                jellyfin_fallback_folder=(diagnostic_job.jellyfin_fallback_folder),
                fallback_name_policy=diagnostic_job.fallback_name_policy,
            )
        )

    return BoundSpecialFeatureManifest(
        mode="special-feature-rip-binding-plan",
        created_at=datetime.now(UTC).isoformat(),
        diagnostic_manifest_sha256=actual_digest,
        source_plan_sha256=diagnostic.source_plan_sha256,
        inventory_signature_sha256=inventory_signature,
        report_id=diagnostic.report_id,
        catalog_id=diagnostic.catalog_id,
        release_id=diagnostic.release_id,
        execution_authorized=False,
        jobs=tuple(jobs),
        planning_notes=(
            "The diagnostic manifest digest and full inventory signature match.",
            "This binding is not accepted by the episode rip executor.",
            "Separate exact-manifest authorization is required before ripping.",
            "No job has a media-library destination.",
        ),
    )


def write_bound_special_feature_manifest(
    path: Path,
    manifest: BoundSpecialFeatureManifest,
) -> Path:
    if path.exists():
        raise SpecialFeatureBindError("Bound manifest exists; refusing overwrite")
    if not path.parent.exists():
        raise SpecialFeatureBindError("Bound manifest parent directory does not exist")
    path.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def load_bound_special_feature_manifest(  # noqa: C901
    path: Path,
    fresh_inventory_path: Path,
    *,
    expected_bound_sha256: str,
) -> BoundSpecialFeatureManifest:
    """Load and revalidate one immutable bound manifest against saved inventory."""

    expected_digest = _hex_digest(
        expected_bound_sha256,
        "expected bound SHA-256",
    )
    actual_digest = file_sha256(path)
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise SpecialFeatureBindError("Bound manifest SHA-256 does not match")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecialFeatureBindError(
            f"Could not parse bound manifest: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise SpecialFeatureBindError("Bound manifest must be an object")
    if payload.get("mode") != "special-feature-rip-binding-plan":
        raise SpecialFeatureBindError("File is not a bound special-feature plan")
    if payload.get("execution_authorized") is not False:
        raise SpecialFeatureBindError("Bound manifest authority flag is invalid")

    inventory = _load_inventory(fresh_inventory_path)
    titles, inventory_signature = _normalized_inventory(inventory)
    recorded_signature = _hex_digest(
        payload.get("inventory_signature_sha256"),
        "inventory_signature_sha256",
    )
    if not hmac.compare_digest(inventory_signature, recorded_signature):
        raise SpecialFeatureBindError(
            "Fresh inventory no longer matches the bound manifest"
        )
    drive_index = _drive_index(inventory)

    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise SpecialFeatureBindError("Bound manifest contains no jobs")
    jobs: list[BoundSpecialFeatureJob] = []
    seen_job_ids: set[str] = set()
    seen_titles: set[int] = set()
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            raise SpecialFeatureBindError("Bound jobs must be objects")
        try:
            job = BoundSpecialFeatureJob(
                job_id=raw["job_id"],
                drive_index=int(raw["drive_index"]),
                title_index=int(raw["title_index"]),
                relative_output_dir=raw["relative_output_dir"],
                estimated_bytes=raw.get("estimated_bytes"),
                output_basename=raw["output_basename"],
                classification=raw["classification"],
                candidate_feature_ids=tuple(raw["candidate_feature_ids"]),
                audio_policy=raw["audio_policy"],
                evidence_after_rip=tuple(raw["evidence_after_rip"]),
                jellyfin_fallback_folder=raw.get("jellyfin_fallback_folder"),
                fallback_name_policy=raw["fallback_name_policy"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SpecialFeatureBindError("Bound job structure is invalid") from exc
        if job.job_id in seen_job_ids or job.title_index in seen_titles:
            raise SpecialFeatureBindError("Bound jobs must be unique")
        if job.drive_index != drive_index:
            raise SpecialFeatureBindError(
                "Bound drive no longer matches fresh inventory"
            )
        title = titles.get(job.title_index)
        if title is None or title.size_bytes != job.estimated_bytes:
            raise SpecialFeatureBindError(
                "Bound title no longer matches fresh inventory"
            )
        try:
            validate_job(
                RipJob(
                    job_id=job.job_id,
                    drive_index=job.drive_index,
                    title_index=job.title_index,
                    relative_output_dir=job.relative_output_dir,
                    estimated_bytes=job.estimated_bytes,
                    output_basename=job.output_basename,
                )
            )
        except RipError as exc:
            raise SpecialFeatureBindError(
                f"Bound job failed validation: {type(exc).__name__}"
            ) from exc
        seen_job_ids.add(job.job_id)
        seen_titles.add(job.title_index)
        jobs.append(job)

    def required_text(field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise SpecialFeatureBindError(f"Bound {field} is invalid")
        return value

    notes = payload.get("planning_notes")
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        raise SpecialFeatureBindError("Bound planning notes are invalid")
    return BoundSpecialFeatureManifest(
        mode="special-feature-rip-binding-plan",
        created_at=required_text("created_at"),
        diagnostic_manifest_sha256=_hex_digest(
            payload.get("diagnostic_manifest_sha256"),
            "diagnostic_manifest_sha256",
        ),
        source_plan_sha256=_hex_digest(
            payload.get("source_plan_sha256"),
            "source_plan_sha256",
        ),
        inventory_signature_sha256=recorded_signature,
        report_id=required_text("report_id"),
        catalog_id=required_text("catalog_id"),
        release_id=required_text("release_id"),
        execution_authorized=False,
        jobs=tuple(jobs),
        planning_notes=tuple(notes),
    )
