"""Build and validate path-redacted MakeMKV rip manifests."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mkv_episode_matcher.disc.batch_ripper import (
    BatchInventoryTitle,
    SingleOpenBatchPlan,
    plan_single_open_batch,
)
from mkv_episode_matcher.disc.ripper import RipError, RipJob
from mkv_episode_matcher.disc.title_selector import (
    TitlePlanError,
    load_title_plan,
    normalize_title,
    select_rippable_titles,
)


@dataclass(frozen=True)
class SkippedDisc:
    disc_id: str
    drive_index: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MediaContext:
    disc_id: str
    series_name: str
    season: int | None = None
    disc_number: int | None = None
    volume_number: int | None = None
    tmdb_id: int | None = None
    content_hint: str | None = None
    handbrake_profile_id: str | None = None
    staging_attempt: str | None = None
    selected_title_indexes: tuple[int, ...] | None = None
    special_feature_catalog_id: str | None = None
    special_feature_release_id: str | None = None
    special_feature_library_title: str | None = None
    special_feature_library_year: int | None = None
    special_feature_assignments: tuple[dict[str, object], ...] = ()
    episode_assignments: tuple[dict[str, object], ...] = ()
    existing_output_policy: str = "preserve"


def media_context_from_dict(value: dict[str, object]) -> MediaContext:
    """Restore immutable tuple fields from a serialized private context."""

    normalized = dict(value)
    indexes = normalized.get("selected_title_indexes")
    if indexes is not None:
        if not isinstance(indexes, list | tuple):
            raise RipError("Media context selected title indexes are invalid")
        normalized["selected_title_indexes"] = tuple(indexes)
    assignments = normalized.get("special_feature_assignments", ())
    if not isinstance(assignments, list | tuple) or not all(
        isinstance(item, dict) for item in assignments
    ):
        raise RipError("Media context special-feature assignments are invalid")
    normalized["special_feature_assignments"] = tuple(assignments)
    episode_assignments = normalized.get("episode_assignments", ())
    if not isinstance(episode_assignments, list | tuple) or not all(
        isinstance(item, dict) for item in episode_assignments
    ):
        raise RipError("Media context episode assignments are invalid")
    normalized["episode_assignments"] = tuple(episode_assignments)
    if normalized.get("existing_output_policy", "preserve") not in {
        "preserve",
        "missing-only",
        "replace-after-verification",
    }:
        raise RipError("Media context existing-output policy is invalid")
    try:
        return MediaContext(**normalized)
    except TypeError as exc:
        raise RipError("Media context structure is invalid") from exc


@dataclass(frozen=True)
class RipDiscProof:
    """Path-redacted proof used to rebind a fresh inventory at execution."""

    disc_id: str
    drive_index: int
    inventory_signature_sha256: str
    selected_title_indexes: tuple[int, ...]
    batch_eligible: bool
    minimum_length_seconds: int | None = None


@dataclass(frozen=True)
class RipManifest:
    mode: str
    created_at: str
    jobs: tuple[RipJob, ...]
    skipped_discs: tuple[SkippedDisc, ...]
    media_contexts: tuple[MediaContext, ...] = ()
    disc_proofs: tuple[RipDiscProof, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "created_at": self.created_at,
            "jobs": [asdict(job) for job in self.jobs],
            "skipped_discs": [asdict(item) for item in self.skipped_discs],
            "media_contexts": [asdict(context) for context in self.media_contexts],
            "disc_proofs": [asdict(proof) for proof in self.disc_proofs],
        }


def _load_inventory(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RipError(
            f"Could not read a preflight inventory: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise RipError("Preflight inventory must contain a JSON object")
    if payload.get("minimum_length_seconds") != 0:
        raise RipError(
            "Executable rip planning requires a zero-minimum inventory so "
            "MakeMKV title indexes cannot shift"
        )
    return payload


def _drive_index(payload: dict[str, Any]) -> int:
    drive = payload.get("drive")
    if not isinstance(drive, dict):
        raise RipError("Preflight inventory has no drive metadata")
    try:
        index = int(drive["index"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RipError("Preflight inventory has no valid drive index") from exc
    if not 0 <= index <= 99:
        raise RipError("Preflight inventory drive index is outside the safe range")
    return index


def _inventory_fingerprint(payload: dict[str, Any]) -> str:
    """Return a stable identifier without retaining a disc label."""

    drive = payload.get("drive", {})
    titles = payload.get("titles", [])
    identity = {
        "disc_name": drive.get("disc_name") if isinstance(drive, dict) else None,
        "titles": [
            {
                "index": title.get("index"),
                "duration": title.get("attributes", {}).get("9"),
                "size": title.get("attributes", {}).get("11"),
            }
            for title in titles
            if isinstance(title, dict)
        ],
    }
    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _canonical_sha256(value: Any) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _batch_inventory_material(
    payload: dict[str, Any],
) -> tuple[str, tuple[BatchInventoryTitle, ...]]:
    raw_titles = payload.get("titles")
    if not isinstance(raw_titles, list) or not raw_titles:
        raise RipError("Saved inventory has no complete title list")

    identities: list[dict[str, object]] = []
    inventory_titles: list[BatchInventoryTitle] = []
    seen_indexes: set[int] = set()
    for raw in raw_titles:
        if not isinstance(raw, dict):
            raise RipError("Saved inventory contains malformed title metadata")
        title = normalize_title(raw)
        if (
            title.index < 0
            or title.index in seen_indexes
            or title.duration_seconds is None
            or title.duration_seconds < 0
            or title.size_bytes is None
            or title.size_bytes <= 0
            or not isinstance(title.output_name, str)
        ):
            raise RipError("Saved inventory lacks complete batch title metadata")
        seen_indexes.add(title.index)
        identities.append({
            "title_index": title.index,
            "duration_seconds": title.duration_seconds,
            "estimated_bytes": title.size_bytes,
            "output_name": title.output_name,
        })
        inventory_titles.append(
            BatchInventoryTitle(
                title_index=title.index,
                duration_seconds=title.duration_seconds,
                output_name=title.output_name,
            )
        )
    identities.sort(key=lambda item: int(item["title_index"]))
    inventory_titles.sort(key=lambda item: item.title_index)
    return _canonical_sha256(identities), tuple(inventory_titles)


def _build_disc_proof(
    payload: dict[str, Any],
    *,
    disc_id: str,
    drive_index: int,
    jobs: tuple[RipJob, ...],
) -> RipDiscProof | None:
    try:
        signature, inventory_titles = _batch_inventory_material(payload)
    except RipError:
        return None
    try:
        plan = plan_single_open_batch(jobs, inventory_titles)
    except RipError:
        return RipDiscProof(
            disc_id=disc_id,
            drive_index=drive_index,
            inventory_signature_sha256=signature,
            selected_title_indexes=tuple(sorted(job.title_index for job in jobs)),
            batch_eligible=False,
        )
    return RipDiscProof(
        disc_id=disc_id,
        drive_index=drive_index,
        inventory_signature_sha256=signature,
        selected_title_indexes=tuple(job.title_index for job in plan.jobs),
        batch_eligible=True,
        minimum_length_seconds=plan.minimum_length_seconds,
    )


def _safe_media_component(value: str) -> str:
    cleaned = value.replace(":", " -")
    cleaned = re.sub(r'[<>"/\\|?*\x00-\x1f]', " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        raise RipError("Media context series name is empty after sanitization")
    return cleaned[:100]


def _disc_label_slug(payload: dict[str, Any]) -> str | None:
    """Return a readable, filename-safe label without weakening job identity."""

    drive = payload.get("drive")
    value = drive.get("disc_name") if isinstance(drive, dict) else None
    if not isinstance(value, str):
        return None
    words = re.findall(r"[A-Za-z0-9]+", value)
    if not words:
        return None
    pretty = "-".join(
        word if word.isdigit() or re.fullmatch(r"[IVXLCDM]+", word) else word.title()
        for word in words
    )
    return pretty[:80].rstrip("-") or None


def load_media_contexts(path: Path) -> dict[str, MediaContext]:  # noqa: C901
    """Load a reviewed, credential-free disc-to-series mapping."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RipError(f"Could not read media context: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise RipError("Media context must contain a JSON object")

    contexts: dict[str, MediaContext] = {}
    for disc_id, value in payload.items():
        if not isinstance(disc_id, str) or not isinstance(value, dict):
            raise RipError("Media context entries must map disc IDs to objects")
        try:
            context = media_context_from_dict({"disc_id": disc_id, **value})
        except TypeError as exc:
            raise RipError(f"Media context for {disc_id} is invalid") from exc
        if re.fullmatch(r"disc-\d{2}", context.disc_id) is None:
            raise RipError("Media context contains an invalid disc ID")
        if not context.series_name.strip():
            raise RipError(f"Media context for {disc_id} has no series name")
        if context.season is not None and not 0 <= context.season <= 99:
            raise RipError(f"Media context for {disc_id} has an invalid season")
        if context.content_hint not in {None, "tv", "movie", "extras", "mixed"}:
            raise RipError(f"Media context for {disc_id} has an invalid content hint")
        if (
            context.handbrake_profile_id is not None
            and re.fullmatch(r"[a-z][a-z0-9-]{1,47}", context.handbrake_profile_id)
            is None
        ):
            raise RipError(
                f"Media context for {disc_id} has an invalid HandBrake profile"
            )
        if context.selected_title_indexes is not None and (
            not context.selected_title_indexes
            or any(
                not isinstance(index, int) or isinstance(index, bool) or index < 0
                for index in context.selected_title_indexes
            )
            or len(set(context.selected_title_indexes))
            != len(context.selected_title_indexes)
        ):
            raise RipError(
                f"Media context for {disc_id} has invalid selected title indexes"
            )
        for field_name, value_number in (
            ("disc number", context.disc_number),
            ("volume number", context.volume_number),
            ("TMDb ID", context.tmdb_id),
        ):
            if value_number is not None and value_number <= 0:
                raise RipError(
                    f"Media context for {disc_id} has an invalid {field_name}"
                )
        contexts[disc_id] = context
    return contexts


