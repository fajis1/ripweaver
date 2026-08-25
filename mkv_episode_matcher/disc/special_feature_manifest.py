"""Build non-executable diagnostic-rip plans for special-feature discs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from mkv_episode_matcher.core.datetime_compat import UTC
from mkv_episode_matcher.disc.special_features import (
    FeatureClassification,
    SpecialFeaturePlan,
)

DiagnosticEvidence = Literal[
    "ffprobe-sanitized",
    "content-sha256",
    "perceptual-fingerprint",
    "contact-sheet",
    "embedded-text-or-ocr",
    "short-transcript-if-needed",
    "audio-stream-inventory",
]

_INCLUDED_CLASSIFICATIONS = {
    "matched-feature",
    "ambiguous-match",
    "duplicate-candidate",
    "review",
}


class SpecialFeatureManifestError(RuntimeError):
    """Raised when a diagnostic special-feature manifest is unsafe."""


@dataclass(frozen=True)
class DiagnosticSpecialFeatureJob:
    job_id: str
    title_index: int
    classification: FeatureClassification
    candidate_feature_ids: tuple[str, ...]
    estimated_bytes: int | None
    expected_duration_seconds: int | None
    expected_audio_stream_count: int
    relative_staging_dir: str
    output_basename: str
    audio_policy: Literal["preserve-source", "preserve-all", "review"]
    evidence_after_rip: tuple[DiagnosticEvidence, ...]
    jellyfin_fallback_folder: str | None
    fallback_name_policy: Literal["none", "content-fingerprint-required"]


@dataclass(frozen=True)
class ExcludedSpecialFeatureTitle:
    title_index: int
    classification: FeatureClassification
    reason: str


@dataclass(frozen=True)
class DiagnosticSpecialFeatureManifest:
    mode: Literal["special-feature-diagnostic-rip-plan-only"]
    created_at: str
    source_plan_sha256: str
    source_inventory_signature_sha256: str
    report_id: str
    catalog_id: str
    release_id: str
    execution_authorized: Literal[False]
    jobs: tuple[DiagnosticSpecialFeatureJob, ...]
    excluded_titles: tuple[ExcludedSpecialFeatureTitle, ...]
    planning_notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _plan_digest(plan: SpecialFeaturePlan) -> str:
    encoded = json.dumps(
        plan.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def inventory_signature_from_plan(plan: SpecialFeaturePlan) -> str:
    """Hash path-free metadata for every title in a validated plan."""

    identity = [
        {
            "index": decision.title.index,
            "duration_seconds": decision.title.duration_seconds,
            "size_bytes": decision.title.size_bytes,
            "chapters": decision.title.chapters,
            "audio_stream_count": len(decision.title.audio_streams),
        }
        for decision in sorted(plan.decisions, key=lambda item: item.title.index)
    ]
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_for(
    classification: FeatureClassification,
    audio_policy: str,
) -> tuple[DiagnosticEvidence, ...]:
    evidence: list[DiagnosticEvidence] = [
        "ffprobe-sanitized",
        "content-sha256",
        "perceptual-fingerprint",
    ]
    if classification in {
        "ambiguous-match",
        "duplicate-candidate",
        "review",
    }:
        evidence.extend([
            "contact-sheet",
            "embedded-text-or-ocr",
            "short-transcript-if-needed",
        ])
    if audio_policy == "preserve-all":
        evidence.append("audio-stream-inventory")
    return tuple(evidence)


def build_diagnostic_special_feature_manifest(
    plan: SpecialFeaturePlan,
) -> DiagnosticSpecialFeatureManifest:
    """Convert a validated plan to a path-redacted, non-executable manifest."""

    digest = _plan_digest(plan)
    staging_token = digest[:16]
    jobs: list[DiagnosticSpecialFeatureJob] = []
    excluded: list[ExcludedSpecialFeatureTitle] = []

    for decision in plan.decisions:
        title_index = decision.title.index
        if title_index < 0:
            raise SpecialFeatureManifestError("Plan contains an invalid title index")
        if decision.classification not in _INCLUDED_CLASSIFICATIONS:
            excluded.append(
                ExcludedSpecialFeatureTitle(
                    title_index=title_index,
                    classification=decision.classification,
                    reason=(
                        "probable-menu-held"
                        if decision.classification == "menu-candidate"
                        else "play-all-held"
                    ),
                )
            )
            continue

        job_id = f"special-{staging_token}-title-{title_index:03d}"
        jobs.append(
            DiagnosticSpecialFeatureJob(
                job_id=job_id,
                title_index=title_index,
                classification=decision.classification,
                candidate_feature_ids=decision.candidate_feature_ids,
                estimated_bytes=decision.title.size_bytes,
                expected_duration_seconds=decision.title.duration_seconds,
                expected_audio_stream_count=len(decision.title.audio_streams),
                relative_staging_dir=(
                    f".staging/special-features/{staging_token}/title-{title_index:03d}"
                ),
                output_basename=f"{job_id}.mkv",
                audio_policy=decision.audio_policy,
                evidence_after_rip=_evidence_for(
                    decision.classification,
                    decision.audio_policy,
                ),
                jellyfin_fallback_folder=decision.jellyfin_fallback_folder,
                fallback_name_policy=decision.fallback_name_policy,
            )
        )

    if not jobs:
        raise SpecialFeatureManifestError(
            "No plausible special-feature titles are available for diagnostics"
        )

    return DiagnosticSpecialFeatureManifest(
        mode="special-feature-diagnostic-rip-plan-only",
        created_at=datetime.now(UTC).isoformat(),
        source_plan_sha256=digest,
        source_inventory_signature_sha256=inventory_signature_from_plan(plan),
        report_id=plan.report_id,
        catalog_id=plan.catalog_id,
        release_id=plan.release_id,
        execution_authorized=False,
        jobs=tuple(jobs),
        excluded_titles=tuple(excluded),
        planning_notes=(
            "This manifest has no drive binding or execution authority.",
            "A future rip boundary must revalidate against a fresh preflight.",
            "Every output uses isolated collision-refusing staging.",
            "No output is a media-library destination.",
            "Unidentified extras need a content fingerprint before fallback naming.",
        ),
    )


def write_diagnostic_special_feature_manifest(
    path: Path,
    manifest: DiagnosticSpecialFeatureManifest,
) -> Path:
    """Write one new diagnostic plan without overwriting an existing file."""

    if path.exists():
        raise SpecialFeatureManifestError(
            "Diagnostic manifest already exists; refusing overwrite"
        )
    if not path.parent.exists():
        raise SpecialFeatureManifestError(
            "Diagnostic manifest parent directory does not exist"
        )
    path.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path
