"""Derive a collision-safe special-feature resume plan from saved run data."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from mkv_episode_matcher.disc.special_feature_binder import (
    BoundSpecialFeatureManifest,
    SpecialFeatureBindError,
    _drive_index,
    _load_inventory,
    _normalized_inventory,
    file_sha256,
    load_bound_special_feature_manifest,
)


def _completed_job_ids(  # noqa: C901
    events_path: Path,
    expected_job_ids: set[str],
) -> tuple[set[str], str]:
    try:
        payload = events_path.read_bytes()
    except OSError as exc:
        raise SpecialFeatureBindError(
            f"Could not read prior event log: {type(exc).__name__}"
        ) from exc

    started: set[str] = set()
    completed: set[str] = set()
    queue_counts: list[int] = []
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise SpecialFeatureBindError(
                f"Prior event log line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(event, dict):
            raise SpecialFeatureBindError("Prior event log entries must be objects")
        event_name = event.get("event")
        if event_name == "queue_started":
            try:
                queue_counts.append(int(event["job_count"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise SpecialFeatureBindError(
                    "Prior queue start event is invalid"
                ) from exc
        if event_name not in {"job_started", "job_completed"}:
            continue
        job_id = event.get("job_id")
        if not isinstance(job_id, str) or job_id not in expected_job_ids:
            raise SpecialFeatureBindError(
                "Prior event log refers to an unknown special-feature job"
            )
        if event_name == "job_started":
            started.add(job_id)
        else:
            if job_id not in started:
                raise SpecialFeatureBindError(
                    "Prior event log records completion without a start"
                )
            if job_id in completed:
                raise SpecialFeatureBindError(
                    "Prior event log contains duplicate completion"
                )
            completed.add(job_id)

    if queue_counts != [len(expected_job_ids)]:
        raise SpecialFeatureBindError(
            "Prior event log does not describe exactly one complete manifest queue"
        )
    if not completed:
        raise SpecialFeatureBindError(
            "Prior event log contains no completed jobs to resume after"
        )
    if completed == expected_job_ids:
        raise SpecialFeatureBindError("Prior special-feature run is already complete")
    return completed, hashlib.sha256(payload).hexdigest()


def build_special_feature_resume_manifest(
    bound_manifest_path: Path,
    original_inventory_path: Path,
    fresh_inventory_path: Path,
    events_path: Path,
    *,
    expected_bound_sha256: str,
) -> BoundSpecialFeatureManifest:
    """Rebind only unfinished jobs to a metadata-identical fresh disc scan."""

    original = load_bound_special_feature_manifest(
        bound_manifest_path,
        original_inventory_path,
        expected_bound_sha256=expected_bound_sha256,
    )
    fresh_inventory = _load_inventory(fresh_inventory_path)
    fresh_titles, fresh_signature = _normalized_inventory(fresh_inventory)
    if not hmac.compare_digest(
        fresh_signature,
        original.inventory_signature_sha256,
    ):
        raise SpecialFeatureBindError(
            "Fresh inventory does not match the original bound manifest"
        )
    fresh_drive_index = _drive_index(fresh_inventory)

    expected_job_ids = {job.job_id for job in original.jobs}
    completed, events_sha256 = _completed_job_ids(events_path, expected_job_ids)
    resume_token = hashlib.sha256(
        (
            file_sha256(bound_manifest_path)
            + events_sha256
            + fresh_signature
            + str(fresh_drive_index)
        ).encode("ascii")
    ).hexdigest()[:16]

    jobs = []
    for job in original.jobs:
        if job.job_id in completed:
            continue
        title = fresh_titles.get(job.title_index)
        if title is None or title.size_bytes != job.estimated_bytes:
            raise SpecialFeatureBindError(
                "An unfinished title no longer matches the fresh inventory"
            )
        original_dir = PurePosixPath(job.relative_output_dir)
        resume_dir = str(
            original_dir.parent
            / f"resume-{resume_token}"
            / original_dir.name
        )
        jobs.append(
            replace(
                job,
                drive_index=fresh_drive_index,
                relative_output_dir=resume_dir,
            )
        )

    return BoundSpecialFeatureManifest(
        mode="special-feature-rip-binding-plan",
        created_at=datetime.now(UTC).isoformat(),
        diagnostic_manifest_sha256=original.diagnostic_manifest_sha256,
        source_plan_sha256=original.source_plan_sha256,
        inventory_signature_sha256=fresh_signature,
        report_id=original.report_id,
        catalog_id=original.catalog_id,
        release_id=original.release_id,
        execution_authorized=False,
        jobs=tuple(jobs),
        planning_notes=(
            "Resume plan derived from an immutable bound manifest and event log.",
            f"{len(completed)} previously completed job(s) are excluded.",
            "Every unfinished job uses a new collision-refusing resume directory.",
            "Previously completed and partial outputs remain untouched.",
            "Separate exact-manifest authorization is required before ripping.",
        ),
    )