def _context_final_dir(context: MediaContext) -> str | None:
    if context.special_feature_catalog_id is not None or context.content_hint in {
        "movie",
        "extras",
        "mixed",
    }:
        return None
    series = _safe_media_component(context.series_name)
    if context.season is not None:
        return f"TV Shows/{series}/Season {context.season:02d}"
    return f"TV Shows/{series}/Unmatched"


def build_rip_manifest(  # noqa: C901
    report_paths: list[Path],
    media_contexts: dict[str, MediaContext] | None = None,
) -> RipManifest:
    """Select reviewed episode or conservative bonus titles from saved data."""

    if not report_paths:
        raise RipError("At least one preflight inventory is required")

    seen_drives: set[int] = set()
    jobs: list[RipJob] = []
    skipped: list[SkippedDisc] = []
    disc_proofs: list[RipDiscProof] = []

    for ordinal, report_path in enumerate(report_paths, start=1):
        payload = _load_inventory(report_path)
        drive_index = _drive_index(payload)
        if drive_index in seen_drives:
            raise RipError(
                "Multiple inventories refer to the same drive; refresh the "
                "preflight set before planning"
            )
        seen_drives.add(drive_index)

        disc_id = f"disc-{ordinal:02d}"
        fingerprint = _inventory_fingerprint(payload)
        disc_label_slug = _disc_label_slug(payload)
        try:
            plan = load_title_plan(report_path, report_id=disc_id)
        except TitlePlanError as exc:
            skipped.append(
                SkippedDisc(
                    disc_id=disc_id,
                    drive_index=drive_index,
                    reasons=(f"planning-error:{type(exc).__name__}",),
                )
            )
            continue

        context = media_contexts.get(disc_id) if media_contexts else None
        selected = list(select_rippable_titles(plan))
        if context is not None and context.selected_title_indexes is not None:
            decisions_by_index = {
                decision.title.index: decision for decision in plan.decisions
            }
            missing = set(context.selected_title_indexes) - decisions_by_index.keys()
            if missing:
                raise RipError("Media context selects a title absent from inventory")
            selected = [
                decisions_by_index[index] for index in context.selected_title_indexes
            ]
        reasons: list[str] = []
        if not selected:
            reasons.append("no-readable-media-titles")
        if reasons:
            skipped.append(
                SkippedDisc(
                    disc_id=disc_id,
                    drive_index=drive_index,
                    reasons=tuple(reasons),
                )
            )
            continue

        disc_jobs: list[RipJob] = []
        for decision in selected:
            title = decision.title
            job_id = f"{disc_id}-title-{title.index:03d}"
            staging_root = f".staging/{disc_id}"
            if context is not None and context.staging_attempt is not None:
                if re.fullmatch(r"[a-z0-9-]{8,40}", context.staging_attempt) is None:
                    raise RipError("Media context staging attempt is invalid")
                staging_root = f"{staging_root}/{context.staging_attempt}"
            disc_jobs.append(
                RipJob(
                    job_id=job_id,
                    drive_index=drive_index,
                    title_index=title.index,
                    relative_output_dir=(
                        f"{staging_root}/{fingerprint}/title-{title.index:03d}"
                    ),
                    estimated_bytes=title.size_bytes,
                    output_basename=(f"{disc_label_slug}--" if disc_label_slug else "")
                    + (f"{disc_id}-{fingerprint}-title-{title.index:03d}.mkv"),
                    final_relative_dir=(
                        _context_final_dir(context) if context is not None else None
                    ),
                )
            )
        jobs.extend(disc_jobs)
        proof = _build_disc_proof(
            payload,
            disc_id=disc_id,
            drive_index=drive_index,
            jobs=tuple(disc_jobs),
        )
        if proof is not None:
            disc_proofs.append(proof)

    if not jobs:
        raise RipError("No readable media titles are available to rip")
    selected_disc_ids = {job.job_id.rsplit("-title-", 1)[0] for job in jobs}
    if media_contexts is not None:
        missing = selected_disc_ids - media_contexts.keys()
        if missing:
            raise RipError(
                "Media context is missing selected disc(s): "
                + ", ".join(sorted(missing))
            )

    return RipManifest(
        mode="approved-rip-plan",
        created_at=datetime.now(UTC).isoformat(),
        jobs=tuple(jobs),
        skipped_discs=tuple(skipped),
        media_contexts=tuple(
            media_contexts[disc_id] for disc_id in sorted(selected_disc_ids)
        )
        if media_contexts
        else (),
        disc_proofs=tuple(disc_proofs),
    )


