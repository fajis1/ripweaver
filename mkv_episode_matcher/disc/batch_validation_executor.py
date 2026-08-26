"""Synthetic-tested binding and execution boundary for batch validation.

Nothing in this module discovers a disc or media.  The binder consumes only an
exact immutable validation manifest and one explicit saved inventory.  The
executor remains deliberately unwired from the CLI; tests inject a fake batch
runner.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mkv_episode_matcher.disc.batch_ripper import (
    BatchInventoryTitle,
    SingleOpenBatchPlan,
    plan_single_open_batch,
    run_single_open_batch,
)
from mkv_episode_matcher.disc.batch_validation import (
    BatchValidationManifest,
    BatchValidationOutput,
    BatchValidationPlanError,
    plan_batch_physical_validation,
)
from mkv_episode_matcher.disc.ripper import (
    JsonlRipLog,
    RipError,
    RipJob,
    RipResult,
    resolve_job_output,
)
from mkv_episode_matcher.disc.title_selector import normalize_title

_FREE_SPACE_RESERVE = 512 * 1024**2


@dataclass(frozen=True)
class BoundBatchValidation:
    """An exact saved manifest revalidated against a fresh saved inventory."""

    manifest_sha256: str
    manifest: BatchValidationManifest
    batch_plan: SingleOpenBatchPlan


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RipError(
            f"Batch validation manifest could not be read: {type(exc).__name__}"
        ) from exc
    return digest.hexdigest()


def _checked_digest(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RipError("Expected manifest digest must be a lowercase SHA-256")
    return value


def _required_int(raw: dict[str, Any], field: str, *, minimum: int = 0) -> int:
    value = raw.get(field)
    if isinstance(value, bool):
        raise RipError(f"Batch validation {field} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RipError(f"Batch validation {field} is invalid") from exc
    if parsed < minimum:
        raise RipError(f"Batch validation {field} is invalid")
    return parsed


def _load_manifest(path: Path) -> BatchValidationManifest:  # noqa: C901
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RipError(
            f"Batch validation manifest could not be parsed: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise RipError("Batch validation manifest root must be an object")
    if payload.get("mode") != "single-open-makemkv-physical-validation-plan":
        raise RipError("File is not a single-open physical-validation plan")
    if payload.get("execution_authorized") is not False:
        raise RipError("Batch validation authority flag is invalid")
    if payload.get("selector") != "all":
        raise RipError("Batch validation selector must be all")

    raw_outputs = payload.get("expected_outputs")
    if not isinstance(raw_outputs, list) or len(raw_outputs) < 2:
        raise RipError("Batch validation must contain at least two outputs")
    outputs: list[BatchValidationOutput] = []
    seen_indexes: set[int] = set()
    seen_names: set[str] = set()
    for raw in raw_outputs:
        if not isinstance(raw, dict):
            raise RipError("Batch validation output structure is invalid")
        output_name = raw.get("expected_output_name")
        if not isinstance(output_name, str):
            raise RipError("Batch validation output name is invalid")
        title_index = _required_int(raw, "title_index")
        folded_name = output_name.casefold()
        if title_index in seen_indexes or folded_name in seen_names:
            raise RipError("Batch validation outputs must be unique")
        seen_indexes.add(title_index)
        seen_names.add(folded_name)
        outputs.append(
            BatchValidationOutput(
                title_index=title_index,
                duration_seconds=_required_int(raw, "duration_seconds"),
                estimated_bytes=_required_int(
                    raw,
                    "estimated_bytes",
                    minimum=1,
                ),
                expected_output_name=output_name,
            )
        )
    if [item.title_index for item in outputs] != sorted(seen_indexes):
        raise RipError("Batch validation outputs must use stable title order")

    relative_staging_dir = payload.get("relative_staging_dir")
    relative = (
        Path(relative_staging_dir) if isinstance(relative_staging_dir, str) else Path()
    )
    if (
        not isinstance(relative_staging_dir, str)
        or relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) != 2
        or relative.parts[0] != "batch-validation"
    ):
        raise RipError("Batch validation staging directory is unsafe")

    limitations = payload.get("limitations")
    if not isinstance(limitations, list) or not all(
        isinstance(item, str) for item in limitations
    ):
        raise RipError("Batch validation limitations are invalid")

    def digest_field(field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str):
            raise RipError(f"Batch validation {field} is invalid")
        return _checked_digest(value)

    estimated_bytes = _required_int(payload, "estimated_bytes", minimum=1)
    if estimated_bytes != sum(item.estimated_bytes for item in outputs):
        raise RipError("Batch validation total size does not match its outputs")
    return BatchValidationManifest(
        mode="single-open-makemkv-physical-validation-plan",
        source_inventory_sha256=digest_field("source_inventory_sha256"),
        inventory_signature_sha256=digest_field("inventory_signature_sha256"),
        drive_index=_required_int(payload, "drive_index"),
        selector="all",
        minimum_length_seconds=_required_int(
            payload,
            "minimum_length_seconds",
        ),
        relative_staging_dir=relative_staging_dir,
        estimated_bytes=estimated_bytes,
        execution_authorized=False,
        expected_outputs=tuple(outputs),
        limitations=tuple(limitations),
    )


def _fresh_inventory_titles(path: Path) -> tuple[BatchInventoryTitle, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_titles = payload["titles"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError) as exc:
        raise RipError(
            f"Fresh saved inventory could not be read: {type(exc).__name__}"
        ) from exc
    if not isinstance(raw_titles, list):
        raise RipError("Fresh saved inventory title list is invalid")
    titles: list[BatchInventoryTitle] = []
    for raw in raw_titles:
        if not isinstance(raw, dict):
            raise RipError("Fresh saved inventory contains a malformed title")
        title = normalize_title(raw)
        if (
            title.duration_seconds is None
            or title.output_name is None
            or title.size_bytes is None
        ):
            raise RipError("Fresh saved inventory title metadata is incomplete")
        titles.append(
            BatchInventoryTitle(
                title_index=title.index,
                duration_seconds=title.duration_seconds,
                output_name=title.output_name,
            )
        )
    return tuple(titles)


def bind_batch_validation_manifest(
    manifest_path: Path,
    fresh_inventory_path: Path,
    *,
    expected_manifest_sha256: str,
) -> BoundBatchValidation:
    """Bind an exact proposal to a metadata-identical saved inventory."""

    expected_digest = _checked_digest(expected_manifest_sha256)
    actual_digest = _file_sha256(manifest_path)
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise RipError("Batch validation manifest SHA-256 does not match")
    manifest = _load_manifest(manifest_path)
    try:
        fresh_plan = plan_batch_physical_validation(fresh_inventory_path)
    except BatchValidationPlanError as exc:
        raise RipError(
            f"Fresh saved inventory failed validation: {type(exc).__name__}"
        ) from exc
    if (
        fresh_plan.inventory_signature_sha256 != manifest.inventory_signature_sha256
        or fresh_plan.drive_index != manifest.drive_index
        or fresh_plan.minimum_length_seconds != manifest.minimum_length_seconds
        or fresh_plan.expected_outputs != manifest.expected_outputs
    ):
        raise RipError(
            "Fresh saved inventory no longer matches the exact validation plan"
        )

    token = actual_digest[:12]
    jobs = tuple(
        RipJob(
            job_id=f"batch-title-{output.title_index:03d}",
            drive_index=manifest.drive_index,
            title_index=output.title_index,
            relative_output_dir=(
                f"{manifest.relative_staging_dir}/title-{output.title_index:03d}"
            ),
            estimated_bytes=output.estimated_bytes,
            output_basename=(f"batch-{token}-title-{output.title_index:03d}.mkv"),
        )
        for output in manifest.expected_outputs
    )
    try:
        plan = plan_single_open_batch(
            jobs,
            _fresh_inventory_titles(fresh_inventory_path),
        )
    except RipError as exc:
        raise RipError(
            f"Fresh inventory cannot form the exact single-open plan: "
            f"{type(exc).__name__}"
        ) from exc
    if plan.minimum_length_seconds != manifest.minimum_length_seconds:
        raise RipError("Bound single-open cutoff changed unexpectedly")
    return BoundBatchValidation(
        manifest_sha256=actual_digest,
        manifest=manifest,
        batch_plan=plan,
    )


def execute_bound_batch_validation(  # noqa: C901
    bound: BoundBatchValidation,
    *,
    executable: Path,
    output_root: Path,
    run_dir: Path,
    authorized_manifest_sha256: str,
    authorized_title_count: int,
    confirm_validation: bool,
    timeout_seconds: int = 7200,
    on_event: Callable[[str, str], None] | None = None,
    batch_runner: Callable[..., list[RipResult]] = run_single_open_batch,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
) -> list[RipResult]:
    """Execute only the exact bound plan after all local safety checks."""

    if confirm_validation is not True:
        raise RipError("Explicit batch physical-validation confirmation is required")
    authorized_digest = _checked_digest(authorized_manifest_sha256)
    if not hmac.compare_digest(authorized_digest, bound.manifest_sha256):
        raise RipError("Authorized manifest digest does not match the bound plan")
    if bound.manifest.execution_authorized is not False:
        raise RipError("Batch validation authority flag is invalid")
    jobs = bound.batch_plan.jobs
    if authorized_title_count != len(jobs):
        raise RipError(
            "Authorized title count does not match the batch validation manifest"
        )
    if timeout_seconds <= 0:
        raise RipError("Batch validation timeout must be positive")
    if not executable.is_file():
        raise RipError("MakeMKV executable was not found")
    if not output_root.is_dir():
        raise RipError("Authorized batch validation output root does not exist")
    if run_dir.exists():
        raise RipError("Dedicated batch validation run directory already exists")

    resolved_output = output_root.resolve()
    resolved_run = run_dir.resolve()
    try:
        resolved_run.relative_to(resolved_output)
    except ValueError:
        pass
    else:
        raise RipError(
            "Batch validation logs must not be stored inside the staging root"
        )

    base_staging = (resolved_output / bound.manifest.relative_staging_dir).resolve()
    try:
        base_staging.relative_to(resolved_output)
    except ValueError as exc:
        raise RipError("Batch validation staging escaped the output root") from exc
    if base_staging.exists():
        raise RipError("Batch validation staging collision; nothing was started")
    for job in jobs:
        if resolve_job_output(output_root, job).exists():
            raise RipError("Batch validation job collision; nothing was started")

    required_bytes = bound.manifest.estimated_bytes + _FREE_SPACE_RESERVE
    if disk_usage(output_root).free < required_bytes:
        raise RipError("Insufficient conservative free space for batch validation")

    run_dir.mkdir(parents=True)
    stop_file = run_dir / "STOP"
    with JsonlRipLog(run_dir / "authorization.jsonl") as audit:
        audit.write(
            "batch_validation_authorized",
            manifest_sha256=bound.manifest_sha256,
            authorized_title_count=authorized_title_count,
            minimum_length_seconds=bound.manifest.minimum_length_seconds,
            estimated_bytes=bound.manifest.estimated_bytes,
            selector="all",
        )
    if stop_file.exists():
        with JsonlRipLog(run_dir / "events.jsonl") as event_log:
            event_log.write("batch_validation_stopped_before_start")
        raise RipError("STOP requested before batch validation started")

    try:
        with JsonlRipLog(run_dir / "events.jsonl") as event_log:
            results = batch_runner(
                executable,
                output_root,
                bound.batch_plan,
                event_log,
                timeout_seconds=timeout_seconds,
                cancel_file=stop_file,
                on_event=on_event,
            )
    except Exception as exc:
        with JsonlRipLog(run_dir / "events.jsonl") as event_log:
            event_log.write(
                "batch_validation_failed",
                error_type=type(exc).__name__,
            )
        if isinstance(exc, RipError):
            raise
        raise RipError(f"Batch validation runner failed: {type(exc).__name__}") from exc

    expected_ids = {job.job_id for job in jobs}
    if (
        len(results) != len(jobs)
        or {result.job_id for result in results} != expected_ids
    ):
        raise RipError("Batch validation runner returned an incomplete result set")
    return results