def write_rip_manifest(path: Path, manifest: RipManifest) -> Path:
    if path.exists():
        raise RipError("Rip manifest already exists; refusing overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def load_rip_manifest(path: Path) -> RipManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RipError(f"Could not read rip manifest: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or payload.get("mode") != "approved-rip-plan":
        raise RipError("File is not an approved rip manifest")

    try:
        jobs = tuple(RipJob(**item) for item in payload["jobs"])
        skipped = tuple(SkippedDisc(**item) for item in payload["skipped_discs"])
        contexts = tuple(
            media_context_from_dict(item) for item in payload.get("media_contexts", [])
        )
        proofs = tuple(
            RipDiscProof(**{
                **item,
                "selected_title_indexes": tuple(item["selected_title_indexes"]),
            })
            for item in payload.get("disc_proofs", [])
        )
        created_at = str(payload["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RipError("Rip manifest structure is invalid") from exc

    if not jobs:
        raise RipError("Approved rip manifest contains no jobs")
    for job in jobs:
        from mkv_episode_matcher.disc.ripper import validate_job

        validate_job(job)
    proof_drives: set[int] = set()
    for proof in proofs:
        if (
            re.fullmatch(r"disc-\d{2}", proof.disc_id) is None
            or not 0 <= proof.drive_index <= 99
            or proof.drive_index in proof_drives
            or len(proof.inventory_signature_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in proof.inventory_signature_sha256
            )
            or not proof.selected_title_indexes
            or len(set(proof.selected_title_indexes))
            != len(proof.selected_title_indexes)
            or (proof.batch_eligible and proof.minimum_length_seconds is None)
            or (not proof.batch_eligible and proof.minimum_length_seconds is not None)
        ):
            raise RipError("Rip manifest disc proof is invalid")
        proof_drives.add(proof.drive_index)
        job_indexes = {
            job.title_index for job in jobs if job.drive_index == proof.drive_index
        }
        if set(proof.selected_title_indexes) != job_indexes:
            raise RipError("Rip manifest disc proof does not match its jobs")
    return RipManifest(
        mode="approved-rip-plan",
        created_at=created_at,
        jobs=jobs,
        skipped_discs=skipped,
        media_contexts=contexts,
        disc_proofs=proofs,
    )


def bind_fresh_batch_plans(
    manifest: RipManifest,
    fresh_inventory_paths: list[Path],
) -> dict[int, SingleOpenBatchPlan]:
    """Bind eligible manifest drives to explicit metadata-identical reports."""

    if not fresh_inventory_paths:
        return {}
    proofs = {proof.drive_index: proof for proof in manifest.disc_proofs}
    if not proofs:
        raise RipError("This older rip manifest has no fresh-inventory binding proofs")

    plans: dict[int, SingleOpenBatchPlan] = {}
    seen_drives: set[int] = set()
    for path in fresh_inventory_paths:
        payload = _load_inventory(path)
        drive_index = _drive_index(payload)
        if drive_index in seen_drives:
            raise RipError("Fresh inventories contain a duplicate drive")
        seen_drives.add(drive_index)
        proof = proofs.get(drive_index)
        if proof is None:
            raise RipError("Fresh inventory drive is absent from the rip manifest")
        signature, inventory_titles = _batch_inventory_material(payload)
        if not hmac.compare_digest(
            signature,
            proof.inventory_signature_sha256,
        ):
            raise RipError(
                "Fresh inventory no longer matches the reviewed rip manifest"
            )
        if not proof.batch_eligible:
            continue
        jobs = tuple(job for job in manifest.jobs if job.drive_index == drive_index)
        plan = plan_single_open_batch(jobs, inventory_titles)
        if (
            tuple(job.title_index for job in plan.jobs) != proof.selected_title_indexes
            or plan.minimum_length_seconds != proof.minimum_length_seconds
        ):
            raise RipError("Fresh inventory changed the reviewed batch selection")
        plans[drive_index] = plan
    return plans
